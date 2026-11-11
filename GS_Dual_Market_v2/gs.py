# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""GS 日线研究 CLI。无下单功能。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

import engine
import market
from report import RISK, allocation_report, price_report, research_report
from studies import ETFS, allocate, dca_simulate, deposit, event_study, gate, month_offset, order_month, summarize

ROOT = Path(__file__).resolve().parent


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def date_str(text):
    dt = pd.Timestamp(text)
    if pd.isna(dt) or dt.tz is not None or dt != dt.normalize() or str(dt.date()) != text:
        raise ValueError("日期格式须为 YYYY-MM-DD")
    return text


def validate(cfg):
    p = cfg["signals"]
    integers = "FAST SLOW SLOPE MED LONG VN FN DN CN AN VNWP RN VMODE ASIG".split()
    if any(type(p[key]) is not int or not 0 <= p[key] <= 400 for key in integers):
        raise ValueError("窗口和模式必须为 0 至 400 的整数")
    if (any(p[key] == 0 for key in integers if key not in ("CN", "VMODE", "ASIG")) or not 2 <= p["FAST"] < p["SLOW"]
            or not p["DN"] < p["MED"] < p["LONG"] or p["VMODE"] not in (0, 1)
            or not 0 <= p["ASIG"] <= 5):
        raise ValueError("窗口或模式的关系无效")
    for key in "NP JUMP VREL ACAP ASH ATH APMIN RS NREL CP BMIN LVR".split():
        if type(p[key]) not in (int, float) or not np.isfinite(p[key]):
            raise ValueError(f"{key} 必须为有限数值")
    if (min(p[k] for k in ("NP", "JUMP", "ATH")) < 0
            or min(p[k] for k in ("VREL", "ACAP", "ASH", "NREL", "LVR")) <= 0
            or not (-100 <= p["APMIN"] <= 100 and 0 <= p["CP"] <= 1 and 0 <= p["BMIN"] <= 1)):
        raise ValueError("信号阈值超出范围")
    b = cfg["backtest"]
    if not date_str(b["start"]) < date_str(b["split"]) < date_str(b["end"]):
        raise ValueError("回测日期必须按 start < split < end 排列")
    if (any(type(n) is not int or n < 1 for n in b["delays"] + b["horizons"])
            or len(b["delays"]) != len(set(b["delays"]))
            or len(b["horizons"]) != len(set(b["horizons"]))
            or not {5, 10, 20}.issubset(b["horizons"]) or 1 not in b["delays"]
            or b["profiles"].get("base") != [1.0, 1.0]
            or any(len(pair) != 2 or any(not np.isfinite(x) or x <= 0 for x in pair)
                   for pair in b["profiles"].values())):
        raise ValueError("回测需有 base、5/10/20 日和至少下一交易日执行")
    d = cfg["dca"]
    if (any(type(d[key]) is not int or d[key] < 1
            for key in ("initial_months", "later_batch_months", "max_age_days"))
            or any(type(value) not in (int, float) or not np.isfinite(value) or value < 0
                   for value in d.values())
            or not 0 <= d["max_tilt"] <= .1 or not 0 <= d["drift_band"] < .5):
        raise ValueError("ETF 参数无效，倾斜最多10个百分点")
    if (b["commission_bps"] < 0 or b["slippage_bps"] < 0 or
            not 0 <= cfg["data"]["close_buffer_minutes"] < 180 or
            not 0 < cfg["data"]["timeout_seconds"] <= 120):
        raise ValueError("费用、超时或收线缓冲无效")


def provider_for(cfg, code, group):
    # 人民币汇率使用 Yahoo，独立于股票日线接口。
    return "yahoo" if code == market.FX else cfg["data"][group]


