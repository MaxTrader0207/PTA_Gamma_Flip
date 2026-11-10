"""
PTA 資料管線核心測試：解析鄭商所行情、到期日規則、Gamma 計算、燈號／均線／波動指標、模擬帳本、回測分析。
執行：pytest -q
"""
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import date, datetime

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
FIX = os.path.join(os.path.dirname(__file__), "fixtures")

import pta_gex as pg  # noqa: E402
import pta_live as pl  # noqa: E402

FUT_TXT = """郑州商品交易所期货每日行情表(2026-11-09)
合约代码|昨结算|今开盘|最高价|最低价|今收盘|今结算|涨跌1|涨跌2|成交量(手)|持仓量|增减量|成交额(万元)|交割结算价
TA612|6,250|6,274|6,376|6,242|6,248|6,308|0|0|1000|117,142|0|100|
TA701|6,100|6,080|6,150|6,050|6,100|6,110|0|0|2000|300,000|0|100|
SR701|5,000|5,000|5,000|5,000|5,000|5,000|0|0|1|1|0|1|
"""
OPT_TXT = """郑州商品交易所期权每日行情表(2026-11-09)
合约代码|昨结算|今开盘|最高价|最低价|今收盘|今结算|涨跌1|涨跌2|成交量(手)|持仓量|增减量|成交额(万元)|DELTA|隐含波动率|行权量
TA701C6100|100|100|100|100|100|120.5|0|0|10|500|0|1|0.5|28.5|0
TA701P6100|100|100|100|100|100|110.0|0|0|10|400|0|1|-0.5|29.1|0
TA701C6500|10|10|10|10|10|20.0|0|0|10|0|0|1|0.2|30.0|0
"""


def test_parse_czce():
    d = date(2026, 11, 9)
    futs = pg.parse_futures(FUT_TXT, d)
    assert set(futs) == {"TA612", "TA701"} and futs["TA701"]["settle"] == 6110 and futs["TA612"]["oi"] == 117142
    rows = pg.parse_options(OPT_TXT, d, futs)
    assert len(rows) == 2, "持倉量為 0 的不納入"
    r = rows[0]
    assert r[0] == "TA701" and r[3] == "C" and r[4] == 120.5 and abs(r[6] - 0.285) < 1e-9 and r[7] == "TA701"


def test_option_expiry_rule():
    # TA701：2026/12/15（二）往前第 3 個交易日 → 12/11（五）
    assert pg.option_expiry(2027, 1) == date(2026, 12, 11)
    # TA612：2026/11/15 是週日 → 13、12、11 → 11/11
    assert pg.option_expiry(2026, 12) == date(2026, 11, 11)


def test_summarize_matches_snapshot():
    s = json.load(open(os.path.join(FIX, "snap.json"), encoding="utf-8"))
    futs = {k: v for k, v in s["futs"].items() if v.get("settle")}
    out = pg.summarize(s["rows"], futs, s["F"], datetime.fromisoformat(s["valuation"]))
    assert abs(out["naive"]["gex"] - s["summary"]["naive"]["gex"]) < 1e-6
    assert out["naive"]["flip"] == s["summary"]["naive"]["flip"]


def test_vol_ind():
    s = json.load(open(os.path.join(FIX, "snap.json"), encoding="utf-8"))
    futs = {k: v for k, v in s["futs"].items() if v.get("settle")}
    v = pl.vol_ind(s["rows"], futs, s["F"], datetime.fromisoformat(s["valuation"]))
    assert v["iv30"] and 0.05 < v["iv30"] < 2 and v["skew"] is not None


def test_bucket_and_ma():
    assert pl.bucket(100, 110, 1) == "red" and pl.bucket(120, 110, -1) == "green"
    assert pl.bucket(100, None, -5) == "red" and pl.bucket(100, None, 5) == "green"
    closes = [100 + i * 0.5 for i in range(120)]
    closes[-1] = sum(closes[-31:-1]) / 30 - 1
    st = pl.ma_status(closes)
    assert 30 in st["breaks"] and st["rising"][30]
    big = [100 + i * 0.5 for i in range(120)]
    big[-1] = big[-2] - 20
    assert 30 not in pl.ma_status(big)["breaks"]


def test_adj_prices_bridge_roll():
    past = [{"close": 100, "ret": None}, {"close": 103, "ret": 1.0}, {"close": 210, "ret": 2.0}]  # 第 3 天換主力，價位跳到 210
    a = pl.adj_prices(past)
    assert abs(a[-1] - 210) < 1e-9 and abs(a[1] / a[0] - 1.01) < 1e-9 and abs(a[2] / a[1] - 1.02) < 1e-9


def test_paper_update(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "PATH", str(tmp_path / "paper.json"))
    d0, d1 = date(2026, 11, 9), date(2026, 11, 11)
    futs0 = {"TA612": {"settle": 6300.0}}
    rows0 = []
    for k in range(5000, 7601, 100):
        for cp in "CP":
            otm = (k - 6300) if cp == "C" else (6300 - k)
            px = max(0.5, 150 * math.exp(-max(otm, 0) / 200)) + max(-otm, 0)
            rows0.append(["TA612", d1.isoformat(), float(k), cp, round(px, 1), 100, 0.3, "TA612"])
    days = [(d0, futs0, rows0), (d1, {"TA612": {"settle": 6350.0}}, [])]
    items = {d0.isoformat(): {"date": d0.isoformat(), "F": 6300.0, "naive_flip": 6000, "naive_gex": 5, "atr5": 100},
             d1.isoformat(): {"date": d1.isoformat(), "F": 6350.0}}
    out = pl.paper_update(days, items, {"a": 0.5, "b": 0.7, "c": 0.3}, [{"name": "標準", "k": 2.5}])
    led = json.load(open(tmp_path / "paper.json", encoding="utf-8"))
    e = [x for x in led["entries"] if x["tier"] == "標準"][0]
    assert e["status"] == "closed" and e["S"] == 6350.0
    assert e["pnl"] == round(e["credit"] - 2 * pl.COST, 1), "價外到期，損益＝權利金−成本"
    assert out["tiers"][0]["closed"] == 1


def test_analyze_runs(tmp_path):
    # 用小樣本跑完整回測分析，確認輸出格式（不檢查數值）
    work = tmp_path / "w"
    shutil.copytree(os.path.join(ROOT, "scripts"), work / "scripts")
    (work / "backtest").mkdir()
    shutil.copy(os.path.join(FIX, "bt_daily_small.json"), work / "backtest" / "daily.json")
    r = subprocess.run([sys.executable, str(work / "scripts" / "pta_analyze.py")], capture_output=True, text=True, cwd=work)
    assert r.returncode == 0, r.stderr[-2000:]
    sig = json.load(open(work / "backtest" / "signal.json", encoding="utf-8"))
    for k in ("red", "green", "_meta", "_model", "_strategy", "_buyer", "_ma", "_rules2", "_board"):
        assert k in sig
    assert [t["name"] for t in sig["_strategy"]["tiers"]] == ["標準", "保守"]
