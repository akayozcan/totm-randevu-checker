"""Gmail SMTP üzerinden bildirim maili gönderir.

Gerekli ortam değişkenleri:
    GMAIL_USER           Gönderici Gmail (ör. akymltya44@gmail.com)
    GMAIL_APP_PASSWORD   Gmail App Password (16 hane, boşluksuz)
    MAIL_TO              Hedef adres (varsayılan: GMAIL_USER)
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def _load_dotenv() -> None:
    """Proje kökündeki .env dosyasını oku; mevcut env'i ezme."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


def send_mail(subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to_addr = os.environ.get("MAIL_TO", user).strip() or user

    if not user or not pw:
        raise RuntimeError(
            "GMAIL_USER ve GMAIL_APP_PASSWORD environment variable'ları gerekli"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(user, pw)
        s.send_message(msg)


if __name__ == "__main__":
    send_mail(
        "TOTM Randevu Checker — test maili",
        "Bu bir test mailidir. Kurulum başarılı.",
    )
    print("✓ Test maili gönderildi")
