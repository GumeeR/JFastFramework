"""Verify code against its declared contract.

Static, AST-based, and deliberately **conservative**: it only reports what it
can prove from the syntax. A checker that cries wolf gets an ignore file within
a week, and then the contract is decoration again.

Three consequences of that choice, all intentional:

* Files that match no layer are not layer-checked. You opt a path *in* by
  giving it to a layer; nothing is guessed.
* Only static imports and direct calls are inspected. ``importlib`` and
  ``getattr`` chains are out of scope — a contract is a design guardrail, not
  a sandbox.
* Every violation can be waived inline with ``# contracts: allow <reason>``.
  The reason is required, so a waiver is a decision someone can review rather
  than a silent bypass.
"""

from __future__ import annotations

import ast
from fnmatch import fnmatch
from pathlib import Path

from jfastframework.contracts._scan import (
    SKIP_DIRS,
    WAIVER,
    Violation,
    call_name,
    python_files,
    waived,
)
from jfastframework.contracts.blocking import check_blocking
from jfastframework.contracts.model import Contract, Layer

__all__ = ["SKIP_DIRS", "WAIVER", "Violation", "check", "waivers"]


def _resolve_relative(module: str | None, level: int, current: Path, root: Path) -> str | None:
    """Turn a relative import into a repo-relative module path.

    ``from .storage import X`` inside ``modules/order/http.py`` resolves to
    ``modules/order/storage``.
    """
    if level == 0:
        return module
    base = current.parent
    for _ in range(level - 1):
        base = base.parent
    try:
        prefix = base.relative_to(root).as_posix().replace("/", ".")
    except ValueError:
        return None
    return f"{prefix}.{module}" if module else prefix


def _module_to_paths(dotted: str) -> list[str]:
    """Candidate file paths a dotted module could live in."""
    stem = dotted.replace(".", "/")
    return [f"{stem}.py", f"{stem}/__init__.py"]


def _layer_of_module(contract: Contract, dotted: str) -> Layer | None:
    for candidate in _module_to_paths(dotted):
        layer = contract.layer_for(candidate)
        if layer is not None:
            return layer
    return None


def _top_package(name: str) -> str:
    return name.split(".", 1)[0]


def check_imports(contract: Contract, root: Path) -> list[Violation]:
    violations: list[Violation] = []

    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        layer = contract.layer_for(relative)

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError):
            # A file that does not parse is ruff's problem, not the
            # contract's. Reporting it twice helps nobody.
            continue
        lines = source.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [(alias.name, node.lineno) for alias in node.names]
                dotted_targets = [(name, line) for name, line in names]
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_relative(node.module, node.level, path, root)
                dotted_targets = [(resolved, node.lineno)] if resolved else []
            else:
                continue

            for dotted, line in dotted_targets:
                if dotted is None:
                    continue
                reason = waived(lines, line)

                # 1. Layer boundaries.
                if layer is not None:
                    target_layer = _layer_of_module(contract, dotted)
                    if (
                        target_layer is not None
                        and target_layer.name != layer.name
                        and target_layer.name not in layer.may_import
                    ):
                        allowed = ", ".join(layer.may_import) or "nothing"
                        violation = Violation(
                            relative,
                            line,
                            "layer",
                            f"{layer.name!r} imports {target_layer.name!r} "
                            f"({dotted}); it may import: {allowed}",
                            layer.description,
                        )
                        if reason is None:
                            violations.append(violation)

                    # 2. Packages this layer must not touch at all.
                    package = _top_package(dotted)
                    if package in layer.forbid_packages and reason is None:
                        violations.append(
                            Violation(
                                relative,
                                line,
                                "layer-package",
                                f"{layer.name!r} must not import {package!r}",
                                layer.description,
                            )
                        )

                # 3. Explicit forbid_import rules.
                for rule in contract.forbid_imports:
                    if rule.in_paths and not any(fnmatch(relative, p) for p in rule.in_paths):
                        continue
                    if _top_package(dotted) in rule.packages and reason is None:
                        violations.append(
                            Violation(
                                relative,
                                line,
                                "forbid-import",
                                f"{dotted} is not allowed here",
                                rule.why,
                            )
                        )

    return violations


