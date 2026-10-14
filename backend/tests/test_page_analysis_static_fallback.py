from app.services.crawler import _static_html_structure, _suspicious_extraction


def test_static_fallback_extracts_content_rich_html():
    body = " ".join(["term insurance protection premium family future coverage"] * 20)
    html = f"""
    <!doctype html>
    <html>
      <head>
        <title>Term Insurance Plans</title>
        <meta name="description" content="Term insurance comparison page">
        <link rel="canonical" href="/insurance/term-insurance-plans">
      </head>
      <body>
        <header><p>Global navigation</p></header>
        <main>
          <h1>Term Insurance</h1>
          <p>{body}</p>
          <h2>Benefits of Term Insurance</h2>
          <p>{body}</p>
          <ul>
            <li>High coverage at low cost</li>
            <li>Customisable plans</li>
          </ul>
        </main>
        <form><p>Lead form noise must not become page content.</p></form>
      </body>
    </html>
    """

    structure = _static_html_structure(
        html,
        "https://www.example.com/insurance/term-insurance-plans",
    )

    assert structure["root"] == "main"
    assert structure["title"] == "Term Insurance Plans"
    assert structure["canonical_url"] == (
        "https://www.example.com/insurance/term-insurance-plans"
    )
    assert structure["h1"] == ["Term Insurance"]
    assert "Benefits of Term Insurance" in structure["h2"]
    assert "High coverage at low cost" in structure["main_text"]
    assert "Lead form noise" in structure["main_text"]
    assert not _suspicious_extraction(structure)


def test_static_fallback_uses_body_when_main_is_only_a_shell():
    body = " ".join(["useful visible server rendered page content"] * 35)
    html = f"""
    <!doctype html>
    <html>
      <head><title>Useful Page</title></head>
      <body>
        <main><div id="app-shell"></div></main>
        <section>
          <h1>Useful Page Heading</h1>
          <p>{body}</p>
        </section>
      </body>
    </html>
    """

    structure = _static_html_structure(html, "https://www.example.com/page")

    assert structure["root"] == "body"
    assert structure["h1"] == ["Useful Page Heading"]
    assert "server rendered page content" in structure["main_text"]
    assert not _suspicious_extraction(structure)



def test_static_fallback_prefers_clean_body_when_main_is_partial_app_shell():
    hero = " ".join(["term insurance hero premium cover family"] * 110)
    article = " ".join(["long form term insurance guidance benefits eligibility claims"] * 260)
    html = f"""
    <!doctype html>
    <html>
      <head><title>Term Insurance Plans 2026</title></head>
      <body>
        <header><p>{'navigation ' * 300}</p></header>
        <main>
          <div class="hero-copy">{hero}</div>
        </main>
        <section class="seo-content">
          <h1>Term Insurance</h1>
          <h2>What is term insurance?</h2>
          <p>{article}</p>
          <h2>Benefits of term insurance</h2>
          <p>{article}</p>
        </section>
        <footer><p>{'footer ' * 200}</p></footer>
      </body>
    </html>
    """

    structure = _static_html_structure(
        html,
        "https://www.example.com/insurance/term-insurance-plans",
    )

    assert structure["root"] == "body"
    assert structure["h1"] == ["Term Insurance"]
    assert "What is term insurance?" in structure["h2"]
    assert len(structure["main_text"].split()) > 3000
    assert structure["_extraction"]["root_selection_reason"] in {
        "body_preserves_more_editorial_content",
        "semantic_root_missing_heading_structure",
        "semantic_root_covers_less_than_60pct_of_clean_body",
    }
