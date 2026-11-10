"""
PTA 每日管線的附加計算（與 scripts/pta_analyze.py 同一套定義）：
- attach_signal：燈號、預估波幅所需資料、波動放大、當日漲跌、均線狀態、VRP／偏斜，附上回測統計（backtest/signal.json）
- vol_ind：30 天價平 IV 與最近月份 Put 偏斜
- paper_update：模擬帳本（賣方兩級、買方兩條、均線 Call），到期以標的期貨結算價計算損益
"""
import json
import math
import os
from datetime import date, timedelta

import pta_gex as pg

ROOT = os.path.join(os.path.dirname(__file__), "..")
COST = 1.0
MA_LIST = (30, 60, 100)


def vol_ind(rows, futs, S0, val):
    cs = pg.enrich(rows, futs, S0, val)
    out = {"iv30": None, "skew": None}
    by = {}
    for c in cs:
        by.setdefault(c[0], []).append(c)
    if not by:
        return out
    code = min(by, key=lambda k: abs(by[k][0][4] * 365 - 30))
    near = sorted(by[code], key=lambda c: abs(c[2] - S0))[:4]
    v = [c[5] for c in near if c[5]]
    out["iv30"] = round(sum(v) / len(v), 4) if v else None
    n1 = min({r[1] for r in rows})
    und = next(r[7] for r in rows if r[1] == n1)
    F1 = futs[und]["settle"]
    puts = [r for r in rows if r[7] == und and r[3] == "P" and r[6]]
    calls = [r for r in rows if r[7] == und and r[3] == "C" and r[6]]
    if puts and calls:
        p = min(puts, key=lambda r: abs(r[2] - F1 * 0.97))[6]
        c = min(calls, key=lambda r: abs(r[2] - F1 * 1.03))[6]
        out["skew"] = round(p - c, 4)
    return out


def bucket(S0, flip, g):
    if flip:
        return "red" if S0 < flip else "green"
    return "red" if (g is not None and g < 0) else "green"


def ma_status(prices, N=10, slope=1):
    C = [p for p in prices if p]
    out = {"vals": {}, "rising": {}, "above": {}, "breaks": []}

    def sma(n, i):
        return sum(C[i - n + 1:i + 1]) / n if i >= n - 1 else None
    i = len(C) - 1
    for n in MA_LIST:
        m, m1, ms = sma(n, i), sma(n, i - 1), sma(n, i - slope)
        if m is None:
            continue
        out["vals"][n] = round(m, 1)
        out["above"][n] = C[i] >= m
        if ms is not None:
            out["rising"][n] = m > ms
        if m1 is not None and ms is not None and i - N >= n - 1 and C[i] < m and C[i - 1] >= m1 and m > ms \
                and all(C[k] >= sma(n, k) for k in range(i - N, i)):
            out["breaks"].append(n)
    return out


def adj_prices(past):
    """主力連續價格：以每天同一合約的報酬率（ret）串接，最後一天等於當天主力收盤"""
    rets = [it.get("ret") for it in past]
    if not past or any(r is None for r in rets[1:]):
        return [it.get("close") for it in past]
    a = [1.0]
    for r in rets[1:]:
        a.append(a[-1] * (1 + r / 100))
    scale = (past[-1].get("close") or 1) / a[-1]
    return [x * scale for x in a]


def vol_status(past):
    cur = past[-1] if past else {}
    rets = [it.get("ret") for it in past[-20:] if it.get("ret") is not None]
    rv = math.sqrt(sum(x * x for x in rets) / len(rets)) * math.sqrt(252) / 100 if len(rets) >= 15 else None
    iv = cur.get("iv30")
    sk = [it.get("skew") for it in past[-60:] if it.get("skew") is not None]
    pct = (sum(1 for x in sk if x <= cur["skew"]) / len(sk)) if cur.get("skew") is not None and len(sk) >= 40 else None
    return {"iv30": iv, "rv20": round(rv, 4) if rv else None, "vrp": round(iv - rv, 4) if iv and rv else None,
            "skew": cur.get("skew"), "skewPct": round(pct, 3) if pct is not None else None}


def attach_signal(latest, seq):
    path = os.path.join(ROOT, "backtest", "signal.json")
    if not os.path.exists(path):
        return None
    table = json.load(open(path, encoding="utf-8"))
    d = latest["tradeDate"]
    past = [it for it in seq if it["date"] <= d]
    s = latest["summary"]["naive"]
    b = bucket(latest["F"], s["flip"], s["gex"])
    expand = None
    if len(past) >= 6 and past[-1].get("atr5") and past[-6].get("atr5"):
        expand = past[-1]["atr5"] / past[-1]["F"] > past[-6]["atr5"] / past[-6]["F"]
    return {"bucket": b, "label": "紅燈｜Flip 之下（負 Gamma）" if b == "red" else "綠燈｜Flip 之上（正 Gamma）",
            "flip": s["flip"], "gex": s["gex"], "expand": expand, "ret": past[-1].get("ret") if past else None,
            "ma": ma_status(adj_prices(past)), "maSrc": "PTA 主力連續", "vol": vol_status(past), "stats": table}


# ---------------- 模擬帳本 ----------------
PATH = os.path.join(ROOT, "data", "paper.json")


def trading_days(d, exp):
    n, x = 0, d
    while x < exp:
        x += timedelta(days=1)
        if x.weekday() < 5 and x.isoformat() not in pg.HOLIDAYS:
            n += 1
    return max(1, n)


