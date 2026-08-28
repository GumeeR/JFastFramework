"""Templates for email, with the two rules that make them safe.

**Autoescape is on.** These render HTML that goes to somebody's inbox, built
from data the application did not write -- a customer name, an invoice line.
The code-generation templates elsewhere in this framework deliberately have it
off, because escaping Python source corrupts it; getting the two confused is
how a name containing an apostrophe becomes a broken email, and how a name
containing a script tag becomes something worse.

**A plain-text part is always produced.** An HTML-only email is spam-filtered
harder, unreadable in a text client, and unreadable by a screen reader that
falls back. If a `.txt` template exists it is used; otherwise a readable
approximation is derived from the HTML rather than left empty.
"""

from __future__ import annotations

import html as htmllib
import re
from pathlib import Path
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(p|div|tr|h[1-6]|li|table)>", re.IGNORECASE)
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n{3,}")


def html_to_text(markup: str) -> str:
    """A readable plain-text approximation of an HTML body.

    Not a full renderer. It keeps the line structure a reader needs -- blocks
    and breaks become newlines -- and drops everything else, which beats both
    an empty text part and a wall of markup.
    """
    text = _BREAK.sub("\n", markup)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = htmllib.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


class TemplateRenderer:
    """Renders ``<name>.html`` and, when present, ``<name>.txt``.

    Requires: ``pip install jfastframework[mail]``
    """

    def __init__(self, directory: str | Path) -> None:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

        self.directory = Path(directory)
        self._env = Environment(
            loader=FileSystemLoader(str(self.directory)),
            # StrictUndefined: a typo in a variable name should fail the render,
            # not silently email somebody "Hola ,".
            undefined=StrictUndefined,
            autoescape=select_autoescape(default_for_string=True, default=True),
            keep_trailing_newline=True,
        )

    def exists(self, name: str) -> bool:
        return (self.directory / f"{name}.html").is_file()

    def render(self, name: str, context: dict[str, Any] | None = None) -> tuple[str, str]:
        """Return ``(html, text)`` for a template name."""
        values = context or {}
        html_body = self._env.get_template(f"{name}.html").render(**values)

        text_path = self.directory / f"{name}.txt"
        if text_path.is_file():
            # Autoescape off for the text part: escaping it would put &amp;
            # into a plain-text email.
            template = self._env.get_template(f"{name}.txt")
            text_body = template.render(**values)
            if "&amp;" in text_body or "&lt;" in text_body:
                text_body = htmllib.unescape(text_body)
        else:
            text_body = html_to_text(html_body)

        return html_body, text_body
