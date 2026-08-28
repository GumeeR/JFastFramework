"""Server-rendered pages with Jinja2 and HTMX.

This is the Laravel-shaped half of the framework: a service that returns HTML
instead of JSON, with no JavaScript build step. Templates in ``templates/``,
assets in ``static/``, and HTMX for interactivity.

The one idea worth understanding is **partial rendering**. HTMX sends
``HX-Request: true`` and expects a fragment, not a whole page. ``render()``
handles both from a single handler::

    @router.get("/orders")
    async def index(request: Request):
        render = request.app.state.jfast.require("render")
        return render(request, "orders/index.html", {"orders": orders},
                      partial="orders/_rows.html")

A browser navigation gets the full page. An ``hx-get`` gets just the rows.
Same handler, same context, no duplication.

Errors are handled the same way: a ``JFastError`` raised during an HTMX request
returns an HTML fragment rather than ``problem+json``, because HTMX would swap
the JSON into the DOM as text.

Requires: ``pip install jfastframework[web]``
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from jfastframework.errors import JFastError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext

HTMX_HEADER = "HX-Request"


def is_htmx(request: Request) -> bool:
    """True when HTMX issued this request and expects a fragment."""
    return request.headers.get(HTMX_HEADER, "").lower() == "true"


class WebSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_WEB_", env_file=".env", extra="ignore")

    templates_dir: str = "templates"
    static_dir: str = "static"
    static_url: str = "/static"
    # Serve HTMX from the CDN. Set false and vendor it into static/ when the
    # deployment must not reach out to third-party hosts.
    htmx_cdn: bool = True
    htmx_version: str = "2.0.4"
    # Reload templates on every render. Development only -- it stats the file
    # system per request.
    auto_reload: bool = False


class Renderer:
    """Callable that renders a template, or its partial for HTMX requests."""

    def __init__(self, templates: Any, settings: WebSettings, app_name: str) -> None:
        self._templates = templates
        self._settings = settings
        self._app_name = app_name

    def __call__(
        self,
        request: Request,
        template: str,
        context: dict[str, Any] | None = None,
        *,
        partial: str | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response:
        chosen = partial if (partial and is_htmx(request)) else template
        payload: dict[str, Any] = {
            "request": request,
            "app_name": self._app_name,
            "static_url": self._settings.static_url,
            "is_htmx": is_htmx(request),
            **(context or {}),
        }
        response: Response = self._templates.TemplateResponse(
            request=request,
            name=chosen,
            context=payload,
            status_code=status_code,
            headers=headers,
        )
        return response


class WebPlugin(Plugin):
    meta = PluginMeta(
        name="web",
        version="0.1.0",
        description="Jinja2 templates, static files and HTMX partial rendering.",
        after=("observability",),
        provides=("templates", "render"),
        default_enabled=False,
        extra="jfastframework[web]",
    )
    Settings = WebSettings

    def register(self, ctx: AppContext) -> None:
        from fastapi.staticfiles import StaticFiles
        from fastapi.templating import Jinja2Templates

        settings: WebSettings = self.settings
        templates_path = Path(settings.templates_dir)
        if not templates_path.is_dir():
            ctx.logger.warning(
                "web plugin: templates directory %s does not exist; "
                "create it or set [plugin.web] templates_dir",
                templates_path,
            )
            templates_path.mkdir(parents=True, exist_ok=True)

        templates = Jinja2Templates(directory=str(templates_path))
        templates.env.auto_reload = settings.auto_reload
        templates.env.globals["app_name"] = ctx.settings.app_name
        templates.env.globals["static_url"] = settings.static_url
        templates.env.globals["htmx_src"] = (
            f"https://unpkg.com/htmx.org@{settings.htmx_version}"
            if settings.htmx_cdn
            else f"{settings.static_url}/htmx.min.js"
        )

        renderer = Renderer(templates, settings, ctx.settings.app_name)
        ctx.provide("templates", templates)
        ctx.provide("render", renderer)

        static_path = Path(settings.static_dir)
        if static_path.is_dir():
            ctx.app.mount(
                settings.static_url,
                StaticFiles(directory=str(static_path)),
                name="static",
            )
        else:
            ctx.logger.info("web plugin: no %s directory, static files not mounted", static_path)

        self._install_html_error_handler(ctx)

    def _install_html_error_handler(self, ctx: AppContext) -> None:
        """Return HTML for HTMX requests, problem+json for everything else.

        Registered after the kernel's handlers, so this one wins. It delegates
        back to problem+json whenever the caller is not HTMX, which keeps a
        mixed API/web service honest on both surfaces.
        """
        from starlette.responses import JSONResponse

        from jfastframework.errors import PROBLEM_CONTENT_TYPE

        @ctx.app.exception_handler(JFastError)
        async def _html_or_problem(request: Request, exc: JFastError) -> Response:
            problem = exc.to_problem(instance=str(request.url.path))
            request_id = getattr(request.state, "request_id", None)
            if request_id:
                problem.setdefault("request_id", request_id)

            if not is_htmx(request):
                return JSONResponse(
                    status_code=exc.status_code,
                    content=problem,
                    media_type=PROBLEM_CONTENT_TYPE,
                )

            # Deliberately inline rather than a template: an error path that
            # depends on template lookup fails twice when templates are broken.
            return HTMLResponse(
                status_code=exc.status_code,
                content=(
                    f'<div class="jfast-error" role="alert" data-status="{exc.status_code}">'
                    f"<strong>{exc.title}</strong> {exc.detail}"
                    f"</div>"
                ),
            )

    async def health(self, ctx: AppContext) -> HealthReport:
        settings: WebSettings = self.settings
        templates_ok = Path(settings.templates_dir).is_dir()
        if not templates_ok:
            return HealthReport.fail(
                f"templates directory {settings.templates_dir} missing", critical=False
            )
        return HealthReport.ok(
            "templates loaded",
            templates_dir=settings.templates_dir,
            static_mounted=Path(settings.static_dir).is_dir(),
        )
