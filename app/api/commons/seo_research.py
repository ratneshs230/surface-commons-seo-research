"""commons/seo_research — Free SEO Research tool (Commons guild).

Replaces basic Ahrefs / Semrush features. Three real endpoints:

  • POST /commons/seo/keywords       — seed keyword → related keywords + estimated volumes
  • POST /commons/seo/competitors    — keyword → top 10 organic results (DDG HTML)
  • POST /commons/seo/site-audit     — domain → on-page SEO snapshot
  • GET  /commons/seo/health         — liveness probe

Auth: X-Service-Key + X-Agent-Id (the Surface convention; AN forwards on behalf
of users). SC charged + audited via tool_calls + sc_transactions:

    keywords   = 3 SC
    competitors= 5 SC
    site-audit = 4 SC

Free-tier sources (no paid keys):
  • Google Autocomplete (suggestqueries.google.com, public XML)
  • DuckDuckGo HTML (html.duckduckgo.com, no-JS results page)
  • Direct httpx fetch of the audited homepage

Results are cached in-process for 1 hour (per-input key) to be respectful of
rate limits.

Volumes returned by /keywords are *estimated* — Autocomplete does not expose
real volume. We rank suggestions by Google's own ordering (top suggestion =
most-searched of the bunch) and compute a heuristic relative score (0-100).
This is documented in the response payload (`volume_kind = "estimated"`).
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import quote_plus, urlparse, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from lxml import html as lxml_html
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.service_auth import require_service_key_and_agent
from app.models.agent import Agent

log = logging.getLogger("surface.commons.seo")

router = APIRouter(prefix="/commons/seo", tags=["commons-seo"])

# ───────────────────────── pricing ───────────────────────────────────────────

SC_COSTS: dict[str, Decimal] = {
    "seo_keywords":    Decimal("3"),
    "seo_competitors": Decimal("5"),
    "seo_site_audit":  Decimal("4"),
}

# ───────────────────────── tiny in-process cache ─────────────────────────────

_CACHE_TTL_SECONDS = 60 * 60  # 1 hour
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    hit = _cache.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


# ───────────────────────── SC ledger helpers ─────────────────────────────────

class _InsufficientFunds(Exception):
    pass


async def _debit_sc(
    db: AsyncSession,
    agent: Agent,
    cost_sc: Decimal,
    tool_name: str,
    ref_id: str,
) -> Decimal:
    """Pre-debit SC. Raises _InsufficientFunds if balance is too low.
    Writes one row to sc_transactions and updates agents.wallet_balance.
    Returns the new balance.
    """
    cost_f = float(cost_sc)
    current = float(agent.wallet_balance or 0.0)
    if current < cost_f:
        raise _InsufficientFunds(
            f"need {cost_f} SC, have {current:.4f} SC"
        )
    new_balance = current - cost_f
    agent.wallet_balance = new_balance

    await db.execute(
        text(
            "INSERT INTO sc_transactions "
            "(id, agent_id, delta, reason, ref_id, balance_after, note, created_at) "
            "VALUES (:id, :agent_id, :delta, :reason, :ref_id, :balance, :note, now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent.id,
            "delta": -cost_f,
            "reason": f"commons:{tool_name}",
            "ref_id": ref_id,
            "balance": new_balance,
            "note": f"commons {tool_name}",
        },
    )
    return Decimal(str(new_balance))


async def _refund_sc(
    db: AsyncSession,
    agent: Agent,
    cost_sc: Decimal,
    tool_name: str,
    ref_id: str,
) -> None:
    """Refund a previously-debited cost (caller-observable failure)."""
    cost_f = float(cost_sc)
    current = float(agent.wallet_balance or 0.0)
    new_balance = current + cost_f
    agent.wallet_balance = new_balance

    await db.execute(
        text(
            "INSERT INTO sc_transactions "
            "(id, agent_id, delta, reason, ref_id, balance_after, note, created_at) "
            "VALUES (:id, :agent_id, :delta, :reason, :ref_id, :balance, :note, now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent.id,
            "delta": cost_f,
            "reason": f"commons:{tool_name}:refund",
            "ref_id": ref_id,
            "balance": new_balance,
            "note": f"refund commons {tool_name}",
        },
    )


async def _record_tool_call(
    db: AsyncSession,
    *,
    agent_id: str,
    tool_name: str,
    input_json: dict[str, Any],
    output_len: int,
    sc_cost: Decimal,
    latency_ms: int,
    error: str | None,
) -> None:
    await db.execute(
        text(
            "INSERT INTO tool_calls "
            "(id, agent_id, tool_name, input_json, output_len, sc_cost, latency_ms, error, created_at) "
            "VALUES (:id, :agent_id, :tool_name, CAST(:input_json AS jsonb), :output_len, "
            ":sc_cost, :latency_ms, :error, now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "tool_name": tool_name,
            "input_json": _json_dumps(input_json),
            "output_len": output_len,
            "sc_cost": float(sc_cost),
            "latency_ms": latency_ms,
            "error": error,
        },
    )


def _json_dumps(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, default=str)[:10_000]
    except Exception:
        return "{}"


# ───────────────────────── accounted-call wrapper ────────────────────────────

class _CallEnvelope:
    """Tracks output size + carries cost/ref for finally-write."""

    def __init__(self, agent: Agent, tool_name: str, input_json: dict, cost: Decimal):
        self.agent = agent
        self.tool_name = tool_name
        self.input_json = input_json
        self.cost = cost
        self.ref_id = str(uuid.uuid4())
        self.output_len = 0
        self.t0 = time.perf_counter()
        self.error: str | None = None

    def set_output(self, output: Any) -> None:
        self.output_len = len(_json_dumps(output))

    @property
    def latency_ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)


async def _accounted(
    db: AsyncSession,
    agent: Agent,
    tool_name: str,
    input_json: dict[str, Any],
) -> _CallEnvelope:
    """Pre-debit + return an envelope. Caller must call _finalize after work."""
    cost = SC_COSTS[tool_name]
    env = _CallEnvelope(agent, tool_name, input_json, cost)
    try:
        await _debit_sc(db, agent, cost, tool_name, env.ref_id)
    except _InsufficientFunds as e:
        await _record_tool_call(
            db,
            agent_id=agent.id,
            tool_name=tool_name,
            input_json=input_json,
            output_len=0,
            sc_cost=Decimal("0"),
            latency_ms=0,
            error=f"insufficient_funds: {e}",
        )
        await db.commit()
        raise HTTPException(
            status_code=402,
            detail={"error": "insufficient_funds", "cost_sc": float(cost), "message": str(e)},
        )
    return env


async def _finalize(
    db: AsyncSession,
    env: _CallEnvelope,
    *,
    error: str | None = None,
) -> None:
    """Write tool_calls audit row. Refund if error. Commit."""
    if error is not None:
        await _refund_sc(db, env.agent, env.cost, env.tool_name, env.ref_id)
        sc_cost_logged = Decimal("0")
    else:
        sc_cost_logged = env.cost

    await _record_tool_call(
        db,
        agent_id=env.agent.id,
        tool_name=env.tool_name,
        input_json=env.input_json,
        output_len=env.output_len,
        sc_cost=sc_cost_logged,
        latency_ms=env.latency_ms,
        error=error,
    )
    await db.commit()


# ───────────────────────── request models ────────────────────────────────────

class KeywordsRequest(BaseModel):
    seed_keyword: str = Field(..., min_length=1, max_length=200)
    country: str = Field(default="IN", min_length=2, max_length=2)

    @field_validator("seed_keyword")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("seed_keyword empty")
        return v

    @field_validator("country")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class CompetitorsRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=200)
    country: str = Field(default="IN", min_length=2, max_length=2)

    @field_validator("keyword")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("keyword empty")
        return v


class SiteAuditRequest(BaseModel):
    domain: str = Field(..., min_length=3, max_length=253)

    @field_validator("domain")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("domain empty")
        return v


# ───────────────────────── core implementations ──────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (compatible; SurfaceCommonsSEO/1.0; +https://surface.agentnation.in/commons)"
)


def _normalise_url(value: str) -> str:
    """Domain → http(s) URL (homepage). Accepts bare host, http/https URL.
    Falls back to https when no scheme provided."""
    v = value.strip()
    if not v:
        raise ValueError("empty")
    if "://" not in v:
        v = "https://" + v
    p = urlparse(v)
    if not p.netloc:
        raise ValueError("invalid_url")
    # keep only scheme + netloc (homepage)
    return urlunparse((p.scheme, p.netloc, "/", "", "", ""))


async def _google_autocomplete(seed: str, country: str) -> list[str]:
    """Free public Google Autocomplete XML feed. Returns list of suggestions."""
    url = (
        "http://suggestqueries.google.com/complete/search"
        f"?q={quote_plus(seed)}&output=toolbar&hl=en&gl={country.lower()}"
    )
    async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": USER_AGENT}) as client:
        r = await client.get(url)
        r.raise_for_status()
        body = r.content
    return _parse_autocomplete_xml(body)


def _parse_autocomplete_xml(body: bytes) -> list[str]:
    """Extract <suggestion data="..."/> values without a full XML parser
    (the feed is well-formed; regex is fast and dep-free)."""
    suggestions: list[str] = []
    # data="value" — value never contains an unescaped "
    for m in re.finditer(rb'<suggestion[^>]*\bdata="([^"]+)"', body):
        try:
            suggestions.append(m.group(1).decode("utf-8", errors="replace"))
        except Exception:
            continue
    return suggestions


def _rank_suggestions(seed: str, suggestions: list[str]) -> list[dict[str, Any]]:
    """Convert Google suggestion order to a ranked list with heuristic
    relative volume. Top of list = highest score (Google ranks autocomplete
    by query frequency, so position is a meaningful proxy)."""
    # de-dupe while preserving order; drop the seed itself
    seen: set[str] = set()
    keep: list[str] = []
    seed_lower = seed.lower().strip()
    for s in suggestions:
        sl = s.lower().strip()
        if not sl or sl == seed_lower or sl in seen:
            continue
        seen.add(sl)
        keep.append(s)

    n = len(keep)
    out: list[dict[str, Any]] = []
    for i, kw in enumerate(keep):
        # Linear decay 100 → 30 across positions; never below 30.
        rel = max(30, int(100 - (70 * i / max(n - 1, 1))))
        # Token overlap bonus when the suggestion expands the seed
        bonus = 5 if seed_lower in kw.lower() else 0
        out.append({
            "keyword": kw,
            "rank": i + 1,
            "relative_volume_score": min(100, rel + bonus),
            "tokens": len(kw.split()),
        })
    return out


async def _ddg_html_search(query: str, country: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Scrape the no-JS DuckDuckGo HTML SERP. Free, no API key."""
    url = "https://html.duckduckgo.com/html/"
    region = {"IN": "in-en", "US": "us-en", "GB": "uk-en"}.get(country.upper(), "wt-wt")
    params = {"q": query, "kl": region}

    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        r = await client.post(url, data=params)
        r.raise_for_status()
        body = r.text

    return _parse_ddg_html(body, max_results)


