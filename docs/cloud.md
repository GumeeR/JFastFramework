# Secrets, functions and notifications

Three things a service reaches for once it leaves your laptop: configuration it
cannot keep in a file, a way to run something without a server, and a way to
reach a phone.

## Secrets

Environment variables are the right interface. Every plugin's settings already
read them, and so does every language in a workspace. What is usually wrong is
how they get there: a `.env` copied onto a server, or a value pasted into a CI
variable nobody can rotate.

```python
from jfastframework.secrets import load_secrets
from jfastframework import create_app

load_secrets()          # reads JFAST_SECRETS_*, populates os.environ
app = create_app()
```

```bash
JFAST_SECRETS_PROVIDER=aws
JFAST_SECRETS_NAME=prod/billing
JFAST_SECRETS_REGION=eu-west-1
```

or:

```bash
JFAST_SECRETS_PROVIDER=gcp
JFAST_SECRETS_NAME=billing
JFAST_SECRETS_PROJECT=my-project
```

The stored secret is a JSON object or `KEY=value` lines:

```json
{ "JFAST_DB_DSN": "postgresql+asyncpg://...", "JFAST_AUTH_SECRET": "..." }
```

Two rules it enforces:

- **An existing environment value wins.** A developer exporting `JFAST_DB_DSN`
  locally is not silently overridden by a production secret, and a container's
  own configuration beats a stale stored copy. Pass `override=True` to reverse
  that, deliberately.
- **Nothing is logged.** The log line says how many names arrived and which,
  never a value.

Nested JSON is refused rather than flattened: `{"db": {"host": ...}}` has no
obvious environment-variable spelling, and inventing one produces a name nobody
can predict. Store it as a JSON string if the value really is structured.

Use `prefix` to keep several services in one secret without them reading each
other's configuration:

```bash
JFAST_SECRETS_PREFIX=BILLING_        # BILLING_DSN in the secret becomes DSN
```

No credentials are passed to either client. Both use their cloud's default
chain, so an instance role, a task role or workload identity works and there is
no long-lived key to leak.

Install: `pip install jfastframework[aws]` or `jfastframework[gcp]`.

## Functions

```bash
jfast deploy function billing --target aws --account-id 123456789012
jfast deploy function billing --target gcp --project my-project
```

This writes files. It does not run them. Read the script before you do — these
are the only generated artifacts that spend money.

Both targets run **the same ASGI app** the container runs. A second copy of the
application that exists only in the cloud is a second copy that drifts.

**AWS Lambda** gets `handler.py` (Mangum), `Dockerfile.lambda` and
`deploy-lambda.sh`. Container image, not a zip: the dependency set here —
pydantic's compiled core, often asyncpg — makes the 250 MB unzipped limit a
recurring surprise, and the image path does not have that problem. The script
pins `--platform linux/amd64`, because an ARM build on Lambda's x86 runtime
fails as a timeout rather than an error, which is a genuinely miserable hour.

**Google** gets `deploy-cloudrun.sh`. "Cloud Functions gen2" *is* Cloud Run;
deploying the container directly skips the functions-framework shim, whose only
job is making a WSGI handler look like a container — which an ASGI app already
is.

Both default to **private**. `--public` opts in and prints a warning, because
both clouds make public the easy option and the mistake is silent.

### When not to use this

Cold starts are the trade-off. A JFast app with the database plugin opens a
connection pool on startup, and Lambda runs startup on every cold container.
For a service that is called constantly, a container behind Caddy is cheaper
*and* faster.

Functions win for spiky, low-volume or event-driven work: a webhook receiver, a
nightly job, a thumbnailer.

## Notifications

Push, over Firebase Cloud Messaging.

```toml
[plugin.notifications]
backend = "fcm"          # fcm | console
project_id = "my-project"
```

```python
from jfastframework.plugins.builtin.notifications import Notification

notifications = request.app.state.jfast.require("notifications")
await notifications.send(Notification(
    title="Invoice paid",
    body="INV-1042 was paid.",
    tokens=[device_token],
    data={"invoice_id": "1042"},
))
```

The default backend is `console`: it logs instead of sending. No Firebase
project, no credentials, and the payload is visible — which is what you want in
development and in tests. In production it warns that nothing is being
delivered.

FCM's HTTP v1 API, not the legacy server-key one: the legacy key cannot be
scoped or rotated without breaking every client, and Google has been retiring
it. Credentials come from Application Default Credentials unless you set
`credentials_json`; on GKE or Cloud Run that means workload identity and no key
file anywhere.

**Send from a worker, not from a request handler.** A push that fails should be
retried by the queue, not fail the HTTP request that triggered it. Enable the
`queue` plugin and enqueue a job; the plugin logs a reminder when it sees no
queue.

`SendResult.invalid_tokens` holds the tokens FCM reported as unregistered — the
app was uninstalled, or the token rotated. Delete them from your database.
Retrying them forever is how a push backlog grows without bound.

Install: `pip install jfastframework[fcm]`.

**Not verified end to end.** The payload construction and the batching are
tested; nothing has been sent to a real FCM project in CI. Treat your first
send as the test.

## See also

- [Deployment](deploy.md) — the container path
- [Queues and events](queues-and-events.md) — where a send belongs