def check_calls(contract: Contract, root: Path) -> list[Violation]:
    if not contract.forbid_calls:
        return []

    violations: list[Violation] = []
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError):
            continue
        lines = source.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            if name is None:
                continue

            for rule in contract.forbid_calls:
                if any(fnmatch(relative, pattern) for pattern in rule.except_in):
                    continue
                if not _call_matches(name, rule.pattern):
                    continue
                if waived(lines, node.lineno) is None:
                    violations.append(
                        Violation(
                            relative,
                            node.lineno,
                            "forbid-call",
                            f"{name}() is not allowed here",
                            rule.why,
                        )
                    )
                break
    return violations


def _call_matches(name: str, pattern: str) -> bool:
    """Does this call site match a forbidden pattern?

    A dotted pattern also matches the bare name, because
    ``from os import getenv`` is the same mistake as ``os.getenv`` and the
    contract should not have to list both. A bare pattern matches only
    exactly, so forbidding ``print`` does not also flag ``report.print``.
    """
    if name == pattern:
        return True
    if "." not in pattern:
        return False
    return name.endswith("." + pattern) or name == pattern.rsplit(".", 1)[1]


def check_requirements(contract: Contract, root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for requirement in contract.requirements:
        for module_dir in sorted(root.glob(requirement.applies_to)):
            if not module_dir.is_dir() or module_dir.name.startswith((".", "_")):
                continue
            if not (module_dir / requirement.path).exists():
                relative = module_dir.relative_to(root).as_posix()
                violations.append(
                    Violation(
                        f"{relative}/",
                        0,
                        "missing",
                        f"{requirement.path} is required but absent",
                        requirement.why,
                    )
                )
    return violations


def check_contract(contract: Contract) -> list[Violation]:
    """Check the contract itself before checking the code against it.

    Two authoring mistakes produce confident, wrong results rather than
    errors, so they are caught here:

    * two layers claiming the same path — whichever wins the tie decides the
      rules, and the answer is silently arbitrary;
    * ``may_import`` naming a layer that does not exist — the permission has
      no effect and the import is rejected for a reason nobody can find.
    """
    violations: list[Violation] = []
    source = contract.source.name if contract.source else CONTRACTS_FILE_FALLBACK

    claims: dict[str, list[str]] = {}
    for layer in contract.layers.values():
        for pattern in layer.paths:
            claims.setdefault(pattern, []).append(layer.name)

    for pattern, owners in sorted(claims.items()):
        if len(owners) > 1:
            violations.append(
                Violation(
                    source,
                    0,
                    "contract",
                    f"path {pattern!r} is claimed by {' and '.join(sorted(owners))}; "
                    f"one of them must give it up",
                )
            )

    for layer in contract.layers.values():
        for target in layer.may_import:
            if target not in contract.layers:
                known = ", ".join(sorted(contract.layers)) or "none"
                violations.append(
                    Violation(
                        source,
                        0,
                        "contract",
                        f"layer {layer.name!r} may_import {target!r}, which is not a layer. "
                        f"Known layers: {known}",
                    )
                )
    return violations


CONTRACTS_FILE_FALLBACK = "contracts.toml"


def check(contract: Contract, root: Path) -> list[Violation]:
    """Every check, in a stable order.

    The contract is validated first: if it contradicts itself, the findings
    below are answers to the wrong question.
    """
    own = check_contract(contract)
    if own:
        return own

    violations = [
        *check_imports(contract, root),
        *check_calls(contract, root),
        *check_requirements(contract, root),
        *check_blocking(contract, root),
    ]
    return sorted(violations, key=lambda v: (v.path, v.line, v.rule))


def waivers(root: Path) -> list[Violation]:
    """Every inline waiver, so they can be reviewed rather than accumulate.

    A waiver is a decision. Decisions that nobody ever looks at again turn
    into the reason a contract stopped meaning anything.
    """
    found: list[Violation] = []
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for number, text in enumerate(lines, start=1):
            if WAIVER in text:
                reason = text.split(WAIVER, 1)[1].strip(" #\t").strip()
                found.append(Violation(relative, number, "waiver", reason or "(no reason given)"))
    return found
