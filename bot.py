import asyncio
import datetime
import html
import logging
import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

# Windows uchun terminal kodirovkasini to'g'irlash
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import aiosqlite
from aiohttp import web

async def start_web_healthcheck():
    port = int(os.environ.get("PORT", "10000"))
    app = web.Application()
    async def index(request):
        return web.Response(text="🚀 Downloatbot is live and running!")
    async def health(request):
        return web.Response(text="OK")
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Healthcheck web server successfully running on port {port}")

from dotenv import load_dotenv
import yt_dlp

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# =====================================================================
# SOZLAMALAR (CONFIG)
# =====================================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip() or "8545045154:AAEgxLX2Y8dl5ozfv8C1Q8I64zfoDxoGpl0"
ADMIN_ID = int(os.getenv("ADMIN_ID", "7394490565"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "Downloatbot")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

DB_PATH = str(BASE_DIR / "universal_bot.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

dp = Dispatcher(storage=MemoryStorage())

# Barcha video havolalari regexi
URL_REGEX = re.compile(
    r'(https?://(?:www\.)?(?:'
    r'youtube\.com|youtu\.be|'
    r'tiktok\.com|vt\.tiktok\.com|'
    r'instagram\.com|instagr\.am|'
    r'pinterest\.com|pin\.it|'
    r'twitter\.com|x\.com|'
    r'facebook\.com|fb\.watch'
    r')[^\s]+)'
)

media_cache: Dict[str, Dict[str, Any]] = {}


# =====================================================================
# FSM HOLATLARI
# =====================================================================
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_caption = State()
    waiting_for_channel = State()
    waiting_for_user_search = State()
    waiting_for_direct_points = State()
    waiting_for_direct_ban = State()


# =====================================================================
# MA'LUMOTLAR BAZASI (SQLITE)
# =====================================================================
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Foydalanuvchilar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                referred_by INTEGER,
                points INTEGER DEFAULT 0,
                downloads_count INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Agar is_banned ustuni bo'lmasa qo'shish
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
        except Exception:
            pass

        # Majburiy obuna kanallari jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE,
                channel_title TEXT,
                channel_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Sozlamalar jadvali (masalan video ostidagi reklama matni)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Yuklamalar tarixi
        await db.execute("""
            CREATE TABLE IF NOT EXISTS downloads_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                platform TEXT,
                url TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Tezkor kesh jadvali (0.01 soniyada video yuborish uchun)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS file_cache (
                url_hash TEXT PRIMARY KEY,
                file_id TEXT,
                media_type TEXT,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Standart video osti matni
        default_caption = "✨ @Downloatbot orqali eng yuqori sifatda yuklab olindi!\n🤖 Bizning bot: @Downloatbot"
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('caption_text', ?)",
            (default_caption,)
        )
        await db.commit()


# =====================================================================
# TEZKOR KESH FUNKSIYALARI (INSTANT DELIVERY)
# =====================================================================
import hashlib

def get_url_hash(url: str) -> str:
    clean = re.sub(r'[\?\&](utm_[^&]+|si=[^&]+|feature=[^&]+)', '', url).strip()
    return hashlib.md5(clean.encode()).hexdigest()


async def get_cached_file(url_hash: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM file_cache WHERE url_hash = ?", (url_hash,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def save_cached_file(url_hash: str, file_id: str, media_type: str, title: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO file_cache (url_hash, file_id, media_type, title) VALUES (?, ?, ?, ?)",
            (url_hash, file_id, media_type, title)
        )
        await db.commit()


# =====================================================================
# SOZLAMALAR VA BAZA FUNKSIYALARI
# =====================================================================
async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        await db.commit()


async def register_user(user_id: int, full_name: str, username: Optional[str], referred_by: Optional[int] = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cur:
            exists = await cur.fetchone()
        if exists:
            await db.execute("UPDATE users SET full_name = ?, username = ? WHERE user_id = ?", (full_name, username, user_id))
            await db.commit()
            return False

        valid_referrer = None
        if referred_by and referred_by != user_id:
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (referred_by,)) as cur:
                if await cur.fetchone():
                    valid_referrer = referred_by

        await db.execute(
            "INSERT INTO users (user_id, full_name, username, referred_by, points) VALUES (?, ?, ?, ?, ?)",
            (user_id, full_name, username, valid_referrer, 0)
        )
        if valid_referrer:
            await db.execute("UPDATE users SET points = points + 1 WHERE user_id = ?", (valid_referrer,))
        await db.commit()
        return True


async def is_user_banned(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return bool(row[0]) if row else False


async def toggle_ban_user(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            new_status = 0 if row[0] else 1
            await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (new_status, user_id))
            await db.commit()
            return bool(new_status)


async def increment_download(user_id: int, platform: str, url: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET downloads_count = downloads_count + 1 WHERE user_id = ?", (user_id,))
        await db.execute("INSERT INTO downloads_log (user_id, platform, url) VALUES (?, ?, ?)", (user_id, platform, url))
        await db.commit()


async def get_user_stats(user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            user_data = dict(row)

            async with db.execute("SELECT COUNT(*) FROM users WHERE points > ?", (user_data['points'],)) as rank_cur:
                rank_row = await rank_cur.fetchone()
                user_data['rank'] = (rank_row[0] + 1) if rank_row else 1

            async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as ref_cur:
                ref_count = await ref_cur.fetchone()
                user_data['referrals_count'] = ref_count[0] if ref_count else 0

            return user_data


async def get_top_users(limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT user_id, full_name, username, points, downloads_count
            FROM users
            ORDER BY points DESC, downloads_count DESC
            LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_user_ids() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


async def add_user_points(user_id: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("UPDATE users SET points = MAX(0, points + ?) WHERE user_id = ?", (amount, user_id))
        await db.commit()
        return res.rowcount > 0


# =====================================================================
# MAJBURIY OBUNA (CHANNELS) BAZASI
# =====================================================================
async def get_all_channels() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM channels ORDER BY id ASC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def add_channel(channel_id: str, title: str, url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO channels (channel_id, channel_title, channel_url) VALUES (?, ?, ?)",
                (channel_id, title, url)
            )
            await db.commit()
            return True
        except Exception:
            return False


async def remove_channel(channel_db_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("DELETE FROM channels WHERE id = ?", (channel_db_id,))
        await db.commit()
        return res.rowcount > 0


async def check_user_subscriptions(bot: Bot, user_id: int) -> Tuple[bool, List[Dict[str, Any]]]:
    if user_id == ADMIN_ID:
        return True, []

    channels = await get_all_channels()
    if not channels:
        return True, []

    unsubscribed: List[Dict[str, Any]] = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch['channel_id'], user_id=user_id)
            if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
                unsubscribed.append(ch)
        except Exception as e:
            logger.warning(f"Kanal obunasini tekshirib bo'lmadi ({ch['channel_id']}): {e}")
            # Agar bot kanal a'zolarini ko'ra olmasa ham havola tugmasi qoldiriladi
            unsubscribed.append(ch)

    return len(unsubscribed) == 0, unsubscribed


# =====================================================================
# STATISTIKA
# =====================================================================
async def get_detailed_stats() -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        # Jami userlar
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]

        # Bugun qo'shilganlar
        async with db.execute("SELECT COUNT(*) FROM users WHERE DATE(joined_at) = DATE('now')") as c:
            today_users = (await c.fetchone())[0]

        # Oxirgi 7 kun
        async with db.execute("SELECT COUNT(*) FROM users WHERE joined_at >= DATE('now', '-7 days')") as c:
            week_users = (await c.fetchone())[0]

        # Jami yuklamalar
        async with db.execute("SELECT COUNT(*) FROM downloads_log") as c:
            total_downloads = (await c.fetchone())[0]

        # Bugungi yuklamalar
        async with db.execute("SELECT COUNT(*) FROM downloads_log WHERE DATE(downloaded_at) = DATE('now')") as c:
            today_downloads = (await c.fetchone())[0]

        # Bloklanganlar
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1") as c:
            banned_users = (await c.fetchone())[0]

        # Kanallar soni
        async with db.execute("SELECT COUNT(*) FROM channels") as c:
            channels_count = (await c.fetchone())[0]

        return {
            "total_users": total_users,
            "today_users": today_users,
            "week_users": week_users,
            "total_downloads": total_downloads,
            "today_downloads": today_downloads,
            "banned_users": banned_users,
            "channels_count": channels_count,
        }


# =====================================================================
# YUKLAB OLISH XIZMATI (DOWNLOADER)
# =====================================================================
class FileSizeLimitError(Exception):
    pass


def get_ffmpeg_path() -> Optional[str]:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return None


def get_js_runtime() -> Optional[Dict[str, Any]]:
    node_path = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if os.path.exists(node_path):
        return {'node': {'path': node_path}}
    return None


def extract_direct_stream_url(info: Dict[str, Any]) -> Optional[str]:
    if info.get('url') and str(info['url']).startswith('http'):
        return info['url']
    formats = info.get('formats', [])
    combos = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url')]
    if combos:
        combos.sort(key=lambda x: x.get('height') or 0)
        return combos[-1]['url']
    vids = [f for f in formats if f.get('vcodec') != 'none' and f.get('url')]
    if vids:
        vids.sort(key=lambda x: x.get('height') or 0)
        return vids[-1]['url']
    return None


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "Noma'lum"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours > 0 else f"{minutes:02d}:{secs:02d}"


def format_bytes(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def detect_platform(url: str) -> str:
    lower = url.lower()
    if 'tiktok.com' in lower:
        return 'tiktok'
    elif 'youtube.com' in lower or 'youtu.be' in lower:
        return 'youtube'
    elif 'instagram.com' in lower:
        return 'instagram'
    elif 'pinterest.com' in lower or 'pin.it' in lower:
        return 'pinterest'
    elif 'twitter.com' in lower or 'x.com' in lower:
        return 'twitter'
    elif 'facebook.com' in lower or 'fb.watch' in lower:
        return 'facebook'
    return 'universal'


def get_platform_display(platform: str) -> str:
    icons = {
        'youtube': '🔴 <b>YouTube</b>',
        'tiktok': '⬛ <b>TikTok (Suv belgisisiz)</b>',
        'instagram': '📸 <b>Instagram</b>',
        'pinterest': '📌 <b>Pinterest</b>',
        'twitter': '🐦 <b>Twitter (X)</b>',
        'facebook': '🔵 <b>Facebook</b>',
        'universal': '🌐 <b>Internet</b>'
    }
    return icons.get(platform, '🌐 <b>Media</b>')


async def extract_media_info(url: str) -> Dict[str, Any]:
    platform = detect_platform(url)
    ydl_opts: Dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
    }
    ffmpeg_path = get_ffmpeg_path()
    if ffmpeg_path:
        ydl_opts['ffmpeg_location'] = ffmpeg_path
    js_runtime = get_js_runtime()
    if js_runtime:
        ydl_opts['js_runtimes'] = js_runtime

    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await loop.run_in_executor(None, _extract)
    if not info:
        raise ValueError("Video ma'lumotlarini olib bo'lmadi.")

    title = info.get('title') or 'Video'
    duration_sec = info.get('duration')
    duration_str = format_duration(duration_sec)
    thumbnail = info.get('thumbnail')
    uploader = info.get('uploader') or info.get('channel') or "Noma'lum"

    formats = info.get('formats', [])
    estimated_size = None
    for f in reversed(formats):
        if f.get('filesize'):
            estimated_size = f['filesize']
            break
        elif f.get('filesize_approx'):
            estimated_size = f['filesize_approx']
            break

    direct_stream_url = extract_direct_stream_url(info)
    return {
        'id': str(info.get('id', hash(url))),
        'title': title,
        'url': direct_stream_url,
        'platform': platform,
        'platform_display': get_platform_display(platform),
        'duration_sec': duration_sec,
        'duration_str': duration_str,
        'thumbnail': thumbnail,
        'uploader': uploader,
        'estimated_size': estimated_size,
    }


async def download_media(url: str, media_type: str = "video") -> Tuple[Path, Dict[str, Any]]:
    platform = detect_platform(url)
    ffmpeg_path = get_ffmpeg_path()
    out_template = str(DOWNLOAD_DIR / f"{platform}_%(id)s_{media_type}.%(ext)s")

    ydl_opts: Dict[str, Any] = {
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'max_filesize': MAX_FILE_SIZE_BYTES,
        'buffersize': 1024 * 1024,
        'http_chunk_size': 10485760,
        'concurrent_fragment_downloads': 8,
        'nocheckcertificate': True,
    }

    if ffmpeg_path:
        ydl_opts['ffmpeg_location'] = ffmpeg_path
    js_runtime = get_js_runtime()
    if js_runtime:
        ydl_opts['js_runtimes'] = js_runtime

    if media_type == "audio":
        ydl_opts.update({
            'format': 'ba[ext=m4a]/ba/b',
        })
    else:
        if platform == "youtube":
            ydl_opts['format'] = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best'
        elif platform == "tiktok":
            ydl_opts['format'] = 'best[ext=mp4]/best'
        elif platform == "instagram":
            ydl_opts['format'] = 'best[ext=mp4]/best'
        else:
            ydl_opts['format'] = 'best[ext=mp4]/best'

    loop = asyncio.get_running_loop()

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename, info

    try:
        filename, info = await loop.run_in_executor(None, _download)
    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)
        if "File is larger than max-filesize" in err_msg or "larger than maximum" in err_msg:
            raise FileSizeLimitError(f"Video hajmi {MAX_FILE_SIZE_MB}MB dan katta! Telegram cheklovi tufayli yuklab bo'lmaydi.")
        raise

    file_path = Path(filename)
    if not file_path.exists():
        base_no_ext = os.path.splitext(filename)[0]
        for candidate in DOWNLOAD_DIR.glob(f"{Path(base_no_ext).name}.*"):
            if candidate.is_file():
                file_path = candidate
                break

    if not file_path.exists():
        raise FileNotFoundError("Yuklangan fayl saqlanmadi yoki topilmadi.")

    file_size = file_path.stat().st_size
    if file_size > MAX_FILE_SIZE_BYTES:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise FileSizeLimitError(f"Fayl hajmi ({format_bytes(file_size)}) Telegramning {MAX_FILE_SIZE_MB}MB limitidan oshib ketdi!")

    return file_path, info


def safe_delete(path: Path) -> None:
    try:
        if path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


# =====================================================================
# TUGMALAR (KEYBOARDS)
# =====================================================================
def get_main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🎵 Musiqa qidirish"), KeyboardButton(text="📥 Qanday yuklanadi?")],
        [KeyboardButton(text="👤 Profilim"), KeyboardButton(text="🏆 Peshqadamlar (Top-10)")],
        [KeyboardButton(text="🎁 Do'stlarni taklif qilish")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="👑 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_download_keyboard(media_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Video (HD)", callback_data=f"dl:video:{media_id}"),
            InlineKeyboardButton(text="🎵 Musiqa (MP3)", callback_data=f"dl:audio:{media_id}"),
        ],
        [
            InlineKeyboardButton(text="🚀 Do'stlarga ulashish", switch_inline_query=f"https://t.me/{BOT_USERNAME}")
        ]
    ])


def get_referral_keyboard(user_id: int) -> InlineKeyboardMarkup:
    share_text = f"🔥 Barcha ijtimoiy tarmoqlardan (Instagram, TikTok, YouTube, Pinterest) sifatli video va musiqalarni bir zumda yuklab beruvchi ajoyib bot! 👇\nhttps://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    encoded = urllib.parse.quote(share_text)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Do'stlarga ulashish", url=f"https://t.me/share/url?url={encoded}")],
        [InlineKeyboardButton(text="🏆 Peshqadamlar reytingi", callback_data="view_top")],
    ])


def get_subscription_keyboard(unsubscribed_channels: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons = []
    for idx, ch in enumerate(unsubscribed_channels, start=1):
        title = ch.get('channel_title') or f"{idx}-Kanal"
        url = ch.get('channel_url') or f"https://t.me/{ch.get('channel_id', '').replace('@', '')}"
        buttons.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])

    buttons.append([InlineKeyboardButton(text="✅ A'zo bo'ldim (Tekshirish)", callback_data="check_subscription")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="admin:stats"),
            InlineKeyboardButton(text="📢 Majburiy obuna", callback_data="admin:channels"),
        ],
        [
            InlineKeyboardButton(text="💰 Ball berish", callback_data="admin:give_points"),
            InlineKeyboardButton(text="✉️ Xabar tarqatish", callback_data="admin:broadcast"),
        ],
        [
            InlineKeyboardButton(text="👤 User qidirish", callback_data="admin:search_user"),
            InlineKeyboardButton(text="🚫 Bloklash/Ochish", callback_data="admin:ban_user"),
        ],
        [
            InlineKeyboardButton(text="✏️ Video tagidagi yozuv", callback_data="admin:edit_caption"),
            InlineKeyboardButton(text="🏆 Top foydalanuvchilar", callback_data="admin:top_users"),
        ],
        [
            InlineKeyboardButton(text="💾 Bazani yuklab olish (Backup)", callback_data="admin:backup"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Panelni yopish", callback_data="admin:close"),
        ]
    ])


# =====================================================================
# HANDLERLAR: START, PROFIL, REYTÍNG
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot):
    user = message.from_user
    if not user:
        return

    # Bloklanganlikni tekshirish
    if await is_user_banned(user.id):
        await message.answer("🚫 Siz botdan foydalanishdan bloklangansiz.")
        return

    referred_by = None
    args = command.args
    if args and args.startswith("ref_"):
        try:
            referred_by = int(args.replace("ref_", ""))
        except ValueError:
            pass

    is_new = await register_user(user.id, user.full_name, user.username, referred_by)
    if is_new and referred_by and referred_by != user.id:
        try:
            await bot.send_message(
                chat_id=referred_by,
                text=f"🎉 <b>Yangi taklif!</b> Sizning havolangiz orqali <b>{html.escape(user.full_name)}</b> botga qo'shildi va sizga <b>+1 ball</b> berildi! 🚀",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    # Majburiy obunani tekshirish
    is_sub, unsubs = await check_user_subscriptions(bot, user.id)
    if not is_sub:
        await message.answer(
            "⚠️ <b>Botdan to'liq foydalanish uchun quyidagi homiy kanallarga a'zo bo'ling!</b>\n\n"
            "Barcha kanallarga a'zo bo'lgach, pastdagi <b>«✅ A'zo bo'ldim»</b> tugmasini bosing:",
            reply_markup=get_subscription_keyboard(unsubs),
            parse_mode=ParseMode.HTML
        )
        return

    is_admin = (user.id == ADMIN_ID)
    welcome_text = (
        f"Assalomu alaykum, <b>{html.escape(user.full_name)}</b>! 👋\n\n"
        "Men barcha ijtimoiy tarmoqlardan video va audio yuklab beruvchi universal botman! 🚀\n\n"
        "📥 <b>Qo'llab-quvvatlanadigan platformalar:</b>\n"
        "• 🔴 <b>YouTube</b> & Shorts\n"
        "• ⬛ <b>TikTok</b> (Suv belgisisiz toza HD)\n"
        "• 📸 <b>Instagram</b> (Reels, Post, Stories)\n"
        "• 📌 <b>Pinterest</b> (Video va rasm)\n"
        "• 🐦 <b>Twitter / X</b> & 🔵 <b>Facebook</b>\n\n"
        "Menga shunchaki video havolasini yuboring! 👇"
    )

    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard(is_admin=is_admin),
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data == "check_subscription")
async def callback_check_sub(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    is_sub, unsubs = await check_user_subscriptions(bot, user_id)
    if not is_sub:
        await callback.answer("❌ Hali hamma kanallarga a'zo bo'lmadingiz! Iltimos, barcha kanallarga obuna bo'ling.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=get_subscription_keyboard(unsubs))
        except Exception:
            pass
        return

    await callback.answer("✅ Obuna muvaffaqiyatli tasdiqlandi!", show_alert=False)
    try:
        await callback.message.delete()
    except Exception:
        pass

    is_admin = (user_id == ADMIN_ID)
    await callback.message.answer(
        "🎉 <b>Ajoyib! Barcha kanallarga a'zo bo'ldingiz.</b>\n\n"
        "Endi menga istalgan video havolasini yuboring va yuklab oling! 👇",
        reply_markup=get_main_keyboard(is_admin=is_admin),
        parse_mode=ParseMode.HTML
    )


@dp.message(F.text == "📥 Qanday yuklanadi?")
@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "💡 <b>Videolarni qanday yuklab olish mumkin?</b>\n\n"
        "1️⃣ <b>Instagram, TikTok, YouTube yoki Pinterest</b> ilovasida yoqqan videoni oching.\n"
        "2️⃣ <b>«Ulashish» (Поделиться / Share)</b> tugmasini bosib, havolani nusxalang.\n"
        "3️⃣ Havolani ushbu bot chatiga yuboring.\n"
        "4️⃣ Kerakli formatni tanlang:\n"
        "   • 🎬 <b>Video (HD)</b> — Suv belgisisiz to'liq video\n"
        "   • 🎵 <b>Musiqa (MP3)</b> — Videodagi musiqani alohida olish\n"
        "5️⃣ Bot faylni bir necha soniyada sizga yetkazib beradi! ⚡"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(F.text == "🎁 Do'stlarni taklif qilish")
@dp.message(Command("referral"))
async def cmd_referral(message: Message):
    user = message.from_user
    stats = await get_user_stats(user.id)
    points = stats['points'] if stats else 0
    ref_count = stats['referrals_count'] if stats else 0
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user.id}"

    text = (
        "🎁 <b>Do'stlarni taklif qiling va ballar to'plang!</b>\n\n"
        "Har bir taklif qilgan do'stingiz uchun sizga <b>+1 ball</b> beriladi. "
        "Eng ko'p ball to'plagan foydalanuvchilar <b>Peshqadamlar (Top-10)</b> reytingidan joy oladi!\n\n"
        f"📊 <b>Sizning ko'rsatkichlaringiz:</b>\n"
        f"• To'plangan ballaringiz: <b>{points} ball</b>\n"
        f"• Chaqirgan do'stlaringiz: <b>{ref_count} ta</b>\n\n"
        f"🔗 <b>Sizning shaxsiy referal havolangiz:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        "Pastdagi <b>«🚀 Do'stlarga ulashish»</b> tugmasini bosing:"
    )
    await message.answer(text, reply_markup=get_referral_keyboard(user.id), parse_mode=ParseMode.HTML)


@dp.message(F.text == "🏆 Peshqadamlar (Top-10)")
@dp.message(Command("top"))
async def cmd_leaderboard(message: Message):
    top_users = await get_top_users(limit=10)
    if not top_users:
        await message.answer("Hozircha reytingda hech kim yo'q.")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = ["🏆 <b>Eng ko'p do'st taklif qilganlar (Top-10):</b>\n"]

    for i, u in enumerate(top_users):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        name = html.escape(u['full_name'] or 'Foydalanuvchi')
        points = u['points']
        lines.append(f"{medal} <b>{name}</b> — <b>{points}</b> ball")

    lines.append("\n💡 <i>Siz ham do'stlaringizni taklif qiling va yuqori o'rinni egallang!</i>")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "view_top")
async def callback_view_top(callback: CallbackQuery):
    top_users = await get_top_users(limit=10)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = ["🏆 <b>Eng ko'p do'st taklif qilganlar (Top-10):</b>\n"]
    for i, u in enumerate(top_users):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        name = html.escape(u['full_name'] or 'Foydalanuvchi')
        points = u['points']
        lines.append(f"{medal} <b>{name}</b> — <b>{points}</b> ball")
    await callback.message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(F.text == "👤 Profilim")
@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user = message.from_user
    stats = await get_user_stats(user.id)
    if not stats:
        await message.answer("Profil ma'lumotlari topilmadi, /start bosing.")
        return

    text = (
        "👤 <b>Sizning profilingiz:</b>\n\n"
        f"• Ism: <b>{html.escape(user.full_name)}</b>\n"
        f"• Telegram ID: <code>{user.id}</code>\n"
        f"• To'plangan ballar: <b>{stats['points']} ball</b>\n"
        f"• Taklif qilingan do'stlar: <b>{stats['referrals_count']} ta</b>\n"
        f"• Jami yuklab olingan videolar: <b>{stats['downloads_count']} ta</b>\n"
        f"• Umumiy reytingdagi o'rningiz: <b>#{stats['rank']}</b>\n\n"
        f"🔗 <b>Taklif havolangiz:</b>\n"
        f"<code>https://t.me/{BOT_USERNAME}?start=ref_{user.id}</code>"
    )
    await message.answer(text, reply_markup=get_referral_keyboard(user.id), parse_mode=ParseMode.HTML)


# =====================================================================
# HANDLERLAR: YUKLAB OLISH (DOWNLOAD)
# =====================================================================
@dp.message(F.text.regexp(URL_REGEX))
async def handle_url(message: Message, bot: Bot):
    user_id = message.from_user.id
    if await is_user_banned(user_id):
        await message.answer("🚫 Siz botdan foydalanishdan bloklangansiz.")
        return

    # Majburiy obunani tekshirish
    is_sub, unsubs = await check_user_subscriptions(bot, user_id)
    if not is_sub:
        await message.answer(
            "⚠️ <b>Video yuklash uchun avval homiy kanallarga a'zo bo'ling!</b>\n\n"
            "A'zo bo'lgach, «✅ A'zo bo'ldim» tugmasini bosing:",
            reply_markup=get_subscription_keyboard(unsubs),
            parse_mode=ParseMode.HTML
        )
        return

    match = URL_REGEX.search(message.text)
    if not match:
        return
    url = match.group(0)
    url_hash = get_url_hash(url)

    # 1. TEZKOR KESH TEKSHIRUVI (0.01 soniyada yuborish!)
    cached = await get_cached_file(url_hash)
    if cached and cached.get('file_id'):
        custom_caption = await get_setting(
            "caption_text",
            "✨ @Downloatbot orqali eng yuqori sifatda yuklab olindi!\n🤖 Bizning bot: @Downloatbot"
        )
        title = html.escape(cached.get('title') or 'Video')
        caption_full = f"🎬 <b>{title}</b>\n\n{custom_caption}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Do'stlarga ulashish", switch_inline_query=f"https://t.me/{BOT_USERNAME}")]
        ])
        await bot.send_video(
            chat_id=message.chat.id,
            video=cached['file_id'],
            caption=caption_full,
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
            reply_markup=kb
        )
        await increment_download(user_id, "cache", url)
        return

    status_msg = await message.reply("⚡ <b>Video yuklanmoqda...</b>", parse_mode=ParseMode.HTML)
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)

    # 2. TO'G'RIDAN-TO'G'RI CDN URL ORQALI YUBORISH (UY INTERNETIDA KUTISHNI YO'QOTISH!)
    try:
        info = await extract_media_info(url)
        direct_url = info.get('url')
        title = html.escape(info.get('title') or 'Video')
        custom_caption = await get_setting(
            "caption_text",
            "✨ @Downloatbot orqali eng yuqori sifatda yuklab olindi!\n🤖 Bizning bot: @Downloatbot"
        )
        caption_full = f"🎬 <b>{title}</b>\n\n{custom_caption}"

        media_id = str(info.get('id', hash(url)))
        media_cache[media_id] = {'url': url, 'info': info}

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎵 Musiqasini olish (MP3)", callback_data=f"dl:audio:{media_id}")],
            [InlineKeyboardButton(text="🚀 Do'stlarga ulashish", switch_inline_query=f"https://t.me/{BOT_USERNAME}")]
        ])

        if direct_url and direct_url.startswith("http"):
            try:
                sent_msg = await bot.send_video(
                    chat_id=message.chat.id,
                    video=direct_url,
                    caption=caption_full,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    reply_markup=kb
                )
                await status_msg.delete()
                await increment_download(user_id, info.get('platform', 'universal'), url)
                if sent_msg.video and sent_msg.video.file_id:
                    await save_cached_file(url_hash, sent_msg.video.file_id, "video", title)
                return
            except Exception as e:
                logger.info(f"Direct CDN stream failed ({e}), using fast local download fallback...")
    except Exception as e:
        err_str = str(e)
        logger.warning(f"Direct extract failed: {err_str}")
        if "there is no video in this post" in err_str.lower() or "no video in this post" in err_str.lower():
            await status_msg.edit_text(
                "⚠️ <b>Ushbu postda video mavjud emas (faqat rasm)!</b>\n\n"
                "Iltimos, video yoki Reels havolasini yuboring. 🎬",
                parse_mode=ParseMode.HTML
            )
            return

    # 3. ZAXIRA (FALLBACK): TEZKOR STREAM YUKLASH
    file_path: Optional[Path] = None
    try:
        file_path, info = await download_media(url, media_type="video")

        title = html.escape(info.get('title') or 'Video')
        video_file = FSInputFile(str(file_path))

        custom_caption = await get_setting(
            "caption_text",
            "✨ @Downloatbot orqali eng yuqori sifatda yuklab olindi!\n🤖 Bizning bot: @Downloatbot"
        )
        caption_full = f"🎬 <b>{title}</b>\n\n{custom_caption}"

        media_id = str(info.get('id', hash(url)))
        media_cache[media_id] = {'url': url, 'info': info}

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎵 Musiqasini olish (MP3)", callback_data=f"dl:audio:{media_id}")],
            [InlineKeyboardButton(text="🚀 Do'stlarga ulashish", switch_inline_query=f"https://t.me/{BOT_USERNAME}")]
        ])

        sent_msg = await bot.send_video(
            chat_id=message.chat.id,
            video=video_file,
            caption=caption_full,
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
            reply_markup=kb
        )
        await status_msg.delete()
        await increment_download(user_id, info.get('platform', 'universal'), url)

        # Kelgusi so'rovlar uchun Telegram keshiga saqlash (0.01 soniyada yuborilishi uchun)
        if sent_msg.video and sent_msg.video.file_id:
            await save_cached_file(url_hash, sent_msg.video.file_id, "video", title)

    except FileSizeLimitError as e:
        await status_msg.edit_text(f"⚠️ <b>Hajm limiti oshdi:</b>\n{str(e)}", parse_mode=ParseMode.HTML)
    except Exception as e:
        err_str = str(e)
        if "there is no video in this post" in err_str.lower() or "no video in this post" in err_str.lower():
            await status_msg.edit_text(
                "⚠️ <b>Ushbu postda video mavjud emas (faqat rasm)!</b>\n\n"
                "Iltimos, video yoki Reels havolasini yuboring. 🎬",
                parse_mode=ParseMode.HTML
            )
            return
        await status_msg.edit_text(
            f"❌ Videoni yuklab bo'lmadi.\n"
            f"<i>Havola noto'g'ri, video maxfiy yoki server cheklovi mavjud bo'lishi mumkin.</i>\n\n"
            f"Xatolik: <code>{html.escape(str(e)[:150])}</code>",
            parse_mode=ParseMode.HTML
        )
    finally:
        if file_path:
            safe_delete(file_path)


@dp.callback_query(F.data.startswith("dl:video:"))
async def callback_dl_video(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    if await is_user_banned(user_id):
        await callback.answer("🚫 Siz bloklangansiz.", show_alert=True)
        return

    # Majburiy obunani tekshirish
    is_sub, unsubs = await check_user_subscriptions(bot, user_id)
    if not is_sub:
        await callback.answer("⚠️ Avval homiy kanallarga a'zo bo'ling!", show_alert=True)
        await callback.message.answer(
            "⚠️ <b>Video yuklash uchun quyidagi homiy kanallarga a'zo bo'ling:</b>",
            reply_markup=get_subscription_keyboard(unsubs),
            parse_mode=ParseMode.HTML
        )
        return

    media_id = callback.data.split(":")[2]
    item = media_cache.get(media_id)
    if not item:
        await callback.answer("Fayl ma'lumotlari eskirgan. Havolani qayta yuboring.", show_alert=True)
        return

    url = item['url']
    info = item['info']
    platform = info.get('platform', 'universal')

    await callback.answer("🎬 Video yuklab olish boshlandi...")
    status_msg = await callback.message.reply("⏳ Video serverga yuklanmoqda... Iltimos kuting.")
    await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_VIDEO)

    file_path: Optional[Path] = None
    try:
        file_path, _ = await download_media(url, media_type="video")
        await status_msg.edit_text("📤 Video Telegramga yuborilmoqda...")
        await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_VIDEO)

        title = html.escape(info.get('title', 'Video'))
        video_file = FSInputFile(str(file_path))

        # Dinamik caption (video tagidagi reklama va bot havolasi)
        custom_caption = await get_setting(
            "caption_text",
            "✨ @Downloatbot orqali eng yuqori sifatda yuklab olindi!\n🤖 Bizning bot: @Downloatbot"
        )
        caption_full = f"🎬 <b>{title}</b>\n\n{custom_caption}"

        await bot.send_video(
            chat_id=callback.message.chat.id,
            video=video_file,
            caption=caption_full,
            parse_mode=ParseMode.HTML,
            supports_streaming=True
        )
        await status_msg.delete()
        await increment_download(user_id, platform, url)

    except FileSizeLimitError as e:
        await status_msg.edit_text(f"⚠️ <b>Hajm limiti oshdi:</b>\n{str(e)}", parse_mode=ParseMode.HTML)
    except Exception as e:
        await status_msg.edit_text(f"❌ Xatolik yuz berdi: <code>{html.escape(str(e)[:150])}</code>", parse_mode=ParseMode.HTML)
    finally:
        if file_path:
            safe_delete(file_path)


@dp.callback_query(F.data.startswith("dl:audio:"))
async def callback_dl_audio(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    if await is_user_banned(user_id):
        await callback.answer("🚫 Siz bloklangansiz.", show_alert=True)
        return

    # Majburiy obunani tekshirish
    is_sub, unsubs = await check_user_subscriptions(bot, user_id)
    if not is_sub:
        await callback.answer("⚠️ Avval homiy kanallarga a'zo bo'ling!", show_alert=True)
        await callback.message.answer(
            "⚠️ <b>Musiqa yuklash uchun quyidagi homiy kanallarga a'zo bo'ling:</b>",
            reply_markup=get_subscription_keyboard(unsubs),
            parse_mode=ParseMode.HTML
        )
        return

    media_id = callback.data.split(":")[2]
    item = media_cache.get(media_id)
    if not item:
        await callback.answer("Fayl ma'lumotlari eskirgan. Havolani qayta yuboring.", show_alert=True)
        return

    url = item['url']
    info = item['info']
    platform = info.get('platform', 'universal')
    audio_hash = f"audio_{get_url_hash(url)}"

    # 1. Tezkor audio keshni tekshirish (0.01 soniya)
    cached_audio = await get_cached_file(audio_hash)
    if cached_audio and cached_audio.get('file_id'):
        title = cached_audio.get('title') or 'Audio'
        custom_caption = await get_setting(
            "caption_text",
            "✨ @Downloatbot orqali yuklab olindi!\n🤖 Bizning bot: @Downloatbot"
        )
        caption_full = f"🎵 <b>{html.escape(title)}</b>\n\n{custom_caption}"
        await bot.send_audio(
            chat_id=callback.message.chat.id,
            audio=cached_audio['file_id'],
            caption=caption_full,
            parse_mode=ParseMode.HTML
        )
        await callback.answer("✅ Musiqa yuborildi!")
        await increment_download(user_id, "cache", url)
        return

    await callback.answer("🎵 Musiqa tayyorlanmoqda...")
    status_msg = await callback.message.reply("⏳ Musiqa (MP3) ajratib olinmoqda...")
    await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.RECORD_VOICE)

    file_path: Optional[Path] = None
    try:
        file_path, _ = await download_media(url, media_type="audio")
        await status_msg.edit_text("📤 Audio Telegramga jo'natilmoqda...")
        await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.RECORD_VOICE)

        title = info.get('title', 'Audio')
        uploader = info.get('uploader', 'Artist')
        duration = info.get('duration_sec')

        audio_file = FSInputFile(str(file_path))
        custom_caption = await get_setting(
            "caption_text",
            "✨ @Downloatbot orqali yuklab olindi!\n🤖 Bizning bot: @Downloatbot"
        )
        caption_full = f"🎵 <b>{html.escape(title)}</b>\n👤 <b>Ijrochi:</b> {html.escape(str(uploader))}\n\n{custom_caption}"

        sent_audio = await bot.send_audio(
            chat_id=callback.message.chat.id,
            audio=audio_file,
            title=title[:60],
            performer=uploader[:60],
            duration=duration,
            caption=caption_full,
            parse_mode=ParseMode.HTML
        )
        await status_msg.delete()
        await increment_download(user_id, platform, url)

        # Kelgusi so'rovlar uchun Telegram keshiga saqlash
        if sent_audio.audio and sent_audio.audio.file_id:
            await save_cached_file(audio_hash, sent_audio.audio.file_id, "audio", title)

    except FileSizeLimitError as e:
        await status_msg.edit_text(f"⚠️ <b>Hajm limiti oshdi:</b>\n{str(e)}", parse_mode=ParseMode.HTML)
    except Exception as e:
        await status_msg.edit_text(f"❌ Xatolik yuz berdi: <code>{html.escape(str(e)[:150])}</code>", parse_mode=ParseMode.HTML)
    finally:
        if file_path:
            safe_delete(file_path)


# =====================================================================
# HANDLERLAR: MUSIQA QIDIRISH (+MUSIQA BOT)
# =====================================================================
async def search_music(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
    }
    js_runtime = get_js_runtime()
    if js_runtime:
        ydl_opts['js_runtimes'] = js_runtime
    loop = asyncio.get_running_loop()

    def _search():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            return res.get('entries', []) if res else []

    entries = await loop.run_in_executor(None, _search)
    results = []
    for e in entries:
        if not e:
            continue
        v_id = e.get('id') or e.get('url')
        if not v_id:
            continue
        results.append({
            'id': str(v_id),
            'title': e.get('title') or 'Musiqa',
            'duration': format_duration(e.get('duration')),
            'uploader': e.get('uploader') or e.get('channel') or "Noma'lum",
        })
    return results


@dp.message(F.text == "🎵 Musiqa qidirish")
async def cmd_music_search_prompt(message: Message):
    await message.answer(
        "🎵 <b>Musiqa qidirish xizmati</b>\n\n"
        "Istalgan qo'shiq nomi, ijrochi yoki qo'shiq so'zlarini yozib yuboring (masalan: <code>Konsta Insonlar</code> yoki <code>Eminem</code>).\n\n"
        "Bot darhol topib beradi! 🎧",
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data == "delete_msg")
async def callback_delete_msg(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()




# =====================================================================
# ADMIN PANEL: ASOSIY VA BO'LIMLAR
# =====================================================================
@dp.message(F.text == "👑 Admin Panel")
@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    await state.clear()
    text = (
        "👑 <b>Boshqaruv paneli (Admin Panel)</b>\n\n"
        "Kerakli bo'limni tanlang:"
    )
    await message.answer(text, reply_markup=get_admin_keyboard(), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "admin:close")
async def callback_admin_close(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await callback.message.delete()
    await callback.answer("Panel yopildi.")


# ---------------------------------------------------------------------
# ADMIN: TO'LIQ STATISTIKA
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:stats")
async def callback_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    s = await get_detailed_stats()
    text = (
        "📊 <b>Botning to'liq statistikasi:</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{s['total_users']} ta</b>\n"
        f"🆕 Bugun qo'shilganlar: <b>{s['today_users']} ta</b>\n"
        f"📅 Oxirgi 7 kunda qo'shilganlar: <b>{s['week_users']} ta</b>\n\n"
        f"📥 Jami yuklangan videolar: <b>{s['total_downloads']} ta</b>\n"
        f"📊 Bugun yuklangan videolar: <b>{s['today_downloads']} ta</b>\n\n"
        f"📢 Faol homiy kanallar: <b>{s['channels_count']} ta</b>\n"
        f"🚫 Bloklangan foydalanuvchilar: <b>{s['banned_users']} ta</b>\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Yangilash", callback_data="admin:stats")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin:menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data == "admin:menu")
async def callback_admin_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text = (
        "👑 <b>Boshqaruv paneli (Admin Panel)</b>\n\n"
        "Kerakli bo'limni tanlang:"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()


# ---------------------------------------------------------------------
# ADMIN: MAJBURIY OBUNA KANALLARI (KANAL QO'SHISH / O'CHIRISH)
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:channels")
async def callback_admin_channels(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    await state.clear()
    channels = await get_all_channels()
    text = "📢 <b>Majburiy obuna kanallari:</b>\n\n"

    if not channels:
        text += "<i>Hozircha hech qanday homiy kanal ulanmagan. Foydalanuvchilar to'g'ridan-to'g'ri botdan foydalanishmoqda.</i>\n"
    else:
        for idx, ch in enumerate(channels, start=1):
            text += f"{idx}. <b>{html.escape(ch['channel_title'])}</b>\n   ID: <code>{ch['channel_id']}</code>\n   Havola: {ch['channel_url']}\n\n"

    buttons = [
        [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="admin:add_channel")],
    ]
    if channels:
        buttons.append([InlineKeyboardButton(text="🗑 Kanalni o'chirish", callback_data="admin:del_channel_menu")])
    buttons.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin:menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data == "admin:add_channel")
async def callback_admin_add_channel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    await state.set_state(AdminStates.waiting_for_channel)
    text = (
        "➕ <b>Yangi majburiy kanal qo'shish</b>\n\n"
        "Kanal username (@kanalim), ID (-100...) yoki havolasini yuboring.\n\n"
        "⚠️ <b>MUHIM:</b> Bot a'zolikni tekshira olishi uchun ushbu kanalga <b>ADMINISTRATOR</b> qilib tayinlangan bo'lishi shart!\n\n"
        "Bekor qilish uchun /cancel deb yozing."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin:channels")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_for_channel)
async def process_add_channel(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text in ["/cancel", "❌ Bekor qilish"]:
        await state.clear()
        await message.answer("Amal bekor qilindi.", reply_markup=get_main_keyboard(is_admin=True))
        return

    raw = message.text.strip()
    # Formatlash
    channel_id = raw
    if "t.me/" in raw:
        part = raw.split("t.me/")[1].split("/")[0].split("?")[0]
        channel_id = f"@{part}" if not part.startswith("+") else part

    status_msg = await message.answer("⏳ Kanal tekshirilmoqda...")

    try:
        chat = await bot.get_chat(channel_id)
        chat_id_str = str(chat.id)
        title = chat.title or "Kanal"
        url = f"https://t.me/{chat.username}" if chat.username else (chat.invite_link or raw)

        # Bot adminligini tekshirish
        try:
            bot_member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
            if bot_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await status_msg.edit_text(
                    f"⚠️ <b>Diqqat!</b> Bot <b>{html.escape(title)}</b> kanalida admin emas!\n"
                    "Iltimos, botni kanalga admin qilib qo'shing va qayta urinib ko'ring.",
                    parse_mode=ParseMode.HTML
                )
                return
        except Exception as e:
            logger.warning(f"Bot admin tekshiruvida xatolik: {e}")

        success = await add_channel(channel_id=chat_id_str, title=title, url=url)
        if success:
            await status_msg.edit_text(
                f"✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>\n\n"
                f"📢 <b>Nomi:</b> {html.escape(title)}\n"
                f"🆔 <b>ID:</b> <code>{chat_id_str}</code>\n"
                f"🔗 <b>Havola:</b> {url}\n\n"
                "Endi botdan foydalanuvchilar ushbu kanalga a'zo bo'lishlari shart bo'ladi.",
                parse_mode=ParseMode.HTML
            )
            await state.clear()
        else:
            await status_msg.edit_text("⚠️ Bu kanal avvalroq qo'shilgan!")
            await state.clear()

    except Exception as e:
        await status_msg.edit_text(
            f"❌ Kanalni tekshirib bo'lmadi.\n\n"
            f"Xatolik: <code>{html.escape(str(e))}</code>\n\n"
            f"Kanal mavjudligini va bot unga qo'shilganligini tekshiring.",
            parse_mode=ParseMode.HTML
        )


@dp.callback_query(F.data == "admin:del_channel_menu")
async def callback_admin_del_channel_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    channels = await get_all_channels()
    if not channels:
        await callback.answer("O'chirish uchun kanallar mavjud emas.", show_alert=True)
        return

    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {ch['channel_title']}",
            callback_data=f"del_ch:{ch['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin:channels")])

    text = "🗑 <b>O'chirmoqchi bo'lgan kanal ustiga bosing:</b>"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.callback_query(F.data.startswith("del_ch:"))
async def callback_admin_del_channel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    ch_id = int(callback.data.split(":")[1])
    await remove_channel(ch_id)
    await callback.answer("✅ Kanal majburiy obunadan olib tashlandi!", show_alert=True)
    # Kanallar menyusiga qaytish
    channels = await get_all_channels()
    text = "📢 <b>Majburiy obuna kanallari:</b>\n\n"
    if not channels:
        text += "<i>Hozircha hech qanday homiy kanal ulanmagan.</i>\n"
    else:
        for idx, ch in enumerate(channels, start=1):
            text += f"{idx}. <b>{html.escape(ch['channel_title'])}</b>\n   ID: <code>{ch['channel_id']}</code>\n\n"

    buttons = [[InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="admin:add_channel")]]
    if channels:
        buttons.append([InlineKeyboardButton(text="🗑 Kanalni o'chirish", callback_data="admin:del_channel_menu")])
    buttons.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin:menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------
# ADMIN: VIDEO TAGIDAGI YOZUV (CAPTION) SOZLASH
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:edit_caption")
async def callback_admin_edit_caption(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    await state.set_state(AdminStates.waiting_for_caption)
    current_caption = await get_setting("caption_text", "Yo'q")
    text = (
        "✏️ <b>Video va audio tagidagi yozuvni sozlash</b>\n\n"
        "Foydalanuvchi video yoki musiqa yuklaganda fayl ostida chiqadigan yozuv:\n\n"
        f"<b>Hozirgi matn:</b>\n"
        f"<blockquote>{html.escape(current_caption)}</blockquote>\n\n"
        "Yangi matnni yuboring (reklama, kanalingiz linki, @downloatbot va boshqalar).\n\n"
        "Bekor qilish uchun /cancel deb yozing."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin:menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_for_caption)
async def process_save_caption(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text in ["/cancel", "❌ Bekor qilish"]:
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=get_main_keyboard(is_admin=True))
        return

    new_caption = message.text.strip()
    await set_setting("caption_text", new_caption)
    await state.clear()
    await message.answer(
        f"✅ <b>Video tagidagi yozuv muvaffaqiyatli yangilandi!</b>\n\n"
        f"<b>Yangi matn:</b>\n<blockquote>{html.escape(new_caption)}</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard(is_admin=True)
    )


# ---------------------------------------------------------------------
# ADMIN: FOYDALANUVCHINI QIDIRISH VA BOSHQARISH (BAN / BALL)
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:search_user")
async def callback_admin_search_user(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    await state.set_state(AdminStates.waiting_for_user_search)
    text = (
        "🔍 <b>Foydalanuvchini boshqarish</b>\n\n"
        "Kerakli foydalanuvchining <b>Telegram ID</b> sini yuboring:\n"
        "<i>(Masalan: 7394490565)</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin:menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_for_user_search)
async def process_user_search(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text in ["/cancel", "❌ Bekor qilish"]:
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=get_main_keyboard(is_admin=True))
        return

    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Iltimos, faqat raqamli Telegram ID kiriting:")
        return

    stats = await get_user_stats(target_id)
    if not stats:
        await message.answer("❌ Bu ID dagi foydalanuvchi bot bazasida topilmadi.")
        await state.clear()
        return

    await state.clear()
    status_emoji = "🔴 Bloklangan" if stats.get('is_banned') else "🟢 Faol"
    ban_btn_text = "✅ Blokdan chiqarish" if stats.get('is_banned') else "🚫 Bloklash"

    text = (
        f"👤 <b>Foydalanuvchi ma'lumotlari:</b>\n\n"
        f"• Ism: <b>{html.escape(stats['full_name'] or 'Noma\'lum')}</b>\n"
        f"• Username: @{stats['username'] or 'mavjud_emas'}\n"
        f"• Telegram ID: <code>{stats['user_id']}</code>\n"
        f"• Holat: <b>{status_emoji}</b>\n"
        f"• To'plagan ballari: <b>{stats['points']} ball</b>\n"
        f"• Taklif qilganlari: <b>{stats['referrals_count']} ta</b>\n"
        f"• Yuklagan videolari: <b>{stats['downloads_count']} ta</b>\n"
        f"• Ro'yxatdan o'tgan: <code>{stats['joined_at']}</code>\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ 5 ball berish", callback_data=f"user:add_pts:{target_id}:5"),
            InlineKeyboardButton(text="➖ 5 ball ayirish", callback_data=f"user:add_pts:{target_id}:-5"),
        ],
        [
            InlineKeyboardButton(text=ban_btn_text, callback_data=f"user:toggle_ban:{target_id}"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Admin Menyu", callback_data="admin:menu")
        ]
    ])
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("user:add_pts:"))
async def callback_user_add_points(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    parts = callback.data.split(":")
    target_id = int(parts[2])
    amount = int(parts[3])

    await add_user_points(target_id, amount)
    await callback.answer(f"Ball o'zgartirildi ({amount:+d})!", show_alert=True)

    stats = await get_user_stats(target_id)
    if stats:
        status_emoji = "🔴 Bloklangan" if stats.get('is_banned') else "🟢 Faol"
        ban_btn_text = "✅ Blokdan chiqarish" if stats.get('is_banned') else "🚫 Bloklash"
        text = (
            f"👤 <b>Foydalanuvchi ma'lumotlari:</b>\n\n"
            f"• Ism: <b>{html.escape(stats['full_name'] or 'Noma\'lum')}</b>\n"
            f"• Username: @{stats['username'] or 'mavjud_emas'}\n"
            f"• Telegram ID: <code>{stats['user_id']}</code>\n"
            f"• Holat: <b>{status_emoji}</b>\n"
            f"• To'plagan ballari: <b>{stats['points']} ball</b>\n"
            f"• Taklif qilganlari: <b>{stats['referrals_count']} ta</b>\n"
            f"• Yuklagan videolari: <b>{stats['downloads_count']} ta</b>\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ 5 ball berish", callback_data=f"user:add_pts:{target_id}:5"),
                InlineKeyboardButton(text="➖ 5 ball ayirish", callback_data=f"user:add_pts:{target_id}:-5"),
            ],
            [
                InlineKeyboardButton(text=ban_btn_text, callback_data=f"user:toggle_ban:{target_id}"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Admin Menyu", callback_data="admin:menu")
            ]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("user:toggle_ban:"))
async def callback_user_toggle_ban(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split(":")[2])
    new_ban_state = await toggle_ban_user(target_id)
    action_text = "foydalanuvchi bloklandi" if new_ban_state else "foydalanuvchi blokdan chiqarildi"
    await callback.answer(f"Muvaffaqiyatli: {action_text}!", show_alert=True)

    stats = await get_user_stats(target_id)
    if stats:
        status_emoji = "🔴 Bloklangan" if stats.get('is_banned') else "🟢 Faol"
        ban_btn_text = "✅ Blokdan chiqarish" if stats.get('is_banned') else "🚫 Bloklash"
        text = (
            f"👤 <b>Foydalanuvchi ma'lumotlari:</b>\n\n"
            f"• Ism: <b>{html.escape(stats['full_name'] or 'Noma\'lum')}</b>\n"
            f"• Username: @{stats['username'] or 'mavjud_emas'}\n"
            f"• Telegram ID: <code>{stats['user_id']}</code>\n"
            f"• Holat: <b>{status_emoji}</b>\n"
            f"• To'plagan ballari: <b>{stats['points']} ball</b>\n"
            f"• Taklif qilganlari: <b>{stats['referrals_count']} ta</b>\n"
            f"• Yuklagan videolari: <b>{stats['downloads_count']} ta</b>\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ 5 ball berish", callback_data=f"user:add_pts:{target_id}:5"),
                InlineKeyboardButton(text="➖ 5 ball ayirish", callback_data=f"user:add_pts:{target_id}:-5"),
            ],
            [
                InlineKeyboardButton(text=ban_btn_text, callback_data=f"user:toggle_ban:{target_id}"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Admin Menyu", callback_data="admin:menu")
            ]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------
# ADMIN: TO'G'RIDAN-TO'G'RI BALL BERISH / AYIRISH
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:give_points")
async def callback_admin_give_points(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_for_direct_points)
    text = (
        "💰 <b>Foydalanuvchiga ball berish yoki ayirish</b>\n\n"
        "Foydalanuvchi Telegram ID si va ball miqdorini probel bilan yozib yuboring:\n\n"
        "<i>Misollar:</i>\n"
        "• <code>7394490565 50</code> (50 ball qo'shish)\n"
        "• <code>7394490565 -20</code> (20 ball ayirish)\n\n"
        "Bekor qilish uchun /cancel deb yozing."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin:menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_for_direct_points)
async def process_direct_points(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text in ["/cancel", "❌ Bekor qilish"]:
        await state.clear()
        await message.answer("Amal bekor qilindi.", reply_markup=get_main_keyboard(is_admin=True))
        return

    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("⚠️ Noto'g'ri format! ID va ball miqdorini probel bilan yozing:\n<i>Masalan: 7394490565 50</i>", parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(parts[0])
        amount = int(parts[1])
    except ValueError:
        await message.answer("⚠️ Iltimos, faqat butun sonlar kiriting!")
        return

    stats = await get_user_stats(target_id)
    if not stats:
        await message.answer("❌ Bu ID dagi foydalanuvchi bot bazasida topilmadi.")
        return

    await add_user_points(target_id, amount)
    new_stats = await get_user_stats(target_id)
    new_pts = new_stats['points'] if new_stats else 0
    await state.clear()

    await message.answer(
        f"✅ <b>Ball muvaffaqiyatli o'zgartirildi!</b>\n\n"
        f"👤 Foydalanuvchi: <b>{html.escape(stats['full_name'] or 'Noma\'lum')}</b>\n"
        f"🆔 ID: <code>{target_id}</code>\n"
        f"💰 O'zgarish: <b>{amount:+d} ball</b>\n"
        f"💎 Jami bal: <b>{new_pts} ball</b>",
        reply_markup=get_main_keyboard(is_admin=True),
        parse_mode=ParseMode.HTML
    )

    try:
        action_text = f"+{amount}" if amount > 0 else f"{amount}"
        await bot.send_message(
            chat_id=target_id,
            text=f"🎁 <b>Hisobingizga {action_text} ball qo'shildi!</b>\n💎 Hozirgi balingiz: <b>{new_pts} ball</b> 🚀",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


# ---------------------------------------------------------------------
# ADMIN: TO'G'RIDAN-TO'G'RI BLOKLASH / OCHISH
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:ban_user")
async def callback_admin_ban_user(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_for_direct_ban)
    text = (
        "🚫 <b>Foydalanuvchini bloklash yoki blokdan chiqarish</b>\n\n"
        "Foydalanuvchi Telegram ID sini yuboring:\n"
        "<i>(Masalan: 7394490565)</i>\n\n"
        "Bekor qilish uchun /cancel deb yozing."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin:menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_for_direct_ban)
async def process_direct_ban(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text in ["/cancel", "❌ Bekor qilish"]:
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=get_main_keyboard(is_admin=True))
        return

    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Iltimos, to'g'ri Telegram ID kiriting:")
        return

    stats = await get_user_stats(target_id)
    if not stats:
        await message.answer("❌ Bu ID dagi foydalanuvchi topilmadi.")
        return

    new_ban = await toggle_ban_user(target_id)
    await state.clear()
    res_text = "🚫 Foydalanuvchi bloklandi!" if new_ban else "✅ Foydalanuvchi blokdan chiqarildi!"
    await message.answer(f"{res_text}\nID: <code>{target_id}</code>", reply_markup=get_main_keyboard(is_admin=True), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------
# ADMIN: TOP FOYDALANUVCHILAR RO'YXATI
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:top_users")
async def callback_admin_top_users(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    top = await get_top_users(limit=15)
    lines = ["🏆 <b>Eng faol foydalanuvchilar (Top-15):</b>\n"]
    if not top:
        lines.append("<i>Hozircha hech qanday foydalanuvchi yo'q.</i>")
    else:
        for idx, u in enumerate(top, start=1):
            name = html.escape(u['full_name'] or 'Noma\'lum')
            lines.append(f"{idx}. <b>{name}</b> (<code>{u['user_id']}</code>) — <b>{u['points']} ball</b> | <b>{u['downloads_count']} yuklama</b>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin:menu")]
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


# ---------------------------------------------------------------------
# ADMIN: MA'LUMOTLAR BAZASINI YUKLAB OLISH (BACKUP)
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:backup")
async def callback_admin_backup(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        return

    if not os.path.exists(DB_PATH):
        await callback.answer("Ma'lumotlar bazasi fayli topilmadi.", show_alert=True)
        return

    await callback.answer("Baza yuborilmoqda...")
    db_file = FSInputFile(DB_PATH, filename=f"backup_universal_bot_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.db")
    await bot.send_document(
        chat_id=callback.message.chat.id,
        document=db_file,
        caption=f"💾 <b>Ma'lumotlar bazasi zaxirasi</b>\n📅 Sana: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.HTML
    )


# ---------------------------------------------------------------------
# ADMIN: XABAR TARQATISH (BROADCAST)
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "admin:broadcast")
async def callback_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    await state.set_state(AdminStates.waiting_for_broadcast)
    text = (
        "📢 <b>Barcha foydalanuvchilarga xabar tarqatish</b>\n\n"
        "Tarqatmoqchi bo'lgan xabaringizni yuboring (matn, rasm, video, audio yoki forward xabar).\n\n"
        "Bekor qilish uchun /cancel deb yozing."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin:menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text in ["/cancel", "❌ Bekor qilish"]:
        await state.clear()
        await message.answer("Xabar tarqatish bekor qilindi.", reply_markup=get_main_keyboard(is_admin=True))
        return

    await state.clear()
    user_ids = await get_all_user_ids()
    total = len(user_ids)

    if total == 0:
        await message.answer("Bazada birorta ham foydalanuvchi yo'q.")
        return

    status_msg = await message.answer(f"⏳ Xabar tarqatish boshlandi... (Jami: {total} ta)")

    sent = 0
    blocked = 0

    for uid in user_ids:
        try:
            await message.copy_to(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            pass

    report = (
        f"📢 <b>Xabar tarqatish yakunlandi!</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total} ta</b>\n"
        f"✅ Muvaffaqiyatli yetkazildi: <b>{sent} ta</b>\n"
        f"🚫 Botni bloklaganlar: <b>{blocked} ta</b>"
    )
    await status_msg.edit_text(report, parse_mode=ParseMode.HTML)
    await message.answer("Asosiy menyu:", reply_markup=get_main_keyboard(is_admin=True))


SYSTEM_MENU_BUTTONS = {
    "📥 Qanday yuklanadi?", "👤 Profilim", "🏆 Peshqadamlar (Top-10)",
    "🎁 Do'stlarni taklif qilish", "👑 Admin Panel", "🎵 Musiqa qidirish",
    "❌ Bekor qilish"
}


@dp.message(F.text, ~F.text.startswith("/"))
async def handle_text_music_search(message: Message, bot: Bot, state: FSMContext):
    # Agar biror FSM holatida bo'lsa, o'tkazib yuborish
    current_state = await state.get_state()
    if current_state:
        return

    query = message.text.strip()
    if len(query) < 2:
        return

    # Tizim menyu tugmalarini e'tiborsiz qoldirish
    if query in SYSTEM_MENU_BUTTONS:
        return

    # Agar havola (URL) bo'lsa, uni o'tkazib yuborish (handle_url ga tegmaslik uchun)
    if URL_REGEX.search(query):
        return

    user_id = message.from_user.id
    if await is_user_banned(user_id):
        await message.answer("🚫 Siz botdan foydalanishdan bloklangansiz.")
        return

    # Majburiy obunani tekshirish
    is_sub, unsubs = await check_user_subscriptions(bot, user_id)
    if not is_sub:
        await message.answer(
            "⚠️ <b>Musiqa qidirish uchun quyidagi homiy kanallarga a'zo bo'ling:</b>",
            reply_markup=get_subscription_keyboard(unsubs),
            parse_mode=ParseMode.HTML
        )
        return

    status_msg = await message.reply(f"🔎 <b>«{html.escape(query)}»</b> bo'yicha qidirilmoqda...")
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    try:
        tracks = await search_music(query, limit=5)
        if not tracks:
            await status_msg.edit_text(f"❌ <b>«{html.escape(query)}»</b> bo'yicha hech qanday musiqa topilmadi.")
            return

        lines = [f"🎵 <b>«{html.escape(query)}» bo'yicha topilgan natijalar:</b>\n"]
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        row = []

        for idx, t in enumerate(tracks):
            t_id = t['id']
            media_cache[t_id] = {
                'url': f"https://www.youtube.com/watch?v={t_id}",
                'info': {
                    'id': t_id,
                    'title': t['title'],
                    'uploader': t['uploader'],
                    'duration_sec': None,
                    'platform': 'youtube'
                }
            }
            lines.append(f"{emojis[idx]} <b>{html.escape(t['title'])}</b> ({t['duration']})")
            row.append(InlineKeyboardButton(text=emojis[idx], callback_data=f"dl:audio:{t_id}"))

        lines.append("\n<i>Yuklab olish uchun kerakli raqam ustiga bosing:</i>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            row,
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="delete_msg")]
        ])

        await status_msg.edit_text("\n".join(lines), reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        await status_msg.edit_text(f"❌ Qidirishda xatolik yuz berdi: <code>{html.escape(str(e)[:150])}</code>", parse_mode=ParseMode.HTML)


# =====================================================================
# ISHGA TUSHIRISH (MAIN)
# =====================================================================
async def main():
    if not BOT_TOKEN:
        print("[XATOLIK] BOT_TOKEN kiritilmagan!")
        sys.exit(1)

    await init_db()
    
    print("Ma'lumotlar bazasi tayyorlandi.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    print("\n" + "*" * 50)
    print("🚀 Universal Video Yuklovchi Boti ishga tushdi!")
    print(f"🤖 Bot: @{BOT_USERNAME}")
    print(f"👑 Admin ID: {ADMIN_ID}")
    print("*" * 50 + "\n")

    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot to'xtatildi.")
