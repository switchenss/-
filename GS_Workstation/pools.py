# Copyright © 2026 Swiftchen
# GS 双市场实时看盘终端 | 作者：Swiftchen
"""兼容旧自选文件的分池管理，仅导入导出池和标的。"""
import market

DEFAULT = "默认池"
GROUPS = ("CN", "CN_L2", "US")


def name(text):
    text = text.strip()
    if not text or len(text) > 24 or any(ord(c) < 32 for c in text):
        raise ValueError("池名须为 1–24 个字符")
    return text


def normalize(state):
    names = list(dict.fromkeys(name(x) for x in state.get("pools", [DEFAULT])))
    if not names:
        names = [DEFAULT]
    rows, seen, opted = [], set(), set()
    for entry in state.get("stocks", []):
        group = entry["group"]
        if group not in GROUPS:
            raise ValueError("自选市场须为 CN、CN_L2 或 US")
        code = market.canonical(entry["code"])
        if group.startswith("CN") and (len(code) != 6 or not code.isascii() or not code.isdigit()):
            raise ValueError("A 股代码须为 6 位数字")
        pool = name(entry.get("pool", DEFAULT))
        if pool not in names:
            names.append(pool)
        ident = (pool, group, code)
        if entry.get("paper_enabled") is True:
            opted.add((group, code))
        if ident not in seen:
            seen.add(ident)
            rows.append(dict(pool=pool, group=group, code=code, enabled=entry.get("enabled") is not False,
                             name=str(entry.get("name") or "")[:160]))
    for row in rows:
        row["paper_enabled"] = (row["group"], row["code"]) in opted
    return {**state, "pools": names, "stocks": rows}


def enabled_for_monitor(state):
    rows = normalize(state)["stocks"]
    return list(dict.fromkeys((row["group"], row["code"]) for row in rows
                              if row["enabled"] and row["paper_enabled"]))


def set_participation(state, group, code, active):
    for row in state["stocks"]:
        if (row["group"], row["code"]) == (group, code):
            row["paper_enabled"] = active is True


def subgroup(group):
    return "US" if group == "US" else "CN"


def move(rows, source, target):
    """只在同池、同市场大组内重新排列。"""
    if source == target:
        return False
    a, b = rows[source], rows[target]
    if a["pool"] != b["pool"] or subgroup(a["group"]) != subgroup(b["group"]):
        return False
    row = rows.pop(source)
    rows.insert(target, row)
    return True


def merge(state, imported):
    if not isinstance(imported, dict) or not isinstance(imported.get("stocks"), list):
        raise ValueError("导入文件必须包含 stocks 数组")
    info = normalize({"pools":imported.get("pools", [DEFAULT]), "stocks":imported["stocks"]})
    # 导入可恢复自选和排序，不会擅自把陌生标的投入挂机。
    existing = {(row["group"], row["code"]): row.get("paper_enabled") is True for row in state["stocks"]}
    for row in info["stocks"]:
        row["paper_enabled"] = existing.get((row["group"], row["code"]), False)
    return normalize({**state, "pools":[*state["pools"], *info["pools"]],
                      "stocks":[*state["stocks"], *info["stocks"]]})
