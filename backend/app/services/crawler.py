import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from app.config import get_settings
from app.services.security import validate_public_url
from app.services.text import tokens

settings = get_settings()


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except Exception:
        return default


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


# Adaptive rendered-page hydration.  There is intentionally NO pixel-height
# ceiling and NO fixed number of scroll positions.  Infinite/ever-growing feeds
# are bounded only by elapsed wall-clock time, and are explicitly marked
# incomplete when that safety budget is reached.
PAGE_HYDRATION_STABLE_ROUNDS = _env_int("PAGE_HYDRATION_STABLE_ROUNDS", 2, 1)
PAGE_HYDRATION_MAX_ELAPSED_MS = _env_int("PAGE_HYDRATION_MAX_ELAPSED_MS", 120000, 5000)
PAGE_HYDRATION_SETTLE_MS = _env_int("PAGE_HYDRATION_SETTLE_MS", 700, 100)
PAGE_HYDRATION_SCROLL_WAIT_MS = _env_int("PAGE_HYDRATION_SCROLL_WAIT_MS", 120, 25)
PAGE_HYDRATION_STEP_RATIO = _env_float("PAGE_HYDRATION_STEP_RATIO", 0.72, 0.25, 0.95)

# Bump whenever the persisted extraction payload shape/semantics change enough that
# existing PageCache rows should be refreshed automatically.
PAGE_STRUCTURE_VERSION = 9

# HTTP states that are usually transient and worth retrying with the same browser
# strategy before declaring the page unavailable.
_RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}

# HTTP states that indicate a page is genuinely gone rather than bot-blocked.
_STALE_HTTP = {404, 410}

# States that commonly represent a WAF/CDN/browser-fingerprint block.  These should
# receive one bounded alternative fetch attempt instead of being returned immediately.
_FALLBACK_HTTP = {401, 403, 406}

_HARD_CHALLENGE_PATTERNS = [
    ("access_denied", re.compile(r"\baccess denied\b", re.I)),
    ("forbidden", re.compile(r"\b(?:403\s+)?forbidden\b", re.I)),
    (
        "verify_human",
        re.compile(r"\b(?:verify|confirm) (?:that )?you are (?:a )?human\b", re.I),
    ),
    (
        "security_check",
        re.compile(r"\b(?:security check|checking your browser|challenge-platform)\b", re.I),
    ),
    (
        "bot_block",
        re.compile(
            r"\b(?:request blocked|bot detected|automated requests|unusual traffic)\b",
            re.I,
        ),
    ),
    ("cloudflare", re.compile(r"\b(?:cloudflare ray id|attention required|just a moment)\b", re.I)),
    ("akamai", re.compile(r"\breference\s*#\s*\d+\.[a-f0-9.]+\b", re.I)),
]
_CAPTCHA_PATTERN = re.compile(r"\b(?:captcha|recaptcha|hcaptcha)\b", re.I)


