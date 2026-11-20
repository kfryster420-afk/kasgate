# Contributing to KasGate

Thanks for your interest. Bug reports, fixes and documentation improvements are welcome.

## Before you start

- For anything larger than a small fix, open an issue first so we can agree on the approach.
- Report security problems privately, as described in [SECURITY.md](SECURITY.md), not in public issues.
- The wire protocol is documented in [docs/CONTRACTS.md](docs/CONTRACTS.md). Do not rename a field, header, route or error code without discussing it.

## Setup

[docs/RUNBOOK.md](docs/RUNBOOK.md) covers configuration, running the stack and every test suite. The offline checks below need no funds, no gateway and no network access to Kaspa.

```bash
# gateway (Python 3.12)
cd apps/gateway
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q && ruff check . && mypy --strict app

# buyer SDK and MCP server
pip install -e "packages/buyer-sdk[test]" -e "packages/mcp-server[test]"
(cd packages/buyer-sdk && pytest -q)
(cd packages/mcp-server && pytest -q)

# web (Node 20 or newer)
cd apps/web && npm ci && npm run typecheck && npm run lint && npm run build
```

The integration suite in `tests/integration` has an offline tier that runs without a gateway; see its README.

## Pull requests

- Keep a change focused, with tests for new behaviour. Payment and authentication code needs a test for the failure case, not only the happy path.
- CI must pass: `ruff`, `mypy --strict`, the offline test suites and the web lint, typecheck and build.
- Never commit secrets: private keys, `.env` files, API keys or `*.key` files. Test fixtures must use throw-away testnet keys and say so.
- Use clear commit messages in the imperative mood, for example `fix: reject payments with duplicate outputs`.

By contributing you agree that your contribution is licensed under the MIT license of this repository.
