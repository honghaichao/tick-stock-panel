"""F1-5: 快照 / diff / 回滚 (2026-10-13)."""
from __future__ import annotations

import pytest

from app.services.trading import code_store, history


def _content_v1() -> str:
    return "# v1\nSTRATEGY = 1\n"


def _content_v2() -> str:
    return "# v2\nSTRATEGY = 2\n"


# ── snapshot_now / list_history ──────────────────────────────────────
def test_snapshot_creates_file_and_manifest(tmp_path):
    m = history.snapshot_now(tmp_path, "s1", _content_v1(), note="init")
    assert m.size == len(_content_v1().encode("utf-8"))
    items = history.list_history(tmp_path, "s1")
    assert len(items) == 1
    assert items[0].note == "init"


def test_list_history_returns_newest_first(tmp_path):
    import time
    history.snapshot_now(tmp_path, "s1", _content_v1(), note="first")
    time.sleep(1.1)  # 不同秒保证 ts 不同
    history.snapshot_now(tmp_path, "s1", _content_v2(), note="second")
    items = history.list_history(tmp_path, "s1")
    assert items[0].note == "second"
    assert items[1].note == "first"


def test_history_pruned_to_MAX_HISTORY(tmp_path):
    """超过 MAX_HISTORY 时删最旧的."""
    for i in range(history.MAX_HISTORY + 5):
        history.snapshot_now(tmp_path, "s1", f"v{i}\n", note=f"v{i}")
    items = history.list_history(tmp_path, "s1")
    assert len(items) == history.MAX_HISTORY


# ── diff_snapshot_vs_current ──────────────────────────────────────
def test_diff_returns_unified_diff_lines(tmp_path):
    history.snapshot_now(tmp_path, "s1", _content_v1(), note="v1")
    # 把 live/s1.py 写成 v2
    code_store.write_code(tmp_path, "s1", _content_v2(), note="v2")
    items = history.list_history(tmp_path, "s1")
    ts = items[0].ts  # 最新的 v1 snapshot
    d = history.diff_snapshot_vs_current(tmp_path, "s1", ts)
    assert d is not None
    assert d["ts"] == ts
    assert len(d["diff"]) > 0
    assert any(line.startswith("+") or line.startswith("-") for line in d["diff"])


def test_diff_returns_none_for_missing_snapshot(tmp_path):
    d = history.diff_snapshot_vs_current(tmp_path, "s1", "20990101_000000Z")
    assert d is None


# ── rollback ──────────────────────────────────────
def test_rollback_restores_old_content(tmp_path):
    history.snapshot_now(tmp_path, "s1", _content_v1(), note="v1")
    code_store.write_code(tmp_path, "s1", _content_v2(), note="v2")
    items = history.list_history(tmp_path, "s1")
    v1_ts = [m.ts for m in items if m.note == "v1"][0]
    info = history.rollback(tmp_path, "s1", v1_ts)
    assert info["after_size"] == len(_content_v1().encode("utf-8"))
    # live/{s1}.py 应该是 v1
    p = code_store._strategy_file(tmp_path, "s1")
    assert "STRATEGY = 1" in p.read_text(encoding="utf-8")


def test_rollback_raises_for_missing_snapshot(tmp_path):
    with pytest.raises(FileNotFoundError, match="快照不存在"):
        history.rollback(tmp_path, "s1", "20990101_000000Z")


def test_rollback_auto_snapshots_current_before_overwrite(tmp_path):
    """回滚前先 snapshot 当前 → 防止数据丢失."""
    import time
    history.snapshot_now(tmp_path, "s1", _content_v1(), note="v1")
    code_store.write_code(tmp_path, "s1", _content_v2(), note="v2")
    items_before = history.list_history(tmp_path, "s1")
    v1_ts = [m.ts for m in items_before if m.note == "v1"][0]
    time.sleep(1.1)
    # rollback (不带 current_content → 内部自动 snapshot v2)
    history.rollback(tmp_path, "s1", v1_ts)
    items_after = history.list_history(tmp_path, "s1")
    # 应该多一条 auto_before_rollback_to_<ts> 记录 v2 内容
    auto = [m for m in items_after if m.note.startswith("auto_before_rollback_to_")]
    assert len(auto) == 1
    # 验证 auto snapshot 的内容是 v2 (不是 v1)
    auto_ts = auto[0].ts
    hist_dir = tmp_path / "user_data" / "strategies" / "live" / ".history" / "s1"
    snap_file = hist_dir / f"{auto_ts.rstrip('Z')}.py"
    assert "STRATEGY = 2" in snap_file.read_text(encoding="utf-8")
    # live/s1.py 此时已被 rollback 写成 v1
    p = code_store._strategy_file(tmp_path, "s1")
    assert "STRATEGY = 1" in p.read_text(encoding="utf-8")


# ── 集成: write_code 自动 snapshot 旧版 ────────────────────────────
def test_write_code_snapshots_old_version(tmp_path):
    """F1-5: write_code 第二次时自动 snapshot 旧版本."""
    import time
    code_store.write_code(tmp_path, "s1", _content_v1(), note="v1")
    time.sleep(1.1)  # 不同秒, 避免 ts 冲突
    code_store.write_code(tmp_path, "s1", _content_v2(), note="v2")
    # 第二次 write_code 时, 旧 v1 内容被 snapshot. snapshot note 是 caller 传的 "v2".
    items = history.list_history(tmp_path, "s1")
    assert len(items) == 1
    snap = items[0]
    assert snap.note == "v2"  # note 是 caller 当前写的
    # 但 snapshot 文件内容应该是 v1 (旧版)
    hist_dir = tmp_path / "user_data" / "strategies" / "live" / ".history" / "s1"
    snap_file = hist_dir / f"{snap.ts.rstrip('Z')}.py"
    assert snap_file.exists()
    assert "STRATEGY = 1" in snap_file.read_text(encoding="utf-8")  # 旧 v1 内容
