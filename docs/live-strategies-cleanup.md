# 实盘策略清理（保留 dividend_auntie）

> 状态：定稿（用户指示"删除其他的实盘，就保留这个"）
> 范围：删 `data/user_data/strategies/live/*.py` 中除 dividend_auntie.py 外的 19 个文件

## 删除清单
保留：dividend_auntie.py + _originals/ (ground truth)
删除 19 个：dividend_balanced / base / breakout / diversified / etf / growth / lowvol / momentum / monthly / quality / relative / reversal / riskoff / sector_rotation / stoploss / trailing / volsized / weekly

## 备份
移到 `data/user_data/strategies/live/.bak_cleanup_<ts>/` 而非物理删除（保险）。

## 下一步
跑真实回测（1 年）+ 出报告。
