"""Error model shared by every JFast service.

All failures serialise to RFC 7807 ``application/problem+json`` so clients and
sibling services parse one shape, not one shape per team.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"


class JFastError(Exception):
    """Base class for framework and domain errors."""

    status_code: int = 500
    title: str = "Internal Server Error"
    type_: str = "about:blank"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": self.type_,
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
        }
        if instance:
            problem["instance"] = instance
        problem.update(self.extra)
        return problem


class NotFoundError(JFastError):
    status_code = 404
    title = "Not Found"


class ConflictError(JFastError):
    status_code = 409
    title = "Conflict"


class ValidationError(JFastError):
    status_code = 422
    title = "Unprocessable Entity"


class UnauthorizedError(JFastError):
    status_code = 401
    title = "Unauthorized"


class ForbiddenError(JFastError):
    status_code = 403
    title = "Forbidden"


class ServiceUnavailableError(JFastError):
    status_code = 503
    title = "Service Unavailable"


class PluginError(JFastError):
    """Configuration-time failure in the plugin graph."""

    title = "Plugin Error"


def _problem_response(problem: dict[str, Any], request: Request) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        problem.setdefault("request_id", request_id)
    return JSONResponse(
        status_code=int(problem["status"]),
        content=problem,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def problem_response(exc: JFastError, request: Request) -> JSONResponse:
    """Render an error as problem+json, without an exception handler.

    Middleware runs *outside* the exception handlers, so an error raised there
    would escape as a 500 with a stack trace instead of the documented shape.
    Anything raising from middleware returns this instead.
    """
    return _problem_response(exc.to_problem(instance=str(request.url.path)), request)


def install_error_handlers(app: FastAPI, *, debug: bool = False) -> None:
    """Register the problem+json handlers on an app."""

    @app.exception_handler(JFastError)
    async def _jfast_error(request: Request, exc: JFastError) -> JSONResponse:
        return _problem_response(exc.to_problem(instance=str(request.url.path)), request)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        problem = {
            "type": "about:blank",
            "title": exc.detail if isinstance(exc.detail, str) else "HTTP Error",
            "status": exc.status_code,
            "detail": exc.detail,
            "instance": str(request.url.path),
        }
        return _problem_response(problem, request)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problem = {
            "type": "about:blank",
            "title": "Unprocessable Entity",
            "status": 422,
            "detail": "Request validation failed",
            "instance": str(request.url.path),
            "errors": exc.errors(),
        }
        return _problem_response(problem, request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        problem = {
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            # Never leak internals in production; debug builds get the message.
            "detail": str(exc) if debug else "An unexpected error occurred",
            "instance": str(request.url.path),
        }
        return _problem_response(problem, request)
