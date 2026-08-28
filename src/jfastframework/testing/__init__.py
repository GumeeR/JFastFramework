"""Test helpers.

A framework nobody can test easily is a framework nobody writes tests for.
These helpers exist so a service's test suite is five lines of setup, not
fifty.

Usage in a service's ``conftest.py``::

    from jfastframework.testing import build_test_app, client_for

    @pytest.fixture
    def app():
        return build_test_app(plugins=["observability"])

Requires ``httpx`` (ships with the ``dev`` extra).
"""

from jfastframework.testing.fixtures import (
    NullPlugin,
    build_test_app,
    client_for,
    make_config,
)

__all__ = ["NullPlugin", "build_test_app", "client_for", "make_config"]
