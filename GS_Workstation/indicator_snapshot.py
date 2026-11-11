# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""从已算好的指标视图提取日志快照。只向前累计，不参与信号计算。"""
import math

import pandas as pd

import resonance


def finite(value):
    try:
        value=float(value)
    except (ValueError,TypeError):
        return None
    return value if math.isfinite(value) else None


def label(value):
    return str(value) if pd.notna(value) and value!="" else None


def truth(value):
    return bool(value) if pd.notna(value) else None


def with_day_funds(view):
    if "TradeDate" not in view:
        return view.assign(AuditVisibleDay=view.VisibleFlow,AuditHiddenDay=view.HiddenProxy)
    days=view.TradeDate
    columns={}
    for key,source in (("AuditVisibleDay","VisibleFlow"),("AuditHiddenDay","HiddenProxy")):
        value=view[source]
        weight=view.Volume.where(value.notna(),0)
        total=weight.groupby(days).cumsum()
        sum_score=(value.fillna(0)*weight).groupby(days).cumsum()
        columns[key]=sum_score/total.where(total>0)
    return view.assign(**columns)


def capture(row, group, end, day, settings):
    close, atr_pct = finite(row.get("Close")), finite(row.get("ATRPct"))
    atr = close * atr_pct / 100 if close is not None and atr_pct is not None else None
    event=label(row.get("Resonance"))
    raw=label(row.get("Signal"))
    level=label(row.get("ActivityLevel"))
    hidden=finite(row.get("HiddenProxy"))
    relvol=finite(row.get("SessionRelVolume"))
    intraday="BarEnd" in row
    session=label(row.get("Session")) if intraday else "日线"
    full=intraday and settings["us"]["scope"]=="full"
    required=settings["us"]["full_volume_min"].get(session) if full else None
    volume_ok=(relvol>=required if relvol is not None and required is not None else False) if full else True
    stop_limit=settings["resonance"]["s_flow_max"]
    go_conditions={
        "trend":truth(row.get("BuyState")),"activity_red_or_purple":level in ("红","紫") if level else None,
        "fund_positive":hidden>0 if hidden is not None else None,"session_volume_pass":volume_ok,
    }
    stop_conditions={
        "trend":truth(row.get("SellState")),"activity_weakens":truth(row.get("ActivityWeakens")),
        "fund_negative":hidden<stop_limit if hidden is not None else None,"session_volume_pass":volume_ok,
    }
    def complete(conditions):
        if False in conditions.values():
            return False
        return None if None in conditions.values() else True
    go_ok,stop_ok=complete(go_conditions),complete(stop_conditions)
    return {
        "snapshot_status":"当时已收 K 线",
        "observed_bar_end_utc":end, "trade_date":day, "session":session,
        "signal":"G/GO" if event==resonance.G else "S/STOP" if event==resonance.S else None,
        "display_signal":raw, "legacy_signal":label(row.get("LegacySignal")),
        "price_currency":"USD" if group=="US" else "CNY",
        **{key.lower():finite(row.get(key)) for key in ("Open","High","Low","Close","Volume")},
        "atr":atr, "atr_pct":atr_pct,
        "activity_score":finite(row.get("ActivityScore")), "activity_level":level,
        "visible_flow_bar":finite(row.get("VisibleFlow")), "hidden_flow_bar":hidden,
        "visible_flow_day":finite(row.get("AuditVisibleDay")), "hidden_flow_day":finite(row.get("AuditHiddenDay")),
        "flow_basis":"当日已收分时棒的成交量加权量价代理" if intraday else
                     "授权 L2 与疑似拆单金额（CNY）" if group=="CN_L2" else "当日日线量价代理，分别计算",
        "main_flow":finite(row.get("MainFlow")), "retail_flow":finite(row.get("RetailFlow")),
        "resonance_satisfied":True if True in (go_ok,stop_ok) else None if None in (go_ok,stop_ok) else False,
        "go_conditions":go_conditions,"stop_conditions":stop_conditions,
        "go_satisfied":go_ok,"stop_satisfied":stop_ok,
        "market_data_ready":truth(row.get("Ready")),
        "session_volume_ratio":relvol,"session_volume_threshold":required,
        "fast_ma":finite(row.get("Fast")), "slow_ma":finite(row.get("Slow")), "vwap":finite(row.get("VWAP")),
        "obv":finite(row.get("OBV")), "support":finite(row.get("Support")),
        "resistance":finite(row.get("Resistance")), "relative_strength":finite(row.get("RelativeStrength")),
        "risk_state":truth(row.get("RiskState")), "risk_settings":settings["paper"],
    }
