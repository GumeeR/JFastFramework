# Checkpoint — 2026-08-28

Where the framework actually is, written so the next session does not have to
re-derive it. Read `STATUS.md` for per-plugin maturity, `CHANGELOG.md` for what
changed when. This file is the *state of play*: decisions made, traps found, and
what is deliberately not done yet.

---

## Published

| | |
| --- | --- |
| PyPI | `jfastframework` — `0.1.0a1` published, `0.1.0a2` built and ready |
| TestPyPI | `0.1.0a1` |
| Repo | `github.com/JFabrizzio5/JFastFramework`, branch `main` |
| Local | WSL Ubuntu-26.04, `~/github/JFastFramework` |
| Docs | GitHub Pages, built by `docs-site/build.py` (25 pages) |

**Version lives in exactly one place:** `src/jfastframework/__init__.py`.
`pyproject.toml` declares it `dynamic`, and `framework_pin()` in
`cli/scaffold.py` derives what generated projects pin. Never write a version
into a template — that was the `~=0.7` bug that shipped an unsatisfiable
requirements file into every generated project.

### Publishing, and the two traps already hit

1. **A PyPI version number is burned on first upload.** Deleting the release
   does not free it. `0.1.0a1` can never be re-uploaded. `release.yml` now has a
   preflight step that asks the index before building, so a reused version fails
   in two seconds instead of after the container pull.
2. **The trusted publisher is registered per-site.** pypi.org and test.pypi.org
   are separate registrations; registering on one and publishing to the other
   gives `invalid-publisher`, which reads like a permissions bug and is not.

TestPyPI publishes with `skip-existing: true`, PyPI does not — deliberately. A
release that silently no-ops is worse than one that fails loudly.

To release: bump `__version__`, cut a `CHANGELOG.md` section, push, then
**Actions → Release → Run workflow → target `pypi`** (the default is testpypi).

---

## Environment traps

These cost real time. They are properties of this machine, not of the code.

- **Never run `git` from Windows against the WSL repo.** `core.autocrlf=true`
  rewrites `scripts/*.sh` to CRLF and they stop executing. Use
  `wsl.exe -d Ubuntu-26.04 -- bash -lc 'cd ~/github/JFastFramework && git ...'`.
  A `.gitattributes` with `* text=auto eol=lf` is committed as a second line of
  defence. Commits are made through GitHub Desktop on Windows; there is no git
  identity configured inside WSL.
- **Do not pass shell scripts through `bash -lc '...'` when they contain single
  quotes.** The quote closes the wrapper and the shell executes the `$(...)`
  inside. This silently wrote a broken `release.yml` once. Write the file with
  an editor tool, then run it.
- **`# nosec B608 - reason` breaks bandit** — everything after `nosec` is parsed
  as test IDs. Use a bare `# nosec B608` and put the reason on its own line.
- **Local Python is 3.14; CI runs 3.11–3.13.** The matrix cannot be reproduced
  here. Parsing every file with `ast.parse(..., feature_version=(3, 11))` catches
  syntax that is too new; it does not catch runtime differences.
- **Windows console encoding is cp1252 or cp850, not UTF-8.** `▟█████▙` and `✓`
  raise `UnicodeEncodeError` on cp1252; `›` also fails on cp850. Any glyph in
  CLI output needs an ASCII fallback.
- Port 8001 on Windows is owned by WSL's `wslrelay` while Docker publishes on
  `::` — `localhost:8001` reaches WSL and dies mid-handshake. Generated
  workspaces use the 9400 block.

---

## Verification that actually runs

```bash
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests     # skipping this shipped a red CI once
.venv/bin/mypy src
.venv/bin/python -m pytest -q               # 461 tests
.venv/bin/bandit -c pyproject.toml -r src -ll
.venv/bin/pip-audit --skip-editable
python docs-site/build.py --version ci --output /tmp/site && python docs-site/check.py /tmp/site
```

Plus 10 smoke scripts in `scripts/`, including `smoke_docker.sh`, which builds
the generated image and runs it against real PostgreSQL. **Run all of them, not
a subset** — a partial run is how a formatting failure reached CI.

---

## Decisions worth not re-litigating

- **Numbering restarted at `0.1.0a1`.** The old `0.1.0`–`0.7.0` overstated
  maturity and were never published. Pre-releases pin exactly (`==`), because
  `~=0.1` does not match `0.1.0a1` under PEP 440.
- **Bulk PDF merging uses `pypdf` + `img2pdf`, not WeasyPrint.** WeasyPrint
  answers "render a PDF from a template", which is a different question from
  "merge thousands of existing PDFs without holding them all in memory".
- **No arrow-key menus in the CLI.** Raw keyboard handling means a dependency in
  every install so one command looks nicer. Numbered choices work over ssh, in
  CI logs, and with no colour.
- **`required=` on a channel is key names, not a pydantic model** — Laravel
  publishes to some of these channels, so the enforceable contract on both sides
  is "these keys are present".
- **`shared/` may not import a module, and modules may not import each other.**
  Both directions are checked; without the second rule `shared/` becomes the
  place everything ends up.

---

## Open

- `0.1.0a2` is built and verified but **not yet published**.
- Redis queue visibility timeout is implemented but not run against a real
  server in CI. Same for the RabbitMQ and Kafka backends, and SMTP.
- The terminal experience is being reworked toward the RED.CORE palette
  (`#dc2626` crimson on `#09090b`), with an encoding-safe glyph layer.
