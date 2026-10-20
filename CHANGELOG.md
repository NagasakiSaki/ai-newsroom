# Changelog

## 0.2.0 — 2026-10-20

- General topic/audience/style configuration and optional legacy AI feed bundle.
- Persisted source URLs, selectors, explicit additional hosts, include/exclude keywords and any/all matching.
- Built-in RSS/Atom, HTML, article and XML sitemap crawler with public-address/redirect checks, robots rules, rate limits and collection checkpoints.
- Model-free local text library, source/query filters and JSON/CSV exports with formula escaping.
- Undated reference material retained without invented publication times; optional news writing keeps its own qualification.
- Library-first UI and separate collection/writing buttons. User settings and fetched bodies stay in ignored data/.
- Isolated browser checks covered configuration, source add/save, keyword search and saved-body reading. Python.org live capture yielded about 2,400 characters with zero model calls. Real model/WeChat services were not used.

Validation: complete Windows regression run 369 passed; final focused crawler/API checks cover the subsequent redirect-delay and keyword-URL fixes. OpenAPI consistency, JavaScript syntax, source/archive credential checks and documentation links also verified. Linux CI is not claimed.


## 0.1.0 — 2026-10-20

Initial experimental open-source snapshot:

- News ingestion, source/time policy, scoring, event crosscheck, merged evidence/planning and long-form writing.
- Original-image collection, bounded parallel downloads, illustration briefs and optional image-provider generation.
- Native MiniMax image protocol and compatible image adapters with bounded requests and caching.
- Manual article-count target, local editing/export, usage alerts and cooperative workflow controls.
- WeChat connection diagnostics and batch draft creation with revision/account matching and unknown-result reconciliation.
- Safe demo configuration, generic author defaults, sanitized synthetic test addresses, MIT license, public documentation and an optional CI template.

Pre-release checks: 340 tests passed on the sanitized Windows working tree; clean-profile demo smoke test passed; OpenAPI consistency, JavaScript syntax, staged-file credential matching and source-pattern checks passed. No real model or WeChat requests were used for release validation. CI is provided as docs/ci.example.yml and is not enabled because the publishing credential lacks workflow scope; Linux CI results are not claimed.

This release is a source-code snapshot with known limitations. It is not a validated Android app, public cloud service or guarantee of fully autonomous quality. See [known issues](docs/KNOWN_ISSUES.md).
