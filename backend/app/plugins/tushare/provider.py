"""Tushare 数据源插件 (项目 plugin 机制, 不需要独立 HTTP 服务).

通过 backend/app/plugins/tushare/plugin.yaml 注册:
  - name: tushare
  - datasets: [instruments, daily, adj_factor, minute, financial]
  - realtime 不支持 (Tushare 没有 A 股实时快照 API, 自动回退 TickFlow)

Token 来源 (按优先级):
  1. secrets.json (设置页「数据源卡片」输入)  ← secrets_store.get_env_backed_secret
  2. .env / 环境变量 TUSHARE_TOKEN
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import polars as pl
import tushare as ts

from app.data_providers.base import AssetType, ProviderCapabilities

logger = logging.getLogger(__name__)

# ---- 配置常量 ----
API_KEY_ENV = "TUSHARE_TOKEN"
SECRETS_FIELD = "tushare_api_key"
SOURCE_NAME = "tushare"

# ---- 单位约定 ----
# Tushare vol 单位: 股. 项目日K契约: 手 (= 100 股). 一律 floor(股/100).
# Tushare amount 单位: 元. 与项目一致, 透传.
# Tushare adj_factor: 复权因子, 透传 (项目内 indicators.pipeline 自合成复权价).


@dataclass(frozen=True)
class _TushareProviderConfig:
    """最小 config 包装: 仅满足 loader.provider_has_dataset 检查 (它读 .config.datasets).

    真数据能力通过 TushareProvider.capabilities (ProviderCapabilities) 声明,
    本 dict 镜像 capabilities 的字段 (datasets=True/False 不重要, key 在不在才重要).
    """
    datasets: frozenset[str] = frozenset({"instruments", "daily", "adj_factor", "minute", "financial", "dividend"})


_FREQ_MAP = {"1m": "1MIN", "5m": "5MIN", "15m": "15MIN", "30m": "30MIN", "60m": "60MIN"}


# ---- 模块级 helpers ----
def get_api_key() -> str:
    """读 Tushare token: secrets.json 优先 → 环境变量 → .env (uvicorn --env-file 已注入)."""
    from app import secrets_store
    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def _pro():
    """懒加载 Tushare pro 实例; token 缺失时抛 503 类错误, 让上层处理."""
    token = get_api_key()
    if not token:
        raise RuntimeError("TUSHARE_TOKEN 未配置 (secrets.json / .env / 环境变量)")
    return ts.pro_api(token)


def _ts_safe(func, **kwargs):
    """调 Tushare 方法, 异常转 logger.warning + 返回空 DataFrame (不阻断上层).

    tushare SDK 的方法其实是 functools.partial (没有 __name__), 用 getattr 安全取名.
    """
    func_name = getattr(getattr(func, "func", func), "__name__", repr(func))
    try:
        df = func(**kwargs)
        return df if df is not None else None
    except Exception as e:
        logger.warning("Tushare %s(%s) 失败: %s", func_name, kwargs, e)
        return None


# ---- 限速处理 ----
# Tushare 限速错误关键字 (免费档 ~200次/分, 付费档档位不同但仍有上限);
# 命中时 sleep 一个完整限速窗口再重试, 避免 1~2s 短退避后立刻再次被拒。
_RATE_LIMIT_PATTERNS = ("频率超限", "rate limit", "too many requests", "超出限制", "访问频率")
# 默认与 Tushare 限速窗口一致 (1 分钟), 避免短退避反复触发同一限频
RATE_LIMIT_COOLDOWN_S = 30.0  # 半窗: 缩短等待, 接受可能被二次限速的少量重试 (¥0/低成本)
# 单次调用最多重试次数 (限速错误单独计; 非限速异常不重试, 由调用方决策)
RATE_LIMIT_MAX_RETRIES = 3

# ---- 进程级 RPM 守卫 (前端节流, 与 _ts_call_with_backoff 的限速重试互补) ----
# 限额: 免费档 200 次/分, 80% 安全预算 = 160 rpm。
# 同步路径单次要拉 5000+ 只股票 + adj_factor, 一分钟自然撞限; 这里用
# 滑动窗口队列让每个调用间距 ~375ms, 整窗口持续在限内。
# 注意: 这是进程级守卫 — 多 worker 不会跨进程共享; 当前 kline_sync 走主线程足够。
import threading as _threading
_RPM_LIMIT = 160
_RPM_WINDOW_S = 60.0
_rpm_lock = _threading.Lock()
_rpm_calls: list[float] = []  # 时间戳队列 (只保留窗口内的最近 N 个)


def _rpm_acquire() -> None:
    """阻塞直到下一调用窗口有可用配额。无锁版本会丢精度, 但限速不能溢出。"""
    while True:
        with _rpm_lock:
            now = time.monotonic()
            while _rpm_calls and _rpm_calls[0] <= now - _RPM_WINDOW_S:
                _rpm_calls.pop(0)
            if len(_rpm_calls) < _RPM_LIMIT:
                _rpm_calls.append(now)
                return
            sleep_for = max(0.05, _rpm_calls[0] + _RPM_WINDOW_S - now)
        time.sleep(sleep_for)


def _is_rate_limit_error(msg: str) -> bool:
    text = (msg or "").lower()
    return any(p.lower() in text for p in _RATE_LIMIT_PATTERNS)


def _ts_call_with_backoff(func, **kwargs):
    """调 Tushare 方法; 限速错误按 RATE_LIMIT_COOLDOWN_S 退避重试, 非限速异常保持原行为.

    与 _ts_safe 的区别: 限速类错误会重试最多 RATE_LIMIT_MAX_RETRIES 次, 每次 sleep
    一个限速窗口, 让被拒请求有机会跨过窗口。其他异常仍 logger.warning + 返回 None,
    不阻断上层。
    """
    _rpm_acquire()  # 前端节流, 与下方限速重试互补, 避免单次同步窗口直接撞限
    func_name = getattr(getattr(func, "func", func), "__name__", repr(func))
    last_err: Exception | None = None
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        try:
            df = func(**kwargs)
            return df if df is not None else None
        except Exception as e:
            last_err = e
            if _is_rate_limit_error(str(e)) and attempt < RATE_LIMIT_MAX_RETRIES:
                logger.warning(
                    "Tushare %s 限速 (attempt %d/%d), 退避 %.0fs 后重试: %s",
                    func_name, attempt + 1, RATE_LIMIT_MAX_RETRIES,
                    RATE_LIMIT_COOLDOWN_S, e,
                )
                time.sleep(RATE_LIMIT_COOLDOWN_S)
                continue
            break
    if last_err is not None:
        logger.warning("Tushare %s(%s) 失败: %s", func_name, kwargs, last_err)
    return None
def _date_yyyymmdd(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y%m%d")


def _date_yyyy_mm_dd(s) -> str | None:
    """Tushare 返回 YYYYMMDD (str/int); 项目期望 YYYY-MM-DD; 已是该格式则原样."""
    if s is None or s == "":
        return None
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s  # 已是 YYYY-MM-DD 或其它


def _df_to_polars_records(df: pd.DataFrame) -> list[dict]:
    """pandas DataFrame → list[dict], NaN → None."""
    if df is None or df.empty:
        return []
    return df.replace({pd.NA: None, float("nan"): None}).to_dict("records")


def _records_to_df(records: list[dict]) -> pl.DataFrame:
    if not records:
        return pl.DataFrame()
    return pl.from_dicts(records)


def _df_empty(df) -> bool:
    """统一 ``DataFrame/None`` 的"是否为空"判定, 避免裸 ``bool(df)`` 抛 ValueError.

    Polars DataFrame 的 ``__bool__`` 自 1.0 起抛 ``ValueError: the truth value of a
    DataFrame is ambiguous`` (与 pandas 行为不同, 后者 empty=True 时返回 False)。
    历史上偶发的 ``provider tushare get_instruments 失败: the truth value of a
    DataFrame is ambiguous`` 就是因为调用方某处 ``df or []`` 触发了它。
    """
    if df is None:
        return True
    is_empty_attr = getattr(df, "is_empty", None)
    if callable(is_empty_attr):
        try:
            return bool(is_empty_attr())
        except Exception:
            pass
    return bool(getattr(df, "empty", True))


def availability() -> tuple[bool, str]:
    """loader 启动自检: token 已配置才注册为可切换. 不抛异常."""
    if get_api_key():
        return True, "ok"
    return False, f"缺少 API Key(可在下方输入框直接填写,或配置环境变量 {API_KEY_ENV})"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Key 实探一次 stock_basic(取 1 只, 验证鉴权). 不落盘."""
    try:
        pro = ts.pro_api(api_key, timeout=10.0)
        df = pro.stock_basic(list_status="L", limit=1)
        if df is None or df.empty:
            return False, "Tushare 返回空 (key 可能有效但无数据)"
        return True, "ok"
    except Exception as e:
        return False, f"探测失败: {e}"


