"""
PTA 長期回測資料：逐日下載鄭商所期貨與期權日行情，計算合併 Gamma Flip，並保存回測需要的精簡資料。
輸出 backtest/daily.json（每天一筆）：
  date、main（主力合約）、S0（主力結算價）、fs（各 TA 期貨 [開, 高, 低, 收, 結算]）、
  naive_gex / naive_flip（合併、主力等價）、iv30（約 30 天價平 IV）、
  chains（最近兩個期權月份：{und: [[履約價, Call 結算, Put 結算, Call IV, Put IV], ...]}）
到期日事後依「實際有資料的交易日」推算（避免休市日表缺漏），見 scripts/pta_analyze.py。
用法：python scripts/pta_bt.py [--days 2600] [--resume]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests

sys.path.insert(0, os.path.dirname(__file__))
import pta_gex as pg  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "backtest")


def flip_fast(cs, S0, pct=0.08, n=160):
    return pg.flip(cs, S0, "naive", pct=pct, n=n)


def record(d, futs, rows):
    main = max(futs, key=lambda k: futs[k]["oi"] if futs[k]["oi"] == futs[k]["oi"] else 0)
    S0 = futs[main]["settle"]
    val = datetime(d.year, d.month, d.day, 15, 0, tzinfo=pg.CST)
    rows = [r for r in rows if abs(r[2] * S0 / futs[r[7]]["settle"] - S0) / S0 <= 0.25]
    if not rows:
        return None
    cs = pg.enrich(rows, futs, S0, val)
    rec = {"date": d.isoformat(), "main": main, "S0": S0,
           "fs": {k: [f["open"], f["high"], f["low"], f["close"], f["settle"]] for k, f in futs.items()},
           "naive_gex": round(pg.gex(cs, S0, "naive"), 2), "naive_flip": flip_fast(cs, S0)}
    # 約 30 天的價平 IV（各月份的價平 IV，取到期最接近 30 天者）
    by = {}
    for c in cs:
        by.setdefault(c[0], []).append(c)
    code = min(by, key=lambda k: abs(by[k][0][4] * 365 - 30))
    near = sorted(by[code], key=lambda c: abs(c[2] - S0))[:4]
    v = [c[5] for c in near if c[5]]
    rec["iv30"] = round(sum(v) / len(v), 4) if v else None
    # 最近兩個期權月份的報價（實際履約價、結算價、交易所 IV）
    unds = sorted({r[7] for r in rows}, key=lambda u: (futs[u]["year"], futs[u]["month"]))[:2]
    ch = {}
    for u in unds:
        t = {}
        for code_, exp, k, cp, px, oi, iv, und in rows:
            if und != u:
                continue
            x = t.setdefault(k, [None, None, None, None])
            if cp == "C":
                x[0], x[2] = px, iv
            else:
                x[1], x[3] = px, iv
        ch[u] = [[k] + v for k, v in sorted(t.items())]
    rec["chains"] = ch
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2600)
    ap.add_argument("--resume", action="store_true", help="保留既有 backtest/daily.json，只補缺的日子")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "daily.json")
    have = {}
    if a.resume and os.path.exists(path):
        have = {r["date"]: r for r in json.load(open(path, encoding="utf-8"))}
    hol = os.path.join(pg.DATA, "holidays.json")
    if os.path.exists(hol):
        pg.HOLIDAYS.update(json.load(open(hol, encoding="utf-8")))
    today = datetime.now(pg.CST).date()
    start = max(today - timedelta(days=a.days), datetime(2019, 12, 16).date())
    sess = requests.Session()
    d = start
    got = 0
    t0 = time.time()
    while d <= today:
        if d.weekday() < 5 and d.isoformat() not in have:
            fut_txt = pg.fetch("fut", d, sess)
            if fut_txt:
                opt_txt = pg.fetch("opt", d, sess)
                if opt_txt:
                    futs = pg.parse_futures(fut_txt, d)
                    rows = pg.parse_options(opt_txt, d, futs)
                    if futs and rows:
                        try:
                            rec = record(d, futs, rows)
                            if rec:
                                have[rec["date"]] = rec
                                got += 1
                        except Exception as e:  # noqa: BLE001
                            print("計算失敗", d, e, flush=True)
            time.sleep(0.3)
            if got and got % 50 == 0:
                print(d, "累計", len(have), "天", f"{time.time() - t0:.0f}s", flush=True)
                json.dump(sorted(have.values(), key=lambda r: r["date"]), open(path, "w", encoding="utf-8"),
                          ensure_ascii=False, separators=(",", ":"))
        d += timedelta(days=1)
    rows = sorted(have.values(), key=lambda r: r["date"])
    json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("完成", len(rows), "天", rows[0]["date"] if rows else None, "～", rows[-1]["date"] if rows else None)


if __name__ == "__main__":
    main()
