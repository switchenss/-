# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""将截图的三项状态映射到本地因子，保留原 GS 指标和原始事件。"""
import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

import engine

G, S = engine.LABELS[2], engine.LABELS[4]
AI_COLORS = {"紫": "#e438fa", "红": "#ff3434", "绿": "#37d335"}


def attach_aux(data, path: Path):
    fields = ["SuspectedBuy", "SuspectedSell", "SmallBuy", "SmallSell"]
    if not path.is_file():
        return data.assign(**{name: np.nan for name in fields}, AuxDisjoint=False)
    blob = path.read_bytes()
    raw = pd.read_csv(BytesIO(blob), encoding="utf-8-sig", index_col="Date")
    raw.index = pd.to_datetime(raw.index, format="%Y-%m-%d")
    if raw.index.has_duplicates or not {"SuspectedBuy", "SuspectedSell"}.issubset(raw.columns):
        raise ValueError("授权暗盘代理 CSV 需要唯一日期和 SuspectedBuy/SuspectedSell")
    for field in fields:
        if field not in raw:
            raw[field] = np.nan
    aux = raw[fields].apply(pd.to_numeric, errors="coerce")
    meta = path.with_suffix(".json")
    details = json.loads(meta.read_text(encoding="utf-8-sig")) if meta.is_file() else {}
    disjoint = (details.get("disjoint_with_large") is True
                and details.get("csv_sha256") == sha256(blob).hexdigest())
    return data.join(aux.where(np.isfinite(aux) & aux.ge(0))).assign(AuxDisjoint=disjoint)


def ratio(base, window, sessions=None):
    if sessions is None:
        denominator = base.shift().rolling(window).mean()
    else:
        denominator = base.groupby(sessions).transform(lambda values: values.shift().rolling(window).mean())
    return base / denominator.where(denominator > 0)


def apply(data, group, settings):
    q, us = settings["resonance"], settings["us"]
    c, h, l, v = (data[k] for k in ("Close", "High", "Low", "Volume"))
    typical = (c + h + l) / 3
    notional = typical * v
    window = us["volume_window"]
    session = data["Session"] if "Session" in data else None
    relvol = ratio(v, window, session)
    relamt = ratio(notional, window, session)
    score = (relvol + relamt) / 2
    level = pd.Series(np.select([score >= q["purple_min"], score >= q["red_min"]],
                                ["紫", "红"], default="绿"), index=c.index).where(score.notna())
    amount_unit = "比率"
    if group == "CN_L2":
        visible = (data.LargeBuy - data.LargeSell).where(data.LargeBuy.ge(0) & data.LargeSell.ge(0))
        hidden = data.SuspectedBuy - data.SuspectedSell
        small = data.SmallBuy - data.SmallSell
        vwap = data.VWAP
        amount_unit = "元"
    elif group == "US":
        visible = data.FlowRaw
        small = pd.Series(np.nan, index=c.index)
        if session is None:
            vwap = data.VWAP
            obv_change = data.OBV.diff(q["obv_window"])
        else:
            periods = [data.TradeDate, session]
            vwap = (typical * v).groupby(periods).cumsum() / v.groupby(periods).cumsum()
            # 使用同一交易时段内的 OBV 改变量，避免盘后第一根与主盘尾盘直接比较。
            signed = np.sign(c.groupby(periods).diff()) * v
            obv_change = signed.groupby(periods).transform(
                lambda values: values.rolling(q["obv_window"]).sum())
        spread = (c - vwap) / vwap
        buy_proxy = (obv_change > 0) & (spread > 0) & (
            (relamt >= q["notional_min"]) | data.PositiveDivergence)
        sell_proxy = (obv_change < 0) & (spread < 0) & (
            (relamt >= q["notional_min"]) | data.NegativeDivergence)
        hidden = (100 * spread).where(buy_proxy | sell_proxy, 0).where(obv_change.notna() & vwap.notna())
    else:
        visible = data.FlowRaw
        hidden = small = pd.Series(np.nan, index=c.index)
        vwap = data.VWAP
    volume_ok = pd.Series(True, index=c.index)
    if session is not None and us["scope"] == "full":
        volume_ok = relvol.ge(session.map(us["full_volume_min"]))
    go = data.BuyState & level.isin(["红", "紫"]) & hidden.gt(0) & volume_ok
    peak = score.shift().rolling(q["s_activity_lookback"]).max()
    activity_weakens = level.eq("绿") | (score <= peak * q["s_activity_drop_ratio"])
    stop = data.SellState & activity_weakens & hidden.lt(q["s_flow_max"]) & volume_ok
    # 行情中断时可产生新的边沿；后台额外检查时效和最新 bar 去重。
    strict = np.select([engine.pulse(stop), engine.pulse(go)], [S, G], default="")
    labels = data.Signal.where(~data.Signal.isin([G, S]), "")
    labels = labels.where(strict == "", strict)
    # 汇总只在真实同单位分项均可用时展示，不把散户设为主力的机械相反数。
    total = (visible + hidden).where(data.AuxDisjoint) if group == "CN_L2" else pd.Series(np.nan, index=c.index)
    return data.assign(LegacySignal=data.Signal, Signal=labels, Resonance=strict, ActivityScore=score,
                       ActivityLevel=level, ActivityWeakens=activity_weakens, VisibleFlow=visible, HiddenProxy=hidden, RetailFlow=small,
                       MainFlow=total, SessionRelVolume=relvol, VWAP=vwap, FlowUnit=amount_unit)
