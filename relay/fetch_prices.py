#!/usr/bin/env python3
"""
Radar BEI data relay — dijalankan oleh GitHub Actions setelah bursa tutup.

Sumber (berurutan):
  1. BEI resmi  : idx.co.id TradingSummary (GetStockSummary + GetIndexSummary) — JSON, harga penutupan resmi.
  2. CNBC Indonesia market-data quote pages (teks halaman, ada "Last updated HH:MM:SS WIB | DD/MM/YYYY").
  3. Ajaib aset pages (teks halaman, ada "Hari, DD Bulan YYYY HH:MM WIB").
Sumber 2-3 dipakai untuk konfirmasi silang / cadangan bila 1 gagal.

Keluaran:
  data/latest.json            -> snapshot terbaru (dibaca run pagi)
  data/daily/YYYY-MM-DD.json  -> arsip per hari bursa
  data/history.csv            -> baris per ticker per hari
  data/relay.log              -> diagnostik run terakhir
"""
import csv, datetime as dt, json, os, re, sys, time, traceback
from zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")
TICKERS = ["BMRI","BBCA","BBRI","BBNI","TLKM","ASII","AMRT","ISAT","INDF","SIDO",
           "AADI","ADRO","PTBA","ANTM","MEDC","BRMS"]
IDX_STOCK = "https://www.idx.co.id/primary/TradingSummary/GetStockSummary?length=9999&start=0"
IDX_INDEX = "https://www.idx.co.id/primary/TradingSummary/GetIndexSummary?length=9999&start=0"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    "Referer": "https://www.idx.co.id/id/data-pasar/ringkasan-perdagangan/ringkasan-saham/",
}
LOG = []
def log(msg):
    line = f"[{dt.datetime.now(WIB).strftime('%H:%M:%S')}] {msg}"
    print(line); LOG.append(line)

def num(s):
    if s is None: return None
    s = str(s).strip().replace("Rp", "").replace(" ", "")
    # 6,075 / 6.075 / 6071.138 / 6.071,14
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", s): s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s): s = s.replace(",", "")
    else: s = s.replace(",", ".") if s.count(",") == 1 and s.count(".") == 0 else s.replace(",", "")
    try: return float(s)
    except ValueError: return None

