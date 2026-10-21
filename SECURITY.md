# Security Policy

## Reporting a vulnerability

Please report security issues privately by opening a GitHub Security Advisory
("Report a vulnerability" link on the Security tab) rather than a public issue.
Do not include live API keys in the report - redact them.

## Supported versions

Only the latest `main` branch receives fixes.

## What this project does with credentials

- **No credential is ever stored in this repository.** `config/config.yaml` keeps every
  `api_key` field empty; keys are read at runtime from the process environment or
  from a local `.env` file (see `.env.example`).
- `.env`, `config/.env`, `config/providers.local.yaml`, `data/` (SQLite usage DB,
  logs, probe reports) and `__pycache__/` are excluded by `.gitignore` and must
  never be committed.
- A CI job (`scripts/scan_secrets.py`) fails the build if a credential-shaped
  string is committed. Run it locally before pushing:
  ```bash
  python scripts/scan_secrets.py .
  ```
- If you ever commit a key by accident, treat it as compromised: revoke it at the
  provider first, then rewrite history (`git filter-repo` / force-push) and notify
  the maintainer. Removing it in a later commit is not enough.

## Listening exposure

The gateway binds to `127.0.0.1` by default. Do **not** bind it to `0.0.0.0` on a
public interface without putting an authenticating reverse proxy in front of it -
the upstream provider keys would then be usable by anyone who can reach the port.
