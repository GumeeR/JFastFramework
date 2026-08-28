"""Push notifications, over Firebase Cloud Messaging.

    [plugin.notifications]
    backend = "fcm"          # fcm | console
    project_id = "my-project"

    notifications = request.app.state.jfast.require("notifications")
    await notifications.send(Notification(
        title="Invoice paid",
        body="INV-1042 was paid.",
        tokens=[device_token],
        data={"invoice_id": "1042"},
    ))

The `console` backend prints instead of sending, which is what you want in
development and in tests: no Firebase project, no credentials, and you can see
the payload.

**Sending is a job, not a request.** A push that fails should be retried by the
queue, not by the HTTP handler that happened to trigger it. Enqueue a task and
send from the worker; `send()` here is deliberately unopinionated about that,
but the plugin will not pretend a network call to Google belongs in a request
path.

Requires: ``pip install jfastframework[fcm]``

Verified: payload construction and token batching are tested; **nothing has
been sent to a real FCM project in CI.** Treat your first send as the test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from jfastframework.errors import PluginError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.notifications")

BACKENDS = ("fcm", "console")

# FCM's HTTP v1 API sends one message per call; multicast is client-side.
# Batching more than this per flush just makes a failure harder to attribute.
MAX_TOKENS_PER_SEND = 500


@dataclass
class Notification:
    """One push."""

    title: str
    body: str
    tokens: list[str] = field(default_factory=list)
    topic: str = ""
    # Data payloads are strings on the wire; FCM rejects anything else.
    data: dict[str, str] = field(default_factory=dict)
    image: str = ""
    # Android/iOS delivery hints.
    priority: str = "high"
    collapse_key: str = ""

    def __post_init__(self) -> None:
        if not self.tokens and not self.topic:
            raise ValueError("a notification needs either tokens or a topic")
        if self.tokens and self.topic:
            raise ValueError("send to tokens or to a topic, not both")
        non_strings = [k for k, v in self.data.items() if not isinstance(v, str)]
        if non_strings:
            # Caught here rather than by a 400 from Google, which arrives
            # asynchronously and names neither the key nor the request.
            raise ValueError(
                f"FCM data values must be strings; these are not: {', '.join(non_strings)}"
            )


@dataclass
class SendResult:
    sent: int = 0
    failed: int = 0
    # Tokens FCM reported as unregistered. Delete these from your database:
    # retrying them forever is how a push backlog grows without bound.
    invalid_tokens: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


@runtime_checkable
class NotificationBackend(Protocol):
    async def send(self, notification: Notification) -> SendResult: ...
    async def health(self) -> tuple[bool, str]: ...


class ConsoleNotifier:
    """Logs instead of sending. The default, and the one used in tests."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> SendResult:
        self.sent.append(notification)
        logger.info(
            "notification (console backend, not delivered)",
            extra={
                "title": notification.title,
                "recipients": len(notification.tokens) or f"topic:{notification.topic}",
            },
        )
        return SendResult(sent=len(notification.tokens) or 1)

    async def health(self) -> tuple[bool, str]:
        return True, "console backend: notifications are logged, not delivered"


def build_fcm_message(notification: Notification, token: str = "") -> dict[str, Any]:
    """One FCM HTTP v1 message body.

    Split out so the payload can be asserted without a Firebase project --
    which is the part that is actually easy to get wrong.
    """
    message: dict[str, Any] = {
        "notification": {"title": notification.title, "body": notification.body},
    }
    if notification.image:
        message["notification"]["image"] = notification.image
    if notification.data:
        message["data"] = dict(notification.data)

    if token:
        message["token"] = token
    elif notification.topic:
        message["topic"] = notification.topic

    android: dict[str, Any] = {"priority": "HIGH" if notification.priority == "high" else "NORMAL"}
    if notification.collapse_key:
        android["collapse_key"] = notification.collapse_key
    message["android"] = android
    message["apns"] = {
        "headers": {"apns-priority": "10" if notification.priority == "high" else "5"}
    }
    return {"message": message}


