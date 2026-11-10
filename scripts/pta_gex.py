"""
PTA 期權 Gamma Flip 每日資料管線（鄭州商品交易所）
- 下載鄭商所每日行情：期權 OptionDataDaily.txt、期貨 FutureDataDaily.txt（| 分隔）
- 只取 PTA（TA）；每個期權月份對應同月份期貨為標的
- 以「主力期貨」（持倉量最大的 TA 月份）為基準價 S0，各月份履約價換成主力等價：K_eq = K × S0 / F_月
  這樣所有月份可以放在同一條價格軸上合併計算 GEX 與 Flip（主力移動 x%，各月標的同比例移動 x%）
- 輸出 data/latest.json、data/history.json、data/daily/YYYYMMDD.json

用法：python scripts/pta_gex.py [--days 14]
"""
import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

CST = timezone(timedelta(hours=8))
PRODUCT = "TA"
MULT = 5            # PTA 期貨與期權交易單位：5 噸／手
RATE = 0.015
DATA = os.path.join(os.path.dirname(__file__), "..", "data")
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.czce.com.cn/cn/jysj/mrhq/H077003001index_1.htm",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
URLS = {
    "opt": ["https://www.czce.com.cn/cn/DFSStaticFiles/Option/{y}/{d}/OptionDataDaily.txt",
            "http://www.czce.com.cn/cn/DFSStaticFiles/Option/{y}/{d}/OptionDataDaily.txt"],
    "fut": ["https://www.czce.com.cn/cn/DFSStaticFiles/Future/{y}/{d}/FutureDataDaily.txt",
            "http://www.czce.com.cn/cn/DFSStaticFiles/Future/{y}/{d}/FutureDataDaily.txt"],
}
# 已知會影響到期日推算的大陸休市日，可在 data/holidays.json 追加（["2027-02-08", ...]）
HOLIDAYS = set()
DEBUG = []


# ---------------- download ----------------
def fetch(kind, d, sess):
    for tpl in URLS[kind]:
        url = tpl.format(y=d.year, d=d.strftime("%Y%m%d"))
        for attempt in range(2):
            try:
                r = sess.get(url, headers=UA, timeout=30)
                DEBUG.append(f"{kind} {url} -> {r.status_code} {len(r.content)}")
                if r.status_code == 404:
                    break
                if r.status_code != 200:
                    time.sleep(2)
                    continue
                raw = r.content
                for enc in ("utf-8-sig", "gbk", "gb18030"):
                    try:
                        txt = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        txt = None
                if txt and "|" in txt and PRODUCT in txt:
                    return txt
                break
            except Exception as e:  # noqa: BLE001
                DEBUG.append(f"{kind} {url} error {e}")
                time.sleep(3)
    return ""


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return float("nan")


def table(txt):
    """回傳 (表頭欄位, 資料列)；表頭是含「代码／月份」與「今结算」的那一行"""
    lines = [l for l in txt.splitlines() if "|" in l]
    head, rows = None, []
    for l in lines:
        f = [x.strip() for x in l.split("|")]
        if head is None:
            if any("结算" in x for x in f) and any(("代码" in x or "月份" in x) for x in f):
                head = f
            continue
        rows.append(f)
    return head or [], rows


def col(head, *keys):
    for i, h in enumerate(head):
        if any(k in h for k in keys):
            return i
    return -1


