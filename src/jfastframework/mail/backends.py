"""Where a message actually goes.

One protocol, three implementations. The SMTP conversation is synchronous --
``smtplib`` is a blocking library and there is no standard async replacement --
so it runs in a worker thread. That is deliberate and it is the honest shape:
pretending otherwise would stall the event loop for the length of a network
round trip to somebody else's mail server.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage as MIMEMessage
from typing import Any, Protocol, runtime_checkable

from jfastframework.mail.message import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    EmailMessage,
    MailError,
)


@runtime_checkable
class MailBackend(Protocol):
    async def send(self, message: EmailMessage) -> None: ...

    async def health(self) -> tuple[bool, str]: ...


def build_mime(message: EmailMessage, *, default_from: str) -> MIMEMessage:
    """The message as MIME, built at the last possible moment."""
    mime = MIMEMessage()
    mime["From"] = message.from_email or default_from
    mime["To"] = ", ".join(message.to)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    mime["Subject"] = message.subject
    for key, value in message.headers.items():
        mime[key] = value

    # Bcc is deliberately never a header: it goes in the envelope only, which
    # is the whole point of it.
    mime.set_content(message.text or " ")
    if message.html:
        mime.add_alternative(message.html, subtype="html")

    for attachment in message.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        mime.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return mime


class ConsoleMailer:
    """Prints the message. The default outside production.

    Nobody sends real mail from a laptop by accident, and developing needs no
    credentials at all.
    """

    def __init__(self, *, default_from: str = "noreply@localhost") -> None:
        self._default_from = default_from

    async def send(self, message: EmailMessage) -> None:
        sender = message.from_email or self._default_from
        body = message.text or message.html
        attachments = ", ".join(a.filename for a in message.attachments) or "none"
        print(
            "\n--- email (console backend, nothing was sent) ---\n"
            f"from        {sender}\n"
            f"to          {', '.join(message.to)}\n"
            f"subject     {message.subject}\n"
            f"attachments {attachments}\n"
            f"\n{body}\n"
            "------------------------------------------------\n"
        )

    async def health(self) -> tuple[bool, str]:
        return True, "console backend: messages are printed, not sent"


class MemoryMailer:
    """Collects messages so a test can assert on them."""

    def __init__(self) -> None:
        self.outbox: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)

    async def health(self) -> tuple[bool, str]:
        return True, f"memory backend: {len(self.outbox)} message(s) held"


class SMTPMailer:
    """A real server.

    The size limit is enforced here rather than at the server: a message over
    the limit is rejected after you have paid to base64-encode it, put it
    through a queue and upload it, and the rejection arrives as an opaque SMTP
    code.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        default_from: str = "",
        use_starttls: bool = True,
        use_ssl: bool = False,
        timeout: float = 30.0,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    ) -> None:
        if use_ssl and use_starttls:
            raise MailError(
                "use_ssl and use_starttls are mutually exclusive: SSL wraps the "
                "connection from the start, STARTTLS upgrades a plain one. Pick "
                "the one your server speaks -- usually STARTTLS on 587, SSL on 465."
            )
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._default_from = default_from or username
        self._use_starttls = use_starttls
        self._use_ssl = use_ssl
        self._timeout = timeout
        self._max_attachment_bytes = max_attachment_bytes

    def _send_blocking(self, message: EmailMessage) -> None:
        mime = build_mime(message, default_from=self._default_from)
        context = ssl.create_default_context()

        client: Any
        if self._use_ssl:
            client = smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context
            )
        else:
            client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with client:
            if self._use_starttls:
                client.starttls(context=context)
            if self._username:
                client.login(self._username, self._password)
            client.send_message(
                mime,
                from_addr=message.from_email or self._default_from,
                to_addrs=message.recipients,
            )

    async def send(self, message: EmailMessage) -> None:
        size = message.total_attachment_bytes()
        if size > self._max_attachment_bytes:
            raise MailError(
                f"Attachments total {size} bytes, over the {self._max_attachment_bytes} "
                f"byte limit. Send a link to the file instead."
            )
        # smtplib is blocking and has no async equivalent in the standard
        # library. A thread is the honest answer; doing it inline would stall
        # every other request for the length of the SMTP conversation.
        await asyncio.to_thread(self._send_blocking, message)

    async def health(self) -> tuple[bool, str]:
        def probe() -> tuple[bool, str]:
            try:
                if self._use_ssl:
                    with smtplib.SMTP_SSL(self._host, self._port, timeout=5) as client:
                        client.noop()
                else:
                    with smtplib.SMTP(self._host, self._port, timeout=5) as client:
                        client.noop()
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                return False, f"smtp {self._host}:{self._port} unreachable: {exc}"
            return True, f"smtp {self._host}:{self._port} reachable"

        return await asyncio.to_thread(probe)
