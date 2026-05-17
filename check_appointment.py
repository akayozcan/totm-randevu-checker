"""
TOTM (İnönü Üniversitesi) Karaciğer Nakil Polikliniği randevu kontrolcüsü.

Akış:
    Portal → "Randevu Al" → "HASTANE RANDEVU" → "KARACİĞER NAKİL ENS.POL.1"
    → "dolu" görürse: randevu yok
    → değilse: randevu var → mail at

Kullanım:
    python check_appointment.py              # headless, screenshot yok (Actions için)
    python check_appointment.py --debug      # screenshot/HTML dump (hâlâ headless)
    python check_appointment.py --headed     # tarayıcı görünür (WSLg/X gerekir)
    python check_appointment.py --no-mail    # mail göndermeden test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import (
    Page,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

from notify import send_mail

PORTAL_URL = "https://totmhastaportali.mergentech.com.tr/#/auth/uyesiz-randevu"
PORTAL_URL_FALLBACK = "https://totmhastaportali.mergentech.com.tr/#/auth/uyesiz-randevu-islemleri"

# Aranan poliklinik adı (boşlukla ayrılmış kelimeler, sırasıyla eşleşmeli).
# Türkçe karakterler toLocaleLowerCase('tr-TR') ile JS tarafında doğru eşlenir.
STEP_POLIKLINIK = os.environ.get("POLIKLINIK_ADI", "KARACİĞER NAKİL ENS.POL.1")

# "Randevu yok" anlamına gelen metinler
NO_APPT_PHRASES = [
    "dolu",
    "randevu bulunamadı",
    "randevu bulunmuyor",
    "müsait randevu yok",
    "uygun randevu bulunamadı",
    "randevu yok",
    "müsait randevu bulunamadı",
    "uygun randevu yok",
]

DEBUG_DIR = Path("debug_screenshots")
STATE_FILE = Path("last_state.json")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def snap(page: Page, name: str, debug: bool) -> None:
    if not debug:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
        html = await page.content()
        (DEBUG_DIR / f"{name}.html").write_text(html, encoding="utf-8")
        log(f"  📸 {name}")
    except Exception as e:
        log(f"  ⚠️ screenshot failed: {e}")


async def wait_settle(page: Page, ms: int = 1500) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeoutError:
        pass
    await page.wait_for_timeout(ms)


async def wait_for_content(page: Page, min_text_len: int = 200, timeout_ms: int = 30000) -> bool:
    """Angular SPA'in gerçekten content render etmesini bekle."""
    try:
        await page.wait_for_function(
            f"() => document.body && document.body.innerText && "
            f"document.body.innerText.trim().length > {min_text_len}",
            timeout=timeout_ms,
        )
        return True
    except PWTimeoutError:
        return False


async def dismiss_dialogs(page: Page) -> None:
    """Açık olabilecek confirm/dialog/KVKK pencerelerini kapat."""
    for pattern in (r"tamam", r"kabul", r"onayla", r"evet", r"devam", r"kapat", r"anlad[ıi]m"):
        try:
            regex = re.compile(pattern, re.IGNORECASE)
            for role in ("button", "link"):
                loc = page.get_by_role(role, name=regex)
                if await loc.count() > 0:
                    el = loc.first
                    if await el.is_visible():
                        await el.click(timeout=2000)
                        await page.wait_for_timeout(500)
                        return
        except Exception:
            continue


async def click_by_text(page: Page, pattern: str, timeout_s: int = 15) -> bool:
    """
    Sayfada pattern (regex, case-insensitive) ile eşleşen ilk GÖRÜNÜR ve
    tıklanabilir elementi bul ve tıkla. Birden fazla strateji dener.
    """
    regex = re.compile(pattern, re.IGNORECASE | re.UNICODE)
    deadline = asyncio.get_event_loop().time() + timeout_s

    while asyncio.get_event_loop().time() < deadline:
        # Strateji 1: get_by_role button/link
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=regex)
                n = await loc.count()
                for i in range(min(n, 10)):
                    el = loc.nth(i)
                    if await el.is_visible():
                        await el.scroll_into_view_if_needed()
                        await el.click()
                        return True
            except Exception:
                pass

        # Strateji 2: tüm metin eşleşmeleri — clickable parent'a yükselt
        try:
            loc = page.get_by_text(regex, exact=False)
            n = await loc.count()
            for i in range(min(n, 15)):
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                # Önce kendisi tıklanabilir mi?
                try:
                    await el.click(timeout=2000)
                    return True
                except Exception:
                    pass
                # Üst tıklanabilir parent'ı bul
                clickable = el.locator(
                    "xpath=ancestor-or-self::*[self::a or self::button or "
                    "@role='button' or contains(@class,'card') or "
                    "contains(@class,'btn') or contains(@class,'p-button')][1]"
                )
                try:
                    if await clickable.count() > 0:
                        await clickable.first.click(timeout=2000)
                        return True
                except Exception:
                    pass
        except Exception:
            pass

        await page.wait_for_timeout(800)

    return False


