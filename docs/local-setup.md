# Trying it locally

JFastFramework is not on PyPI yet. You install it from the checkout, and then
`jfast` works from any directory.

---

## Once, to install it

```bash
cd ~/github/JFastFramework
python3 -m venv .venv
./.venv/bin/pip install -e ".[all,dev]"
```

`-e` is an editable install: the `jfast` command runs the code in this
checkout, so editing the framework takes effect immediately, with no
reinstall.

Put it on your PATH for the session:

```bash
export PATH="$HOME/github/JFastFramework/.venv/bin:$PATH"
jfast version
```

Or make it permanent:

```bash
echo 'export PATH="$HOME/github/JFastFramework/.venv/bin:$PATH"' >> ~/.bashrc
```

### If `python3 -m venv` fails

Ubuntu splits `venv` out of the base Python. Either install it:

```bash
sudo apt install python3-venv
```

or bootstrap pip into the venv without it:

```bash
python3 -m venv --without-pip .venv
curl -sS https://bootstrap.pypa.io/get-pip.py | ./.venv/bin/python
```

---

## A new project, the fast way

```bash
mkdir ~/projects/shop && cd ~/projects/shop
jfast start shop
```

That is the whole thing. You get:

```
shop/                  FastAPI + PostgreSQL/pgvector + Redis + background jobs
  modules/item/        a starter module whose tests already pass
  contracts.toml       the rules this service holds itself to
  alembic.ini          migrations, reading the app's own DSN
shop-web/              Vue 3 + Vite + Tailwind, pointed at the backend
docker-compose.yml     derived from the enabled plugins
Caddyfile              one hostname in front of both
jfast.workspace.toml   ports, and what the frontend should call
```

### Run the backend

```bash
cd shop
pip install -r requirements.txt
cp .env.example .env          # fill in POSTGRES_PASSWORD
pytest                        # the starter module's tests
uvicorn main:app --reload --port 8000
```

```bash
curl localhost:8000/health
curl localhost:8000/docs      # OpenAPI UI
```

Endpoints that need the database only work once one is running:

```bash
docker compose up -d shop-database
alembic revision --autogenerate -m "initial"
alembic upgrade head
```

### Run the frontend

```bash
cd ../shop-web
npm install
npm run dev                   # http://localhost:8010
```

The home page calls the backend's `/health` on load, so if the wiring is wrong
you see it immediately rather than on your first real feature.

### Or all of it in Docker

```bash
cd ~/projects/shop
docker compose up --build
```

---

## A new project, choosing as you go

```bash
jfast init
```

Asks what you are building, which datastores you want and on which port, then
generates exactly what the flags would have:

```bash
jfast new service billing --with database,cache,queue
jfast new service edge --language go
jfast new service admin --kind spa --frontend vue
```

---

## The loop you will actually use

```bash
jfast new module invoice          # a domain module with tests and a README
pytest modules/invoice/tests
jfast contracts check             # layer boundaries, forbidden calls
alembic revision --autogenerate -m "add invoices"
```

Then mount the router in `main.py` — the generator prints the two lines.

---

## Working on the framework itself

```bash
cd ~/github/JFastFramework
pytest                                  # unit tests, ~1s
ruff check src tests docs-site
mypy src

bash scripts/smoke.sh                   # both module layouts, HTMX, alembic
bash scripts/smoke_contracts.sh         # contracts catch what they should
bash scripts/smoke_workspace.sh         # workspace, gateway, view patching
bash scripts/smoke_start.sh             # the default stack end to end
bash scripts/smoke_go.sh                # needs go on PATH
bash scripts/smoke_frontend.sh          # needs npm on PATH
```

The smoke scripts each generate a project in a temp directory, run it, and
delete it. They are the only checks that catch a template which renders
cleanly and produces code that does not work — `pytest` alone cannot.

### Optional toolchains

The Go and frontend suites skip themselves when their toolchain is missing.
To run them locally without touching your system:

```bash
mkdir -p ~/.jfast-toolchains && cd ~/.jfast-toolchains
curl -sSL https://go.dev/dl/go1.23.4.linux-amd64.tar.gz | tar xz
curl -sSL https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz | tar xJ
mv node-v22.12.0-linux-x64 node
export PATH="$HOME/.jfast-toolchains/go/bin:$HOME/.jfast-toolchains/node/bin:$PATH"
```

Delete the directory to undo it. Nothing is installed system-wide.

---

## Common problems

**`jfast: command not found`** — the venv is not on PATH. Either export it as
above, or call it directly: `~/github/JFastFramework/.venv/bin/jfast`.

**`No jfast.toml found`** — `jfast describe`, `doctor` and `deploy` run inside
a service directory. `jfast start`, `init` and `workspace` run above it.

**`Port 8011 is already taken by 'billing'`** — the workspace allocates
ten-port blocks. Let it pick (`jfast new service …` with no `--port`) rather
than choosing by hand.

**Alembic can't reach the database** — `migrations/env.py` reads
`JFAST_DB_DSN` from `.env`, deliberately the same value the app uses. Start
the container first: `docker compose up -d <service>-database`.

**Windows** — run all of this inside WSL. The generated Dockerfiles, the
compose files and the shell scripts assume a POSIX shell.
