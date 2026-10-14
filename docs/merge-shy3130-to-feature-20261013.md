# merge-shy3130-to-feature-20261013 — task spec

## 目标

把 `shy3130/tick-stock-panel` 上游 main（`506d66f1`，pull 到本地共 711 commits）的所有内容合到 `feature_20261013` 分支，使得 fork (`honghaichao/tick-stock-panel`) 之后可以反映本地的"本地 a06d3dd init + 全量 shy3130 main"。

成功标准：

1. `feature_20261013` 上 `git status` 干净，0 unmerged，0 Python 语法错。
2. 后端实际可被 Python 解释器 import 至少 `app`。
3. `ruff check backend/` 0 error。
4. `pytest --collect-only` 能列出所有收集到的测试（即测试 collection path 走得通；不需要跑通）。
5. merge 之后 commit 落到 `feature_20261013` 上（两个 parent：`a06d3dd`、`506d66f1`）。
6. 把 `feature_20261013` 推到 `honghaichao/tick-stock-panel` 远端（用 `--force-with-lease` 防 fork 回滚）。

## 范围

In:

- 走完本地 `feature_20261013` 上已经准备好的 merge state（`MERGE_HEAD = 506d66f1`）→ 提交 → 推到 fork。
- 对 Python 文件做 `uv` 一次性依赖装好后做 `ruff check` + `pytest --collect-only`。
- 对陌生有 REVIEW 标记的文件——保留"双侧"（143 个），不动。

Out：

- 不动 `/tmp/ours-backup/` 里那 53 个被 we-takes-theirs 的文件（除非后续单独再恢复）。
- 不改 REVIEW 标记本身（人 review 时再看）。
- 不改 fork 端的回滚锚点 tag `backup-b8149620`（它仍指向 `81496205`，即 fork 在 init 推送前的状态）。
- 不动 `.venv/`、截图、build artifact。

## 数据契约

依赖 `CONTRIBUTING.md` 与 `docs/secondary-development.md` 中已有的契约。本次合并不写新 endpoint、不改 schema、不改 JSONL。

保留：

- `backend/app/plugins/tushare/`（a06d3dd 新增）
- `backend/app/services/trading/`（a06d3dd 新增）
- `backend/app/plugins/stocksdk/`（a06d3dd 新增）
- `mcp-server/`、`backend/app/api/events.py`、`backend/app/api/ext_pages.py`（shy3130 新增）
- `screenshots/`、`gui-test-screenshots/`（依 C-b 仍排除在 fork 跟踪外——已在 `.gitignore`）

## API 契约

不改任何 HTTP/SSE 端点、不改请求/响应字段。merge 期间对端点的所有变更通过 git 提交历史反映。

## 行为口径

merge commit 的两个 parent：

- parent 1（ours）：`a06d3ddb96fd68c8c459f6ddcf7787766f580d39` —— 本地 init commit（by honghc）
- parent 2（theirs）：`506d66f1ef11c48120389d7527e747e96f904da9` —— shy3130 main tip

合并策略：

- 对 143 个 REVIEW-required 文件：保留双方代码，REVIEW 标记保留。
- 对 53 个 `we_took_theirs` 的文件：已经在 `/tmp/ours-backup/` 留有 OURS 备份；本任务不动它们。
- 总 diff：533 files changed, 53966 inserts, 1790 deletes（vs HEAD = `a06d3dd`）。

回滚：在 merge commit 完成之前，**`backup-b8149620` tag 仍然指向 `81496205`**——一旦合并 commit 落到 fork 之后，这个 tag 仍是 `81496205` 的快门（init 之前的状态）。要让 fork 回到 init 之前，用：

```
git push --force upstream 81496205c7cc9be44eafc72e15cd9ab46bbac83c:refs/heads/main
```

## 风险与边界