async def click_list_item(page: Page, search_text: str, debug: bool) -> bool:
    """
    Listede Türkçe-aware substring araması yapıp eşleşeni Playwright'in
    gerçek mouse event'iyle tıklar (Angular/PrimeNG handler'ları tetiklenir).
    Scroll'lu liste içinde de arar.
    """
    # JS: en spesifik (en küçük metinli) eşleşmeyi bul, küçük clickable
    # ancestor'a çık, koordinatlarını dön
    locate_js = """
    async ({needle, maxScrolls}) => {
        const norm = s => (s || '').toLocaleLowerCase('tr-TR').replace(/\\s+/g, ' ').trim();
        const parts = norm(needle).split(' ').filter(Boolean);

        const matches = (txt) => {
            const t = norm(txt);
            let idx = 0;
            for (const p of parts) {
                const i = t.indexOf(p, idx);
                if (i < 0) return false;
                idx = i + p.length;
            }
            return true;
        };

        // En yakın tıklanabilir ata; ama elementi çok büyütmüyor
        const findClickable = (el) => {
            let p = el;
            for (let i = 0; i < 5 && p && p !== document.body; i++) {
                const cs = getComputedStyle(p);
                const r = p.getBoundingClientRect();
                // Çok küçük veya çok büyük olmamalı (gerçek kart/buton)
                if (r.width >= 60 && r.height >= 20 &&
                    r.width <= 600 && r.height <= 400) {
                    if (
                        p.tagName === 'BUTTON' || p.tagName === 'A' ||
                        p.getAttribute('role') === 'button' ||
                        p.getAttribute('role') === 'option' ||
                        /card|btn|button|item|tile|option/i.test(p.className || '') ||
                        p.onclick != null || cs.cursor === 'pointer'
                    ) return p;
                }
                p = p.parentElement;
            }
            return el;  // fallback: text element kendisi
        };

        // En küçük (en spesifik) metinli eşleşmeyi bul
        const findTarget = () => {
            const candidates = [];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (el.children.length > 5) continue;
                const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!txt || txt.length > 120) continue;  // çok uzunsa pas
                if (!matches(txt)) continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                candidates.push({el, len: txt.length});
            }
            // En kısa metinli (= en spesifik) eşleşme
            candidates.sort((a, b) => a.len - b.len);
            return candidates.length ? findClickable(candidates[0].el) : null;
        };

        const scrollers = Array.from(document.querySelectorAll('*')).filter(el => {
            const cs = getComputedStyle(el);
            return (cs.overflowY === 'auto' || cs.overflowY === 'scroll') &&
                   el.scrollHeight > el.clientHeight + 5;
        });
        scrollers.push(document.scrollingElement || document.documentElement);

        let t = findTarget();
        for (let s = 0; !t && s < maxScrolls; s++) {
            for (const sc of scrollers) sc.scrollTop += 250;
            await new Promise(r => setTimeout(r, 200));
            t = findTarget();
        }

        if (!t) return {ok: false};

        t.scrollIntoView({block: 'center', behavior: 'instant'});
        await new Promise(r => setTimeout(r, 250));
        const r = t.getBoundingClientRect();
        return {
            ok: true,
            x: r.left + r.width / 2,
            y: r.top + r.height / 2,
            w: r.width,
            h: r.height,
            text: (t.innerText || '').replace(/\\s+/g, ' ').slice(0, 100),
            tag: t.tagName,
            cls: (t.className || '').toString().slice(0, 80)
        };
    }
    """
    try:
        result = await page.evaluate(locate_js, {"needle": search_text, "maxScrolls": 40})
        if not result.get("ok"):
            log(f"  ⚠️  Liste içinde bulunamadı")
            await snap(page, "01-list-not-found", debug)
            return False

        x, y = result["x"], result["y"]
        log(
            f"  → Hedef: <{result.get('tag','?')}> "
            f"({result.get('w',0):.0f}x{result.get('h',0):.0f}) "
            f"@ ({x:.0f}, {y:.0f}) — '{result['text'].strip()[:60]}'"
        )

        # Playwright gerçek mouse click — Angular/PrimeNG event handler tetiklenir
        await page.mouse.move(x, y)
        await page.wait_for_timeout(120)
        await page.mouse.click(x, y, delay=50)
        log("  ✓ Tıklandı (mouse click)")
        return True
    except Exception as e:
        log(f"  ⚠️  Tıklama hatası: {e}")
        return False


