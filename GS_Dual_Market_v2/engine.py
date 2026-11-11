# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""因果计算层。A股 L2、A股量价代理、美股量价代理各有明确模式。"""
import numpy as np
import pandas as pd

LABELS = ["GS观察", "GS预警", "GS买入观察", "GS风险预警", "GS卖出观察"]
COLORS = dict(zip(LABELS, ["#60a5fa", "#facc15", "#f87171", "#fb923c", "#34d399"]))
MODES = {"US": "US_OHLCV_PROXY", "CN": "CN_OHLCV_PROXY", "CN_L2": "CN_L2"}


def trend_features(close, p):
    fast, slow = close.rolling(p["FAST"]).mean(), close.rolling(p["SLOW"]).mean()
    slope = fast - fast.shift(p["SLOPE"])
    up = (close > slow) & (fast > slow) & (slope > 0)
    down = (close < slow) & (fast < slow) & (slope < 0)
    regime = pd.Series(np.select([up, down], ["上涨", "下跌"], default="震荡"), index=close.index)
    return fast, slow, regime.where(slow.notna() & slope.notna() & close.notna())


def volatility(bars, window):
    c, h, l = bars.Close, bars.High, bars.Low
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return 100 * tr.rolling(window).mean() / c


def context(benchmark, baskets, p):
    close = benchmark.Close
    _, _, regime = trend_features(close, p)
    participation = {}
    for name, bars in baskets.items():
        member = bars.Close.reindex(close.index)
        mean = member.rolling(p["FAST"]).mean()
        participation[name] = (member > mean).where(mean.notna() & member.notna())
    trend = pd.DataFrame(participation, index=close.index)
    # 固定 ETF 样本的参与率；缺数时不把缺失成分当作下跌。
    width = trend.astype(float).mean(axis=1).where(trend.notna().sum(axis=1) >= 2)
    return pd.DataFrame(dict(Benchmark=close, MarketRegime=regime, Breadth=width,
                             BenchmarkATRPct=volatility(benchmark, p["AN"])))


def pulse(mask):
    return mask & ~mask.shift(fill_value=False)


