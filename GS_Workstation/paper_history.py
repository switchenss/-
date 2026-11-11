# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""将既有信号映射为模拟 K 线。历史与实时复用同一个账本。"""
from contextlib import closing
import json
from pathlib import Path
import math

import pandas as pd

import engine
import market
import monitor_sources as sources
import paper
import resonance
import indicator_snapshot

DB = sources.ROOT / "runtime" / "virtual_usd.sqlite3"


def fx_history():
    frame, _ = market.read_snapshot(sources.CACHE, "US", market.FX)
    return frame.Close.dropna()


def fx_rate(fx, day):
    value = pd.Timestamp(day)
    prices = fx.loc[fx.index < value]
    if prices.empty or (value - prices.index[-1]).days > 7 or prices.iloc[-1] <= 0:
        raise ValueError("缺少七天内的前日 USD/CNY 行情，跳过 A 股模拟")
    return 1 / float(prices.iloc[-1])


def bar_time(row, stamp, group):
    intraday = "BarEnd" in row
    if intraday:
        start, end, day = stamp, row.BarEnd, str(row.TradeDate)
    else:
        group_zone = market.ZONES["US" if group == "US" else "CN"]
        local = stamp.tz_localize(group_zone)
        hour = market.CLOSE_HOURS["US" if group == "US" else "CN"]
        start = (stamp + pd.Timedelta(hours=9, minutes=30)).tz_localize(group_zone)
        end, day = (stamp + pd.Timedelta(hours=hour)).tz_localize(group_zone), str(local.date())
    return start,end,day,intraday


def snapshot(view,group,config):
    row=indicator_snapshot.with_day_funds(view).iloc[-1]
    _,end,day,_=bar_time(row,row.name,group)
    return indicator_snapshot.capture(row,group,end.tz_convert("UTC").isoformat(),day,config)


def as_bar(row, stamp, group, symbol, settings, fx=None, captured=None):
    start,end,day,intraday=bar_time(row,stamp,group)
    captured=captured if captured is not None else indicator_snapshot.capture(
        row,group,end.tz_convert("UTC").isoformat(),day,settings)
    hidden = float(row.HiddenProxy) if pd.notna(row.HiddenProxy) else None
    atr = float(row.ATRPct * row.Close / 100) if pd.notna(row.ATRPct) and row.ATRPct > 0 else None
    code = "G" if row.Resonance == resonance.G else "S" if row.Resonance == resonance.S else ""
    return paper.Bar(f"{group}/{symbol}", start.tz_convert("UTC").isoformat(), end.tz_convert("UTC").isoformat(),
                     day, float(row.Open), float(row.High), float(row.Low), float(row.Close),
                     atr, 1. if group == "US" else fx_rate(fx, day), code, hidden,
                     str(row.ActivityLevel) if pd.notna(row.ActivityLevel) else "",
                     row.LegacySignal == resonance.G, settings["us"]["bar_minutes"] if intraday else 0,
                     captured)


def live_bar(view, group, symbol, config, now, fx=None, captured=None):
    row, stamp = view.iloc[-1], view.index[-1]
    if not math.isfinite(row.Close):
        return None
    bar = as_bar(row, stamp, group, symbol, config, fx, captured)
    if group == "US":
        if stamp != view.attrs.get("benchmark_latest"):
            return None
        if not 0 <= (now - pd.Timestamp(bar.end)).total_seconds() <= config["us"]["max_age_minutes"] * 60:
            return None
    elif (now.tz_convert("Asia/Shanghai").date() - stamp.date()).days > config["resonance"]["cn_aux_max_age_days"]:
        return None
    return bar


def daily_view(group, code, config):
    if group == "CN_L2":
        return sources.cn_l2(code, config)
    ref = sources.BASE["reference"][group]
    p = sources.BASE["signals"]
    bars, meta = market.read_snapshot(sources.CACHE, group, code)
    benchmark, reference = market.read_snapshot(sources.CACHE, group, ref["benchmark"])
    if meta["provider"] != reference["provider"]:
        raise ValueError(f"{code} 与基准数据源不一致")
    width = {}
    for key in ref["breadth"]:
        try:
            frame, detail = market.read_snapshot(sources.CACHE, group, key)
            if detail["provider"] == reference["provider"]:
                width[key] = frame
        except (OSError, ValueError, KeyError):
            continue
    bars = market.completed(bars, group, sources.BASE["data"]["close_buffer_minutes"])
    data = engine.analyze(bars, engine.context(benchmark, width, p), group, p)
    return sources.apply_daily(data, group, code, config)


def replay(selected, start, end, config):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if pd.isna(start) or pd.isna(end) or start.tz is not None or end.tz is not None or start > end:
        raise ValueError("请输入有效的起止日期 YYYY-MM-DD")
    warnings, bars, used = [], [], []
    try:
        fx = fx_history() if any(group.startswith("CN") for group, _ in selected) else None
    except (ValueError, OSError, KeyError):
        fx = None
    for group, symbol in dict.fromkeys(selected):
        try:
            if group.startswith("CN") and fx is None:
                raise ValueError("缺少 USD/CNY 缓存")
            data = daily_view(group, symbol, config).loc[start:end].dropna(subset=["Open","High","Low","Close"])
            if data.empty:
                raise ValueError("区间无日线缓存")
            data = indicator_snapshot.with_day_funds(data)
            series = []
            missing_fx = 0
            for stamp, row in data.iterrows():
                try:
                    series.append(as_bar(row, stamp, group, symbol, config, fx))
                except ValueError:
                    missing_fx += 1
            if missing_fx:
                warnings.append(f"{group}/{symbol} 有 {missing_fx} 根日线因汇率缺失跳过")
            if series:
                used.append(f"{group}/{symbol}")
                bars.extend(series)
        except (ValueError, OSError, KeyError) as exc:
            warnings.append(f"{group}/{symbol}: {exc}")
    if not bars:
        raise ValueError("没有可用的回测行情。 " + "；".join(warnings))
    with closing(paper.Paper()) as store:
        store.run(bars, config["paper"])
        return {"account":store.report(), "entries":store.entries(),
                "scope":f"日线回测 {start.date()} 至 {end.date()}",
                "bars":len(bars), "symbols":used, "warnings":warnings, "parameters":config["paper"]}