async def select_birim_from_list(page: Page, value_pattern: str, debug: bool) -> bool:
    """
    "Birim Seç" dropdown'unu açar, içindeki listeden value_pattern eşleşen
    seçeneği bulup tıklar. Liste uzunsa filtre input kullanır veya scroll yapar.
    """
    regex = re.compile(value_pattern, re.IGNORECASE | re.UNICODE)

    # 1) Birim Seç dropdown'unu aç
    log("  → 'Birim Seç' dropdown'u açılıyor...")
    opened = False
    for trigger_loc in [
        page.get_by_role("combobox", name=re.compile(r"birim", re.I)),
        page.get_by_text(re.compile(r"birim\s*se[çc]", re.I)),
        page.locator(".p-dropdown, .p-select, p-dropdown, p-select").first,
        page.locator("[role='combobox']").first,
    ]:
        try:
            if await trigger_loc.count() == 0:
                continue
            el = trigger_loc.first
            if not await el.is_visible():
                continue
            await el.scroll_into_view_if_needed()
            await el.click()
            opened = True
            break
        except Exception:
            continue

    if not opened:
        log("  ⚠️  Dropdown trigger bulunamadı")
        return False

    await page.wait_for_timeout(800)
    await snap(page, "01a-dropdown-acik", debug)

    # 2) Filtre input'una yaz (PrimeNG filterable select)
    filter_selectors = [
        ".p-dropdown-filter",
        ".p-select-filter",
        "input.p-inputtext",
        "[role='listbox'] input",
        ".p-dropdown-panel input",
        ".p-select-overlay input",
    ]
    typed = False
    # "Karaci" yazınca filtrelenir
    for sel in filter_selectors:
        try:
            inp = page.locator(sel).first
            if await inp.count() > 0 and await inp.is_visible():
                await inp.fill("KARACI")
                await page.wait_for_timeout(800)
                typed = True
                log("  → Filtre input'a 'KARACI' yazıldı")
                break
        except Exception:
            continue

    if typed:
        await snap(page, "01b-filtreli", debug)

    # 3) Açılan listede option ara
    option_selectors = [
        ".p-dropdown-item",
        ".p-select-option",
        "li.p-dropdown-item",
        "[role='option']",
        ".p-dropdown-items li",
    ]

    async def try_click_option() -> bool:
        for sel in option_selectors:
            try:
                opts = page.locator(sel)
                n = await opts.count()
                for i in range(n):
                    opt = opts.nth(i)
                    try:
                        text = (await opt.inner_text()).strip()
                    except Exception:
                        continue
                    if regex.search(text):
                        await opt.scroll_into_view_if_needed()
                        await opt.click()
                        log(f"  ✓ Seçildi: '{text}'")
                        return True
            except Exception:
                continue
        return False

    if await try_click_option():
        return True

    # 4) Görünür değilse panelde scroll yap
    log("  → Görünmüyor, listede scroll yapılıyor...")
    for sel in [".p-dropdown-items-wrapper", ".p-select-list-container",
                ".p-dropdown-panel", ".p-select-overlay", "[role='listbox']"]:
        try:
            panel = page.locator(sel).first
            if await panel.count() == 0:
                continue
            for _ in range(20):
                await panel.evaluate("el => el.scrollBy(0, 200)")
                await page.wait_for_timeout(300)
                if await try_click_option():
                    return True
        except Exception:
            continue

    return False


async def page_has_phrase(page: Page, phrase: str) -> bool:
    """Sayfa metninde (case-insensitive) phrase geçiyor mu?"""
    try:
        body = await page.locator("body").inner_text()
        return phrase.lower() in body.lower()
    except Exception:
        return False