def _parse_ddg_html(html_body: str, max_results: int) -> list[dict[str, Any]]:
    """Extract result rows from the DuckDuckGo HTML SERP."""
    try:
        tree = lxml_html.fromstring(html_body)
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    # Each result is in div.result (the no-JS page is stable). XPath is used
    # because lxml.cssselect requires the optional `cssselect` package which
    # is not installed on Surface; XPath is built-in.
    result_nodes = tree.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' result ')]"
    )
    for node in result_nodes[: max_results * 2]:
        a_nodes = node.xpath(
            ".//a[contains(concat(' ', normalize-space(@class), ' '), ' result__a ')]"
        )
        snippet_nodes = node.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' result__snippet ')]"
        )
        if not a_nodes:
            continue
        a = a_nodes[0]
        href = a.get("href") or ""
        # DuckDuckGo wraps URLs in /l/?uddg=...
        href = _unwrap_ddg(href)
        if not href:
            continue
        try:
            domain = urlparse(href).netloc.lower().lstrip("www.")
        except Exception:
            domain = ""
        title = (a.text_content() or "").strip()
        snippet = (snippet_nodes[0].text_content().strip() if snippet_nodes else "")
        out.append({
            "rank": len(out) + 1,
            "url": href,
            "domain": domain,
            "title": title,
            "snippet": snippet,
        })
        if len(out) >= max_results:
            break
    return out


