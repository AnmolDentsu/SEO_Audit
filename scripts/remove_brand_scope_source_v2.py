from __future__ import annotations

import argparse
import ast
import re
import shutil
from pathlib import Path
from typing import Iterable

# Transactional Brand Scope feature removal for the current SEO Analysis Platform.
#
# Key safety properties:
# - edits are computed in memory first;
# - every Python result is AST-parsed before any file is written;
# - --strict scans the whole active backend/frontend tree before commit;
# - if any active Brand Scope implementation reference remains, NOTHING is written;
# - backups are created once with suffix .pre-brand-scope-removal;
# - the script is idempotent and can be rerun safely.

PYTHON_TARGETS = [
    "backend/app/worker.py",
    "backend/app/services/sitemap_analysis.py",
    "backend/app/services/content_analysis.py",
    "backend/app/services/sitemap_ai.py",
    "backend/app/services/job_runner.py",
    "backend/app/services/sitemap_job_runner.py",
    "backend/app/services/export.py",
    "backend/app/services/sitemap_export.py",
    "backend/app/api.py",
]

JS_TARGETS = [
    "frontend/src/App.jsx",
    "frontend/src/SitemapIntelligence.jsx",
    "frontend/src/api.js",
]

# These compatibility files may legitimately mention the retired field because
# their only purpose is to strip it from historical payloads.
SCAN_EXCLUDED_NAMES = {
    "analysis_contract.py",
    "strip_brand_scope.py",
    "remove_brand_scope_source.py",
    "test_reliability_brand_removal.py",
}

RETIRED_LITERAL_KEYS = {"Brand Scope", "brand_scope"}
RETIRED_HELPERS = {"_brand_scope", "_brand_tokens"}
RETIRED_VARIABLES = {"brand_scope", "analysisBrandScope", "brandScope"}


def _backup(path: Path) -> Path:
    backup = path.with_suffix(path.suffix + ".pre-brand-scope-removal")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for m in re.finditer(r"\n", text):
        starts.append(m.end())
    return starts


def _offset(starts: list[int], lineno: int, col: int) -> int:
    # AST offsets are zero-based columns. Project sources are UTF-8 but these
    # retired identifiers/literals are ASCII, so normal character offsets are
    # correct for the affected spans.
    return starts[lineno - 1] + col


def _node_span(text: str, node: ast.AST) -> tuple[int, int]:
    starts = _line_starts(text)
    return (
        _offset(starts, node.lineno, node.col_offset),
        _offset(starts, node.end_lineno, node.end_col_offset),
    )


