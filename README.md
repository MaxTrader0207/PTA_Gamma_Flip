# PTA Gamma Flip 戰情板

鄭州商品交易所 PTA（TA）期權的造市商 Gamma 曝險與 Gamma Flip 每日自動計算，提供電腦版與手機版兩種頁面。架構與 TXO_Gamma_Flip 相同。

## 資料與計算

- 資料來源：鄭商所每日行情 `DFSStaticFiles/Option/{年}/{日期}/OptionDataDaily.txt` 與 `.../Future/.../FutureDataDaily.txt`（[每日行情頁](https://www.czce.com.cn/cn/jysj/mrhq/H077003001index_1.htm)）
- 每個期權月份以同月份 PTA 期貨為標的；以持倉量最大的主力期貨為基準價，履約價換算成主力等價（K × 主力價 ÷ 該月期貨價），所有月份合併在同一條價格軸上
- IV 優先採用鄭商所公布的隱含波動率；GEX 單位萬元（每手 5 噸）；ATR 為主力期貨近 5 日真實波幅平均
- 到期日：標的期貨交割月份前一個月第 15 個日曆日之前（含）的倒數第 3 個交易日；遇大陸長假可在 `data/holidays.json` 加入休市日（例如 `["2027-02-08"]`）
- 區塊：近月期權、次月期權、其餘為遠月期權

## 檔案

- `scripts/pta_gex.py`：下載與計算；`.github/workflows/update.yml`：交易日 16:37／17:13／18:07 自動執行
- `data/latest.json`、`data/history.json`：網頁讀取的資料
- `src/desktop.html`：電腦版原始檔與計算引擎；`src/mobile.src.html`：手機版樣板
- `scripts/build_pages.py`：產生 `index.html`、`mobile.html` 與 `build/`（Claude 頁面發布用）

修改流程：改 `src/` → `python scripts/build_pages.py` → 提交並重新發布兩個 Claude 頁面。
