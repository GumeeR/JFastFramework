"""Identifiers that get interpolated into SQL, checked once at the boundary.

Table and column names cannot be bound as parameters -- no database accepts
``SELECT 1 FROM :table`` -- so anywhere a schema object is configurable, its
name ends up inside an f-string. That is the shape of an injection even when
the value comes from a settings file rather than a request, and "it comes from
config" is an argument that stops being true the first time config is built
from something else.

So the name is validated where it enters, once, and the interpolation that
follows is then provably safe rather than merely likely to be.
"""

from __future__ import annotations

import re

# Unquoted SQL identifiers: a letter or underscore, then letters, digits or
# underscores. Deliberately narrower than what PostgreSQL accepts when quoted --
# a table this framework creates has no business being called "my table".
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

MAX_IDENTIFIER_LENGTH = 63  # PostgreSQL truncates past this, silently.


def safe_identifier(name: str, *, kind: str = "identifier") -> str:
    """Return ``name`` if it can be interpolated into SQL, or raise.

    Raises:
        ValueError: the name is empty, too long, or contains anything that
            would change the meaning of the statement it lands in.
    """
    if not name:
        raise ValueError(f"{kind} cannot be empty")
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"{kind} {name!r} is longer than {MAX_IDENTIFIER_LENGTH} characters, "
            f"which PostgreSQL truncates without telling you"
        )
    if not _IDENTIFIER.match(name):
        raise ValueError(
            f"{kind} {name!r} is not a plain SQL identifier. "
            f"Use letters, digits and underscores, starting with a letter."
        )
    return name
