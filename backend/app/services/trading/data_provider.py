"""实盘策略数据访问门面 — 把项目数据源封装成 JQData 风格的统一接口.

策略在 on_event 内通过 context.market 之外的字段访问:

  ctx.data_provider.instruments          # polars DataFrame, 列同 instruments.parquet
  ctx.data_provider.minute_close(symbols) # {symbol: 最新分钟 K 的 close}
  ctx.data_provider.latest_close(symbols) # {symbol: enriched.last_price}
  ctx.data_provider.dividends(symbols, since, until) # polars DataFrame
  ctx.data_provider.paused(symbols)       # {symbol: bool}
  ctx.data_provider.name(symbols)         # {symbol: name}
  ctx.data_provider.limit_up(symbols)     # {symbol: limit_up}
  ctx.data_provider.limit_down(symbols)   # {symbol: limit_down}
  ctx.data_provider.total_shares(symbols) # {symbol: total_shares}

设计:
  - 不引入新数据源, 复用 repo (TickflowRepository) 缓存的 instruments + K 数据.
  - dividends 通过 DividendSource Protocol 注入 (默认 CSV, 真实生产环境可换 fuyao 财报).
  - paused / name / limit_up 从 instruments.parquet 单次扫盘, 缓存到 LiveStrategyContext 内.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import polars as pl

logger = logging.getLogger(__name__)


# ── 分红源 (Protocol) ───────────────────────────────
class DividendSource(Protocol):
    """分红数据源 — 用户在真实环境实现此 Protocol (如 fuyao 财报).

    默认实现: CSVDividendSource 从 data/user_data/dividends.csv (行: symbol, ex_date,
    bonus_per_share) 读; 不存在则按最近分类平均 / 总股本生成 mock (mock 仅用于无数据环境).
    """

    def dividends(self, symbols: list[str], since: date, until: date) -> pl.DataFrame:
        """返回 {symbol, ex_date, bonus_per_share} 三列的 polars DataFrame.

        Args:
            symbols: 标的列表.
            since/until: 时间范围 (含两端).

        Returns:
            polars DataFrame; 列名严格: symbol, ex_date, bonus_per_share. 无数据返空 df.
        """


class CSVDividendSource:
    """CSV / Parquet 落盘的本地分红源.

    落盘格式: data/user_data/dividends.csv 或 dividends.parquet
    列: symbol, ex_date (YYYY-MM-DD), bonus_per_share (元/股, 现金分红)
    不存在 → 返回空 DataFrame, 上层走 proxy fallback.
    """

    def __init__(self, data_dir: Any) -> None:
        self._data_dir = data_dir

    def dividends(self, symbols: list[str], since: date, until: date) -> pl.DataFrame:
        csv = self._data_dir / "user_data" / "dividends.csv"
        pq = self._data_dir / "user_data" / "dividends.parquet"
        if pq.exists():
            df = pl.read_parquet(pq)
        elif csv.exists():
            df = pl.read_csv(csv)
        else:
            return pl.DataFrame(schema={"symbol": pl.Utf8, "ex_date": pl.Date, "bonus_per_share": pl.Float64})
        if df.is_empty():
            return df
        if "symbol" not in df.columns:
            return pl.DataFrame()
        syms = set(symbols)
        df = df.filter(pl.col("symbol").is_in(syms))
        if "ex_date" in df.columns:
            # CSV/Parquet 落盘的 ex_date 可能是 String (pandas 写的), 显式转 Date
            # 才能与 since/until (datetime.date) 比较. 无效值丢弃.
            if df.schema["ex_date"] == pl.Utf8:
                df = df.with_columns(
                    pl.col("ex_date").str.to_date("%Y-%m-%d", strict=False).alias("ex_date")
                )
                df = df.filter(pl.col("ex_date").is_not_null())
            df = df.filter((pl.col("ex_date") >= since) & (pl.col("ex_date") <= until))
        return df


class TushareDividendSource:
    """实时拉 Tushare pro.dividend 的分红源 — 默认 DividendSource.

    性能策略:
      - 持久化缓存: data/user_data/_tushare_dividends_cache.parquet (跨进程复用)
      - 进程内单例: 多个 DP 共用同一份 cache + provider, 避免重复拉数据
      - 增量拉取: 只拉 caller 传入的 symbols 中未缓存的
      - 5900 只票全量初次拉 ≈ 100 分钟 (Tushare 200 rpm); 之后每日增量只拉新增票
      - 跨次回测共用同一份缓存, 第二次几乎秒级

    Token 缺失 / plugin 未注册 → 抛 RuntimeError; 上层 fallback 到 CSVDividendSource.
    """

    _SINGLETON: TushareDividendSource | None = None

    @classmethod
    def instance(cls, **kwargs) -> TushareDividendSource:
        """进程内单例; 多次调用共享缓存. 第二次调用快 (cache 已 warm).

        调用方负责传 ``repo=`` 或 ``cache_path=``, cache 落盘到对应项目目录,
        不再用类常量绑死绝对路径。
        """
        if cls._SINGLETON is None or kwargs.get("force_refresh"):
            cls._SINGLETON = cls(**kwargs)
        return cls._SINGLETON

    def __init__(
        self,
        repo: Any | None = None,
        symbols_universe: list[str] | None = None,
        *,
        cache_path: Path | None = None,
        force_refresh: bool = False,
    ) -> None:
        self._provider = None
        self._init_error: str | None = None
        self._repo = repo
        self._symbols_universe = symbols_universe
        # 缓存路径优先级: 显式 cache_path > repo.store.data_dir > None (禁用持久缓存)
        if cache_path is not None:
            self._cache_path = cache_path
        elif repo is not None and getattr(getattr(repo, "store", None), "data_dir", None) is not None:
            self._cache_path = repo.store.data_dir / "user_data" / "_tushare_dividends_cache.parquet"
        else:
            self._cache_path = None
        self._force_refresh = force_refresh
        try:
            from app.data_providers import custom as custom_sources
            custom_sources.load_all()
            self._provider = custom_sources.get_provider("tushare")
        except Exception as e:
            self._init_error = f"Tushare plugin 未注册或加载失败: {e}"

    def dividends(self, symbols: list[str], since: date, until: date) -> pl.DataFrame:
        if self._provider is None:
            raise RuntimeError(self._init_error or "Tushare plugin 未就绪")
        # 1. 加载缓存 (首次调用) — cache_path 未注入时退化到纯实时拉取 (不落盘).
        if not hasattr(self, "_cached_df") or self._cached_df is None:
            self._cached_df = self._load_cache() if self._cache_path is not None else pl.DataFrame()
            self._cached_syms = set(self._cached_df["symbol"].to_list()) if not self._cached_df.is_empty() else set()
        # 2. 计算缺失
        missing = [s for s in symbols if s not in self._cached_syms]
        if self._force_refresh and self._cached_syms:
            missing = list(set(symbols))
        elif self._cached_df.is_empty():
            missing = self._get_universe()
            logger.info("TushareDividendSource: 缓存为空, 全 universe 拉 %d 只 (单次 ~100min)", len(missing))
        elif missing:
            logger.info("TushareDividendSource: 缓存命中 %d, 增量拉 %d (caller 未缓存的)",
                        len(self._cached_syms & set(symbols)), len(missing))
        # 3. 拉增量
        if missing:
            new_rows = self._fetch(missing)
            # 合并 (Tushare 可能返空 df, schema 不一致; 用 diagonal_relaxed 更稳)
            if new_rows is not None and not new_rows.is_empty():
                self._cached_df = pl.concat(
                    [self._cached_df, new_rows], how="diagonal_relaxed",
                ).unique(["symbol", "ex_date"])
                if self._cache_path is not None:
                    self._save_cache(self._cached_df)
            # 关键: 把 missing 加入已查集合 (即使 Tushare 返空也记),
            # 这样下次 caller 再问这 50 只就不会重复拉
            self._cached_syms.update(set(missing))
        # 4. 按 [since, until] 和 symbols 过滤
        df = self._cached_df
        if df.is_empty() or not symbols:
            return df if df is not None else pl.DataFrame()
        if "ex_date" in df.columns:
            df = df.filter((pl.col("ex_date") >= since) & (pl.col("ex_date") <= until))
        if "symbol" in df.columns:
            df = df.filter(pl.col("symbol").is_in(set(symbols)))
        return df

    def _get_universe(self) -> list[str]:
        # 优先级: 显式 universe > 注入 repo 的 instruments > 空列表.
        # 不再硬编码绝对路径, repo 由 caller (build_data_provider / StrategyDataProvider) 注入.
        if self._symbols_universe is not None:
            return list(self._symbols_universe)
        if self._repo is not None:
            try:
                inst = self._repo.get_instruments()
                syms = inst["symbol"].to_list() if "symbol" in inst.columns else []
                self._symbols_universe = syms
                return syms
            except Exception as e:
                logger.warning("TushareDividendSource: 从 repo 拿 universe 失败: %s", e)
                return []
        return []

    def _fetch(self, target_syms: list[str]) -> pl.DataFrame:
        tz = timezone(timedelta(hours=8))
        # 拉超宽窗口 (1990-今天+1年), 客户端裁剪. 因为 Tushare end_date 参数
        # 在 >= 20260101 时行为异常 (返空), 用足够宽的 start_date + 无 end_date 拉全历史.
        start_dt = datetime(1990, 1, 1, tzinfo=tz)
        end_dt = datetime.now(tz) + timedelta(days=365)
        return self._provider.get_dividends(
            symbols=target_syms,
            start_time=start_dt,
            end_time=end_dt,
            asset_type="stock",
        )

    def _load_cache(self) -> pl.DataFrame:
        if not self._cache_path.exists():
            return pl.DataFrame()
        try:
            df = pl.read_parquet(self._cache_path)
            if "ex_date" in df.columns and df.schema["ex_date"] == pl.Utf8:
                df = df.with_columns(pl.col("ex_date").str.to_date("%Y-%m-%d", strict=False).alias("ex_date"))
                df = df.filter(pl.col("ex_date").is_not_null())
            return df
        except Exception as e:
            logger.warning("TushareDividendSource: 缓存加载失败, 重置: %s", e)
            return pl.DataFrame()

    def _save_cache(self, df: pl.DataFrame) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.write_parquet(self._cache_path)
        except Exception as e:
            logger.warning("TushareDividendSource: 缓存保存失败: %s", e)

    @classmethod
    def clear_cache(cls, cache_path: Path | None = None) -> None:
        """删除缓存文件. 必须显式传 cache_path (从外面拿到的 instance._cache_path)."""
        if cache_path is None:
            raise ValueError(
                "clear_cache 需要显式 cache_path; 从 TushareDividendSource.instance()._cache_path 取"
            )
        if cache_path.exists():
            cache_path.unlink()


# ── 数据访问门面 ────────────────────────────────────
@dataclass
class StrategyDataProvider:
    """LiveStrategyContext.data_provider 实例. 构造时 snapshot 一次基础数据."""

    repo: Any                       # KlineRepository
    instruments: pl.DataFrame       # 全部 instruments (symbol 列主键)
    minute_close: dict[str, float]  # {symbol: 最新 1m close}
    paused: dict[str, bool]         # {symbol: paused}
    limit_up: dict[str, float]      # {symbol: limit_up (今日)}
    limit_down: dict[str, float]    # {symbol: limit_down (今日)}
    name: dict[str, str]            # {symbol: 名称}
    total_shares: dict[str, float]  # {symbol: 总股本 (股)}
    market_cap: dict[str, float]    # {symbol: market_cap = total_shares * last_price}

    def dividends(self, symbols: list[str], since: date, until: date, *,
                  source: DividendSource | None = None) -> pl.DataFrame:
        """读分红; 默认走 TushareDividendSource (实时拉 pro.dividend).

        Fallback 链: source 参数 > TushareDividendSource > CSVDividendSource.
        Tushare 拉取失败或 plugin 未注册时, 自动 fallback 到 CSV 快照.
        """
        if source is not None:
            return source.dividends(symbols, since, until)
        # 尝试 Tushare 实时源 (进程内单例, 跨 DP 共享 cache); 注入 repo 让 cache 落盘到项目目录.
        try:
            ts_src = TushareDividendSource.instance(repo=self.repo)
            return ts_src.dividends(symbols, since, until)
        except RuntimeError as e:
            logger.info("data_provider: Tushare 实时分红源不可用, fallback CSV: %s", e)
            return CSVDividendSource(self.repo.store.data_dir).dividends(symbols, since, until)

    def fundamentals_market_cap(self, symbols: list[str]) -> dict[str, float]:
        """市值 = 总股本 * 最新价 (JoinQuant valuation.market_cap 替代)."""
        return {s: self.market_cap.get(s, 0.0) for s in symbols}


def build_data_provider(repo: Any, quote_service: Any | None = None) -> StrategyDataProvider:
    """构造 strategy context 时调用. 一次扫盘, 缓存到 ctx 内."""
    try:
        inst = repo.get_instruments()
    except Exception as e:
        logger.warning("data_provider: 拉 instruments 失败: %s", e)
        inst = pl.DataFrame()

    if inst.is_empty():
        return StrategyDataProvider(
            repo=repo, instruments=inst,
            minute_close={}, paused={}, limit_up={}, limit_down={},
            name={}, total_shares={}, market_cap={},
        )

    symbols = inst["symbol"].to_list() if "symbol" in inst.columns else []

    # last_price 来自 quote_service 缓存
    last_price: dict[str, float] = {}
    if quote_service is not None:
        try:
            df = quote_service.get_quotes_compat()
            if not df.is_empty():
                for row in df.iter_rows(named=True):
                    sym = row.get("symbol")
                    lp = row.get("last_price") or row.get("close")
                    if sym and lp is not None:
                        last_price[sym] = float(lp)
        except Exception as e:
            logger.warning("data_provider: 拉 quotes 失败: %s", e)

    paused = {s: last_price.get(s, 0.0) <= 0 for s in symbols}
    limit_up_map: dict[str, float] = {}
    limit_down_map: dict[str, float] = {}
    name_map: dict[str, str] = {}
    shares_map: dict[str, float] = {}
    if "limit_up" in inst.columns:
        for r in inst.select(["symbol", "limit_up"]).iter_rows(named=True):
            limit_up_map[r["symbol"]] = float(r["limit_up"] or 0.0)
    if "limit_down" in inst.columns:
        for r in inst.select(["symbol", "limit_down"]).iter_rows(named=True):
            limit_down_map[r["symbol"]] = float(r["limit_down"] or 0.0)
    if "name" in inst.columns:
        for r in inst.select(["symbol", "name"]).iter_rows(named=True):
            name_map[r["symbol"]] = str(r["name"] or "")
    if "total_shares" in inst.columns:
        for r in inst.select(["symbol", "total_shares"]).iter_rows(named=True):
            shares_map[r["symbol"]] = float(r["total_shares"] or 0.0)

    # minute close: 拉一次全市场最新 1m K
    minute_close: dict[str, float] = {}
    try:
        from app.market_time import cn_today
        today = cn_today()
        if symbols:
            df = repo.get_minute_batch(symbols, today)
            if not df.is_empty():
                for r in df.sort("datetime").iter_rows(named=True):
                    sym = r.get("symbol")
                    close = r.get("close") or r.get("raw_close")
                    if sym and close is not None and close > 0:
                        minute_close[sym] = float(close)
    except Exception as e:
        logger.warning("data_provider: 拉分钟 K 失败: %s", e)

    # market_cap
    market_cap: dict[str, float] = {}
    for s in symbols:
        ts = shares_map.get(s, 0.0)
        lp = last_price.get(s) or minute_close.get(s, 0.0)
        if ts > 0 and lp > 0:
            market_cap[s] = ts * lp

    return StrategyDataProvider(
        repo=repo,
        instruments=inst,
        minute_close=minute_close,
        paused=paused,
        limit_up=limit_up_map,
        limit_down=limit_down_map,
        name=name_map,
        total_shares=shares_map,
        market_cap=market_cap,
    )


__all__ = [
    "CSVDividendSource",
    "DividendSource",
    "StrategyDataProvider",
    "TushareDividendSource",
    "build_data_provider",
]
