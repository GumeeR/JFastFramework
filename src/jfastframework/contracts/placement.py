"""Where a shared thing belongs, checked instead of agreed.

Two rules, and they are the same rule seen from both sides.

**A module may not import another module.** Two modules that reach into each
other are one module with a folder between them, and the seam stops being a
seam: you cannot extract either into a service, and a change to one breaks the
other in a way no test covers.

**What two modules both need lives in ``shared/``.** The moment an enum, a
type or a pure function is wanted twice, the copy-paste and the cross-import
are both worse than moving it -- so this reports the import and names the file
it should move to.

The direction is one-way and enforced: modules import ``shared/``, ``shared/``
imports nothing from a module. Without that, ``shared/`` becomes the place
everything ends up and the dependency graph is a circle.

Reported separately from a layer violation because the fix is different. A
layer violation means the call is in the wrong place; this means the *code* is,
and the message says where to put it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from jfastframework.contracts._scan import Violation, python_files, waived
from jfastframework.contracts.model import Contract

RULE = "cross-module"
SHARED_RULE = "shared-direction"

# `modules/<name>/...` -- the layout both generated layouts share.
_MODULE_PATH = re.compile(r"^modules/([^/]+)/")
_SHARED_PATH = re.compile(r"^shared/")


def _module_of(relative: str) -> str | None:
    match = _MODULE_PATH.match(relative)
    return match.group(1) if match else None


def _imported_module(dotted: str) -> str | None:
    """The module name in ``modules.invoice.enums``, if that is what this is."""
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == "modules":
        return parts[1]
    return None


def _suggest_shared_target(dotted: str) -> str:
    """Where the imported thing should live instead."""
    parts = dotted.split(".")
    tail = parts[2] if len(parts) > 2 else "types"
    # An enum is the common case and has an obvious home.
    if "enum" in tail:
        return "shared/enums.py"
    return f"shared/{tail}.py"


def check_placement(contract: Contract, root: Path) -> list[Violation]:
    """Report modules importing each other, and shared/ importing a module."""
    if not contract.enforce_placement:
        return []

    violations: list[Violation] = []

    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        here = _module_of(relative)
        in_shared = bool(_SHARED_PATH.match(relative))
        if here is None and not in_shared:
            continue

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError):
            continue

        lines = source.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:  # relative: inside this module, which is fine
                    continue
                dotted = node.module or ""
            elif isinstance(node, ast.Import):
                dotted = node.names[0].name
            else:
                continue

            other = _imported_module(dotted)
            if other is None:
                continue
            if waived(lines, node.lineno) is not None:
                continue

            if in_shared:
                violations.append(
                    Violation(
                        relative,
                        node.lineno,
                        SHARED_RULE,
                        f"shared/ imports modules.{other}",
                        "the direction is one-way: modules use shared/, never the "
                        "reverse, or the graph becomes a circle",
                    )
                )
            elif other != here:
                target = _suggest_shared_target(dotted)
                violations.append(
                    Violation(
                        relative,
                        node.lineno,
                        RULE,
                        f"module {here!r} imports module {other!r}",
                        f"two modules that need the same thing should share it: "
                        f"move it to {target}",
                    )
                )

    return violations


__all__ = ["RULE", "SHARED_RULE", "check_placement"]