async def detect_result(page: Page) -> tuple[bool, str]:
    """
    Poliklinik tıklandıktan sonra çalışır:
      - Alt-birim listesinin yüklenmesini bekler
      - Her alt-birimde "(DOLU)" var mı bakar
      - Toast'ta "Bulunmamaktadır" veya "Bulunamadı" var mı bakar
      - Hiçbiri yoksa → randevu var
    Dönüş: (randevu_var_mi, açıklama_metni)
    """
    # Alt-birim sayfasına özgü sinyalleri bekle:
    #   - "TAKİPLİ" / "YENİ HASTA" → alt-birim listesi geldi
    #   - "Bulunmamaktadır" / "Bulunamadı" → toast geldi (yine sonuç hazır)
    # Bu sinyaller orijinal poliklinik listesinde YOKTUR, sadece tıklamadan sonra çıkar
    wait_js = """
    () => {
        const txt = (document.body.innerText || '').toLocaleLowerCase('tr-TR');
        return /takipli|yeni\\s*hasta|bulunmamaktadır|bulunamadı/.test(txt);
    }
    """
    try:
        await page.wait_for_function(wait_js, timeout=15000)
    except PWTimeoutError:
        log("  ⚠️  Alt-birim sinyali 15s'de gelmedi, yine de okuyacağız")

    await page.wait_for_timeout(1800)  # toast / animasyon oturması için

    # Tüm görünür metni topla
    try:
        body = (await page.locator("body").inner_text()).strip()
    except Exception:
        body = ""

    body_lower = body.lower()

    # 1) Açık negatif sinyaller (toast veya mesaj) — bunlar varsa kesin yok
    for phrase in ("bulunmamaktadır", "bulunamadı",
                   "müsait randevu yok", "uygun randevu yok"):
        if phrase in body_lower:
            return (False, f"Toast/mesaj: '{phrase}'")

    # 2) Alt-birim listesini JS ile çıkar — sadece "TAKİPLİ"/"YENİ HASTA"
    #    içeren öğeler ki bunlar gerçek alt-birim öğeleridir
    extract_js = """
    () => {
        const items = [];
        const seen = new Set();
        const all = document.querySelectorAll(
            'button, [role="button"], li, div[class*="card"], div[class*="item"]'
        );
        for (const el of all) {
            if (el.children.length > 4) continue;
            const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!txt || txt.length > 200 || seen.has(txt)) continue;
            // Alt-birim öğeleri özellikle "TAKİPLİ" veya "YENİ HASTA" içerir
            const low = txt.toLocaleLowerCase('tr-TR');
            if (/takipli|yeni\\s*hasta/.test(low)) {
                seen.add(txt);
                items.push(txt);
            }
        }
        return items;
    }
    """
    try:
        sub_units = await page.evaluate(extract_js)
    except Exception:
        sub_units = []

    if not sub_units:
        snippet = body[:400].replace("\n", " ")
        return (False, f"Alt-birim listesi bulunamadı. Snippet: {snippet}")

    # 3) Alt-birimlerin her birinde DOLU işareti var mı?
    available = []
    dolu = []
    for s in sub_units:
        if "dolu" in s.lower():
            dolu.append(s)
        else:
            available.append(s)

    if available:
        return (True, "MÜSAİT alt-birim(ler): " + " | ".join(available[:5]))
    return (False, "Tüm alt-birimler DOLU: " + " | ".join(dolu[:5]))


