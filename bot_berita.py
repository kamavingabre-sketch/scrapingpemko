"""
====================================================
 BOT AUTO POSTING BERITA — HEADLESS (Railway)
 portal.medan.go.id → Web Kecamatan Medan Johor

 DEPLOY:
   1. Push ke GitHub
   2. Deploy di Railway
   3. Set environment variables di Railway:
      ADMIN_USERNAME, ADMIN_PASSWORD

 ENVIRONMENT VARIABLES (Railway):
   ADMIN_USERNAME  → username panel admin
   ADMIN_PASSWORD  → password panel admin
   INTERVAL_MENIT  → interval scraping (default: 30)
====================================================
"""

import asyncio
import threading
import json
import requests
import sqlite3
import hashlib
import re
import os
import tempfile
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ============================================================
#  ⚙️  KONFIGURASI — ambil dari env variable jika ada
# ============================================================
CONFIG = {
    "admin_url":         "https://medanjohor.medan.go.id/backdata",
    "login_url":         "https://medanjohor.medan.go.id/login-admins",
    "username":          os.environ.get("ADMIN_USERNAME", "admin123"),
    "password":          os.environ.get("ADMIN_PASSWORD", "p*Fg#72[Ls8wC(5y"),
    "sumber_url":        "https://portal.medan.go.id/berita",
    "max_halaman":       3,
    "db_file":           "berita_medan.db",
    "ambil_isi_lengkap": True,
    "interval_menit":    int(os.environ.get("INTERVAL_MENIT", "30")),
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MedanJohorBot/1.0)"}

# ============================================================
#  LOG
# ============================================================
def log(pesan):
    waktu = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{waktu}] {pesan}", flush=True)

