# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/) with pre-release identifiers per
[PEP 440](https://peps.python.org/pep-0440/). While the API is pre-alpha, services
pin exactly (`jfastframework==0.1.0a3`); a compatible-release pin (`~=`) starts
making sense at 0.2.

## Renumbering

The entries below were originally numbered `0.1.0` through `0.7.0`. That numbering
overstated the maturity of the code. Nothing has ever been published; the workspace
file format is about to change; the Redis queue backend does not implement the
visibility timeout its own contract documents; the RabbitMQ and Kafka backends have
never been run against a real broker.

The package therefore restarts at `0.1.0a1`. The history is kept verbatim as a
development log — it records what was built and when — but those numbers were never
releases and were never installable. `pip install jfastframework` will not resolve a
pre-release without `--pre`, so the packaging tool enforces the warning rather than a
sentence in a README.

Subsystem-level maturity lives in [STATUS.md](STATUS.md), which is the file to read
before depending on any single part of this.

## [Unreleased]

## [0.1.0a3] - 2026-08-29

### Documentation

- **A landing page separate from the wiki.** The front page rendered through the
  same chrome as every documentation page -- a sidebar of nineteen links, a
  version picker, a pager -- so arriving at the project meant arriving already
  inside the manual. `index.html` is now its own page and `docs.html` is the
  documentation home.
- **The README says what this is for.** It opened with a category ("a
  plugin-based FastAPI framework") and went straight into a fifteen-section
  feature tour, which is written for somebody who has already decided. It now
  leads with who it is for, **who it is not for**, the shape of what gets
  generated, and four concrete situations it was built for. The reference half
  is unchanged; the problem was never that it existed.
- **A Spanish edition.** Every page has a Spanish URL under `/es/`, and 24 of
  26 are really translated -- the rest render the English source under a notice
  saying so, rather than serving English silently or leaving dead links in a
  translated sidebar. A translation lives at `docs/es/<name>.md` and overrides
  the English one; adding a page is dropping a file there.
- **`jfast dev` and the agent surface are documented** (`docs/dev.md`,
  `docs/agents.md`), along with the four module layouts, the base components,
  the stores, and `python -m jfastframework`. Ten things shipped undocumented;
  a check over the documentation now reports none.
- **A light/dark switch**, replacing "whatever the operating system says". Three
  states rather than two: an explicit choice, or follow the system.
- **The sidebar distinguishes labels from links.** Group headings and entries
  were both muted grey in one column, so a heading read as a smaller link. The
  headings are now accent-coloured and monospaced with a rule after them, and
  every entry carries an icon.
- **The language a reader picks is remembered**, and acted on **only at the site
  root**. Redirecting deep links would mean a shared URL lands somebody
  somewhere they did not click, and a crawler bounced off every page it
  requests indexes nothing.

### Fixed

- **`jfast start` wrote a compose file that could not start.** Its own
  next-steps panel said to run `docker compose up --build`, and that command
  failed: no workspace `.env`, so compose refused to interpolate
  `${SHOP_DATABASE_PASSWORD}` rather than defaulting it, and no per-service
  `.env`, which the compose file lists as an `env_file` and treats as an error
  when missing. Both are now written alongside the compose file, so the two
  agree by construction instead of by instruction.
- **The HTMX form never submitted.** `--ui htmx` mounted its HTML router on the
  same prefix as the JSON one, both declaring the same verbs, so whichever
  registered first answered: browsing returned JSON and the form POSTed into
  the API handler. The HTML surface now lives under `/ui/<table>`. It also
  passed a plain dict to a service that reads `payload.name`, and extended a
  `base.html` that only ships with `--kind web`. Three separate faults on one
  path, none of which generation, import, mounting or `contracts check` could
  see -- only sending the form finds them, which `scripts/smoke_htmx.sh` now
  does on all four layouts.
- **A validation error whose input was bytes returned 500, not 422.** pydantic
  puts the offending value in the error's `input` field, and a form posted
  without a content type puts the raw body there -- which the JSON encoder
  cannot represent, so serialising the 422 raised inside the handler. The
  caller got a stack trace about `json.dumps` instead of the field name, and a
  status that invites a retry of a request that can never succeed.

### Added

- **Four module layouts, and a prompt that asks which.** `layered`, `modular`,
  `screaming` and `hexagonal`. A modular monolith should not force a catalogue
  and an orders module to the same shape; the point of the boundary is that
  each side can differ. `jfast new module` asks when `--layout` is omitted, and
  falls back to `layered` without asking when there is no terminal, so a
  script or a CI job does not hang on a prompt nobody can see.
- **`jfast.toml` remembers which layout each module used.** Asking again each
  time eventually gets a different answer, and guessing from the folders on
  disk breaks the moment somebody adds one. Written where the rest of the
  service's configuration lives, and ignored by the runtime.
- **A contract per layout.** `contracts_modular` and `contracts_hexagonal`
  match the folders their layouts actually create; without them
  `jfast contracts init --layout hexagonal` pointed at a template that did not
  exist. The hexagonal one is the interesting one: `domain/` may import
  nothing -- not the ORM, not FastAPI -- because a domain that imports
  SQLAlchemy has already stopped paying for the layout.
- **Modules register themselves in `main.py`.** The frontend has patched its
  own routes and menu since the beginning; the backend printed two lines and
  left them to be pasted, so a generated module was inert until somebody did.
  A module that is not mounted looks exactly like a module that does not work.
  Non-fatal by design: a hand-edited `main.py` that lost its markers gets the
  lines to paste rather than an unwound scaffold.
- **`jfast dev`.** Containers up and waited for, migrations applied, then the
  API and the frontend together. Every stage degrades and says so; the one hard
  stop is a failing migration, because a server on a stale schema fails later
  in a request that has nothing to do with the missing column. It also
  translates the generated `.env` for a host process -- container hostnames
  become `localhost:<published port>` and `${...}` is interpolated -- since
  neither is true outside the compose network. Ctrl-C and SIGTERM both take the
  children with them, which needed its own handler: the default SIGTERM
  disposition kills the interpreter outright, and the children, being in their
  own process groups, would have survived holding the ports.
- **`python -m jfastframework`.** For when the console script is not reachable:
  a Windows install where `Scripts/` is not on PATH, a virtualenv nobody
  activated. The only module path that worked before printed a `RuntimeWarning`
  on every invocation.
- **Base components and toasts, in both frontends.** `BaseButton`, `BaseInput`,
  `BaseModal`, `BaseBadge`, `SkeletonLoader`, `EmptyState` and a toast host,
  mirrored between Vue and React so the two are the same product. Loading is a
  skeleton shaped like what is coming rather than the word "Loading", and empty
  says what would be there and offers the action that creates the first one.
- **Pinia and Zustand stores, wired up.** Pinia was a dependency no generated
  file imported; React had no state library at all. Auth and notifications now
  ship as stores, so a toast raised inside a service and one raised in a
  component land in the same list.
- **The spinner has callers.** `ui.working()` was written, ASCII-safe, and
  invoked from nowhere -- the function existed, the feature did not.

### Changed

- **The brand colour is crimson**, replacing the placeholder blue, matching the
  documentation site and the terminal.

<!-- earlier in this cycle -->


### Fixed

- **`jfast init` and `jfast start` crashed on a Windows console.** Not a broken
  character -- a `UnicodeEncodeError` raised by `sys.stdout` partway through
  writing a project, so the command died with a traceback having already
  created half of it. cp1252 has no `U+2713`; cp850 has neither that nor
  `U+203A`; the banner's block characters are in neither. Every symbol now
  resolves through `cli/glyphs.py` against the encoding the console actually
  reports, and falls back to ASCII -- panels, tree guides and spinner included.
  The check is a real `str.encode` rather than a list of known-good codepages,
  because terminals lie about themselves and `PYTHONIOENCODING` overrides all
  of it.
- **Three generated stylesheets pointed at a file that was never generated.**
  `frontend_vue`, `frontend_react` and `service_web` all told the reader to
  consult `.jfast/skills/design-system/SKILL.md`, and no generated project
  contained it. A pointer to nothing is worse than no pointer: it costs a
  reader the trip, and it teaches an agent that this project's instructions are
  unreliable. The reference is now conditional on the skill being written, and
  a test walks every generated file to keep the two in step.

### Added

- **A tree instead of a wall.** A scaffold prints forty-odd paths, and in a
  flat column the one line worth reading -- a file left alone because it
  already existed -- looks exactly like the thirty-nine that were written.
  Every generating command funnels through one reporter, so the shape of the
  output is decided once rather than per command.
- **The agent surface, opt-in: `--agent-docs`, or a question in `jfast init`.**
  An `AGENTS.md` and a skill under `.jfast/skills/`, reusing the layout the
  framework repo already uses rather than inventing a second place to look for
  conventions. A frontend also gets the design skill, which is what makes the
  stylesheet reference above true. Off by default, because a project nobody
  points an agent at owes no agent files -- and every file shipped is a file
  that can drift.


## [0.1.0a2] - 2026-08-28

### Added

- **A `shared/` layer, and a check that says when to use it.** Two modules
  that import each other are one module with a folder between them: neither
  can be extracted into a service later, and a change to one breaks the other
  in a way no test covers. `jfast contracts check` now reports the
  cross-import **and names the file to move the code to**, so the fix does
  not need a design discussion. The direction is enforced both ways: modules
  import `shared/`, `shared/` imports no module -- without that second rule
  `shared/` becomes the place everything ends up, which is the failure mode
  of every `utils` package ever written.
- **`jfast new enum`**, which asks where it goes when you do not say.
  The placement is the decision; the file is not. Start one in the module
  that needs it and the check tells you the day a second module wants it, so
  nobody has to predict it. Generated modules and `shared/` both ship an
  `enums.py` using `str, Enum`, because a plain Enum serialises as
  `Status.DRAFT` down some paths and `"DRAFT"` down others.
- **Declared channels (`channels` plugin).** Replaces a file of string
  constants, which fails in three ways: nothing checks the payload, the
  transport is welded to the call site, and nobody can list the channels a
  system uses. A `Channel` validates its payload **where the message is
  built** rather than in a worker three services away, and carries its own
  backend -- memory by default and needing no infrastructure, redis for a
  channel something in another language also speaks, kafka when a consumer
  that was down has to catch up. Mixing them is the normal case.

- **`jfast serve`.** Runs a service locally, and refuses to start when there
  is no `jfast.toml` in the directory -- which is the case that used to boot
  silently with framework defaults, no database, and no complaint. Binds
  loopback rather than `0.0.0.0`, because a development server should not be
  on the network unless you say so.
- **`mail` plugin.** Templates, three backends, and **queued by default**: a
  slow or briefly refusing mail server should not become the latency or the
  error of the request that triggered it. `send_now()` is the synchronous
  escape hatch and reads like one. The default backend is `console`, so
  nobody emails a customer from a laptop by accident and no credentials are
  needed to develop. In production with the smtp backend it refuses to start
  without credentials rather than failing on the first send.
- **`jfast add` and a capability catalogue.** Excel and PDF assembly, HTML to
  PDF, large XML, dataframes, vision, validation, locale, retries. Nothing is
  installed by default: a service that serves JSON should not carry numpy, and
  the plugin graph stops describing the service the moment it does. In a
  workspace with several backends it asks which one, because adding a heavy
  dependency to the wrong service is invisible until the image is built.
- **`jfastframework.exports.pdf`**, for assembling many documents. `merge()`
  **reports what it could not include** -- the obvious implementation logs a
  warning and returns a bundle that looks complete, which for a fiscal or
  legal bundle is worse than an error. It also merges in batches, because
  `PdfWriter.append()` holds every page until `write()` and peak memory
  otherwise grows with the whole job.
- **`jfastframework.exports.excel`**, using openpyxl's write-only mode so a
  cursor can be streamed to a file without either being held whole.
- **The installer is rendered with `rich`** -- which arrives with Typer, so no
  new dependency. A banner, tabulated choices, a summary before anything is
  written, and the next steps with what each command does beside it.

### Fixed

Six defects that shipped in `0.1.0a1`. Together they meant a generated
service could not be installed, could not be built into an image, could not
answer a GET, and could not answer a PATCH. Each is now covered by a test,
and by `scripts/smoke_docker.sh`, which builds the generated image and runs
it against a real PostgreSQL -- the check whose absence let all six through.

- **Every route taking a database session answered 422.**
  `session_dependency(request: Any)`: FastAPI decides what a dependency
  parameter *is* from its annotation, and from `Any` it concluded the only
  thing left -- a required query parameter. Reproduced from the OpenAPI
  schema (`name='request' in='query' required=True`), not inferred. Now
  annotated `Request`.
- **Every update returned 500 once a timestamp was serialised.**
  `TimestampMixin.updated_at` carries `onupdate`, which SQLAlchemy expires at
  flush; the next read -- Pydantic building the response -- attempted IO in a
  coroutine and raised `MissingGreenlet`. The mixin now asks for
  `eager_defaults`, so PostgreSQL returns the value with `RETURNING` in the
  same statement. A `session.refresh()` would have worked too, at the cost of
  a SELECT on every write, including the writes that never read a timestamp.
- **The generated Dockerfile could not build.** `COPY pyproject.toml ./`
  named a file the generator never writes, and COPY fails when its source is
  absent. Globbed, like the `requirements.txt*` line directly below it always
  was.
- **A container against an empty database answered 500 to everything.**
  Nothing ran migrations. The image now has an entrypoint that runs
  `alembic upgrade head` and then `exec`s uvicorn: `set -e` stops the
  container on a failed migration instead of serving a half-migrated schema,
  and `exec` keeps uvicorn as PID 1 so it receives SIGTERM. `create_all` was
  rejected as the fix -- it builds a schema Alembic does not know about, and
  the first real migration then diverges in silence.
- **The workspace search walked to the root of the filesystem.** Running
  `jfast start` once in a home directory left a workspace file there, and
  every project underneath then joined it: one compose file, one port space,
  unrelated services registering against each other, and nothing failing. The
  search now stops at the home directory and at a `.git`, because a
  repository root is where a project ends.
- **A service started from the wrong directory booted misconfigured in
  silence.** `session_dependency` reached into `request.app.state.jfast`
  directly and raised `KeyError: 'jfast'` on any app this framework did not
  build. It now goes through `get_context()`, which says so.

### Added

- **`scripts/smoke_docker.sh`**, gated in CI. It builds the image the
  generator writes, runs it against a real PostgreSQL, and asserts three
  things: a failed migration stops the container with the database's own
  error, a successful one leaves an `alembic_version` table behind, and
  `/ready` reports the database healthy from inside the container.

- **Every generated service shipped a `requirements.txt` pip could not
  satisfy.** The template carried a literal `jfastframework[...]~=0.7`, which
  survived the renumbering to `0.1.0a1`, so `pip install -r requirements.txt`
  in a scaffolded project failed with *No matching distribution found*. The
  pin is now derived from the framework's own version by `framework_pin()`.

  A pre-release is pinned **exactly**, because `~=0.1` does not match
  `0.1.0a1` either: a compatible-release clause normalises to
  `>= 0.1, == 0.*` and `0.1.0a1` sorts below `0.1.0`, so it is out of range
  even with `--pre`. Once the framework reaches a final release the pin
  becomes `~=major.minor` on its own.

  A test now fails if any requirements template hardcodes a version again.
  The resolution itself is deliberately not checked in CI: at release time
  the version being pinned is not published yet, so that check would fail on
  exactly the commit that is correct.

### Added

- **The documentation site has the project's own identity.** A monogram
  (`mark.svg`) and favicon in black and crimson, replacing the letters-in-a-box
  placeholder and the emoji favicon. `docs-site/assets/BRAND.md` says where the
  owl goes; the site falls back to the monogram when it is absent, so a missing
  binary cannot break the build.
- **The sidebar is grouped** into Start here, Build, Run, Guard and Project.
  Twenty-two flat links is a list nobody scans.
- **An "on this page" index** on any page with three or more sections,
  previous/next links in reading order, and a copy button on every code block.
- **A Maturity page**, rendering `STATUS.md`. It is the most useful page on the
  site for anyone deciding whether to depend on a part of this.
- **`docs/local-setup.md` opens with one copy-paste block** that goes from
  nothing to a running stack. It is executed end to end before shipping, not
  written from memory.

- **The resource graph.** Datastores are named instances the workspace owns
  (`[[workspace.resources]]`), and a service binds to one under a variable
  (`uses = [{ resource = "core-db", as = "JFAST_DB_DSN" }]`). Two databases
  of the same type, and one cache shared by two services, are both now
  expressible; neither was before.
- **The DSN is generated.** `jfast workspace env` writes each service's `.env`
  from its bindings. The compose file used to emit a datastore container and
  leave the connection string to a human, which is where drift came from.
- **One password per resource**, generated into a gitignored workspace `.env`
  and never overwritten once set. It replaces the single workspace-wide
  `POSTGRES_PASSWORD`, where a leak anywhere was a leak everywhere.
- `jfast workspace resource`, `jfast link`, `jfast unlink`,
  `jfast workspace validate`, `jfast workspace migrate-resources` and
  `jfast workspace graph` (mermaid or dot, edges labelled with the variable).
- Binding two resources to one variable is refused, and `validate` reports a
  port claimed twice, a binding to a resource that does not exist, and a
  resource nobody uses.

### Fixed

- **The site's hero advertised `pip install jfastframework`,** which does not
  resolve because nothing has been published. It now shows the clone-and-install
  that works today, and says why it is not on PyPI yet.
- A mangled em dash in the hero copy, which had been rendering as `â` since the
  page was written.

### Fixed

- **Every smoke script reported success when it failed.** `trap 'rm -rf
  "${WORK}"' EXIT` ends with a successful `rm`, and bash hands the trap's
  status to the script -- so a failed assertion exited 0 and CI went green.
  All nine now preserve the failing code.

- **The Redis queue had no visibility timeout.** `visibility_timeout` was
  stored and never read, and recovery only drained the worker's own
  processing list -- under a key that included `id(self)`, a memory address.
  A worker that died came back under a different name and never recovered
  its own in-flight jobs, so the guarantee `queues.base` documents for every
  backend did not exist here. Workers now register in a hash with a
  heartbeat on server time, and any worker returns the jobs of a consumer
  whose heartbeat has gone stale. `close()` hands work back immediately, so
  a rolling deploy does not park jobs until the timeout expires.
- **`/ready` ran its checks serially and without a timeout.** A dependency
  hanging at the TCP level held the probe open until the socket gave up.
  Checks now run concurrently under `readiness_timeout` (default 2s), and a
  timeout is reported as `timeout` rather than `fail` -- one means the
  dependency said no, the other means it never answered.
- **`BaseRepository.paginate()` emitted no `ORDER BY`.** Pages were not
  stable: a row could appear twice while another was never returned.
  Ordering defaults to the primary key and is overridable per repository.
- **The tenant filter failed open.** A repository given a `tenant_id` for a
  model with no such column silently returned every tenant's rows. It now
  raises at construction; a genuinely global model declares
  `tenant_scoped = False`.

### Added

- **Edge protections in the kernel**, all off unless configured: CORS,
  `TrustedHostMiddleware`, a request body size limit answering 413, and a
  request timeout answering 504. Caddy covers these when it is in front;
  `jfast deploy function` puts a service on Lambda with nothing in front.
  Wildcard CORS origins combined with credentials is refused at boot,
  because browsers reject that pair and it would otherwise fail silently.
- `safe_identifier()` validates any table name interpolated into SQL, at the
  point it enters, so the interpolation that follows is provably safe.
- `pip-audit` and `bandit` run in CI as hard gates. Every existing finding is
  waived explicitly with its reason, or fixed.
- Tests for the Redis queue against an in-memory double of the commands it
  issues, and for the repository against SQLite. 380 tests total.

### Changed

- `/docs` and `/openapi.json` are closed when `env = prod` unless set
  explicitly. `/info` already did this; the three are now one rule.
- The PostgreSQL claim query moved to a named `CLAIM_SQL` constant, built
  once per call rather than assembled inline.
- `RabbitMQQueue` no longer takes `visibility_timeout`. The broker redelivers
  unacknowledged messages when a channel closes, so the parameter never did
  anything, and one that does nothing is a promise the caller believes.


### Added

- **`async-blocking` contract rule.** `jfast contracts check` now reports
  calls that stall the event loop from inside `async def`: the standard-library
  cases, the synchronous clients this framework ships with (boto3, pymongo,
  psycopg2, sync redis, sqlite3), a blocking client stored on `self`, and one
  hop into a synchronous helper defined in the same file. Correct offloading
  through `asyncio.to_thread` and friends is recognised and left alone.
  Configurable under `[rules.async_safety]`; waivable inline.

### Changed

- Ruff's `ASYNC` ruleset is enabled for the framework. `ASYNC109` is ignored
  with a reason: it wants a cancel scope instead of a `timeout` parameter, and
  `dequeue(timeout=...)` maps onto a broker primitive.

### Fixed

- `web` plugin: the readiness probe ran two blocking `Path.is_dir()` calls on
  the event loop, once per probe per replica. Now offloaded.


## [0.7.0] - 2026-08-28

Files, tenants, and the three cloud services a deployed app reaches for.

### Added

**`storage` plugin**
- Named disks with a visibility, modelled on Laravel's: code writes to
  `storage.disk("private")` and where that lives is configuration.
- Local and S3/MinIO drivers behind one `StorageBackend` protocol. A local disk
  writes atomically (temp file + `replace`) so a reader never sees a partial
  object.
- A private disk **refuses** to produce a permanent URL. `temporary_url()` signs
  the key *and* the expiry with HMAC, compared in constant time — signing only
  one of the two makes a single valid link a key to the whole disk.
- Every key is validated before it reaches a filesystem or a bucket: traversal,
  absolute paths, backslashes and null bytes rejected, `..` resolved first.
  Local disks re-check after resolution, because a symlink inside the root can
  still point outside it.
- Downloads are `Content-Disposition: attachment` + `nosniff`. An uploaded
  `.html` or `.svg` served inline runs the uploader's script on your origin.
- Expired and forged links return the same 403 with the same message.
- MinIO in the generated compose file at port offset `+6`, opt-in.

**`tenancy` plugin**
- Resolves the tenant from a token claim, a subdomain, a path prefix or a
  header, in that **order of trust**. `header` is not in the default list and
  warns in production: `X-Tenant-ID: acme` is one curl away from another
  tenant's data.
- Subdomain parsing rejects multi-label hosts, the bare base domain, and a
  reserved list (`www`, `api`, `admin`, …). `base_domain` is required, or every
  hostname looks like a tenant.
- `require_tenant` returns problem+json 403, with health, metrics and docs
  exempt so probes still pass.
- `jfast workspace caddy --wildcard-tenants` emits a wildcard site block with
  on-demand TLS **and** the `ask` endpoint that gates it. Without `ask`, anyone
  pointing DNS at you can burn your certificate rate limit.

**Social login (`auth`)**
- Google, Microsoft and GitHub presets; any other provider by its endpoints.
- `/auth/{provider}/start` and `/auth/{provider}/callback`, with the state and
  nonce carried in an httponly, samesite=lax cookie and both verified on the
  way back.
- ID tokens verified for audience and issuer. Without the audience check, a
  token minted for anyone else's Google app logs in here.
- `@auth.on_identity` is where a verified identity becomes your user. Missing
  it is a 500, not a cheerful 200.
- `OIDCIdentity.federated_id` is provider-qualified, because subject ids are
  unique per provider and not globally.

**Secrets**
- `load_secrets()` populates `os.environ` from AWS Secrets Manager or Google
  Secret Manager before `create_app()`. An existing environment value wins
  unless overridden; only names are logged, never values; nested JSON is
  refused rather than given an unpredictable flattened name.

**Serverless**
- `jfast deploy function <name> --target aws|gcp` writes the handler, the
  Dockerfile and a deploy script — and does not run them.
- Both targets run the same ASGI app the container runs. Private by default on
  both clouds; `--public` opts in and warns.

**`notifications` plugin**
- Firebase Cloud Messaging over the HTTP v1 API, with a `console` backend that
  logs instead of sending for development and tests.
- Unregistered device tokens are reported back so they can be deleted.
- Not verified against a real FCM project in CI; the payload construction is.

### Fixed

- **The `observability` plugin no longer overwrites a resolved tenant.** It
  trusted `X-Tenant-ID` unconditionally and clobbered `request.state.tenant_id`
  on the way past, so a tenant resolved from a signed claim was replaced by
  `None` before the handler ran. It now fills the gap only when nothing else
  resolved one.
- **`tenancy` runs innermost.** `add_middleware` puts middleware *outermost*,
  which ran tenancy before auth and left the signed `token` source permanently
  unreadable. It is appended instead.
- **`secrets.parse` refuses a JSON array** instead of falling through to the
  `KEY=value` parser and silently loading nothing.

### Added (internal)

- `errors.problem_response()` for middleware, which runs outside FastAPI's
  exception handlers and would otherwise surface a 500 with a stack trace.

## [0.6.0] - 2026-08-27

JWT authentication, and Kubernetes manifests derived from the service contract.

### Added

**`auth` plugin**
- Verification in three modes: `jwks` (fetch the issuer's public keys — the
  default, and the only sane one across services), `public_key` (a pinned PEM),
  `secret` (HMAC, for a single service).
- `require_auth`, `require_scopes(...)`, `require_roles(...)`, `optional_auth`
  as FastAPI dependencies. 401 for "who are you", 403 for "you may not".
- JWKS client with caching, rotation on an unknown `kid`, and a rate limit on
  refresh so forged `kid`s cannot be used to hammer the identity provider.
  Cached keys keep working through a JWKS outage; `/ready` reports staleness.
- Token issuance for a service that owns its own login, with **refresh
  rotation and reuse detection**: a replayed refresh token revokes the whole
  session family.
- Revocation: `POST /auth/logout` denies the `jti` and its refresh family,
  backed by Redis when the `cache` plugin is on. The in-memory fallback
  reports itself as not shared rather than pretending.
- `GET /auth/me` returns identity and permissions — never the token, never the
  raw claims.

**Security decisions, each with a test**
- Algorithms are pinned by configuration and passed explicitly to the decoder,
  so `alg: none` and RS256→HS256 confusion are both refused. Configuring
  symmetric and asymmetric algorithms together is rejected at startup: that
  combination *is* the attack.
- `aud` and `iss` are verified — off by default in most libraries, and without
  them a token for a sibling service is accepted here.
- Expiry leeway is 30 seconds, not minutes.
- Rejection reasons go to the log; the client gets a plain 401.
- **`tenant_id` now comes from a signed claim**, not the forgeable
  `X-Tenant-ID` header. This is the main security reason to enable auth.

**Kubernetes**
- `jfast workspace k8s` — a kustomize tree: Deployment, Service, ConfigMap,
  HPA and PodDisruptionBudget per service, one Ingress, `dev`/`prod` overlays.
- `jfast init` asks whether you need it.
- Liveness probes `/health`, readiness probes `/ready` — the two-endpoint
  contract is what keeps a database blip from restarting every healthy pod.
  A startup probe allows 150s for a slow first boot.
- Non-root, read-only root filesystem, dropped capabilities,
  `maxUnavailable: 0`, and a PDB so a node drain cannot take every replica.
- Ingress serves `/api`, the same shape as the generated Caddyfile, so the
  frontend build is identical locally and in the cluster.

### Not generated, deliberately
- **Databases.** A StatefulSet for PostgreSQL from a scaffolder is how people
  lose data. The manifests read a DSN from a Secret.
- **Real secrets.** `*-secrets.example.yaml` holds placeholders.
- **A login endpoint.** Checking a password against your user table is the
  application's job; `auth.issuer` is provided for your own route.
- **NetworkPolicies, ServiceMonitors, migration Jobs, Helm.** Each needs a
  decision about your system that a generator should not guess.

### Notes
- The manifests are validated as YAML and asserted structurally in
  `tests/test_kubernetes.py`. They have **not** been applied to a real
  cluster in CI. Treat the first `kubectl apply` as the test.

## [0.5.0] - 2026-08-27

Per-project contracts, and a quickstart that CI actually executes.

### Added

**Contracts**
- `contracts.toml` in every generated service: scope (`owns` /
  `does_not_own`), layer boundaries, forbidden calls, required structure,
  declared interfaces, and invariants no checker can verify.
- `jfast contracts init | check | show --json | render | waivers`.
  `check` exits non-zero, so it fails a build rather than printing advice.
- A static, AST-based checker: layer boundaries (relative and absolute
  imports), per-layer forbidden packages, forbidden calls with the reason
  attached, and required files per module.
- Inline waivers — `# contracts: allow <reason>` — with the reason required
  and `jfast contracts waivers` listing every one.
- The contract is validated before the code: two layers claiming one path, or
  a `may_import` naming a layer that does not exist, are reported as contract
  errors rather than producing confident answers to the wrong question.
- `CONTRACTS.md` generated from the same file, so the document and the
  enforced rule cannot disagree.
- `.jfast/skills/respect-contracts/SKILL.md`, and `AGENTS.md` now opens with
  `jfast contracts show --json`.

**Getting started**
- `docs/local-setup.md` — installing from a checkout, generating a project,
  the loop you actually use, and the failure modes worth knowing.
- `scripts/smoke_docs.sh` runs those commands **exactly as documented**, in
  CI. Documentation that has never been executed is a guess.

### Fixed
- **A generated service with `queue` enabled could not start without
  PostgreSQL.** `setup()` raised at startup, so the process crash-looped with
  an asyncpg traceback instead of serving. It now starts, logs the reason, and
  reports itself unready — an orchestrator handles "not ready" gracefully and
  handles a crash loop by paging someone. Same fix for `rag`'s `auto_migrate`.
- The layered contract defaults claimed `modules/*/schemas.py` for two layers.
  Caught by a freshly generated service failing its own contract, which is
  exactly the check that should catch it.
- Layer matching ranked patterns by string length, so the screaming layout's
  catch-all `modules/*/[!_]*.py` beat `modules/*/http.py` and classified every
  router as domain code. It now ranks by specificity — fewest wildcards.

## [0.4.0] - 2026-08-27

Polyglot services, queues and events, one-command start, Caddy at the edge,
and a documentation site — plus real build verification for everything that
had until now only been verified by grep.

### Added

**`jfast start`**
- One command for the opinionated default: a Python modular monolith with
  PostgreSQL + pgvector, Redis, background jobs and a starter module, a Vue
  frontend, a Caddyfile and a workspace compose file.
- A monolith rather than three services on purpose: splitting later is a move,
  un-splitting is a rewrite.

**Polyglot services**
- `docs/service-contract.md` — the contract every JFast service satisfies
  regardless of language: `/health`, `/ready`, `X-Request-ID`, problem+json,
  `JFAST_*` config, ten-port blocks, JSON logs on stdout.
- `jfast new service --language go` — a Go service with **zero third-party
  dependencies**, implementing the contract in ~300 vendored lines. CI runs
  `go vet`, `go test`, `go build`, starts the binary and curls it.
- `jfastframework/languages.py` — the language registry. You only need the
  toolchain for the languages you actually use.

**gRPC**
- `--grpc` generates the `.proto` contract (health, problem, a domain service)
  and reserves port offset +9. **Contract only:** no stubs are generated and no
  server is wired, because pinning a `protoc` version inside a scaffolder makes
  generated stubs disagree with whatever CI has. See `proto/README.md`.

**Queues**
- `queue` plugin with a `QueueBackend` protocol and three backends: PostgreSQL
  (`FOR UPDATE SKIP LOCKED`, transactional enqueue), Redis (`BLMOVE` into a
  per-worker processing list), RabbitMQ (dead-letter exchange with a TTL for
  delays).
- `TaskRegistry` and `Worker`: bounded exponential backoff, dead-lettering,
  job timeouts, in-flight draining on shutdown, immediate wake on stop.
- `GET /queue/stats`.

**Events**
- `events` plugin — Kafka publish/subscribe with partition keys, offsets
  committed after handling, and KRaft-mode infrastructure (no ZooKeeper).

**Edge and workspace deployment**
- `jfast workspace compose` — one compose file for every service, whatever its
  language, with per-service datastores.
- `jfast workspace caddy` — a Caddyfile putting the workspace behind one
  hostname. Backends live under `/api` with or without a gateway, so the
  frontend's production build keeps working the day one appears.
- Frontends gained `.env.production` with `VITE_API_URL=/api` — relative, so
  no CORS and no rebuild per environment.

**Documentation site**
- `docs-site/build.py` renders the repository's own markdown into a static,
  versioned site; `docs-site/check.py` validates links, anchors, assets, theme
  tokens and unrendered template artifacts.
- `.github/workflows/pages.yml` publishes it, rebuilding every released minor
  version from its own tag so older versions keep working.

### Fixed
- **The generated Vue and React routers were not valid JavaScript.** The marker
  comment `/*nuevaRuta*/` sat inside a `/* … */` block comment, whose inner
  `*/` closed the comment early. Every grep-based check passed — the marker
  *was* there — and only `vite build` caught it. Both frontends now install and
  build in CI.
- **The worker busy-waited on any non-blocking backend.** PostgreSQL polls and
  returns instantly when the queue is empty, so the loop never yielded: it
  burned a core and starved the event loop, which meant the HTTP handlers in
  the same process stopped responding while "the worker is running".
- `jfast start`'s frontend called a dev port that Caddy served under a
  different path. Both now agree on `/api`.

### Changed
- `PLUGIN_CATALOG` gained `queue` and `events`; `--with queue` pulls in a
  backend the service can actually reach, the same way `rag` does.
- `SERVICE_KINDS` and the installer offer Go.
- CI gained three jobs: Go (`setup-go`), frontend (`setup-node`) and the docs
  site. The frontend job exists because of the router bug above.

### Not done, deliberately
- **Angular** is not generated. A hand-rolled `angular.json` that has never run
  under `ng serve` looks finished and fails in a way that is hard to attribute.
- **React Native** is not generated, for the same reason.
- **RabbitMQ and Kafka** are written against documented APIs but have not been
  round-tripped against real brokers in CI.
- **Laravel and .NET** extensions are not started. The service contract is the
  extension point; a language needs a `LanguageSpec`, a template tree and a CI
  job that builds what it generates.

## [0.3.0] - 2026-08-27

Multi-service workspaces, an API gateway, frontend generation, and the
migration/testing setup that the previous release only *documented*.

### Added

**Workspaces**
- `jfast.workspace.toml` and `jfastframework.workspace`: services register
  themselves, take the next free ten-port block, and the file records what the
  frontend should call.
- `jfast workspace init | list | gateway | env`.

**API gateway**
- `gateway` plugin: prefix-based reverse proxy with hop-by-hop header
  stripping, `X-Request-ID` propagation, and 502/504 as problem+json.
- Generated automatically once a workspace has more than one backend. One
  backend deliberately does not get one.
- Not a catch-all: only configured prefixes are proxied, so the gateway keeps
  its own `/health`, `/ready` and `/metrics`. Readiness does not probe
  upstreams, so one restart does not fail the whole system.

**Frontends**
- `jfast new service <name> --kind spa --frontend vue|react` — Vite +
  Tailwind v4 project with a working home page that calls the backend's
  `/health` on load.
- `jfast new view <Name>` — the `Modulo<Name>/{Components,Pages,Routes,Services}`
  structure, registered in the router and the sidebar at marker comments.
- `jfastframework.cli.patcher`: idempotent, loud, marker-preserving patching.
  Re-running the generator does not duplicate; a missing marker raises with the
  path instead of silently doing nothing.
- Framework auto-detected from the project, so `--frontend` is not repeated.

**Migrations and tests in generated services**
- `alembic.ini`, `migrations/env.py`, `script.py.mako` and `versions/`.
  `env.py` reads the app's own `JFAST_DB_DSN` and auto-imports every module's
  models, so autogenerate cannot silently emit an empty migration.
  `compare_type` and `compare_server_default` are on.
- `pytest.ini` and a `conftest.py` with `app` / `client` fixtures.

**Datastore selection from the terminal**
- `jfast init` — interactive installer: kind, frontend, datastores, port.
- `jfast new service --with database,cache,qdrant,rag` — the plugin list, the
  `[plugin.*]` blocks, the `.env` keys and the pinned extras all derive from it.
- `rag`'s store is inferred from the datastores chosen, so `--with qdrant,rag`
  cannot generate a service configured for pgvector.

### Changed
- **Breaking:** `jfast new service --port` now defaults to the next free block
  in the workspace instead of 8000.
- `SERVICE_KINDS` gained `spa` and `gateway`.
- `.vue`, `.jsx` and `.tsx` templates render through the square-bracket Jinja
  environment, so Vue interpolation and JSX braces survive scaffolding.

### Fixed
- **CI:** `mypy --strict` failed on `redis.asyncio.from_url` being untyped in
  some redis releases and annotated in others — a strict run that passed
  locally and failed in CI on nothing we wrote. Optional third-party packages
  are now `follow_imports = "skip"`, which is honest about types we neither
  control nor can rely on across the support matrix.
- **CI:** `scripts/smoke.sh` hardcoded `.venv/bin/python`, which does not exist
  in a CI job. It now falls back to `PATH`.
- Dropped the dead `tomli` dependency marker (`requires-python` is already
  `>=3.11`).

## [0.2.0] - 2026-08-27

Datastores became a choice, and a frontend became a service.

### Added

**Datastores**
- `VectorStore` protocol in `jfastframework.vectors`, with `Chunk` and
  `SearchHit` as the shared vocabulary. Every store normalises its score to
  cosine similarity in [0, 1].
- `qdrant` plugin — client, health check, container with HTTP and gRPC ports.
- `mongo` plugin — Motor client and database handle.
- `rag` now selects its store from config: `pgvector`, `qdrant`, or a dotted
  path to your own class. Same for the embedder.
- `InfraService.extra_ports` for containers exposing more than one port.

**Server-rendered frontends**
- `web` plugin — Jinja2 templates, static files, and `render()` with HTMX
  partial rendering: a browser navigation gets the page, an `hx-get` gets the
  fragment, from one handler.
- HTMX-aware error handling: a `JFastError` raised during an HTMX request
  returns an HTML fragment instead of `problem+json`, which HTMX would
  otherwise swap into the DOM as raw text.

**Generator**
- `jfast new service <name> [--kind api|web]` — scaffolds a whole service.
- `jfast new module <name> [--layout layered|screaming] [--ui api|htmx]`.
- `module_screaming` layout: framework-free domain, one file per use case,
  storage and HTTP at the edges, domain tests separated from use-case tests.
- `ui_htmx` overlay — composed onto either layout rather than duplicated, so
  three template trees cover all four combinations.
- Two Jinja environments in the scaffolder: `.html.j2` templates use `[[ ]]`
  for scaffold-time values so the runtime `{{ }}` the browser needs survives.
- Table names are pluralised, which also dodges the SQL reserved words that
  singular nouns keep landing on (`order`, `user`, `group`). Override with
  `--table`.

**Docs**
- `docs/modules.md`, `docs/datastores.md`.

### Changed
- **Breaking:** `jfast new <name>` is now `jfast new module <name>`.
- **Breaking:** `[plugin.rag] table` renamed to `collection` — it names a
  Qdrant collection just as often as a PostgreSQL table now.
- `rag` no longer hard-requires `database`. It declares `after` and validates
  the store it was actually configured with, naming the missing plugin.
- Module templates moved to `module_layered/`; both layouts now export
  `build_service(session, tenant_id)`, the seam the HTMX overlay consumes.
- mypy no longer pins `python_version`; it checks against the interpreter it
  runs on, which CI varies across the support matrix.

### Fixed
- `chunk_text` emitted a final sliver already contained in the previous chunk
  whenever the text did not divide evenly — a wasted embedding call and a
  duplicate in every result set.

## [0.1.0] - 2026-08-27

First alpha. Kernel and built-in plugins.

### Added
- `create_app()` with plugin resolution, registration and lifespan orchestration
- Typed configuration: `JFastSettings`, `JFastConfig`, `jfast.toml` + env
- `AppContext` with `provide` / `require` indirection between plugins
- Plugin contract: `PluginMeta`, `PluginSettings`, lifecycle hooks,
  `infra()`, `describe()`
- Registry: entry-point discovery, allow/deny lists, dependency ordering,
  cycle detection, duplicate-provider detection
- RFC 7807 `application/problem+json` error model
- `/health`, `/ready`, `/info` system endpoints
- Built-in plugins: `observability`, `metrics`, `sentry`, `database`, `cache`, `rag`
- `jfastframework.db`: declarative `Base` with a pinned constraint naming
  convention, `TimestampMixin`, `TenantMixin`, generic `BaseRepository`
- Deploy generation: `docker-compose` and `Dockerfile` derived from the plugin
  graph's `infra()` declarations
- `jfast` CLI: `new`, `describe`, `doctor`, `plugins list`, `deploy`
- Jinja2 module template, `jfastframework.testing` fixtures
- Agent surface: `AGENTS.md`, `.jfast/skills/` with four starter skills

### Notes
- Multi-tenancy is a convention enforced by `BaseRepository`, not a guarantee.
  Row-level security is phase 2. Do not describe it as isolation until then.
- The v0 prototype is preserved under `legacy/` for reference.
