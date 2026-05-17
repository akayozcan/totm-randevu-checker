# TOTM Randevu Kontrolcüsü

Malatya İnönü Üniversitesi TOTM Hastanesi **Karaciğer Nakil Polikliniği** için randevu açıldığında otomatik olarak mail atan sistem. GitHub Actions üzerinde **30 dakikada bir** çalışır.

## Nasıl Çalışır

1. GitHub Actions cron her 30 dakikada bir tetiklenir.
2. Playwright (headless Chromium) [hasta portalına](https://totmhastaportali.mergentech.com.tr) gider, **üyesiz randevu** akışını dolaşır:
   - Portal → **"Randevu Al"**
   - → **"HASTANE RANDEVU"**
   - → **"KARACİĞER NAKİL ENS.POL.1"**
3. Açılan sayfa **"dolu"** içeriyorsa: randevu yok, mail atılmaz.
4. Aksi halde (tarih/saat görünüyorsa): randevu var → Gmail SMTP üzerinden mail gönderilir.
5. Spam önlemek için sadece **"yok → var"** geçişlerinde mail atılır; randevu açık kaldığı sürece tekrar mail gelmez.

## Kurulum

### 1) Repo'yu kendi GitHub hesabınızda oluşturun

```bash
cd /mnt/c/Users/akay44/Desktop/totm-randevu-checker
git init
git add .
git commit -m "İlk kurulum: TOTM randevu kontrolcüsü"

# GitHub'da boş bir repo açın (örn: totm-randevu-checker) ve:
git remote add origin https://github.com/<KULLANICI_ADINIZ>/totm-randevu-checker.git
git branch -M main
git push -u origin main
```

> ⚠️ Repo **public** ise GitHub Actions tamamen ücretsiz. Private repo'da aylık 2000 dakika ücretsiz hakkınız var (30 dk'da bir × günde 48 × ~1 dk = ~1440 dk/ay — yine de yeterli).

### 2) Gmail App Password oluşturun

1. `akymltya44@gmail.com` ile [myaccount.google.com](https://myaccount.google.com)'a girin.
2. **Security → 2-Step Verification**'ı aktif edin (zaten değilse).
3. **Security → App passwords** sayfasında yeni bir app password oluşturun. Adına `totm-randevu` yazabilirsiniz.
4. Çıkan **16 haneli şifreyi** kopyalayın (boşluksuz hâli: ör. `abcdwxyzabcdwxyz`).

### 3) GitHub Secrets ekleyin

Repo'da: **Settings → Secrets and variables → Actions → New repository secret**

| Secret adı           | Değer                                |
|----------------------|--------------------------------------|
| `GMAIL_USER`         | `akymltya44@gmail.com`               |
| `GMAIL_APP_PASSWORD` | Adım 2'de aldığınız 16 haneli şifre  |
| `MAIL_TO`            | `akymltya44@gmail.com` (opsiyonel)   |

### 4) Çalışmayı test edin

Repo → **Actions** sekmesi → **TOTM Randevu Kontrol** → **Run workflow** ile manuel tetikleyin. İlk çalışmada log'ları izleyin.

## Lokal Test (WSL üzerinde)

```bash
cd /mnt/c/Users/akay44/Desktop/totm-randevu-checker
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium

# Sadece sayfayı dolaşıp screenshot al (mail atmadan, tarayıcı görünür):
python check_appointment.py --debug --no-mail

# Tüm screenshot'lar debug_screenshots/ klasörüne düşer.
```

Mail testini ayrı çalıştırın:

```bash
export GMAIL_USER="akymltya44@gmail.com"
export GMAIL_APP_PASSWORD="<16-haneli-app-password>"
python notify.py
```

## Selector'lar Tutmazsa

Site arayüzü değişirse veya ilk çalıştırmada akış kırılırsa:

1. `python check_appointment.py --debug --no-mail` çalıştırın.
2. `debug_screenshots/` içindeki ekran görüntülerine bakın. Hangi adımda takıldığını göreceksiniz (`01-landing.png`, `02-form.png`, …, `99-error.png`).
3. İlgili adımdaki `.html` dosyasından gerçek selector'ları bulup `check_appointment.py` içindeki `select_primeng_dropdown(page, INDEX, "PATTERN")` indeks ve pattern'lerini düzeltin.

## Sınırlar ve Notlar

- Bu sistem sadece **müsaitliği** kontrol eder. Randevuyu **kesinleştirmek için kendiniz** TC kimlik ile siteye girip rezervasyonu tamamlamalısınız.
- 30 dakikalık aralık makul. Daha sık çalıştırmak hem etik dışı hem de IP banlanmasına sebep olabilir.
- GitHub Actions cron'u yüksek yük altındayken birkaç dakika gecikebilir; bu normaldir.
- Site geçici olarak yavaşlarsa script `01-landing` veya `02-form` adımında timeout edebilir. Log'larda `99-error.png` artifact'ına bakın.

## Dosya Yapısı

```
totm-randevu-checker/
├── check_appointment.py       # Ana Playwright script'i
├── notify.py                  # Gmail SMTP mail modülü
├── requirements.txt
├── .gitignore
├── README.md
└── .github/
    └── workflows/
        └── check.yml          # 30 dk cron + Actions runner
```
