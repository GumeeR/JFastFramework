"""Push notifications: payload shape and the failures worth handling.

Nothing here reaches Firebase. What is tested is the part that is wrong before
the network is involved: the message body, and what happens to a token FCM
says no longer exists.
"""

from __future__ import annotations

import pytest

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.notifications import (
    ConsoleNotifier,
    Notification,
    NotificationsPlugin,
    SendResult,
    build_fcm_message,
)
from jfastframework.testing import build_test_app


def notification(**overrides: object) -> Notification:
    params: dict[str, object] = {
        "title": "Invoice paid",
        "body": "INV-1042 was paid.",
        "tokens": ["device-token"],
    }
    params.update(overrides)
    return Notification(**params)  # type: ignore[arg-type]


# -- the payload --------------------------------------------------------


def test_a_notification_needs_a_recipient() -> None:
    with pytest.raises(ValueError, match="tokens or a topic"):
        Notification(title="t", body="b")


def test_tokens_and_topic_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        Notification(title="t", body="b", tokens=["a"], topic="news")


def test_non_string_data_is_caught_before_the_network() -> None:
    # FCM rejects these with a 400 that names neither the key nor the request.
    with pytest.raises(ValueError, match="invoice_id"):
        Notification(title="t", body="b", topic="news", data={"invoice_id": 1042})  # type: ignore[dict-item]


def test_message_targets_a_token() -> None:
    message = build_fcm_message(notification(), "device-token")["message"]
    assert message["token"] == "device-token"
    assert message["notification"] == {"title": "Invoice paid", "body": "INV-1042 was paid."}
    assert "topic" not in message


def test_message_targets_a_topic() -> None:
    message = build_fcm_message(notification(tokens=[], topic="billing"))["message"]
    assert message["topic"] == "billing"
    assert "token" not in message


def test_data_is_carried_separately_from_the_notification() -> None:
    message = build_fcm_message(notification(data={"invoice_id": "1042"}), "t")["message"]
    # Data and notification are different fields: merging them means the
    # payload shows up as visible text on some clients.
    assert message["data"] == {"invoice_id": "1042"}
    assert "invoice_id" not in message["notification"]


def test_priority_maps_to_both_platforms() -> None:
    high = build_fcm_message(notification(), "t")["message"]
    assert high["android"]["priority"] == "HIGH"
    assert high["apns"]["headers"]["apns-priority"] == "10"

    normal = build_fcm_message(notification(priority="normal"), "t")["message"]
    assert normal["android"]["priority"] == "NORMAL"
    assert normal["apns"]["headers"]["apns-priority"] == "5"


def test_collapse_key_is_optional() -> None:
    assert "collapse_key" not in build_fcm_message(notification(), "t")["message"]["android"]
    with_key = build_fcm_message(notification(collapse_key="invoice"), "t")["message"]
    assert with_key["android"]["collapse_key"] == "invoice"


# -- results ------------------------------------------------------------


def test_a_result_with_failures_is_not_ok() -> None:
    assert SendResult(sent=3).ok is True
    assert SendResult(sent=2, failed=1).ok is False


# -- the console backend ------------------------------------------------


async def test_console_backend_records_instead_of_sending() -> None:
    backend = ConsoleNotifier()
    result = await backend.send(notification(tokens=["a", "b"]))
    assert result.sent == 2
    assert backend.sent[0].title == "Invoice paid"

    healthy, detail = await backend.health()
    assert healthy is True
    assert "not delivered" in detail


# -- the plugin ---------------------------------------------------------


def notifications_app(**config: object) -> object:
    return build_test_app(
        plugins=["observability", "notifications"],
        extra_plugins=[NotificationsPlugin],
        raw={"plugin": {"notifications": config}},
    )


def test_default_backend_is_the_console() -> None:
    app = notifications_app()
    backend = app.state.jfast.require("notifications")  # type: ignore[attr-defined]
    assert isinstance(backend, ConsoleNotifier)


def test_unknown_backend_is_a_startup_error() -> None:
    with pytest.raises(PluginError, match="unknown"):
        notifications_app(backend="apns")


def test_fcm_without_a_project_is_a_startup_error() -> None:
    with pytest.raises(PluginError, match="project_id"):
        notifications_app(backend="fcm")


def test_fcm_backend_is_built_when_configured() -> None:
    app = notifications_app(backend="fcm", project_id="my-project")
    backend = app.state.jfast.require("notifications")  # type: ignore[attr-defined]
    assert type(backend).__name__ == "FCMNotifier"
