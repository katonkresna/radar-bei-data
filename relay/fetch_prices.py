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
  data/debug/*.txt            -> potongan teks halaman yang gagal diparse (untuk perbaikan regex)
"""
import csv, datetime as dt, json, os, re, sys, time, traceback
from zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")
TICKERS = ["BMRI","BBCA","BBRI","BBNI","TLKM","ASII","AMRT","ISAT","INDF","SIDO",
           "AADI","ADRO","PTBA","ANTM","MEDC","BRMS"]
IDX_STOCK = "https://www.idx.co.id/primary/TradingSummary/GetStockSummary?length=9999&start=0"
IDX_INDEX = "https://www.idx.co.id/primary/TradingSummary/GetIndexSummary?length=9999&start=0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    "Referer": "https://www.idx.co.id/id/data-pasar/ringkasan-perdagangan/ringkasan-saham/",
}
LOG = []
def log(msg):
    line = f"[{dt.datetime.now(WIB).strftime('%H:%M:%S')}] {msg}"
    print(line); LOG.append(line)

def dump(name, text, limit=6000):
    os.makedirs("data/debug", exist_ok=True)
    with open(f"data/debug/{name}.txt", "w", encoding="utf-8") as f:
        f.write((text or "")[:limit])

def num(s):
    if s is None: return None
    s = str(s).strip().replace("Rp", "").replace(" ", "").replace("−", "-")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", s): s = s.replace(".", "").replace(",", ".")      # 6.075 / 6.071,14
    elif re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s): s = s.replace(",", "")                         # 6,075 / 6,071.14
    elif re.fullmatch(r"-?\d+,\d+", s): s = s.replace(",", ".")                                          # 1,22
    else: s = s.replace(",", "")
    try: return float(s)
    except ValueError: return None

# ---------- 1. BEI resmi ----------
def fetch_idx_json(url, page=None, tag="idx"):
    import requests
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "json" in ct:
            return r.json(), "requests"
        log(f"{tag} requests: HTTP {r.status_code} ct={ct} body[:120]={r.text[:120]!r}")
    except Exception as e:
        log(f"{tag} requests error: {e}")
    if page is not None:  # lewat browser (lolos tantangan JS bila ada)
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)  # beri waktu tantangan Cloudflare selesai
            body = page.inner_text("body")
            try:
                return json.loads(body), "playwright"
            except Exception:
                log(f"{tag} playwright: HTTP {resp.status if resp else '?'} body[:200]={body[:200]!r}")
                dump(f"{tag}_playwright", body)
        except Exception as e:
            log(f"{tag} playwright error: {e}")
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
MONTHS = {"januari":1,"februari":2,"maret":3,"april":4,"mei":5,"juni":6,"juli":7,"agustus":8,"september":9,"oktober":10,"november":11,"desember":12,
          "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"agu":8,"agt":8,"sep":9,"okt":10,"nov":11,"des":12}

def page_text(page, url, wait_re=None):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try: page.wait_for_load_state("networkidle", timeout=15000)
    except Exception: pass
    if wait_re:
        try: page.wait_for_function("re => new RegExp(re,'i').test(document.body.innerText)", arg=wait_re, timeout=15000)
        except Exception: pass
    page.wait_for_timeout(1500)
    return page.inner_text("body")

NUMRE = r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?"

def parse_cnbc(text):
    t = re.sub(r"[ \t]+", " ", text)
    m = re.search(r"Last\s*updated[:\s]*(\d{1,2}:\d{2}:\d{2})\s*WIB\s*\|?\s*(\d{2})/(\d{2})/(\d{4})", t, re.I)
    ts = f"{m.group(4)}-{m.group(3)}-{m.group(2)}T{m.group(1).zfill(8)}" if m else None
    prev = re.search(r"Prev(?:ious)?\.?\s*Close\s*[:\n]?\s*(" + NUMRE + ")", t, re.I)
    opn  = re.search(r"(?<![A-Za-z])Open\s*[:\n]?\s*(" + NUMRE + ")", t, re.I)
    rng  = re.search(r"Day\s*Range\s*[:\n]?\s*(" + NUMRE + r")\s*[-–]\s*(" + NUMRE + ")", t, re.I)
    # harga + perubahan: "6,075\n-75 (-1.22%)" atau "6,075 -75 -1.22%"
    px = re.search(r"(" + NUMRE + r")\s*\n?\s*([+\-−]?\s?" + NUMRE + r")\s*\n?\s*\(?\s*([+\-−]?\s?" + NUMRE + r")\s*%\s*\)?", t)
    return {"close": num(px.group(1)) if px else None, "chg_pct": num(px.group(3).replace(" ", "")) if px else None,
            "prev": num(prev.group(1)) if prev else None, "open": num(opn.group(1)) if opn else None,
            "low": num(rng.group(1)) if rng else None, "high": num(rng.group(2)) if rng else None,
            "as_of": ts, "source": "cnbcindonesia.com"}

def parse_ajaib(text):
    """Teks halaman Ajaib (rendered): ... 'Mulai Investasi' / '6,075' / '75 (-1.22%)' / 'Rabu, 30 September 2026 16:14 WIB' / '221.34 M' / 'Volume' ..."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    date_re = re.compile(r"^(Senin|Selasa|Rabu|Kamis|Jumat|Sabtu|Minggu),?\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s+(\d{1,2}[:.]\d{2})\s*WIB$", re.I)
    out = {"close": None, "chg_pts": None, "chg_pct": None, "as_of": None, "volume_txt": None, "source": "ajaib.co.id"}
    for i, l in enumerate(lines):
        m = date_re.match(l)
        if not m or m.group(3).lower() not in MONTHS: continue
        hhmm = m.group(5).replace(".", ":").zfill(5)
        out["as_of"] = f"{m.group(4)}-{MONTHS[m.group(3).lower()]:02d}-{int(m.group(2)):02d}T{hhmm}:00"
        if i >= 2:
            px = re.fullmatch(r"(" + NUMRE + r")", lines[i-2])
            ch = re.fullmatch(r"([+\-\u2212]?\s?" + NUMRE + r")\s*\(\s*([+\-\u2212]?\s?" + NUMRE + r")\s*%\s*\)", lines[i-1])
            if px: out["close"] = num(px.group(1))
            if ch:
                out["chg_pct"] = num(ch.group(2).replace(" ", ""))
                pts = num(ch.group(1).replace(" ", ""))
                # tanda poin sering hilang di teks ('75 (-1.22%)') -> ikuti tanda persen
                if pts is not None and out["chg_pct"] is not None and out["chg_pct"] < 0 and pts > 0: pts = -pts
                out["chg_pts"] = pts
            else:
                l1 = lines[i-1]
                if re.fullmatch(r"[-–—]?|0|0[.,]0+|0\s*\(\s*0(?:[.,]0+)?\s*%\s*\)", l1):  # saham stagnan: '-', '0', '0 (0%)'
                    out["chg_pts"], out["chg_pct"] = 0.0, 0.0
                else:
                    m2 = re.fullmatch(r"\(?\s*([+\-\u2212]?\s?" + NUMRE + r")\s*%\s*\)?", l1)  # hanya persen
                    if m2: out["chg_pct"] = num(m2.group(1).replace(" ", ""))
                    out["chg_line_raw"] = l1[:40]
        if i + 2 < len(lines) and lines[i+2].lower() == "volume": out["volume_txt"] = lines[i+1]
        break
    hi = re.search(r"Harga Tertinggi \(52 Minggu\)\s*\n\s*(" + NUMRE + ")", text)
    lo = re.search(r"Harga Terendah \(52 Minggu\)\s*\n\s*(" + NUMRE + ")", text)
    if hi: out["hi52"] = num(hi.group(1))
    if lo: out["lo52"] = num(lo.group(1))
    if out["close"] is not None and out["chg_pts"] is not None:
        out["prev"] = round(out["close"] - out["chg_pts"], 2)
    return out

def parse_pluang(text):
    """Judul/H1 Pluang: 'Harga Saham Bank Central Asia Tbk (BBCA) Hari Ini: Rp6.125' (tanpa stempel waktu -> hanya pembanding nilai)."""
    m = re.search(r"Hari Ini:?\s*Rp\s?(" + NUMRE + ")", text)
    return {"close": num(m.group(1)) if m else None, "source": "pluang.com"}

def main():
    now = dt.datetime.now(WIB); today = now.date().isoformat()
    bust = now.strftime("%H%M%S")
    result = {"generated_at": now.isoformat(timespec="seconds"), "trade_date": None, "status": "pending",
              "prices": {}, "ihsg": None, "notes": []}
    page = browser = pw = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="id-ID", timezone_id="Asia/Jakarta", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
    except Exception as e:
        log(f"playwright unavailable: {e}")

    # 1. BEI resmi
    sj, how1 = fetch_idx_json(IDX_STOCK, None, "idx-stock"); ij, how2 = fetch_idx_json(IDX_INDEX, None, "idx-index")
    idx_prices, idx_ihsg = parse_idx(sj, ij)
    log(f"idx.co.id: {len(idx_prices)}/16 saham ({how1}), IHSG {'ok' if idx_ihsg else 'n/a'} ({how2})")

    # 2-3. Ajaib (utama dari runner GitHub; CNBC & idx.co.id memblokir ASN GitHub via Cloudflare) + Pluang (pembanding nilai)
    ajaib, pluang = {}, {}
    if page is not None:
        for i, t in enumerate(TICKERS):
            try:
                txt = page_text(page, f"https://ajaib.co.id/saham/aset/{t}?v={bust}", wait_re="WIB")
                ajaib[t] = parse_ajaib(txt)
                if not ajaib[t].get("close") or not ajaib[t].get("as_of"):
                    log(f"ajaib {t}: parse gagal")
                    if i < 3: dump(f"ajaib_{t}", txt)
                elif ajaib[t].get("chg_pct") is None:
                    log(f"ajaib {t}: baris perubahan tak dikenal: {ajaib[t].get('chg_line_raw')!r}"); dump(f"ajaib_{t}_chg", txt, 1200)
            except Exception as e: log(f"ajaib {t}: {e}")
            try:
                txt = page_text(page, f"https://pluang.com/asset/indo-stock/{t.lower()}?v={bust}", wait_re="Hari Ini")
                pluang[t] = parse_pluang(txt)
                if not pluang[t].get("close"):
                    log(f"pluang {t}: parse gagal")
                    if i < 2: dump(f"pluang_{t}", txt)
            except Exception as e: log(f"pluang {t}: {e}")
    ok_a = sum(1 for v in ajaib.values() if v.get("close")); ok_p = sum(1 for v in pluang.values() if v.get("close"))
    log(f"ajaib: {ok_a}/16 harga, pluang: {ok_p}/16 harga")

    # Gabungkan + validasi
    def closing_ok(ts):  # timestamp hari ini & >= 15:50 WIB
        return bool(ts) and ts[:10] == today and ts[11:16] >= "15:50"
    for t in TICKERS:
        rec, conf = {}, []
        if t in idx_prices and idx_prices[t].get("close"):
            rec = dict(idx_prices[t]); conf.append("idx")
            if rec.get("date") and rec["date"] != today: rec["stale"] = True
        a = ajaib.get(t, {})
        if a.get("close") and closing_ok(a.get("as_of")):
            if not rec:
                rec = {"close": a["close"], "prev": a.get("prev"), "chg_pct": a.get("chg_pct"), "hi52": a.get("hi52"), "lo52": a.get("lo52"),
                       "volume_txt": a.get("volume_txt"), "date": today, "as_of": a["as_of"], "source": "ajaib.co.id"}
                conf.append("ajaib")
            elif abs(a["close"] - rec["close"]) < 1e-6: conf.append("ajaib")
            else: rec.setdefault("conflicts", []).append({"ajaib": a["close"], "as_of": a["as_of"]})
        elif a.get("close"):
            log(f"ajaib {t}: stempel {a.get('as_of')} bukan penutupan hari ini -> diabaikan")
        p = pluang.get(t, {})
        if rec and p.get("close"):
            if abs(p["close"] - rec["close"]) < 1e-6: conf.append("pluang")
            else: rec.setdefault("conflicts", []).append({"pluang": p["close"]})
        if rec:
            if rec.get("prev") and rec.get("close") and rec.get("chg_pct") is None:
                rec["chg_pct"] = round((rec["close"]/rec["prev"]-1)*100, 2)
            rec["confirmed_by"] = conf
            rec["status"] = "stale" if rec.get("stale") else ("confirmed_2" if len(conf) >= 2 else "single_source")
            result["prices"][t] = rec
    ih = idx_ihsg  # IHSG: run pagi memakai berita penutupan + kuotasi CNBC dari sandbox Claude
    if ih:
        if ih.get("prev") and ih.get("close") and ih.get("chg_pct") is None: ih["chg_pct"] = round((ih["close"]/ih["prev"]-1)*100, 2)
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
    prev_ok = False
    try:
        prev_ok = json.load(open("data/latest.json", encoding="utf-8")).get("status") in ("ok", "partial")
    except Exception: pass
    if result["status"] == "failed" and prev_ok:
        log("hasil gagal — latest.json lama (baik) dipertahankan")
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
