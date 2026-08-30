"""Event-loop blocking: what the checker catches, and what it refuses to guess.

The negative cases carry most of the weight here. A blocking-call checker that
flags correct offloading is worse than no checker at all, because the first
thing anyone does about a noisy rule is turn it off.
"""

from __future__ import annotations

from pathlib import Path

from jfastframework.contracts import AsyncSafety, Contract, Violation, check_blocking


def _violations(
    tmp_path: Path, source: str, *, safety: AsyncSafety | None = None
) -> list[Violation]:
    (tmp_path / "service.py").write_text(source, encoding="utf-8")
    contract = Contract(project="t", async_safety=safety or AsyncSafety())
    return check_blocking(contract, tmp_path)


def _scan(tmp_path: Path, source: str, *, safety: AsyncSafety | None = None) -> list[str]:
    return [v.message for v in _violations(tmp_path, source, safety=safety)]


# -- caught ------------------------------------------------------------


def test_time_sleep_in_coroutine(tmp_path: Path) -> None:
    found = _scan(tmp_path, "import time\n\n\nasync def handler():\n    time.sleep(1)\n")
    assert len(found) == 1
    assert "time.sleep() blocks the event loop inside async handler()" in found[0]


def test_import_alias_is_resolved(tmp_path: Path) -> None:
    found = _scan(tmp_path, "import time as t\n\n\nasync def h():\n    t.sleep(1)\n")
    assert "time.sleep()" in found[0]


def test_from_import_is_resolved(tmp_path: Path) -> None:
    found = _scan(tmp_path, "from time import sleep\n\n\nasync def h():\n    sleep(1)\n")
    assert "time.sleep()" in found[0]


def test_sync_client_stored_on_self(tmp_path: Path) -> None:
    """The shape a linter reading one function at a time cannot see."""
    source = "\n".join(
        [
            "import boto3",
            "",
            "",
            "class Uploader:",
            "    def __init__(self):",
            "        self._s3 = boto3.client('s3')",
            "",
            "    async def upload(self, key):",
            "        self._s3.put_object(Key=key)",
            "",
        ]
    )
    found = _scan(tmp_path, source)
    assert len(found) == 1
    assert "self._s3.put_object() is a synchronous client call" in found[0]


def test_sync_client_in_a_local(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "import boto3",
            "",
            "",
            "async def upload():",
            "    s3 = boto3.client('s3')",
            "    s3.put_object()",
            "",
        ]
    )
    assert any("s3.put_object() is a synchronous client call" in m for m in _scan(tmp_path, source))


def test_local_sync_helper_that_blocks(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "import time",
            "",
            "",
            "def warm_cache():",
            "    time.sleep(2)",
            "",
            "",
            "async def handler():",
            "    warm_cache()",
            "",
        ]
    )
    assert any("calls time.sleep(), which blocks" in m for m in _scan(tmp_path, source))


def test_blocking_path_io(tmp_path: Path) -> None:
    source = "from pathlib import Path\n\n\nasync def h(p):\n    return Path(p).read_text()\n"
    assert "pathlib.Path.read_text()" in _scan(tmp_path, source)[0]


def test_nested_event_loop(tmp_path: Path) -> None:
    found = _scan(tmp_path, "import asyncio\n\n\nasync def h(c):\n    asyncio.run(c)\n")
    assert "asyncio.run()" in found[0]


def test_run_until_complete_needs_no_import_to_be_wrong(tmp_path: Path) -> None:
    found = _scan(tmp_path, "async def h(loop, c):\n    loop.run_until_complete(c)\n")
    assert "run_until_complete()" in found[0]


def test_synchronous_pillow_in_a_coroutine(tmp_path: Path) -> None:
    """Decoding an image is seconds of CPU, and no linter attributes it to a library."""
    source = "\n".join(
        [
            "import io",
            "from PIL import Image",
            "",
            "",
            "async def optimise(data):",
            "    return Image.open(io.BytesIO(data))",
            "",
        ]
    )
    found = _scan(tmp_path, source)
    assert len(found) == 1
    assert "PIL.Image.open() blocks the event loop inside async optimise()" in found[0]


