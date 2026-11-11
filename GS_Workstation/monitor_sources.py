# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""复用工作台配置和原指标，提供对齐后的日线／分时数据入口。"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pandas as pd

import engine
import market
import resonance
import sessions
import paper

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data_cache"
BASE = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
SETTINGS = ROOT / "monitor_config.json"


def read_settings(config=None):
    if config is None:
        config = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
    if config.get("watch_policy","paper_opt_in")!="paper_opt_in":
        raise ValueError("后台只支持显式勾选的标的池")
    us, r = (config[k] for k in ("us", "resonance"))
    if (config["mode"] not in ("alert", "local_paper") or not 15 <= config["poll_seconds"] <= 14400
            or not isinstance(config["alert_signals"], list)
            or set(config["alert_signals"]) - {"G", "S"}):
        raise ValueError("模式或轮询间隔无效")
    if (us["scope"] not in ("core", "full") or us["regular_feed"] not in ("yahoo", "sip") or us["bar_minutes"] not in (5, 15)
            or not 3 <= us["lookback_days"] <= 30 or not 2 <= us["volume_window"] <= 120
            or not 0 <= us["data_lag_minutes"] < us["max_age_minutes"] <= 60
            or set(us["full_volume_min"]) != set(sessions.SESSIONS)
            or min(us["full_volume_min"].values()) < 1
            or us["full_volume_min"]["Overnight"] <= us["full_volume_min"]["Core"]
            or any(us["full_volume_min"][name] <= us["full_volume_min"]["Core"]
                   for name in ("Pre-Market", "After-Hours"))):
        raise ValueError("美股时段或成交量过滤设置无效")
    if (not 0 < r["red_min"] < r["purple_min"] <= 20
            or not 0 < r["s_activity_drop_ratio"] < 1 or r["s_flow_max"] > 0
            or not 1 <= r["s_activity_lookback"] <= 30 or not 1 <= r["obv_window"] <= 30
            or not 1 <= r["cn_aux_max_age_days"] <= 7 or not 1 <= r["notional_min"] <= 20):
        raise ValueError("共振阈值设置无效")
    live=config.get("live",{})
    if (type(live.get("interval_seconds",60)) is not int
            or not 15<=live.get("interval_seconds",60)<=3600
            or any(type(live.get(key,True)) is not bool for key in ("chart_enabled","sip_realtime"))):
        raise ValueError("实时图表刷新间隔须为 15–3600 秒")
    paper.settings(config["paper"])
    return config


def apply_daily(data, group, symbol, settings):
    if group == "CN_L2":
        data = resonance.attach_aux(data, ROOT / "authorized_aux" / f"{symbol}.csv")
    return resonance.apply(data, group, settings)


def cn_l2(symbol, settings):
    bars, _ = market.read_snapshot(CACHE, "CN_L2", symbol)
    ref = BASE["reference"]["CN"]
    benchmark, _ = market.read_snapshot(CACHE, "CN", ref["benchmark"])
    bars = market.completed(bars, "CN", BASE["data"]["close_buffer_minutes"])
    if bars.empty:
        raise ValueError("授权 L2 没有已完成日线")
    cal = benchmark.loc[bars.index[0]:bars.index[-1]].index.union(bars.index)
    view = engine.analyze(bars.reindex(cal), engine.context(benchmark, {}, BASE["signals"]),
                          "CN_L2", BASE["signals"])
    return apply_daily(view, "CN_L2", symbol, settings)


def us_fetch(symbols, settings, now):
    # 参考 ETF 只做特征输入，不作为扫描标的发出信号。
    ref = BASE["reference"]["US"]
    codes = list(dict.fromkeys([*symbols, ref["benchmark"], *ref["breadth"]]))
    frames, errors = {}, {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {code: pool.submit(sessions.load, code, settings["us"], now) for code in codes}
        for code, job in jobs.items():
            try:
                frames[code] = job.result()
            except (OSError, ValueError, KeyError) as exc:
                errors[code] = str(exc)
    return frames, errors


def us_view(symbol, settings, frames):
    ref = BASE["reference"]["US"]
    p = BASE["signals"]
    bars = frames[symbol]
    benchmark = frames[ref["benchmark"]]
    # 主盘／全时段的 VWAP 在共振层计算，原引擎先提供候选及其它因子。
    views = {code: frames[code] for code in ref["breadth"] if code in frames}
    ctx = engine.context(benchmark, views, p)
    frame = engine.analyze(bars, ctx, "US", p)
    result = resonance.apply(frame, "US", settings)
    result.attrs["coverage_note"] = bars.attrs.get("coverage_note")
    result.attrs["benchmark_latest"] = benchmark.index[-1]
    return result
