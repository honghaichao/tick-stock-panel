"""通知中心测试 — 落盘 + 读 + 端到端."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

from app.services.trading import notify_store


def test_notify_append_and_list(tmp_path):
    """append + list_recent 基本."""
    e1 = notify_store.append(tmp_path, kind="info", text="hello", source="test")
    e2 = notify_store.append(tmp_path, kind="user_message", text="world", source="strategy:rebalance")
    assert e1["kind"] == "info"
    assert e2["kind"] == "user_message"
    items = notify_store.list_recent(tmp_path, limit=10)
    assert len(items) == 2
    assert items[0]["text"] == "world"  # 倒序


def test_notify_filter_by_kind(tmp_path):
    notify_store.append(tmp_path, kind="info", text="a")
    notify_store.append(tmp_path, kind="warn", text="b")
    notify_store.append(tmp_path, kind="error", text="c")
    info = notify_store.list_recent(tmp_path, kind="info")
    assert len(info) == 1
    assert info[0]["text"] == "a"
    err = notify_store.list_recent(tmp_path, kind="error")
    assert len(err) == 1


def test_notify_invalid_kind_falls_back_to_info(tmp_path):
    """未注册的 kind 自动降级为 info (防御性)."""
    e = notify_store.append(tmp_path, kind="bogus_xxx", text="x")
    assert e["kind"] == "info"


def test_notify_extra_metadata(tmp_path):
    e = notify_store.append(tmp_path, kind="order_filled", text="成交 100 股",
                            source="order:ord_abc", extra={"qty": 100, "price": 7.56})
    assert e["extra"]["qty"] == 100
    assert e["extra"]["price"] == 7.56
    items = notify_store.list_recent(tmp_path)
    assert items[0]["extra"]["qty"] == 100


def test_notify_list_recent_limit(tmp_path):
    for i in range(20):
        notify_store.append(tmp_path, kind="info", text=f"msg-{i}")
    items = notify_store.list_recent(tmp_path, limit=5)
    assert len(items) == 5


def test_notify_list_recent_empty(tmp_path):
    """无文件不抛错, 返空 list."""
    items = notify_store.list_recent(tmp_path)
    assert items == []


def test_notify_corrupt_lines_skipped(tmp_path):
    """坏行不阻塞, 自动跳过."""
    p = tmp_path / "user_data" / "trading" / "notify.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"ts":"2025-01-01T00:00:00+08:00","kind":"info","text":"good","source":"x"}\n'
                  'CORRUPT LINE NOT JSON\n'
                  '{"ts":"2025-01-02T00:00:00+08:00","kind":"info","text":"also good","source":"x"}\n',
                  encoding="utf-8")
    items = notify_store.list_recent(tmp_path)
    assert len(items) == 2
    assert items[0]["text"] == "also good"
