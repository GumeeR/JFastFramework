"""Time zones: the arithmetic, not the storage.

Every assertion here is a number that a naive implementation gets wrong. A
``day_bounds`` test written in UTC asserts the half that already worked -- both
edges land on midnight and the offset is zero -- so the cases below are all
zones with an offset, and the two edges of a DST transition, where a local day
is 23 or 25 hours long and the "obvious" 24-hour arithmetic is off by one hour
for a whole day.

Two zones on purpose: America/Santiago changes its clocks at 00:00, so on the
day DST starts local midnight *does not exist*; America/Mexico_City changed
them at 02:00 until it abolished DST in 2022, so midnight exists and the
transition falls inside the day. They are the same rule and different code
paths, and a 2026 date in Mexico City proves the offset is read from tzdata
rather than remembered from 2021.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends, Request

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.tenancy import TenancyPlugin, tenant_zone
from jfastframework.settings import JFastConfig, JFastSettings
from jfastframework.testing import build_test_app, client_for
from jfastframework.time import (
    _load,
    day_bounds,
    default_zone,
    in_zone,
    now,
    parse,
    set_default_zone,
    today,
    zone,
)

SANTIAGO = "America/Santiago"
MEXICO_CITY = "America/Mexico_City"


@pytest.fixture(autouse=True)
def _restore_default_zone() -> Iterator[None]:
    """The business zone is process-wide; a test that moves it must put it back."""
    previous = default_zone()
    yield
    set_default_zone(previous)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


# -- now / today -------------------------------------------------------


def test_now_is_aware_and_utc() -> None:
    value = now()
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


def test_today_depends_on_the_zone_not_the_machine() -> None:
    """+14 and -11 are 25 hours apart, so their dates never agree."""
    assert today("Pacific/Kiritimati") != today("Pacific/Niue")


def test_today_follows_the_configured_business_zone() -> None:
    set_default_zone("Pacific/Kiritimati")
    east = today()
    set_default_zone("Pacific/Niue")
    assert today() != east


# -- day_bounds: the offset half ---------------------------------------


def test_day_bounds_in_utc_is_the_half_that_already_worked() -> None:
    start, end = day_bounds(date(2026, 3, 1), "UTC")
    assert (start, end) == (_utc("2026-03-01T00:00:00+00:00"), _utc("2026-03-02T00:00:00+00:00"))


def test_day_bounds_is_shifted_by_the_offset() -> None:
    """Mexico City is -06 in 2026: the local day starts at 06:00 UTC.

    Midnight-to-midnight UTC would count six hours of the previous local day
    and miss the last six hours of this one.
    """
    start, end = day_bounds(date(2026, 3, 1), MEXICO_CITY)
    assert start == _utc("2026-03-01T06:00:00+00:00")
    assert end == _utc("2026-03-02T06:00:00+00:00")
    assert end - start == timedelta(hours=24)


def test_day_bounds_does_not_remember_an_abolished_dst() -> None:
    """Mexico City dropped DST in 2022. The 2026 date it would have moved on.

    A hardcoded offset table written before 2022 answers this with -05.
    """
    start, _ = day_bounds(date(2026, 4, 5), MEXICO_CITY)
    assert start == _utc("2026-04-05T06:00:00+00:00")


# -- day_bounds: the DST half ------------------------------------------


def test_spring_forward_day_is_23_hours_when_midnight_does_not_exist() -> None:
    """Santiago starts DST at 00:00 on 2026-09-06: the clock goes 00:00 -> 01:00.

    So local midnight is not a real instant that day. The first instant of the
    local day is 01:00-03, which is 04:00 UTC -- exactly the transition. The
    ``fold=1`` reading would give 03:00 UTC, an hour before the day began.
    """
    start, end = day_bounds(date(2026, 9, 6), SANTIAGO)
    assert start == _utc("2026-09-06T04:00:00+00:00")
    assert end == _utc("2026-09-07T03:00:00+00:00")
    assert end - start == timedelta(hours=23)
    assert start.astimezone(zone(SANTIAGO)).isoformat() == "2026-09-06T01:00:00-03:00"


def test_fall_back_day_is_25_hours() -> None:
    """Santiago ends DST at the close of 2026-04-04, so that day gets an hour."""
    start, end = day_bounds(date(2026, 4, 4), SANTIAGO)
    assert start == _utc("2026-04-04T03:00:00+00:00")
    assert end == _utc("2026-04-05T04:00:00+00:00")
    assert end - start == timedelta(hours=25)


def test_mexico_city_dst_transitions_at_02_00_not_at_midnight() -> None:
    """The other shape: midnight exists, and the hour is added or lost inside the day."""
    short_start, short_end = day_bounds(date(2021, 4, 4), MEXICO_CITY)
    assert short_start == _utc("2021-04-04T06:00:00+00:00")
    assert short_end == _utc("2021-04-05T05:00:00+00:00")
    assert short_end - short_start == timedelta(hours=23)

    long_start, long_end = day_bounds(date(2021, 10, 31), MEXICO_CITY)
    assert long_start == _utc("2021-10-31T05:00:00+00:00")
    assert long_end == _utc("2021-11-01T06:00:00+00:00")
    assert long_end - long_start == timedelta(hours=25)


@pytest.mark.parametrize(
    ("day", "name"),
    [
        (date(2026, 9, 5), SANTIAGO),
        (date(2026, 9, 6), SANTIAGO),
        (date(2026, 4, 4), SANTIAGO),
        (date(2021, 4, 4), MEXICO_CITY),
        (date(2021, 10, 31), MEXICO_CITY),
    ],
)
def test_consecutive_days_touch_exactly_once(day: date, name: str) -> None:
    """No gap and no overlap across a transition: an instant belongs to one day."""
    _, end = day_bounds(day, name)
    next_start, _ = day_bounds(day + timedelta(days=1), name)
    assert end == next_start


def test_day_bounds_uses_the_business_zone_when_none_is_given() -> None:
    set_default_zone(SANTIAGO)
    assert day_bounds(date(2026, 9, 6)) == day_bounds(date(2026, 9, 6), SANTIAGO)


# -- zone resolution ---------------------------------------------------


def test_unknown_zone_names_the_package_it_may_be_missing() -> None:
    with pytest.raises(ValueError, match="tzdata"):
        zone("Mars/Olympus_Mons")


def test_utc_needs_no_system_tzdata() -> None:
    """`ZoneInfo("UTC")` reads a file like any other name; the default must not.

    This is the slim-container case: an image with no tzdata package would
    otherwise fail at import, before a service that never names a zone gets to
    do anything.
    """
    _load.cache_clear()
    # ZoneInfo keeps its own cache, so a zone another test already built would
    # come back from memory and the empty search path would prove nothing.
    zoneinfo.ZoneInfo.clear_cache()
    zoneinfo.reset_tzpath([])
    try:
        assert zone("UTC") is UTC
        with pytest.raises(ValueError, match="tzdata"):
            zone(SANTIAGO)
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()
        _load.cache_clear()


def test_a_tzinfo_passes_through() -> None:
    assert zone(UTC) is UTC


# -- parse / in_zone ---------------------------------------------------


def test_parse_refuses_a_string_with_no_offset() -> None:
    with pytest.raises(ValueError, match="carries no UTC offset"):
        parse("2026-03-01T12:00:00")


def test_parse_reads_a_naive_string_in_a_stated_zone() -> None:
    assert parse("2026-03-01T12:00:00", MEXICO_CITY) == _utc("2026-03-01T18:00:00+00:00")


def test_parse_normalises_an_offset_to_utc() -> None:
    assert parse("2026-03-01T12:00:00-03:00") == _utc("2026-03-01T15:00:00+00:00")


def test_in_zone_refuses_naive_input() -> None:
    with pytest.raises(ValueError, match="naive datetime"):
        in_zone(datetime(2026, 3, 1, 12, 0))


def test_in_zone_renders_without_changing_the_instant() -> None:
    instant = _utc("2026-09-06T04:00:00+00:00")
    rendered = in_zone(instant, SANTIAGO)
    assert rendered == instant
    assert rendered.isoformat() == "2026-09-06T01:00:00-03:00"


# -- [app] timezone ----------------------------------------------------


def test_an_unknown_business_zone_fails_at_boot() -> None:
    with pytest.raises(ValueError, match="Mars/Olympus_Mons"):
        JFastSettings(timezone="Mars/Olympus_Mons")


def test_the_default_business_zone_is_utc() -> None:
    assert JFastSettings().timezone == "UTC"
    assert JFastSettings().business_zone is UTC


def test_loading_a_config_publishes_the_business_zone(tmp_path: Path) -> None:
    config_file = tmp_path / "jfast.toml"
    config_file.write_text(f'[app]\nname = "billing"\ntimezone = "{SANTIAGO}"\n', encoding="utf-8")
    config = JFastConfig.load(config_file)
    assert config.settings.timezone == SANTIAGO
    assert today() == today(SANTIAGO)


# -- per-tenant zones --------------------------------------------------
#
# The multi-region case: one deployment, tenants in different countries, each
# one's day starting at a different instant.


def _tenant_app(**config: object) -> Any:
    router = APIRouter()

    @router.get("/day")
    async def day(request: Request, tz: Any = Depends(tenant_zone)) -> dict[str, str | None]:
        start, end = day_bounds(date(2026, 9, 6), tz)
        return {
            "tenant": request.state.tenant_id,
            "zone": str(tz),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    return build_test_app(
        plugins=["observability", "tenancy"],
        extra_plugins=[TenancyPlugin],
        routers=[router],
        raw={"plugin": {"tenancy": config}},
    )


async def test_each_tenant_gets_its_own_day_boundaries() -> None:
    app = _tenant_app(
        sources=["subdomain"],
        base_domain="app.example.com",
        timezones={"acme": SANTIAGO, "globex": MEXICO_CITY},
    )
    async with client_for(app) as client:
        acme = (await client.get("/day", headers={"host": "acme.app.example.com"})).json()
        globex = (await client.get("/day", headers={"host": "globex.app.example.com"})).json()

    assert acme["start"] == "2026-09-06T04:00:00+00:00"
    assert globex["start"] == "2026-09-06T06:00:00+00:00"
    assert acme["start"] != globex["start"]


async def test_a_tenant_with_no_zone_gets_the_business_zone() -> None:
    """Never the server's: that is the thing that makes an answer move house."""
    set_default_zone(MEXICO_CITY)
    app = _tenant_app(
        sources=["subdomain"],
        base_domain="app.example.com",
        timezones={"acme": SANTIAGO},
    )
    async with client_for(app) as client:
        unlisted = (await client.get("/day", headers={"host": "initech.app.example.com"})).json()
        untenanted = (await client.get("/day", headers={"host": "app.example.com"})).json()

    assert unlisted["tenant"] == "initech"
    assert unlisted["zone"] == MEXICO_CITY
    assert untenanted["tenant"] is None
    assert untenanted["zone"] == MEXICO_CITY


def test_an_unknown_tenant_zone_stops_the_boot() -> None:
    with pytest.raises(PluginError, match="Mars/Olympus_Mons"):
        _tenant_app(
            sources=["subdomain"],
            base_domain="app.example.com",
            timezones={"acme": "Mars/Olympus_Mons"},
        )