def _unwrap_ddg(href: str) -> str:
    """DDG redirect URLs look like //duckduckgo.com/l/?uddg=https%3A%2F%2F...
    Return the underlying URL.
    """
    from urllib.parse import parse_qs, unquote

    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if "duckduckgo.com" in p.netloc and p.path.startswith("/l/"):
        q = parse_qs(p.query)
        u = q.get("uddg", [None])[0]
        if u:
            return unquote(u)
    if href.startswith("http"):
        return href
    return ""


async def _audit_homepage(domain: str) -> dict[str, Any]:
    """Fetch homepage + return on-page SEO snapshot."""
    target = _normalise_url(domain)
    parsed_target = urlparse(target)
    target_host = parsed_target.netloc.lower()

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        try:
            r = await client.get(target)
        except httpx.ConnectError:
            # Try http:// fallback if https failed
            if target.startswith("https://"):
                fallback = "http://" + target_host + "/"
                r = await client.get(fallback)
            else:
                raise
        r.raise_for_status()
        body = r.text
        page_bytes = len(r.content)
        final_url = str(r.url)

    return _parse_audit(body, page_bytes, final_url, target_host)


def _parse_audit(html_body: str, page_bytes: int, final_url: str, host: str) -> dict[str, Any]:
    try:
        tree = lxml_html.fromstring(html_body)
    except Exception:
        # Empty or unparseable
        return {
            "url": final_url,
            "title": "",
            "title_length": 0,
            "meta_description": "",
            "meta_description_length": 0,
            "h1_count": 0,
            "h1_texts": [],
            "internal_links": 0,
            "external_links": 0,
            "image_count": 0,
            "images_with_alt": 0,
            "alt_text_coverage_pct": 0.0,
            "page_weight_kb": round(page_bytes / 1024, 2),
            "warnings": ["html_parse_failed"],
        }

    title_nodes = tree.xpath("//title")
    title = (title_nodes[0].text_content().strip() if title_nodes else "")

    meta_desc = ""
    for n in tree.xpath('//meta[@name="description" or @name="Description"]'):
        meta_desc = (n.get("content") or "").strip()
        if meta_desc:
            break

    h1_nodes = tree.xpath("//h1")
    h1_texts = [(n.text_content() or "").strip() for n in h1_nodes if (n.text_content() or "").strip()]

    a_nodes = tree.xpath("//a[@href]")
    internal = 0
    external = 0
    for a in a_nodes:
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        if href.startswith("/") or href.startswith("./") or href.startswith("../"):
            internal += 1
            continue
        try:
            netloc = urlparse(href).netloc.lower()
        except Exception:
            continue
        if not netloc or netloc == host or netloc.endswith("." + host) or host.endswith("." + netloc):
            internal += 1
        else:
            external += 1

    img_nodes = tree.xpath("//img")
    image_count = len(img_nodes)
    images_with_alt = 0
    for img in img_nodes:
        alt = img.get("alt")
        if alt is not None and alt.strip():
            images_with_alt += 1
    alt_pct = round((images_with_alt / image_count) * 100, 1) if image_count else 100.0

    warnings: list[str] = []
    # SEO best-practice ranges (Google snippet limits)
    if not title:
        warnings.append("missing_title")
    elif len(title) < 10:
        warnings.append("title_too_short")
    elif len(title) > 70:
        warnings.append("title_too_long")
    if not meta_desc:
        warnings.append("missing_meta_description")
    elif len(meta_desc) > 160:
        warnings.append("meta_description_too_long")
    elif len(meta_desc) < 50:
        warnings.append("meta_description_too_short")
    if not h1_texts:
        warnings.append("missing_h1")
    elif len(h1_texts) > 1:
        warnings.append("multiple_h1")
    if image_count and alt_pct < 80:
        warnings.append("low_alt_coverage")
    if page_bytes > 2_000_000:
        warnings.append("page_weight_over_2mb")

    return {
        "url": final_url,
        "title": title,
        "title_length": len(title),
        "meta_description": meta_desc,
        "meta_description_length": len(meta_desc),
        "h1_count": len(h1_texts),
        "h1_texts": h1_texts[:5],
        "internal_links": internal,
        "external_links": external,
        "image_count": image_count,
        "images_with_alt": images_with_alt,
        "alt_text_coverage_pct": alt_pct,
        "page_weight_kb": round(page_bytes / 1024, 2),
        "warnings": warnings,
    }


