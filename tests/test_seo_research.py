"""Tests for app.api.commons.seo_research.

Covers the pure parsing / ranking helpers (no network) and a route-level
smoke test for /commons/seo/health on an isolated FastAPI app. Unlike the
network-dependent /keywords, /competitors, /site-audit endpoints, the parsers
are deterministic and fully exercise the SEO logic against fixture HTML/XML.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.commons.seo_research import (
    SC_COSTS,
    _normalise_url,
    _parse_audit,
    _parse_autocomplete_xml,
    _parse_ddg_html,
    _rank_suggestions,
    _unwrap_ddg,
    router as commons_seo_router,
)


# ─────────────────── /health route ────────────────────────────────────────────

def test_health_route_returns_costs_and_endpoints() -> None:
    app = FastAPI()
    app.include_router(commons_seo_router)
    client = TestClient(app)

    r = client.get("/commons/seo/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["tool"] == "commons-seo-research"
    assert "POST /commons/seo/keywords" in data["endpoints"]
    assert "POST /commons/seo/competitors" in data["endpoints"]
    assert "POST /commons/seo/site-audit" in data["endpoints"]
    # Mandated SC pricing: keywords=3, competitors=5, site-audit=4
    assert data["sc_costs"]["seo_keywords"] == 3.0
    assert data["sc_costs"]["seo_competitors"] == 5.0
    assert data["sc_costs"]["seo_site_audit"] == 4.0
    assert "google_autocomplete" in data["providers"]
    assert "duckduckgo_html" in data["providers"]


def test_sc_cost_table_matches_spec() -> None:
    assert int(SC_COSTS["seo_keywords"]) == 3
    assert int(SC_COSTS["seo_competitors"]) == 5
    assert int(SC_COSTS["seo_site_audit"]) == 4


# ─────────────────── Google Autocomplete XML parsing ─────────────────────────

AUTOCOMPLETE_XML = b"""<?xml version="1.0"?>
<toplevel>
<CompleteSuggestion><suggestion data="digital marketing course"/></CompleteSuggestion>
<CompleteSuggestion><suggestion data="digital marketing salary in india"/></CompleteSuggestion>
<CompleteSuggestion><suggestion data="digital marketing jobs"/></CompleteSuggestion>
<CompleteSuggestion><suggestion data="digital marketing strategy"/></CompleteSuggestion>
<CompleteSuggestion><suggestion data="digital marketing institute"/></CompleteSuggestion>
</toplevel>"""


def test_parse_autocomplete_xml_extracts_all_suggestions() -> None:
    out = _parse_autocomplete_xml(AUTOCOMPLETE_XML)
    assert len(out) == 5
    assert out[0] == "digital marketing course"
    assert "digital marketing strategy" in out


def test_parse_autocomplete_xml_handles_empty_body() -> None:
    assert _parse_autocomplete_xml(b"") == []
    assert _parse_autocomplete_xml(b"<toplevel></toplevel>") == []


def test_rank_suggestions_orders_by_position_and_dedupes() -> None:
    seed = "digital marketing"
    raw = [
        "digital marketing",  # the seed itself — must be filtered
        "digital marketing course",
        "Digital Marketing Course",  # dupe (case-insensitive)
        "digital marketing salary",
        "digital marketing jobs",
    ]
    out = _rank_suggestions(seed, raw)
    keywords = [r["keyword"] for r in out]
    # seed and case-dupe filtered, original order preserved
    assert keywords == ["digital marketing course", "digital marketing salary", "digital marketing jobs"]
    # Top suggestion must score highest
    assert out[0]["relative_volume_score"] >= out[-1]["relative_volume_score"]
    # Each result has the required shape
    for r in out:
        assert "rank" in r
        assert "relative_volume_score" in r
        assert 30 <= r["relative_volume_score"] <= 100
        assert r["tokens"] > 0


def test_rank_suggestions_empty_input() -> None:
    assert _rank_suggestions("seed", []) == []


# ─────────────────── DuckDuckGo HTML SERP parsing ────────────────────────────

DDG_FIXTURE = """<html><body>
<div class="results">
  <div class="result results_links">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage1&amp;rut=abc">
        Example Page Title
      </a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage1">
      This is the snippet for the first result describing the page.
    </a>
  </div>
  <div class="result">
    <a class="result__a" href="https://second.example.org/article">
      Second Site Title
    </a>
    <div class="result__snippet">Snippet for second result</div>
  </div>
  <div class="result result--ad">
    <!-- ad without a result__a should be skipped or yield nothing -->
  </div>
