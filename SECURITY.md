# Security and credentials

This is a single-user, local-first application. The default server binds to 127.0.0.1:8088. Do not expose it directly to the public internet or assume it provides production multi-tenant authentication.

## Credential storage

- Keep model API keys, WeChat AppSecret and administrator tokens out of Git, screenshots, issue reports and logs.
- .env.example contains placeholders only. .env, data/, database files, logs and certificate keys are ignored.
- UI settings are encrypted with the current Windows user's DPAPI. On other platforms they are stored in a local file with restrictive permissions, **not equivalent at-rest encryption**.
- DPAPI files are user/machine-bound and must not be treated as portable credential backups.
- The administrator bearer token is stored locally under data/. The browser keeps its login token in localStorage. This design assumes a trusted local device.
- Export bundles may contain complete articles and source material. Inspect anything you share.

## Runtime boundaries

Remote image fetches enforce public-address and redirect checks, TLS verification and response limits. WeChat creation intents are persisted before writes; ambiguous network outcomes require reconciliation rather than blind replay.

Pause/cancel is cooperative and does not refund already-sent requests. The separate illustrations job is not yet integrated into all control paths; see [known issues](docs/KNOWN_ISSUES.md).

## Reporting

For a suspected vulnerability, use GitHub private vulnerability reporting if enabled, or open an issue requesting a private contact without exploit details or credentials. Do not attach real databases, .env, runtime settings, tokens or account screenshots. If credentials are exposed, revoke/rotate them with the provider; deleting a Git commit alone does not revoke a key.

Run `python scripts/check_secrets.py` before committing. Pattern checks are an additional safeguard, not a guarantee that all private information is detected.
