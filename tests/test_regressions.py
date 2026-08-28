"""Six defects that reached a published release, and the checks that keep them out.

Every one of these shipped in 0.1.0a1. None was caught by the existing suite,
because the suite installs the framework from the checkout with every extra
present and never drives a generated service through a real request. These are
the cheapest possible guards for the exact failures.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI
from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jfastframework.db.base import TimestampMixin
from jfastframework.db.repository import BaseRepository
from jfastframework.deploy.compose import render_dockerfile
from jfastframework.plugins.builtin.database import session_dependency
from jfastframework.workspace import WORKSPACE_FILE, Workspace

# -- the session dependency must not become a query parameter ----------


def test_session_dependency_is_not_a_query_parameter() -> None:
    """Annotated `Any`, FastAPI made `request` a required query parameter.

    Every route that took a database session then answered 422 to every call.
    Nothing raised; the schema was simply wrong.
    """
    app = FastAPI()

    @app.get("/items")
    async def list_items(session: object = Depends(session_dependency)) -> dict[str, str]:
        return {"ok": "yes"}

    parameters = app.openapi()["paths"]["/items"]["get"].get("parameters", [])
    names = [p["name"] for p in parameters]
    assert "request" not in names, f"`request` leaked into the schema as {parameters}"
    assert parameters == [], f"the dependency contributed parameters: {parameters}"


# -- the Dockerfile must build what the generator writes ---------------


def test_dockerfile_copies_are_optional() -> None:
    """`COPY pyproject.toml ./` failed: the generator only writes requirements."""
    dockerfile = render_dockerfile()
    assert "COPY pyproject.toml* ./" in dockerfile
    assert "COPY pyproject.toml ./" not in dockerfile


def test_the_image_migrates_before_it_serves() -> None:
    """A container against an empty database answers 500 to everything."""
    dockerfile = render_dockerfile()
    assert "alembic upgrade head" in dockerfile
    assert "set -e" in dockerfile, "a failed migration must stop the container"
    assert "exec uvicorn" in dockerfile, "uvicorn must be PID 1 to receive SIGTERM"


# -- writes must not expire a column into a lazy load ------------------


class Base(DeclarativeBase):
    pass


class Thing(TimestampMixin, Base):
    __tablename__ = "regression_things"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(default="")
    stamped: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ThingRepository(BaseRepository[Thing]):
    model = Thing
    tenant_scoped = False


async def test_a_server_default_is_readable_after_update() -> None:
    """`onupdate` expired `updated_at`; reading it raised MissingGreenlet.

    The read is normally Pydantic serialising the response, so every PATCH
    returned 500 while the write itself had succeeded.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with maker() as session:
            repository = ThingRepository(session)
            thing = await repository.create(label="before")
            assert thing.updated_at is not None

            await repository.update(thing, label="after")
            # This attribute access is the whole test.
            assert thing.updated_at is not None
            assert thing.stamped is not None
    finally:
        await engine.dispose()


def test_the_mixin_asks_for_eager_defaults() -> None:
    assert TimestampMixin.__mapper_args__.get("eager_defaults") is True


# -- the workspace search must not leave the project -------------------


def test_the_search_stops_at_a_repository_root(tmp_path: Path) -> None:
    """A workspace above a `.git` belongs to a different project."""
    outer = tmp_path / "outer"
    project = outer / "project"
    nested = project / "service"
    nested.mkdir(parents=True)
    (outer / WORKSPACE_FILE).write_text("[workspace]\nname='outer'\n", encoding="utf-8")
    (project / ".git").mkdir()

    assert Workspace.find(nested) is None


def test_a_workspace_inside_the_project_is_still_found(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "service" / "modules"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    marker = project / WORKSPACE_FILE
    marker.write_text("[workspace]\nname='p'\n", encoding="utf-8")

    assert Workspace.find(nested) == marker


def test_the_home_directory_is_never_a_workspace(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Running `jfast start` once in $HOME used to enrol every project below it."""
    home = tmp_path / "home"
    work = home / "somewhere" / "deep"
    work.mkdir(parents=True)
    (home / WORKSPACE_FILE).write_text("[workspace]\nname='accident'\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert Workspace.find(work) is None
