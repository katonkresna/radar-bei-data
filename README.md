# radar-bei-data

Relay data harga penutupan Bursa Efek Indonesia untuk **Briefing Pagi Radar Saham BEI** (K).
Dijalankan otomatis oleh GitHub Actions tiap hari bursa setelah penutupan; run briefing pagi
membaca `data/latest.json` lewat `raw.githubusercontent.com` (tanpa cache halaman, tanpa gerbang izin).

## Jadwal (WIB)
| Jam | Tujuan |
|---|---|
| 16.40 | Potret harga penutupan (bursa tutup 15.50, post-trading selesai 16.15) |
| 18.30 | Ulangan bila sumber sore belum lengkap |
| 06.45 (hari berikutnya) | Cadangan sebelum briefing 07.30 WIB / 08.30 WITA |

Workflow juga bisa dijalankan manual dari tab **Actions → Radar BEI price relay → Run workflow**.

## Sumber (berurutan)
1. **BEI resmi** — `idx.co.id` TradingSummary (`GetStockSummary`, `GetIndexSummary`): harga penutupan resmi + tanggal.
2. **CNBC Indonesia** market-data quote (`Last updated HH:MM:SS WIB | DD/MM/YYYY`) — konfirmasi silang / cadangan.
3. **Ajaib** halaman aset (stempel `Hari, DD Bulan YYYY HH:MM WIB`) — konfirmasi silang / cadangan.

Harga dari sumber 2–3 hanya dianggap sah bila stempelnya = hari itu **dan** jam ≥ 15.50 WIB (penutupan).

## Keluaran
- `data/latest.json` — snapshot terbaru: `trade_date`, `status` (`ok` / `partial` / `failed`), `prices.<TICKER>` (`close`, `prev`, `chg_pct`, `open`, `high`, `low`, `volume`, `status`: `confirmed_2` / `single_source` / `stale`, `confirmed_by`), `ihsg`, `notes`, `log`.
- `data/daily/YYYY-MM-DD.json` — arsip per hari bursa.
- `data/history.csv` — satu baris per ticker per hari.
- `data/relay.log` — diagnostik run terakhir.

`latest.json` **tidak** ditimpa bila run gagal total, supaya pembaca selalu mendapat data terakhir yang baik
(cek `generated_at` dan `trade_date` sebelum memakai).

## Ticker
BMRI, BBCA, BBRI, BBNI, TLKM, ASII, AMRT, ISAT, INDF, SIDO, AADI, ADRO, PTBA, ANTM, MEDC, BRMS + IHSG (COMPOSITE).

Bukan nasihat keuangan. Data dari sumber publik pihak ketiga bisa berbeda dengan data resmi bursa.