async def run_check(debug: bool, no_mail: bool, headed: bool) -> int:
    log("Kontrol başladı: TOTM Karaciğer Nakil Polikliniği")
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        # Stealth init: bot detection bypass
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['tr-TR','tr','en-US','en'] });
            window.chrome = { runtime: {} };
            const orig = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : orig(p);
            """
        )
        page = await context.new_page()
        page.set_default_timeout(30000)

        # Network ve console hatalarını yakala (debug için)
        if debug:
            page.on("console", lambda msg: log(f"  [console.{msg.type}] {msg.text[:200]}"))
            page.on("requestfailed", lambda req: log(f"  [REQ-FAIL] {req.url} → {req.failure}"))
            page.on("response", lambda r: log(f"  [HTTP {r.status}] {r.url[:100]}") if r.status >= 400 else None)

        try:
            # ADIM 0a: Önce kök sayfaya git, SPA bootstrap olsun
            log("0a) Önce ana sayfa yükleniyor (SPA bootstrap için)...")
            await page.goto("https://totmhastaportali.mergentech.com.tr/", wait_until="domcontentloaded")
            await wait_settle(page, 3000)
            await snap(page, "00a-root", debug)

            # ADIM 0b: Hash URL'sine in-app navigate
            log("0b) /uyesiz-randevu rotasına geçiliyor...")
            await page.evaluate(f"window.location.hash = '#/auth/uyesiz-randevu'")
            await wait_settle(page, 2000)
            await page.goto(PORTAL_URL, wait_until="domcontentloaded")
            await wait_settle(page, 2000)

            # Angular render olana kadar bekle (ECDH handshake + ilk render)
            log("   → Angular render olmasını bekliyor...")
            ready = await wait_for_content(page, min_text_len=300, timeout_ms=45000)
            if not ready:
                log("   ⚠️  30s'de content render olmadı, yine de devam ediliyor")
            await page.wait_for_timeout(2000)
            await dismiss_dialogs(page)
            await page.wait_for_timeout(1500)
            await snap(page, "00-landing", debug)

            # ADIM 1a: Önce "HASTANE RANDEVU" kartına tıkla (poliklinik listesi onun arkasında)
            log("1a) 'HASTANE RANDEVU' tıklanıyor...")
            clicked_hr = await click_list_item(page, "HASTANE RANDEVU", debug)
            if not clicked_hr:
                raise RuntimeError("'HASTANE RANDEVU' butonu bulunamadı")
            # Poliklinik listesi yüklenene kadar bekle (yaygın bir poliklinik adı varlığıyla)
            try:
                await page.wait_for_function(
                    "() => /algoloji|dermatol|kardiyol|karaciğer|jinekoloji/i.test("
                    "document.body.innerText || '')",
                    timeout=15000,
                )
            except PWTimeoutError:
                log("  ⚠️  Poliklinik listesi 15s'de yüklenmedi, yine de denenecek")
            await page.wait_for_timeout(1200)
            await snap(page, "01-poliklinik-liste", debug)

            # ADIM 1b: "Birim Seç" listesinden poliklinik tıkla
            log(f"1b) Listede poliklinik aranıyor: '{STEP_POLIKLINIK}'")
            clicked = await click_list_item(page, STEP_POLIKLINIK, debug)
            if not clicked:
                log("  → Tam ad bulunamadı, daha kısa ad deneniyor: 'KARACİĞER NAKİL ENS'")
                clicked = await click_list_item(page, "KARACİĞER NAKİL ENS", debug)
            if not clicked:
                raise RuntimeError("Karaciğer Nakil polikliniği seçilemedi")

            await wait_settle(page, 4000)
            await snap(page, "02-sonuc", debug)

            # ADIM 4: Sonucu yorumla
            has_appt, msg = await detect_result(page)
            log(f"SONUÇ → randevu_var={has_appt}")
            log(f"        {msg}")

            # Önceki state ile karşılaştır
            prev_has = False
            if STATE_FILE.exists():
                try:
                    prev = json.loads(STATE_FILE.read_text())
                    prev_has = prev.get("has_appointments", False)
                except Exception:
                    pass

            new_state = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "has_appointments": has_appt,
                "message": msg,
            }
            STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2))

            # Sadece yok→var geçişinde mail at (her 30 dk'da spam'ı önlemek için)
            should_mail = has_appt and not prev_has
            if should_mail and not no_mail:
                subject = "🟢 TOTM Karaciğer Nakil — RANDEVU AÇILDI!"
                body = (
                    f"Müsait randevu bulundu! Hemen siteye girip TC ile rezerve edin:\n\n"
                    f"{PORTAL_URL}\n\n"
                    f"Detay: {msg}\n\n"
                    f"Kontrol zamanı: {new_state['checked_at']}\n"
                )
                send_mail(subject, body)
                log("📧 Bildirim maili gönderildi")
            elif has_appt and prev_has:
                log("ℹ️  Randevu hâlâ açık (önceki kontrol de açıktı) — mail atılmadı")
            else:
                log("ℹ️  Randevu yok — mail atılmadı")

            await browser.close()
            return 0

        except Exception as e:
            log(f"❌ HATA: {e}")
            traceback.print_exc()
            try:
                await snap(page, "99-error", True)  # hatada her zaman screenshot
            except Exception:
                pass
            await browser.close()
            return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Her adımda screenshot + HTML dump")
    parser.add_argument("--headed", action="store_true", help="Tarayıcı görünür (WSLg/X gerektirir)")
    parser.add_argument("--no-mail", action="store_true", help="Mail gönderme")
    args = parser.parse_args()
    return asyncio.run(run_check(debug=args.debug, no_mail=args.no_mail, headed=args.headed))


if __name__ == "__main__":
    sys.exit(main())
