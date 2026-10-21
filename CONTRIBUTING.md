# Contributing

Thanks for looking at Free-Router. A few ground rules first.

## License

This repository is **proprietary** - see [LICENSE](LICENSE). By contributing you
agree that your contribution is licensed under the same terms and that the
copyright holder may decline or relicense it. Work that you are not allowed to
contribute (e.g. employer-owned code without permission) must not be submitted.

## What a good contribution looks like

- One topic per pull request; keep the diff focused.
- Add or update tests in `tests/` for behaviour changes. The suite must stay green:
  ```bash
  pip install -r requirements.txt
  python -m pytest tests -q
  ```
- Never commit credentials. Keys live in `.env` / the environment only. If your
  change touches config handling, run the secret scanner:
  ```bash
  python scripts/scan_secrets.py .
  ```
- Keep the zero-extra-dependency promise: the runtime depends only on
  `fastapi`, `uvicorn`, `httpx`, `pyyaml` and `pydantic`. Standard library only
  for metrics, cache and alerting.
- The gateway must stay framework-agnostic: it is an OpenAI-compatible HTTP
  service, not an agent plugin. Do not import Hermes, QwenPaw, LangChain, etc.
- Respect the provider abstraction in `app/providers/`: a new provider should
  slot in without touching the routing core unless the routing semantics
  genuinely change.
- Documentation is bilingual by default: Chinese and English side by side, as in
  the existing README/USAGE files.

## Reporting bugs

Open an issue with: what you ran, what you expected, what happened, the relevant
log lines, and your OS / Python version. Redact any keys that appear in logs.

## Code of conduct

Be respectful and constructive. No harassment, no spam, no unrelated promotion.
