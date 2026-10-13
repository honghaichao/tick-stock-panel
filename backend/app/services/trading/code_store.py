"""策略代码读写 + changelog — 在线编辑策略代码.

落盘位置: data/user_data/strategies/live/{strategy_id}.py
changelog: data/user_data/trading/code_changelog.jsonl

校验 (F0-1, 2026-10-13):
  - content 非空
  - 必须含 'STRATEGY' 子串 (聚宽策略必须导出 STRATEGY 实例)
  - 必须通过 `ast.parse(content)` (Python 语法正确)
    失败 → 抛 SyntaxError(line, col, msg), 由 router 映射到 400 + 详情。
"""
from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))

STRATEGY_DIR_NAME = "live"


def _strategies_dir(data_dir: Path) -> Path:
    """data/user_data/strategies/live/ 目录."""
    p = data_dir / "user_data" / "strategies" / STRATEGY_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _strategy_file(data_dir: Path, strategy_id: str) -> Path:
    return _strategies_dir(data_dir) / f"{strategy_id}.py"


def _changelog_path(data_dir: Path) -> Path:
    p = data_dir / "user_data" / "trading" / "code_changelog.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_code(data_dir: Path, strategy_id: str) -> str:
    """读策略文件内容; 不存在抛 FileNotFoundError."""
    p = _strategy_file(data_dir, strategy_id)
    if not p.exists():
        raise FileNotFoundError(f"策略文件不存在: {p}")
    return p.read_text(encoding="utf-8")


def write_code(
    data_dir: Path, strategy_id: str, content: str, note: str = "",
    *,
    verify: bool = False,
) -> dict:
    """写策略文件 + 追加 changelog.

    Args:
        verify: F0-4 dry-verify gate (False = 向后兼容, 跳过).
                True → 先调 dry_verify_strategy_code,
                失败抛 RuntimeError("verify_failed") + 详情;
                通过 → 落盘 + changelog.
    Returns: changelog entry dict.
    Raises:
        ValueError (校验失败) / SyntaxError (语法错) /
        RuntimeError("verify_failed", ...) (verify 不通过) /
        OSError (写盘失败).
    """
    if not content or not content.strip():
        raise ValueError("内容不能为空")
    if "STRATEGY" not in content:
        raise ValueError("策略文件必须包含 STRATEGY 实例定义 (聚宽惯例)")
    # F0-1: Python 语法预检 — ast.parse 在写盘前拦截 SyntaxError,
    # 防止 importlib.reload 时把策略静默掉 (用户感受: 改坏后页面无法访问)。
    try:
        ast.parse(content)
    except SyntaxError as e:
        # 重新抛: 把 line/col/msg 带进路由层 → 400 详情
        raise SyntaxError(
            f"line {e.lineno}, col {e.offset}: {e.msg}",
            (e.filename, e.lineno, e.offset, e.text),
        ) from None

    # F0-4: dry-verify gate — importlib 加载 + 跑一次 on_event,
    # 失败则不落盘 (防止坏策略进 live/)。向后兼容: verify=False 跳过。
    if verify:
        from app.services.trading.auto_verify import dry_verify_strategy_code
        result = dry_verify_strategy_code(content)
        if not result.passed:
            raise RuntimeError(
                f"verify_failed:{result.error_kind}:{result.error_msg}"
            )

    p = _strategy_file(data_dir, strategy_id)
    old_size = p.stat().st_size if p.exists() else 0
    new_size = len(content.encode("utf-8"))
    # F1-5: 写盘前 snapshot 旧版本 (已有文件时)
    if old_size > 0:
        try:
            from app.services.trading import history
            old_content = p.read_text(encoding="utf-8")
            history.snapshot_now(
                data_dir, strategy_id, old_content,
                note=note or "auto_before_overwrite",
            )
        except Exception as e:
            # snapshot 失败不阻塞落盘 (历史是辅助功能)
            import logging
            logging.getLogger(__name__).warning("snapshot 失败: %s", e)
    # 原子写
    from app.services.fs_utils import atomic_write_text
    atomic_write_text(p, content)
    # changelog
    entry = {
        "ts": datetime.now(CN_TZ).isoformat(),
        "strategy_id": strategy_id,
        "old_size": old_size,
        "new_size": new_size,
        "diff_summary": _diff_summary(old_size, new_size),
        "note": note or "",
    }
    with _changelog_path(data_dir).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("code 替换: %s old=%d new=%d", strategy_id, old_size, new_size)
    return entry