def _expand_comma_item_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand an AST element span to remove one surrounding comma safely."""
    # Prefer removing the following comma so the previous element is untouched.
    i = end
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i < len(text) and text[i] == ",":
        i += 1
        # Consume only horizontal whitespace after the comma; keep newlines to
        # preserve surrounding layout and comments.
        while i < len(text) and text[i] in " \t":
            i += 1
        return start, i

    # Otherwise consume the preceding comma.
    j = start - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j >= 0 and text[j] == ",":
        return j, end
    return start, end


def _statement_span(text: str, node: ast.AST) -> tuple[int, int]:
    start, end = _node_span(text, node)
    # Remove indentation and the trailing newline when the node occupies a
    # statement line/block. This keeps empty whitespace from accumulating.
    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():
        start = line_start
    if end < len(text):
        nl = text.find("\n", end)
        if nl != -1 and not text[end:nl].strip():
            end = nl + 1
    return start, end


def _apply_edits(text: str, edits: Iterable[tuple[int, int, str]]) -> str:
    # Deduplicate exact edits and reject overlapping edits conservatively.
    unique = sorted(set(edits), key=lambda x: (x[0], x[1], x[2]))
    accepted: list[tuple[int, int, str]] = []
    last_end = -1
    for edit in unique:
        start, end, _ = edit
        if start < last_end:
            # If this edit is wholly inside the previous statement/function
            # removal, the outer edit already covers it.
            prev = accepted[-1]
            if start >= prev[0] and end <= prev[1]:
                continue
            raise RuntimeError(f"Overlapping source edits detected: {prev} vs {edit}")
        accepted.append(edit)
        last_end = end

    out = text
    for start, end, replacement in reversed(accepted):
        out = out[:start] + replacement + out[end:]
    return out


def _literal_key(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _name_targets(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            names.update(_name_targets(elt))
    return names


def _is_brand_tokens_call(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_brand_tokens"
    )


def _python_ast_cleanup(text: str) -> str:
    tree = ast.parse(text)
    edits: list[tuple[int, int, str]] = []

    # 1) Remove retired dictionary keys without regex guessing. This is the
    # exact operation that previously broke content_analysis.py.
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if _literal_key(key) in RETIRED_LITERAL_KEYS:
                    start, _ = _node_span(text, key)
                    _, end = _node_span(text, value)
                    start, end = _expand_comma_item_span(text, start, end)
                    edits.append((start, end, ""))

        # 2) Remove literal Brand Scope column/list elements.
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and elt.value in RETIRED_LITERAL_KEYS:
                    start, end = _node_span(text, elt)
                    start, end = _expand_comma_item_span(text, start, end)
                    edits.append((start, end, ""))

        # 3) Remove helper functions after their call sites (which live in
        # dictionary values above) are removed.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in RETIRED_HELPERS:
            start, end = _statement_span(text, node)
            edits.append((start, end, ""))

        # 4) Remove local assignments used only by the retired feature.
        if isinstance(node, ast.Assign):
            target_names: set[str] = set()
            for target in node.targets:
                target_names.update(_name_targets(target))
            if "brand_scope" in target_names or ("brands" in target_names and _is_brand_tokens_call(node.value)):
                start, end = _statement_span(text, node)
                edits.append((start, end, ""))
        elif isinstance(node, ast.AnnAssign):
            target_names = _name_targets(node.target)
            if "brand_scope" in target_names or ("brands" in target_names and _is_brand_tokens_call(node.value)):
                start, end = _statement_span(text, node)
                edits.append((start, end, ""))

    return _apply_edits(text, edits)


def _remove_api_brand_filter(text: str) -> str:
    # Remove brand_scope from the known FastAPI/helper one-line signatures.
    text = re.sub(
        r"\s*,?\s*brand_scope\s*:\s*str\s*\|\s*None\s*=\s*None\s*,?",
        lambda m: ", " if m.group(0).rstrip().endswith(",") else "",
        text,
    )

    # Normalize malformed commas potentially left by signature removal.
    text = re.sub(r"\(\s*,", "(", text)
    text = re.sub(r",\s*,", ",", text)

    # Retired filtering block in _filter_analysis_rows(). Use exact semantics
    # rather than deleting arbitrary if-blocks.
    text = re.sub(
        r"(?m)^\s{4}if\s+brand_scope\s+and\s+brand_scope\.upper\(\)\s+in\s+\{[^\n]*\}:\s*\n"
        r"\s{8}scope\s*=\s*brand_scope\.upper\(\)\s*\n"
        r"\s{8}out\s*=\s*\[x\s+for\s+x\s+in\s+out\s+if\s+[^\n]*\]\s*\n",
        "",
        text,
    )

    # Calls to _filter_analysis_rows(..., brand_scope, q) now receive q only.
    text = re.sub(r",\s*brand_scope\s*,\s*q\s*\)", ", q)", text)

    # If a call had only (..., brand_scope), remove that positional argument.
    text = re.sub(r",\s*brand_scope\s*\)", ")", text)

    return text


def _remove_worker_brand_comment(text: str) -> str:
    return text.replace(
        "# Remove any legacy Brand Scope filter metadata from the run config too.",
        "# Sanitize deprecated analysis-filter metadata from the run config too.",
    )


def _cleanup_python_text(path: Path, text: str) -> str:
    updated = _python_ast_cleanup(text)

    if path.name == "api.py":
        updated = _remove_api_brand_filter(updated)

    if path.name == "worker.py":
        updated = _remove_worker_brand_comment(updated)

    # Remove now-empty excess blank lines created by statement deletion, while
    # avoiding broad source reformatting.
    updated = re.sub(r"\n{4,}", "\n\n\n", updated)

    # Final safety gate for every Python target.
    ast.parse(updated)
    return updated


def _remove_js_selects(text: str) -> str:
    select_re = re.compile(r"<select\b[^>]*>.*?</select>", re.S | re.I)
    return select_re.sub(
        lambda m: "" if any(
            marker in m.group(0)
            for marker in ("Brand only", "Non-Brand only", "Brand + Non-Brand")
        ) else m.group(0),
        text,
    )


def _cleanup_js_text(path: Path, text: str) -> str:
    updated = text

    # Remove the visible Brand/Non-Brand filter control and its description.
    updated = _remove_js_selects(updated)
    updated = re.sub(
        r"(?is)<(?:small|p)[^>]*>[^<]*Brand scope[^<]*</(?:small|p)>",
        "",
        updated,
    )

    # Remove React state dedicated to this retired feature.
    updated = re.sub(
        r"(?m)^\s*const\s*\[\s*analysisBrandScope\s*,\s*setAnalysisBrandScope\s*\]\s*=\s*useState\([^\n]*\)\s*;?\s*$",
        "",
        updated,
    )
    updated = re.sub(
        r"(?m)^\s*const\s*\[\s*brandScope\s*,\s*setBrandScope\s*\]\s*=\s*useState\([^\n]*\)\s*;?\s*$",
        "",
        updated,
    )

    # Remove query/body properties when they occupy their own object line.
    updated = re.sub(
        r"(?m)^\s*brand_scope\s*:\s*[^\n]+,?\s*$",
        "",
        updated,
    )
    updated = re.sub(
        r"(?m)^\s*brandScope\s*:\s*[^\n]+,?\s*$",
        "",
        updated,
    )

    # Current SitemapIntelligence inline params object.
    updated = re.sub(
        r"\{\s*brand_scope\s*:\s*analysisBrandScope\s*===\s*['\"]ALL['\"]\s*\?\s*undefined\s*:\s*analysisBrandScope\s*,\s*q\s*:\s*analysisQuery\s*\|\|\s*undefined\s*\}",
        "{ q: analysisQuery || undefined }",
        updated,
    )

    # Generic inline brand_scope / brandScope object properties.
    updated = re.sub(r"\bbrand_scope\s*:\s*[^,}]+,\s*", "", updated)
    updated = re.sub(r"\bbrandScope\s*:\s*[^,}]+,\s*", "", updated)
    updated = re.sub(r",\s*\bbrand_scope\s*:\s*[^,}]+", "", updated)
    updated = re.sub(r",\s*\bbrandScope\s*:\s*[^,}]+", "", updated)

    # Remove the actual scope predicate while keeping surrounding filter logic
    # valid. If downstream code still uses scopeOk, `true` is semantically the
    # retired filter's neutral identity.
    updated = re.sub(
        r"(?m)^(\s*)const\s+scopeOk\s*=\s*analysisBrandScope\s*===\s*['\"]ALL['\"]\s*\|\|[^\n]*$",
        r"\1const scopeOk = true",
        updated,
    )

    # Remove table column/cell from Page Analysis section comparison.
    updated = updated.replace("'Brand Scope',", "").replace('"Brand Scope",', "")
    updated = updated.replace(", 'Brand Scope'", "").replace(', "Brand Scope"', "")
    updated = re.sub(
        r"\s*r\[['\"]Brand Scope['\"]\]\s*\|\|\s*['\"]NON_BRAND['\"]\s*,",
        "",
        updated,
    )

    # Remove sitemap row badge that displayed the scope.
    updated = re.sub(
        r"\s*<Badge>\{x\.brand_scope\s*\|\|\s*['\"]NON_BRAND['\"]\}</Badge>\s*",
        "",
        updated,
    )

    # Remove now-unused setter usages that only belonged to the deleted select.
    updated = re.sub(r"\bsetAnalysisBrandScope\([^\n;]*\);?", "", updated)
    updated = re.sub(r"\bsetBrandScope\([^\n;]*\);?", "", updated)

    # React hook dependency arrays can still retain the retired state variable
    # after the state declaration and filtering expressions are removed, e.g.
    #   }), [opps, analysisBrandScope, analysisQuery])
    # Remove the identifier conservatively from comma-separated lists without
    # changing the surrounding hook expression.
    updated = re.sub(
        r"(?<=\[)\s*analysisBrandScope\s*,\s*",
        "",
        updated,
    )
    updated = re.sub(
        r",\s*analysisBrandScope\s*(?=[,\]])",
        "",
        updated,
    )
    updated = re.sub(
        r"(?<=\[)\s*brandScope\s*,\s*",
        "",
        updated,
    )
    updated = re.sub(
        r",\s*brandScope\s*(?=[,\]])",
        "",
        updated,
    )

    updated = re.sub(r"\n{4,}", "\n\n\n", updated)
    return updated


def _active_reference_patterns() -> tuple[re.Pattern[str], ...]:
    return (
        re.compile(r"Brand Scope", re.I),
        re.compile(r"\bbrand_scope\b"),
        re.compile(r"\bbrandScope\b"),
        re.compile(r"\banalysisBrandScope\b"),
        re.compile(r"\bsetAnalysisBrandScope\b"),
        re.compile(r"\bsetBrandScope\b"),
        re.compile(r"\b_brand_scope\b"),
        re.compile(r"\b_brand_tokens\b"),
        re.compile(r"Brand only", re.I),
        re.compile(r"Non-Brand only", re.I),
        re.compile(r"Brand \+ Non-Brand", re.I),
    )


def _scan_active_tree(root: Path, overrides: dict[Path, str]) -> list[str]:
    hits: list[str] = []
    patterns = _active_reference_patterns()

    candidates = list((root / "backend/app").rglob("*.py"))
    for ext in ("*.js", "*.jsx", "*.ts", "*.tsx"):
        candidates.extend((root / "frontend/src").rglob(ext))

    for path in sorted(set(candidates)):
        if path.name in SCAN_EXCLUDED_NAMES:
            continue
        if ".pre-brand-scope-removal" in path.name:
            continue
        if "__pycache__" in path.parts:
            continue

        try:
            text = overrides.get(path, path.read_text(errors="ignore"))
        except Exception:
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(p.search(line) for p in patterns):
                hits.append(
                    f"{path.relative_to(root)}:{lineno}: {line.strip()[:240]}"
                )
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transactionally remove the retired Brand Scope feature from active Page/Sitemap source."
    )
    parser.add_argument(
        "--project-root",
        default="..",
        help="Project root containing backend/ and frontend/. Run from backend/; default '..'.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Abort without writing if any active Brand Scope implementation reference would remain.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute/validate changes and print the plan without writing files.",
    )
    args = parser.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    overrides: dict[Path, str] = {}
    changed: list[Path] = []

    # PHASE 1: compute every edit in memory. Nothing is written here.
    for rel in PYTHON_TARGETS:
        path = root / rel
        if not path.exists():
            continue
        original = path.read_text()
        try:
            updated = _cleanup_python_text(path, original)
        except SyntaxError as exc:
            raise SystemExit(f"Python validation failed for {rel}: {exc}") from exc
        overrides[path] = updated
        if updated != original:
            changed.append(path)

    for rel in JS_TARGETS:
        path = root / rel
        if not path.exists():
            continue
        original = path.read_text()
        updated = _cleanup_js_text(path, original)
        overrides[path] = updated
        if updated != original:
            changed.append(path)

    # PHASE 2: strict whole-tree check against the would-be source tree.
    remaining = _scan_active_tree(root, overrides)
    if remaining:
        print("Brand Scope removal is NOT ready to commit.")
        print("No files were written.")
        print("\nRemaining active references:")
        for item in remaining[:200]:
            print(" -", item)
        if len(remaining) > 200:
            print(f" - ... plus {len(remaining) - 200} more")
        if args.strict:
            raise SystemExit(2)

    print("Brand Scope source cleanup validation passed.")
    print("Files that would change:" if args.dry_run else "Files to change:")
    if changed:
        for path in changed:
            print(" -", path.relative_to(root))
    else:
        print(" - none")

    if args.dry_run:
        print("Dry run only: no files written.")
        return

    # PHASE 3: commit only after every Python file and the whole active tree have
    # passed validation.
    for path in changed:
        _backup(path)
        path.write_text(overrides[path])

    print("\nBrand Scope source cleanup committed successfully.")
    print("Backups use suffix: .pre-brand-scope-removal")
    print("Verified: no active Page/Sitemap Brand Scope implementation references remain.")


if __name__ == "__main__":
    main()