def test_pillow_save_on_an_image_stored_in_a_local(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "import io",
            "from PIL import Image",
            "",
            "",
            "async def optimise(data, out):",
            "    image = Image.open(io.BytesIO(data))",
            "    image.save(out, format='WEBP')",
            "",
        ]
    )
    assert any("image.save() is a synchronous client call" in m for m in _scan(tmp_path, source))


def test_pillow_chained_in_one_expression(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "import io",
            "from PIL import Image",
            "",
            "",
            "async def optimise(data, out):",
            "    Image.open(io.BytesIO(data)).save(out)",
            "",
        ]
    )
    assert any("PIL.Image.open.save()" in m for m in _scan(tmp_path, source))


def test_extra_blocking_from_the_contract(tmp_path: Path) -> None:
    safety = AsyncSafety(extra_blocking={"mylib.fetch": "mylib.afetch"})
    source = "import mylib\n\n\nasync def h():\n    mylib.fetch()\n"
    found = _violations(tmp_path, source, safety=safety)
    assert len(found) == 1
    assert "mylib.fetch() blocks the event loop" in found[0].message
    # The replacement the team declared is what the developer actually needs.
    assert "mylib.afetch" in found[0].why


# -- deliberately not caught -------------------------------------------


def test_synchronous_handler_is_not_a_bug(tmp_path: Path) -> None:
    """FastAPI runs a `def` handler in a threadpool. Flagging it is wrong."""
    assert _scan(tmp_path, "import time\n\n\ndef handler():\n    time.sleep(1)\n") == []


def test_offloaded_with_to_thread(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "import asyncio",
            "import time",
            "",
            "",
            "async def h():",
            "    await asyncio.to_thread(lambda: time.sleep(1))",
            "",
        ]
    )
    assert _scan(tmp_path, source) == []


def test_sync_closure_handed_to_an_executor(tmp_path: Path) -> None:
    """The shape `storage/s3.py` uses. It must stay silent."""
    source = "\n".join(
        [
            "import asyncio",
            "import boto3",
            "",
            "",
            "class S3:",
            "    def __init__(self):",
            "        self._client = boto3.client('s3')",
            "",
            "    async def put(self, key):",
            "        def _put():",
            "            return self._client.put_object(Key=key)",
            "",
            "        return await asyncio.to_thread(_put)",
            "",
        ]
    )
    assert _scan(tmp_path, source) == []


def test_pillow_inside_a_closure_handed_to_a_thread_is_silent(tmp_path: Path) -> None:
    """The shape `storage/images.py` uses. It must stay silent."""
    source = "\n".join(
        [
            "import asyncio",
            "import io",
            "from PIL import Image",
            "",
            "",
            "async def optimise(data):",
            "    def _encode():",
            "        image = Image.open(io.BytesIO(data))",
            "        out = io.BytesIO()",
            "        image.save(out, format='WEBP')",
            "        return out.getvalue()",
            "",
            "    return await asyncio.to_thread(_encode)",
            "",
        ]
    )
    assert _scan(tmp_path, source) == []


def test_unresolvable_call_is_left_alone(tmp_path: Path) -> None:
    """`self._client.ping()` could be anything. Guessing would flag everything."""
    source = "\n".join(
        [
            "class Cache:",
            "    async def health(self):",
            "        return await self._client.ping()",
            "",
        ]
    )
    assert _scan(tmp_path, source) == []


def test_pure_path_methods_are_not_io(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "from pathlib import Path",
            "",
            "",
            "async def h(p):",
            "    return Path(p).with_suffix('.j2')",
            "",
        ]
    )
    assert _scan(tmp_path, source) == []


def test_waiver_suppresses_with_a_reason(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "import time",
            "",
            "",
            "async def h():",
            "    time.sleep(0)  # contracts: allow one-off at startup",
            "",
        ]
    )
    assert _scan(tmp_path, source) == []


def test_allow_in_paths(tmp_path: Path) -> None:
    safety = AsyncSafety(allow_in=["service.py"])
    source = "import time\n\n\nasync def h():\n    time.sleep(1)\n"
    assert _scan(tmp_path, source, safety=safety) == []


