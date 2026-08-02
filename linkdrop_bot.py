"""
LinkDrop v1.1 - Tek Dosyalık Telegram Video İndirme Botu
=====================================================
TikTok, Instagram, YouTube linklerini indirir.

Yenilikler (v1.1):
  - Telegram sistem diline göre otomatik dil algılama (AZ/TR/EN)
  - Buton tabanlı admin paneli (/panel, veya /admin <kod> ile giriş)
  - Instagram indirme başarısını artırmak için cookie (cookies.txt) ve
    mobil User-Agent desteği

Kurulum:
    pip install aiogram yt-dlp

Çalıştırma:
    python linkdrop_bot.py

Not: Aşağıdaki BOT_TOKEN alanını kendi tokeninle değiştir, ya da
BOT_TOKEN ortam değişkeni olarak set et (tercih edilen, daha güvenli yol).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =========================================================================
# 1) KONFİQURASİYA
# =========================================================================

# Tokeni birbaşa buraya yaza bilərsən (sürətli test üçün) və ya
# terminalda: export BOT_TOKEN="..." yazıb ortam dəyişəni kimi ver (daha təhlükəsiz).
BOT_TOKEN = os.getenv("BOT_TOKEN", "8969379656:AAFsdiRxoHylAcKswlf9unRYu9anPVPAF7g")

# Admin Telegram user ID-ləri (bura öz ID-ni yaza bilərsən, amma məcburi deyil —
# aşağıdakı ADMIN_CODE ilə botda /admin <kod> yazaraq da admin ola bilərsən)
ADMIN_IDS: list[int] = []  # məsələn: [123456789]

# Bu kodu botda "/admin zenty001" kimi yazan şəxs avtomatik admin olur.
# Təhlükəsizlik üçün bu kodu dəyişməyi tövsiyə edirik.
ADMIN_CODE = "zenty001"

# Reklam bölümü: video göndərildikdən sonra istifadəçiyə göstərilən kiçik reklam.
# Admin panelindən söndürüb-yandırmaq mümkündür (aşağıda AD_ENABLED runtime dəyəri).
AD_LINK = "https://t.me/standoff2hack389"
AD_TEXT: dict[str, str] = {
    "az": "🎮 Pulsuz Standoff 2 / PUBG Hile — kanala qoşul",
    "tr": "🎮 Ücretsiz Standoff 2 / PUBG Hile — kanala katıl",
    "en": "🎮 Free Standoff 2 / PUBG Hack — join the channel",
}

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DB_PATH = BASE_DIR / "linkdrop.db"

# Instagram anonim sorğuları getdikcə daha çox rədd edir. Bu fayl mövcuddursa
# (Netscape formatında, "Get cookies.txt LOCALLY" kimi brauzer əlavəsi ilə
# ixrac edilmiş), yt-dlp "giriş etmiş" kimi davranır və uğur nisbəti artır.
# Faylı repo-nun kökünə "cookies.txt" adı ilə əlavə etmək kifayətdir.
COOKIES_FILE = BASE_DIR / "cookies.txt"

# Bəzi platformalar (xüsusən Instagram) botlara xas User-Agent-ləri rədd edir,
# ona görə adi bir mobil brauzer User-Agent-i istifadə edirik.
DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

MAX_FILE_SIZE_MB = 50
DOWNLOAD_TIMEOUT_SECONDS = 240
RATE_LIMIT_SECONDS = 3.0          # ardıcıl sorğular arası minimum fasilə
MAX_REQUESTS_PER_MINUTE = 15      # istifadəçi başına dəqiqəlik limit
DEFAULT_LANGUAGE = "az"

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================================
# 2) LOGGING
# =========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logger = logging.getLogger("linkdrop")

# =========================================================================
# 3) VERİLƏNLƏR BAZASI (sqlite3, sadəlik üçün senkron + thread-safe wrapper)
# =========================================================================

_db_lock = asyncio.Lock()


@contextmanager
def _connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db_sync() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language TEXT NOT NULL DEFAULT 'az',
                is_blocked INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


async def init_db() -> None:
    async with _db_lock:
        await asyncio.to_thread(_init_db_sync)
    logger.info("Verilənlər bazası hazır: %s", DB_PATH)


async def get_or_create_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
    telegram_language_code: str | None = None,
) -> sqlite3.Row:
    def _work() -> sqlite3.Row:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                detected_lang = detect_user_language(telegram_language_code)
                conn.execute(
                    "INSERT INTO users (id, username, first_name, language) VALUES (?, ?, ?, ?)",
                    (user_id, username, first_name, detected_lang),
                )
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            else:
                conn.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE id = ?",
                    (username, first_name, user_id),
                )
            return row

    async with _db_lock:
        return await asyncio.to_thread(_work)


async def set_user_language(user_id: int, language: str) -> None:
    def _work() -> None:
        with _connect() as conn:
            conn.execute("UPDATE users SET language = ? WHERE id = ?", (language, user_id))

    async with _db_lock:
        await asyncio.to_thread(_work)


async def set_user_blocked(user_id: int, blocked: bool) -> bool:
    def _work() -> bool:
        with _connect() as conn:
            row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE users SET is_blocked = ? WHERE id = ?", (int(blocked), user_id))
            return True

    async with _db_lock:
        return await asyncio.to_thread(_work)


async def get_all_active_user_ids() -> list[int]:
    def _work() -> list[int]:
        with _connect() as conn:
            rows = conn.execute("SELECT id FROM users WHERE is_blocked = 0").fetchall()
            return [r["id"] for r in rows]

    async with _db_lock:
        return await asyncio.to_thread(_work)


async def log_download(user_id: int, platform: str, url: str, status: str, error_message: str | None = None) -> None:
    def _work() -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO download_logs (user_id, platform, url, status, error_message) VALUES (?, ?, ?, ?, ?)",
                (user_id, platform, url, status, error_message),
            )

    async with _db_lock:
        await asyncio.to_thread(_work)


# Runtime-da bütün admin ID-lərini saxlayan set. Başlanğıcda ADMIN_IDS-dən
# doldurulur, sonra init_db() içində DB-dən əlavə edilənlər də qoşulur.
admin_user_ids: set[int] = set(ADMIN_IDS)


async def add_admin_to_db(user_id: int) -> None:
    def _work() -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,)
            )

    async with _db_lock:
        await asyncio.to_thread(_work)
    admin_user_ids.add(user_id)


async def load_admin_ids_from_db() -> None:
    def _work() -> list[int]:
        with _connect() as conn:
            rows = conn.execute("SELECT user_id FROM admins").fetchall()
            return [r["user_id"] for r in rows]

    async with _db_lock:
        db_ids = await asyncio.to_thread(_work)
    admin_user_ids.update(db_ids)


async def set_setting(key: str, value: str) -> None:
    def _work() -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async with _db_lock:
        await asyncio.to_thread(_work)


async def load_settings_from_db() -> dict[str, str]:
    def _work() -> dict[str, str]:
        with _connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            return {r["key"]: r["value"] for r in rows}

    async with _db_lock:
        return await asyncio.to_thread(_work)


async def get_stats() -> dict:
    def _work() -> dict:
        with _connect() as conn:
            total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            total_downloads = conn.execute("SELECT COUNT(*) c FROM download_logs").fetchone()["c"]
            today = conn.execute(
                "SELECT COUNT(*) c FROM download_logs WHERE date(created_at) = date('now')"
            ).fetchone()["c"]
            success = conn.execute(
                "SELECT COUNT(*) c FROM download_logs WHERE status = 'success'"
            ).fetchone()["c"]
            by_platform = conn.execute(
                "SELECT platform, COUNT(*) c FROM download_logs GROUP BY platform"
            ).fetchall()
            rate = round((success / total_downloads) * 100, 2) if total_downloads else 0.0
            return {
                "total_users": total_users,
                "total_downloads": total_downloads,
                "today": today,
                "success_rate": rate,
                "by_platform": {r["platform"]: r["c"] for r in by_platform},
            }

    async with _db_lock:
        return await asyncio.to_thread(_work)


# =========================================================================
# 4) ÇOX DİLLİ MƏTNLƏR (AZ / TR / EN)
# =========================================================================

TEXTS: dict[str, dict[str, str]] = {
    "welcome": {
        "az": "👋 Salam, {name}!\n\nMən <b>LinkDrop</b> botuyam. TikTok, Instagram və YouTube linklərini göndər, videonu endirim.\n\nKömək üçün /help yaz.",
        "tr": "👋 Merhaba, {name}!\n\nBen <b>LinkDrop</b> botuyum. TikTok, Instagram ve YouTube linklerini gönder, videoyu indireyim.\n\nYardım için /help yaz.",
        "en": "👋 Hello, {name}!\n\nI'm <b>LinkDrop</b>. Send a link from TikTok, Instagram or YouTube and I'll download it.\n\nType /help for help.",
    },
    "help": {
        "az": "ℹ️ Video linkini göndər, mən endirib sənə göndərəcəm.\nDəstəklənən: TikTok, Instagram, YouTube\nDil dəyişmək üçün: /language",
        "tr": "ℹ️ Video linkini gönder, ben indirip sana göndereceğim.\nDesteklenen: TikTok, Instagram, YouTube\nDil değiştirmek için: /language",
        "en": "ℹ️ Send a video link and I'll fetch it for you.\nSupported: TikTok, Instagram, YouTube\nTo change language: /language",
    },
    "choose_language": {"az": "🌐 Dilinizi seçin:", "tr": "🌐 Dilinizi seçin:", "en": "🌐 Choose your language:"},
    "language_set": {"az": "✅ Dil Azərbaycan dilinə dəyişdirildi.", "tr": "✅ Dil Türkçe olarak değiştirildi.", "en": "✅ Language set to English."},
    "preparing": {"az": "⏳ Video hazırlanır, gözləyin...", "tr": "⏳ Video hazırlanıyor, bekleyin...", "en": "⏳ Preparing your video..."},
    "invalid_link": {
        "az": "❌ Bu link dəstəklənmir. Dəstəklənən: TikTok, Instagram, YouTube",
        "tr": "❌ Bu link desteklenmiyor. Desteklenen: TikTok, Instagram, YouTube",
        "en": "❌ Link not supported. Supported: TikTok, Instagram, YouTube",
    },
    "no_url_found": {"az": "🔗 Zəhmət olmasa video linki göndər.", "tr": "🔗 Lütfen bir video linki gönder.", "en": "🔗 Please send a video link."},
    "download_failed": {"az": "⚠️ Video endirilmədi. Link məhdud/silinmiş ola bilər.", "tr": "⚠️ Video indirilemedi. Link kısıtlı/silinmiş olabilir.", "en": "⚠️ Download failed. The link may be private or deleted."},
    "file_too_large": {"az": "⚠️ Video çox böyükdür (limit: {limit} MB).", "tr": "⚠️ Video çok büyük (limit: {limit} MB).", "en": "⚠️ Video too large (limit: {limit} MB)."},
    "blocked": {"az": "🚫 Botdan istifadə etməkdən məhrum edilmisiniz.", "tr": "🚫 Bottan yararlanmanız engellendi.", "en": "🚫 You are blocked from using this bot."},
    "rate_limited": {"az": "⏱ Çox tez sorğu göndərirsiniz, gözləyin.", "tr": "⏱ Çok hızlı istek gönderiyorsun, bekle.", "en": "⏱ Too many requests, please slow down."},
    "success_caption": {"az": "✅ Buyurun! 🎬 Platform: {platform}", "tr": "✅ Buyurun! 🎬 Platform: {platform}", "en": "✅ Here you go! 🎬 Platform: {platform}"},
    "admin_only": {"az": "⛔ Bu əmr yalnız adminlər üçündür.", "tr": "⛔ Bu komut sadece yöneticiler içindir.", "en": "⛔ Admins only."},
    "user_not_found": {"az": "❌ İstifadəçi tapılmadı.", "tr": "❌ Kullanıcı bulunamadı.", "en": "❌ User not found."},
    "blocked_ok": {"az": "✅ İstifadəçi {uid} bloklandı.", "tr": "✅ Kullanıcı {uid} engellendi.", "en": "✅ User {uid} blocked."},
    "unblocked_ok": {"az": "✅ İstifadəçi {uid} blokdan çıxarıldı.", "tr": "✅ Kullanıcı {uid} engeli kaldırıldı.", "en": "✅ User {uid} unblocked."},
    "broadcast_usage": {"az": "İstifadə: /broadcast <mesaj>", "tr": "Kullanım: /broadcast <mesaj>", "en": "Usage: /broadcast <message>"},
    "broadcast_done": {"az": "📢 Tamamlandı. Göndərildi: {sent}, Uğursuz: {failed}", "tr": "📢 Tamamlandı. Gönderildi: {sent}, Başarısız: {failed}", "en": "📢 Done. Sent: {sent}, Failed: {failed}"},
    "panel_title": {
        "az": "🛠 <b>LinkDrop Admin Panel</b>\n\nAşağıdan bir əməliyyat seç:",
        "tr": "🛠 <b>LinkDrop Admin Panel</b>\n\nAşağıdan bir işlem seç:",
        "en": "🛠 <b>LinkDrop Admin Panel</b>\n\nChoose an action below:",
    },
    "panel_ask_broadcast": {
        "az": "📢 Bütün istifadəçilərə göndəriləcək mesajı yaz:",
        "tr": "📢 Tüm kullanıcılara gönderilecek mesajı yaz:",
        "en": "📢 Type the message to broadcast to all users:",
    },
    "panel_ask_block": {
        "az": "🚫 Bloklanacaq istifadəçinin Telegram ID-sini yaz:",
        "tr": "🚫 Engellenecek kullanıcının Telegram ID'sini yaz:",
        "en": "🚫 Send the Telegram ID of the user to block:",
    },
    "panel_ask_unblock": {
        "az": "✅ Blokdan çıxarılacaq istifadəçinin Telegram ID-sini yaz:",
        "tr": "✅ Engeli kaldırılacak kullanıcının Telegram ID'sini yaz:",
        "en": "✅ Send the Telegram ID of the user to unblock:",
    },
    "panel_invalid_id": {
        "az": "❌ Düzgün bir Telegram ID yaz (yalnız rəqəm).",
        "tr": "❌ Geçerli bir Telegram ID yaz (sadece rakam).",
        "en": "❌ Please send a valid Telegram ID (numbers only).",
    },
    "panel_closed": {"az": "Panel bağlandı.", "tr": "Panel kapatıldı.", "en": "Panel closed."},
}


def t(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in ("az", "tr", "en") else DEFAULT_LANGUAGE
    template = TEXTS.get(key, {}).get(lang) or TEXTS.get(key, {}).get("en") or key
    return template.format(**kwargs) if kwargs else template


def detect_user_language(telegram_language_code: str | None) -> str:
    """Telegram-ın verdiyi sistem dili kodunu (məs. 'az', 'tr-TR', 'ru') bota uyğun
    dilə çevirir. Bota naməlum dillər üçün DEFAULT_LANGUAGE-ə düşür (yalnız yeni
    istifadəçi üçün işlədilir — mövcud istifadəçinin dilini dəyişmir)."""
    if not telegram_language_code:
        return DEFAULT_LANGUAGE
    code = telegram_language_code.lower().split("-")[0]
    if code in ("az", "tr", "en"):
        return code
    return DEFAULT_LANGUAGE


# =========================================================================
# 5) LİNK ALQORİTMASI (allow-list əsaslı platform algılama)
# =========================================================================

ALLOWED_DOMAINS: dict[str, tuple[str, ...]] = {
    "tiktok": ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "youtube": ("youtube.com", "youtu.be", "m.youtube.com"),
}

_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    if not text:
        return None
    match = _URL_PATTERN.search(text.strip())
    return match.group(0) if match else None


def detect_platform(url: str) -> str:
    try:
        hostname = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return "unknown"
    for platform, domains in ALLOWED_DOMAINS.items():
        if any(hostname == d or hostname.endswith("." + d) for d in domains):
            return platform
    return "unknown"


def is_supported_url(url: str) -> bool:
    return detect_platform(url) != "unknown"


# =========================================================================
# 6) VİDEO ENDİRMƏ (yt-dlp, thread-də işləyir ki event loop bloklanmasın)
# =========================================================================

class DownloadError(Exception):
    pass


class FileTooLargeError(DownloadError):
    pass


@dataclass(slots=True)
class DownloadResult:
    file_path: Path
    file_size_bytes: int


_download_semaphore = asyncio.Semaphore(10)


def _download_sync(url: str, platform: str) -> DownloadResult:
    unique_id = uuid.uuid4().hex[:10]
    output_template = str(DOWNLOADS_DIR / f"{unique_id}_%(id)s.%(ext)s")
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

    ydl_opts = {
        "outtmpl": output_template,
        # Əvvəlcə 720p-dən aşağı, tək fayl (audio+video birləşmiş) formatı seç —
        # Telegram-ın 50MB limitinə uyğun və vaxt aşımı riskini azaldır.
        # Tapılmasa, mövcud ən yaxşı mp4-ə, sonra istənilən formata düşür.
        "format": (
            "best[ext=mp4][height<=720][filesize<50M]/"
            "best[ext=mp4][height<=720]/best[ext=mp4]/best"
        ),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": max_bytes,
        "retries": 3,
        "extractor_retries": 3,
        "socket_timeout": DOWNLOAD_TIMEOUT_SECONDS,
        "merge_output_format": "mp4",
        "restrictfilenames": True,
    }

    # Xüsusi mobil User-Agent yalnız Instagram üçün tətbiq olunur.
    # TikTok və YouTube-un daxili yt-dlp ekstraktorları öz platformasına uyğun
    # header/fingerprint-i özü idarə edir — bunu qlobal əvəz etmək onları poza bilər.
    if platform == "instagram":
        ydl_opts["http_headers"] = {"User-Agent": DOWNLOAD_USER_AGENT}

    # Cookie faylı varsa (Instagram və digər giriş tələb edən platformalar üçün) istifadə et
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise DownloadError("Video məlumatı alınamadı.")
            file_path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc)
        logger.warning("yt-dlp xətası: %s | url=%s", message, url)
        if "max-filesize" in message.lower() or "too large" in message.lower():
            raise FileTooLargeError(message) from exc
        raise DownloadError(message) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gözlənilməz endirmə xətası: url=%s", url)
        raise DownloadError(str(exc)) from exc

    if not file_path.exists():
        raise DownloadError("Endirilən fayl tapılmadı.")

    file_size = file_path.stat().st_size
    if file_size > max_bytes:
        file_path.unlink(missing_ok=True)
        raise FileTooLargeError(f"Fayl limiti aşır: {file_size} bytes")

    return DownloadResult(file_path=file_path, file_size_bytes=file_size)


async def download_video(url: str, platform: str) -> DownloadResult:
    async with _download_semaphore:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_download_sync, url, platform), timeout=DOWNLOAD_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            raise DownloadError("Endirmə vaxtı bitdi.") from exc


def cleanup_file(file_path: Path) -> None:
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Fayl silinmədi: %s", file_path)


def _extract_audio_sync(video_path: Path) -> Path | None:
    """Endirilmiş videodan ffmpeg ilə MP3 audio çıxarır. ffmpeg yoxdursa
    və ya çıxarma uğursuz olsa None qaytarır (video göndərişini pozmasın deyə)."""
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg tapılmadı — audio (mp3) çıxarıla bilmir, yalnız video göndəriləcək.")
        return None

    audio_path = video_path.with_suffix(".mp3")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(video_path),
                "-vn", "-acodec", "libmp3lame", "-q:a", "4",
                str(audio_path),
            ],
            capture_output=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Audio çıxarma zamanı gözlənilməz xəta: %s", video_path)
        return None

    if result.returncode != 0 or not audio_path.exists():
        logger.warning("ffmpeg audio çıxara bilmədi (kod=%s): %s", result.returncode, video_path)
        return None

    return audio_path


async def extract_audio(video_path: Path) -> Path | None:
    return await asyncio.to_thread(_extract_audio_sync, video_path)


# =========================================================================
# 7) RATE LIMIT / FLOOD QORUMASI (yaddaşda saxlanan sadə həll)
# =========================================================================

_last_request: dict[int, float] = {}
_request_windows: dict[int, list[float]] = {}


def is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()

    last = _last_request.get(user_id, 0.0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _last_request[user_id] = now

    window = _request_windows.setdefault(user_id, [])
    window.append(now)
    window[:] = [ts for ts in window if now - ts <= 60]
    return len(window) > MAX_REQUESTS_PER_MINUTE


# =========================================================================
# 8) KLAVİATURA
# =========================================================================

def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇦🇿 Azərbaycanca", callback_data="lang:az"),
                InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="lang:tr"),
            ],
            [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en")],
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    ads_label = "📣 Reklamı söndür" if ads_enabled else "📣 Reklamı yandır"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Statistika", callback_data="panel:stats")],
            [InlineKeyboardButton(text="📢 Broadcast", callback_data="panel:broadcast")],
            [
                InlineKeyboardButton(text="🚫 Blokla", callback_data="panel:block"),
                InlineKeyboardButton(text="✅ Blokdan çıxar", callback_data="panel:unblock"),
            ],
            [InlineKeyboardButton(text=ads_label, callback_data="panel:toggle_ads")],
            [
                InlineKeyboardButton(text="✏️ Reklam mətni", callback_data="panel:edit_ad_text"),
                InlineKeyboardButton(text="🔗 Reklam linki", callback_data="panel:edit_ad_link"),
            ],
            [InlineKeyboardButton(text="✖️ Bağla", callback_data="panel:close")],
        ]
    )


def ad_keyboard(lang: str) -> InlineKeyboardMarkup:
    text = ad_text_override or AD_TEXT.get(lang, AD_TEXT["az"])
    link = ad_link_override or AD_LINK
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=link)]])


# =========================================================================
# 9) BOT / DISPATCHER VƏ HANDLER-LƏR
# =========================================================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def is_admin(user_id: int) -> bool:
    return user_id in admin_user_ids


@dp.message(Command("admin"))
async def handle_admin_login(message: Message) -> None:
    """Gizli koda görə istifadəçini admin edir: /admin <kod>"""
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    lang = user["language"]
    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2:
        await message.answer("İstifadə: /admin <kod>")
        return

    entered_code = parts[1].strip()
    if entered_code != ADMIN_CODE:
        await message.answer("❌ Kod yanlışdır.")
        return

    if is_admin(message.from_user.id):
        await message.answer(t("panel_title", lang), reply_markup=admin_panel_keyboard())
        return

    await add_admin_to_db(message.from_user.id)
    logger.info("Yeni admin əlavə edildi: %s", message.from_user.id)
    await message.answer("✅ Təbriklər, admin oldun!")
    await message.answer(t("panel_title", lang), reply_markup=admin_panel_keyboard())


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    if user["is_blocked"]:
        await message.answer(t("blocked", user["language"]))
        return
    name = message.from_user.first_name or "dostum"
    await message.answer(t("welcome", user["language"], name=name))


@dp.message(Command("help"))
async def handle_help(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    if user["is_blocked"]:
        await message.answer(t("blocked", user["language"]))
        return
    await message.answer(t("help", user["language"]))


@dp.message(Command("language"))
async def handle_language(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    await message.answer(t("choose_language", user["language"]), reply_markup=language_keyboard())


@dp.callback_query(F.data.startswith("lang:"))
async def handle_language_callback(callback: CallbackQuery) -> None:
    lang_code = callback.data.split(":", maxsplit=1)[1]
    await set_user_language(callback.from_user.id, lang_code)
    await callback.message.edit_text(t("language_set", lang_code))
    await callback.answer()


# Admin panelinin "növbəti mesajı gözləyən" vəziyyəti (broadcast/block/unblock üçün)
admin_pending: dict[int, str] = {}

# Reklamın aktiv olub-olmadığı — admin panelindən "Reklamı söndür/yandır" ilə dəyişilir
ads_enabled: bool = True

# Admin panelindən dəyişdirilə bilən reklam mətni/linki. None olduqda
# faylın başındakı standart AD_TEXT/AD_LINK istifadə olunur.
ad_text_override: str | None = None
ad_link_override: str | None = None


@dp.message(Command("panel"))
async def handle_panel(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    if not is_admin(message.from_user.id):
        await message.answer(t("admin_only", user["language"]))
        return
    await message.answer(t("panel_title", user["language"]), reply_markup=admin_panel_keyboard())


@dp.callback_query(F.data.startswith("panel:"))
async def handle_panel_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(t("admin_only", DEFAULT_LANGUAGE), show_alert=True)
        return

    user = await get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.first_name,
        callback.from_user.language_code,
    )
    lang = user["language"]
    action = callback.data.split(":", maxsplit=1)[1]

    if action == "stats":
        stats = await get_stats()
        lines = "\n".join(f"  • {p}: {c}" for p, c in stats["by_platform"].items()) or "  —"
        text = (
            "📊 <b>LinkDrop İstatistikləri</b>\n\n"
            f"👥 Toplam istifadəçi: <b>{stats['total_users']}</b>\n"
            f"⬇️ Toplam endirmə: <b>{stats['total_downloads']}</b>\n"
            f"📅 Bugünkü: <b>{stats['today']}</b>\n"
            f"✅ Uğur nisbəti: <b>{stats['success_rate']}%</b>\n\n"
            f"📱 Platform üzrə:\n{lines}"
        )
        await callback.message.answer(text)
        await callback.answer()

    elif action == "broadcast":
        admin_pending[callback.from_user.id] = "broadcast"
        await callback.message.answer(t("panel_ask_broadcast", lang))
        await callback.answer()

    elif action == "block":
        admin_pending[callback.from_user.id] = "block"
        await callback.message.answer(t("panel_ask_block", lang))
        await callback.answer()

    elif action == "unblock":
        admin_pending[callback.from_user.id] = "unblock"
        await callback.message.answer(t("panel_ask_unblock", lang))
        await callback.answer()

    elif action == "toggle_ads":
        global ads_enabled
        ads_enabled = not ads_enabled
        status_text = "yandırıldı ✅" if ads_enabled else "söndürüldü ❌"
        await callback.message.edit_text(t("panel_title", lang), reply_markup=admin_panel_keyboard())
        await callback.answer(f"Reklam {status_text}", show_alert=False)

    elif action == "edit_ad_text":
        admin_pending[callback.from_user.id] = "edit_ad_text"
        await callback.message.answer("✏️ Yeni reklam mətnini yaz (bütün istifadəçilərə bu mətn görünəcək):")
        await callback.answer()

    elif action == "edit_ad_link":
        admin_pending[callback.from_user.id] = "edit_ad_link"
        await callback.message.answer("🔗 Yeni reklam linkini yaz (məs. https://t.me/kanal):")
        await callback.answer()

    elif action == "close":
        admin_pending.pop(callback.from_user.id, None)
        await callback.message.edit_text(t("panel_closed", lang))
        await callback.answer()


@dp.message(Command("stats"))
async def handle_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        user = await get_or_create_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name,
            message.from_user.language_code,
        )
        await message.answer(t("admin_only", user["language"]))
        return
    stats = await get_stats()
    lines = "\n".join(f"  • {p}: {c}" for p, c in stats["by_platform"].items()) or "  —"
    text = (
        "📊 <b>LinkDrop İstatistikləri</b>\n\n"
        f"👥 Toplam istifadəçi: <b>{stats['total_users']}</b>\n"
        f"⬇️ Toplam endirmə: <b>{stats['total_downloads']}</b>\n"
        f"📅 Bugünkü: <b>{stats['today']}</b>\n"
        f"✅ Uğur nisbəti: <b>{stats['success_rate']}%</b>\n\n"
        f"📱 Platform üzrə:\n{lines}"
    )
    await message.answer(text)


@dp.message(Command("block"))
async def handle_block(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    if not is_admin(message.from_user.id):
        await message.answer(t("admin_only", user["language"]))
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("İstifadə: /block <user_id>")
        return
    target_id = int(parts[1])
    ok = await set_user_blocked(target_id, True)
    await message.answer(t("blocked_ok", user["language"], uid=target_id) if ok else t("user_not_found", user["language"]))


@dp.message(Command("unblock"))
async def handle_unblock(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    if not is_admin(message.from_user.id):
        await message.answer(t("admin_only", user["language"]))
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("İstifadə: /unblock <user_id>")
        return
    target_id = int(parts[1])
    ok = await set_user_blocked(target_id, False)
    await message.answer(t("unblocked_ok", user["language"], uid=target_id) if ok else t("user_not_found", user["language"]))


@dp.message(Command("broadcast"))
async def handle_broadcast(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    if not is_admin(message.from_user.id):
        await message.answer(t("admin_only", user["language"]))
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer(t("broadcast_usage", user["language"]))
        return
    user_ids = await get_all_active_user_ids()
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(t("broadcast_done", user["language"], sent=sent, failed=failed))


@dp.message(F.text)
async def handle_text_message(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        message.from_user.language_code,
    )
    lang = user["language"]

    # Admin panelindən gələn "növbəti mesajı gözlə" tələbi varsa, əvvəlcə onu emal et
    pending_action = admin_pending.get(message.from_user.id)
    if pending_action and is_admin(message.from_user.id):
        del admin_pending[message.from_user.id]
        text_value = (message.text or "").strip()

        if pending_action == "broadcast":
            user_ids = await get_all_active_user_ids()
            sent, failed = 0, 0
            for uid in user_ids:
                try:
                    await bot.send_message(chat_id=uid, text=text_value)
                    sent += 1
                except Exception:  # noqa: BLE001
                    failed += 1
                await asyncio.sleep(0.05)
            await message.answer(t("broadcast_done", lang, sent=sent, failed=failed))
            return

        if pending_action in ("block", "unblock"):
            if not text_value.lstrip("-").isdigit():
                await message.answer(t("panel_invalid_id", lang))
                return
            target_id = int(text_value)
            ok = await set_user_blocked(target_id, pending_action == "block")
            if ok:
                key = "blocked_ok" if pending_action == "block" else "unblocked_ok"
                await message.answer(t(key, lang, uid=target_id))
            else:
                await message.answer(t("user_not_found", lang))
            return

        if pending_action == "edit_ad_text":
            global ad_text_override
            ad_text_override = text_value
            await set_setting("ad_text", text_value)
            await message.answer(f"✅ Reklam mətni yeniləndi:\n\n{text_value}")
            return

        if pending_action == "edit_ad_link":
            if not (text_value.startswith("http://") or text_value.startswith("https://")):
                await message.answer("❌ Link http:// və ya https:// ilə başlamalıdır.")
                return
            global ad_link_override
            ad_link_override = text_value
            await set_setting("ad_link", text_value)
            await message.answer(f"✅ Reklam linki yeniləndi:\n{text_value}")
            return

    if user["is_blocked"]:
        await message.answer(t("blocked", lang))
        return

    if is_rate_limited(message.from_user.id):
        await message.answer(t("rate_limited", lang))
        return

    raw_url = extract_url(message.text or "")

    if raw_url is None:
        await message.answer(t("no_url_found", lang))
        return

    url = raw_url.strip().strip("<>\"'")

    if not is_supported_url(url):
        await message.answer(t("invalid_link", lang))
        return

    platform = detect_platform(url)
    status_message = await message.answer(t("preparing", lang))

    try:
        result = await download_video(url, platform)
    except FileTooLargeError:
        await status_message.edit_text(t("file_too_large", lang, limit=MAX_FILE_SIZE_MB))
        await log_download(message.from_user.id, platform, url, "rejected", "file_too_large")
        return
    except DownloadError as exc:
        await status_message.edit_text(t("download_failed", lang))
        await log_download(message.from_user.id, platform, url, "failed", str(exc)[:500])
        return

    audio_path: Path | None = None
    try:
        await message.answer_video(
            video=FSInputFile(result.file_path),
            caption=t("success_caption", lang, platform=platform),
        )
        await status_message.delete()
        await log_download(message.from_user.id, platform, url, "success")

        audio_path = await extract_audio(result.file_path)
        if audio_path:
            await message.answer_audio(
                audio=FSInputFile(audio_path),
                title=t("success_caption", lang, platform=platform),
            )

        if ads_enabled:
            await message.answer(ad_text_override or AD_TEXT.get(lang, AD_TEXT["az"]), reply_markup=ad_keyboard(lang))
    finally:
        cleanup_file(result.file_path)
        if audio_path:
            cleanup_file(audio_path)


# =========================================================================
# 10) HEALTH-CHECK SERVERİ (yalnız Render/Railway kimi platformalarda lazımdır)
# =========================================================================

class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"LinkDrop bot is running.")

    def do_HEAD(self) -> None:  # noqa: N802
        # UptimeRobot və digər monitorlar bəzən HEAD sorğusu göndərir.
        # Bunu tətbiq etməsək server 501 (Not Implemented) qaytarır və monitor
        # botu "down" sayır, halbuki server əslində işləkdir.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - susdur, botun loglarını qarışdırmasın
        pass


def _start_health_server_if_needed() -> None:
    """Render/Railway kimi platformalar PORT env dəyişəni verir və açıq port gözləyir.
    Lokal/Termux işlədərkən PORT təyin olunmadığı üçün bu server işə düşmür."""
    port = os.getenv("PORT")
    if not port:
        return

    def _serve() -> None:
        server = HTTPServer(("0.0.0.0", int(port)), _HealthCheckHandler)
        logger.info("Health-check serveri işə düşdü: 0.0.0.0:%s", port)
        server.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()


# =========================================================================
# 11) BAŞLADICI
# =========================================================================

async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN.startswith("your_"):
        raise SystemExit("BOT_TOKEN təyin olunmayıb. Skriptin başındakı BOT_TOKEN dəyişənini doldur.")

    _start_health_server_if_needed()
    await init_db()
    await load_admin_ids_from_db()

    global ad_text_override, ad_link_override
    saved_settings = await load_settings_from_db()
    ad_text_override = saved_settings.get("ad_text")
    ad_link_override = saved_settings.get("ad_link")

    if COOKIES_FILE.exists():
        logger.info("Cookie faylı tapıldı: %s (Instagram və s. üçün istifadə olunacaq)", COOKIES_FILE)
    else:
        logger.info("Cookie faylı tapılmadı — Instagram-da bəzi videolar rədd oluna bilər.")
    logger.info("LinkDrop botu başladılır... (admin sayı: %d)", len(admin_user_ids))
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("LinkDrop dayandırıldı.")

