# Time zones

Storage was settled in `0.1.0a4`: `UTCDateTime` writes UTC, reads back UTC, and
refuses a naive datetime on write. This page is about the other half —
**computing** with those values — which is where a multi-region deployment
actually goes wrong.

## The decision

**Nothing is stored in local time. Store UTC, render local.**

Not a preference. A local timestamp is *ambiguous* for one hour every autumn
and *impossible* for one hour every spring, and no column type records which of
the two a given row was. Order it, subtract it, or replicate it to a machine in
another country and there is no way to recover what the writer meant. UTC has
neither problem: every instant has exactly one representation, and every
representation names exactly one instant.

If someone needs the local time **of when something happened** — not of now —
that is a separate column with the zone name beside it:

```python
closed_at: Mapped[datetime] = mapped_column(UTCDateTime)   # the instant
closed_at_zone: Mapped[str]                                # "America/Santiago"
```

Never a second timestamp holding the same moment in local time. Two timestamps
for one event is two chances to be wrong and no way to tell which one is.

## The failure this exists to prevent

```sql
SELECT date_trunc('day', created_at), sum(total) FROM orders GROUP BY 1
```

`date_trunc` computes in the **session's** `TimeZone`, which defaults to the
server's. Two replicas of the same data, configured differently, answer
differently — and neither raises, logs, or looks wrong:

```
row stored: 2026-03-01T02:30:00+00:00, identical in both cases

TimeZone=America/Santiago   date_trunc -> 2026-02-28   ::date -> 2026-02-28
TimeZone=UTC                date_trunc -> 2026-03-01   ::date -> 2026-03-01
```

Different day, different month, same row. `CURRENT_DATE`, `now()::date`,
`localtimestamp` and every `AT TIME ZONE` without an explicit zone behave the
same way. Nobody notices until someone reconciles two reports.

## Three zones, and they are not the same zone

| | What it is | Who sets it |
| --- | --- | --- |
| **Storage zone** | UTC. Always. | Nobody — not configurable |
| **Session zone** | What the database computes in | `[plugin.database] session_timezone`, pinned to UTC |
| **Business zone** | What "today" means for a report | `[app] timezone` |
| **Tenant zone** | The business zone for one tenant | `[plugin.tenancy.timezones]` |

The server's own zone is deliberately absent from that table. It is an accident
of where the container runs and it is never allowed to decide an answer.

## The session zone is pinned to UTC

Every engine the `database` plugin opens carries `timezone=UTC` as an asyncpg
**startup parameter**:

```toml
[plugin.database]
session_timezone = "UTC"   # the default
```

A startup parameter and not a `SET` after connect, deliberately. A pooled
connection is handed out mid-life, so a statement that ran once when the socket
opened is one `DISCARD ALL`, one `RESET ALL` or one pgbouncer server-reset away
from being gone — and the session then falls back to the server's zone with
nothing to show for it. A startup parameter is part of what the connection *is*,
and asyncpg replays it on reconnect.

This applies to named connections, replicas, and per-tenant engines alike: a
tenant database is a database like any other, and the report it answers must
not depend on which server that tenant landed on.

Setting `session_timezone = ""` opts out and leaves the server's setting alone.
That is only right when something outside the service already guarantees it.

Only `postgresql+asyncpg` DSNs get the parameter — `server_settings` is an
asyncpg argument and another driver would reject it. SQLite has no server-side
zone to pin.

## `[app] timezone` — the business zone

```toml
[app]
timezone = "America/Santiago"
```

This is what answers "what day is it" for a report, an invoice period or a
daily quota. Default `"UTC"`. The name is validated against `zoneinfo` **at
boot**, not at the first request that formats a date:

```
ValidationError: unknown time zone 'America/Santaigo'. Use an IANA name such as
'America/Santiago' or 'UTC'. ...
```

A typo here is otherwise invisible until a month-end total lands on the wrong
day.

## `jfastframework.time`

The small module every project rewrites badly. It is small on purpose — it
earns its place by being correct, not broad.

```python
from jfastframework.time import now, today, day_bounds, in_zone, parse

now()                       # aware, UTC, always
today()                     # the local date in the business zone
today("America/Santiago")   # the local date in a named zone
day_bounds(today())         # the UTC half-open range of that local day
in_zone(value, tz)          # render an instant; refuses naive input
parse("2026-03-01T12:00:00-03:00")   # aware UTC; refuses a string with no offset
```

### `day_bounds` is the one that matters

"Today's orders" is a time zone question. The naive version — midnight to
midnight in UTC — is wrong by the offset at **both** edges: it counts part of
yesterday and misses the end of today.

```python
start, end = day_bounds(date(2026, 3, 1), "America/Mexico_City")
# 2026-03-01T06:00:00+00:00 .. 2026-03-02T06:00:00+00:00
```

```python
rows = await session.execute(
    select(Order).where(Order.created_at >= start, Order.created_at < end)
)
```

Half-open, never `BETWEEN`. A closed upper bound either double-counts midnight
or drops the last microsecond, and which of the two depends on the column's
precision.

### A local day is not always 24 hours

This is the half that breaks, and the half worth testing. Real numbers, from
`tests/test_time.py`:

| Local day | Zone | UTC range | Length |
| --- | --- | --- | --- |
| 2026-09-06 | America/Santiago | `04:00Z` → `2026-09-07T03:00Z` | **23 h** |
| 2026-04-04 | America/Santiago | `03:00Z` → `2026-04-05T04:00Z` | **25 h** |
| 2021-04-04 | America/Mexico_City | `06:00Z` → `2021-04-05T05:00Z` | **23 h** |
| 2021-10-31 | America/Mexico_City | `05:00Z` → `2021-11-01T06:00Z` | **25 h** |
| 2026-03-01 | America/Mexico_City | `06:00Z` → `2026-03-02T06:00Z` | 24 h |