def _parse_retry_after(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            return None
    return max(0.0, min(float(settings.sitemap_http_max_retry_after_seconds), seconds))



HYDRATE_JS = r"""
async (options) => {
  const opts = options || {};
  const stableRoundsRequired = Math.max(1, Number(opts.stable_rounds || 2));
  const maxElapsedMs = Math.max(5000, Number(opts.max_elapsed_ms || 90000));
  const settleMs = Math.max(100, Number(opts.settle_ms || 700));
  const perScrollWaitMs = Math.max(25, Number(opts.per_scroll_wait_ms || 120));
  const stepRatio = Math.min(0.95, Math.max(0.25, Number(opts.step_ratio || 0.72)));

  const scrollingElement = () =>
    document.scrollingElement || document.documentElement || document.body;

  const pageHeight = () => Math.max(
    document.body?.scrollHeight || 0,
    document.documentElement?.scrollHeight || 0,
    scrollingElement()?.scrollHeight || 0
  );

  const renderedTextLength = () => (document.body?.innerText || '').length;

  const startX = window.scrollX;
  const startY = window.scrollY;
  const startedAt = performance.now();

  let rounds = 0;
  let totalScrollSteps = 0;
  let stableRounds = 0;
  let previousHeight = 0;
  let previousTextLength = 0;
  let complete = false;
  let stopReason = 'unknown';
  let observedGrowth = false;
  const initialHeight = pageHeight();
  const initialTextLength = renderedTextLength();
  let lastHeight = initialHeight;
  let lastTextLength = initialTextLength;

  // IMPORTANT:
  // There is deliberately NO fixed pixel-height limit and NO fixed number of
  // scroll positions. Each sweep keeps extending its target whenever the page
  // grows. This handles ordinary lazy loading and long pages correctly.
  //
  // Truly infinite feeds can never be "fully" crawled. For those pages the only
  // safe termination condition is a wall-clock budget. When the budget is hit
  // we return complete=false so downstream code never silently claims the page
  // was exhaustively hydrated.
  outer:
  while (true) {
    rounds += 1;

    let targetHeight = pageHeight();
    const viewport = Math.max(window.innerHeight || 0, 640);
    const stepPx = Math.max(320, Math.floor(viewport * stepRatio));
    let y = 0;

    while (y < targetHeight) {
      window.scrollTo(0, y);
      totalScrollSteps += 1;
      await new Promise(resolve => setTimeout(resolve, perScrollWaitMs));

      const grownHeight = pageHeight();
      if (grownHeight > targetHeight + 8) {
        targetHeight = grownHeight;
        observedGrowth = true;
      }

      if (performance.now() - startedAt >= maxElapsedMs) {
        stopReason = 'time_budget';
        break outer;
      }

      y += stepPx;
    }

    // Always touch the current true bottom. Some IntersectionObserver / lazy
    // loaders only fire when the bottom sentinel enters the viewport.
    const bottom = pageHeight();
    window.scrollTo(0, bottom);
    await new Promise(resolve => setTimeout(resolve, settleMs));

    const heightAfter = pageHeight();
    const textLengthAfter = renderedTextLength();

    if (heightAfter > targetHeight + 8) {
      observedGrowth = true;
    }

    const heightStable = Math.abs(heightAfter - previousHeight) < 16;
    const textTolerance = Math.max(32, Math.floor(Math.max(previousTextLength, 1) * 0.002));
    const textStable = Math.abs(textLengthAfter - previousTextLength) <= textTolerance;

    if (heightStable && textStable) {
      stableRounds += 1;
    } else {
      stableRounds = 0;
    }

    previousHeight = heightAfter;
    previousTextLength = textLengthAfter;
    lastHeight = heightAfter;
    lastTextLength = textLengthAfter;

    if (stableRounds >= stableRoundsRequired) {
      complete = true;
      stopReason = 'stable';
      break;
    }

    if (performance.now() - startedAt >= maxElapsedMs) {
      stopReason = 'time_budget';
      break;
    }
  }

  window.scrollTo(startX, startY);
  await new Promise(resolve => setTimeout(resolve, 120));

  return {
    rounds,
    total_scroll_steps: totalScrollSteps,
    initial_height: initialHeight,
    initial_text_length: initialTextLength,
    final_height: pageHeight(),
    final_text_length: renderedTextLength(),
    stable_rounds: stableRounds,
    complete,
    stop_reason: stopReason,
    observed_growth: observedGrowth,
    elapsed_ms: Math.round(performance.now() - startedAt),
    configured_max_elapsed_ms: maxElapsedMs,
    configured_stable_rounds: stableRoundsRequired,
    configured_step_ratio: stepRatio,
    configured_settle_ms: settleMs,
    configured_per_scroll_wait_ms: perScrollWaitMs,
  };
}
"""

EXTRACT_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  function rawWordCount(el) {
    if (!el) return 0;
    const text = clean(el.innerText || el.textContent || '');
    if (!text) return 0;
    return text.split(/\s+/).filter(Boolean).length;
  }

  // Browser-rendered text is the canonical Page Analysis source.  Use BODY as
  // the scope and exclude navigation/global chrome below.  Choosing the first
  // <main> caused real content outside that container to disappear; parsing
  // page.content() first caused hidden responsive/carousel copies to be counted.
  const body = document.body;
  const semanticFallback =
    document.querySelector('main') ||
    document.querySelector('article') ||
    document.querySelector("[role='main']");
  const root = body || semanticFallback;
  const rootName = body ? 'body' : semanticFallback ? 'semantic-fallback' : null;

  if (!root) return {
    root: null,
    title: clean(document.title),
    meta_description: '',
    canonical_url: '',
    main_text: '',
    headings: [],
    sections: [],
    faqs: [],
    paragraph_count: 0,
    h1: [], h2: [], h3: [], h4: [], h5: [], h6: []
  };

  const ignoredSelector = [
    'script','style','noscript','svg','canvas','template','iframe',
    'header','footer','nav','aside','form','button',
    '[role="navigation"]','[role="banner"]','[role="contentinfo"]','[role="dialog"]',
    '[aria-hidden="true"]','[inert]','[hidden]',
    '.cookie','.cookies','.cookie-banner','.cookie-consent','#cookie','#cookies',
    '.modal','.popup','.newsletter','.login','.signup','.sign-up','.social-share',
    '.sr-only','.visually-hidden','.screen-reader-text'
  ].join(',');

  function isVisible(el) {
    if (!el) return false;
    let current = el;
    while (current && current !== root.parentElement) {
      if (current.matches && current.matches(ignoredSelector)) return false;

      // Chromium's visibility API catches display:none, content-visibility:hidden,
      // visibility:hidden/collapse and opacity:0 without excluding ordinary off-screen content.
      if (typeof current.checkVisibility === 'function') {
        try {
          if (!current.checkVisibility({
            // Do NOT enable contentVisibilityAuto here. Off-screen
            // content-visibility:auto sections are still legitimate whole-page
            // content and must be counted for SEO density when the user scrolls.
            opacityProperty: true,
            visibilityProperty: true,
          })) return false;
        } catch (_) {}
      }

      const style = window.getComputedStyle(current);
      if (
        style.display === 'none' ||
        style.visibility === 'hidden' ||
        style.visibility === 'collapse' ||
        Number.parseFloat(style.opacity || '1') === 0 ||
        style.contentVisibility === 'hidden' ||
        current.hasAttribute('hidden')
      ) return false;

      current = current.parentElement;
    }
    return true;
  }

  function visibleText(el) {
    if (!el || !isVisible(el)) return '';
    const clone = el.cloneNode(true);
    clone.querySelectorAll(ignoredSelector).forEach(n => n.remove());
    return clean(clone.innerText || clone.textContent || '');
  }

  const title = clean(document.title);
  const metaDescription = clean(
    document.querySelector('meta[name="description"]')?.getAttribute('content') || ''
  );
  const canonicalUrl = clean(
    document.querySelector('link[rel="canonical"]')?.getAttribute('href') || ''
  );

  const browserEnvironment = {
    browser_mode: window.__SEO_ANALYSIS_BROWSER_MODE__ || '',
    user_agent: navigator.userAgent || '',
    language: navigator.language || '',
    viewport: `${window.innerWidth || 0}x${window.innerHeight || 0}`,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
  };

  const headingElements = Array.from(
    root.querySelectorAll('h1,h2,h3,h4,h5,h6')
  ).filter(isVisible);

  const headings = headingElements.map((el, index) => ({
    order: index + 1,
    level: Number(el.tagName.substring(1)),
    text: clean(el.innerText || el.textContent || ''),
    id: clean(el.id || ''),
  })).filter(x => x.text);

  const sectionMap = new Map();
  headings.forEach((h, i) => sectionMap.set(i, {
    order: h.order,
    heading_level: h.level,
    heading: h.text,
    textParts: [],
  }));

  const preamble = {
    order: 0,
    heading_level: 0,
    heading: 'Page introduction',
    textParts: []
  };

  let currentSection = preamble;
  let headingCursor = -1;

  const walker = document.createTreeWalker(
    root,
    NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT
  );

  while (walker.nextNode()) {
    const node = walker.currentNode;

    if (node.nodeType === Node.ELEMENT_NODE) {
      const el = node;
      if ((el.matches && el.matches(ignoredSelector)) || !isVisible(el)) continue;

      if (/^H[1-6]$/.test(el.tagName)) {
        const text = clean(el.innerText || el.textContent || '');
        if (text) {
          headingCursor += 1;
          currentSection = sectionMap.get(headingCursor) || currentSection;
        }
      }
      continue;
    }

    const parent = node.parentElement;
    if (
      !parent ||
      !isVisible(parent) ||
      parent.closest(ignoredSelector) ||
      parent.closest('h1,h2,h3,h4,h5,h6')
    ) continue;

    const value = clean(node.nodeValue || '');
    if (value) currentSection.textParts.push(value);
  }

  function sectionObject(s) {
    const text = clean(s.textParts.join(' '));
    const wordCount = text ? text.split(/\s+/).filter(Boolean).length : 0;
    return {
      order: s.order,
      heading_level: s.heading_level,
      heading: s.heading,
      text,
      word_count: wordCount
    };
  }

  const sections = [];
  const intro = sectionObject(preamble);
  if (intro.text) sections.push(intro);

  for (let i = 0; i < headings.length; i++) {
    const item = sectionObject(sectionMap.get(i));
    if (item.heading || item.text) sections.push(item);
  }

  const faqs = [];
  const seenFaq = new Set();

  function pushFaq(question, answer, source) {
    question = clean(question);
    answer = clean(answer);
    if (!question) return;

    const key = question.toLowerCase();
    if (seenFaq.has(key)) return;

    seenFaq.add(key);
    faqs.push({
      order: faqs.length + 1,
      question,
      answer,
      answer_word_count: answer ? answer.split(/\s+/).filter(Boolean).length : 0,
      source
    });
  }

  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const payload = JSON.parse(script.textContent || 'null');
      const stack = Array.isArray(payload) ? [...payload] : [payload];

      while (stack.length) {
        const item = stack.pop();
        if (!item || typeof item !== 'object') continue;

        if (item['@type'] === 'FAQPage' && Array.isArray(item.mainEntity)) {
          for (const entity of item.mainEntity) {
            if (entity && entity['@type'] === 'Question') {
              pushFaq(
                entity.name || '',
                entity.acceptedAnswer?.text || entity.suggestedAnswer?.text || '',
                'FAQPage JSON-LD'
              );
            }
          }
        }

        if (Array.isArray(item['@graph'])) stack.push(...item['@graph']);
      }
    } catch (_) {}
  }

  for (const details of root.querySelectorAll('details')) {
    if (!isVisible(details)) continue;
    const summary = details.querySelector('summary');
    if (!summary) continue;

    const clone = details.cloneNode(true);
    clone.querySelector('summary')?.remove();
    pushFaq(visibleText(summary), visibleText(clone), 'details/summary');
  }

  for (const q of root.querySelectorAll(
    '[itemtype*="schema.org/Question"], [itemtype*="Question"]'
  )) {
    if (!isVisible(q)) continue;
    pushFaq(
      visibleText(q.querySelector('[itemprop="name"]')),
      visibleText(
        q.querySelector('[itemprop="acceptedAnswer"], [itemprop="suggestedAnswer"]')
      ),
      'Question microdata'
    );
  }

  for (const s of sections) {
    if (s.heading && s.heading.endsWith('?') && s.text) {
      pushFaq(s.heading, s.text, 'heading heuristic');
    }
  }

  // IMPORTANT: keyword-density corpus intentionally follows the browser's
  // rendered BODY text rather than the editorial section subset.  Chrome Find
  // searches the rendered document, including header/footer/button text that is
  // actually rendered.  document.body.innerText is the browser-native rendered
  // text representation and avoids raw-DOM responsive/template duplicates.
  // Structural sections below remain content-focused for headings/FAQ analysis.
  const mainText = clean(
    document.body?.innerText || root.innerText || root.textContent || ''
  );

  const byLevel = level => headings
    .filter(h => h.level === level)
    .map(h => h.text);

  return {
    root: rootName,
    title,
    meta_description: metaDescription,
    canonical_url: canonicalUrl,
    main_text: mainText,
    headings,
    sections,
    faqs,
    paragraph_count: Array.from(root.querySelectorAll('p')).filter(isVisible).length,
    h1: byLevel(1),
    h2: byLevel(2),
    h3: byLevel(3),
    h4: byLevel(4),
    h5: byLevel(5),
    h6: byLevel(6),
    browser_environment: browserEnvironment,
  };
}
"""



def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


_STATIC_REMOVE_SELECTOR = (
    "script,style,noscript,svg,canvas,template,iframe,header,footer,nav,aside,"
    "form,button,[role='navigation'],[role='banner'],[role='contentinfo'],"
    "[role='dialog'],[aria-hidden='true'],.cookie,.cookies,.cookie-banner,"
    ".cookie-consent,#cookie,#cookies,.modal,.popup,.newsletter,.login,.signup,"
    ".sign-up,.social-share"
)


def _clean_static_candidate(node):
    """Clone and remove obvious global chrome/non-content nodes for scoring."""
    if node is None:
        return None
    cloned = BeautifulSoup(str(node), "lxml")
    root = cloned.find(getattr(node, "name", None)) or cloned
    for junk in root.select(_STATIC_REMOVE_SELECTOR):
        junk.decompose()
    return root


def _static_candidate_metrics(name: str, node) -> dict:
    cleaned = _clean_static_candidate(node)
    if cleaned is None:
        return {
            "name": name,
            "words": 0,
            "headings": 0,
            "paragraphs": 0,
            "score": 0.0,
        }

    text = _clean_text(cleaned.get_text(" ", strip=True))
    words = len(tokens(text))
    headings = len(cleaned.find_all(re.compile(r"^h[1-6]$", re.I)))
    paragraphs = len(cleaned.find_all("p"))

    # Words dominate. Headings and paragraphs break ties in favour of roots that
    # retain real editorial structure instead of shells/application wrappers.
    score = float(words + headings * 24 + min(paragraphs, 500) * 1.5)
    return {
        "name": name,
        "words": words,
        "headings": headings,
        "paragraphs": paragraphs,
        "score": round(score, 2),
    }


def _choose_static_root(candidates: list[tuple[str, object]]) -> tuple[tuple[str, object] | None, dict]:
    """Choose the content root using cleaned evidence rather than first-match DOM order.

    A common failure mode on enterprise/SPAs is a <main> that contains only the
    above-the-fold application shell while the long-form SEO copy lives in sibling
    containers.  Selecting the first semantic root can then under-count thousands of
    words and miss every H1/H2.  We therefore compare cleaned semantic roots with the
    cleaned body and prefer body whenever it materially preserves more editorial
    content/heading evidence.
    """
    if not candidates:
        return None, {"reason": "no_root_candidates", "candidates": []}

    metrics = {
        name: _static_candidate_metrics(name, node)
        for name, node in candidates
    }
    node_by_name = {name: node for name, node in candidates}

    semantic_names = [name for name, _ in candidates if name != "body"]
    semantic_names = [name for name in semantic_names if metrics[name]["words"] > 0]
    best_semantic_name = (
        max(semantic_names, key=lambda n: metrics[n]["score"])
        if semantic_names else None
    )

    body_available = "body" in node_by_name
    body_m = metrics.get("body", {"words": 0, "headings": 0, "score": 0.0})

    if not best_semantic_name:
        chosen = "body" if body_available else candidates[0][0]
        return (chosen, node_by_name[chosen]), {
            "reason": "body_only_or_no_meaningful_semantic_root",
            "candidates": list(metrics.values()),
        }

    sem_m = metrics[best_semantic_name]

    if not body_available or body_m["words"] <= 0:
        return (best_semantic_name, node_by_name[best_semantic_name]), {
            "reason": "best_semantic_root",
            "candidates": list(metrics.values()),
        }

    # Prefer body after chrome cleanup when the semantic root loses a substantial
    # portion of content or heading structure. This is the key guard for pages where
    # <main> is only an app shell/hero but SEO content continues in sibling sections.
    body_materially_richer = (
        body_m["words"] >= max(sem_m["words"] + 250, int(sem_m["words"] * 1.35))
        and body_m["headings"] >= sem_m["headings"]
    )
    semantic_missing_headings = (
        sem_m["headings"] == 0
        and body_m["headings"] >= 2
        and body_m["words"] >= int(max(1, sem_m["words"]) * 1.10)
    )
    semantic_too_narrow = (
        sem_m["words"] < int(body_m["words"] * 0.60)
        and body_m["words"] >= 200
    )

    if body_materially_richer or semantic_missing_headings or semantic_too_narrow:
        reason = (
            "body_preserves_more_editorial_content"
            if body_materially_richer
            else "semantic_root_missing_heading_structure"
            if semantic_missing_headings
            else "semantic_root_covers_less_than_60pct_of_clean_body"
        )
        return ("body", node_by_name["body"]), {
            "reason": reason,
            "candidates": list(metrics.values()),
        }

    return (best_semantic_name, node_by_name[best_semantic_name]), {
        "reason": "semantic_root_is_representative",
        "candidates": list(metrics.values()),
    }


def _static_html_structure(html: str, final_url: str) -> dict:
    """Extract Page Analysis evidence directly from a successful HTTP snapshot.

    This intentionally does not apply browser visibility/CSS rules.  A curl_cffi
    recovery response can contain complete SEO-relevant HTML even when a WAF blocks
    Playwright navigation.  Feeding that snapshot through ``page.set_content`` can
    make valid server-rendered text appear empty because the document has no real
    origin, relative assets/scripts do not hydrate normally, or page CSS leaves
    content hidden until site JavaScript runs.

    The static extractor therefore treats the successful HTML response itself as the
    evidence source, removes obvious global chrome/non-content elements, and builds
    the same structure shape used by the rendered extractor.
    """
    soup = BeautifulSoup(html or "", "lxml")

    title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")

    meta_description = ""
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta:
        meta_description = _clean_text(str(meta.get("content") or ""))

    canonical_url = ""
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        rel_values = rel if isinstance(rel, list) else [rel]
        if any(str(x).lower() == "canonical" for x in rel_values):
            href = _clean_text(str(link.get("href") or ""))
            if href:
                canonical_url = urljoin(final_url, href)
            break

    # Keyword density must use the whole page text, not a content-section subset.
    # Keep headers, footers, nav, buttons, forms, etc.  Remove only executable or
    # non-human source payloads that a browser user does not read as page copy.
    keyword_soup = BeautifulSoup(str(soup.body or soup), "lxml")
    for node in keyword_soup.select("script,style,noscript,template"):
        node.decompose()
    keyword_text = _clean_text(keyword_soup.get_text(" ", strip=True))

    candidates: list[tuple[str, object]] = []
    main = soup.find("main")
    article = soup.find("article")
    role_main = soup.find(attrs={"role": re.compile(r"^main$", re.I)})
    body = soup.body
    for name, node in (
        ("main", main),
        ("article", article),
        ("[role='main']", role_main),
        ("body", body),
    ):
        if node is not None:
            candidates.append((name, node))

    selected, root_debug = _choose_static_root(candidates)

    if selected is None:
        return {
            "root": None,
            "title": title,
            "meta_description": meta_description,
            "canonical_url": canonical_url,
            "main_text": "",
            "headings": [],
            "sections": [],
            "faqs": [],
            "paragraph_count": 0,
            "h1": [],
            "h2": [],
            "h3": [],
            "h4": [],
            "h5": [],
            "h6": [],
        }

    root_name, original_root = selected

    # Reparse the chosen root before destructive cleanup so no changes leak back to
    # metadata/JSON-LD extraction above.
    root_soup = BeautifulSoup(str(original_root), "lxml")
    root = root_soup.find(original_root.name) or root_soup

    for node in root.select(_STATIC_REMOVE_SELECTOR):
        node.decompose()

    headings: list[dict] = []
    for idx, node in enumerate(root.find_all(re.compile(r"^h[1-6]$", re.I)), start=1):
        text = _clean_text(node.get_text(" ", strip=True))
        if not text:
            continue
        try:
            level = int(str(node.name)[1])
        except Exception:
            continue
        headings.append(
            {
                "order": len(headings) + 1,
                "level": level,
                "text": text,
                "id": _clean_text(str(node.get("id") or "")),
            }
        )

    sections: list[dict] = []
    preamble_parts: list[str] = []
    current: dict | None = None

    for node in root.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"],
        recursive=True,
    ):
        name = str(node.name or "").lower()
        text = _clean_text(node.get_text(" ", strip=True))
        if not text:
            continue

        if re.fullmatch(r"h[1-6]", name):
            if current is not None:
                section_text = _clean_text(" ".join(current.pop("_parts", [])))
                current["text"] = section_text
                current["word_count"] = len(tokens(section_text))
                sections.append(current)
            current = {
                "order": len(sections) + 1,
                "heading_level": int(name[1]),
                "heading": text,
                "_parts": [],
            }
            continue

        # Do not double count <li><p>...</p></li> text.
        if name == "li" and node.find("p") is not None:
            continue

        if current is None:
            preamble_parts.append(text)
        else:
            current["_parts"].append(text)

    if current is not None:
        section_text = _clean_text(" ".join(current.pop("_parts", [])))
        current["text"] = section_text
        current["word_count"] = len(tokens(section_text))
        sections.append(current)

    preamble_text = _clean_text(" ".join(preamble_parts))
    if preamble_text:
        sections.insert(
            0,
            {
                "order": 0,
                "heading_level": 0,
                "heading": "Page introduction",
                "text": preamble_text,
                "word_count": len(tokens(preamble_text)),
            },
        )

    cleaned_root_text = _clean_text(root.get_text(" ", strip=True))
    section_text = _clean_text(
        " ".join(
            _clean_text(
                " ".join(
                    x
                    for x in [section.get("heading") or "", section.get("text") or ""]
                    if x
                )
            )
            for section in sections
        )
    )

    # Some sites place important copy in generic div/span containers rather than
    # paragraphs.  Prefer structured section text when it is representative, but
    # never throw away a much richer cleaned root snapshot.
    section_words = len(tokens(section_text))
    root_words = len(tokens(cleaned_root_text))
    if root_words > max(80, section_words * 2):
        main_text = cleaned_root_text
        if not sections and main_text:
            sections = [
                {
                    "order": 0,
                    "heading_level": 0,
                    "heading": "Page content",
                    "text": main_text,
                    "word_count": root_words,
                }
            ]
    else:
        main_text = section_text or cleaned_root_text

    faqs: list[dict] = []
    seen_faq: set[str] = set()

    def push_faq(question: str, answer: str, source: str) -> None:
        question = _clean_text(question)
        answer = _clean_text(answer)
        if not question:
            return
        key = question.casefold()
        if key in seen_faq:
            return
        seen_faq.add(key)
        faqs.append(
            {
                "order": len(faqs) + 1,
                "question": question,
                "answer": answer,
                "answer_word_count": len(tokens(answer)),
                "source": source,
            }
        )

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = {str(x) for x in item_type} if isinstance(item_type, list) else {str(item_type)}
            if "FAQPage" in types and isinstance(item.get("mainEntity"), list):
                for entity in item["mainEntity"]:
                    if not isinstance(entity, dict):
                        continue
                    accepted = entity.get("acceptedAnswer") or entity.get("suggestedAnswer") or {}
                    answer = accepted.get("text") if isinstance(accepted, dict) else ""
                    push_faq(str(entity.get("name") or ""), str(answer or ""), "FAQPage JSON-LD")
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)

    for section in sections:
        heading = _clean_text(str(section.get("heading") or ""))
        text = _clean_text(str(section.get("text") or ""))
        if heading.endswith("?") and text:
            push_faq(heading, text, "heading heuristic")

    by_level = {
        level: [h["text"] for h in headings if h["level"] == level]
        for level in range(1, 7)
    }

    return {
        "root": root_name,
        "title": title,
        "meta_description": meta_description,
        "canonical_url": canonical_url,
        "main_text": keyword_text or main_text,
        "headings": headings,
        "sections": sections,
        "faqs": faqs,
        "paragraph_count": len(root.find_all("p")),
        "h1": by_level[1],
        "h2": by_level[2],
        "h3": by_level[3],
        "h4": by_level[4],
        "h5": by_level[5],
        "h6": by_level[6],
        "_extraction": {
            "engine": "canonical-html-v5-fullpage-fallback-corpus",
            "root_selection_reason": root_debug.get("reason"),
            "root_candidates": root_debug.get("candidates") or [],
            "keyword_corpus": "full HTML body text excluding only script/style/noscript/template",
            "keyword_semantic_exclusions": [],
        },
    }

def _challenge_reason(title: str, body: str) -> str | None:
    """Detect real challenge pages without treating captcha widgets as pages.

    Captcha libraries/lead-form widgets are common on otherwise valid pages, so
    a body-only captcha token is not challenge evidence. A captcha title still
    counts because that usually means the entire document is the challenge.
    """
    title_probe = (title or "")[:500]
    body_probe = (body or "")[:12000]

    title_hits = [
        name for name, pattern in _HARD_CHALLENGE_PATTERNS if pattern.search(title_probe)
    ]
    if title_hits:
        return title_hits[0]
    if _CAPTCHA_PATTERN.search(title_probe):
        return "captcha"

    body_hits = [
        name for name, pattern in _HARD_CHALLENGE_PATTERNS if pattern.search(body_probe)
    ]
    body_words = len(re.findall(r"\b\w+\b", body_probe))

    if len(set(body_hits)) >= 2:
        return body_hits[0]
    if body_hits and body_words <= 260:
        return body_hits[0]

    # Deliberately ignore body-only captcha/recaptcha/hcaptcha. It is normally a
    # form widget, not proof that the requested page itself is blocked.
    return None


def _structure_metrics(structure: dict) -> tuple[int, int, int]:
    body = structure.get("main_text") or ""
    word_count = len(tokens(body))
    heading_count = sum(
        len(structure.get(level) or []) for level in ("h1", "h2", "h3")
    )
    paragraph_count = int(structure.get("paragraph_count") or 0)
    return word_count, heading_count, paragraph_count


def _suspicious_extraction(structure: dict) -> bool:
    word_count, heading_count, paragraph_count = _structure_metrics(structure)
    if word_count <= 0:
        return True
    return word_count < 40 and heading_count == 0 and paragraph_count == 0


def _annotate_section_hashes(structure: dict) -> None:
    for section in structure.get("sections") or []:
        section_text = " ".join(
            x
            for x in [section.get("heading") or "", section.get("text") or ""]
            if x
        )
        section["content_hash"] = hashlib.sha256(
            section_text.strip().casefold().encode("utf-8", errors="ignore")
        ).hexdigest()


def _success_payload(
    *,
    url: str,
    final_url: str | None,
    status_code: int | None,
    attempt: int,
    structure: dict,
    fetch_method: str,
    fallback_used: bool,
) -> dict:
    body = structure.get("main_text") or ""
    word_count = len(tokens(body))

    _annotate_section_hashes(structure)
    structure["_fetch"] = {
        "terminal_state": "usable",
        "retryable": False,
        "error": None,
        "method": fetch_method,
        "fallback_used": fallback_used,
    }

    return {
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "retry_count": attempt,
        "word_count": word_count,
        "title": structure.get("title"),
        "meta_description": structure.get("meta_description"),
        "canonical_url": structure.get("canonical_url"),
        "extraction_root": structure.get("root"),
        "paragraph_count": int(structure.get("paragraph_count") or 0),
        "h1": structure.get("h1") or [],
        "h2": structure.get("h2") or [],
        "h3": structure.get("h3") or [],
        "body_text": body,
        "structure": structure,
        "structure_version": PAGE_STRUCTURE_VERSION,
        "content_hash": hashlib.sha256(
            body.encode("utf-8", errors="ignore")
        ).hexdigest(),
        "error": None,
    }


def _failure_payload(
    url: str,
    *,
    attempt: int,
    error: str,
    status_code: int | None = None,
    final_url: str | None = None,
    terminal_state: str = "fetch_failed",
    retryable: bool = False,
    title: str | None = None,
    fetch_method: str | None = None,
    fallback_used: bool = False,
) -> dict:
    structure = {
        "root": None,
        "title": title or "",
        "meta_description": "",
        "canonical_url": "",
        "main_text": "",
        "headings": [],
        "sections": [],
        "faqs": [],
        "paragraph_count": 0,
        "h1": [],
        "h2": [],
        "h3": [],
        "h4": [],
        "h5": [],
        "h6": [],
        "_fetch": {
            "terminal_state": terminal_state,
            "retryable": retryable,
            "error": error,
            "method": fetch_method,
            "fallback_used": fallback_used,
        },
    }

    return {
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "retry_count": attempt,
        "word_count": 0,
        "title": title,
        "meta_description": None,
        "canonical_url": None,
        "extraction_root": None,
        "paragraph_count": 0,
        "h1": [],
        "h2": [],
        "h3": [],
        "body_text": "",
        "structure": structure,
        "structure_version": PAGE_STRUCTURE_VERSION,
        "content_hash": None,
        "error": error,
    }


async def _canonical_structure_from_page(page, final_url: str) -> dict:
    """Extract the canonical Page Analysis snapshot from the rendered browser.

    A successful Playwright navigation already has the real CSS/layout state. Before
    extraction we repeatedly sweep the CURRENT document height until both page height
    and rendered text converge. There is no pixel-height ceiling and no fixed scroll
    count. Truly infinite feeds are bounded only by a wall-clock safety budget and are
    explicitly marked incomplete.

    Keyword density uses the complete rendered document.body.innerText corpus: header,
    navigation, hero, body, cards, FAQs, forms, buttons and footer are all included.
    Structural section/FAQ extraction remains separately content-focused. Static HTML
    is only a recovery fallback.
    """
    rendered = None
    rendered_error = None

    hydration = None
    try:
        hydration = await page.evaluate(
            HYDRATE_JS,
            {
                # No fixed page-height or scroll-count ceiling.  A genuinely
                # infinite feed is bounded only by elapsed time, and is marked
                # incomplete instead of being silently treated as exhaustive.
                "stable_rounds": PAGE_HYDRATION_STABLE_ROUNDS,
                "max_elapsed_ms": PAGE_HYDRATION_MAX_ELAPSED_MS,
                "settle_ms": PAGE_HYDRATION_SETTLE_MS,
                "per_scroll_wait_ms": PAGE_HYDRATION_SCROLL_WAIT_MS,
                "step_ratio": PAGE_HYDRATION_STEP_RATIO,
            },
        )
    except Exception:
        # Hydration is best-effort. A page can still have a perfectly usable
        # rendered DOM even if scripted scrolling is blocked/interrupted.
        hydration = None

    try:
        rendered = await page.evaluate(EXTRACT_JS)
        if isinstance(rendered, dict):
            rendered.setdefault("_extraction", {})
            rendered["_extraction"].update({
                "engine": "rendered-fullpage-innertext-v6-adaptive",
                "root_selection_reason": "full-rendered-body-adaptively-hydrated-no-semantic-exclusions",
                "keyword_corpus": "document.body.innerText",
                "keyword_semantic_exclusions": [],
                "hydration": hydration,
                "keyword_corpus_complete": bool((hydration or {}).get("complete", False)),
                "keyword_corpus_stop_reason": (hydration or {}).get("stop_reason"),
                "fallback_used": False,
            })
        if rendered is not None and not _suspicious_extraction(rendered):
            return rendered
    except Exception as exc:
        rendered_error = f"{type(exc).__name__}: {exc}"

    try:
        html = await page.content()
        static = await asyncio.to_thread(_static_html_structure, html, final_url)
        if isinstance(static, dict):
            static.setdefault("_extraction", {})
            static["_extraction"].update({
                "engine": "canonical-html-v5-fallback",
                "fallback_reason": rendered_error or "rendered extraction was suspicious/empty",
                "fallback_used": True,
            })
        if not _suspicious_extraction(static):
            return static
    except Exception:
        static = None

    if rendered is not None:
        return rendered
    if static is not None:
        return static

    return {
        "root": None,
        "title": "",
        "meta_description": "",
        "canonical_url": final_url,
        "main_text": "",
        "headings": [],
        "sections": [],
        "faqs": [],
        "paragraph_count": 0,
        "h1": [], "h2": [], "h3": [], "h4": [], "h5": [], "h6": [],
        "_extraction": {
            "engine": "failed-rendered-and-static",
            "fallback_used": True,
        },
    }


class Crawler:
    def __init__(self):
        self.dns_cache: dict[str, bool] = {}

    async def crawl_many(
        self,
        targets: list[tuple[int, str]],
        concurrency: int | None = None,
        on_result=None,
    ):
        """Crawl targets concurrently and optionally report each completed URL."""
        import inspect

        limit = max(1, min(concurrency or settings.crawl_concurrency, 10))
        sem = asyncio.Semaphore(limit)

        async with async_playwright() as pw:
            # Use a real/full Chromium engine whenever available.  The old
            # SEOAnalysisBot user-agent caused some sites to serve a different
            # document from the one a normal Chrome user receives, which makes
            # any downstream keyword count incomparable with a manual browser.
            # No keyword/count value is hard-coded here: we only standardize the
            # browser environment used to fetch the page.
            browser = None
            browser_mode = None
            launch_errors = []
            launch_candidates = [
                ({"channel": "chrome", "headless": True}, "google-chrome-new-headless"),
                ({"channel": "chromium", "headless": True}, "chromium-new-headless"),
                ({"headless": True}, "playwright-headless-shell"),
            ]
            for launch_kwargs, mode in launch_candidates:
                try:
                    browser = await pw.chromium.launch(**launch_kwargs)
                    browser_mode = mode
                    break
                except Exception as exc:
                    launch_errors.append(f"{mode}: {type(exc).__name__}: {exc}")

            if browser is None:
                raise RuntimeError(
                    "Unable to launch a Chromium browser: " + " | ".join(launch_errors)
                )

            context = await browser.new_context(
                ignore_https_errors=False,
                # Canonical desktop analysis viewport.  This is an environment
                # choice, not a keyword/count override.  It avoids responsive
                # breakpoints changing from machine to machine.
                viewport={"width": 1440, "height": 900},
            )

            # Make the actual launch mode auditable from each page snapshot.
            await context.add_init_script(
                script=f"window.__SEO_ANALYSIS_BROWSER_MODE__ = {browser_mode!r};"
            )

            async def guard(route):
                req_url = route.request.url
                scheme = urlparse(req_url).scheme
                if scheme in {"data", "blob", "about"}:
                    return await route.continue_()

                try:
                    await validate_public_url(req_url, self.dns_cache)
                except Exception:
                    return await route.abort("blockedbyclient")

                return await route.continue_()

            await context.route("**/*", guard)

            async def one(target_id, url):
                async with sem:
                    return target_id, await self._crawl_one(context, url)

            tasks = [
                asyncio.create_task(one(target_id, url))
                for target_id, url in targets
            ]

            out = []
            completed = 0

            try:
                for future in asyncio.as_completed(tasks):
                    target_id, payload = await future
                    out.append((target_id, payload))
                    completed += 1

                    if on_result is not None:
                        callback_result = on_result(
                            target_id,
                            payload,
                            completed,
                            len(targets),
                        )
                        if inspect.isawaitable(callback_result):
                            await callback_result
            finally:
                await context.close()
                await browser.close()

        return out

    async def _curl_cffi_fetch(self, url: str) -> dict:
        """One bounded browser-impersonating HTTP fallback.

        This is deliberately not an unbounded bypass loop.  It is used once when the
        normal browser receives a WAF-like response or when a successful HTTP response
        produces an obviously empty extraction.
        """

        await validate_public_url(url, self.dns_cache)

        def _do_fetch() -> dict:
            try:
                from curl_cffi import requests as curl_requests
            except Exception as exc:  # pragma: no cover - dependency/environment guard
                return {
                    "error": f"curl_cffi unavailable: {type(exc).__name__}: {exc}",
                    "status_code": None,
                    "final_url": url,
                    "html": "",
                }

            timeout_seconds = max(10.0, float(settings.crawl_timeout_ms) / 1000.0)

            try:
                response = curl_requests.get(
                    url,
                    impersonate="chrome",
                    timeout=timeout_seconds,
                    allow_redirects=True,
                    headers={
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml;q=0.9,"
                            "image/avif,image/webp,*/*;q=0.8"
                        ),
                        "Accept-Language": "en-US,en;q=0.9",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                )
            except Exception as exc:
                return {
                    "error": f"curl_cffi fetch failed: {type(exc).__name__}: {exc}",
                    "status_code": None,
                    "final_url": url,
                    "html": "",
                }

            # Page Analysis handles a few benchmark URLs at a time, but still cap the
            # fallback payload to avoid unexpectedly holding enormous responses.
            content = bytes(response.content or b"")
            max_bytes = 12 * 1024 * 1024
            if len(content) > max_bytes:
                content = content[:max_bytes]

            encoding = getattr(response, "encoding", None) or "utf-8"
            try:
                html = content.decode(encoding, errors="replace")
            except LookupError:
                html = content.decode("utf-8", errors="replace")

            return {
                "error": None,
                "status_code": int(response.status_code),
                "final_url": str(response.url),
                "html": html,
                "retry_after_seconds": _parse_retry_after(response.headers.get("retry-after")),
            }

        return await asyncio.to_thread(_do_fetch)

    async def _curl_fallback_payload(
        self,
        context,
        *,
        url: str,
        attempt: int,
        original_status: int | None,
        reason: str,
    ) -> dict | None:
        result = await self._curl_cffi_fetch(url)

        final_url = result.get("final_url") or url
        try:
            await validate_public_url(final_url, self.dns_cache)
        except Exception as exc:
            return _failure_payload(
                url,
                attempt=attempt,
                error=(
                    f"{reason}; curl_cffi redirected to a blocked/non-public URL: "
                    f"{type(exc).__name__}: {exc}"
                ),
                status_code=result.get("status_code") or original_status,
                final_url=final_url,
                terminal_state="security_challenge",
                retryable=False,
                fetch_method="curl_cffi",
                fallback_used=True,
            )

        if result.get("error"):
            # For an explicit WAF-style status, repeating the identical browser request
            # is unlikely to help. Surface the fallback failure clearly instead of
            # hiding it behind a generic HTTP 403. For extraction-only failures, let
            # the caller continue its normal retry policy.
            if original_status in _FALLBACK_HTTP:
                return _failure_payload(
                    url,
                    attempt=attempt,
                    error=f"{reason}; alternative fallback failed: {result.get('error')}",
                    status_code=original_status,
                    final_url=final_url,
                    terminal_state="security_challenge",
                    retryable=False,
                    fetch_method="playwright",
                    fallback_used=True,
                )
            return None

        status = result.get("status_code")
        html = result.get("html") or ""
        raw_challenge = _challenge_reason("", html)

        if status is not None and status >= 400:
            terminal_state = (
                "stale_page"
                if status in _STALE_HTTP
                else "security_challenge"
                if status in _FALLBACK_HTTP or raw_challenge
                else "http_error"
            )
            return _failure_payload(
                url,
                attempt=attempt,
                error=(
                    f"{reason}; curl_cffi fallback returned HTTP {status}"
                    + (f" ({raw_challenge})" if raw_challenge else "")
                ),
                status_code=status,
                final_url=final_url,
                terminal_state=terminal_state,
                retryable=status in _RETRYABLE_HTTP,
                fetch_method="curl_cffi",
                fallback_used=True,
            )

        # First trust the recovered HTTP snapshot itself.  Do not run a 200 curl_cffi
        # response through page.set_content as the primary extractor: set_content uses
        # an about:blank-style document origin, which can leave valid server-rendered
        # content hidden/unhydrated and previously caused pages with hundreds of
        # paragraphs to report zero extracted words.
        structure = await asyncio.to_thread(_static_html_structure, html, final_url)
        body = structure.get("main_text") or ""
        title = structure.get("title") or ""
        challenge = _challenge_reason(title, body)

        if challenge:
            return _failure_payload(
                url,
                attempt=attempt,
                error=(
                    f"{reason}; curl_cffi fallback returned a security/challenge "
                    f"document: {challenge}"
                ),
                status_code=status,
                final_url=final_url,
                terminal_state="security_challenge",
                retryable=False,
                title=title,
                fetch_method="curl_cffi-static",
                fallback_used=True,
            )

        if not _suspicious_extraction(structure):
            return _success_payload(
                url=url,
                final_url=final_url,
                status_code=status,
                attempt=attempt,
                structure=structure,
                fetch_method="curl_cffi-static",
                fallback_used=True,
            )

        # Secondary best-effort hydration.  Some sites return only a client-side shell
        # to curl_cffi.  A <base> tag gives relative assets the recovered URL origin.
        # This path is attempted only when the static snapshot truly lacks useful copy.
        fallback_page = await context.new_page()
        try:
            hydrated_html = html
            if "<base" not in hydrated_html.lower():
                base_tag = f'<base href="{final_url}">'
                if re.search(r"<head(?:\s[^>]*)?>", hydrated_html, re.I):
                    hydrated_html = re.sub(
                        r"(<head(?:\s[^>]*)?>)",
                        r"\1" + base_tag,
                        hydrated_html,
                        count=1,
                        flags=re.I,
                    )
                else:
                    hydrated_html = base_tag + hydrated_html

            await fallback_page.set_content(
                hydrated_html,
                wait_until="domcontentloaded",
                timeout=settings.crawl_timeout_ms,
            )
            try:
                await fallback_page.wait_for_load_state(
                    "networkidle",
                    timeout=min(5000, settings.crawl_timeout_ms),
                )
            except PlaywrightTimeoutError:
                pass
            await fallback_page.wait_for_timeout(500)

            hydrated = await _canonical_structure_from_page(fallback_page, final_url)
            hydrated_body = hydrated.get("main_text") or ""
            hydrated_title = hydrated.get("title") or title
            hydrated_challenge = _challenge_reason(hydrated_title, hydrated_body)

            if not hydrated_challenge and not _suspicious_extraction(hydrated):
                return _success_payload(
                    url=url,
                    final_url=final_url,
                    status_code=status,
                    attempt=attempt,
                    structure=hydrated,
                    fetch_method="curl_cffi-hydrated",
                    fallback_used=True,
                )

            static_metrics = _structure_metrics(structure)
            hydrated_metrics = _structure_metrics(hydrated)
            return _failure_payload(
                url,
                attempt=attempt,
                error=(
                    f"{reason}; curl_cffi returned HTTP {status} but neither static "
                    "nor hydrated extraction produced meaningful content: "
                    f"static(words={static_metrics[0]}, headings={static_metrics[1]}, "
                    f"paragraphs={static_metrics[2]}); "
                    f"hydrated(words={hydrated_metrics[0]}, headings={hydrated_metrics[1]}, "
                    f"paragraphs={hydrated_metrics[2]})"
                ),
                status_code=status,
                final_url=final_url,
                terminal_state="extraction_failed",
                retryable=False,
                title=hydrated_title or title,
                fetch_method="curl_cffi",
                fallback_used=True,
            )
        finally:
            await fallback_page.close()

    async def _crawl_one(self, context, url: str) -> dict:
        await validate_public_url(url, self.dns_cache)
        last_error = "Unknown crawl error"

        for attempt in range(settings.crawl_max_retries + 1):
            page = await context.new_page()

            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=settings.crawl_timeout_ms,
                )

                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=min(8000, settings.crawl_timeout_ms),
                    )
                except PlaywrightTimeoutError:
                    pass

                final_url = page.url
                await validate_public_url(final_url, self.dns_cache)
                status = response.status if response else None

                if status is not None and status >= 400:
                    title = ""
                    body_probe = ""
                    try:
                        title = await page.title()
                    except Exception:
                        pass
                    try:
                        body_probe = await page.locator("body").inner_text(timeout=2000)
                    except Exception:
                        pass

                    challenge = _challenge_reason(title, body_probe)
                    last_error = f"HTTP {status}"

                    # 401/403/406 and explicit challenge pages get one bounded
                    # browser-impersonating HTTP fallback before they become terminal.
                    if status in _FALLBACK_HTTP or challenge:
                        fallback = await self._curl_fallback_payload(
                            context,
                            url=url,
                            attempt=attempt,
                            original_status=status,
                            reason=(
                                f"Playwright returned HTTP {status}"
                                + (f" ({challenge})" if challenge else "")
                            ),
                        )
                        if fallback is not None:
                            return fallback

                    retryable = status in _RETRYABLE_HTTP
                    terminal_state = (
                        "stale_page"
                        if status in _STALE_HTTP
                        else "security_challenge"
                        if status in _FALLBACK_HTTP or challenge
                        else "http_error"
                    )

                    if retryable and attempt < settings.crawl_max_retries:
                        retry_after = None
                        if status == 429 and response is not None:
                            try:
                                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                            except Exception:
                                retry_after = None
                        delay = (
                            retry_after
                            if retry_after is not None
                            else settings.crawl_backoff_base_seconds * (2**attempt)
                        )
                        await asyncio.sleep(delay)
                        continue

                    return _failure_payload(
                        url,
                        attempt=attempt,
                        error=last_error,
                        status_code=status,
                        final_url=final_url,
                        terminal_state=terminal_state,
                        retryable=retryable,
                        title=title,
                        fetch_method="playwright",
                        fallback_used=status in _FALLBACK_HTTP or bool(challenge),
                    )

                structure = await _canonical_structure_from_page(page, final_url)
                body = structure.get("main_text") or ""
                title = structure.get("title") or ""

                challenge = _challenge_reason(title, body)
                if challenge:
                    last_error = f"Security/challenge page detected: {challenge}"
                    fallback = await self._curl_fallback_payload(
                        context,
                        url=url,
                        attempt=attempt,
                        original_status=status,
                        reason=last_error,
                    )
                    if fallback is not None:
                        return fallback

                    if attempt < settings.crawl_max_retries:
                        await asyncio.sleep(
                            settings.crawl_backoff_base_seconds * (2**attempt)
                        )
                        continue

                    return _failure_payload(
                        url,
                        attempt=attempt,
                        error=last_error,
                        status_code=status,
                        final_url=final_url,
                        terminal_state="security_challenge",
                        retryable=False,
                        title=title,
                        fetch_method="playwright",
                        fallback_used=True,
                    )

                # A 2xx response is not enough.  Give dynamic pages one short settling
                # window and re-extract before declaring the browser extraction empty.
                if _suspicious_extraction(structure):
                    await page.wait_for_timeout(1200)
                    structure = await _canonical_structure_from_page(page, final_url)
                    body = structure.get("main_text") or ""
                    title = structure.get("title") or ""

                if _suspicious_extraction(structure):
                    word_count, heading_count, paragraph_count = _structure_metrics(structure)
                    last_error = (
                        "HTTP success but meaningful content was not extracted: "
                        f"words={word_count}, headings={heading_count}, "
                        f"paragraphs={paragraph_count}"
                    )

                    fallback = await self._curl_fallback_payload(
                        context,
                        url=url,
                        attempt=attempt,
                        original_status=status,
                        reason=last_error,
                    )
                    if fallback is not None:
                        return fallback

                    if attempt < settings.crawl_max_retries:
                        await asyncio.sleep(
                            settings.crawl_backoff_base_seconds * (2**attempt)
                        )
                        continue

                    return _failure_payload(
                        url,
                        attempt=attempt,
                        error=last_error,
                        status_code=status,
                        final_url=final_url,
                        terminal_state="extraction_failed",
                        retryable=False,
                        title=title,
                        fetch_method="playwright",
                        fallback_used=True,
                    )

                return _success_payload(
                    url=url,
                    final_url=final_url,
                    status_code=status,
                    attempt=attempt,
                    structure=structure,
                    fetch_method="playwright",
                    fallback_used=False,
                )

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < settings.crawl_max_retries:
                    await asyncio.sleep(
                        settings.crawl_backoff_base_seconds * (2**attempt)
                    )
                    continue
            finally:
                await page.close()

        return _failure_payload(
            url,
            attempt=settings.crawl_max_retries,
            error=last_error,
            terminal_state="fetch_failed",
            retryable=True,
            fetch_method="playwright",
        )
