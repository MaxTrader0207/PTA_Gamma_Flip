"""
比較兩份 backtest/signal.json 的規則回測狀態，列出有變動的規則（每月回測重跑後用）。
狀態定義與儀表板「規則總覽」相同（只看回測，不含模擬帳本實績）：
  回測平均 ≤0 或 t<1.5 → 回測不支持；前段或後段任一段平均 ≤0 → 前後段不一致；其餘 → 回測支持
用法：python scripts/board_diff.py <舊 signal.json> <新 signal.json>
"""
import json
import sys


def status(r):
    f, a, b = r.get("full"), r.get("first"), r.get("second")
    if not f or f.get("avg") is None or f["avg"] <= 0 or (f.get("t") is not None and f["t"] < 1.5):
        return "回測不支持"
    if not a or not b or (a.get("avg") or 0) <= 0 or (b.get("avg") or 0) <= 0:
        return "前後段不一致"
    return "回測支持"


def board(path):
    s = json.load(open(path, encoding="utf-8"))
    B = s.get("_board") or {}
    return B, {r["key"]: (r.get("name", r["key"]), status(r), r.get("full") or {}) for r in B.get("rules", [])}


def main():
    Bo, old = board(sys.argv[1])
    Bn, new = board(sys.argv[2])
    print(f"回測期間：舊 {Bo.get('from')}～{Bo.get('to')}，新 {Bn.get('from')}～{Bn.get('to')}")
    changed = 0
    for k, (name, st, f) in new.items():
        o = old.get(k)
        if o is None:
            print(f"新增｜{name}：{st}")
            changed += 1
        elif o[1] != st:
            print(f"變動｜{name}：{o[1]} → {st}（平均 {o[2].get('avg')} → {f.get('avg')}，t {o[2].get('t')} → {f.get('t')}）")
            changed += 1
    for k in old:
        if k not in new:
            print(f"移除｜{old[k][0]}")
            changed += 1
    print(f"狀態變動 {changed} 項；目前：" + "；".join(f"{v[0]}＝{v[1]}" for v in new.values()))


if __name__ == "__main__":
    main()
