# Flight Ops

Predicts how late US domestic flights will arrive (median plus an 80% range) and flags unusual flight behavior worldwide, running on free tiers ($0/month): GitHub Actions runs the pipelines, Cloudflare R2 stores the data, and the API and web app run locally.

Status: **Phase 0 (foundation)**. See `docs/setup.md` for one-time account setup.

## Development

```bash
uv sync                    # install Python 3.12 + dependencies into .venv
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy                # type-check src/ (strict)
uv run pytest              # tests (S3 is mocked; no cloud access needed)
```

## Layout

```
src/flightops/     Python package (config, storage; more modules arrive per phase)
tests/             pytest suites
.github/workflows/ CI: lint, types, tests, dependency audit
docs/              setup guide (ADRs, runbooks and the model card arrive in later phases)
```
