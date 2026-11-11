# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""日线数据、校验和可复现快照。OHLCV 与 L2 是明确区分的来源。"""
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import time
from tempfile import NamedTemporaryFile
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

BAR = ["Open", "High", "Low", "Close", "Volume"]
L2 = ["LargeBuy", "LargeSell", "ActiveBuy", "ActiveSell"]
ZONES = {"US": "America/New_York", "CN": "Asia/Shanghai"}
CLOSE_HOURS = {"US": 16, "CN": 15}
FX = "CNY=X"


def canonical(symbol):
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.=\-]{0,15}", symbol):
        raise ValueError("标的代码格式无效")
    return symbol


def query(host, params, timeout):
    url = host + "?" + urlencode(params)
    last = None
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last = type(exc).__name__
            if attempt < 2:
                time.sleep(attempt + 1)
    # URL 中可能包含 API key，因此只记录主机/路径和错误类型。
    raise ValueError(f"{host} 请求失败（{last}）")


def yahoo_data(symbol, start, end, timeout):
    # Yahoo chart 是公开可读的端点，接口契约可能变动。
    first = int(pd.Timestamp(start, tz="UTC").timestamp())
    last = int(pd.Timestamp(end, tz="UTC").timestamp()) + 86400
    raw = query(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                dict(period1=first, period2=last, interval="1d", events="div,splits"), timeout)
    return parse_yahoo(raw, symbol)


def parse_yahoo(raw, symbol):
    try:
        result = raw["chart"]["result"][0]
        tz = result["meta"]["exchangeTimezoneName"]
        dates = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(tz)
        quotes = result["indicators"]["quote"][0]
        frame = pd.DataFrame({name: quotes[name.lower()] for name in BAR},
                             index=dates.tz_localize(None).normalize())
        # 指标和回测均使用供应商总回报调整序列；保留可读的参考原价。
        frame["RawClose"] = frame["Close"]
        adjusted = result["indicators"]["adjclose"][0]["adjclose"]
        factor = pd.Series(adjusted, index=frame.index) / frame["Close"]
        frame[BAR[:4]] = frame[BAR[:4]].mul(factor, axis=0)
        return frame
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError(f"Yahoo {symbol} 日线响应缺少完整 OHLCV 或调整因子") from None


def eastmoney_data(symbol, start, end, timeout):
    if not re.fullmatch(r"[01365]\d{5}", symbol):
        raise ValueError("东方财富接入仅支持本项目的沪深六位代码")
    exchange = int(symbol.startswith(("6", "5")))
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    raw = query(url, dict(secid=f"{exchange}.{symbol}", klt=101, fqt=1,
                          beg=start.replace("-", ""), end=end.replace("-", ""),
                          fields1="f1,f2,f3,f4,f5,f6", fields2="f51,f52,f53,f54,f55,f56,f57"), timeout)
    return parse_eastmoney(raw, symbol)


def parse_eastmoney(raw, symbol):
    try:
        frame = pd.DataFrame([line.split(",")[:7] for line in raw["data"]["klines"]],
                             columns=["Date", "Open", "Close", "High", "Low", "Volume", "Amount"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"东方财富 {symbol} 未返回有效日线") from None
    return frame.set_index("Date")[BAR + ["Amount"]]


def alpha_data(symbol, timeout):
    key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
    if not key:
        raise ValueError("Alpha Vantage 需要环境变量 ALPHA_VANTAGE_API_KEY")
    raw = query("https://www.alphavantage.co/query",
                dict(function="TIME_SERIES_DAILY", symbol=symbol, outputsize="compact", apikey=key),
                timeout)
    fields = ["1. open", "2. high", "3. low", "4. close", "5. volume"]
    try:
        result = pd.DataFrame.from_dict(raw["Time Series (Daily)"], orient="index")
        return result[fields].set_axis(BAR, axis=1)
    except (TypeError, KeyError, ValueError):
        raise ValueError(f"Alpha Vantage {symbol} 返回无效，请检查额度或代码") from None


def yfinance_data(symbol, start, end, timeout):
    import yfinance as yf

    frame = yf.download(symbol, start=start, end=str(pd.Timestamp(end).date() + timedelta(days=1)),
                        interval="1d", auto_adjust=False, ignore_tz=True,
                        multi_level_index=False, progress=False, threads=False, timeout=timeout)
    if frame is None or "Adj Close" not in frame:
        raise ValueError(f"yfinance {symbol} 无日线调整因子")
    factor = frame["Adj Close"] / frame["Close"]
    frame["RawClose"] = frame["Close"]
    frame[BAR[:4]] = frame[BAR[:4]].mul(factor, axis=0)
    return frame[BAR + ["RawClose"]]


def clean(frame, symbol, *, fx=False, need_l2=False):
    required = BAR + (L2 if need_l2 else [])
    if frame.empty or not set(required).issubset(frame.columns):
        raise ValueError(f"{symbol} 缺少行情或必需字段 {required}")
    frame = frame.copy()
    dates = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="coerce"))
    if dates.isna().any() or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ValueError(f"{symbol} 需要无时区的交易日日期标签")
    if dates.has_duplicates:
        raise ValueError(f"{symbol} 有重复交易日")
    frame.index = dates
    frame.index.name = "Date"
    frame = frame.sort_index()
    fields = required + [key for key in ("Amount", "RawClose") if key in frame]
    frame = frame[fields].apply(pd.to_numeric, errors="coerce").astype(float)
    o, h, l, c, v = (frame[name] for name in BAR)
    eps = h.abs() * 1e-8
    valid = (np.isfinite(frame[BAR]).all(axis=1) & (l > 0) & (h + eps >= o)
             & (h + eps >= c) & (l - eps <= o) & (l - eps <= c) & (v >= 0 if fx else v > 0))
    # Yahoo FX 的成交量可能为 null；只有 FX 可以使用无成交量的价格。
    if fx:
        frame["Volume"] = frame["Volume"].fillna(0)
        valid = (np.isfinite(frame[BAR[:4]]).all(axis=1) & (l > 0) & (h + eps >= o)
                 & (h + eps >= c) & (l - eps <= o) & (l - eps <= c))
    if not valid.any():
        raise ValueError(f"{symbol} 没有通过校验的行情")
    frame.loc[~valid, fields] = np.nan
    return frame