# ============================================================
#  DATABASE
# ============================================================
def init_db():
    conn = sqlite3.connect(CONFIG["db_file"])
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS berita (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        hash_id      TEXT UNIQUE,
        judul        TEXT NOT NULL,
        ringkasan    TEXT,
        isi_lengkap  TEXT,
        gambar_url   TEXT,
        url_asli     TEXT,
        tanggal_str  TEXT,
        tanggal      TEXT,
        sudah_dipost INTEGER DEFAULT 0,
        dibuat_at    TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def simpan_berita(berita_list):
    conn = sqlite3.connect(CONFIG["db_file"])
    c = conn.cursor()
    baru = 0
    for b in berita_list:
        try:
            c.execute('''INSERT INTO berita
                (hash_id,judul,ringkasan,isi_lengkap,gambar_url,url_asli,tanggal_str,tanggal)
                VALUES (?,?,?,?,?,?,?,?)''',
                (b['hash_id'], b['judul'], b['ringkasan'], b.get('isi_lengkap',''),
                 b['gambar_url'], b['url_asli'], b['tanggal_str'], b['tanggal']))
            baru += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    return baru

def tandai_selesai(hash_id):
    conn = sqlite3.connect(CONFIG["db_file"])
    c = conn.cursor()
    c.execute("UPDATE berita SET sudah_dipost=1 WHERE hash_id=?", (hash_id,))
    conn.commit()
    conn.close()

def ambil_belum_dipost():
    conn = sqlite3.connect(CONFIG["db_file"])
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM berita WHERE sudah_dipost=0 ORDER BY tanggal ASC, id ASC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

# ============================================================
#  SCRAPER
# ============================================================
def parse_tanggal(teks):
    bulan = {'Januari':'01','Februari':'02','Maret':'03','April':'04',
             'Mei':'05','Juni':'06','Juli':'07','Agustus':'08',
             'September':'09','Oktober':'10','November':'11','Desember':'12'}
    for nama, num in bulan.items():
        teks = teks.replace(nama, num)
    angka = re.findall(r'\d+', teks)
    try:
        if len(angka) >= 3:
            return f"{angka[2]}-{angka[1]}-{angka[0]}"
    except:
        pass
    return datetime.now().strftime('%Y-%m-%d')

def scrape_detail(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')

        for tag in soup.find_all(['script', 'style', 'nav', 'header', 'footer']):
            tag.decompose()

        # Strategi 1: portal.medan.go.id — div.entry-body
        entry_body = soup.select_one('div.entry-body')
        if entry_body:
            teks = entry_body.get_text('\n', strip=True)
            if len(teks) > 200:
                return teks

        # Strategi 2: Selector class umum
        for sel in ['.entry-content', '.content-detail', '.post-content',
                    'article .content', '.post-body']:
            el = soup.select_one(sel)
            if not el:
                continue
            for t in el.find_all(['script', 'style', 'nav']):
                t.decompose()
            teks = el.get_text('\n', strip=True)
            if len(teks) > 200:
                return teks

        # Strategi 3: Fallback semua <p>
        paragraphs = soup.find_all('p')
        hasil = '\n\n'.join(
            p.get_text(strip=True) for p in paragraphs
            if len(p.get_text(strip=True)) > 40
        )
        return hasil
    except Exception as e:
        return f'[ERROR: {e}]'

def scrape_semua():
    log("=" * 50)
    log("🔄 Mulai scraping portal.medan.go.id...")
    semua = []
    try:
        for page in range(1, CONFIG["max_halaman"] + 1):
            url = CONFIG["sumber_url"] if page == 1 else f"{CONFIG['sumber_url']}?page={page}"
            log(f"  Mengambil halaman {page}...")
            resp = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')

            rows = soup.find_all('div', class_='row')
            seen = set()
            for row in rows:
                col4 = row.find('div', class_='col-md-4')
                if not col4: continue
                link_el = col4.find('a', href=re.compile(r'/berita/.+__read\d+\.html'))
                if not link_el: continue

                href = link_el.get('href', '')
                url_berita = href if href.startswith('http') else f"https://portal.medan.go.id{href}"
                match = re.search(r'__read(\d+)', url_berita)
                uid = match.group(1) if match else url_berita
                if uid in seen: continue
                seen.add(uid)

                img = col4.find('img')
                gambar_url = ''
                if img:
                    src = img.get('src', '')
                    gambar_url = src if src.startswith('http') else f"https://portal.medan.go.id{src}"

                col8 = row.find('div', class_='col-md-8')
                if not col8: continue

                judul_el = col8.find('h3', class_='entry-title')
                judul = judul_el.get_text(strip=True) if judul_el else ''
                if not judul: continue

                tanggal_el = col8.find('span', class_='date')
                tanggal_str = tanggal_el.get_text(strip=True) if tanggal_el else ''
                tanggal = parse_tanggal(tanggal_str)

                body_el = col8.find('div', class_='entry-body')
                ringkasan = ''
                if body_el:
                    p = body_el.find('p')
                    ringkasan = p.get_text(strip=True)[:400] if p else ''

                hash_id = hashlib.md5(url_berita.encode()).hexdigest()
                semua.append({
                    'hash_id': hash_id, 'judul': judul, 'ringkasan': ringkasan,
                    'isi_lengkap': '', 'gambar_url': gambar_url,
                    'url_asli': url_berita, 'tanggal_str': tanggal_str, 'tanggal': tanggal
                })

            log(f"  Halaman {page}: {len(seen)} berita ditemukan")
            time.sleep(1)

        baru = simpan_berita(semua)
        log(f"✅ Scraping selesai! Total: {len(semua)} | Baru: {baru}")

        # Ambil isi lengkap untuk semua yang kosong
        if CONFIG["ambil_isi_lengkap"]:
            conn = sqlite3.connect(CONFIG["db_file"])
            c = conn.cursor()
            c.execute("SELECT hash_id, url_asli FROM berita WHERE isi_lengkap='' ORDER BY id DESC LIMIT 30")
            rows_db = c.fetchall()
            if rows_db:
                log(f"  Mengambil isi lengkap {len(rows_db)} berita...")
            for i, (hid, url_b) in enumerate(rows_db):
                isi = scrape_detail(url_b)
                c.execute("UPDATE berita SET isi_lengkap=? WHERE hash_id=?", (isi, hid))
                log(f"  [{i+1}/{len(rows_db)}] isi diambil ({len(isi)} karakter)")
                time.sleep(0.5)
            conn.commit()
            conn.close()

        return baru

    except Exception as e:
        log(f"❌ Error scraping: {e}")
        return 0

# ============================================================
#  DOWNLOADER GAMBAR
# ============================================================
def download_gambar(url):
    if not url: return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            ext = url.split('.')[-1].split('?')[0]
            ext = ext if ext in ['jpg','jpeg','png','gif','webp'] else 'jpg'
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}')
            tmp.write(resp.content)
            tmp.close()
            return tmp.name
    except:
        pass
    return None

# ============================================================
#  POSTER (Playwright)
# ============================================================
async def posting_berita_async(berita_list):
    berhasil = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # LOGIN
        log("🌐 Login ke panel admin...")
        await page.goto(CONFIG["login_url"], wait_until="networkidle")
        await page.fill('#username', CONFIG["username"])
        await page.fill('#password', CONFIG["password"])
        await page.click('#btnSignIn')

        try:
            await page.wait_for_url(lambda url: "login" not in url.lower(), timeout=10000)
        except:
            pass
        await page.wait_for_timeout(2000)

        if "login" in page.url.lower():
            log("❌ Login gagal! Cek ADMIN_USERNAME / ADMIN_PASSWORD.")
            await browser.close()
            return 0

        log("✅ Login berhasil!")

        for i, berita in enumerate(berita_list, 1):
            log(f"\n📝 [{i}/{len(berita_list)}] {berita['judul'][:60]}...")
            gambar_path = None
            try:
                await page.goto(f"{CONFIG['admin_url']}/berita", wait_until="networkidle")
                await page.wait_for_timeout(800)
                await page.click('#btnTambah')
                await page.wait_for_timeout(2000)

                # Upload gambar
                if berita['gambar_url']:
                    gambar_path = download_gambar(berita['gambar_url'])
                    if gambar_path:
                        try:
                            async with page.expect_file_chooser() as fc_info:
                                await page.click('#ganti_photo')
                            fc = await fc_info.value
                            await fc.set_files(gambar_path)
                            await page.wait_for_timeout(1500)
                            log("  📷 Gambar diupload")
                        except Exception as e:
                            log(f"  ⚠️ Gambar gagal: {e}")

                # Pilih kategori via Select2
                try:
                    opened = await page.evaluate('''() => {
                        const sel = document.querySelector("select#kategori");
                        if (!sel) return "no_select";
                        try {
                            const jq = window.$ || window.jQuery;
                            if (jq) { jq(sel).select2("open"); return "opened_jquery"; }
                        } catch(e) {}
                        const container = sel.closest(".select2-container") ||
                                          document.querySelector(".select2-container");
                        if (container) { container.click(); return "opened_click"; }
                        return "no_container";
                    }''')

                    if opened != "no_select":
                        await page.wait_for_selector('.select2-results__option', state='visible', timeout=8000)
                        await page.wait_for_function(
                            '''() => {
                                const opts = document.querySelectorAll(".select2-results__option");
                                return opts.length > 0 && ![...opts].every(
                                    o => o.textContent.toLowerCase().includes("searching")
                                );
                            }''',
                            timeout=8000
                        )
                        await page.wait_for_timeout(300)

                        hasil = await page.evaluate('''() => {
                            const opts = Array.from(document.querySelectorAll(".select2-results__option"));
                            if (opts.length === 0) return "no_options";
                            const semua = opts.map(o => o.textContent.trim()).join("|");
                            const target = opts.find(
                                o => o.textContent.toLowerCase().includes("berita harian")
                            ) || opts[0];
                            ["mousedown","mouseup","click"].forEach(evtName => {
                                target.dispatchEvent(new MouseEvent(evtName, {
                                    bubbles: true, cancelable: true, view: window
                                }));
                            });
                            return "ok:" + target.textContent.trim() + "||" + semua;
                        }''')

                        if hasil.startswith("ok:"):
                            bagian = hasil[3:].split("||")
                            log(f"  🏷️ Kategori: {bagian[0]}")
                            log(f"  📋 Opsi: {bagian[1] if len(bagian) > 1 else '-'}")

                    await page.wait_for_timeout(400)
                except Exception as e:
                    log(f"  ⚠️ Kategori error: {e}")

                # Isi judul
                await page.fill('#judul', berita['judul'])
                log("  📌 Judul diisi")

                # Isi konten CKEditor
                isi = berita['isi_lengkap'] or berita['ringkasan'] or berita['judul']
                isi_html = '<p>' + isi.replace('\n\n', '</p><p>').replace('\n', '<br>') + '</p>'
                try:
                    await page.evaluate(f'''() => {{
                        if (typeof CKEDITOR !== 'undefined') {{
                            for (const name in CKEDITOR.instances) {{
                                CKEDITOR.instances[name].setData({repr(isi_html)});
                            }}
                        }}
                    }}''')
                    log("  📄 Konten diisi")
                except:
                    log("  ⚠️ Konten tidak bisa diisi otomatis")

                # Simpan
                await page.click('#btnSimpan')
                log("  🖱️ Klik Simpan...")

                try:
                    await page.wait_for_selector('#modalBtnSaveOPD', state='attached', timeout=8000)
                    await page.wait_for_timeout(600)
                    await page.evaluate(
                        '(sel) => { const b = document.querySelector(sel); if (b) b.click(); }',
                        '#modalBtnSaveOPD'
                    )
                    log("  🔘 Klik Simpan OPD")
                except Exception as e:
                    log(f"  ⚠️ Modal tidak muncul: {e}")

                # SweetAlert konfirmasi
                await page.wait_for_timeout(800)
                try:
                    await page.wait_for_selector('button.swal2-confirm', state='visible', timeout=6000)
                    await page.click('button.swal2-confirm')
                    log("  ✔️ Konfirmasi diklik")
                    await page.wait_for_timeout(2500)
                except Exception as e:
                    log(f"  ⚠️ SweetAlert gagal: {e}")

                # Cek hasil
                try:
                    form_masih_ada = await page.is_visible('#btnSimpan')
                    konten = await page.content()
                    sukses = not form_masih_ada or any(
                        k in konten.lower() for k in ['berhasil', 'sukses', 'success']
                    )
                except:
                    sukses = False

                if sukses:
                    tandai_selesai(berita['hash_id'])
                    berhasil += 1
                    log(f"  ✅ BERHASIL!")
                else:
                    log(f"  ⚠️ Tidak yakin berhasil — cek manual di panel admin")

            except Exception as e:
                log(f"  ❌ Error: {str(e)[:150]}")
            finally:
                if gambar_path and os.path.exists(gambar_path):
                    os.unlink(gambar_path)
                await page.wait_for_timeout(1000)

        await browser.close()

    log(f"\n🎉 Posting selesai: {berhasil}/{len(berita_list)} berhasil")
    return berhasil

# ============================================================
#  WEB DASHBOARD
# ============================================================
HTML_STYLE = """
<style>
  body { font-family: Arial, sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; background: #f0f2f5; }
  h1 { background: #1a6b3c; color: white; padding: 15px 20px; border-radius: 8px; margin: 0 0 20px; }
  .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }
  .stats { display: flex; gap: 16px; flex-wrap: wrap; }
  .stat { background: #e8f5e9; border-radius: 8px; padding: 16px 24px; text-align: center; flex: 1; }
  .stat .num { font-size: 2em; font-weight: bold; color: #1a6b3c; }
  .stat .lbl { color: #555; font-size: .9em; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
  .btn { padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-size: .95em; font-weight: bold; text-decoration: none; }
  .btn-green  { background: #4CAF50; color: white; }
  .btn-orange { background: #FF9800; color: white; }
  .btn-red    { background: #f44336; color: white; }
  .btn-blue   { background: #2196F3; color: white; }
  table { width: 100%; border-collapse: collapse; font-size: .9em; }
  th { background: #37474F; color: white; padding: 10px; text-align: left; }
  td { padding: 8px 10px; border-bottom: 1px solid #eee; }
  tr:hover td { background: #f9f9f9; }
  .badge-ok  { background: #e8f5e9; color: #2e7d32; padding: 2px 8px; border-radius: 10px; font-size:.85em; }
  .badge-no  { background: #fff3e0; color: #e65100; padding: 2px 8px; border-radius: 10px; font-size:.85em; }
  .alert { padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }
  .alert-ok  { background: #e8f5e9; color: #2e7d32; }
  .alert-err { background: #ffebee; color: #c62828; }
</style>
"""

def db_query(sql, params=(), fetchall=True):
    conn = sqlite3.connect(CONFIG["db_file"])
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(sql, params)
    if fetchall:
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    conn.commit()
    conn.close()
    return None

def db_execute(sql, params=()):
    conn = sqlite3.connect(CONFIG["db_file"])
    c = conn.cursor()
    c.execute(sql, params)
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default HTTP logs

    def send_html(self, html, code=200):
        body = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path='/'):
        self.send_response(302)
        self.send_header('Location', path)
        self.end_headers()

    def do_GET(self):
        if self.path == '/api/stats':
            stats = {
                'total':  db_query("SELECT COUNT(*) as n FROM berita")[0]['n'],
                'sudah':  db_query("SELECT COUNT(*) as n FROM berita WHERE sudah_dipost=1")[0]['n'],
                'belum':  db_query("SELECT COUNT(*) as n FROM berita WHERE sudah_dipost=0")[0]['n'],
            }
            body = json.dumps(stats).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # Ambil parameter query
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        halaman = int(params.get('halaman', ['1'])[0])
        filter_val = params.get('filter', ['semua'])[0]
        per_hal = 30
        offset = (halaman - 1) * per_hal

        where = ''
        if filter_val == 'belum':
            where = 'WHERE sudah_dipost=0'
        elif filter_val == 'sudah':
            where = 'WHERE sudah_dipost=1'

        total_filter = db_query(f"SELECT COUNT(*) as n FROM berita {where}")[0]['n']
        berita_list  = db_query(
            f"SELECT * FROM berita {where} ORDER BY tanggal DESC, id DESC LIMIT ? OFFSET ?",
            (per_hal, offset)
        )
        total_hal = max(1, (total_filter + per_hal - 1) // per_hal)

        stats = {
            'total': db_query("SELECT COUNT(*) as n FROM berita")[0]['n'],
            'sudah': db_query("SELECT COUNT(*) as n FROM berita WHERE sudah_dipost=1")[0]['n'],
            'belum': db_query("SELECT COUNT(*) as n FROM berita WHERE sudah_dipost=0")[0]['n'],
        }

        alert = ''
        act = params.get('act', [''])[0]
        if act == 'tandai_ok':
            alert = '<div class="alert alert-ok">✅ Semua berita berhasil ditandai sudah dipost.</div>'
        elif act == 'reset_ok':
            alert = '<div class="alert alert-ok">✅ Semua isi_lengkap berhasil direset.</div>'
        elif act == 'reset_satu_ok':
            alert = '<div class="alert alert-ok">✅ Berita berhasil direset.</div>'
        elif act == 'tandai_satu_ok':
            alert = '<div class="alert alert-ok">✅ Berita berhasil ditandai dipost.</div>'

        rows_html = ''
        for b in berita_list:
            status = '<span class="badge-ok">✅ Sudah</span>' if b['sudah_dipost'] else '<span class="badge-no">⏳ Belum</span>'
            tgl    = (b['tanggal'] or '')[:10]
            judul  = b['judul'][:70] + ('...' if len(b['judul']) > 70 else '')
            hid    = b['hash_id']
            rows_html += f"""
            <tr>
              <td>{tgl}</td>
              <td>{judul}</td>
              <td>{status}</td>
              <td>
                <form method="post" action="/tandai-satu" style="display:inline">
                  <input type="hidden" name="hash_id" value="{hid}">
                  <button class="btn btn-green" style="padding:4px 10px;font-size:.8em">✅ Tandai</button>
                </form>
                <form method="post" action="/reset-satu" style="display:inline">
                  <input type="hidden" name="hash_id" value="{hid}">
                  <button class="btn btn-orange" style="padding:4px 10px;font-size:.8em">🔄 Reset</button>
                </form>
              </td>
            </tr>"""

        # Paginasi
        pag = ''
        for h in range(1, total_hal + 1):
            active = 'background:#1a6b3c;color:white;' if h == halaman else 'background:white;color:#333;border:1px solid #ccc;'
            pag += f'<a href="/?halaman={h}&filter={filter_val}" class="btn" style="{active}padding:6px 12px;">{h}</a> '

        html = f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot Berita — Medan Johor</title>
{HTML_STYLE}
</head><body>
<h1>📰 Bot Berita Otomatis — Kecamatan Medan Johor</h1>
{alert}
<div class="card">
  <div class="stats">
    <div class="stat"><div class="num">{stats['total']}</div><div class="lbl">Total Berita</div></div>
    <div class="stat"><div class="num">{stats['sudah']}</div><div class="lbl">✅ Sudah Dipost</div></div>
    <div class="stat"><div class="num">{stats['belum']}</div><div class="lbl">⏳ Belum Dipost</div></div>
  </div>
</div>
<div class="actions">
  <form method="post" action="/tandai-semua">
    <button class="btn btn-green">✅ Tandai Semua Sudah Dipost</button>
  </form>
  <form method="post" action="/reset-isi">
    <button class="btn btn-orange">🔄 Reset Semua Isi Lengkap</button>
  </form>
  <form method="post" action="/hapus-sudah">
    <button class="btn btn-red">🗑️ Hapus Berita Sudah Dipost</button>
  </form>
</div>
<div class="card">
  <div style="display:flex;gap:10px;margin-bottom:12px;">
    <a href="/?filter=semua" class="btn {'btn-blue' if filter_val=='semua' else ''}" style="{'color:#333;background:#eee;' if filter_val!='semua' else ''}">Semua</a>
    <a href="/?filter=belum" class="btn {'btn-blue' if filter_val=='belum' else ''}" style="{'color:#333;background:#eee;' if filter_val!='belum' else ''}">Belum Dipost</a>
    <a href="/?filter=sudah" class="btn {'btn-blue' if filter_val=='sudah' else ''}" style="{'color:#333;background:#eee;' if filter_val!='sudah' else ''}">Sudah Dipost</a>
  </div>
  <table>
    <thead><tr><th width="100">Tanggal</th><th>Judul</th><th width="110">Status</th><th width="150">Aksi</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap;">{pag}</div>
</div>
</body></html>"""
        self.send_html(html)

    def do_POST(self):
        from urllib.parse import parse_qs

        length  = int(self.headers.get('Content-Length', 0))
        body    = self.rfile.read(length).decode('utf-8')
        params  = parse_qs(body)

        if self.path == '/tandai-semua':
            db_execute("UPDATE berita SET sudah_dipost=1")
            self.redirect('/?act=tandai_ok')

        elif self.path == '/reset-isi':
            db_execute("UPDATE berita SET isi_lengkap=''")
            self.redirect('/?act=reset_ok')

        elif self.path == '/hapus-sudah':
            db_execute("DELETE FROM berita WHERE sudah_dipost=1")
            self.redirect('/?act=tandai_ok')

        elif self.path == '/tandai-satu':
            hid = params.get('hash_id', [''])[0]
            if hid:
                db_execute("UPDATE berita SET sudah_dipost=1 WHERE hash_id=?", (hid,))
            self.redirect('/?act=tandai_satu_ok')

        elif self.path == '/reset-satu':
            hid = params.get('hash_id', [''])[0]
            if hid:
                db_execute("UPDATE berita SET isi_lengkap='', sudah_dipost=0 WHERE hash_id=?", (hid,))
            self.redirect('/?act=reset_satu_ok')

        else:
            self.redirect('/')

def jalankan_web_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(('0.0.0.0', port), DashboardHandler)
    log(f"🌐 Web dashboard berjalan di port {port}")
    server.serve_forever()

# ============================================================
#  MAIN LOOP
# ============================================================
async def main():
    log("🤖 Bot Berita Medan Johor dimulai")
    log(f"⚙️  Interval: setiap {CONFIG['interval_menit']} menit")
    init_db()

    while True:
        try:
            # 1. Scrape berita baru
            scrape_semua()

            # 2. Ambil semua yang belum dipost
            belum = ambil_belum_dipost()
            if belum:
                log(f"\n🚀 Ditemukan {len(belum)} berita belum dipost, mulai posting...")
                await posting_berita_async(belum)
            else:
                log("ℹ️  Tidak ada berita baru untuk dipost.")

        except Exception as e:
            log(f"❌ Error di main loop: {e}")

        # Tunggu interval berikutnya
        interval = CONFIG["interval_menit"] * 60
        log(f"\n⏳ Menunggu {CONFIG['interval_menit']} menit sebelum cycle berikutnya...")
        await asyncio.sleep(interval)

if __name__ == '__main__':
    # Jalankan web dashboard di thread terpisah
    t = threading.Thread(target=jalankan_web_server, daemon=True)
    t.start()
    # Jalankan bot di main thread
    asyncio.run(main())