def full_year(digit, ref):
    y = (ref.year // 10) * 10 + digit
    if y < ref.year - 2:
        y += 10
    return y


# ---------------- dates ----------------
def is_trading(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def option_expiry(y, m):
    """鄭商所規則：標的期貨交割月份前一個月第 15 個日曆日之前（含該日）的倒數第 3 個交易日"""
    py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
    d, n = date(py, pm, 15), 0
    while True:
        if is_trading(d):
            n += 1
            if n == 3:
                return d
        d -= timedelta(days=1)


# ---------------- parse ----------------
def parse_futures(txt, d):
    head, rows = table(txt)
    if not head:
        return {}
    c = dict(code=0, settle=col(head, "今结算"), o=col(head, "今开盘"), h=col(head, "最高价"),
             l=col(head, "最低价"), close=col(head, "今收盘"), oi=col(head, "持仓量", "空盘量"))
    out = {}
    for f in rows:
        m = re.match(rf"^{PRODUCT}(\d)(\d{{2}})$", f[0])
        if not m:
            continue
        settle = num(f[c["settle"]])
        if not settle > 0:
            continue
        out[f[0]] = {"settle": settle, "open": num(f[c["o"]]), "high": num(f[c["h"]]), "low": num(f[c["l"]]),
                     "close": num(f[c["close"]]), "oi": num(f[c["oi"]]),
                     "year": full_year(int(m[1]), d), "month": int(m[2])}
    return out


def parse_options(txt, d, futs):
    head, rows = table(txt)
    if not head:
        return []
    c = dict(settle=col(head, "今结算"), close=col(head, "今收盘"), oi=col(head, "持仓量", "空盘量"),
             iv=col(head, "隐含波动率", "隐含波动"), delta=col(head, "DELTA", "Delta"))
    out = []
    for f in rows:
        m = re.match(rf"^({PRODUCT}(\d)(\d{{2}}))([CP])(\d+)$", f[0])
        if not m:
            continue
        und = m[1]
        if und not in futs:
            continue
        oi = num(f[c["oi"]])
        if not oi > 0:
            continue
        y, mo = full_year(int(m[2]), d), int(m[3])
        exp = option_expiry(y, mo)
        if exp <= d:
            continue
        px = num(f[c["settle"]])
        if not px > 0 and c["close"] >= 0:
            px = num(f[c["close"]])
        iv = num(f[c["iv"]]) if c["iv"] >= 0 else float("nan")
        if iv > 3:
            iv /= 100.0
        out.append([und, exp.isoformat(), float(m[5]), m[4], round(px, 2) if px > 0 else None, int(oi),
                    round(iv, 4) if iv > 0 else None, und])
    return out


# ---------------- math（與網頁計算引擎相同） ----------------
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def b76(F, K, T, s, call):
    sq = s * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s * T) / sq
    d2 = d1 - sq
    df = math.exp(-RATE * T)
    return df * (F * ncdf(d1) - K * ncdf(d2)) if call else df * (K * ncdf(-d2) - F * ncdf(-d1))


def gamma(F, K, T, s):
    sq = s * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s * T) / sq
    return math.exp(-RATE * T) * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (F * sq)


def implied_vol(px, F, K, T, call):
    if px is None or px <= 0:
        return None
    intr = math.exp(-RATE * T) * max(F - K if call else K - F, 0)
    if px <= intr + 0.05 or b76(F, K, T, 3.0, call) < px:
        return None
    lo, hi = 0.01, 3.0
    for _ in range(70):
        m = (lo + hi) / 2
        lo, hi = (lo, m) if b76(F, K, T, m, call) > px else (m, hi)
    return (lo + hi) / 2


def year_frac(exp_iso, val):
    e = datetime.fromisoformat(exp_iso).replace(hour=15, minute=0, tzinfo=CST)
    return max((e - val).total_seconds() / 86400, 0.15) / 365


def enrich(rows, futs, S0, val, fallback=0.2):
    """轉成主力等價：(code, rank, K_eq, call, T, iv, oi, w)"""
    exps = sorted({r[1] for r in rows})
    rank = {e: i for i, e in enumerate(exps)}
    by = {}
    for code, exp, k, cp, px, oi, ivx, und in rows:
        F0 = futs[und]["settle"]
        ratio = S0 / F0
        T = year_frac(exp, val)
        call = cp == "C"
        iv = ivx if ivx else implied_vol(px, F0, k, T, call)
        by.setdefault(code, []).append(dict(code=code, rank=rank[exp], K=k * ratio, call=call, T=T, oi=oi, iv=iv, w=F0 / S0))
    out = []
    for code, lst in by.items():
        otm = lambda x: (x["call"] and x["K"] >= S0) or (not x["call"] and x["K"] <= S0)  # noqa: E731
        pool = [x for x in lst if x["iv"] and otm(x)] or [x for x in lst if x["iv"]]
        for x in lst:
            iv = x["iv"] if (x["iv"] and otm(x)) else None
            if iv is None:
                iv = min(pool, key=lambda p: abs(p["K"] - x["K"]))["iv"] if pool else fallback
            out.append((code, x["rank"], x["K"], x["call"], x["T"], iv, x["oi"], x["w"]))
    return out


