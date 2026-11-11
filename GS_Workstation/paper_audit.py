# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""模拟日志 SQLite 迁移及分日 JSONL 投影。所有写入先和账户变更一起提交。"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")
CORE = {
    "snapshot_status": "行情未取得",
    "observed_bar_end_utc": None,
    "trade_date": None,
    "session": None,
    "signal": None,
    "display_signal": None,
    "legacy_signal": None,
    "price_currency": None,
    "open": None, "high": None, "low": None, "close": None, "volume": None,
    "atr": None, "atr_pct": None,
    "activity_score": None, "activity_level": None,
    "visible_flow_bar": None, "hidden_flow_bar": None,
    "visible_flow_day": None, "hidden_flow_day": None, "flow_basis": None,
    "main_flow": None, "retail_flow": None,
    "resonance_satisfied": None, "go_conditions": None, "stop_conditions": None,
}


def bj_time(stamp):
    return datetime.fromisoformat(stamp).astimezone(BJ)


def dump(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def initialize(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(journal)")}
    if {"audit_day", "snapshot"}.issubset(columns):
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in db.execute("PRAGMA table_info(journal)")}
        if "audit_day" not in columns:
            db.execute("ALTER TABLE journal ADD COLUMN audit_day TEXT")
        if "snapshot" not in columns:
            db.execute("ALTER TABLE journal ADD COLUMN snapshot TEXT")
        db.execute("CREATE TABLE IF NOT EXISTS journal_dirty(day TEXT PRIMARY KEY)")
        db.execute("CREATE INDEX IF NOT EXISTS journal_day_id ON journal(audit_day,id)")
        for ident, stamp in db.execute("SELECT id,ts FROM journal WHERE audit_day IS NULL").fetchall():
            day = bj_time(stamp).date().isoformat()
            db.execute("UPDATE journal SET audit_day=? WHERE id=?",(day,ident))
            db.execute("INSERT OR IGNORE INTO journal_dirty VALUES(?)",(day,))


def insert(db, stamp, symbol, category, action, detail, snapshot=None):
    day = bj_time(stamp).date().isoformat()
    if snapshot is not None:
        snapshot = {**CORE, **snapshot, "recorded_at_utc": recorded()}
    db.execute("INSERT INTO journal(ts,symbol,category,action,payload,audit_day,snapshot) VALUES(?,?,?,?,?,?,?)",
               (stamp,symbol,category,action,dump(detail),day,dump(snapshot) if snapshot is not None else None))
    db.execute("INSERT OR IGNORE INTO journal_dirty VALUES(?)",(day,))


def decode(row):
    ident, stamp, symbol, category, action, payload, context = row
    detail = json.loads(payload)
    market, _, code = symbol.partition("/")
    snapshot = {**CORE, **json.loads(context)} if context else {**CORE, "snapshot_status":"旧账本没有完整快照"}
    return {
        "version": 1, "event_id": ident, "time": stamp,
        "time_beijing": bj_time(stamp).isoformat(), "symbol": symbol,
        "market": market if code else None, "code": code or symbol,
        "category": category, "action": action,
        "signal": snapshot["signal"] or snapshot["display_signal"],
        "execution_price": detail.get("price") if category=="fill" else None,
        "realized_pnl_usd": detail.get("realized"),
        "unrealized_pnl_usd": detail.get("unrealized"),
        "snapshot": snapshot, "detail": detail,
    }


FIELDS = "id,ts,symbol,category,action,payload,snapshot"


def project(db, directory, rebuild=False):
    directory = Path(directory)
    if rebuild:
        with db:
            db.execute("INSERT OR IGNORE INTO journal_dirty SELECT DISTINCT audit_day FROM journal WHERE audit_day IS NOT NULL")
    for (day,) in db.execute("SELECT day FROM journal_dirty ORDER BY day").fetchall():
        # 一天一事务；如果文件投影失败，保留 dirty 标志，重启后按原事件 ID 重建。
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM journal_dirty WHERE day=?",(day,)).fetchone():
                continue
            directory.mkdir(parents=True,exist_ok=True)
            temp = None
            try:
                with NamedTemporaryFile("w",dir=directory,encoding="utf-8",newline="\n",delete=False) as stream:
                    temp = Path(stream.name)
                    rows=db.execute(f"SELECT {FIELDS} FROM journal WHERE audit_day=? ORDER BY id",(day,))
                    for row in rows:
                        stream.write(dump(decode(row))+"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.replace(directory/f"{day}.jsonl")
                db.execute("DELETE FROM journal_dirty WHERE day=?",(day,))
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)


def recorded():
    return datetime.now(timezone.utc).isoformat()
