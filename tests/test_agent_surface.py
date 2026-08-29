"""The files an AI agent reads before it writes, and the promise they make.

Three generated stylesheets told the reader to consult
``.jfast/skills/design-system/SKILL.md``. No generated project contained that
file. A pointer to nothing is worse than no pointer: it costs a reader the time
to go looking, and it teaches an agent that this project's instructions are
unreliable.

So the rule under test is not "the skill exists" but "the reference and the
file agree" -- in both directions, because the surface is opt-in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jfastframework.cli.scaffold import Scaffolder, service_context, service_trees

SKILL_REFERENCE = re.compile(r"\.jfast/skills/([\w-]+)/SKILL\.md")


def _generate(tmp_path: Path, *, kind: str, frontend: str | None, agent_docs: bool) -> Path:
    target = tmp_path / "svc"
    context = service_context("svc", kind=kind, frontend=frontend, agent_docs=agent_docs)
    Scaffolder().render_trees(service_trees(kind, frontend, target, agent_docs=agent_docs), context)
    return target


def _dangling(root: Path) -> list[str]:
    """Every reference to a skill file that is not there."""
    missing = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for skill in SKILL_REFERENCE.findall(body):
            if not (root / ".jfast" / "skills" / skill / "SKILL.md").is_file():
                missing.append(f"{path.relative_to(root)} -> {skill}")
    return missing


@pytest.mark.parametrize(
    ("kind", "frontend"),
    [("api", None), ("web", None), ("spa", "vue"), ("spa", "react")],
)
@pytest.mark.parametrize("agent_docs", [True, False])
def test_no_generated_file_points_at_a_skill_that_is_not_there(
    tmp_path: Path, kind: str, frontend: str | None, agent_docs: bool
) -> None:
    root = _generate(tmp_path, kind=kind, frontend=frontend, agent_docs=agent_docs)
    assert _dangling(root) == []


def test_the_surface_is_written_when_asked_for(tmp_path: Path) -> None:
    root = _generate(tmp_path, kind="api", frontend=None, agent_docs=True)
    assert (root / "AGENTS.md").is_file()
    assert (root / ".jfast/skills/respect-contracts/SKILL.md").is_file()


def test_a_frontend_also_gets_the_design_skill(tmp_path: Path) -> None:
    """The stylesheet's pointer is only true if this tree comes with it."""
    root = _generate(tmp_path, kind="spa", frontend="vue", agent_docs=True)
    assert (root / ".jfast/skills/design-system/SKILL.md").is_file()


def test_nothing_is_written_when_it_is_not_asked_for(tmp_path: Path) -> None:
    """Off by default: a project nobody points an agent at owes no agent files."""
    root = _generate(tmp_path, kind="api", frontend=None, agent_docs=False)
    assert not (root / "AGENTS.md").exists()
    assert not (root / ".jfast" / "skills").exists()


@pytest.mark.parametrize("skill", ["respect-contracts", "design-system"])
def test_every_skill_declares_when_not_to_use_it(tmp_path: Path, skill: str) -> None:
    """A skill that never says when to skip it gets loaded for everything."""
    root = _generate(tmp_path, kind="spa", frontend="vue", agent_docs=True)
    if skill == "respect-contracts":
        root = _generate(tmp_path / "api", kind="api", frontend=None, agent_docs=True)
    body = (root / ".jfast/skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    for field in ("name:", "description:", "when_to_use:", "when_not_to_use:"):
        assert field in body, f"{skill} is missing {field}"


def test_the_templates_ship_in_the_package() -> None:
    """A dot-directory is exactly the kind of thing a build backend drops.

    If ``.jfast`` is missing from an installed copy, ``--agent-docs`` fails only
    for people who installed from PyPI -- never in this repo, where the files
    are on disk either way.
    """
    import jfastframework

    templates = Path(jfastframework.__file__).parent / "templates"
    assert (templates / "agent_docs/.jfast/skills/respect-contracts/SKILL.md.j2").is_file()
    assert (templates / "agent_design/.jfast/skills/design-system/SKILL.md.j2").is_file()
