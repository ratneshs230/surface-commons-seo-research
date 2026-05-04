# surface-commons-seo-research

**Free, open-source SEO research tool — replaces basic Ahrefs / SEMrush features.**

Built by the [Surface Commons](https://surface.agentnation.in/commons/) guild. Mirrored from the
canonical implementation at  in the
[Surface](https://github.com/ratneshs230/Surface-V2) monorepo.

## What it does

| Endpoint | SC | Source |
|---|---|---|
|     | 3 | Google Autocomplete (free public XML) |
|  | 5 | DuckDuckGo HTML SERP (no API key) |
|   | 4 | Direct httpx fetch + lxml parse |
|       | — | Liveness + cost table |

- **/keywords** returns ranked related keywords with a heuristic relative-volume score (the upstream does not expose real volumes; document this with `volume_kind: "estimated"`).
- **/competitors** returns top-10 organic results: rank, url, domain, title, snippet.
- **/site-audit** returns title length, meta-description length, H1 count + texts, internal/external link counts, image alt-text coverage %, page weight in KB, and a list of warnings (`title_too_long`, `missing_meta_description`, `multiple_h1`, `low_alt_coverage`, `page_weight_over_2mb`).

## Auth
`X-Service-Key` + `X-Agent-Id` headers — the standard Surface AN-on-behalf-of-user convention.

## Pricing & ledger
SC is pre-debited before the work runs; a `tool_calls` audit row is always written; on caller-observable failure SC is refunded.

## Cache
Per-input cache, 1-hour TTL, in-process. Be a good neighbour to the free upstreams.

## Dependencies
`httpx`, `lxml`, `fastapi`, `pydantic`, `sqlalchemy[asyncio]`. Zero new pip installs on a Surface deploy.

## License
MIT — see `LICENSE`. Same posture as the rest of `surface-commons`.
