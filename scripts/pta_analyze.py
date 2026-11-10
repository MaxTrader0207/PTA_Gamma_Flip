"""
PTA 回測分析（讀 backtest/daily.json，由 scripts/pta_bt.py 產生）。與台指期版本相同的規則與輸出格式：
- 燈號：主力結算價在合併 Flip 之下＝紅燈；沒有 Flip 時看總 GEX 正負
- 隔日真實波幅（同一合約、以前一日收盤為前收）、隔日漲跌、未來 5 日實現波動 ÷ 30 天價平 IV
- 預估波幅模型：隔日真實波幅% = a + b × 近 5 日 ATR% + c × 紅燈
- 賣方：綠燈日收盤以結算價賣最近到期月份的價外 Call＋Put，距離 = k × 預估波幅 × √持有交易日數，持有到期
- 買方：紅燈＋波動放大買價平跨式；紅燈＋當日下跌買價平 Call；均線（主力連續、轉倉以報酬率銜接）首次跌破且上揚時買價外 1% Call
- 波動指標：VRP（30 天價平 IV − 近 20 日實現波動）、Put 偏斜（最近月份價外 3% Put IV − 價外 3% Call IV）
鄭商所沒有公布買賣報價，成交一律用結算價，每腳扣 COST 元／噸（手續費＋滑價估計）。
輸出 backtest/signal.json、backtest/report.md。
"""
import json
import math
import os
import re
import statistics as st
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import pta_gex as pg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")
COST = 1.0
BIG = 1.5


def ols(X, y):
    k = len(X[0])
    XtX = [[sum(r[i] * r[j] for r in X) for j in range(k)] for i in range(k)]
    Xty = [sum(r[i] * v for r, v in zip(X, y)) for i in range(k)]
    A = [row[:] + [1.0 if i == j else 0.0 for j in range(k)] for i, row in enumerate(XtX)]
    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(A[r][i]))
        A[i], A[p] = A[p], A[i]
        d = A[i][i]
        A[i] = [x / d for x in A[i]]
        for r in range(k):
            if r != i:
                f = A[r][i]
                A[r] = [x - f * z for x, z in zip(A[r], A[i])]
    inv = [row[k:] for row in A]
    beta = [sum(inv[i][j] * Xty[j] for j in range(k)) for i in range(k)]
    res = [v - sum(b * x for b, x in zip(beta, r)) for r, v in zip(X, y)]
    s2 = sum(e * e for e in res) / (len(y) - k)
    return beta, [beta[i] / math.sqrt(s2 * inv[i][i]) for i in range(k)]


def summ(v, extra=None):
    """v：[(date, pnl, credit/premium)]"""
    if len(v) < 3:
        return None
    p = [x[1] for x in v]
    sd = st.stdev(p)
    eq = pk = dd = 0
    for x in p:
        eq += x
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    by = {}
    for x in v:
        by.setdefault(x[0][:4], []).append(x[1])
    out = {"n": len(p), "win": round(sum(1 for x in p if x > 0) / len(p), 3), "avg": round(st.mean(p), 2),
           "total": round(sum(p), 1), "worst": round(min(p), 1), "best": round(max(p), 1), "dd": round(dd, 1), "sd": round(sd, 2),
           "t": round(st.mean(p) / (sd / math.sqrt(len(p))), 2) if sd > 0 else None,
           "credit": round(st.mean(x[2] for x in v), 1) if len(v[0]) > 2 else None,
           "years": {y: round(st.mean(a), 1) for y, a in sorted(by.items())}}
    if extra:
        out.update(extra)
    return out


def load():
    rows = json.load(open(os.path.join(OUT, "daily.json"), encoding="utf-8"))
    rows = [r for r in rows if r.get("fs") and r.get("main") in r["fs"]]
    rows.sort(key=lambda r: r["date"])
    return rows