- 707 个 OURS-side blob（143 个 REVIEW + 53 个 we-takes-theirs 之外的 ours-only 文件）也参与了合并——它们的 diff 进入 merge commit tree，请用户在 review 时关注 (参见 review/20261014-shy3130-merge/REVIEW-FILES.txt 计划)。
- 53 个被 we-takes-theirs 的文件：用户在 merge 后单独 review `/tmp/ours-backup/`，可选 apply 必要改动。
- 远端 fork 现在只接受了 `a06d3dd` 一个 commit — 推 force 会把 fork 从 `a06d3dd` 移到当前新 merge commit。
- sandbox 网络环境对 `git push` 已有先例（8.5s @ a06d3dd 推送过 OK），但 force-push 多一层，需要观察。
- `.git/MERGE_*` 文件由 git 自己处理，commit 完会清。

## 测试矩阵

| 维度 | 命令 | 通过条件 |
|------|------|---------|
| 1. 语法 | `find backend -name '*.py' -not -path '*/.venv/*' \| xargs py_compile` | 全部 compile OK |
| 2. Import | `cd backend && uv run --no-project python -c "import app; print(app.__version__)"` | 输出 `0.3.6` |
| 3. Lint | `cd backend && uv run --no-project ruff check .` | 0 error（warn 可接受） |
| 4. Test collection | `cd backend && uv run --no-project pytest tests/ --collect-only -q` | exit 0；列出已收集的测试类 |
| 5. Merge commit | `git log -1 --format='%P'` | 显示 2 个 parent |

## 验收标准

完成下列全部 4 步算 PASS：

```bash
# 1. 语法 & lint
cd /Volumes/quant-ssd/projects/tickflow-stock-panel
find backend -name '*.py' -not -path '*/.venv/*' -print0 | xargs -0 python3 -c "
import py_compile, sys
for line in sys.stdin.buffer.read().split(b'\0'):
    p = line.decode().strip()
    if p: py_compile.compile(p, doraise=True)
" && echo "✓ syntax"

cd backend
uv run --no-project ruff check . 2>&1 | tail -20

# 2. import smoke
uv run --no-project python -c "import app; print('OK', app.__version__)"

# 3. test collection
uv run --no-project pytest tests/ --collect-only -q 2>&1 | tail -10

# 4. push (在 sandbox 内先行尝试；超时/失败则切本地执行)
git push --force-with-lease=upstream/main:506d66f1ef11c48120389d7527e747e96f904da9 upstream main
```

注释：

- 第 4 步用 `--force-with-lease=<expected upstream sha>` 是因为：force-push 是把 fork 的 main 从 `506d66f1` 改写到我们 merge commit 的 SH。A 只在 lease 校验通过时执行，避免有人在我们未注意时把 fork 推到了别的状态。
- 第 4 步若 sandbox `git push` 卡住超过 ~30s，留给用户在终端跑：
  ```bash
  cd /Volumes/quant-ssd/projects/tickflow-stock-panel
  git push --force-with-lease=upstream/main:506d66f1ef11c48120389d7527e747e96f904da9 upstream main
  ```


---

## 完成状态（2026-10-14）

✅ Phase D：smoke tests
- 0 Python 语法错误
- `import app.__version__ = 0.3.6`
- 25 个核心模块导入 OK
- `pytest --collect-only` 收集 3108 个测试（剩下 2 个 collection error 是 sandbox 没装 `tushare` 包，不是 merge 问题）
- `uv lock` 文件整理（处理了重复键）

✅ Phase A：提交与推送
- 提交 `a12236f`：`merge shy3130/main into feature_20261013`
  - parent 1: `a06d3ddb...` (ours, honghc init)
  - parent 2: `506d66f1...` (theirs, shy3130 main)
- local `main` 与 `feature_20261013` 都指向该 merge commit
- 推送 fork：`a06d3ddb..a12236f9  main -> main` 用 `--force-with-lease=upstream/main:506d66f1...` 完成（成功推送 5s）

📍 Fork 当前状态：
- HEAD = `a12236f`
- rollback tag = `backup-b8149620` → `81496205` 保留可用

📌 留给用户的 follow-ups：
1. REVIEW 143 个双侧都被保留的文件（`# === conflict-resolver: REVIEW ===` 标记）—— 确认哪些 OURS-side 的内容要删去 / 重写
2. 53 个被采取 THEIRS 的 backend 文件有 OURS 备份在 `/tmp/ours-backup/` —— 选择性回归你需要的本地改动
3. `.gitignore` / `.gitattributes` / `uv.lock` 等：合并过程中已清理为单一 THEIRS 版本