def _diff_summary(old: int, new: int) -> str:
    if old == 0:
        return f"新建 ({new} 字节)"
    delta = new - old
    if delta > 0:
        return f"新增 {delta} 字节"
    if delta < 0:
        return f"删除 {-delta} 字节"
    return "替换 (字节数不变)"


def list_changelog(data_dir: Path, strategy_id: str | None = None, limit: int = 50) -> list[dict]:
    """读 code_changelog.jsonl, 可按 strategy_id 过滤."""
    p = _changelog_path(data_dir)
    if not p.exists():
        return []
    items: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if strategy_id and rec.get("strategy_id") != strategy_id:
            continue
        items.append(rec)
    items.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return items[:limit]


__all__ = ["list_changelog", "read_code", "write_code"]


# ── Trash recovery (PR-1 子任务 §5.4 / B4) ─────────────────────────────────
def _trash_dir(data_dir: Path) -> Path:
    """data/user_data/strategies/live/.trash/ 目录. 与 registry 不参与, 只文件 IO."""
    p = _strategies_dir(data_dir) / ".trash"
    p.mkdir(parents=True, exist_ok=True)
    return p


def move_to_trash(data_dir: Path, strategy_id: str) -> Path:
    """移动 .py 到 .trash/<id>_<ts>.py.

    不存在 → FileNotFoundError. 目标已存在 (极小概率同 ts) → 加随机后缀.
    返回新文件路径.
    """
    import secrets
    import shutil
    src = _strategy_file(data_dir, strategy_id)
    if not src.exists():
        raise FileNotFoundError(f"策略文件不存在: {src}")
    ts = datetime.now(CN_TZ).strftime("%Y%m%d%H%M%S")
    suffix = secrets.token_hex(2)  # 防同秒冲突
    dst_name = f"{strategy_id}_{ts}_{suffix}.py"
    dst = _trash_dir(data_dir) / dst_name
    shutil.move(str(src), str(dst))
    logger.info("策略 %s 已移到回收站: %s", strategy_id, dst)
    return dst


def restore_from_trash(data_dir: Path, trashed_filename: str) -> Path:
    """从 .trash/<file> 还原回 live/<id>.py.

    解析 filename 得到 strategy_id (按 '_YYYYMMDDHHMMSS_<rand>.py' 截前缀).
    """
    if "/" in trashed_filename or "\\" in trashed_filename:
        raise ValueError("filename 不允许路径分隔符")
    src = _trash_dir(data_dir) / trashed_filename
    if not src.exists():
        raise FileNotFoundError(f"trash 文件不存在: {src}")
    # 解析 strategy_id: 文件名 = <strategy_id>_<YYYYMMDDHHMMSS>_<rand>.py
    # 末两段是 ts + suffix; 前面的 _ 拼回去.
    parts = trashed_filename.rsplit(".py", 1)[0].rsplit("_", 2)
    if len(parts) < 3:
        raise ValueError(f"无法解析 trashed_filename: {trashed_filename}")
    strategy_id = parts[0]
    dst = _strategy_file(data_dir, strategy_id)
    if dst.exists():
        raise FileExistsError(f"目标策略文件已存在, 拒绝覆盖: {dst}")
    import shutil as _sh
    _sh.move(str(src), str(dst))
    return dst
