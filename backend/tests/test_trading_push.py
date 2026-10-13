"""F0-2: trading event → wecom/feishu webhook 推送."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import preferences as base_prefs
from app.services.trading import push


@pytest.fixture(autouse=True)
def _clean_prefs(tmp_path, monkeypatch):
    base_prefs._invalidate_cache()
    import app.config
    monkeypatch.setattr(app.config.settings, "data_dir", tmp_path, raising=False)
    yield
    base_prefs._invalidate_cache()


def test_push_event_skips_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(base_prefs, "load", lambda: {
        push.KEY_PUSH_ENABLED: False,
        push.KEY_WECOM_WEBHOOK: "http://x",
        push.KEY_FEISHU_WEBHOOK: "http://y",
    })
    with patch.object(push, "_post_json") as mock_post:
        result = push.push_event("order_filled", "test msg")
    mock_post.assert_not_called()
    assert result == {"wecom": False, "feishu": False}


def test_push_event_sends_to_wecom_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(base_prefs, "load", lambda: {
        push.KEY_PUSH_ENABLED: True,
        push.KEY_WECOM_WEBHOOK: "https://wecom.example/webhook",
        push.KEY_FEISHU_WEBHOOK: "",
    })
    with patch.object(push, "_post_json", return_value=True) as mock_post:
        result = push.push_event("order_filled", "msg")
    assert mock_post.call_count == 1
    args, _ = mock_post.call_args
    assert args[0] == "https://wecom.example/webhook"
    payload = args[1]
    assert payload["msgtype"] == "markdown"
    assert "msg" in payload["markdown"]["content"]
    assert result == {"wecom": True, "feishu": False}


def test_push_event_sends_to_feishu_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(base_prefs, "load", lambda: {
        push.KEY_PUSH_ENABLED: True,
        push.KEY_WECOM_WEBHOOK: "",
        push.KEY_FEISHU_WEBHOOK: "https://feishu.example/webhook",
    })
    with patch.object(push, "_post_json", return_value=True) as mock_post:
        result = push.push_event("order_rejected", "rejected!")
    assert mock_post.call_count == 1
    args, _ = mock_post.call_args
    assert args[0] == "https://feishu.example/webhook"
    payload = args[1]
    assert payload["msg_type"] == "interactive"
    assert "实盘通知" in payload["card"]["header"]["title"]["content"]
    assert result == {"wecom": False, "feishu": True}


def test_push_event_sends_to_both_when_both_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(base_prefs, "load", lambda: {
        push.KEY_PUSH_ENABLED: True,
        push.KEY_WECOM_WEBHOOK: "https://wecom",
        push.KEY_FEISHU_WEBHOOK: "https://feishu",
    })
    with patch.object(push, "_post_json", return_value=True) as mock_post:
        result = push.push_event("order_submitted", "hi")
    assert mock_post.call_count == 2
    assert result == {"wecom": True, "feishu": True}


def test_push_event_handles_post_failure_silently(tmp_path, monkeypatch):
    """_post_json 返回 False 时, push_event 不抛异常 (静默降级)."""
    monkeypatch.setattr(base_prefs, "load", lambda: {
        push.KEY_PUSH_ENABLED: True,
        push.KEY_WECOM_WEBHOOK: "https://wecom",
        push.KEY_FEISHU_WEBHOOK: "",
    })
    with patch.object(push, "_post_json", return_value=False):
        result = push.push_event("order_filled", "msg")
    assert result == {"wecom": False, "feishu": False}


def test_post_json_returns_false_on_network_error():
    """真打 URL → network error → False (不抛)."""
    result = push._post_json("http://localhost:1/webhook", {"x": 1}, timeout_s=0.5)
    assert result is False
