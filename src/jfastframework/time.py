"""Instants are UTC; days belong to a zone. The two are not the same question.

``UTCDateTime`` already guarantees that what is *stored* is UTC and that a naive
value never reaches a column. This module covers the other half, which is where
a multi-region deployment actually breaks: *computing* with those values.

    SELECT date_trunc('day', created_at) FROM orders

answers differently on two replicas whose servers are configured with different
``TimeZone`` settings, from byte-identical rows. ``CURRENT_DATE``, ``now()::date``
and any ``AT TIME ZONE`` without an explicit zone behave the same way. The
database plugin pins every session it opens to UTC so the server's configuration
stops being an input; what remains is deciding *which* day a UTC instant belongs
to, and that is a business question with a business answer:

* the **storage zone** is UTC, always, and is not configurable;
* the **business zone** -- ``[app] timezone`` -- is what "today" means for a
  report, an invoice period or a daily quota;
* a **tenant zone** overrides the business zone for one tenant, which is the
  case one deployment serving several countries exists to handle.

Nothing here is stored in local time. Store UTC, render local: a local timestamp
is ambiguous for one hour a year and impossible for another, and no column type
records which of the two it was. When the *local* time of an event is itself the
fact -- "the shop closes at 18:00 in its own zone" -- that is a separate column
next to the zone name, never a second timestamp.

``zoneinfo`` is standard library, but on Linux it reads the system tzdata under
``/usr/share/zoneinfo`` -- including for the key ``"UTC"``. A slim container
without the ``tzdata`` package raises ``ZoneInfoNotFoundError`` for every name,
which is why ``"UTC"`` here resolves to ``datetime.UTC`` and reads no file: the
default zone keeps working on an image that has none. Naming a real zone on
such an image fails at boot with a message that says which package is missing.
See docs/timezones.md.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "day_bounds",
    "default_zone",
    "in_zone",
    "now",
    "parse",
    "set_default_zone",
    "today",
    "zone",
]

# `ZoneInfo("UTC")` reads /usr/share/zoneinfo/UTC like any other name and
# raises ZoneInfoNotFoundError on an image with no tzdata. `datetime.UTC` is a
# fixed offset with no file behind it, so the default -- and every service that
# never names a real zone -- keeps working on a slim container.
_UTC_NAME = "UTC"

_default: tzinfo = UTC


@lru_cache(maxsize=128)
def _load(name: str) -> tzinfo:
    if name == _UTC_NAME:
        return UTC
    return ZoneInfo(name)


def zone(name: str | tzinfo | None) -> tzinfo:
    """Resolve a zone name, or fall through to the configured business zone.

    An unknown or misspelled name raises here rather than at the first request
    that formats a date. ``ValueError`` and not ``ZoneInfoNotFoundError``,
    because the two ways this fails -- a typo, and a container with no tzdata
    -- need the same answer from the caller and different words in the message.
    """
    if name is None:
        return _default
    if isinstance(name, tzinfo):
        return name
    try:
        return _load(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"unknown time zone {name!r}. Use an IANA name such as "
            f"'America/Santiago' or 'UTC'. On a slim Linux image this also "
            f"means the system tzdata is missing: install the tzdata package, "
            f"or add the `tzdata` Python package as a dependency. ({exc})"
        ) from exc
    except (ValueError, KeyError) as exc:
        raise ValueError(f"invalid time zone {name!r}: {exc}") from exc


def set_default_zone(name: str | tzinfo | None) -> tzinfo:
    """Publish the business zone for the process. Called once, at boot.

    ``JFastConfig.load`` calls this with ``[app] timezone`` so that
    ``today()`` and ``day_bounds()`` answer the configured business question
    without every call site threading a zone through. Code that runs outside a
    loaded configuration -- a worker entry point, a script -- calls it itself.
    """
    global _default
    _default = zone(name) if name is not None else UTC
    return _default


def default_zone() -> tzinfo:
    """The business zone in effect, UTC until configuration says otherwise."""
    return _default


def now() -> datetime:
    """The current instant, aware, in UTC. The only clock this framework reads.

    ``datetime.now()`` without an argument returns a naive value in whatever
    zone the machine is set to, which is how a container that is UTC in
    production and Europe/Madrid on a laptop produces two different answers
    from the same code. The contract checker rejects it for that reason.
    """
    return datetime.now(UTC)


def today(tz: str | tzinfo | None = None) -> date:
    """The current *local* date in the business zone, not the server's.

    At 23:30 UTC on the 1st, a service whose business zone is
    ``America/Santiago`` is still on the 1st and one whose zone is
    ``Asia/Tokyo`` is already on the 2nd. Both are correct; the wrong answer is
    the one that depends on where the container runs.
    """
    return now().astimezone(zone(tz)).date()


def day_bounds(day: date, tz: str | tzinfo | None = None) -> tuple[datetime, datetime]:
    """The UTC half-open range ``[start, end)`` covering one local day.

    This is what "today's orders" means once the rows are UTC. The naive
    version -- midnight to midnight in UTC -- is wrong by the zone's offset at
    both edges, so it counts some of yesterday and misses the end of today.

    A local day is not always 24 hours. Both edges are resolved with
    ``fold=0``, and that is a constraint rather than a default: where local
    midnight is *ambiguous* (clocks went back) the first of the two midnights
    is the one the day starts at, and where local midnight does not *exist*
    (America/Santiago starts DST at 00:00) ``fold=0`` resolves through the
    pre-transition offset, which lands exactly on the transition instant --
    the first instant of that local day. ``fold=1`` would land an hour before
    the day began.

    Half-open, never ``BETWEEN``: a closed upper bound either double-counts
    midnight or drops the last microsecond, and which of the two depends on the
    column's precision.
    """
    tzone = zone(tz)
    start = datetime.combine(day, time.min, tzinfo=tzone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tzone)
    return start.astimezone(UTC), end.astimezone(UTC)


def in_zone(value: datetime, tz: str | tzinfo | None = None) -> datetime:
    """Render an instant in a zone. Presentation only -- never store the result.

    Refuses a naive input: there is no correct zone to assume for it, and
    assuming one is how a value lands hours off with nothing in the logs.
    """
    if value.tzinfo is None:
        raise ValueError(
            f"naive datetime {value!r} has no instant to render. Attach a "
            f"tzinfo, or read it from a UTCDateTime column."
        )
    return value.astimezone(zone(tz))


def parse(value: str, tz: str | tzinfo | None = None) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    A string without an offset is refused unless ``tz`` says how to read it,
    because the two plausible readings -- UTC, or the reader's local zone --
    differ by exactly the amount that makes a bug hard to see. Passing ``tz``
    is a caller stating which zone the *source* wrote, which is a fact about
    the source rather than a guess.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not an ISO-8601 timestamp: {value!r} ({exc})") from exc

    if parsed.tzinfo is None:
        if tz is None:
            raise ValueError(
                f"{value!r} carries no UTC offset, so the instant it names "
                f"depends on who reads it. Add an offset, or pass tz= to say "
                f"which zone wrote it."
            )
        parsed = parsed.replace(tzinfo=zone(tz))
    return parsed.astimezone(UTC)
