// PTA 頁面冒煙測試：用 repo 內的 data/latest.json 與 history.json 開啟電腦版與手機版，確認沒有 JavaScript 錯誤、重要區塊都有內容。
// 執行：node tests/page_smoke.js（需先 python scripts/build_pages.py；需要 playwright 與 chromium）
const { chromium } = require("playwright");
const fs = require("fs");
const http = require("http");
const path = require("path");
const ROOT = path.join(__dirname, "..");
const TYPES = { ".html": "text/html; charset=utf-8", ".json": "application/json", ".js": "application/javascript" };
const server = http.createServer((req, res) => {
  const p = path.join(ROOT, decodeURIComponent(req.url.split("?")[0]));
  if (!p.startsWith(ROOT) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(p)] || "application/octet-stream" });
  fs.createReadStream(p).pipe(res);
});
(async () => {
  await new Promise(r => server.listen(0, r));
  const port = server.address().port, b = await chromium.launch();
  const chart = fs.readFileSync(path.join(ROOT, "vendor/chart.umd.min.js"), "utf8");
  let fail = 0;
  const L = JSON.parse(fs.readFileSync(path.join(ROOT, "data/latest.json"), "utf8"));
  const hasBT = !!L.btSignal;  // 回測尚未完成時，燈號與今日重點不會出現
  const SEL_D = hasBT ? ["#btLight", ".expchips", "#summaryCard"] : [".expchips"];
  const SEL_M = hasBT ? ["#btLight", ".expchips", "#summaryCard"] : [".expchips"];
  const cases = [
    ["index.html", { width: 1300, height: 900 }, SEL_D],
    ["mobile.html", { width: 390, height: 844 }, SEL_M],
  ];
  for (const [u, vp, sels] of cases) {
    const p = await b.newPage({ viewport: vp });
    await p.route(/cdnjs|fonts\.googleapis|fonts\.gstatic/, r => r.request().url().includes("chart") ?
      r.fulfill({ body: chart, contentType: "application/javascript" }) : r.fulfill({ body: "", contentType: "text/css" }));
    const errs = [];
    p.on("pageerror", e => errs.push(e.message));
    await p.goto(`http://localhost:${port}/${u}`);
    await p.waitForTimeout(3000);
    const res = await p.evaluate(s => s.map(q => { const e = document.querySelector(q); return [q, !!e && !e.hidden && e.textContent.trim().length > 0]; }), sels);
    const wide = await p.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    const bad = res.filter(x => !x[1]).map(x => x[0]);
    if (errs.length || bad.length || wide) {
      fail++;
      console.log(`✗ ${u}`, { errors: errs, emptyOrMissing: bad, horizontalScroll: wide });
    } else console.log(`✓ ${u}`);
    await p.close();
  }
  await b.close(); server.close();
  process.exit(fail ? 1 : 0);
})();