def analyze(bars, ctx, market, p):
    d = bars.copy()
    h, l, c, v = (d[key] for key in ("High", "Low", "Close", "Volume"))
    typical = (h + l + c) / 3
    fast, slow, regime = trend_features(c, p)
    change = c.diff()
    obv_delta = np.sign(change) * v
    obv = obv_delta.fillna(0).cumsum()
    span = h - l
    pressure = ((2 * c - h - l) / span.where(span > 0)).where(span != 0, 0)
    vn, fn, dn, cn, an = (p[name] for name in ("VN", "FN", "DN", "CN", "AN"))
    rv = v / v.shift().rolling(vn).mean()
    vw = (typical * v).rolling(p["VNWP"]).sum() / v.rolling(p["VNWP"]).sum()
    notional = typical * v
    nr = notional / notional.shift().rolling(vn).mean()
    if market == "CN_L2":
        lb, ls, ab, av = (d[key] for key in ("LargeBuy", "LargeSell", "ActiveBuy", "ActiveSell"))
        l2 = np.isfinite(d[["LargeBuy", "LargeSell", "ActiveBuy", "ActiveSell"]]).all(axis=1)
        l2 &= d[["LargeBuy", "LargeSell", "ActiveBuy", "ActiveSell"]].ge(0).all(axis=1)
        l2 &= (lb + ls > 0) & (ab + av > 0) & c.notna()
        raw = (100 * (lb - ls) / (lb + ls).clip(lower=1)).where(l2)
        active = (100 * (ab - av) / (ab + av).clip(lower=1)).where(l2)
        flow = raw.rolling(fn).mean()
        valid_flow = l2.rolling(fn + dn).sum().eq(fn + dn)
    elif market == "CN":
        # A股量价对照使用成交量加权涨跌方向，沿用 L2 规则框架。
        raw = 100 * np.sign(change)
        flow = (raw * v).rolling(fn).sum() / v.rolling(fn).sum()
        active = 100 * pressure
        valid_flow = flow.shift(dn).notna()
    else:
        # 美股方向来自收盘位置，整根 K 线估算名义金额不能识别单笔成交。
        raw = 100 * pressure
        flow = (raw * v).rolling(fn).sum() / v.rolling(fn).sum()
        active = 100 * pressure
        valid_flow = flow.shift(dn).notna()

    atr = volatility(d, an)
    atr_before = atr.shift().rolling(vn).mean()
    shock = (atr >= p["ATH"]) & (atr >= atr_before * p["ASH"])
    support, resistance = l.shift().rolling(dn).min(), h.shift().rolling(dn).max()
    div = (c <= c.shift(dn)) & (flow > flow.shift(dn))
    neg_div = (c >= c.shift(dn)) & (flow < flow.shift(dn))
    jump = flow.diff()
    ctx = ctx.reindex(c.index)
    ratio = c / ctx.Benchmark
    relative = 100 * (ratio / ratio.shift(p["RN"]) - 1)
    efficiency = (c - c.shift(vn)).abs() / change.abs().rolling(vn).sum().replace(0, np.nan)
    # 硬性历史窗口在同一份配置中推导，通达信公式使用同一数值。
    needed = history(p)
    ready = c.notna().rolling(needed).sum().eq(needed) & valid_flow
    nearby = l <= support * (1 + p["NP"] / 100)
    watch = ready & nearby & (jump > 0)
    activity = rv >= p["VREL"]
    if market == "US":
        activity &= (nr >= p["NREL"]) & (pressure >= p["CP"])
    warn = watch & (jump >= p["JUMP"]) & (activity | div)
    falling_pressure = (c < fast) & (flow < 0)
    risk = ready & (shock | (falling_pressure & ((rv >= p["VREL"]) | neg_div | regime.eq("下跌"))))
    recent_risk = risk.rolling(cn + 1, min_periods=1).max().eq(1)
    sell = ready & recent_risk & (c < support) & (c < slow) & (flow < 0) & (active < 0)
    recent_warn = warn.rolling(cn + 1, min_periods=1).max().eq(1)
    buy = ready & recent_warn & ~risk & ~sell & (c > h.shift()) & (flow > 0) & (atr <= p["ACAP"])
    if market == "US":
        buy &= ((c > vw) & (c > fast) & (change > 0)
                & (obv > obv.shift(p["RN"])) & (relative >= p["RS"])
                & (ctx.Breadth >= p["BMIN"]))
    else:
        buy &= (c > fast) & (active >= p["APMIN"])

    # 边沿事件仅输出一个标签。候选记忆和状态仍保留。
    raw_flags = [watch, warn, buy, risk, sell]
    flags = [pulse(flag) for flag in raw_flags]
    signal = np.select(flags[::-1], LABELS[::-1], default="")
    return d.assign(
        Signal=signal, Ready=ready, Mode=MODES[market], Trend=regime, Fast=fast, Slow=slow,
        Efficiency=efficiency, RelativeVolume=rv, NotionalRatio=nr, FlowRaw=raw, Flow=flow,
        ActivePressure=active, VWAP=vw, OBV=obv, Support=support, Resistance=resistance,
        ShortMomentum=100 * (c / c.shift(dn) - 1),
        MediumMomentum=100 * (c / c.shift(p["MED"]) - 1),
        LongMomentum=100 * (c / c.shift(p["LONG"]) - 1),
        PositiveDivergence=div, NegativeDivergence=neg_div, ATRPct=atr,
        ShockState=shock & ready, RelativeStrength=relative, Breadth=ctx.Breadth,
        MarketRegime=ctx.MarketRegime, BenchmarkATRPct=ctx.BenchmarkATRPct, RiskState=risk, SellState=sell,
    )


def history(p):
    return max(p["LONG"] + 1, p["MED"] + 1, p["SLOW"], p["FAST"] + p["SLOPE"],
               p["VN"] + p["AN"] + 1, p["DN"] + p["FN"] + 1, p["RN"] + 1, p["VNWP"])


def params_for(base, factors):
    jump, vol = factors
    return {**base, "JUMP": base["JUMP"] * jump, "VREL": base["VREL"] * vol}