Two zones because they break differently:

- **Santiago** changes its clocks at **00:00**. On 2026-09-06 the clock goes
  straight from `00:00` to `01:00`, so *local midnight does not exist that day*.
  `day_bounds` resolves both edges with `fold=0`, which reads a non-existent
  local time through the pre-transition offset and lands exactly on the
  transition instant — the true first instant of that local day. `fold=1` would
  give `03:00Z`, an hour *before* the day began.
- **Mexico City** changed its clocks at **02:00** until it abolished DST in
  2022. Midnight exists; the hour is added or lost inside the day. And the 2026
  row is the point: the same calendar date that would have moved in 2021 does
  not move now, because the offset is read from tzdata rather than remembered.

Consecutive days touch exactly once across every one of those transitions —
no gap, no overlap, so an instant belongs to exactly one local day.

## Per-tenant zones

One deployment, tenants in different countries, each seeing its own day
boundaries:

```toml
[plugin.tenancy.timezones]
acme = "America/Santiago"
globex = "America/Mexico_City"
```

```python
from fastapi import Depends
from jfastframework.plugins.builtin.tenancy import tenant_zone
from jfastframework.time import day_bounds, today

@router.get("/reports/today")
async def report(tz = Depends(tenant_zone)):
    start, end = day_bounds(today(tz), tz)
    ...
```

**A tenant with no entry gets `[app] timezone`**, which defaults to UTC — never
the server's zone, because that is precisely the thing that makes an answer
depend on where the container runs. The same fallback applies to a request that
resolved to no tenant at all, which is what health checks, `/metrics` and the
docs are.

Every name in the table is validated at boot. A typo stops the service instead
of shifting one tenant's reports by a day.

The map is static configuration. A service that keeps tenant zones in a
database sets `request.state.tenant_timezone` from its own dependency or
middleware; `tenant_zone` reads that attribute and only falls back when it is
absent.

## The contract check

`datetime.now()` with no argument and `datetime.utcnow()` are where naive
values are born. `UTCDateTime` refusing one at write time is the backstop — and
a backstop tells you a row was wrong, not which line wrote it. So the checker
rejects them:

```
service.py:12: naive-datetime: datetime.datetime.now() returns a datetime with
no time zone  (it reads as local time to whoever renders it, and UTCDateTime
refuses it on write. Use datetime.now(UTC), or jfastframework.time.now())
```

Reported: `datetime.now()` (no `tz`), `datetime.utcnow()`,
`datetime.utcfromtimestamp()`. Not reported: `datetime.now(UTC)`,
`datetime.now(tz=zone)`, and `datetime.now(*args)` — the syntax cannot say what
a splat contains, and a checker that guessed would be wrong confidently.

Waivable inline like every other finding:

```python
return datetime.now()  # contracts: allow wall clock, for display only
```

The rule lives in `[rules.async_safety]` — the only rules table the contract
model had — but it has its own switch, so silencing it does not silence the
event-loop check with it:

```toml
[rules.async_safety]
enabled = true
naive_datetime = false   # naive-datetime off, async-blocking still on
```

Measured on a generated service with one `time.sleep()` and one
`datetime.now()` inside `async def`, and both again in an async method:

| Setting | `jfast contracts check` | Exit |
| --- | --- | --- |
| defaults | 4 violations: 2 `async-blocking`, 2 `naive-datetime` | 5 |
| `naive_datetime = false` | 2 violations: `async-blocking` only | 5 |
| `enabled = false` | none | 0 |

`enabled = false` still turns both off. That is the switch worth knowing about
before anyone reaches for it — `naive_datetime = false` is the narrower one.

## `zoneinfo` and where the zone database comes from

`zoneinfo` is standard library since 3.9, but the database it reads is not part
of it. On Linux it reads the **system** copy under `/usr/share/zoneinfo` —
including for the key `"UTC"`. Windows ships none at all, and neither does an
Alpine image or a distroless one.

So `tzdata`, the PyPI package, is a **kernel dependency of this framework**. A
`timezone` setting that stops the boot on an unknown name cannot leave the
presence of names to whichever base image somebody picked. It is ~500 KB of
pure Python, and `zoneinfo` still prefers the system copy where there is one —
the package is the floor, not an override.

What that buys, concretely:

- `America/Mexico_City` resolves on a Windows laptop, on `python:3.12-slim`
  (which does ship the system database) and on Alpine (which does not).
- Nobody has to remember a `RUN apt-get install tzdata` line, which is the
  kind of thing that gets remembered on the image somebody tested and
  forgotten on the one they shipped.

Where a name still fails to resolve, something has removed the package;
`pip show tzdata` says whether it is there. `"UTC"` is the one key that never
depends on any of this: it resolves to `datetime.UTC`, a fixed offset with no
file behind it.

Do **not** set the container's `TZ` and call it done. That changes what
`datetime.now()` returns, which this framework does not read, and leaves the
database session zone — the thing that actually decides the report — untouched.

## `jfast doctor`

`doctor` reports the database's `TimeZone` and warns when it is not UTC. The
warning is not about this service, which pins its own sessions: it is about
every *other* client of that database — `psql`, a BI tool, a migration run by
hand — which is still computing days in the server's zone.
