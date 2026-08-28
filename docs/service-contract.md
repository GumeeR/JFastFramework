# The service contract

A JFast service is not "a service written with JFast". It is a service that
satisfies this contract. That distinction is the whole reason a Go service and
a Python service can sit behind the same gateway, take ports from the same
workspace, and be fronted by the same Caddyfile — none of which know or care
which language produced them.

Keep this stable. The templates are implementation; this is the interface.

---

## 1. System endpoints

| Endpoint | Must | Must not |
| --- | --- | --- |
| `GET /health` | Return 200 while the process is up | Probe any dependency |
| `GET /ready` | Return 200 when serving, 503 when a **critical** dependency is down | Be used as the liveness probe |
| `GET /info` | Report version and inventory | Exist when `JFAST_ENV=prod` |

`/health` and `/ready` are separate because conflating them causes cascading
restarts: the database blips, liveness fails, the orchestrator kills healthy
pods, and the stampede finishes off the database.

A non-critical dependency failing makes `/ready` report `"degraded"` with a
200. A cold cache should not take a service out of rotation.

```json
// GET /health
{"status": "ok", "service": "billing", "version": "0.1.0", "env": "local"}

// GET /ready
{"status": "degraded", "service": "billing",
 "checks": {"cache": {"healthy": false, "detail": "cold", "critical": false}}}
```

## 2. Request correlation

Read `X-Request-ID` from the request. **Reuse it if present**, mint one if not.
Echo it on the response, attach it to every log line, and forward it on
outbound calls.

Minting a fresh id per service instead of reusing the caller's is the single
most common way a distributed trace becomes useless: every hop starts a new
"trace" and nothing joins up.

## 3. Errors

Every failure serialises to RFC 7807 `application/problem+json`:

```json
{"type": "about:blank", "title": "Not Found", "status": 404,
 "detail": "invoice 7 not found", "instance": "/invoices/7",
 "request_id": "9f2c…"}
```

Unhandled exceptions included. The `detail` for an unhandled error must be
generic unless `JFAST_DEBUG=true` — leaking internals to clients is an
information-disclosure finding, and the default has to be the safe one.

## 4. Configuration

From the environment, with these names:

| Variable | Meaning |
| --- | --- |
| `JFAST_APP_NAME` | Service name, used in logs and metrics |
| `JFAST_ENV` | `local` / `dev` / `staging` / `prod` |
| `JFAST_PORT` | HTTP port — the base of the service's block |
| `JFAST_DEBUG` | Whether internal error detail reaches clients |
| `JFAST_LOG_JSON_LOGS` | Structured logs on or off |

One `.env` convention covers every language. Do not invent per-language names.

## 5. Ports

A service owns **ten consecutive ports** from its base. Plugins claim offsets
inside that block:

| Offset | Use |
| --- | --- |
| +0 | HTTP |
| +1 | PostgreSQL |
| +2 | Kafka / PgBouncer |
| +3 | Redis |
| +4 | MongoDB |
| +5 / +6 | Prometheus / Grafana, RabbitMQ |
| +7 / +8 | Qdrant HTTP / gRPC |
| +9 | The service's own gRPC |

The workspace allocates the blocks. Do not hand-pick a port unless you have
checked what is already in one.

## 6. Logs

One JSON object per line, on stdout, carrying at least `service`, `env`,
`level`, `message` and — inside a request — `request_id`.

Not a file, not a socket. The runtime collects stdout; a service that manages
its own log files fights whatever is already collecting them.

---

## Languages

| | Python | Go |
| --- | --- | --- |
| Toolchain | `python3` | `go` |
| Contract implementation | `jfastframework` (imported) | `internal/jfast/` (vendored, ~300 lines) |
| Plugin system | yes | no |
| Module generator | yes | one sample module |
| Migrations | Alembic | bring your own |
| Kinds | `api`, `web`, `gateway` | `api` |

```bash
jfast new service billing                     # Python
jfast new service edge --language go          # Go
jfast new service edge --language go --grpc   # + the .proto contract
```

You only need the toolchain for the languages you actually use. A Python-only
team never installs Go.

### Why Go vendors the contract instead of importing a shared module

At two services `internal/jfast/` is 300 lines you can read in one sitting. At
ten, extract it into its own Go module and import it. Extracting on day one
buys a versioning problem before there is anything to version.

### Where Go earns its keep

A hot path, a long-lived connection handler, a binary you want to be 12MB and
start in 5ms. Not "because it is faster" — the plugin system, the migrations
and the module generator are worth more than the milliseconds on most services.

---

## Adding a language

1. Implement the six sections above.
2. Add a `LanguageSpec` to `jfastframework/languages.py`.
3. Add a `service_<lang>/` template tree.
4. Add a CI job that **builds and runs** the generated service. A scaffold
   nobody has run is a liability that looks like a feature.

Step 4 is not optional. The Go support in this repo exists because CI compiles
the generated service, runs its tests, starts the binary and curls it — not
because the template looks right.

## What is deliberately not in the contract

- **A shared client library.** Services talk HTTP or gRPC. A shared client is
  a shared deploy.
- **A common ORM or serialisation format** beyond JSON on the wire.
- **A required tracing vendor.** `X-Request-ID` is the floor; OpenTelemetry
  is a plugin, not a mandate.