def collect(args, cfg):
    start = date_str(args.start or cfg["backtest"]["start"])
    end = date_str(args.end or str(pd.Timestamp.now(tz="UTC").date()))
    if start > end:
        raise ValueError("下载起始日必须不晚于结束日")
    if args.source and args.market == "all":
        raise ValueError("--source 需要明确指定单个市场")
    groups = ("US", "CN") if args.market == "all" else (args.market,)
    jobs = []
    for group in groups:
        ref = cfg["reference"][group]
        symbols = [*cfg["universe"][group], ref["benchmark"], *ref["breadth"]]
        if group == "US":
            symbols += [*ETFS, market.FX]
        if args.symbols:
            symbols = [market.canonical(code) for code in args.symbols]
        jobs.extend((group, code) for code in dict.fromkeys(symbols))

    def fetch(job):
        group, code = job
        provider = args.source or provider_for(cfg, code, group)
        if code == market.FX:
            provider = "yahoo"
        if (group == "CN") != (provider == "eastmoney"):
            raise ValueError(f"{group} 不支持数据源 {provider}")
        bars = market.download(provider, code, start, end, cfg["data"]["timeout_seconds"])
        bars = market.completed(bars, group, cfg["data"]["close_buffer_minutes"])
        return market.write_snapshot(bars, args.cache, group, code, provider)

    failures = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [(job, executor.submit(fetch, job)) for job in jobs]
        for (group, code), future in futures:
            try:
                info = future.result()
                if not getattr(args, "quiet", False):
                    print(json.dumps(info, ensure_ascii=False))
            except (ValueError, OSError, ImportError) as exc:
                failures.append(f"{group}/{code}: {exc}")
    if failures:
        raise ValueError("以下快照未更新，可能仍保留此前文件\n" + "\n".join(failures))


def inputs(cache, group, cfg, cutoff):
    ref_group = "CN" if group == "CN_L2" else group
    ref = cfg["reference"][ref_group]
    benchmark, benchmark_meta = market.read_snapshot(cache, ref_group, ref["benchmark"])
    benchmark = benchmark.loc[:cutoff]
    width = {}
    metas = [benchmark_meta]
    for symbol in ref["breadth"]:
        try:
            bars, meta = market.read_snapshot(cache, ref_group, symbol)
        except (ValueError, FileNotFoundError):
            continue
        if meta["provider"] == benchmark_meta["provider"]:
            width[symbol] = bars.loc[:cutoff]
            metas.append(meta)
    if benchmark.empty:
        raise ValueError(f"{ref_group} 基准快照在指定日期之前为空")
    return benchmark, width, metas


def series(args, cfg, group, symbol, cutoff, ref, p):
    bars, meta = market.read_snapshot(args.cache, group, symbol)
    if group != "CN_L2" and ref[2][0]["provider"] != meta["provider"]:
        raise ValueError(f"{symbol} 和基准快照来自不同源，请将同市场快照存入独立目录")
    bars = bars.loc[:cutoff]
    if bars.empty:
        raise ValueError(f"{symbol} 在指定日期之前无日线")
    cal = ref[0].loc[bars.index[0]:bars.index[-1]].index.union(bars.index)
    bars = bars.reindex(cal)
    return engine.analyze(bars, engine.context(ref[0], ref[1], p), group, p), meta


def current_cutoff(group, requested, cfg):
    key = "CN" if group == "CN_L2" else group
    date = market.market_today(key)
    minutes = date.hour * 60 + date.minute
    if minutes < 60 * market.CLOSE_HOURS[key] + cfg["data"]["close_buffer_minutes"]:
        date -= pd.DateOffset(days=1)
    allowed = str(date.date())
    return min(date_str(requested), allowed) if requested else allowed


def analyze_one(args, cfg):
    code = market.canonical(args.symbol)
    cutoff = current_cutoff(args.market, args.as_of, cfg)
    ref = inputs(args.cache, args.market, cfg, cutoff)
    data, meta = series(args, cfg, args.market, code, cutoff, ref, cfg["signals"])
    base = args.out / f"{args.market}_{code}"
    data.to_csv(str(base) + ".csv", encoding="utf-8-sig")
    price_report(data, Path(str(base) + ".html"), f"{code} · {engine.MODES[args.market]}",
                 args.chart_bars)
    last = data.iloc[-1]
    age = (pd.Timestamp(cutoff) - data.index[-1]).days
    status = ("stale" if age > cfg["dca"]["max_age_days"] or ref[0].index[-1] > data.index[-1] else
              "ok" if last.Ready else "insufficient_data")
    alert = dict(symbol=code, date=str(data.index[-1].date()), status=status, provider=meta["provider"],
                 mode=last.Mode, signal=(last.Signal or None) if status == "ok" else None, risk=RISK)
    write_json(Path(str(base) + "_alert.json"), alert)
    print(json.dumps(alert, ensure_ascii=False))