def market_today(market, now=None):
    return (pd.Timestamp.now(tz=ZONES[market]) if now is None
            else pd.Timestamp(now).tz_convert(ZONES[market]))


def completed(frame, market, buffer_minutes, now=None):
    local = market_today(market, now)
    today = local.tz_localize(None).normalize()
    cutoff = today + pd.Timedelta(hours=CLOSE_HOURS[market], minutes=buffer_minutes)
    return frame.loc[frame.index <= today] if local.tz_localize(None) >= cutoff else frame.loc[frame.index < today]


def download(provider, symbol, start, end, timeout):
    loaders = {"yahoo": yahoo_data, "eastmoney": eastmoney_data, "yfinance": yfinance_data}
    frame = alpha_data(symbol, timeout) if provider == "alpha" else loaders[provider](symbol, start, end, timeout)
    return clean(frame, symbol, fx=symbol == FX).loc[start:end]


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write_snapshot(frame, root, market, symbol, provider):
    if frame.empty:
        raise ValueError(f"{symbol} 请求区间内无可用行情")
    root.mkdir(parents=True, exist_ok=True)
    stem = f"{market}_{canonical(symbol)}"
    # 内容寻址的 CSV 先写完，最后原子切换 JSON 指针。并发读者仍可读旧版本。
    with NamedTemporaryFile("w", encoding="utf-8", newline="", dir=root,
                            prefix=stem + "-", suffix=".tmp", delete=False) as temp:
        pending = Path(temp.name)
        frame.to_csv(temp, index_label="Date", float_format="%.12g")
    try:
        checksum = digest(pending)
        path = root / f"{stem}-{checksum}.csv"
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    meta = dict(symbol=symbol, market=market, provider=provider, rows=len(frame),
                first=str(frame.index[0].date()), last=str(frame.index[-1].date()),
                retrieved_utc=pd.Timestamp.now(tz="UTC").isoformat(), sha256=checksum,
                file=path.name, invalid_bars=int(frame.Close.isna().sum()))
    with NamedTemporaryFile("w", encoding="utf-8", dir=root, prefix=stem + "-",
                            suffix=".json.tmp", delete=False) as temp:
        pending = Path(temp.name)
        json.dump(meta, temp, ensure_ascii=False, indent=2)
    try:
        pending.replace(root / f"{stem}.json")
    finally:
        pending.unlink(missing_ok=True)
    return meta


def read_snapshot(root, market, symbol):
    stem = f"{market}_{canonical(symbol)}"
    manifest = root / f"{stem}.json"
    if not manifest.is_file():
        raise ValueError(f"缺少快照 {manifest.name}，先运行 fetch")
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    checksum = meta["sha256"]
    # 兼容早期两文件快照，且不允许清单指定其他目录的文件。
    if not re.fullmatch(r"[0-9a-f]{64}", checksum) or meta["symbol"] != symbol or meta["market"] != market:
        raise ValueError(f"{manifest.name} 清单身份无效")
    name = meta.get("file", f"{stem}.csv")
    if name not in (f"{stem}-{checksum}.csv", f"{stem}.csv"):
        raise ValueError(f"{manifest.name} 清单路径无效")
    raw = (root / name).read_bytes()
    if sha256(raw).hexdigest() != checksum:
        raise ValueError(f"{name} 与快照清单不符")
    frame = pd.read_csv(BytesIO(raw), index_col="Date")
    return clean(frame, symbol, fx=symbol == FX, need_l2=meta["provider"] == "licensed_l2"), meta


def import_l2(path, root, symbol, attested):
    if not attested:
        raise ValueError("导入 L2 必须确认你有权使用该数据")
    if not re.fullmatch(r"[036]\d{5}", symbol):
        raise ValueError("L2 导入只接受沪深股票代码")
    frame = pd.read_csv(path, encoding="utf-8-sig", index_col="Date")
    frame = clean(frame, symbol, need_l2=True)
    return write_snapshot(frame, root, "CN_L2", symbol, "licensed_l2")
