"""Email: validation, templates, and the queue that makes it survivable.

The behaviour worth defending is that `send()` does not talk to a mail server
inside a request. Everything else here is guarding the ways an email quietly
goes out wrong -- an HTML-only body, an unescaped name, a bcc leaked into a
header.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.mail.backends import MemoryMailer, build_mime
from jfastframework.mail.message import Attachment, EmailMessage, MailError
from jfastframework.mail.templates import TemplateRenderer, html_to_text
from jfastframework.plugins.builtin.mail import SEND_TASK, Mailer, MailSettings, build_backend

# -- a malformed message fails where it was written --------------------


def test_a_recipient_is_required() -> None:
    with pytest.raises(MailError, match="at least one recipient"):
        EmailMessage(to=[], subject="hi", text="body")


def test_an_address_must_look_like_one() -> None:
    with pytest.raises(MailError, match="not an email address"):
        EmailMessage(to=["nope"], subject="hi", text="body")


def test_a_body_is_required() -> None:
    with pytest.raises(MailError, match="text or an html body"):
        EmailMessage(to=["a@b.com"], subject="hi")


def test_a_single_recipient_string_is_accepted() -> None:
    """Passing `to="a@b.com"` is the easy mistake; silently iterating it is worse."""
    message = EmailMessage(to="a@b.com", subject="hi", text="body")  # type: ignore[arg-type]
    assert message.to == ["a@b.com"]


# -- the envelope -------------------------------------------------------


def test_bcc_never_becomes_a_header() -> None:
    """A bcc in a header is a bcc that is not blind."""
    message = EmailMessage(to=["a@b.com"], bcc=["secret@b.com"], subject="hi", text="body")
    mime = build_mime(message, default_from="from@b.com")
    assert mime["Bcc"] is None
    assert "secret@b.com" not in mime.as_string()
    # It is still in the envelope, which is how it gets delivered.
    assert "secret@b.com" in message.recipients


def test_a_message_survives_a_queue_round_trip() -> None:
    original = EmailMessage(
        to=["a@b.com"],
        subject="Factura",
        html="<p>hola</p>",
        text="hola",
        attachments=[Attachment("f.pdf", b"%PDF-1.4 binary\x00bytes", "application/pdf")],
    )
    restored = EmailMessage.from_json(original.to_json())
    assert restored.to == original.to
    assert restored.subject == original.subject
    assert restored.attachments[0].content == original.attachments[0].content


# -- templates ----------------------------------------------------------


def test_templates_autoescape(tmp_path: Path) -> None:
    """These render HTML for an inbox from data the app did not write."""
    (tmp_path / "welcome.html").write_text("<p>Hola {{ name }}</p>", encoding="utf-8")
    renderer = TemplateRenderer(tmp_path)
    html, _ = renderer.render("welcome", {"name": "<script>alert(1)</script>"})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_plain_text_part_is_always_produced(tmp_path: Path) -> None:
    """HTML-only mail is filtered harder and unreadable to a screen reader."""
    (tmp_path / "invoice.html").write_text(
        "<h1>Factura</h1><p>Total: 100</p><br><p>Gracias</p>", encoding="utf-8"
    )
    html, text = TemplateRenderer(tmp_path).render("invoice", {})
    assert "<h1>" in html
    assert "<" not in text
    assert "Factura" in text and "Gracias" in text


def test_a_text_template_wins_when_present(tmp_path: Path) -> None:
    (tmp_path / "n.html").write_text("<p>rich</p>", encoding="utf-8")
    (tmp_path / "n.txt").write_text("plain & simple", encoding="utf-8")
    _, text = TemplateRenderer(tmp_path).render("n", {})
    assert text.strip() == "plain & simple", "the text part must not be html-escaped"


def test_a_missing_variable_fails_the_render(tmp_path: Path) -> None:
    """Better than emailing somebody 'Hola ,'."""
    from jinja2 import UndefinedError

    (tmp_path / "x.html").write_text("<p>Hola {{ name }}</p>", encoding="utf-8")
    with pytest.raises(UndefinedError):
        TemplateRenderer(tmp_path).render("x", {})


def test_html_to_text_keeps_the_line_structure() -> None:
    text = html_to_text("<p>uno</p><p>dos</p><br>tres")
    assert text.splitlines()[0] == "uno"
    assert "dos" in text and "tres" in text


# -- the queue is the default ------------------------------------------


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[object] = []

    async def enqueue(self, job: object) -> str:
        self.jobs.append(job)
        return "job-1"


async def test_send_queues_instead_of_talking_to_a_server() -> None:
    """The whole point: a slow mail server must not become a slow request."""
    backend = MemoryMailer()
    queue = FakeQueue()
    mailer = Mailer(backend, default_from="from@b.com", queue=queue)

    job_id = await mailer.send(mailer.message(to="a@b.com", subject="hi", text="body"))

    assert job_id == "job-1"
    assert backend.outbox == [], "send() must not reach the backend directly"
    assert len(queue.jobs) == 1
    assert queue.jobs[0].task == SEND_TASK  # type: ignore[attr-defined]


async def test_send_now_is_the_synchronous_escape_hatch() -> None:
    backend = MemoryMailer()
    mailer = Mailer(backend, default_from="from@b.com", queue=FakeQueue())

    await mailer.send_now(mailer.message(to="a@b.com", subject="otp", text="123456"))

    assert len(backend.outbox) == 1


async def test_without_a_queue_it_sends_inline() -> None:
    """No queue plugin is not an error; it changes what send() does."""
    backend = MemoryMailer()
    mailer = Mailer(backend, default_from="from@b.com", queue=None)

    assert await mailer.send(mailer.message(to="a@b.com", subject="hi", text="b")) == "sent"
    assert len(backend.outbox) == 1


# -- configuration ------------------------------------------------------


def test_the_default_backend_does_not_send() -> None:
    """A laptop must not be able to email a customer by accident."""
    assert MailSettings(_env_file=None).backend == "console"  # type: ignore[call-arg]


def test_ssl_and_starttls_are_mutually_exclusive() -> None:
    settings = MailSettings(backend="smtp", use_ssl=True, use_starttls=True, _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(MailError, match="mutually exclusive"):
        build_backend(settings)


def test_an_unknown_backend_is_refused() -> None:
    settings = MailSettings(backend="carrier-pigeon", _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="Unknown mail backend"):
        build_backend(settings)


async def test_an_oversized_attachment_is_refused_before_the_upload() -> None:
    from jfastframework.mail.backends import SMTPMailer

    mailer = SMTPMailer(host="localhost", max_attachment_bytes=10)
    message = EmailMessage(
        to=["a@b.com"],
        subject="big",
        text="body",
        attachments=[Attachment("big.bin", b"x" * 100)],
    )
    with pytest.raises(MailError, match="over the"):
        await mailer.send(message)
