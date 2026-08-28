"""Server-rendered surface: partial rendering and HTMX-aware errors."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, Request, Response

from jfastframework.errors import NotFoundError
from jfastframework.plugins.builtin.web import is_htmx
from jfastframework.testing import build_test_app, client_for


@pytest.fixture
def site(tmp_path: Path) -> Path:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "page.html").write_text(
        "<html><body><h1>{{ title }}</h1>{% include 'rows.html' %}</body></html>",
        encoding="utf-8",
    )
    (templates / "rows.html").write_text("<ul><li>{{ title }}</li></ul>", encoding="utf-8")
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.css").write_text("body{color:red}", encoding="utf-8")
    return tmp_path


def build(site: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/page", response_class=Response)
    async def page(request: Request) -> Response:
        render = request.app.state.jfast.require("render")
        return render(request, "page.html", {"title": "Hello"}, partial="rows.html")

    @router.get("/boom", response_class=Response)
    async def boom(request: Request) -> Response:
        raise NotFoundError("widget 7 not found")

    return router


def app_for(site: Path):
    return build_test_app(
        plugins=["web"],
        routers=[build(site)],
        raw={
            "plugin": {
                "web": {
                    "templates_dir": str(site / "templates"),
                    "static_dir": str(site / "static"),
                }
            }
        },
    )


async def test_a_browser_navigation_gets_the_whole_page(site: Path) -> None:
    async with client_for(app_for(site)) as client:
        response = await client.get("/page")
    assert response.status_code == 200
    assert "<html>" in response.text
    assert "<li>Hello</li>" in response.text


async def test_an_htmx_request_gets_only_the_fragment(site: Path) -> None:
    async with client_for(app_for(site)) as client:
        response = await client.get("/page", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html>" not in response.text
    assert response.text.strip() == "<ul><li>Hello</li></ul>"


async def test_static_files_are_served(site: Path) -> None:
    async with client_for(app_for(site)) as client:
        response = await client.get("/static/app.css")
    assert response.status_code == 200
    assert "color:red" in response.text


async def test_errors_stay_problem_json_for_normal_requests(site: Path) -> None:
    async with client_for(app_for(site)) as client:
        response = await client.get("/boom")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == "widget 7 not found"


async def test_errors_become_html_for_htmx_requests(site: Path) -> None:
    # HTMX swaps the response body into the DOM; JSON would render as text.
    async with client_for(app_for(site)) as client:
        response = await client.get("/boom", headers={"HX-Request": "true"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "widget 7 not found" in response.text
    assert 'class="jfast-error"' in response.text


def test_is_htmx_only_matches_the_real_header() -> None:
    class FakeRequest:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    assert is_htmx(FakeRequest({"HX-Request": "true"}))  # type: ignore[arg-type]
    assert not is_htmx(FakeRequest({}))  # type: ignore[arg-type]
    assert not is_htmx(FakeRequest({"HX-Request": "false"}))  # type: ignore[arg-type]
