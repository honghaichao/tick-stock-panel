"""Tushare 限速退避单元测试 — 不调真实 Tushare SDK, mock 调用 + mock sleep.

覆盖:
  1. 限速错误关键字识别 (中英文)
  2. 限速时按 RATE_LIMIT_COOLDOWN_S 退避重试
  3. 超过 RATE_LIMIT_MAX_RETRIES 后放弃, 不无限循环
  4. 非限速异常不重试 (与 _ts_safe 行为一致)
  5. 正常调用 (空 DataFrame / None) 不触发重试
  6. _ts_safe 旧行为保持: 单次尝试 + 异常转 None (向后兼容)
"""
from __future__ import annotations

import pytest

from app.plugins.tushare import provider as tp


# ---- 1) 限速关键字识别 ----
@pytest.mark.parametrize("msg,expected", [
    ("抱歉，您访问接口(adj_factor)频率超限(500次/分钟)", True),
    ("rate limit exceeded", True),
    ("Too Many Requests", True),
    ("访问频率过高", True),
    ("超出限制", True),
    # 假阴性: 网络/参数错误不应误判为限速
    ("Network connection failed", False),
    ("symbol not found", False),
    ("", False),
])
def test_is_rate_limit_error(msg, expected):
    assert tp._is_rate_limit_error(msg) is expected


# ---- 2) 限速触发退避重试, 最终成功 ----
def test_rate_limit_triggers_retry_then_succeeds(monkeypatch):
    sleep_calls: list[float] = []
    monkeypatch.setattr(tp.time, "sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}

    def fake_func(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise RuntimeError(f"频率超限 (attempt {call_count['n']})")
        return None  # 第 3 次成功返回空 DF (Tushare 行为)

    result = tp._ts_call_with_backoff(fake_func, ts_code="000001.SZ")
    assert result is None  # 成功路径: df is None → 返回 None
    assert call_count["n"] == 3
    # 前两次失败后各 sleep 一次 (第三次成功, 无需 sleep)
    assert sleep_calls == [tp.RATE_LIMIT_COOLDOWN_S, tp.RATE_LIMIT_COOLDOWN_S]


# ---- 3) 超过重试上限后放弃 ----
def test_rate_limit_gives_up_after_max_retries(monkeypatch):
    sleep_calls: list[float] = []
    monkeypatch.setattr(tp.time, "sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}

    def fake_func(**_kwargs):
        call_count["n"] += 1
        raise RuntimeError(f"频率超限 (attempt {call_count['n']})")

    result = tp._ts_call_with_backoff(fake_func)
    assert result is None
    # 总共调用 RATE_LIMIT_MAX_RETRIES + 1 次 (初始 + N 次重试)
    assert call_count["n"] == tp.RATE_LIMIT_MAX_RETRIES + 1
    # 重试前 sleep, 最后一次失败不 sleep (按设计)
    assert len(sleep_calls) == tp.RATE_LIMIT_MAX_RETRIES
    assert all(s == tp.RATE_LIMIT_COOLDOWN_S for s in sleep_calls)


# ---- 4) 非限速异常不重试 ----
def test_non_rate_limit_error_no_retry(monkeypatch):
    sleep_calls: list[float] = []
    monkeypatch.setattr(tp.time, "sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}

    def fake_func(**_kwargs):
        call_count["n"] += 1
        raise ValueError("Network timeout")

    result = tp._ts_call_with_backoff(fake_func)
    assert result is None
    assert call_count["n"] == 1
    assert sleep_calls == []


# ---- 5) 正常调用 (返回 None) 不触发重试 ----
def test_normal_empty_response_no_retry(monkeypatch):
    sleep_calls: list[float] = []
    monkeypatch.setattr(tp.time, "sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}

    def fake_func(**_kwargs):
        call_count["n"] += 1
        return None  # Tushare SDK 在 df is None 时原样返回

    result = tp._ts_call_with_backoff(fake_func)
    assert result is None
    assert call_count["n"] == 1
    assert sleep_calls == []


# ---- 6) _ts_safe 旧行为不变 (向后兼容) ----
def test_ts_safe_legacy_single_shot(monkeypatch):
    """_ts_safe 必须保留旧行为: 单次调用, 异常 → None. 否则破坏向后兼容或外部测试."""
    sleep_calls: list[float] = []
    monkeypatch.setattr(tp.time, "sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}

    def fake_func(**_kwargs):
        call_count["n"] += 1
        raise RuntimeError("频率超限")

    result = tp._ts_safe(fake_func)
    assert result is None
    assert call_count["n"] == 1
    assert sleep_calls == []  # 关键: _ts_safe 不应触发退避