</body></html>"""


def test_parse_ddg_html_extracts_results_and_unwraps_redirect() -> None:
    out = _parse_ddg_html(DDG_FIXTURE, max_results=10)
    assert len(out) == 2
    first = out[0]
    assert first["rank"] == 1
    assert first["url"] == "https://example.com/page1"
    assert first["domain"] == "example.com"
    assert "Example Page Title" in first["title"]
    assert "first result" in first["snippet"]

    second = out[1]
    assert second["rank"] == 2
    assert second["url"] == "https://second.example.org/article"
    assert second["domain"] == "second.example.org"


def test_parse_ddg_html_respects_max_results() -> None:
    body = "<html><body>" + ('<div class="result"><a class="result__a" href="https://e.com">x</a></div>' * 15) + "</body></html>"
    out = _parse_ddg_html(body, max_results=5)
    assert len(out) == 5


def test_parse_ddg_html_invalid_input() -> None:
    assert _parse_ddg_html("", 10) == []


def test_unwrap_ddg_redirect() -> None:
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=x"
    assert _unwrap_ddg(wrapped) == "https://example.com/a?b=1"
    # already-absolute URL passes through
    assert _unwrap_ddg("https://target.com/x") == "https://target.com/x"
    assert _unwrap_ddg("") == ""


# ─────────────────── Site-audit HTML parsing ─────────────────────────────────

GOOD_HOMEPAGE_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<title>Acme Widgets — Premium Industrial Widgets Since 1992</title>
<meta name="description" content="Acme makes the highest quality industrial widgets in the world. Shop online with free shipping in India.">
</head><body>
<header><h1>Acme Widgets</h1></header>
<main>
  <p>Welcome. Browse our <a href="/catalog">catalog</a> or <a href="/about">about us</a>.</p>
  <p>News from <a href="https://news.example.com/article">our partner</a>.</p>
  <p>External link <a href="https://x.com/acme">on social</a>.</p>
  <img src="/img/hero.jpg" alt="Hero widget photo">
  <img src="/img/lineup.jpg" alt="Product lineup">
  <img src="/img/missing-alt.jpg">
</main>
</body></html>"""


def test_parse_audit_extracts_full_snapshot() -> None:
    body_bytes = len(GOOD_HOMEPAGE_HTML.encode())
    snap = _parse_audit(
        GOOD_HOMEPAGE_HTML, body_bytes,
        "https://acme.example.com/", "acme.example.com",
    )
    assert snap["title"].startswith("Acme Widgets")
    assert snap["title_length"] > 10
    assert "highest quality" in snap["meta_description"]
    assert snap["meta_description_length"] > 0
    assert snap["h1_count"] == 1
    assert snap["h1_texts"] == ["Acme Widgets"]
    # /catalog, /about (relative) → internal; news.example.com + x.com → external
    assert snap["internal_links"] >= 2
    assert snap["external_links"] >= 2
    assert snap["image_count"] == 3
    assert snap["images_with_alt"] == 2
    assert snap["alt_text_coverage_pct"] == round(2 / 3 * 100, 1)
    assert snap["page_weight_kb"] > 0
    # 2 of 3 images have alt → low_alt_coverage warning fires (66% < 80%)
    assert "low_alt_coverage" in snap["warnings"]


BARE_HTML = "<html><head></head><body></body></html>"


def test_parse_audit_empty_page_emits_warnings() -> None:
    snap = _parse_audit(BARE_HTML, len(BARE_HTML), "https://x.com/", "x.com")
    assert snap["title_length"] == 0
    assert snap["meta_description_length"] == 0
    assert snap["h1_count"] == 0
    assert "missing_title" in snap["warnings"]
    assert "missing_meta_description" in snap["warnings"]
    assert "missing_h1" in snap["warnings"]


LONG_TITLE_HTML = """<html><head>
<title>This is an extremely long page title that exceeds the seventy character limit set by Google for SEO previews</title>
<meta name="description" content="x">
</head><body><h1>One</h1><h1>Two</h1></body></html>"""


def test_parse_audit_detects_long_title_and_multiple_h1() -> None:
    snap = _parse_audit(LONG_TITLE_HTML, 1024, "https://x.com/", "x.com")
    assert snap["title_length"] > 70
    assert "title_too_long" in snap["warnings"]
    assert snap["h1_count"] == 2
    assert "multiple_h1" in snap["warnings"]
    assert "meta_description_too_short" in snap["warnings"]


def test_parse_audit_with_no_images_returns_full_alt_coverage() -> None:
    body = "<html><head><title>Hello world this is good</title>" \
           "<meta name='description' content='" + ("a" * 100) + "'>" \
           "</head><body><h1>Hi</h1></body></html>"
    snap = _parse_audit(body, len(body), "https://x.com/", "x.com")
    assert snap["image_count"] == 0
    assert snap["alt_text_coverage_pct"] == 100.0
    assert "low_alt_coverage" not in snap["warnings"]


def test_parse_audit_handles_unparseable_body() -> None:
    snap = _parse_audit("", 0, "https://x.com/", "x.com")
    assert snap["title_length"] == 0
    assert snap["page_weight_kb"] == 0.0


# ─────────────────── URL normalisation ───────────────────────────────────────

def test_normalise_url_adds_https_for_bare_host() -> None:
    assert _normalise_url("example.com") == "https://example.com/"
    assert _normalise_url("https://example.com/path?q=1") == "https://example.com/"
    assert _normalise_url("http://x.in") == "http://x.in/"


def test_normalise_url_rejects_empty() -> None:
    import pytest
    with pytest.raises(ValueError):
        _normalise_url("")
    with pytest.raises(ValueError):
        _normalise_url("   ")