def test_disabled(tmp_path: Path) -> None:
    safety = AsyncSafety(enabled=False)
    source = "import time\n\n\nasync def h():\n    time.sleep(1)\n"
    assert _scan(tmp_path, source, safety=safety) == []


def test_tests_are_excluded_by_default(tmp_path: Path) -> None:
    (tmp_path / "test_thing.py").write_text(
        "import time\n\n\nasync def test_x():\n    time.sleep(1)\n", encoding="utf-8"
    )
    assert check_blocking(Contract(project="t"), tmp_path) == []


# -- naive datetimes ---------------------------------------------------
#
# The rule the blocking table could not express on its own: `datetime.now()`
# and `datetime.now(UTC)` are the same dotted name and only one of them is a
# defect, so the negative cases below carry the weight.


def test_datetime_now_without_a_zone(tmp_path: Path) -> None:
    source = "from datetime import datetime\n\n\ndef stamp():\n    return datetime.now()\n"
    found = _violations(tmp_path, source)
    assert len(found) == 1
    assert found[0].rule == "naive-datetime"
    assert "datetime.datetime.now() returns a datetime with no time zone" in found[0].message
    assert "jfastframework.time.now()" in found[0].why


def test_datetime_now_with_a_zone_is_correct_code(tmp_path: Path) -> None:
    source = "from datetime import UTC, datetime\n\n\ndef stamp():\n    return datetime.now(UTC)\n"
    assert _scan(tmp_path, source) == []


def test_datetime_now_with_a_tz_keyword_is_correct_code(tmp_path: Path) -> None:
    source = (
        "from datetime import UTC, datetime\n\n\ndef stamp():\n    return datetime.now(tz=UTC)\n"
    )
    assert _scan(tmp_path, source) == []


def test_utcnow_is_naive_whatever_it_is_given(tmp_path: Path) -> None:
    source = "import datetime\n\n\ndef stamp():\n    return datetime.datetime.utcnow()\n"
    found = _scan(tmp_path, source)
    assert len(found) == 1
    assert "datetime.datetime.utcnow()" in found[0]


def test_utcfromtimestamp_is_reported_with_its_replacement(tmp_path: Path) -> None:
    source = (
        "from datetime import datetime\n\n\ndef at(value):\n"
        "    return datetime.utcfromtimestamp(value)\n"
    )
    found = _violations(tmp_path, source)
    assert len(found) == 1
    assert "datetime.fromtimestamp(value, UTC)" in found[0].why


def test_a_splat_is_not_guessed_at(tmp_path: Path) -> None:
    """`datetime.now(*args)` may well be passing a zone; the syntax cannot say."""
    source = "from datetime import datetime\n\n\ndef stamp(args):\n    return datetime.now(*args)\n"
    assert _scan(tmp_path, source) == []


def test_naive_datetime_is_caught_in_synchronous_code(tmp_path: Path) -> None:
    """Not an event-loop rule: the helper that writes the wrong value is often sync."""
    source = "\n".join(
        [
            "from datetime import datetime",
            "",
            "",
            "class Row:",
            "    def touch(self):",
            "        self.updated = datetime.now()",
            "",
        ]
    )
    assert len(_scan(tmp_path, source)) == 1


def test_naive_datetime_is_waivable(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "from datetime import datetime",
            "",
            "",
            "def local_clock():",
            "    return datetime.now()  # contracts: allow wall clock, for display only",
            "",
        ]
    )
    assert _scan(tmp_path, source) == []


def test_naive_datetime_follows_the_async_safety_switch(tmp_path: Path) -> None:
    """One switch for both rules: turning async safety off turns this off too."""
    source = "from datetime import datetime\n\n\ndef stamp():\n    return datetime.now()\n"
    assert _scan(tmp_path, source, safety=AsyncSafety(enabled=False)) == []


def test_an_unrelated_now_is_not_reported(tmp_path: Path) -> None:
    """`pendulum.now()` resolves elsewhere; only datetime's is the rule."""
    source = "import pendulum\n\n\ndef stamp():\n    return pendulum.now()\n"
    assert _scan(tmp_path, source) == []
