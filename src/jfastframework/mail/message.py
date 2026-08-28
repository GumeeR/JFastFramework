"""What a message is, before any backend touches it.

A plain dataclass rather than an ``email.message.EmailMessage``: this has to
survive a round trip through a queue as JSON, and the stdlib object does not.
The conversion to MIME happens in the SMTP backend, at the last possible
moment.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

# Anything larger and most servers reject the message anyway, after you have
# already paid to encode and upload it.
DEFAULT_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class MailError(Exception):
    """A message that cannot be sent as written."""


@dataclass
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"

    def to_json(self) -> dict[str, Any]:
        # base64 because a queue payload is JSON and bytes are not.
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "content_b64": base64.b64encode(self.content).decode("ascii"),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Attachment:
        return cls(
            filename=raw["filename"],
            content=base64.b64decode(raw["content_b64"]),
            content_type=raw.get("content_type", "application/octet-stream"),
        )


@dataclass
class EmailMessage:
    """One message. Validated on construction, not at send time.

    Validating here means a malformed message fails in the request that built
    it, with a stack trace pointing at the mistake -- rather than three minutes
    later inside a worker, in a job whose payload nobody can read.
    """

    to: list[str]
    subject: str
    text: str = ""
    html: str = ""
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: str = ""
    # Left empty, the backend fills it from configuration.
    from_email: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.to, str):  # a very easy mistake, and silent
            self.to = [self.to]
        if not self.to:
            raise MailError("An email needs at least one recipient.")
        for address in [*self.to, *self.cc, *self.bcc]:
            if "@" not in address:
                raise MailError(f"{address!r} is not an email address.")
        if not self.subject:
            raise MailError("An email needs a subject.")
        if not self.text and not self.html:
            raise MailError("An email needs a text or an html body.")

    @property
    def recipients(self) -> list[str]:
        """Everyone the envelope goes to, bcc included."""
        return [*self.to, *self.cc, *self.bcc]

    def total_attachment_bytes(self) -> int:
        return sum(len(a.content) for a in self.attachments)

    def to_json(self) -> dict[str, Any]:
        return {
            "to": self.to,
            "subject": self.subject,
            "text": self.text,
            "html": self.html,
            "cc": self.cc,
            "bcc": self.bcc,
            "reply_to": self.reply_to,
            "from_email": self.from_email,
            "headers": self.headers,
            "attachments": [a.to_json() for a in self.attachments],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> EmailMessage:
        return cls(
            to=list(raw["to"]),
            subject=raw["subject"],
            text=raw.get("text", ""),
            html=raw.get("html", ""),
            cc=list(raw.get("cc", [])),
            bcc=list(raw.get("bcc", [])),
            reply_to=raw.get("reply_to", ""),
            from_email=raw.get("from_email", ""),
            headers=dict(raw.get("headers", {})),
            attachments=[Attachment.from_json(a) for a in raw.get("attachments", [])],
        )