# ───────────────────────── routes ────────────────────────────────────────────

@router.get("/health")
async def seo_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "tool": "commons-seo-research",
        "endpoints": [
            "POST /commons/seo/keywords",
            "POST /commons/seo/competitors",
            "POST /commons/seo/site-audit",
        ],
        "sc_costs": {k: float(v) for k, v in SC_COSTS.items()},
        "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        "providers": ["google_autocomplete", "duckduckgo_html", "direct_fetch"],
    }


@router.post("/keywords")
async def seo_keywords(
    body: KeywordsRequest,
    agent: Agent = Depends(require_service_key_and_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    input_json = body.model_dump()
    cache_key = f"kw:{body.country}:{body.seed_keyword.lower()}"

    env = await _accounted(db, agent, "seo_keywords", input_json)
    error: str | None = None
    try:
        cached = _cache_get(cache_key)
        if cached is not None:
            payload = {**cached, "cached": True}
        else:
            suggestions = await _google_autocomplete(body.seed_keyword, body.country)
            ranked = _rank_suggestions(body.seed_keyword, suggestions)
            payload = {
                "seed_keyword": body.seed_keyword,
                "country": body.country,
                "source": "google_autocomplete",
                "volume_kind": "estimated",
                "results": ranked,
                "result_count": len(ranked),
                "cached": False,
            }
            _cache_set(cache_key, payload)
        env.set_output(payload)
        return {**payload, "sc_cost": float(env.cost), "tool": "seo_keywords"}
    except HTTPException:
        error = "http_error"
        raise
    except httpx.HTTPError as e:
        error = f"upstream:{type(e).__name__}: {e}"[:500]
        raise HTTPException(status_code=502, detail={"error": "upstream_failed", "message": str(e)})
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        raise HTTPException(status_code=500, detail={"error": "internal", "message": str(e)})
    finally:
        await _finalize(db, env, error=error)


@router.post("/competitors")
async def seo_competitors(
    body: CompetitorsRequest,
    agent: Agent = Depends(require_service_key_and_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    input_json = body.model_dump()
    cache_key = f"cp:{body.country}:{body.keyword.lower()}"

    env = await _accounted(db, agent, "seo_competitors", input_json)
    error: str | None = None
    try:
        cached = _cache_get(cache_key)
        if cached is not None:
            payload = {**cached, "cached": True}
        else:
            results = await _ddg_html_search(body.keyword, body.country, max_results=10)
            payload = {
                "keyword": body.keyword,
                "country": body.country,
                "source": "duckduckgo_html",
                "results": results,
                "result_count": len(results),
                "cached": False,
            }
            _cache_set(cache_key, payload)
        env.set_output(payload)
        return {**payload, "sc_cost": float(env.cost), "tool": "seo_competitors"}
    except HTTPException:
        error = "http_error"
        raise
    except httpx.HTTPError as e:
        error = f"upstream:{type(e).__name__}: {e}"[:500]
        raise HTTPException(status_code=502, detail={"error": "upstream_failed", "message": str(e)})
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        raise HTTPException(status_code=500, detail={"error": "internal", "message": str(e)})
    finally:
        await _finalize(db, env, error=error)


@router.post("/site-audit")
async def seo_site_audit(
    body: SiteAuditRequest,
    agent: Agent = Depends(require_service_key_and_agent),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    input_json = body.model_dump()
    cache_key = f"au:{body.domain.lower()}"

    env = await _accounted(db, agent, "seo_site_audit", input_json)
    error: str | None = None
    try:
        cached = _cache_get(cache_key)
        if cached is not None:
            payload = {**cached, "cached": True}
        else:
            audit = await _audit_homepage(body.domain)
            payload = {
                "domain": body.domain,
                "audit": audit,
                "cached": False,
            }
            _cache_set(cache_key, payload)
        env.set_output(payload)
        return {**payload, "sc_cost": float(env.cost), "tool": "seo_site_audit"}
    except HTTPException:
        error = "http_error"
        raise
    except httpx.HTTPError as e:
        error = f"upstream:{type(e).__name__}: {e}"[:500]
        raise HTTPException(status_code=502, detail={"error": "upstream_failed", "message": str(e)})
    except ValueError as e:
        error = f"validation:{e}"[:500]
        raise HTTPException(status_code=400, detail={"error": "bad_domain", "message": str(e)})
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        raise HTTPException(status_code=500, detail={"error": "internal", "message": str(e)})
    finally:
        await _finalize(db, env, error=error)
