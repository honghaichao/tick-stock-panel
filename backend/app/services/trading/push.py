"""F0-2: trading event → wecom/feishu webhook 推送 (2026-10-13).

设计:
  - 不依赖 wecom BotService 长连接 (它只接消息不发消息)
  - 走 webhook URL (企业微信群机器人 webhook / 飞书群机器人 webhook)
  - 读 prefs: trading_notify_wecom_webhook + trading_notify_feishu_webhook
  - 推送失败静默降级 (try/except + log warning, 不影响主流程)
  - 推送时机: order_submitted / order_filled / order_rejected / order_cleared / run_finish

不做:
  - 不实现 wecom/feishu 鉴权 token 计算 (webhook 已含, 简化)
  - 不支持消息卡 / 模板 (只发纯文本)
  - 不做消息去重 / 节流 (caller 控制触发频率)
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib import request as urlrequest
from urllib.error import URLError

from app.services import preferences as base_prefs

logger = logging.getLogger(__name__)

KEY_WECOM_WEBHOOK = "trading_notify_wecom_webhook"
KEY_FEISHU_WEBHOOK = "trading_notify_feishu_webhook"
KEY_PUSH_ENABLED = "trading_notify_push_enabled"


def _post_json(url: str, payload: dict[str, Any], timeout_s: float = 3.0) -> bool:
    """POST JSON. 失败返 False (静默降级)."""
    try:
        req = urlrequest.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300
    except (URLError, OSError, ValueError) as e:
        logger.warning("webhook 推送失败 (url=%s): %s", url[:80], e)
        return False


def _wecom_payload(text: str) -> dict:
    """企业微信群机器人 webhook payload (markdown 格式)."""
    return {"msgtype": "markdown", "markdown": {"content": text}}


def _feishu_payload(text: str) -> dict:
    """飞书群机器人 webhook payload (interactive card, 简化)."""
    return {
        "msg_type": "interactive",
        "card": {
            "elements": [{"tag": "markdown", "content": text}],
            "header": {"title": {"tag": "plain_text", "content": "实盘通知"}},
        },
    }


def push_event(kind: str, text: str, *, source: str = "trading") -> dict:
    """推一条 trading event 到 wecom + feishu webhook (按 prefs).

    Args:
        kind: 事件类型 (order_submitted / order_filled / ...), 用于过滤 + 文案。
        text: 消息文本 (markdown 格式)。

    Returns: {"wecom": bool, "feishu": bool} 各通道推送成功与否.
    """
    result: dict[str, bool] = {"wecom": False, "feishu": False}

    # 总开关
    enabled = base_prefs.load().get(KEY_PUSH_ENABLED, True)
    if not enabled:
        return result

    wecom_url = base_prefs.load().get(KEY_WECOM_WEBHOOK, "")
    if wecom_url:
        result["wecom"] = _post_json(wecom_url, _wecom_payload(text))

    feishu_url = base_prefs.load().get(KEY_FEISHU_WEBHOOK, "")
    if feishu_url:
        result["feishu"] = _post_json(feishu_url, _feishu_payload(text))

    return result


__all__ = ["KEY_FEISHU_WEBHOOK", "KEY_PUSH_ENABLED", "KEY_WECOM_WEBHOOK", "push_event"]