class FCMNotifier:
    """Firebase Cloud Messaging over the HTTP v1 API.

    v1, not the legacy server-key API: the legacy one authenticates with a
    static key that cannot be scoped or rotated without breaking every client,
    and Google has been retiring it.
    """

    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self, *, project_id: str, credentials_json: str = "") -> None:
        self._project_id = project_id
        self._credentials_json = credentials_json
        self._endpoint = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
        self._credentials: Any = None

    def _load_credentials(self) -> Any:
        import json

        from google.oauth2 import service_account

        scopes = ["https://www.googleapis.com/auth/firebase.messaging"]
        if self._credentials_json:
            info = json.loads(self._credentials_json)
            return service_account.Credentials.from_service_account_info(info, scopes=scopes)

        # No inline credentials: fall through to Application Default
        # Credentials. On GKE or Cloud Run that means a workload identity and
        # no key file anywhere, which is the outcome you want.
        import google.auth

        credentials, _ = google.auth.default(scopes=scopes)
        return credentials

    async def _access_token(self) -> str:
        import asyncio

        from google.auth.transport.requests import Request as AuthRequest

        if self._credentials is None:
            self._credentials = await asyncio.to_thread(self._load_credentials)
        if not self._credentials.valid:
            await asyncio.to_thread(self._credentials.refresh, AuthRequest())
        return str(self._credentials.token)

    async def send(self, notification: Notification) -> SendResult:
        import httpx

        targets = notification.tokens[:MAX_TOKENS_PER_SEND] or [""]
        if len(notification.tokens) > MAX_TOKENS_PER_SEND:
            logger.warning(
                "notification truncated to %d recipients; send the rest as separate jobs",
                MAX_TOKENS_PER_SEND,
            )

        token = await self._access_token()
        result = SendResult()

        async with httpx.AsyncClient(timeout=15.0) as client:
            for target in targets:
                body = build_fcm_message(notification, target)
                response = await client.post(
                    self._endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code < 300:
                    result.sent += 1
                    continue

                result.failed += 1
                detail = ""
                try:
                    detail = response.json().get("error", {}).get("status", "")
                except ValueError:
                    detail = response.text[:120]

                # UNREGISTERED means the app was uninstalled or the token
                # rotated. Keeping it is a permanent failure on every send.
                if detail in ("UNREGISTERED", "NOT_FOUND", "INVALID_ARGUMENT") and target:
                    result.invalid_tokens.append(target)
                result.errors.append(f"{response.status_code}: {detail}")

        return result

    async def health(self) -> tuple[bool, str]:
        try:
            await self._access_token()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"cannot obtain FCM credentials: {exc}"
        return True, f"fcm project {self._project_id}"


class NotificationSettings(PluginSettings):
    model_config = SettingsConfigDict(
        env_prefix="JFAST_NOTIFICATIONS_", env_file=".env", extra="ignore"
    )

    backend: str = "console"
    project_id: str = ""
    # The service-account JSON itself. Prefer workload identity and leave this
    # empty: a key file is a credential that never expires.
    credentials_json: SecretStr | None = None
    default_priority: str = "high"
    topics: list[str] = Field(default_factory=list)


class NotificationsPlugin(Plugin):
    meta = PluginMeta(
        name="notifications",
        version="0.1.0",
        description="Push notifications over Firebase Cloud Messaging.",
        after=("observability", "queue"),
        provides=("notifications",),
        default_enabled=False,
        extra="jfastframework[fcm]",
    )
    Settings = NotificationSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._backend: NotificationBackend | None = None

    def register(self, ctx: AppContext) -> None:
        settings: NotificationSettings = self.settings

        if settings.backend not in BACKENDS:
            raise PluginError(
                f"notifications backend {settings.backend!r} is unknown; "
                f"choose from {', '.join(BACKENDS)}"
            )

        if settings.backend == "console":
            self._backend = ConsoleNotifier()
            if ctx.settings.is_production:
                ctx.logger.warning(
                    "notifications is using the console backend in production: "
                    "nothing is actually delivered."
                )
        else:
            if not settings.project_id:
                raise PluginError(
                    'notifications backend "fcm" needs [plugin.notifications] project_id'
                )
            self._backend = FCMNotifier(
                project_id=settings.project_id,
                credentials_json=(
                    settings.credentials_json.get_secret_value()
                    if settings.credentials_json
                    else ""
                ),
            )

        if not ctx.has("queue"):
            ctx.logger.info(
                "notifications has no queue: sends will happen inline, in the request. "
                "Enable the 'queue' plugin and send from a worker so a failed push "
                "is retried instead of failing the request that triggered it."
            )

        ctx.provide("notifications", self._backend)

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._backend is None:
            return HealthReport.fail("notifications not initialised")
        healthy, detail = await self._backend.health()
        meta = {"backend": self.settings.backend}
        if not healthy:
            # A push provider being down does not stop the service serving.
            return HealthReport.fail(detail, critical=False, **meta)
        return HealthReport.ok(detail, **meta)