def prepare(rows):
    dates = [date.fromisoformat(r["date"]) for r in rows]
    tdset = set(dates)
    idx = {r["date"]: i for i, r in enumerate(rows)}
    last = dates[-1]

    def expiry(und, ref):
        m = re.match(r"^TA(\d)(\d{2})$", und)
        y, mo = pg.full_year(int(m[1]), ref), int(m[2])
        py, pm = (y, mo - 1) if mo > 1 else (y - 1, 12)
        d = date(py, pm, 15)
        if d > last:
            return pg.option_expiry(y, mo)
        n = 0
        for _ in range(40):
            if d in tdset:
                n += 1
                if n == 3:
                    return d
            d -= timedelta(days=1)
        return None

    for i, r in enumerate(rows):
        d = dates[i]
        m = r["main"]
        o, h, l, c, s = r["fs"][m]
        r["close"] = c if c and c > 0 else s
        prev = rows[i - 1]["fs"].get(m) if i > 0 else None
        pc = (prev[3] if prev and prev[3] and prev[3] > 0 else (prev[4] if prev else None))
        r["ret"] = (r["close"] / pc - 1) * 100 if pc else None
        r["trp"] = (max(h - l, abs(h - pc), abs(l - pc)) / pc * 100) if pc and h and l and h > 0 else None
        f = r.get("naive_flip")
        r["red"] = (r["S0"] < f) if f else (r.get("naive_gex") is not None and r["naive_gex"] < 0)
        # 最近到期月份（依實際交易日推算到期日）
        best = None
        for und in sorted(r.get("chains") or {}):
            e = expiry(und, d)
            if e and e > d and (best is None or e < best[1]):
                best = (und, e)
        r["n1"] = best[0] if best else None
        r["n1exp"] = best[1].isoformat() if best else None
    for i, r in enumerate(rows):
        w = [rows[k]["trp"] for k in range(max(0, i - 4), i + 1) if rows[k].get("trp") is not None]
        r["atr5p"] = sum(w) / 5 if len(w) == 5 else None
        r["expand"] = i >= 5 and r["atr5p"] is not None and rows[i - 5].get("atr5p") is not None and r["atr5p"] > rows[i - 5]["atr5p"]
        if i + 1 < len(rows):
            nx = rows[i + 1]["fs"].get(r["main"])
            if nx and r["close"]:
                o, h, l, c, s = nx
                cc = c if c and c > 0 else s
                if h and l and h > 0:
                    r["tr1"] = max(h - l, abs(h - r["close"]), abs(l - r["close"])) / r["close"] * 100
                r["ret1"] = (cc / r["close"] - 1) * 100
        rets = [rows[k]["ret"] for k in range(i + 1, min(i + 6, len(rows))) if rows[k].get("ret") is not None]
        if len(rets) == 5:
            r["rv5"] = math.sqrt(sum(x * x for x in rets) / 5) * math.sqrt(252)
            if r.get("iv30"):
                r["rv_iv"] = r["rv5"] / (r["iv30"] * 100)
        past = [rows[k]["ret"] for k in range(max(1, i - 19), i + 1) if rows[k].get("ret") is not None]
        r["rv20"] = math.sqrt(sum(x * x for x in past) / len(past)) * math.sqrt(252) / 100 if len(past) >= 15 else None
        r["vrp"] = r["iv30"] - r["rv20"] if r.get("iv30") and r.get("rv20") else None
        # 偏斜：最近月份價外 3% Put IV − 價外 3% Call IV（交易所公布的 IV）
        r["skew"] = None
        if r["n1"]:
            ch = r["chains"][r["n1"]]
            F1 = r["fs"][r["n1"]][4]
            pc_ = [x for x in ch if x[4]]
            cc_ = [x for x in ch if x[3]]
            if pc_ and cc_:
                p = min(pc_, key=lambda x: abs(x[0] - F1 * 0.97))[4]
                c = min(cc_, key=lambda x: abs(x[0] - F1 * 1.03))[3]
                r["skew"] = p - c
    for i, r in enumerate(rows):
        w = [x["skew"] for x in rows[max(0, i - 59):i + 1] if x.get("skew") is not None]
        r["skewPct"] = (sum(1 for x in w if x <= r["skew"]) / len(w)) if r.get("skew") is not None and len(w) >= 40 else None
    # 主力連續價格（轉倉日用同一合約的報酬率銜接），均線用
    adj = []
    for r in rows:
        adj.append(adj[-1] * (1 + (r["ret"] or 0) / 100) if adj else r["close"])
    for r, a in zip(rows, adj):
        r["adj"] = a
    return idx


def fit_model(rows):
    reg = [r for r in rows if r.get("tr1") is not None and r.get("atr5p") is not None]
    X = [[1.0, r["atr5p"], 1.0 if r["red"] else 0.0] for r in reg]
    b, t = ols(X, [r["tr1"] for r in reg])
    return {"a": round(b[0], 4), "b": round(b[1], 4), "c": round(b[2], 4), "t_red": round(t[2], 2), "n": len(reg)}


