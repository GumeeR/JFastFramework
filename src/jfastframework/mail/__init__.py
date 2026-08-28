"""Sending email: one interface, three backends, templates, and a queue.

Email is where a service quietly becomes unreliable. The SMTP call is slow --
hundreds of milliseconds against a good server, tens of seconds against a bad
one -- and it fails for reasons that have nothing to do with the request that
triggered it: a greylist, a rate limit, an expired app password. Sending inside
the request handler makes every one of those the user's problem, and blocks the
event loop while it happens.

So the default here is **queued**. `mail.send()` enqueues a job and returns;
the worker does the SMTP conversation, and the queue's own retry and
dead-lettering apply. `send_now()` exists for the cases that genuinely have to
be synchronous -- a one-time password, a test -- and says so at the call site.

Three backends, chosen in configuration:

* ``console`` -- prints the message. The default in ``local``, so nobody sends
  real mail from a laptop by accident, and no credentials are needed to
  develop.
* ``smtp`` -- a real server.
* ``memory`` -- collects messages in a list for tests to assert against.
"""

from jfastframework.mail.backends import (
    ConsoleMailer,
    MailBackend,
    MemoryMailer,
    SMTPMailer,
)
from jfastframework.mail.message import Attachment, EmailMessage
from jfastframework.mail.templates import TemplateRenderer

__all__ = [
    "Attachment",
    "ConsoleMailer",
    "EmailMessage",
    "MailBackend",
    "MemoryMailer",
    "SMTPMailer",
    "TemplateRenderer",
]
