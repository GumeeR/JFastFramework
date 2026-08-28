"""Per-project contracts: declared by you, enforced by a command, read by agents.

jfast contracts check          # fails the build on a violation
jfast contracts show --json    # what an agent reads before writing code
jfast contracts render         # CONTRACTS.md, for review
"""

from jfastframework.contracts.blocking import check_blocking
from jfastframework.contracts.checker import Violation, check, waivers
from jfastframework.contracts.model import (
    CONTRACTS_FILE,
    AsyncSafety,
    Contract,
    ForbiddenCall,
    ForbiddenImport,
    Interface,
    Layer,
    Requirement,
)
from jfastframework.contracts.placement import check_placement
from jfastframework.contracts.render import render

__all__ = [
    "CONTRACTS_FILE",
    "AsyncSafety",
    "Contract",
    "ForbiddenCall",
    "ForbiddenImport",
    "Interface",
    "Layer",
    "Requirement",
    "Violation",
    "check",
    "check_blocking",
    "check_placement",
    "render",
    "waivers",
]