def historical(args, cfg):
    options = cfg["backtest"]
    groups = ["US", "CN"] if args.market == "all" else [args.market]
    rows, manifests, timing = [], {}, {}
    from time import perf_counter

    for group in groups:
        tick = perf_counter()
        ref = inputs(args.cache, group, cfg, options["end"])
        for meta in ref[2]:
            manifests[(meta["market"], meta["symbol"])] = meta
        symbols = args.symbols or cfg["universe"]["CN" if group == "CN_L2" else group]
        for code in symbols:
            code = market.canonical(code)
            for profile, factors in options["profiles"].items():
                p = engine.params_for(cfg["signals"], factors)
                data, meta = series(args, cfg, group, code, options["end"], ref, p)
                if ref[2][0]["provider"] == "alpha":
                    raise ValueError("Alpha Vantage compact 样本不足以做默认时间分段研究")
                manifests[(group, code)] = meta
                rows += event_study(data, ref[0], market=group, symbol=code, profile=profile, cfg=options)
        timing[group] = round(perf_counter() - tick, 3)
    if not rows:
        raise ValueError("所选样本未出现信号，检查预热、时间范围和数据覆盖")
    events = pd.DataFrame(rows)
    fixed = ["mode", "profile", "phase", "delay", "horizon", "signal"]
    summary = summarize(events, fixed)
    events.to_csv(args.out / "events.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(args.out / "summary.csv", index=False, encoding="utf-8-sig")
    summarize(events, fixed + ["regime"]).to_csv(args.out / "by_regime.csv", index=False, encoding="utf-8-sig")
    summarize(events, fixed + ["symbol"]).to_csv(args.out / "by_symbol.csv", index=False, encoding="utf-8-sig")
    keep = summary.profile.eq("base") & summary.phase.eq("holdout") & summary.delay.eq(1)
    research_report(args.out / "research.html", summary.loc[keep].drop(columns=["profile", "phase", "delay"]))
    write_json(args.out / "event_manifest.json", dict(snapshots=list(manifests.values()), timings_s=timing,
               settings=options, signal_settings=cfg["signals"], references=cfg["reference"]))
    print(json.dumps(dict(event_horizon_rows=len(events), timings_s=timing, output=str(args.out))))


def dca_views(args, cfg, cutoff, allow_missing_fx=False):
    ref = inputs(args.cache, "US", cfg, cutoff)
    views, meta = {}, list(ref[2])
    for code in ETFS:
        views[code], info = series(args, cfg, "US", code, cutoff, ref, cfg["signals"])
        meta.append(info)
    try:
        fx, info = market.read_snapshot(args.cache, "US", market.FX)
    except (ValueError, OSError, KeyError):
        if not allow_missing_fx:
            raise
        fx = pd.DataFrame(columns=["Close"], index=pd.DatetimeIndex([]))
    else:
        meta.append(info)
    return views, fx.loc[:cutoff], meta


def fingerprint(cfg):
    digest = sha256()
    for name in ("engine.py", "studies.py", "market.py", "gs.py"):
        digest.update((ROOT / name).read_bytes())
    digest.update(json.dumps({name: cfg[name] for name in ("signals", "reference", "dca", "backtest")},
                             sort_keys=True).encode())
    return digest.hexdigest()


def dca_research(args, cfg):
    b, p = cfg["backtest"], cfg["dca"]
    cutoff = b["end"]
    views, fx, meta = dca_views(args, cfg, cutoff)
    split = pd.Timestamp(b["split"])
    train_start = max(pd.Timestamp(b["start"]), *(view.index[0] for view in views.values()))
    stats, paths = {}, {}
    for label, start, end in [("development", train_start, split - pd.Timedelta(days=1)),
                              ("holdout", split, cutoff)]:
        stats[label], paths[label] = {}, {}
        for policy, batch, dynamic in [("fixed_quarterly", p["later_batch_months"], False),
                                       ("dynamic_quarterly", p["later_batch_months"], True),
                                       ("fixed_monthly", 1, False)]:
            outcome, ledger, path = dca_simulate(views, fx, start, end,
                                               {**p, "later_batch_months": batch}, dynamic)
            stats[label][policy] = outcome
            paths[label][policy] = path
            ledger.to_csv(args.out / f"dca_{label}_{policy}_ledger.csv", index=False, encoding="utf-8-sig")
            path.to_csv(args.out / f"dca_{label}_{policy}_path.csv", encoding="utf-8-sig")
    held = stats["holdout"]
    min_edge = p["study_min_wealth_edge_pct"] / 100
    passed = gate(held["fixed_quarterly"], held["dynamic_quarterly"], p["study_drawdown_tolerance"], min_edge)
    stability = []
    for offset in (0, 3, 6, 9):
        date = split + pd.DateOffset(months=offset)
        if date + pd.Timedelta(days=7) >= pd.Timestamp(cutoff):
            continue
        for fee in sorted({0., p["fixed_fee_usd"], 2 * p["fixed_fee_usd"]}):
            cost_p = {**p, "fixed_fee_usd": fee}
            fixed, _, _ = dca_simulate(views, fx, date, cutoff, cost_p, False)
            dynamic, _, _ = dca_simulate(views, fx, date, cutoff, cost_p, True)
            monthly_fixed, _, _ = dca_simulate(views, fx, date, cutoff,
                                                {**cost_p, "later_batch_months": 1}, False)
            stability.append(dict(start=str(date.date()), fixed_fee_usd=fee,
                                  dynamic_minus_fixed_rmb=dynamic["ending_value_rmb"] - fixed["ending_value_rmb"],
                                  dynamic_minus_fixed_mdd_pp=100 * (dynamic["unit_drawdown"] - fixed["unit_drawdown"]),
                                  fixed_quarterly_minus_monthly_rmb=fixed["ending_value_rmb"] - monthly_fixed["ending_value_rmb"]))
    sensitivity = pd.DataFrame(stability)
    sensitivity.to_csv(args.out / "dca_sensitivity.csv", index=False, encoding="utf-8-sig")
    result = dict(qualified=passed, fingerprint=fingerprint(cfg), periods=stats, sources=meta,
                  qualification=f"留出段期末资产至少高出等额季度方案 {100*min_edge:g}%，且回撤差不超阈值",
                  disclaimer=RISK)
    write_json(args.out / "dca_study.json", result)
    allocation_report(args.out / "dca_research.html", stats, paths, sensitivity, passed)
    print(json.dumps(dict(qualified=passed, output=str(args.out))))


def number(value):
    return float(value) if value is not None and np.isfinite(value) else None


def holdings_weights(path, views, date):
    if not path:
        return None, None
    info = read_json(path)
    if date_str(info["date"]) != str(date.date()):
        return None, None
    shares = [info[k] for k in ("qqqm_shares", "vt_shares")]
    if any(type(q) not in (int, float) or not np.isfinite(q) or q < 0 for q in shares):
        raise ValueError("持仓股数须为非负有限数")
    if not sum(shares):
        return None, info
    raw = [views[s].at[date, "RawClose"] if "RawClose" in views[s] else np.nan for s in ETFS]
    if not np.isfinite(raw).all() or min(raw) <= 0:
        return None, info
    values = np.array(shares) * raw
    return float(values[0] / values.sum()), info


def valuation(path, cutoff):
    source = read_json(path) if path else {}
    result = {}
    for symbol in ETFS:
        records = source.get(symbol, [])
        records = sorted((entry for entry in records if date_str(entry["date"]) <= date_str(entry["available_date"]) <= cutoff
                          and type(entry["pe"]) in (float, int) and np.isfinite(entry["pe"])
                          and entry["pe"] > 0 and entry.get("source") and entry.get("method")),
                         key=lambda entry: entry["date"])
        if not records:
            result[symbol] = dict(status="unavailable", pe=None)
            continue
        recent = records[-1]
        comparable = [r["pe"] for r in records[:-1] if r["method"] == recent["method"]]
        result[symbol] = dict(latest=recent, status="dated_reference",
                              historical_count=len(comparable),
                              historical_median=float(np.median(comparable)) if comparable else None)
    return result


def monthly(args, cfg):
    p = cfg["dca"]
    cutoff = current_cutoff("US", args.as_of, cfg)
    calendar_date = args.as_of or str(market.market_today("US").date())
    this_month = pd.Period(args.month or calendar_date, freq="M")
    if this_month < pd.Period(args.start, freq="M"):
        raise ValueError("分析月份早于计划起始月份")
    if this_month > pd.Period(calendar_date, freq="M"):
        raise ValueError("分析月份晚于行情观察日期")
    # 默认月度建议使用上月底前可得的信息，同一月份运行不会追随当月涨跌反复变化。
    decision_date = min(pd.Timestamp(cutoff), this_month.start_time - pd.Timedelta(days=1))
    decision_cutoff = str(decision_date.date())
    refresh_issue = None
    if args.refresh:
        ref = cfg["reference"]["US"]
        request = argparse.Namespace(start=cfg["backtest"]["start"], end=decision_cutoff,
                                     source=None, market="US", cache=args.cache, quiet=True,
                                     symbols=[*dict.fromkeys([*ETFS, ref["benchmark"], *ref["breadth"], market.FX])])
        try:
            collect(request, cfg)
        except (ValueError, OSError) as exc:
            refresh_issue = str(exc)
    try:
        views, fx, meta = dca_views(args, cfg, decision_cutoff, allow_missing_fx=True)
    except (ValueError, OSError, KeyError) as exc:
        views, fx, meta = {}, pd.DataFrame(columns=["Close"]), []
        problem = str(exc)
    else:
        problem = "缺少可用汇率快照" if fx.empty else None
    status, weight, local_holdings, mid = "insufficient_or_stale", None, None, None
    fraction, rule = .5, "留出门槛、数据时效或行情口径未满足，等额投入"
    rates = fx.Close.dropna()
    fx_fresh = len(rates) > 0 and (decision_date - rates.index[-1]).days <= p["max_age_days"]
    if views:
        q, v = (views[code] for code in ETFS)
        qlast, vlast = q.index[-1], v.index[-1]
        date = min(qlast, vlast)
        synced = qlast == vlast and (decision_date - date).days <= p["max_age_days"]
        weight, local_holdings = holdings_weights(args.holdings, views, date) if synced else (None, None)
        if synced:
            mid = number(q.iloc[-1].MediumMomentum - v.iloc[-1].MediumMomentum)
        if synced and fx_fresh and q.iloc[-1].Ready and v.iloc[-1].Ready:
            status = "ok"
        evidence = read_json(args.evidence) if args.evidence else {}
        allowed = (evidence.get("qualified") is True and evidence.get("fingerprint") == fingerprint(cfg)
                   and decision_date > pd.Timestamp(cfg["backtest"]["end"])
                   and meta[0]["provider"] == "yahoo" and fx_fresh)
        if allowed and status == "ok":
            fraction, rule = allocate(q.iloc[-1], v.iloc[-1], weight, p)
    if args.evidence is None:
        rule = "未启用经回测校验的倾斜，等额投入"
    month = month_offset(this_month.start_time, pd.Period(args.start, freq="M").start_time)
    amount = deposit(month, p)
    scheduled = order_month(month, p)
    scheduled_budget = amount * (p["later_batch_months"] if month >= p["initial_months"] else 1)
    budget = scheduled_budget if scheduled else 0.
    if scheduled and local_holdings is not None and "available_rmb" in local_holdings:
        cash = local_holdings["available_rmb"]
        if type(cash) not in (int, float) or not np.isfinite(cash) or cash < 0:
            raise ValueError("可用人民币须为非负有限数")
        budget = min(budget, cash)
    q_rmb = round(budget * fraction, 2)
    etf_status = {}
    for code, view in views.items():
        row = view.iloc[-1]
        etf_status[code] = dict(date=str(view.index[-1].date()), trend=row.Trend if pd.notna(row.Trend) else None,
                               atr_pct=number(row.ATRPct), medium_momentum_pct=number(row.MediumMomentum),
                               gs_event=row.Signal or None, risk_state=bool(row.RiskState))
    output = dict(month=str(this_month), month_number=month + 1, decision_cutoff=decision_cutoff,
                  status=status, contribution_rmb=amount, buy_month=scheduled, deploy_rmb=budget,
                  schedule_action="按计划投入新增资金" if scheduled and budget else "累积新增资金或无可用现金",
                  qqqm_rmb=q_rmb, vt_rmb=round(budget - q_rmb, 2), fixed_each_rmb=round(budget / 2, 2),
                  qqqm_delta_vs_fixed_rmb=round(q_rmb - budget / 2, 2),
                  actual_qqqm_weight=weight, actual_vt_weight=None if weight is None else 1 - weight,
                  qqqm_deviation_pp=None if weight is None else 100 * (weight - .5),
                  target_each_weight=.5, etfs=etf_status, qqqm_minus_vt_momentum_pp=mid,
                  valuation=valuation(args.valuation, calendar_date), fx_reference=number(rates.iloc[-1]) if fx_fresh else None,
                  reason=rule, data_issue=problem, refresh_issue=refresh_issue, risk=RISK)
    if views and synced:
        output["benchmark_atr_pct"] = number(q.iloc[-1].BenchmarkATRPct)
        output["market_regime"] = q.iloc[-1].MarketRegime if pd.notna(q.iloc[-1].MarketRegime) else None
        output["breadth_proxy"] = number(q.iloc[-1].Breadth)
    else:
        output.update(benchmark_atr_pct=None, market_regime=None, breadth_proxy=None)
    write_json(args.out / f"dca_{this_month}.json", output)
    print(json.dumps(output, ensure_ascii=False))


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--cache", type=Path, default=Path("data_cache"))
    parser.add_argument("--out", type=Path, default=Path("gs_results"))
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="联网获取日线并留存带摘要的本地快照")
    fetch.add_argument("--market", choices=["all", "US", "CN"], default="all")
    fetch.add_argument("--start")
    fetch.add_argument("--end")
    fetch.add_argument("--source", choices=["yahoo", "yfinance", "alpha", "eastmoney"])
    fetch.add_argument("--symbols", nargs="+")
    fetch.set_defaults(run=collect)
    imp = commands.add_parser("import-l2", help="导入已获授权的沪深 L2 日线汇总")
    imp.add_argument("symbol")
    imp.add_argument("path", type=Path)
    imp.add_argument("--authorized-data", action="store_true")
    imp.set_defaults(run=lambda a, c: print(json.dumps(
        market.import_l2(a.path, a.cache, market.canonical(a.symbol), a.authorized_data), ensure_ascii=False)))
    one = commands.add_parser("analyze", help="单标的五类信号、K线及最新触发")
    one.add_argument("symbol")
    one.add_argument("--market", choices=["US", "CN", "CN_L2"], default="US")
    one.add_argument("--as-of")
    one.add_argument("--chart-bars", type=int, choices=range(10, 401), default=120, metavar="10..400")
    one.set_defaults(run=analyze_one)
    bt = commands.add_parser("backtest", help="多标的、分市场、分状态及灵敏度事件研究")
    bt.add_argument("--market", choices=["all", "US", "CN", "CN_L2"], default="all")
    bt.add_argument("--symbols", nargs="+")
    bt.set_defaults(run=historical)
    commands.add_parser("dca-study", help="以真实历史比较等额与有界倾斜").set_defaults(run=dca_research)
    dca = commands.add_parser("dca", help="计算月度投入和配置情景")
    dca.add_argument("--start", required=True, help="计划开始月份 YYYY-MM")
    dca.add_argument("--month", help="分析月份 YYYY-MM，默认当前月")
    dca.add_argument("--as-of")
    dca.add_argument("--holdings", type=Path)
    dca.add_argument("--valuation", type=Path)
    dca.add_argument("--evidence", type=Path)
    dca.add_argument("--refresh", action="store_true", help="先刷新公开行情；失败时根据本地快照和时效判断")
    dca.set_defaults(run=monthly)
    args = parser.parse_args()
    if args.command == "backtest" and args.market == "all" and args.symbols:
        parser.error("--symbols 需要明确指定一个回测市场")
    cfg = read_json(args.config)
    validate(cfg)
    args.out.mkdir(parents=True, exist_ok=True)
    args.run(args, cfg)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        cli()
    except (ValueError, OSError, ImportError, KeyError, TypeError) as exc:
        raise SystemExit(f"运行失败：{exc}") from None
