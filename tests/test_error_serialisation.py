"""An error response must not raise while being written.

pydantic puts the offending value in the validation error's ``input`` field,
and that value is whatever arrived. Post a form without a content type and the
raw body lands there as ``bytes``, which the JSON encoder cannot represent --
so serialising the 422 raised *inside the handler*, and the caller got a 500
with a traceback about ``json.dumps`` instead of the field name they needed.

The failure is worse than a bad message: the status is wrong too, so a client
retrying on 5xx retries a request that will never succeed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Form
from fastapi.testclient import TestClient
from pydantic import BaseModel

from jfastframework.errors import _serialisable, install_error_handlers


class Payload(BaseModel):
    name: str
    count: int


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/json")
    async def take_json(payload: Payload) -> dict[str, str]:
        return {"ok": payload.name}

    @app.post("/form")
    async def take_form(name: str = Form(...)) -> dict[str, str]:
        return {"ok": name}

    return TestClient(app)


def test_a_body_that_is_not_json_still_gets_a_422(client: TestClient) -> None:
    """The exact shape that used to 500: raw bytes in the error's input."""
    response = client.post(
        "/json",
        content=b"name=first&count=2",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == 422
    assert body["errors"], "the error detail was dropped rather than sanitised"


def test_undecodable_bytes_do_not_break_the_response(client: TestClient) -> None:
    response = client.post(
        "/json",
        content=b"\xff\xfe\x00binary",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
    assert response.json()["errors"]


def test_a_normal_validation_error_still_names_the_field(client: TestClient) -> None:
    """Sanitising must not cost the diagnosis."""
    response = client.post("/json", json={"name": "first", "count": "not a number"})
    assert response.status_code == 422
    locations = [error["loc"] for error in response.json()["errors"]]
    assert any("count" in location for location in locations), locations


def test_a_missing_form_field_is_reported(client: TestClient) -> None:
    response = client.post("/form", data={})
    assert response.status_code == 422
    assert response.json()["errors"]


# -- the sanitiser itself -----------------------------------------------


def test_text_bytes_come_back_as_text() -> None:
    assert _serialisable(b"name=first") == "name=first"


def test_binary_becomes_a_marker_not_a_wall_of_latin1() -> None:
    """A megabyte upload should not end up in a log line."""
    assert _serialisable(b"\xff\xfe\x00") == "<3 bytes>"


def test_long_text_is_truncated() -> None:
    result = _serialisable(b"x" * 900)
    assert isinstance(result, str)
    assert len(result) == 515 and result.endswith("...")


def test_nested_structures_are_walked() -> None:
    value = {"errors": [{"input": b"raw", "loc": ("body", "name")}]}
    assert _serialisable(value) == {"errors": [{"input": "raw", "loc": ["body", "name"]}]}


def test_scalars_pass_through_unchanged() -> None:
    assert _serialisable({"a": 1, "b": 1.5, "c": True, "d": None, "e": "s"}) == {
        "a": 1,
        "b": 1.5,
        "c": True,
        "d": None,
        "e": "s",
    }


def test_an_arbitrary_object_becomes_its_repr() -> None:
    class Thing:
        def __repr__(self) -> str:
            return "<Thing>"

    assert _serialisable(Thing()) == "<Thing>"
