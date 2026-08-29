"""``python -m jfastframework``.

The ``jfast`` console script is the normal way in, and it is installed by the
entry point in ``pyproject.toml``. This exists for when that script is not
reachable: a Windows install where ``Scripts/`` is not on PATH or the freshly
written ``.exe`` is held by an antivirus scanner, a container that installed
into a virtualenv nobody activated, a CI step that would rather not guess where
pip put the binary.

Without this file ``python -m jfastframework`` fails outright, and the only
module path that works -- ``python -m jfastframework.cli.main`` -- prints a
``RuntimeWarning`` on every invocation, because ``cli/__init__.py`` has already
imported ``main`` into ``sys.modules`` before runpy executes it. So the choice
today is between a path that is broken and one that is noisy. This is the third
option.
"""

from __future__ import annotations

from jfastframework.cli.main import app

if __name__ == "__main__":
    app()
