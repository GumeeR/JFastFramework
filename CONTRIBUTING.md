# Contributing

## Setting up

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

`[dev]` pulls every plugin extra. The suite tests the plugins, so it imports
what the plugins import; a narrower install collects ImportErrors rather than
running.

## What CI checks

Four gates, all of them runnable locally, and none of them advisory:

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest -q
```

Plus `mypy --platform win32 src` if you touched code that branches on the
operating system — `sys.platform` is what mypy narrows on, `os.name` is not.

Beyond that, CI builds the generated image and runs it against a real
PostgreSQL (`scripts/smoke_docker.sh`), runs the scaffolder end to end
(`scripts/smoke*.sh`), and runs the suite on Windows. If you are changing the
generator or the Dockerfile, run the relevant smoke script before pushing:
they catch the class of defect the unit tests structurally cannot, which is
"the thing we generate does not work".

## Tests

A test that skips is a test nobody is running. The suites needing a real
server skip without one, and CI fails if they skip there — so if you add one,
add it to the "Prove the tests needing a real server actually ran" step in
`.github/workflows/ci.yml` too.

Locally:

```bash
JFAST_TEST_PG_URL=postgresql+asyncpg://jfast:jfast@localhost:5432 \
JFAST_TEST_REDIS_URL=redis://localhost:6379/0 pytest -q
```

Two habits worth keeping, because both have already cost this project a
shipped defect:

- **Write the assertion against the real shape.** A test upstream that reads
  `dict(request.query_params)` agrees with a proxy that drops repeated
  parameters, and neither notices.
- **Prove the fix fails without the fix.** Break it back, watch the test go
  red, restore it. A test that passes either way is documentation.

## Security

`pip-audit` and `bandit` are hard gates. When a finding lands, upgrade the
package first; only if it cannot be upgraded, waive it explicitly
(`--ignore-vuln PYSEC-...`, or `# nosec BXXX`) with a comment saying which
code path makes it inapplicable. A check that goes yellow and is ignored has
stopped meaning anything.