def bucket_stats(v):
    v = [r for r in v if r.get("tr1") is not None and r.get("ret1") is not None]
    if not v:
        return None
    rvi = [r["rv_iv"] for r in v if r.get("rv_iv") is not None]
    return {"n": len(v), "tr1": round(st.mean(r["tr1"] for r in v), 3), "absret1": round(st.mean(abs(r["ret1"]) for r in v), 3),
            "big": round(st.mean(1 if abs(r["ret1"]) >= BIG else 0 for r in v), 3),
            "rv_iv": round(st.mean(rvi), 3) if rvi else None, "up": round(st.mean(1 if r["ret1"] > 0 else 0 for r in v), 3)}


def main():
    rows = load()
    idx = prepare(rows)
    model = fit_model(rows)
    mid = rows[len(rows) // 2]["date"]
    model_tr = fit_model([r for r in rows if r["date"] < mid])
    sig = {"red": bucket_stats([r for r in rows if r["red"]]), "green": bucket_stats([r for r in rows if not r["red"]])}
    allv = [r for r in rows if r.get("tr1") is not None and r.get("ret1") is not None]
    sig["_meta"] = {"from": rows[0]["date"], "to": rows[-1]["date"], "days": len(allv), "tr1": round(st.mean(r["tr1"] for r in allv), 3),
                    "big": round(st.mean(1 if abs(r["ret1"]) >= BIG else 0 for r in allv), 3), "bigPct": BIG, "cost": COST}
    sig["_model"] = model

    def held(i):
        r = rows[i]
        if not r["n1"] or r["n1exp"] not in idx:
            return None
        j = idx[r["n1exp"]]
        S_T = rows[j]["fs"].get(r["n1"])
        if not S_T:
            return None
        return r["n1"], j - i, r["fs"][r["n1"]][4], S_T[4], r["chains"][r["n1"]]

    def E_of(r, m):
        return (m["a"] + m["b"] * r["atr5p"] + (m["c"] if r["red"] else 0)) / 100

    def pick(ch, target, call, need):
        if call:
            c = [x for x in ch if x[0] >= target and x[1]]
            return min(c, key=lambda x: x[0]) if c else None
        c = [x for x in ch if x[0] <= target and x[2]]
        return max(c, key=lambda x: x[0]) if c else None

    def seller(k, filt, m=model, cost=COST, sub=None):
        out = []
        for i, r in enumerate(rows):
            if (sub and not sub(r)) or not filt(r) or r.get("atr5p") is None:
                continue
            h = held(i)
            if not h:
                continue
            und, n, F1, S_T, ch = h
            D = k * E_of(r, m) * F1 * math.sqrt(n)
            c, p = pick(ch, F1 + D, True, 1), pick(ch, F1 - D, False, 1)
            if not c or not p:
                continue
            cr = c[1] + p[2]
            out.append((r["date"], cr - 2 * cost - max(0, S_T - c[0]) - max(0, p[0] - S_T), cr))
        return out

    green = lambda r: not r["red"]  # noqa: E731
    grid = {k: summ(seller(k, green)) for k in (1.0, 1.5, 2.0, 2.5, 3.0)}
    tiers = []
    for name, k in (("標準", 2.5), ("保守", 3.0)):
        tiers.append({"name": name, "k": k, "full": summ(seller(k, green)),
                      "oos": summ(seller(k, green, m=model_tr, sub=lambda r: r["date"] >= mid)),
                      "cost2": summ(seller(k, green, cost=2 * COST)), "red": summ(seller(k, lambda r: r["red"]))})
    sig["_strategy"] = {"cost": COST, "from": rows[0]["date"], "to": rows[-1]["date"], "oosFrom": mid, "tiers": tiers}

    def buyer(kind, filt):
        out = []
        for i, r in enumerate(rows):
            if not filt(r):
                continue
            h = held(i)
            if not h:
                continue
            und, n, F1, S_T, ch = h
            both = [x for x in ch if x[1] and x[2]]
            if not both:
                continue
            if kind == "straddle":
                x = min(both, key=lambda z: abs(z[0] - F1))
                prem = x[1] + x[2]
                pay = abs(S_T - x[0])
                out.append((r["date"], pay - prem - 2 * COST, prem))
            elif kind == "call":
                x = min(both, key=lambda z: abs(z[0] - F1))
                out.append((r["date"], max(0, S_T - x[0]) - x[1] - COST, x[1]))
            elif kind == "otm" and r.get("atr5p") is not None:
                D = E_of(r, model) * F1 * math.sqrt(n)
                c, p = pick(ch, F1 + D, True, 1), pick(ch, F1 - D, False, 1)
                if c and p:
                    out.append((r["date"], max(0, S_T - c[0]) + max(0, p[0] - S_T) - c[1] - p[2] - 2 * COST, c[1] + p[2]))
            elif kind == "otm1call":
                c = pick(ch, F1 * 1.01, True, 1)
                if c:
                    out.append((r["date"], max(0, S_T - c[0]) - c[1] - COST, c[1]))
            elif kind == "otm1put":
                p = pick(ch, F1 * 0.99, False, 1)
                if p:
                    out.append((r["date"], max(0, p[0] - S_T) - p[2] - COST, p[2]))
        return out

    def halves(v):
        return {"full": summ(v), "first": summ([x for x in v if x[0] < mid]), "second": summ([x for x in v if x[0] >= mid])}
    dn = lambda r: (r.get("ret") or 0) < 0  # noqa: E731
    sig["_buyer"] = {"from": rows[0]["date"], "to": rows[-1]["date"], "mid": mid, "cost": COST,
                     "straddle": halves(buyer("straddle", lambda r: r["red"] and r["expand"])),
                     "call": halves(buyer("call", lambda r: r["red"] and dn(r))),
                     "straddleOther": halves(buyer("straddle", lambda r: not (r["red"] and r["expand"]))),
                     "callGreenDown": halves(buyer("call", lambda r: (not r["red"]) and dn(r))),
                     "otm": halves(buyer("otm", lambda r: True))}

    # 均線
    A = [r["adj"] for r in rows]

    def sma(n, i):
        return sum(A[i - n + 1:i + 1]) / n if i >= n - 1 else None

    def events(n, N=10, slope=1):
        ev = []
        for i in range(len(rows)):
            m, m1, ms = sma(n, i), sma(n, i - 1), sma(n, i - slope)
            if m is None or m1 is None or ms is None or i - N < n - 1:
                continue
            if A[i] < m and A[i - 1] >= m1 and m > ms and all(A[k] >= sma(n, k) for k in range(i - N, i)):
                ev.append(i)
        return ev
    evs = sorted(set(events(30)) | set(events(60)) | set(events(100)))
    evset = set(evs)
    mc = buyer("otm1call", lambda r: idx[r["date"]] in evset)
    mp = buyer("otm1put", lambda r: idx[r["date"]] in evset)
    split = []
    for lab, f in (("紅燈", lambda r: r["red"]), ("綠燈", lambda r: not r["red"])):
        s = summ(buyer("otm1call", lambda r, f=f: idx[r["date"]] in evset and f(r)))
        if s:
            split.append({"light": lab, "side": "Call", **s})
    sig["_ma"] = {"from": rows[0]["date"], "to": rows[-1]["date"], "src": "PTA 主力連續（轉倉以報酬率銜接）", "N": 10, "slope": 1,
                  "otm": 0.01, "cost": COST, "call": summ(mc), "put": summ(mp), "callNext": None,
                  "baseCall": summ(buyer("otm1call", lambda r: idx[r["date"]] >= 119)), "split": split,
                  "events": [{"date": rows[i]["date"], "close": rows[i]["close"],
                              "ma": [n for n in (30, 60, 100) if i in set(events(n))]} for i in evs]}

    # 波動指標
    vr = sorted(r["vrp"] for r in rows if r.get("vrp") is not None)
    vmed = vr[len(vr) // 2] if vr else 0
    r2 = {"S1 現行：綠燈收盤賣": summ(seller(2.5, green)),
          f"V2 S1＋VRP 高於中位數（{vmed:.3f}）": summ(seller(2.5, lambda r: green(r) and r.get("vrp") is not None and r["vrp"] > vmed)),
          "V3 S1＋偏斜不在 60 日高檔（<80 百分位）": summ(seller(2.5, lambda r: green(r) and r.get("skewPct") is not None and r["skewPct"] < 0.8)),
          "V3' S1＋偏斜在 60 日高檔（≥80 百分位）": summ(seller(2.5, lambda r: green(r) and r.get("skewPct") is not None and r["skewPct"] >= 0.8))}
    reg = [r for r in rows if r.get("tr1") is not None and r.get("atr5p") is not None and r.get("vrp") is not None and r.get("skewPct") is not None]
    regm = None
    if len(reg) > 50:
        b, t = ols([[1.0, r["atr5p"], 1.0 if r["red"] else 0.0, r["vrp"] * 100, r["skewPct"]] for r in reg], [r["tr1"] for r in reg])
        regm = {"n": len(reg), "red": round(b[2], 4), "t_red": round(t[2], 2), "vrp": round(b[3], 4), "t_vrp": round(t[3], 2),
                "skew": round(b[4], 4), "t_skew": round(t[4], 2)}
    sig["_rules2"] = {"from": rows[0]["date"], "to": rows[-1]["date"], "vrpMedian": round(vmed, 4), "results": r2, "reg": regm or {}}

    # 規則總覽
    def rule(key, name, v):
        return {"key": key, "name": name, "full": summ(v), "first": summ([x for x in v if x[0] < mid]),
                "second": summ([x for x in v if x[0] >= mid]), "settle": None}
    sig["_board"] = {"from": rows[0]["date"], "to": rows[-1]["date"], "mid": mid, "fee": COST, "fill": "settle", "rules": [
        rule("標準", "賣方標準：綠燈、勒式 2.5×預估波幅×√天數", seller(2.5, green)),
        rule("保守", "賣方保守：綠燈、勒式 3.0×預估波幅×√天數", seller(3.0, green)),
        rule("買方跨式", "買方：紅燈＋波動放大 → 價平跨式", buyer("straddle", lambda r: r["red"] and r["expand"])),
        rule("買方Call", "買方：紅燈＋當日下跌 → 價平 Call", buyer("call", lambda r: r["red"] and dn(r))),
        rule("均線Call", "均線：首次跌破上揚的 30/60/100MA → 價外 1% Call", mc)]}

    json.dump(sig, open(os.path.join(OUT, "signal.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    f = lambda s: "—" if not s else f"{s['n']}｜{s['avg']:+.1f}｜{s['t']}｜{s['win']:.0%}｜{s['dd']}｜{s['worst']}"  # noqa: E731
    L = ["# PTA 回測報告\n", f"期間 {rows[0]['date']} ～ {rows[-1]['date']}，{len(rows)} 個交易日；前後段以 {mid} 切開。"
         f"成交用結算價，每腳扣 {COST} 元／噸。損益單位：元／噸（1 手 5 噸）。\n",
         "## 燈號\n", f"紅燈：{sig['red']}", f"綠燈：{sig['green']}", f"預估波幅模型：{model}\n",
         "## 賣方 k 值（綠燈、最近月份勒式、持有到期）\n", "格式：筆數｜平均｜t｜勝率｜最大回撤｜最大單筆虧損\n", "| k | 結果 | 各年平均 |", "|---|---|---|"]
    for k, s in grid.items():
        L.append(f"| {k} | {f(s)} | {' '.join(f'{y}:{v:+.1f}' for y, v in (s or {}).get('years', {}).items())} |")
    L += ["\n## 規則總覽\n", "| 規則 | 全期 | 前段 | 後段 |", "|---|---|---|---|"]
    for r in sig["_board"]["rules"]:
        L.append(f"| {r['name']} | {f(r['full'])} | {f(r['first'])} | {f(r['second'])} |")
    L += ["\n## 其他\n", f"賣方紅燈日同規則：{f(tiers[0]['red'])}", f"樣本外（後段、前段估模型）：{f(tiers[0]['oos'])}",
          f"成本加倍：{f(tiers[0]['cost2'])}"]
    for k, v in sig["_buyer"].items():
        if isinstance(v, dict) and "full" in v:
            L.append(f"買方 {k}：{f(v['full'])}")
    L.append(f"均線買 Call：{f(sig['_ma']['call'])}；買 Put：{f(sig['_ma']['put'])}；紅綠拆分：{split}")
    for k, v in r2.items():
        L.append(f"{k}：{f(v)}")
    L.append(f"迴歸（加入 VRP、偏斜）：{regm}")
    open(os.path.join(OUT, "report.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
