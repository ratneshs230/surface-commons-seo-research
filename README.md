# surface-commons-seo-research

**Free, open-source SEO research tool — replaces basic Ahrefs / SEMrush features.**

Built by the [Surface Commons](https://surface.agentnation.in/commons/) guild. Mirrored from the canonical implementation at `app/api/commons/seo_research.py` in the [Surface](https://github.com/ratneshs230/Surface-V2) monorepo.

## What it does

| Endpoint | SC | Source |
|---|---|---|
| `POST /commons/seo/keywords`    | 3 | Google Autocomplete (free public XML) |
| `POST /commons/seo/competitors` | 5 | DuckDuckGo HTML SERP (no API key) |
| `POST /commons/seo/site-audit`  | 4 | Direct httpx fetch + lxml parse |
| `GET  /commons/seo/health`      | — | Liveness + cost table |

- **`/keywords`** returns ranked related keywords with a heuristic relative-volume score. The upstream (Google Autocomplete) does not expose real search volumes — we expose this honestly via `volume_kind: "estimated"` and rank by upstream order (top suggestion = most-typed of the bunch).
- **`/competitors`** returns the top-10 organic results: rank, url, domain, title, snippet.
- **`/site-audit`** returns title length, meta-description length, H1 count + texts, internal/external link counts, image alt-text coverage %, page weight in KB, and a list of warnings (`title_too_long`, `missing_meta_description`, `multiple_h1`, `low_alt_coverage`, `page_weight_over_2mb`).

## Auth

`X-Service-Key` + `X-Agent-Id` headers — the standard Surface AN-on-behalf-of-user convention. AN backend forwards user requests; humans never hit Surface directly.

## Pricing & ledger

SC is pre-debited before the work runs. A `tool_calls` audit row is always written (success or failure). On caller-observable failure (HTTP 4xx/5xx, exception) the SC is refunded automatically.

## Cache

Per-input cache, 1-hour TTL, in-process. Be a good neighbour to the free upstreams (Google Autocomplete, DuckDuckGo HTML).

## Dependencies

`httpx`, `lxml`, `fastapi`, `pydantic`, `sqlalchemy[asyncio]`. Zero new pip installs on a Surface deploy.

## Tests

17 unit tests in `tests/test_seo_research.py` cover XML parsing, DDG SERP scraping, HTML audit warnings, ranking, URL normalisation, and the `/health` route. No network needed for any test — fixture HTML throughout.

```
$ pytest tests/test_seo_research.py -q
17 passed in 0.11s
```

## Live curl example

```
curl -X POST https://surface.agentnation.in/commons/seo/keywords \
  -H "Content-Type: application/json" \
  -H "X-Service-Key: ${SURFACE_SERVICE_KEY}" \
  -H "X-Agent-Id: ${AGENT_ID}" \
  -d '{"seed_keyword":"digital marketing","country":"IN"}'
```

Sample response (truncated):

```json
{
  "seed_keyword": "digital marketing",
  "country": "IN",
  "source": "google_autocomplete",
  "volume_kind": "estimated",
  "results": [
    {"keyword":"digital marketing courses","rank":1,"relative_volume_score":100,"tokens":3},
    {"keyword":"digital marketing courses in pune","rank":2,"relative_volume_score":96,"tokens":5},
    {"keyword":"digital marketing agency pune","rank":3,"relative_volume_score":87,"tokens":4}
  ],
  "result_count": 9,
  "cached": false,
  "sc_cost": 3.0,
  "tool": "seo_keywords"
}
```

## License

MIT — see `LICENSE`. Same posture as the rest of `surface-commons`.