# ---- Provider ----
class TushareProvider:
    """Tushare 数据源 — 项目内 Python 插件 (无需独立 HTTP 服务).

    capabilities 用 Project 内 ProviderCapabilities (声明可提供哪些数据集).
    config 最小包装: 仅满足 loader.provider_has_dataset 的 .config.datasets 检查.
    """

    name = SOURCE_NAME
    builtin = True
    config = _TushareProviderConfig()
    capabilities = ProviderCapabilities(
        instruments=True, daily=True, adj_factor=True,
        minute=True, realtime=False, depth5=False, financial=True,
        dividend=True,
    )

    def __init__(self) -> None:
        self._pro = None

    def _get_pro(self):
        if self._pro is None:
            self._pro = _pro()
        return self._pro

    def close(self) -> None:
        # Tushare SDK 不持有连接, 这里只是占位 (loader 在 reload 时会调 close).
        self._pro = None

    # ---- instruments ----
    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        """统一入口: stock / etf / index. 返回与项目 _flatten_instruments 兼容的结构."""
        if asset_type == "stock":
            df = _ts_call_with_backoff(self._get_pro().stock_basic, list_status="L",
                          fields="ts_code,symbol,name,list_date,industry")
            if df is None or df.empty:
                return pl.DataFrame()
            rows: list[dict] = []
            for r in _df_to_polars_records(df):
                ts_code = r.get("ts_code", "")
                if not ts_code or "." not in ts_code:
                    continue
                code, ex = ts_code.split(".", 1)
                rows.append({
                    "symbol": ts_code,
                    "name": r.get("name"),
                    "code": r.get("symbol") or code,
                    "exchange": ex,
                    "asset_type": "stock",
                    "source": SOURCE_NAME,
                })
            return _records_to_df(rows)

        if asset_type == "etf":
            df = _ts_call_with_backoff(self._get_pro().fund_basic, market="E",
                          fields="ts_code,name,fund_type,list_date")
            if df is None or df.empty:
                return pl.DataFrame()
            rows = []
            for r in _df_to_polars_records(df):
                # 只保留 ETF, 缺 fund_type 列时保留 (兼容旧 SDK)
                if r.get("fund_type") and r["fund_type"] != "ETF":
                    continue
                ts_code = r.get("ts_code", "")
                if not ts_code or "." not in ts_code:
                    continue
                code, ex = ts_code.split(".", 1)
                rows.append({
                    "symbol": ts_code,
                    "name": r.get("name"),
                    "code": code,
                    "exchange": ex,
                    "asset_type": "etf",
                    "source": SOURCE_NAME,
                })
            return _records_to_df(rows)

        if asset_type == "index":
            rows = []
            for market in ("SSE", "SZSE"):
                df = _ts_call_with_backoff(self._get_pro().index_basic, market=market,
                              fields="ts_code,name,category,list_date")
                if df is None or df.empty:
                    continue
                for r in _df_to_polars_records(df):
                    ts_code = r.get("ts_code", "")
                    if not ts_code or "." not in ts_code:
                        continue
                    code, ex = ts_code.split(".", 1)
                    rows.append({
                        "symbol": ts_code,
                        "name": r.get("name"),
                        "code": code,
                        "exchange": ex,
                        "asset_type": "index",
                        "source": SOURCE_NAME,
                    })
            return _records_to_df(rows)

        return pl.DataFrame()

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """日K: Tushare pro.daily 单 ts_code 调用. 字段转项目契约 (symbol/date/...)."""
        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        start = _date_yyyymmdd(start_time) or "20200101"
        end = _date_yyyymmdd(end_time) or datetime.now().strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        pro = self._get_pro()
        for i, sym in enumerate(symbols):
            df = _ts_call_with_backoff(pro.daily, ts_code=sym, start_date=start, end_date=end)
            if df is None or df.empty:
                continue
            rows = []
            for r in _df_to_polars_records(df):
                rows.append({
                    "symbol": sym,
                    "date": _date_yyyy_mm_dd(r.get("trade_date")),
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "volume": (r.get("vol") or 0) / 100.0 if r.get("vol") is not None else None,
                    "amount": r.get("amount"),
                })
            if rows:
                frames.append(_records_to_df(rows))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """除权因子. 列: symbol / trade_date / ex_factor."""
        if not symbols or asset_type != "stock":
            if on_chunk_done:
                on_chunk_done(0, len(symbols))
            return pl.DataFrame()
        start = _date_yyyymmdd(start_time) or "20200101"
        end = _date_yyyymmdd(end_time) or datetime.now().strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        pro = self._get_pro()
        for i, sym in enumerate(symbols):
            df = _ts_call_with_backoff(pro.adj_factor, ts_code=sym, start_date=start, end_date=end)
            if df is None or df.empty:
                continue
            rows = []
            for r in _df_to_polars_records(df):
                rows.append({
                    "symbol": sym,
                    "trade_date": _date_yyyy_mm_dd(r.get("trade_date")),
                    "ex_factor": r.get("adj_factor"),
                })
            if rows:
                frames.append(_records_to_df(rows))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- dividend ----
    def get_dividends(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """现金分红事件. 列: symbol / ex_date / bonus_per_share (元/股).

        Tushare pro.dividend() 字段:
          - ts_code / end_date (财年) / ann_date / ex_date (除权日) /
            cash_div ("10股派 X元" → 元/股 = X/10) / record_date / pay_date

        start_date/end_date 参数实际过滤的是 end_date (财年, 不是除权日);
        但本接口语义约定"按 ex_date 在 [start_time, end_time] 范围内返回",
        所以下面用 ann_date 范围 (略宽) 拉取后客户端按 ex_date 再过滤.

        注: 5000+ 只票 × 1 次调用 = 5900 次/单窗口, 单窗口 30s+ (受 200 rpm 限制),
        上层应做月度/季度增量缓存, 不要高频实时拉.
        """
        if not symbols or asset_type != "stock":
            if on_chunk_done:
                on_chunk_done(0, len(symbols))
            return pl.DataFrame()
        # ann_date 范围: 略宽于 ex_date 范围 (除权日 = 公告日 + 几周到几个月)
        end_d = end_time.date() if isinstance(end_time, datetime) else end_time
        start_d = start_time.date() if isinstance(start_time, datetime) else start_time
        # 向前推 1 年 (ex_date 通常晚于 ann_date 几周到半年)
        ann_start = (start_d - timedelta(days=365)) if start_d else None
        ann_start_s = _date_yyyymmdd(ann_start) or "19900101"
        ann_end_s = _date_yyyymmdd(end_d) or datetime.now().strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        pro = self._get_pro()
        n = len(symbols)
        # 注: Tushare pro.dividend 的 end_date 参数在 >= 20260101 时返空 (服务端的已知行为,
        # 与 start_date 的单边过滤语义不一致). 改为只用 start_date (从 1990 开始全量拉),
        # 然后在客户端按 ex_date 再裁剪到 [since, until] 窗口. 反正 ann_date 范围兜底宽于
        # ex_date 一年, 拉全量历史也不会显著增加耗用.
        for i, sym in enumerate(symbols):
            df = _ts_call_with_backoff(
                pro.dividend, ts_code=sym, start_date=ann_start_s,
            )
            if df is None or df.empty:
                if on_chunk_done:
                    on_chunk_done(i + 1, n)
                continue
            rows = []
            for r in _df_to_polars_records(df):
                # cash_div: "10股派 X元" → 元/股 = X/10
                cd = r.get("cash_div")
                if cd is None or (isinstance(cd, float) and cd <= 0):
                    continue
                ex_d = _date_yyyy_mm_dd(r.get("ex_date"))
                if ex_d is None:
                    continue
                # 客户端按 ex_date 范围过滤 (本接口语义约定)
                if start_d and ex_d < start_d.isoformat():
                    continue
                if end_d and ex_d > end_d.isoformat():
                    continue
                rows.append({
                    "symbol": sym,
                    "ex_date": ex_d,
                    "bonus_per_share": float(cd) / 10.0,
                })
            if rows:
                frames.append(_records_to_df(rows))
            if on_chunk_done:
                on_chunk_done(i + 1, n)
        return pl.concat(frames, how="vertical_relaxed") if frames else pl.DataFrame()

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """分钟K (Tushare pro.stk_mins). 需要 10000+ 积分 + 历史分钟权限.

        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        tushare_freq = _FREQ_MAP.get(freq or "1m", "1MIN")
        start = _date_yyyymmdd(start_time) or "20200101"
        end = _date_yyyymmdd(end_time) or datetime.now().strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        pro = self._get_pro()
        for i, sym in enumerate(symbols):
            df = _ts_call_with_backoff(pro.stk_mins, ts_code=sym, freq=tushare_freq,
                          start_date=start, end_date=end)
            if df is None or df.empty:
                continue
            rows = []
            for r in _df_to_polars_records(df):
                # Tushare trade_time: YYYYMMDDHHMMSS → YYYY-MM-DD HH:MM:SS
                tt = r.get("trade_time")
                dt_str = None
                if tt:
                    s = str(tt).strip()
                    if len(s) == 14 and s.isdigit():
                        dt_str = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
                    else:
                        dt_str = s
                rows.append({
                    "symbol": sym,
                    "datetime": dt_str,
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "volume": (r.get("vol") or 0) / 100.0 if r.get("vol") is not None else None,
                    "amount": r.get("amount"),
                })
            if rows:
                frames.append(_records_to_df(rows))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- realtime (Tushare 不支持 A 股实时快照, 返回空) ----
    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        logger.info("Tushare 不支持 A 股实时快照, get_realtime 返空 (上层应回退 TickFlow)")
        return pl.DataFrame()

    # ---- depth5 (Tushare 不支持) ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        return {}