def grp(rank):
    return "N1" if rank == 0 else "N2" if rank == 1 else "M"


def gex(cs, S, mode):
    g = 0.0
    for _, _, K, call, T, iv, oi, w in cs:
        sign = -1 if mode == "short" else (1 if call else -1)
        g += sign * gamma(S, K, T, iv) * oi * w * MULT * S * S * 0.01
    return g / 1e4  # 萬元


def flip(cs, S0, mode, pct=0.08, n=400):
    if not cs:
        return None
    grid = [S0 * (1 - pct + 2 * pct * i / n) for i in range(n + 1)]
    vals = [gex(cs, s, mode) for s in grid]
    best = None
    for i in range(1, len(grid)):
        a, b = vals[i - 1], vals[i]
        if (a <= 0 < b) or (a >= 0 > b):
            x = grid[i - 1] + (grid[i] - grid[i - 1]) * (-a) / (b - a)
            if best is None or abs(x - S0) < abs(best - S0):
                best = x
    return round(best, 1) if best else None


def summarize(rows, futs, S0, val):
    cs = enrich(rows, futs, S0, val)
    out = {}
    for mode in ("naive", "short"):
        m = {"gex": round(gex(cs, S0, mode), 2), "flip": flip(cs, S0, mode)}
        for g in ("N1", "N2", "M"):
            sub = [c for c in cs if grp(c[1]) == g]
            m[f"flip{g}"] = flip(sub, S0, mode)
            m[f"gex{g}"] = round(gex(sub, S0, mode), 2) if sub else None
        out[mode] = m
    return out


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="往回抓幾個日曆天（首次建議 45）")
    a = ap.parse_args()
    hol = os.path.join(DATA, "holidays.json")
    if os.path.exists(hol):
        HOLIDAYS.update(json.load(open(hol, encoding="utf-8")))
    now = datetime.now(CST)
    today = now.date()
    sess = requests.Session()
    days = []
    for i in range(a.days, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        fut_txt = fetch("fut", d, sess)
        if not fut_txt:
            continue
        opt_txt = fetch("opt", d, sess)
        if not opt_txt:
            continue
        futs = parse_futures(fut_txt, d)
        rows = parse_options(opt_txt, d, futs)
        if futs and rows:
            days.append((d, futs, rows))
        time.sleep(0.8)

    os.makedirs(os.path.join(DATA, "daily"), exist_ok=True)
    if not days:
        print("沒有可用的交易日資料（休市、尚未公布，或連線被拒）")
        with open(os.path.join(DATA, "_debug.txt"), "w", encoding="utf-8") as f:
            f.write(now.isoformat() + "\n" + "\n".join(DEBUG[-60:]))
        sys.exit(0)

    hist_path = os.path.join(DATA, "history.json")
    hist = json.load(open(hist_path, encoding="utf-8")) if os.path.exists(hist_path) else {"items": []}
    items = {it["date"]: it for it in hist["items"]}
    prev_futs = None
    latest = None
    for d, futs, rows in days:
        main_code = max(futs, key=lambda k: futs[k]["oi"] if futs[k]["oi"] == futs[k]["oi"] else 0)
        S0 = futs[main_code]["settle"]
        val = datetime(d.year, d.month, d.day, 15, 0, tzinfo=CST)
        rows = [r for r in rows if abs(r[2] * S0 / futs[r[7]]["settle"] - S0) / S0 <= 0.25]
        mf = futs[main_code]
        pc = prev_futs[main_code]["close"] if prev_futs and main_code in prev_futs else None
        hi, lo, cl = mf["high"], mf["low"], mf["close"]
        tr = None
        if all(v == v and v > 0 for v in (hi, lo)):
            tr = hi - lo if pc is None else max(hi - lo, abs(hi - pc), abs(lo - pc))
        prev_futs = futs
        summ = summarize(rows, futs, S0, val)
        fut_out = {k: {kk: (None if v != v else v) for kk, v in f.items() if kk in ("settle", "open", "high", "low", "close", "oi")}
                   for k, f in futs.items()}
        snap = {"tradeDate": d.isoformat(), "valuation": val.isoformat(), "F": S0, "futContract": main_code,
                "product": "PTA", "mult": MULT, "unit": "萬元",
                "fields": ["code", "expiry", "strike", "cp", "settle", "oi", "iv", "und"],
                "rows": rows, "futs": fut_out, "summary": summ,
                "generatedAt": datetime.now(CST).isoformat(timespec="seconds")}
        it = {"date": d.isoformat(), "F": S0, "main": main_code,
              **{f"{m}_{k}": v for m, s in summ.items() for k, v in s.items()}}
        if all(v == v and v > 0 for v in (mf["open"], hi, lo, cl)):
            it.update({"open": mf["open"], "high": hi, "low": lo, "close": cl})
        if tr is not None:
            it["tr"] = round(tr, 1)
        if pc and cl == cl and cl > 0:
            it["ret"] = round((cl / pc - 1) * 100, 3)
        try:
            import pta_live
            it.update(pta_live.vol_ind(rows, futs, S0, val))
        except Exception as e:  # noqa: BLE001
            print("波動指標失敗", d, e)
        items[d.isoformat()] = it
        json.dump(snap, open(os.path.join(DATA, "daily", d.strftime("%Y%m%d") + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        latest = snap
        print(d, main_code, "S0", S0, "flip", summ["naive"]["flip"], "rows", len(rows), "tr", tr)

    seq = sorted(items.values(), key=lambda x: x["date"])
    trs = []
    for it in seq:
        if "tr" in it:
            trs.append(it["tr"])
            if len(trs) >= 5:
                it["atr5"] = round(sum(trs[-5:]) / 5, 1)
    latest["atr5"] = items[latest["tradeDate"]].get("atr5")
    # 回測燈號、均線、波動指標、模擬帳本、資料健康
    try:
        import pta_live
        for i, it in enumerate(seq):
            if i >= 5 and it.get("atr5") and seq[i - 5].get("atr5"):
                it["expand"] = it["atr5"] / it["F"] > seq[i - 5]["atr5"] / seq[i - 5]["F"]
        recent = {d.isoformat() for d, _, _ in days}
        for i, it in enumerate(seq):
            if it["date"] in recent:
                it["maBreaks"] = pta_live.ma_status(pta_live.adj_prices(seq[:i + 1]))["breaks"]
        latest["btSignal"] = pta_live.attach_signal(latest, seq)
        sig = (latest["btSignal"] or {}).get("stats") or {}
        tiers = [{"name": t["name"], "k": t["k"]} for t in (sig.get("_strategy") or {}).get("tiers", [])]
        if sig.get("_model") and tiers:
            latest["paper"] = pta_live.paper_update(days, items, sig["_model"], tiers)
    except Exception as e:  # noqa: BLE001
        print("回測燈號／模擬帳本失敗：", e)
    rr = latest.get("rows") or []
    latest["health"] = {"rows": len(rr), "iv": round(sum(1 for r in rr if r[6]) / len(rr), 3) if rr else 0,
                        "atr": bool(latest.get("atr5")), "futs": len(latest.get("futs") or {})}
    latest["holidays"] = sorted(h for h in HOLIDAYS if h >= (today - timedelta(days=7)).isoformat())
    li = items[latest["tradeDate"]]
    latest["ohlc"] = [li.get(k) for k in ("open", "high", "low", "close")]
    json.dump(latest, open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    hist["items"] = seq[-250:]
    hist["updatedAt"] = datetime.now(CST).isoformat(timespec="seconds")
    json.dump(hist, open(hist_path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    dbg = os.path.join(DATA, "_debug.txt")
    if os.path.exists(dbg):
        os.remove(dbg)


if __name__ == "__main__":
    main()
