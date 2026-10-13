"""策略代码读写 + changelog 测试."""
from __future__ import annotations

import pytest

from app.services.trading import code_store


def test_read_code_not_exists(tmp_path):
    with pytest.raises(FileNotFoundError):
        code_store.read_code(tmp_path, "missing_strategy")


def test_write_code_basic(tmp_path):
    content = '"""策略注释."""\nSTRATEGY = None\n'
    entry = code_store.write_code(tmp_path, "test_strat", content, note="init")
    p = code_store._strategy_file(tmp_path, "test_strat")
    assert p.exists()
    assert p.read_text(encoding="utf-8") == content
    assert entry["strategy_id"] == "test_strat"
    assert entry["new_size"] == len(content.encode("utf-8"))


def test_write_code_overwrite(tmp_path):
    p = code_store._strategy_file(tmp_path, "test_strat")
    v1 = "# v1\nSTRATEGY = 1\n"
    code_store.write_code(tmp_path, "test_strat", v1)
    # F0-1 后, 第二次必须仍是合法 Python
    v2 = "# v2\nSTRATEGY = 2\n_more = 'lines'\n"
    e2 = code_store.write_code(tmp_path, "test_strat", v2, note="update")
    assert "STRATEGY = 2" in p.read_text(encoding="utf-8")
    assert e2["old_size"] == len(v1.encode("utf-8"))
    assert e2["diff_summary"] != "新建"


def test_write_code_rejects_empty(tmp_path):
    with pytest.raises(ValueError, match="不能为空"):
        code_store.write_code(tmp_path, "test_strat", "")
    with pytest.raises(ValueError, match="不能为空"):
        code_store.write_code(tmp_path, "test_strat", "   \n  \n")


def test_write_code_requires_STRATEGY(tmp_path):
    with pytest.raises(ValueError, match="STRATEGY"):
        code_store.write_code(tmp_path, "test_strat", "x = 1\n")


def test_changelog_recorded(tmp_path):
    code_store.write_code(tmp_path, "s1", "STRATEGY = 1\n", note="first")
    code_store.write_code(tmp_path, "s2", "STRATEGY = 2\n", note="second")
    items = code_store.list_changelog(tmp_path)
    assert len(items) == 2
    assert items[0]["strategy_id"] == "s2"  # 倒序
    assert items[1]["strategy_id"] == "s1"


def test_changelog_filter_by_strategy(tmp_path):
    code_store.write_code(tmp_path, "s1", "STRATEGY = 1\n")
    code_store.write_code(tmp_path, "s2", "STRATEGY = 2\n")
    items = code_store.list_changelog(tmp_path, strategy_id="s1")
    assert len(items) == 1
    assert items[0]["strategy_id"] == "s1"


# ── F0-1: ast.parse pre-check (2026-10-13) ─────────────────────────────────
def test_write_code_rejects_syntax_error(tmp_path):
    """缺冒号的函数定义 → ast.SyntaxError → write_code 抛 SyntaxError."""
    bad_content = (
        '"""策略注释."""\n'
        'STRATEGY = None\n'
        'def broken(x)\n'   # ← 缺冒号
        '    return x\n'
    )
    with pytest.raises(SyntaxError, match=r"line \d+, col \d+"):
        code_store.write_code(tmp_path, "bad_strat", bad_content)


def test_write_code_accepts_multiline_docstring(tmp_path):
    """多行字符串 / 表达式正常 .py 文件必须通过 ast.parse (回归)."""
    content = (
        '"""三引号 docstring 内部可含 \\n 等."""\n'
        'from typing import Any\n'
        'STRATEGY = "ok"\n'
        'x: int = 1\n'
    )
    entry = code_store.write_code(tmp_path, "good_strat", content)
    assert entry["strategy_id"] == "good_strat"
    p = code_store._strategy_file(tmp_path, "good_strat")
    assert p.read_text(encoding="utf-8") == content


def test_write_code_rejects_syntax_error_with_STRATEGY_keyword(tmp_path):
    """含 STRATEGY 但 syntax 错 → SyntaxError (F0-1 拦截)."""
    bad = '"""s""\nSTRATEGY\ndef x(:\n  pass\n'
    with pytest.raises(SyntaxError):
        code_store.write_code(tmp_path, "s", bad)


def test_STRATEGY_check_runs_before_syntax_check(tmp_path):
    """无 STRATEGY + syntax 错 → ValueError 优先 (既有 STRATEGY 检查先)."""
    bad = "def x(:\n  pass\n"
    with pytest.raises(ValueError, match="STRATEGY"):
        code_store.write_code(tmp_path, "s", bad)