def settle_pnl(e, S):
    legs = [(e["Kc"], True)] if e.get("Kc") else []
    legs += [(e["Kp"], False)] if e.get("Kp") else []
    intr = sum(max(0.0, S - K) if c else max(0.0, K - S) for K, c in legs)
    cost = COST * len(legs)
    if e.get("side") == "buy":
        return round(intr - e["debit"] - cost, 1)
    return round(e["credit"] - cost - intr, 1)


def paper_update(days, items, model, tiers):
    """days：[(d, futs, rows)]；items：{date: history item}"""
    led = json.load(open(PATH, encoding="utf-8")) if os.path.exists(PATH) else {"entries": []}
    have = {(e["date"], e["tier"]) for e in led["entries"]}
    for d, futs, rows in days:
        ds = d.isoformat()
        # 結算：到期日（或到期日資料缺漏時的下一個交易日）用標的期貨結算價
        for e in led["entries"]:
            if e.get("status") == "open" and e["exp"] <= ds and e["und"] in futs:
                S = futs[e["und"]]["settle"]
                e.update(status="closed", S=S, approx=e["exp"] != ds, pnl=settle_pnl(e, S))
        it = items.get(ds) or {}
        if not it.get("atr5") or not rows:
            continue
        exp = min(r[1] for r in rows)
        und = next(r[7] for r in rows if r[1] == exp)
        F1 = futs[und]["settle"]
        n = trading_days(d, date.fromisoformat(exp))
        S0 = it["F"]
        red = bucket(S0, it.get("naive_flip"), it.get("naive_gex")) == "red"
        E = (model["a"] + model["b"] * it["atr5"] / S0 * 100 + (model["c"] if red else 0)) / 100 * F1
        ch = [r for r in rows if r[7] == und and r[4]]
        base = {"date": ds, "und": und, "code": und, "exp": exp, "n": n, "F": F1, "light": "red" if red else "green"}
        for t in tiers:
            if (ds, t["name"]) in have:
                continue
            D = t["k"] * E * math.sqrt(n)
            ent = {**base, "tier": t["name"], "k": t["k"], "dist": round(D)}
            if red:
                led["entries"].append({**ent, "status": "skip", "reason": "紅燈不進場"})
                continue
            calls = sorted((r[2], r[4]) for r in ch if r[3] == "C" and r[2] >= F1 + D)
            puts = sorted(((r[2], r[4]) for r in ch if r[3] == "P" and r[2] <= F1 - D), reverse=True)
            if not calls or not puts:
                led["entries"].append({**ent, "status": "skip", "reason": "超出報價範圍"})
                continue
            (Kc, pc), (Kp, pp) = calls[0], puts[0]
            led["entries"].append({**ent, "status": "open", "Kc": Kc, "pc": pc, "Kp": Kp, "pp": pp, "credit": round(pc + pp, 1)})
        Ks = sorted({r[2] for r in ch if r[3] == "C"} & {r[2] for r in ch if r[3] == "P"}, key=lambda k: abs(k - F1))
        if Ks and red:
            K = Ks[0]
            pc = next(r[4] for r in ch if r[2] == K and r[3] == "C")
            pp = next(r[4] for r in ch if r[2] == K and r[3] == "P")
            if it.get("expand") and (ds, "買方跨式") not in have:
                led["entries"].append({**base, "side": "buy", "status": "open", "tier": "買方跨式", "Kc": K, "pc": pc, "Kp": K, "pp": pp,
                                       "debit": round(pc + pp, 1)})
            if (it.get("ret") or 0) < 0 and (ds, "買方Call") not in have:
                led["entries"].append({**base, "side": "buy", "status": "open", "tier": "買方Call", "Kc": K, "pc": pc, "debit": round(pc, 1)})
        if it.get("maBreaks") and (ds, "均線Call") not in have:
            calls = sorted((r[2], r[4]) for r in ch if r[3] == "C" and r[2] >= F1 * 1.01)
            if calls:
                K, pc = calls[0]
                led["entries"].append({**base, "side": "buy", "status": "open", "tier": "均線Call", "Kc": K, "pc": pc,
                                       "debit": round(pc, 1), "ma": it["maBreaks"]})
        have |= {(ds, t["name"]) for t in tiers} | {(ds, "買方跨式"), (ds, "買方Call"), (ds, "均線Call")}
    led["entries"].sort(key=lambda e: (e["date"], e["tier"]))
    led["start"] = led["entries"][0]["date"] if led["entries"] else None
    json.dump(led, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    out = {"start": led["start"], "cost": COST, "unit": "元／噸", "tiers": [], "recent": led["entries"][-40:]}
    for name, k in [(t["name"], t["k"]) for t in tiers] + [("買方跨式", None), ("買方Call", None), ("均線Call", None)]:
        es = [e for e in led["entries"] if e["tier"] == name]
        cl = [e for e in es if e["status"] == "closed"]
        p = [e["pnl"] for e in cl]
        out["tiers"].append({"name": name, "k": k, "days": len(es), "traded": sum(1 for e in es if e["status"] in ("open", "closed")),
                             "open": sum(1 for e in es if e["status"] == "open"), "closed": len(cl),
                             "skipRed": sum(1 for e in es if e.get("reason") == "紅燈不進場"),
                             "skipRange": sum(1 for e in es if e.get("reason") == "超出報價範圍"),
                             "total": round(sum(p), 1), "win": round(sum(1 for x in p if x > 0) / len(p), 3) if p else None,
                             "worst": min(p) if p else None})
    return out