# ---------- 1. BEI resmi ----------
def fetch_idx_json(url, page=None):
    import requests
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and r.headers.get("content-type","").startswith("application/json"):
            return r.json(), "requests"
        log(f"idx requests: HTTP {r.status_code} ct={r.headers.get('content-type')}")
    except Exception as e:
        log(f"idx requests error: {e}")
    if page is not None:  # lewat browser (lolos tantangan JS Cloudflare)
        try:
            page.goto("https://www.idx.co.id/id/data-pasar/ringkasan-perdagangan/ringkasan-saham/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            txt = page.evaluate("async (u) => { const r = await fetch(u, {credentials:'include'}); return await r.text(); }", url)
            return json.loads(txt), "playwright"
        except Exception as e:
            log(f"idx playwright error: {e}")
    return None, None

def parse_idx(stock_json, index_json):
    out, ihsg = {}, None
    rows = (stock_json or {}).get("data") or []
    for row in rows:
        code = row.get("StockCode")
        if code in TICKERS:
            out[code] = {
                "close": num(row.get("Close")), "prev": num(row.get("Previous")),
                "open": num(row.get("OpenPrice")), "high": num(row.get("High")), "low": num(row.get("Low")),
                "volume": num(row.get("Volume")), "date": str(row.get("Date",""))[:10], "source": "idx.co.id",
            }
    for row in (index_json or {}).get("data") or []:
        if str(row.get("IndexCode","")).upper() == "COMPOSITE":
            ihsg = {"close": num(row.get("Close")), "prev": num(row.get("Previous")),
                    "high": num(row.get("Highest") or row.get("High")), "low": num(row.get("Lowest") or row.get("Low")),
                    "date": str(row.get("Date",""))[:10], "source": "idx.co.id"}
    return out, ihsg

# ---------- 2-3. Halaman publik lewat browser ----------
MONTHS = {"januari":1,"februari":2,"maret":3,"april":4,"mei":5,"juni":6,"juli":7,"agustus":8,"september":9,"oktober":10,"november":11,"desember":12}

def page_text(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    return page.inner_text("body")

def parse_cnbc(text):
    m = re.search(r"Last updated[:\s]*(\d{2}:\d{2}:\d{2})\s*WIB\s*\|\s*(\d{2})/(\d{2})/(\d{4})", text, re.I)
    ts = None
    if m:
        ts = f"{m.group(4)}-{m.group(3)}-{m.group(2)}T{m.group(1)}"
    prev = re.search(r"Previous\s*Close\s*\n?\s*([\d.,]+)", text, re.I)
    opn  = re.search(r"\bOpen\s*\n?\s*([\d.,]+)", text, re.I)
    rng  = re.search(r"Day\s*Range\s*\n?\s*([\d.,]+)\s*-\s*([\d.,]+)", text, re.I)
    px   = re.search(r"([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?)\s*\n?\s*([+-]?[\d.,]+)\s*\(([+-]?[\d.,]+)%\)", text)
    return {"close": num(px.group(1)) if px else None, "chg_pct": num(px.group(3)) if px else None,
            "prev": num(prev.group(1)) if prev else None, "open": num(opn.group(1)) if opn else None,
            "low": num(rng.group(1)) if rng else None, "high": num(rng.group(2)) if rng else None,
            "as_of": ts, "source": "cnbcindonesia.com"}

def parse_ajaib(text):
    m = re.search(r"(Senin|Selasa|Rabu|Kamis|Jumat|Sabtu|Minggu),\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s+(\d{2}:\d{2})\s*WIB", text)
    ts = None
    if m and m.group(3).lower() in MONTHS:
        ts = f"{m.group(4)}-{MONTHS[m.group(3).lower()]:02d}-{int(m.group(2)):02d}T{m.group(5)}:00"
    px = re.search(r"Rp\s?([\d.,]+)\s*\n?\s*([+-]?[\d.,]+)\s*\(([+-]?[\d.,]+)%\)", text)
    return {"close": num(px.group(1)) if px else None, "chg_pct": num(px.group(3)) if px else None,
            "as_of": ts, "source": "ajaib.co.id"}

def main():
    now = dt.datetime.now(WIB); today = now.date().isoformat()
    bust = now.strftime("%H%M%S")
    result = {"generated_at": now.isoformat(timespec="seconds"), "trade_date": None, "status": "pending",
              "prices": {}, "ihsg": None, "notes": []}
    page = browser = pw = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="id-ID")
    except Exception as e:
        log(f"playwright unavailable: {e}")

    # 1. BEI resmi
    sj, how1 = fetch_idx_json(IDX_STOCK, page); ij, how2 = fetch_idx_json(IDX_INDEX, page)
    idx_prices, idx_ihsg = parse_idx(sj, ij)
    log(f"idx.co.id: {len(idx_prices)}/16 saham ({how1}), IHSG {'ok' if idx_ihsg else 'n/a'} ({how2})")

    # 2-3. Konfirmasi silang
    cnbc, ajaib = {}, {}
    if page is not None:
        for t in TICKERS:
            try: cnbc[t] = parse_cnbc(page_text(page, f"https://www.cnbcindonesia.com/market-data/quote/{t}.JK?v={bust}"))
            except Exception as e: log(f"cnbc {t}: {e}")
            try: ajaib[t] = parse_ajaib(page_text(page, f"https://ajaib.co.id/saham/aset/{t}?v={bust}"))
            except Exception as e: log(f"ajaib {t}: {e}")
        try:
            c = parse_cnbc(page_text(page, f"https://www.cnbcindonesia.com/market-data/quote/.JKSE?v={bust}"))
            if c.get("close"): cnbc["IHSG"] = c
        except Exception as e: log(f"cnbc IHSG: {e}")
    ok_c = sum(1 for v in cnbc.values() if v.get("close")); ok_a = sum(1 for v in ajaib.values() if v.get("close"))
    log(f"cnbc: {ok_c} harga, ajaib: {ok_a} harga")

    # Gabungkan + validasi
    def closing_ok(ts):  # timestamp hari ini & >= 15:50 WIB
        return bool(ts) and ts[:10] == today and ts[11:16] >= "15:50"
    for t in TICKERS:
        rec, conf = {}, []
        if t in idx_prices and idx_prices[t].get("close"):
            rec = dict(idx_prices[t]); conf.append("idx")
            if rec.get("date") and rec["date"] != today: rec["stale"] = True
        c, a = cnbc.get(t, {}), ajaib.get(t, {})
        if c.get("close") and closing_ok(c.get("as_of")):
            if not rec: rec = dict(c); rec["date"] = today
            if abs(c["close"] - rec["close"]) < 1e-6: conf.append("cnbc")
            else: rec.setdefault("conflicts", []).append({"cnbc": c["close"], "as_of": c["as_of"]})
        if a.get("close") and closing_ok(a.get("as_of")):
            if not rec: rec = dict(a); rec["date"] = today
            if abs(a["close"] - rec["close"]) < 1e-6: conf.append("ajaib")
            else: rec.setdefault("conflicts", []).append({"ajaib": a["close"], "as_of": a["as_of"]})
        if rec:
            if rec.get("prev") and rec.get("close") and not rec.get("chg_pct"):
                rec["chg_pct"] = round((rec["close"]/rec["prev"]-1)*100, 2)
            rec["confirmed_by"] = conf
            rec["status"] = ("confirmed_2" if len(conf) >= 2 else "single_source") if not rec.get("stale") else "stale"
            result["prices"][t] = rec
    ih = idx_ihsg or cnbc.get("IHSG")
    if ih:
        if ih.get("prev") and ih.get("close") and not ih.get("chg_pct"): ih["chg_pct"] = round((ih["close"]/ih["prev"]-1)*100, 2)
        result["ihsg"] = ih
    dates = [r.get("date") for r in result["prices"].values() if r.get("date")]
    result["trade_date"] = max(dates) if dates else None
    n_conf = sum(1 for r in result["prices"].values() if r["status"] == "confirmed_2")
    result["status"] = "ok" if len(result["prices"]) == 16 and result["trade_date"] == today else ("partial" if result["prices"] else "failed")
    result["notes"].append(f"{len(result['prices'])}/16 harga; {n_conf} terkonfirmasi 2+ sumber; trade_date={result['trade_date']}")
    if result["trade_date"] != today:
        result["notes"].append("trade_date != hari ini: kemungkinan libur bursa atau sumber belum diperbarui")

    try:
        if browser: browser.close()
        if pw: pw.stop()
    except Exception: pass

    os.makedirs("data/daily", exist_ok=True)
    result["log"] = LOG[-40:]
    # Jangan timpa latest.json yang baik dengan hasil gagal
    if result["status"] == "failed" and os.path.exists("data/latest.json"):
        log("hasil gagal — latest.json lama dipertahankan")
    else:
        with open("data/latest.json", "w", encoding="utf-8") as f: json.dump(result, f, ensure_ascii=False, indent=1)
        if result["trade_date"]:
            with open(f"data/daily/{result['trade_date']}.json", "w", encoding="utf-8") as f: json.dump(result, f, ensure_ascii=False, indent=1)
            hist = "data/history.csv"; new = not os.path.exists(hist)
            with open(hist, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new: w.writerow(["trade_date","ticker","close","prev","chg_pct","open","high","low","volume","status","confirmed_by"])
                for t, r in result["prices"].items():
                    w.writerow([result["trade_date"], t, r.get("close"), r.get("prev"), r.get("chg_pct"), r.get("open"), r.get("high"), r.get("low"), r.get("volume"), r.get("status"), "+".join(r.get("confirmed_by", []))])
    with open("data/relay.log", "w", encoding="utf-8") as f: f.write("\n".join(LOG))
    print(json.dumps({k: result[k] for k in ("generated_at","trade_date","status","notes")}, ensure_ascii=False))

if __name__ == "__main__":
    try: main()
    except Exception:
        traceback.print_exc(); LOG.append(traceback.format_exc())
        os.makedirs("data", exist_ok=True)
        with open("data/relay.log", "w", encoding="utf-8") as f: f.write("\n".join(LOG))
        sys.exit(1)
