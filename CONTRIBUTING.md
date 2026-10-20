# Contributing

1. Fork the repository and create a focused branch.
2. Use Python 3.11+, install requirements.lock.txt, and run `python -m pytest -q`.
3. Work with temporary data and mock providers. Do not run paid pipelines or write to a real WeChat account as an automated test.
4. After changing requests or routes, run `python scripts/export_api_docs.py` and `python scripts/export_api_docs.py --check`.
5. Run `python scripts/check_secrets.py` and inspect `git diff --cached` before publishing.
6. Explain the user-visible behavior, relevant validation and remaining limitations in the pull request.

Keep default demo mode, localhost binding, draft-only delivery, external-request safeguards and unknown-result reconciliation. Do not add credentials, personal runtime records, scraped articles/images or generated production artifacts. Priorities are listed in [known issues](docs/KNOWN_ISSUES.md).

An optional GitHub Actions template is included at docs/ci.example.yml. Enabling it under .github/workflows/ci.yml requires an account/token authorized to edit workflows; this release does not include an active workflow or claim Linux CI validation.
