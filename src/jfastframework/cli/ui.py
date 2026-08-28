"""Terminal presentation for the interactive commands.

Built on ``rich``, which arrives with Typer -- no new dependency for what is,
after all, decoration. That constraint is also why there is no arrow-key
multi-select here: rich does not do raw keyboard input, and adding a library
that does would put a dependency in every install so that one command in one
mode looks nicer. Numbered choices and grouped confirmations get most of the
way there and work over ssh, in CI logs, and in a terminal with no colour.

Everything degrades. ``rich`` detects a non-tty and drops styling on its own,
so the same code produces clean output when piped to a file -- which matters,
because the installer's transcript is something people paste into issues.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# The brand: crimson on black, the same pair the documentation site uses.
ACCENT = "bold #e51a26"
DIM = "grey62"
OK = "bold green"

console = Console()


@dataclass(frozen=True)
class Choice:
    """One option in a select."""

    key: str
    label: str
    hint: str = ""


def banner(subtitle: str = "") -> None:
    """The mark, drawn in the terminal.

    Four lines of block characters rather than an ASCII-art font: it reads as
    the logo at any width, and it does not become a wall of noise in a CI log.
    """
    mark = Text()
    mark.append("  ▟█████▙\n", style=ACCENT)
    mark.append(" ▟█▛▔▔▔▔▔\n", style=ACCENT)
    mark.append("▔▔▔▜█▙\n", style=ACCENT)
    mark.append("  ▚▄▄▟█▛\n", style=ACCENT)

    title = Text()
    title.append("jfast", style=ACCENT)
    title.append("framework\n", style="bold white")
    if subtitle:
        title.append(subtitle, style=DIM)

    console.print()
    console.print(mark)
    console.print(Panel.fit(title, border_style=ACCENT, padding=(0, 2)))


def rule(text: str) -> None:
    console.print()
    console.rule(Text(text, style=ACCENT), style=DIM, align="left")


def _ask(prompt: str) -> str:
    """Read one answer, and keep the transcript readable when piped.

    An interactive answer ends with the user's Enter. Piped input carries no
    such newline, so the next question landed on the same line as the previous
    answer -- which is exactly the transcript somebody pastes into an issue.
    """
    answer = console.input(prompt)
    if not console.is_terminal:
        console.print()
    return answer.strip()


def select(question: str, choices: list[Choice], *, default: str) -> str:
    """Ask for one option, presented as a table and answered by key."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ACCENT, no_wrap=True)
    table.add_column(style="white", no_wrap=True)
    table.add_column(style=DIM)

    for choice in choices:
        marker = "›" if choice.key == default else " "
        # A choice whose label is its key printed the word twice.
        label = "" if choice.label == choice.key else choice.label
        table.add_row(f"  {marker} {choice.key}", label, choice.hint)

    console.print()
    console.print(Text(question, style="bold"))
    console.print(table)

    valid = {c.key for c in choices}
    while True:
        answer = _ask(f"[{DIM}]  choice \[{default}] › [/]") or default
        if answer in valid:
            return answer
        console.print(f"  [{ACCENT}]Pick one of: {', '.join(sorted(valid))}[/]")


def confirm(question: str, *, default: bool = False, hint: str = "") -> bool:
    """A yes/no, with the hint on the same line so the choice is informed."""
    suffix = "Y/n" if default else "y/N"
    line = Text("  ")
    line.append(question, style="white")
    if hint:
        line.append(f"  {hint}", style=DIM)
    console.print(line)
    # Escaped: rich reads square brackets as markup, so an unescaped "[y/N]"
    # is parsed as a style name and swallowed -- the prompt then showed the
    # default for one question and nothing for the rest.
    answer = _ask(f"[{DIM}]      \[{suffix}] › [/]").lower()
    if not answer:
        return default
    return answer in {"y", "yes", "s", "si", "sí"}


def multiselect(question: str, choices: list[Choice], *, defaults: set[str]) -> list[str]:
    """Several options, asked one at a time.

    A checkbox list needs raw keyboard handling, which is the dependency this
    module exists to avoid. Asking in sequence is slower to answer and cannot
    be got wrong -- and it prints a transcript of what was decided, which a
    checkbox list does not.
    """
    console.print()
    console.print(Text(question, style="bold"))
    picked: list[str] = []
    for choice in choices:
        if confirm(
            f"{choice.key:<14}{choice.label}", default=choice.key in defaults, hint=choice.hint
        ):
            picked.append(choice.key)
    return picked


def summary(title: str, rows: list[tuple[str, str]]) -> None:
    """What is about to happen, before it happens."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style=DIM, no_wrap=True)
    table.add_column(style="white")
    for label, value in rows:
        table.add_row(label, value)
    console.print()
    console.print(
        Panel(
            table,
            title=Text(title, style=ACCENT),
            border_style=DIM,
            title_align="left",
            padding=(1, 2),
        )
    )


def created(path: str, note: str = "") -> None:
    line = Text("  created  ", style=OK)
    # Padded to a column, but never at the cost of running the note into the
    # path: a long path just pushes the note along instead of swallowing it.
    line.append(path.ljust(26) if len(path) < 26 else path, style="white")
    if note:
        line.append(f"  {note}", style=DIM)
    console.print(line)


def step(text: str) -> None:
    console.print(Text(f"  {text}", style=DIM))


def next_steps(title: str, commands: list[tuple[str, str]]) -> None:
    """The commands to run now, with what each one does."""
    table = Table.grid(padding=(0, 3))
    table.add_column(style=ACCENT, no_wrap=True)
    table.add_column(style=DIM)
    for command, description in commands:
        table.add_row(command, description)
    console.print()
    console.print(
        Panel(
            table,
            title=Text(title, style="bold white"),
            border_style=ACCENT,
            title_align="left",
            padding=(1, 2),
        )
    )


def warn(text: str) -> None:
    console.print(Text(f"  ! {text}", style="bold yellow"))


def note(text: str) -> None:
    console.print(Text(f"  {text}", style=DIM))


def working(description: str) -> Any:
    """A spinner for the seconds a scaffold takes, as a context manager."""
    return console.status(Text(description, style=DIM), spinner="dots")


def ask(question: str, *, default: str = "") -> str:
    """Free text, with the default shown and used when the answer is empty."""
    shown = f" \[{default}]" if default else ""
    return _ask(f"[{DIM}]  {question}{shown} › [/]") or default


def ask_int(question: str, *, default: int) -> int:
    """A number, re-asked rather than crashed on.

    The first version called int() on whatever came back, so one stray answer
    ended the installer with a traceback after every other question had already
    been answered.
    """
    while True:
        raw = ask(question, default=str(default))
        try:
            return int(raw)
        except ValueError:
            console.print(f"  [{ACCENT}]{raw!r} is not a number.[/]")
