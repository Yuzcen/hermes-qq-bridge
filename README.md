# Hermes QQ Bridge

An open-source reference bridge for connecting QQ/NapCat OneBot v11 events to a Hermes Agent group profile through an HTTP API.

The bridge is deliberately separated from any live Hermes profile. It contains transport, batching, attachment handling, and tests, but no credentials, production chat history, runtime databases, or private user data.

## Scope

- OneBot v11 / NapCat event intake through NoneBot2
- Per-group short message batching and debounce
- Explicit sender and message identity handling
- Attachment URL/path normalization
- Final-response-only delivery
- Conservative behavior hints for direct calls, questions, technical topics, and small talk
- Structured privacy-conscious logs
- Hermes API integration through environment variables

The bridge does not make a universal semantic decision about whether a bot should speak. That decision belongs to the Hermes model prompt and the deployment's policy. Protocol, authorization, transport, and output-safety checks remain in the bridge.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
# Edit .env with local values
python bot.py
```

For a NoneBot deployment, configure NapCat's OneBot v11 reverse WebSocket client to point at the bridge process. Then configure the Hermes API endpoint and key in `.env`.

## Configuration

See [.env.example](.env.example). Never commit `.env`. Production values, QQ IDs, group lists, API keys, logs, databases, downloaded files, and chat exports are intentionally excluded from this repository.

## Tests

```bash
python -m pytest -q
```

Tests use fake OneBot events and mocked Hermes calls. They do not contact QQ, NapCat, or a production Hermes gateway.

## Design notes

The intended deployment layering is:

```text
QQ/NapCat -> OneBot v11 -> bridge transport -> Hermes profile -> model/tools
```

The bridge should stay small and auditable. Persona changes belong in the Hermes profile's `SOUL.md` or channel prompt. Per-group learning records should be kept outside the core persona prompt and loaded progressively by the deployment that owns them.

## Security notes

- Keep `.env`, runtime databases, logs, attachments, and exports outside version control.
- Use a dedicated QQ bot account rather than a personal QQ account.
- Restrict group and user allowlists in deployment configuration.
- Treat URLs and attachment paths as untrusted input.
- Do not forward provider errors, tool traces, internal reasoning, or framework status text to QQ.
- Review all changes to message routing and outbound delivery with tests before deployment.

## License

MIT. See [LICENSE](LICENSE).
