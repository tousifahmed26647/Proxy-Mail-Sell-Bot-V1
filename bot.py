import os
import sqlite3
import threading
import time
import html
from flask import Flask, request, jsonify
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
import uuid
import re
import json
import base64
import hashlib
import tempfile
import math
import hmac
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from urllib.parse import urlencode
import requests
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

try:
    import psycopg
except ImportError:
    psycopg = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    # Install beautifulsoup4 automatically on first start so no manual pip step is needed.
    BeautifulSoup = None
    try:
        import subprocess, sys as _sys
        for _extra in ([], ["--break-system-packages"]):
            if subprocess.call([_sys.executable, "-m", "pip", "install", "-q", "beautifulsoup4"] + _extra) == 0:
                break
        from bs4 import BeautifulSoup
    except Exception as _e:
        print(f"beautifulsoup4 auto-install failed: {_e}")
        BeautifulSoup = None
from urllib.parse import urljoin, urlparse

# 🔑 টোকেন এবং অ্যাডমিন আইডি
TOKEN = "8287516175:AAFtqoENrOQdNF3KVkchjU6RmnYn-jKE6hU"
ADMIN_ID = 8465541252

FORCE_SUB_CHANNEL = "@Pro_Tools_Officials"

# ----------------- Environment loading (.env support, no extra package needed) -----------------
def _load_env_file():
    """Load KEY=VALUE lines from a .env file next to bot.py. Real environment variables always win."""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        for candidate in (os.path.join(base, ".env"), ".env"):
            if not os.path.isfile(candidate):
                continue
            with open(candidate, "r", encoding="utf-8") as env_file:
                for raw_line in env_file:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
            break
    except Exception as e:
        print(f".env loading skipped: {e}")

_load_env_file()

def _dec(value):
    """Safe Decimal conversion. Returns None for anything that is not a finite number."""
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None
    return parsed if parsed.is_finite() else None

def _q2(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _fmt_dec(value):
    text = f"{_q2(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text

# ----------------- Emon Pay (bKash / Nagad / Rocket) Config -----------------
# Keys are read from environment variables first. The fallback below is your Brand key;
# move it to your host's environment variables / .env file when you can.
EMONPAY_BRAND_KEY = (os.environ.get("EMONPAY_BRAND_KEY") or "do56MXmugVPs17f9DR7j1XMigLXnSbk3c7rUM0HpZAR3tsBWWK").strip()
# If your panel gives a separate API key / secret key, set EMONPAY_API_KEY and EMONPAY_SECRET_KEY.
# Otherwise the Brand key is used for the API-KEY header as well.
EMONPAY_API_KEY = (os.environ.get("EMONPAY_API_KEY") or EMONPAY_BRAND_KEY).strip()
EMONPAY_SECRET_KEY = (os.environ.get("EMONPAY_SECRET_KEY") or "").strip()
BOT_USERNAME = "AllServicesAx_bot"  # without @
# No public URL is needed: after paying, the user returns to the bot and the bot detects the payment by itself.
BOT_RETURN_URL = f"https://t.me/{BOT_USERNAME}"
EMONPAY_BASE_URL = "https://pay.emonpay.xyz/api/payment"
EMONPAY_CREATE_URL = EMONPAY_BASE_URL + "/create"
EMONPAY_VERIFY_URL = EMONPAY_BASE_URL + "/verify"
EMONPAY_METHOD_LABEL = "Bkash, Nagad, Rocket (Emon Pay)"
EMONPAY_LINK_POLL_WINDOW = 1800       # no public URL: poll the id found in the payment link for 30 minutes
EMONPAY_PENDING_TXN_TTL = 6 * 3600    # keep re-checking a TrxID that the gateway still reports as pending
EMONPAY_POLL_INTERVAL = 10            # seconds

# ----------------- EmonPay Dashboard login (MFS TrxID auto verification) -----------------
# Credentials come ONLY from the environment / .env file. Never hardcode them.
# >>> Fill these two lines with your https://emonpay.xyz/sign-in dashboard email and password. No .env file needed. <<<
EMONPAY_LOGIN_EMAIL = (os.environ.get("EMONPAY_LOGIN_EMAIL") or "tousifahmed26647@gmail.com").strip()
EMONPAY_LOGIN_PASSWORD = os.environ.get("EMONPAY_LOGIN_PASSWORD") or "Tousif@@@"
EMONPAY_DASH_BASE = "https://emonpay.xyz"
EMONPAY_DASH_TIMEOUT = 15             # seconds per HTTP request
EMONPAY_DASH_MIN_INTERVAL = 0.6       # minimum seconds between two dashboard requests (rate control)
EMONPAY_DASH_MAX_PAGES = 5            # transaction-list pages scanned per lookup
EMONPAY_DASH_MAX_DETAIL_FETCH = 30    # new detail pages fetched per lookup
EMONPAY_DASH_SUCCESS_TTL = 6 * 3600   # a Success transaction never changes: cache it long
EMONPAY_DASH_OTHER_TTL = 30           # pending/failed ones may change: cache them very briefly
EMONPAY_DASH_STALE_GRACE = 300        # MFS time may be at most 5 min older than the invoice creation time
EMONPAY_DASH_USER_COOLDOWN = 8        # seconds between two TrxID submissions of the same user
EMONPAY_DASH_MAX_ATTEMPTS = 10        # failed TrxID attempts allowed per invoice
EMONPAY_AUTOSCAN_INTERVAL = 12        # seconds between automatic dashboard scans (only runs while a deposit is pending)
EMONPAY_AUTOSCAN_PAGES = 2            # newest dashboard list pages checked per scan
EMONPAY_AUTOSCAN_WINDOW = 6 * 3600    # a pending invoice is auto-matched from the dashboard for this long

# ----------------- Binance Pay Config (edit the values right here; env variables are optional overrides) -----------------
BINANCE_API_KEY = (os.environ.get("BINANCE_API_KEY") or "tPvn9eXNnkaOXS4q17mE8fSG1VlRyp1AxN6oWr204pzibSwecwLn6fqPbeNcbpEo").strip()
BINANCE_API_SECRET = (os.environ.get("BINANCE_API_SECRET") or "9JABSsvkISn4FtO3CY52EVkh0MQLbTklxqo6mpbUMyFb8wfdMDgEBA7Nttt9MylO").strip()
BINANCE_PAY_UID = (os.environ.get("BINANCE_PAY_UID") or "1123474021").strip()
BINANCE_USDT_RATE = _dec(os.environ.get("BINANCE_USDT_RATE") or "125")
if BINANCE_USDT_RATE is None or BINANCE_USDT_RATE <= 0:
    BINANCE_USDT_RATE = Decimal("125")
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_POLL_INTERVAL = 30            # seconds between background checks (each history call costs 3000 weight)
BINANCE_CLAIM_WINDOW = 6 * 3600       # keep re-checking a submitted Binance Order ID for 6 hours

# Unpaid/unverified invoices stay valid for 24h so a late (but real) payment is still credited.
PAYMENT_INVOICE_TTL = 24 * 3600

# ----------------- Flask App Initialization -----------------
app = Flask(__name__)

# ----------------- Render/VPS persistent database backup -----------------
DATABASE_URL = "postgresql://database_j32d_user:FTmrSqscTHXXDwWva91HDu1fuMu1mW41@dpg-da7b6bnavr4c73bhcqk0-a.oregon-postgres.render.com/database_j32d"
PG_BACKUP_TABLE = "bot_sqlite_backup"
_DB_SYNC_LOCK = threading.Lock()
_DB_RESTORING = False

def _pg_connect():
    if not DATABASE_URL or psycopg is None:
        return None
    return psycopg.connect(DATABASE_URL, autocommit=True)

def _restore_sqlite_from_postgres():
    global _DB_RESTORING
    if not DATABASE_URL or psycopg is None or os.path.exists("shop_bot.db"):
        return False
    try:
        with _pg_connect() as pg:
            with pg.cursor() as cur:
                cur.execute(f"CREATE TABLE IF NOT EXISTS {PG_BACKUP_TABLE} (id INTEGER PRIMARY KEY, db_data BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                cur.execute(f"SELECT db_data FROM {PG_BACKUP_TABLE} WHERE id = 1")
                row = cur.fetchone()
                if not row:
                    return False
                tmp = "shop_bot.db.restore.tmp"
                with open(tmp, "wb") as f:
                    f.write(bytes(row[0]))
                os.replace(tmp, "shop_bot.db")
                return True
    except Exception as e:
        print(f"PostgreSQL restore skipped: {e}")
        return False

def _backup_sqlite_to_postgres():
    if _DB_RESTORING or not DATABASE_URL or psycopg is None or not os.path.exists("shop_bot.db"):
        return
    try:
        with _DB_SYNC_LOCK:
            with open("shop_bot.db", "rb") as f:
                data = f.read()
            with _pg_connect() as pg:
                with pg.cursor() as cur:
                    cur.execute(f"CREATE TABLE IF NOT EXISTS {PG_BACKUP_TABLE} (id INTEGER PRIMARY KEY, db_data BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
                    cur.execute(f"INSERT INTO {PG_BACKUP_TABLE} (id, db_data, updated_at) VALUES (1, %s, NOW()) ON CONFLICT (id) DO UPDATE SET db_data = EXCLUDED.db_data, updated_at = NOW()", (data,))
    except Exception as e:
        print(f"PostgreSQL backup failed: {e}")

class _PersistentSQLiteConnection:
    def __init__(self, conn):
        self._conn = conn
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def commit(self):
        result = self._conn.commit()
        _backup_sqlite_to_postgres()
        return result
    def close(self):
        return self._conn.close()
    def __enter__(self):
        self._conn.__enter__()
        return self
    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

def db_connect(path="shop_bot.db", timeout=30):
    if path == "shop_bot.db" and not os.path.exists(path):
        _restore_sqlite_from_postgres()
    return _PersistentSQLiteConnection(sqlite3.connect(path, timeout=timeout))

_restore_sqlite_from_postgres()

# ----------------- Single-instance Telegram polling protection -----------------
# Prevent two copies of this bot (using the same token) from polling simultaneously
# on the same computer. The OS releases this lock automatically if Python exits.
_SINGLE_INSTANCE_LOCK_HANDLE = None


def acquire_single_instance_lock():
    global _SINGLE_INSTANCE_LOCK_HANDLE
    lock_name = "telegram_bot_polling_" + hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:20] + ".lock"
    lock_path = os.path.join(tempfile.gettempdir(), lock_name)
    lock_handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock_handle.seek(0, os.SEEK_END)
            if lock_handle.tell() == 0:
                lock_handle.write(b"0")
                lock_handle.flush()
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        lock_handle.close()
        print("ERROR: Another copy of this bot is already running on this computer. Stop the old bot process before starting this one.")
        return False

    _SINGLE_INSTANCE_LOCK_HANDLE = lock_handle
    return True


bot = telebot.TeleBot(TOKEN)
user_temp_deposit = {}
pending_deposits = {}
admin_states = {}
order_delivery_cache = {}
member_sell_states = {}  # Independent state; does not alter existing service flows.

# ----------------- Premium Custom Emoji Config (Buttons) -----------------
PREMIUM_EMOJIS = {
    "all_services": "5406683434124859552",
    "deposit": "5264895611517300926",
    "profile": "5325971446625758812",
    "support": "5269402556924180806",
    "restart": "6084506256427456298",
    "admin_panel": "6269458311381258421",
    "search_user": "5435907392334215552",

    "bkash": "6152327402098793153",
    "nagad": "6152331409303280461",
    "rocket": "6086803441160558654",
    "binance": "5798751138647905130",
    "bonus": "5321280470460145769",

    "money_10": "5879991085001871624",
    "money_20": "5879991085001871624",
    "money_30": "5879991085001871624",
    "money_40": "5879991085001871624",

    "get_code": "5042101437237036298",
    "referral": "5386380500946414098",

    "package_item": "5251680306385151250",
    "telegram_premium_cat": "5789911984283586269",
    "vpn_service_cat": "5998881947328188290",
    "mail_cat": "5354826222331245563",
    "proxy_cat": "5287292843763713628",
    "mail_item": "5967280668885913944",
    "proxy_item": "4956560549287560231",
    # Screenshot-specific service button emojis
    "mail_service_item": "5354826222331245563",
    "proxy_9_item": "6143321667427904097",
    "proxy_711_item": "6143393822878475954",
    "proxy_h143_item": "5375177250553487549",
    "proxy_rapid_item": "6143192522056283070",
    "proxy_loki_item": "6147909495658913509",
    "qty_select": "6071063305143719063",
    "back_button": "6206505206197261313",

    "channel_join": "6206505206197261313",
    "verify_tick": "6089184854497302293",

    "open_here_btn": "5368516976048104432",
    "txt_file_btn": "6109595432441090250",

    "vpn_express": "5796153709931009517",
    "vpn_cyberghost": "5796309230696797985",
    "vpn_panda": "5796561774773801876",
    "vpn_ipvanish": "5816862049367494010", 
    "vpn_nord": "5796345694969140339",
    "vpn_hma": "5796595022115639741",
    "vpn_pia": "5794298344188678751",
    "vpn_surfshark": "5796592771552777710",
    "vpn_bitdefender": "5793973558761756143",
    "vpn_turbo": "5796633857209931262",
    "vpn_proton": "5796504385420792898",

    "analytics": "5213107179329953547",
    "member_count_file": "5314665894406805639",
    "balance_list": "5215420556089776398",
    "download_txt": "5435932616677145539",
    "recover_balance": "6084506256427456298",
    "live_broadcast": "6219940267626078011",
    "add_balance": "6219646698021463224",
    "bulk_update": "6084506256427456298",
    "remove_money": "6068810023566317366",
    "post_edit": "5435907392334215552",
    "button_edit": "5406683434124859552",
    "edit_service": "5269402556924180806",
    "manage_stock": "5314665894406805639",
    "approve_btn": "6269285159774720688",
    "reject_btn": "6068810023566317366",
    "refresh_data": "6084506256427456298",
    "edit_pencil": "5435907392334215552",
    "delete_trash": "6068810023566317366"
}

POST_PREMIUM_EMOJIS = {
    "welcome_heart": "6292078018638651747",
    "welcome_shop": "5350699789551935589",
    "list_point": "6068777875736109056",
    "trusted_badge": "6084746478243288096",
    "fast_speed": "6088947729352890506",
    "down_arrow": "6219700471012009032",
    "warn_icon": "6068810023566317366",
    "step_tick": "6125147242731933920",
    "support_headphone": "6282996898701775483",
    "profile_user": "5325971446625758812",
    "balance_coin": "5215420556089776398",
    "diamond_badge": "6068673795793624089",
    "id_badge": "5435907392334215552",
    "deposit_money": "6219646698021463224",
    "sendmoney_shield": "5463424023734014980",
    "bkash_method": "6152327402098793153",
    "nagad_method": "6152331409303280461",
    "rocket_method": "6086803441160558654",
    "binance_method": "5798751138647905130",
    "trx_input": "5814427657609153890",
    "waiting_clock": "6122681660921092237",
    "deposit_success": "6269285159774720688",
    "buy_success": "6269285159774720688",
    "box_package": "5314665894406805639",
    "money_spent": "6219646698021463224",
    "wallet_balance": "5215420556089776398",
    "copy_down": "5435932616677145539",
    "no_stock": "6068743889659892487",
    "insufficient_bal": "6071301770317927702",
    "quantity_icon": "5220149332262528784",
    "choose_select": "5854703064887332338",
    "txt_file_caption": "6109595432441090250",
    "order_pending": "5215484787325676090",
    "order_delivered": "6219954312169136748",
    "loudspeaker": "6219940267626078011",
    "admin_crown": "6188206545277291962",
    "alarm_bell": "6283048296575408684",
    "stats_chart": "5213107179329953547",
    "username_link": "5814427657609153890",
    "telegram_premium_cat": "5789911984283586269",
    "vpn_service_cat": "5998881947328188290",
    "mail_cat": "5354826222331245563",
    "proxy_cat": "5287292843763713628",
    "camera_icon": "5244699979506788336"
}

def pe(key):
    emoji_id = POST_PREMIUM_EMOJIS.get(key, "")
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">✨</tg-emoji>'
    return ""

def get_vpn_emoji_key(vpn_name):
    low = vpn_name.lower()
    if "express" in low: return "vpn_express"
    if "cyberghost" in low: return "vpn_cyberghost"
    if "panda" in low: return "vpn_panda"
    if "ipvanish" in low or "ip vanish" in low: return "vpn_ipvanish"
    if "nord" in low: return "vpn_nord"
    if "hma" in low: return "vpn_hma"
    if "pia" in low: return "vpn_pia"
    if "surfshark" in low: return "vpn_surfshark"
    if "bitdefender" in low: return "vpn_bitdefender"
    if "turbo" in low: return "vpn_turbo"
    if "proton" in low: return "vpn_proton"
    return "vpn_service_cat"

PREMIUM_EMOJI_PREFIX = "premium_emoji_"

def save_premium_emoji_mapping(mapping):
    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    for emoji, emoji_id in mapping.items():
        emoji = str(emoji).strip()
        emoji_id = str(emoji_id).strip()
        if emoji and emoji_id.isdigit():
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (PREMIUM_EMOJI_PREFIX + emoji, emoji_id)
            )
    conn.commit()
    conn.close()

def load_premium_emoji_mapping():
    try:
        conn = db_connect("shop_bot.db", timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings WHERE key LIKE ?", (PREMIUM_EMOJI_PREFIX + "%",))
        rows = cursor.fetchall()
        conn.close()
        return {k[len(PREMIUM_EMOJI_PREFIX):]: v for k, v in rows}
    except Exception:
        return {}

def parse_premium_emoji_mapping(file_or_text_content):
    """Parse mappings such as '🔴 = 5788939937335366' from text or a TXT file."""
    mapping = {}
    legacy_entries = {}
    for raw_line in str(file_or_text_content or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [x.strip() for x in re.split(r'\s*\|\s*', line)]
        if len(parts) >= 3 and parts[-1].isdigit():
            emoji = parts[-2]
            if emoji:
                mapping[emoji] = parts[-1]
            if parts[0] and parts[0] != emoji:
                legacy_entries[parts[0]] = parts[-1]
            continue
        match = re.match(r'(.+?)\s*(?:=|:)\s*(\d{10,30})\s*$', line)
        if match:
            emoji, emoji_id = match.group(1).strip(), match.group(2).strip()
            if emoji:
                mapping[emoji] = emoji_id
    return mapping, legacy_entries


def _is_emoji_mapping_key(value):
    """Exclude button-name mappings; only use emoji-only keys in broadcasts."""
    if not value or len(value) > 24:
        return False
    has_emoji = False
    for char in value:
        cp = ord(char)
        is_emoji_codepoint = (
            0x1F000 <= cp <= 0x1FAFF or
            0x2600 <= cp <= 0x27FF or
            0x2300 <= cp <= 0x23FF or
            0x2B00 <= cp <= 0x2BFF or
            0x1F1E6 <= cp <= 0x1F1FF or
            0x1F3FB <= cp <= 0x1F3FF or
            cp in (0x200D, 0xFE0E, 0xFE0F, 0x20E3) or
            0xE0020 <= cp <= 0xE007F
        )
        if not is_emoji_codepoint:
            return False
        if (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27FF or
                0x2300 <= cp <= 0x23FF or 0x2B00 <= cp <= 0x2BFF or
                0x1F1E6 <= cp <= 0x1F1FF):
            has_emoji = True
    return has_emoji


def convert_premium_emoji_text(text, mapping=None):
    """Escape plain broadcast text and replace mapped emoji with Telegram custom emoji."""
    if text is None or text == "":
        return text
    raw_text = str(text)
    mapping = mapping if mapping is not None else load_premium_emoji_mapping()
    usable = {
        str(emoji): str(emoji_id)
        for emoji, emoji_id in mapping.items()
        if str(emoji).strip() and str(emoji_id).isdigit() and _is_emoji_mapping_key(str(emoji))
    }
    if not usable:
        return html.escape(raw_text)

    ordered = sorted(usable.keys(), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(emoji) for emoji in ordered))
    output = []
    last_end = 0
    for match in pattern.finditer(raw_text):
        output.append(html.escape(raw_text[last_end:match.start()]))
        emoji = match.group(0)
        output.append(f'<tg-emoji emoji-id="{usable[emoji]}">{html.escape(emoji)}</tg-emoji>')
        last_end = match.end()
    output.append(html.escape(raw_text[last_end:]))
    return "".join(output)

def _custom_emoji_id_from_text(text):
    if not text:
        return None, text
    mapping = load_premium_emoji_mapping()
    for emoji in sorted(mapping.keys(), key=len, reverse=True):
        if emoji and emoji in text:
            return mapping[emoji], text.replace(emoji, "", 1).strip()
    return None, text

def _emoji_id_for_button(text, emoji_key=None):
    direct_id, clean_text = _custom_emoji_id_from_text(text)
    if direct_id:
        return direct_id, clean_text
    if emoji_key:
        key_value = str(emoji_key).strip()
        if key_value.isdigit():
            return key_value, text
        return PREMIUM_EMOJIS.get(key_value), text
    return None, text

OWNER_PREMIUM_SETTING = "owner_premium"

def update_owner_premium_status(user):
    if not user or user.id != ADMIN_ID: return
    premium = getattr(user, "is_premium", None)
    if premium is None: return
    try:
        conn = db_connect("shop_bot.db", timeout=30)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (OWNER_PREMIUM_SETTING, "1" if premium else "0"))
        conn.commit()
        conn.close()
    except Exception:
        pass

class _RawCustomEmojiButton:
    def __init__(self, text, emoji_id=None, callback_data=None, url=None):
        self.text = str(text)
        self.emoji_id = str(emoji_id) if emoji_id else None
        self.callback_data = callback_data
        self.url = url

    def to_dict(self):
        data = {"text": self.text}
        if self.emoji_id:
            data["icon_custom_emoji_id"] = self.emoji_id
        if self.callback_data is not None:
            data["callback_data"] = self.callback_data
        if self.url is not None:
            data["url"] = self.url
        return data

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)

def premium_keyboard_button(text, emoji_key=None):
    emoji_id, clean_text = _emoji_id_for_button(text, emoji_key)
    if emoji_id:
        return _RawCustomEmojiButton(clean_text, emoji_id=emoji_id)
    return KeyboardButton(clean_text)

def premium_inline_button(text, emoji_key=None, callback_data=None, url=None):
    emoji_id, clean_text = _emoji_id_for_button(text, emoji_key)
    if emoji_id:
        return _RawCustomEmojiButton(
            clean_text,
            emoji_id=emoji_id,
            callback_data=callback_data,
            url=url,
        )
    if callback_data is not None:
        return InlineKeyboardButton(clean_text, callback_data=callback_data)
    if url is not None:
        return InlineKeyboardButton(clean_text, url=url)
    return InlineKeyboardButton(clean_text)

USER_LOG_FILE = "auto_member_count_user.txt"

def save_user_id_to_file(user_id):
    try:
        existing_ids = set()
        if os.path.exists(USER_LOG_FILE):
            with open(USER_LOG_FILE, "r", encoding="utf-8") as f:
                existing_ids = {line.strip() for line in f if line.strip()}
        if str(user_id) not in existing_ids:
            with open(USER_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{user_id}\n")
    except Exception:
        pass

def check_user_subscription(user_id):
    if ADMIN_ID and user_id == ADMIN_ID: return True
    try:
        member = bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        if member.status in ['member', 'administrator', 'creator']: return True
    except Exception:
        pass
    return False

def get_force_sub_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        premium_inline_button("Join Channel", "channel_join", url=f"https://t.me/BotAllUpdateServis"),
        premium_inline_button("Verify", "verify_tick", callback_data="verify_subscription")
    )
    return markup

def get_deposit_amount_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        premium_inline_button("10 টাকা", "money_10", callback_data="depamt_10"),
        premium_inline_button("20 টাকা", "money_20", callback_data="depamt_20"),
        premium_inline_button("30 টাকা", "money_30", callback_data="depamt_30"),
        premium_inline_button("40 টাকা", "money_40", callback_data="depamt_40")
    )
    markup.add(InlineKeyboardButton("💵 Custom Amount", callback_data="depamt_custom"))
    return markup

def get_payment_method_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        premium_inline_button("bKash", "bkash", callback_data="paymethod_bKash"),
        premium_inline_button("Nagad", "nagad", callback_data="paymethod_Nagad"),
        premium_inline_button("Rocket", "rocket", callback_data="paymethod_Rocket"),
        premium_inline_button("Binance", "binance", callback_data="paymethod_Binance")
    )
    return markup

def auto_drive_backup_loop():
    SCOPES = ['https://www.googleapis.com/auth/drive.file']
    SERVICE_ACCOUNT_FILE = 'credentials.json' 
    PARENT_FOLDER_ID = '1Fmf22X9PIWhi1qH5q1pf_4xWGeDOMH6V' 

    while True:
        time.sleep(86400) 
        try:
            if not os.path.exists('shop_bot.db'): continue
            creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
            service = build('drive', 'v3', credentials=creds)
            query = f"name = 'shop_bot.db' and '{PARENT_FOLDER_ID}' in parents and trashed = false"
            response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
            files = response.get('files', [])
            media = MediaFileUpload('shop_bot.db', resumable=True)
            if files:
                service.files().update(fileId=files[0]['id'], media_body=media).execute()
            else:
                service.files().create(body={'name': 'shop_bot.db', 'parents': [PARENT_FOLDER_ID]}, media_body=media, fields='id').execute()
        except Exception:
            pass

def init_db():
    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        first_name TEXT,
                        balance REAL DEFAULT 0.0,
                        username TEXT,
                        referred_by INTEGER
                    )''')
    try: cursor.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE users ADD COLUMN referral_deposit_bonus REAL NOT NULL DEFAULT 0.0")
    except sqlite3.OperationalError: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS categories (
                        cat_id TEXT PRIMARY KEY,
                        name TEXT,
                        price REAL,
                        has_emoji INTEGER DEFAULT 1,
                        group_type TEXT DEFAULT 'mail',
                        button_name TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )''')
    try: cursor.execute("ALTER TABLE categories ADD COLUMN has_emoji INTEGER DEFAULT 1")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE categories ADD COLUMN group_type TEXT DEFAULT 'mail'")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE categories ADD COLUMN button_name TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE categories ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE categories ADD COLUMN created_at TEXT")
    except sqlite3.OperationalError: pass
    cursor.execute("UPDATE categories SET button_name = name WHERE button_name IS NULL OR button_name = ''")
    cursor.execute("UPDATE categories SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL OR created_at = ''")

    cursor.execute('''CREATE TABLE IF NOT EXISTS sub_services (
                        sub_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cat_id TEXT,
                        sub_name TEXT,
                        price REAL,
                        has_emoji INTEGER DEFAULT 1
                    )''')
    try: cursor.execute("ALTER TABLE sub_services ADD COLUMN has_emoji INTEGER DEFAULT 1")
    except sqlite3.OperationalError: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS stock (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cat_id TEXT,
                        content TEXT,
                        status TEXT NOT NULL DEFAULT 'available',
                        sold_by INTEGER,
                        sold_at TEXT,
                        order_id TEXT
                    )''')
    try: cursor.execute("ALTER TABLE stock ADD COLUMN status TEXT NOT NULL DEFAULT 'available'")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE stock ADD COLUMN sold_by INTEGER")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE stock ADD COLUMN sold_at TEXT")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE stock ADD COLUMN order_id TEXT")
    except sqlite3.OperationalError: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS stock_sales (
                        sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        stock_id INTEGER NOT NULL UNIQUE,
                        cat_id TEXT NOT NULL,
                        category_name TEXT NOT NULL,
                        button_name TEXT,
                        buyer_user_id INTEGER NOT NULL,
                        item_content TEXT NOT NULL,
                        unit_price REAL NOT NULL,
                        order_id TEXT NOT NULL,
                        sold_at TEXT NOT NULL
                    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS referral_deposit_bonus_ledger (
                        bonus_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        deposit_request_id TEXT NOT NULL UNIQUE,
                        referrer_id INTEGER NOT NULL,
                        referred_user_id INTEGER NOT NULL,
                        deposit_amount REAL NOT NULL,
                        bonus_amount REAL NOT NULL DEFAULT 0.02,
                        credited_at TEXT NOT NULL
                    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS paymently_transactions (
                        invoice_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        amount REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'PENDING',
                        transaction_id TEXT,
                        created_at INTEGER NOT NULL
                    )''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_paymently_status ON paymently_transactions(status, created_at)")

    # --- Auto-payment system (Emon Pay + Binance): invoices + deposit history ---
    cursor.execute('''CREATE TABLE IF NOT EXISTS payment_invoices (
                        invoice_ref TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        expected_amount TEXT NOT NULL,
                        payment_method TEXT NOT NULL,
                        gateway TEXT NOT NULL,
                        expected_usdt TEXT,
                        transaction_id TEXT,
                        txn_source TEXT,
                        txn_set_at INTEGER,
                        status TEXT NOT NULL DEFAULT 'PENDING',
                        created_at INTEGER NOT NULL,
                        completed_at INTEGER
                    )''')
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_invoices_txn_completed ON payment_invoices(transaction_id) WHERE status = 'COMPLETED' AND transaction_id IS NOT NULL")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payment_invoices_status ON payment_invoices(status, gateway, created_at)")
    cursor.execute('''CREATE TABLE IF NOT EXISTS deposit_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        invoice_ref TEXT NOT NULL UNIQUE,
                        user_id INTEGER NOT NULL,
                        amount TEXT NOT NULL,
                        payment_method TEXT NOT NULL,
                        gateway TEXT NOT NULL,
                        transaction_id TEXT NOT NULL UNIQUE,
                        usdt_amount TEXT,
                        created_at INTEGER NOT NULL
                    )''')
    # --- EmonPay Dashboard TrxID verification: additive, idempotent migration ---
    try:
        existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(payment_invoices)").fetchall()}
        for col_name, col_type in (("normalized_mfs_trxid", "TEXT"), ("emonpay_transaction_id", "TEXT"),
                                   ("verification_status", "TEXT"), ("verified_at", "INTEGER"), ("emon_link_id", "TEXT"),
                                   ("paid_method", "TEXT")):
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE payment_invoices ADD COLUMN {col_name} {col_type}")
        cursor.execute('''CREATE TABLE IF NOT EXISTS mfs_trxid_claims (
                            normalized_mfs_trxid TEXT PRIMARY KEY,
                            invoice_ref TEXT NOT NULL UNIQUE,
                            user_id INTEGER NOT NULL,
                            emonpay_transaction_id TEXT,
                            claimed_at INTEGER NOT NULL
                        )''')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_invoices_mfs_trxid ON payment_invoices(normalized_mfs_trxid) WHERE normalized_mfs_trxid IS NOT NULL")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_payment_invoices_link ON payment_invoices(emon_link_id)")
    except Exception as e:
        print(f"EmonPay dashboard migration skipped: {e}")

    # One-time, safe migration of the previous Emon Pay table so in-flight payments are not lost.
    try:
        cursor.execute('''INSERT OR IGNORE INTO payment_invoices
                        (invoice_ref, user_id, expected_amount, payment_method, gateway, transaction_id, txn_source, txn_set_at, status, created_at, completed_at)
                        SELECT invoice_id, user_id, printf('%.2f', amount), 'EmonPay', 'EMONPAY',
                               CASE WHEN transaction_id = 'N/A' THEN NULL ELSE transaction_id END,
                               CASE WHEN transaction_id IS NULL OR transaction_id = 'N/A' THEN NULL ELSE 'LINK' END,
                               CASE WHEN transaction_id IS NULL OR transaction_id = 'N/A' THEN NULL ELSE created_at END,
                               status, created_at,
                               CASE WHEN status = 'COMPLETED' THEN created_at END
                        FROM paymently_transactions''')
    except Exception as e:
        print(f"Legacy payment migration skipped: {e}")

    cursor.execute('''CREATE TABLE IF NOT EXISTS custom_buttons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        btn_name TEXT,
                        btn_url TEXT
                    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS member_sell_orders (
                        order_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        first_name TEXT,
                        username TEXT,
                        duration_key TEXT NOT NULL,
                        duration_label TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        price REAL NOT NULL,
                        target_link TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        delivery_text TEXT,
                        refunded_at TEXT
                    )''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_member_sell_orders_user ON member_sell_orders(user_id, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_member_sell_orders_status ON member_sell_orders(status, created_at)")
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )''')

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stock_category_status_id ON stock(cat_id, status, id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stock_sales_category ON stock_sales(cat_id, sold_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_referral_bonus_referrer ON referral_deposit_bonus_ledger(referrer_id, credited_at)")

    # These views make each service/button and its live inventory totals easy to inspect in SQLite viewers.
    cursor.execute("DROP VIEW IF EXISTS category_stock_summary")
    cursor.execute('''CREATE VIEW category_stock_summary AS
        SELECT
            c.cat_id AS category_id,
            c.name AS category_name,
            COALESCE(NULLIF(c.button_name, ''), c.name) AS button_name,
            c.group_type AS group_type,
            c.price AS unit_price,
            c.is_active AS is_active,
            COUNT(s.id) AS total_stock,
            SUM(CASE WHEN s.status = 'available' THEN 1 ELSE 0 END) AS available_stock,
            SUM(CASE WHEN s.status = 'sold' THEN 1 ELSE 0 END) AS sold_stock,
            SUM(CASE WHEN s.status = 'removed' THEN 1 ELSE 0 END) AS removed_stock
        FROM categories c
        LEFT JOIN stock s ON s.cat_id = c.cat_id
        GROUP BY c.cat_id, c.name, c.button_name, c.group_type, c.price, c.is_active''')

    cursor.execute("DROP VIEW IF EXISTS stock_details")
    cursor.execute('''CREATE VIEW stock_details AS
        SELECT
            s.id AS stock_id,
            s.cat_id AS category_id,
            COALESCE(c.name, s.cat_id) AS category_name,
            COALESCE(NULLIF(c.button_name, ''), c.name, s.cat_id) AS button_name,
            s.content AS stock_content,
            s.status AS stock_status,
            s.sold_by AS sold_by_user_id,
            s.sold_at AS sold_at,
            s.order_id AS order_id,
            ss.unit_price AS sold_unit_price,
            ss.buyer_user_id AS buyer_user_id
        FROM stock s
        LEFT JOIN categories c ON c.cat_id = s.cat_id
        LEFT JOIN stock_sales ss ON ss.stock_id = s.id''')

    conn.commit()

    default_settings = {
        'bkash_num': '+8801842145918',
        'nagad_num': '+8801842145918',
        'rocket_num': '+8801842145918',
        'binance_uid': '1123474021',
        'get_code_link': 'https://dongvanfb.net/read_mail_box/',
        'pc_directory_path': '',
        'pc_live_money_path': '',
        'pc_live_stock_path': ''
    }
    for k, v in default_settings.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()

    default_posts = {
        'welcome_msg': (
            f"{pe('welcome_heart')} <b>Welcome {{first_name}}!</b> {pe('welcome_shop')} আমাদের Shop এ আপনাকে স্বাগতম።\n\n"
            f"{pe('list_point')} এখানে কম মূল্যে পেয়ে যাবেন:\n\n"
            f"{pe('list_point')} Premium Proxy\n"
            f"{pe('list_point')} Premium VPN\n"
            f"{pe('list_point')} Hotmail Account (High quality)\n"
            f"{pe('list_point')} Outlook Account (High quality)\n"
            f"{pe('list_point')} Telegram Premium Buy\n\n"
            f"{pe('trusted_badge')} <b>Trusted service</b>\n"
            f"{pe('fast_speed')} <b>Auto fast service</b>\n\n"
            f"{pe('down_arrow')} নিচের মেনু বাটনগুলো ব্যবহার করুন:"
        ),
        'not_joined_msg': (
            f"{pe('warn_icon')} <b>আপনি আমাদের চ্যানেলে Join করেননি!</b>\n\n"
            f"{pe('step_tick')} বট ব্যবহার করতে প্রথমে Join Channel এ ক্লিক করে জয়েন করুন, এরপর Verify বাটনে ক্লিক করুন।"
        ),
        'support_msg': f"{pe('support_headphone')} <b>যেকোনো প্রয়োজনে আমাদের সাপোর্ট আইডিতে যোগাযোগ করুন:</b>\nhttps://t.me/FBbuysellAX",
        'deposit_info_msg': (
            f"{pe('deposit_money')} <b>আপনি কত টাকা ডিপোজিট করতে চান সংখ্যাটি লিখে পাঠান:</b>\n\n"
            f"{pe('list_point')} সর্বনিম্ন ১০ টাকা {pe('deposit_money')}\n"
            f"{pe('list_point')} সর্বোচ্চ ১০০০০ টাকা {pe('deposit_money')}"
        )
    }

    for key, val in default_posts.items():
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
    conn.commit()

    # Screenshot-matched Mail and Proxy buttons. Existing IDs are reused where possible
    # so any stock already attached to those IDs remains linked to the same service.
    screenshot_categories = [
        ("hotmail", "Hotmail Trusted", 0.8, "mail"),
        ("outlook", "Outlook Trusted", 0.8, "mail"),
        ("Outlook fr", "Outlook Fr.", 0.85, "mail"),
        ("proxy_9_100", "9 proxy (100MB)", 15.0, "proxy"),
        ("proxy", "9 proxy (200MB)", 27.0, "proxy"),
        ("proxy_711_100", "711 Proxy (100 MB)", 12.0, "proxy"),
        ("proxy_711_200", "711 Proxy (200 MB)", 22.0, "proxy"),
        ("proxy_h143_100", "H143 Proxy (100MB)", 7.0, "proxy"),
        ("proxy_h143_200", "H143 Proxy (200MB)", 15.0, "proxy"),
        ("proxy_rapid_100", "Rapid Proxy (100MB)", 13.0, "proxy"),
        ("proxy_rapid_200", "Rapid Proxy (200MB)", 24.0, "proxy"),
        ("proxy_loki_100", "Loki Proxy (100MB)", 12.0, "proxy"),
    ]
    cursor.execute("SELECT value FROM settings WHERE key = 'screenshot_proxy_mail_seed_v1'")
    screenshot_seed_done = cursor.fetchone() is not None
    for default_cat_id, default_name, default_price, default_group in screenshot_categories:
        if screenshot_seed_done:
            # Add missing default buttons, but never overwrite later Admin edits on restart.
            cursor.execute(
                "INSERT OR IGNORE INTO categories (cat_id, name, price, has_emoji, group_type, button_name, is_active, created_at) "
                "VALUES (?, ?, ?, 1, ?, ?, 1, CURRENT_TIMESTAMP)",
                (default_cat_id, default_name, default_price, default_group, default_name),
            )
        else:
            # One-time migration from the old built-in labels/prices; preserve linked stock rows.
            cursor.execute(
                "INSERT INTO categories (cat_id, name, price, has_emoji, group_type, button_name, is_active, created_at) "
                "VALUES (?, ?, ?, 1, ?, ?, 1, CURRENT_TIMESTAMP) "
                "ON CONFLICT(cat_id) DO UPDATE SET name=excluded.name, price=excluded.price, has_emoji=1, "
                "group_type=excluded.group_type, button_name=excluded.button_name, is_active=1",
                (default_cat_id, default_name, default_price, default_group, default_name),
            )

    if not screenshot_seed_done:
        # This was an old built-in Mail entry, not part of the supplied screenshot list.
        # Keep its row/stock intact but hide it from the customer Mail menu.
        cursor.execute("UPDATE categories SET is_active = 0 WHERE cat_id = 'Ig Hotmail'")
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('screenshot_proxy_mail_seed_v1', '1')")

    cursor.execute("SELECT COUNT(*) FROM sub_services WHERE cat_id = 'telegram_premium'")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('telegram_premium', 'Telegram Premium (3 Month)', 2010.0, 1)")
        cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('telegram_premium', 'Telegram Premium (6 Month)', 2520.0, 1)")
        cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('telegram_premium', 'Telegram Premium (12 Month)', 4030.0, 1)")

    cursor.execute("DELETE FROM sub_services WHERE cat_id IN ('vpn_3d', 'vpn_7d', 'vpn_14d', 'vpn_30d')")
    
    vpn_3d_list = ["Express VPN", "Cyberghost VPN", "Panda VPN", "IP Vanish VPN"]
    for v in vpn_3d_list:
        sub_n = f"{v} (3 Day)"
        cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('vpn_3d', ?, 20.0, 1)", (sub_n,))

    vpn_7d_list = ["Nord VPN", "HMA VPN", "PIA VPN", "Surfshark VPN", "Bitdefender VPN", "Turbo VPN"]
    for v in vpn_7d_list:
        sub_n = f"{v} (7 Day)"
        cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('vpn_7d', ?, 30.0, 1)", (sub_n,))

    vpn_14d_list = ["Proton VPN"]
    for v in vpn_14d_list:
        sub_n = f"{v} (14 Day)"
        cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('vpn_14d', ?, 50.0, 1)", (sub_n,))

    cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('vpn_30d', 'Nord VPN (30 Day)', 210.0, 1)")
    cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('vpn_30d', 'Hma VPN (30 Day)', 110.0, 1)")
    cursor.execute("INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES ('vpn_30d', 'Proton VPN (30 Day)', 160.0, 1)")

    conn.commit()
    conn.close()

init_db()
_backup_sqlite_to_postgres()

def get_setting_msg(key, default=""):
    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else default


# Exact button emoji IDs from the supplied Proxy/Mail screenshots.
CATEGORY_EMOJI_SETTING_PREFIX = "category_emoji_id:"
CATEGORY_EMOJI_IDS = {
    "hotmail": "5354826222331245563",
    "outlook": "5354826222331245563",
    "Outlook fr": "5354826222331245563",
    "proxy": "6143321667427904097",  # Existing proxy row is reused for 9 Proxy (200MB).
    "proxy_9_100": "6143321667427904097",
    "proxy_711_100": "6143393822878475954",
    "proxy_711_200": "6143393822878475954",
    "proxy_h143_100": "5375177250553487549",
    "proxy_h143_200": "5375177250553487549",
    "proxy_rapid_100": "6143192522056283070",
    "proxy_rapid_200": "6143192522056283070",
    "proxy_loki_100": "6147909495658913509",
}


def get_category_button_emoji_key(cat_id, name, group_type, has_emoji=1):
    """Return saved custom emoji ID or the screenshot-matched service emoji."""
    if not has_emoji:
        return None
    custom_id = get_setting_msg(CATEGORY_EMOJI_SETTING_PREFIX + str(cat_id), "")
    if str(custom_id).isdigit():
        return str(custom_id)
    low = (name or "").lower()
    if group_type == "mail":
        return PREMIUM_EMOJIS["mail_service_item"]
    if "711" in low:
        return PREMIUM_EMOJIS["proxy_711_item"]
    if "h143" in low or "h14" in low:
        return PREMIUM_EMOJIS["proxy_h143_item"]
    if "rapid" in low:
        return PREMIUM_EMOJIS["proxy_rapid_item"]
    if "loki" in low:
        return PREMIUM_EMOJIS["proxy_loki_item"]
    if "9 proxy" in low or "9proxy" in low:
        return PREMIUM_EMOJIS["proxy_9_item"]
    if cat_id in CATEGORY_EMOJI_IDS:
        return CATEGORY_EMOJI_IDS[cat_id]
    return PREMIUM_EMOJIS["proxy_item"]


def get_sub_service_button_emoji_key(sub_id, has_emoji=1, fallback=None):
    if not has_emoji:
        return None
    custom_id = get_setting_msg("sub_service_emoji_id:" + str(sub_id), "")
    return str(custom_id) if str(custom_id).isdigit() else fallback


def _save_pending_button(pending_data, has_emoji, custom_emoji_id=None):
    """Create/update a category or sub-service using the same Add/Edit save path."""
    mode = pending_data.get("mode")
    conn = db_connect("shop_bot.db", timeout=30)
    cur = conn.cursor()
    saved_id = None
    name = pending_data.get("name", "")
    price = float(pending_data.get("price", 0))

    if mode == "new_cat":
        clean_id_base = "".join(c for c in name.lower() if c.isalnum() or c == "_")[:12] or "service"
        saved_id = f"{clean_id_base}_{str(uuid.uuid4())[:6]}"
        group_type = "proxy" if "proxy" in name.lower() else "mail"
        cur.execute(
            "INSERT INTO categories (cat_id, name, price, has_emoji, group_type, button_name, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
            (saved_id, name, price, int(bool(has_emoji)), group_type, name),
        )
        emoji_setting_key = CATEGORY_EMOJI_SETTING_PREFIX + saved_id
    elif mode == "edit_cat":
        saved_id = str(pending_data["cat_id"])
        cur.execute(
            "UPDATE categories SET name = ?, button_name = ?, price = ?, has_emoji = ? WHERE cat_id = ?",
            (name, name, price, int(bool(has_emoji)), saved_id),
        )
        emoji_setting_key = CATEGORY_EMOJI_SETTING_PREFIX + saved_id
    elif mode == "new_sub":
        cur.execute(
            "INSERT INTO sub_services (cat_id, sub_name, price, has_emoji) VALUES (?, ?, ?, ?)",
            (pending_data["cat_id"], name, price, int(bool(has_emoji))),
        )
        saved_id = str(cur.lastrowid)
        emoji_setting_key = "sub_service_emoji_id:" + saved_id
    elif mode == "edit_sub":
        saved_id = str(pending_data["sub_id"])
        cur.execute(
            "UPDATE sub_services SET sub_name = ?, price = ?, has_emoji = ? WHERE sub_id = ?",
            (name, price, int(bool(has_emoji)), saved_id),
        )
        emoji_setting_key = "sub_service_emoji_id:" + saved_id
    else:
        conn.close()
        raise ValueError(f"Unsupported button edit mode: {mode}")

    if custom_emoji_id:
        cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (emoji_setting_key, str(custom_emoji_id)))
    elif not has_emoji:
        cur.execute("DELETE FROM settings WHERE key = ?", (emoji_setting_key,))

    conn.commit()
    conn.close()
    return saved_id

def get_permanent_keyboard(user_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        premium_keyboard_button("All Services", "all_services"),
        premium_keyboard_button("Deposit", "deposit")
    )
    markup.row(
        premium_keyboard_button("Profile", "profile"),
        premium_keyboard_button("Support", "support")
    )
    markup.row(
        premium_keyboard_button("Get Code", "get_code"),
        premium_keyboard_button("Referral", "referral")
    )
    if user_id == ADMIN_ID:
        markup.row(
            premium_keyboard_button("Admin Panel", "admin_panel"),
            premium_keyboard_button("Search User ID", "search_user")
        )
    markup.row(premium_keyboard_button("Restart Bot", "restart"))
    return markup

def main_menu_inline(user_id):
    markup = InlineKeyboardMarkup(row_width=2)
    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    cursor.execute("SELECT btn_name, btn_url FROM custom_buttons")
    custom_btns = cursor.fetchall()
    conn.close()
    for b_name, b_url in custom_btns:
        markup.add(premium_inline_button(b_name, "home_button", url=b_url))
    return markup

def get_categories_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        premium_inline_button("Mail Service", "mail_cat", callback_data="group_mail"),
        premium_inline_button("Proxy Service", "proxy_cat", callback_data="group_proxy"),
        premium_inline_button("Telegram Premium Buy", "telegram_premium_cat", callback_data="special_cat_telegram_premium"),
        premium_inline_button("VPN Service", "vpn_service_cat", callback_data="special_cat_vpn_service"),
        premium_inline_button("Telegram Member Sell", "5884079687709234720", callback_data="member_sell")
    )
    return markup


# ----------------- Telegram Member Sell (isolated new service) -----------------
# Custom emoji IDs supplied by the owner for each Telegram Member Sell message field.
MEMBER_SELL_TEXT_EMOJI = "5884079687709234720"
MEMBER_SELL_EMOJIS = {
    "service": "5884079687709234720",
    "duration": "5296370519136812159",
    "delivery": "6267229004311303657",
    "fast": "5415681936070821017",
    "down": "6071406125138318025",
    "menu_description": "4943195336911881191",
    "prompt_members": "6244387315964188652",
    "prompt_price": "6296202896639791835",
    "prompt_target": "6275878213746955453",
    "success_order_id": "6131684255970891449",
    "success_balance": "5287231198098117669",
    "delivery_info": "6188206545277291962",
    "thanks": "6292078018638651747",
    "admin_name": "5256143829672672750",
    "admin_username": "6307589808424228455",
    "admin_user_id": "6307777408300753473",
    "admin_link": "6068777875736109056",
    "admin_status": "6068697658631919093",
    "refund_notice": "5854796227022949904",
    "refund_amount": "5391090636961099009",
}
MEMBER_SELL_PLANS = {
    "30": {
        "label": "30 Day",
        "heading": "30 Day Service",
        "delivery": "Only 10 minutes",
        "packages": [(100, 35), (200, 60), (500, 100), (1000, 150), (2000, 300)],
    },
    "90": {
        "label": "3 Month",
        "heading": "3 Month Service",
        "delivery": "Only 20 minutes",
        "packages": [(500, 130), (1000, 250), (2000, 500)],
    },
    "365": {
        "label": "1 Year",
        "heading": "1 Years Service",
        "delivery": "Only 30 minutes",
        "packages": [(500, 210), (1000, 400), (2000, 800)],
    },
}


def member_sell_emoji(emoji_id, fallback="✨"):
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def is_valid_member_sell_target(value):
    """Accept Telegram public usernames and t.me/telegram.me public or invite links."""
    value = (value or "").strip()
    if re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", value):
        return True
    return bool(re.fullmatch(
        r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z0-9_+/-]+(?:\?[A-Za-z0-9_=&%+.-]*)?/?",
        value
    ))


def get_member_sell_order(order_id):
    conn = db_connect("shop_bot.db", timeout=30)
    cur = conn.cursor()
    cur.execute(
        "SELECT order_id, user_id, first_name, username, duration_label, quantity, price, target_link, status "
        "FROM member_sell_orders WHERE order_id = ?",
        (order_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row

def get_admin_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        premium_inline_button("Live Statistic Dashboard", "analytics", callback_data="admin_stats_today"),
        premium_inline_button("Bot Analytics & Users", "analytics", callback_data="admin_analytics"),
        premium_inline_button("Auto Count Member ID", "member_count_file", callback_data="admin_auto_member_count"),
        premium_inline_button("All Member Balance List", "balance_list", callback_data="admin_member_balance_list"),
        premium_inline_button("Download Balance List TXT", "download_txt", callback_data="admin_download_balance_txt"),
        premium_inline_button("Recover Balance List (.txt)", "recover_balance", callback_data="admin_recover_balance_list"),
        premium_inline_button("Broadcast Message", "live_broadcast", callback_data="admin_broadcast"),
        premium_inline_button("Live Analysis & User Broadcast Message", "live_broadcast", callback_data="admin_live_broadcast"),
        premium_inline_button("Premium Emoji Set", "button_edit", callback_data="admin_premium_emoji_set"),
        premium_inline_button("Payment Method Edit", "edit_service", callback_data="admin_payment_method_edit"),
        premium_inline_button("All Details Download Directory PC", "download_txt", callback_data="admin_pc_directory_setup"),
        premium_inline_button("All Live Analysis & Money Directive PC Download", "download_txt", callback_data="admin_pc_live_money_setup"),
        premium_inline_button("Only Stock Available Directive PC Download", "download_txt", callback_data="admin_pc_live_stock_setup"),
        premium_inline_button("Add Member Money Back", "add_balance", callback_data="admin_add_member_balance"),
        premium_inline_button("Remove Money", "remove_money", callback_data="admin_remove_member_balance"),
        premium_inline_button("Update Back All Money Member", "bulk_update", callback_data="admin_bulk_money_update"),
        premium_inline_button("All Post Edit", "post_edit", callback_data="admin_all_post_edit"),
        premium_inline_button("All Button Edit", "button_edit", callback_data="admin_all_button_edit"),
        premium_inline_button("Edit Prices & Services", "edit_service", callback_data="admin_edit_services"),
        premium_inline_button("Manage Stock (Add/Remove)", "manage_stock", callback_data="admin_add_stock_menu")
    )
    return markup

def pc_directory_export_loop():
    while True:
        time.sleep(15)
        try:
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            date_only = datetime.now().strftime("%Y-%m-%d")
            
            path1 = get_setting_msg("pc_directory_path", "")
            if path1 and os.path.exists(path1):
                conn = db_connect("shop_bot.db", timeout=30)
                cur = conn.cursor()
                cur.execute("SELECT user_id, first_name, balance, username FROM users")
                all_u = cur.fetchall()
                
                with open(os.path.join(path1, f"bot_analysis_user_{date_only}.txt"), "w", encoding="utf-8") as f:
                    f.write(f"--- Last Updated: {timestamp_str} ---\n")
                    for uid, fn, bal, un in all_u:
                        f.write(f"ID: {uid} | Name: {fn} | Username: {un} | Balance: {bal}\n")
                
                with open(os.path.join(path1, f"auto_member_count_user_{date_only}.txt"), "w", encoding="utf-8") as f:
                    f.write(f"--- Last Updated: {timestamp_str} ---\n")
                    for uid, _, _, _ in all_u:
                        f.write(f"{uid}\n")

                cur.execute("SELECT user_id, balance FROM users WHERE balance > 0")
                with open(os.path.join(path1, f"all_member_balance_list_{date_only}.txt"), "w", encoding="utf-8") as f:
                    f.write(f"--- Last Updated: {timestamp_str} ---\n")
                    for uid, bal in cur.fetchall():
                        f.write(f"{uid} | {bal}\n")
                conn.close()

            path2 = get_setting_msg("pc_live_money_path", "")
            if path2 and os.path.exists(path2):
                conn = db_connect("shop_bot.db", timeout=30)
                cur = conn.cursor()
                cur.execute("SELECT user_id, first_name, balance, username FROM users")
                all_u = cur.fetchall()
                with open(os.path.join(path2, "all_time_balance_and_uid_analysis.txt"), "w", encoding="utf-8") as f:
                    f.write(f"--- Live Bot Money Analysis [Updated: {timestamp_str}] ---\n")
                    for uid, fn, bal, un in all_u:
                        f.write(f"UID: {uid} | Name: {fn} | Username: {un} | Balance: ৳{bal}\n")
                conn.close()

            path3 = get_setting_msg("pc_live_stock_path", "")
            if path3 and os.path.exists(path3):
                conn = db_connect("shop_bot.db", timeout=30)
                cur = conn.cursor()
                cur.execute("SELECT cat_id, content FROM stock WHERE status = 'available'")
                stocks = cur.fetchall()
                with open(os.path.join(path3, "live_available_stock_details.txt"), "w", encoding="utf-8") as f:
                    f.write(f"--- Live Stock Available [Updated: {timestamp_str}] ---\n")
                    for cid, cnt in stocks:
                        f.write(f"Category: {cid} -> {cnt}\n")
                conn.close()

        except Exception as e:
            print(f"PC directory export error: {e}")

t_pc_export = threading.Thread(target=pc_directory_export_loop, daemon=True)
t_pc_export.start()

@bot.message_handler(func=lambda message: message.text and "Restart Bot" in message.text)
@bot.message_handler(commands=['start'])
def send_welcome(message):
    update_owner_premium_status(message.from_user)
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "User"
    username = f"@{message.from_user.username}" if message.from_user.username else "N/A"
    
    save_user_id_to_file(user_id)

    args = message.text.split()
    referred_by = None
    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id != user_id:
                referred_by = ref_id
        except ValueError:
            pass

    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, referred_by FROM users WHERE user_id = ?", (user_id,))
    existing_user = cursor.fetchone()
    
    if not existing_user:
        cursor.execute("INSERT INTO users (user_id, first_name, balance, username, referred_by) VALUES (?, ?, 0.0, ?, ?)", (user_id, first_name, username, referred_by))
        conn.commit()
        
        if referred_by:
            # আপডেট লজিক: রেফারেল আর্নিং ও মেইন ব্যালেন্স উভয়ত্রই যোগ করা হচ্ছে
            cursor.execute("UPDATE users SET balance = balance + 0.01 WHERE user_id = ?", (referred_by,))
            conn.commit()
            try:
                bot.send_message(
                    referred_by,
                    f"<tg-emoji emoji-id=\"5321280470460145769\">🎁</tg-emoji> <b>রেফারেল বোনাস!</b>\nআপনার রেফারেল লিংক থেকে একজন নতুন মেম্বার যুক্ত হয়েছেন। আপনি পেয়েছেন <b>0.01 টাকা</b> বোনাস!",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    else:
        cursor.execute("UPDATE users SET first_name = ?, username = ? WHERE user_id = ?", (first_name, username, user_id))
        conn.commit()
    conn.close()

    if not check_user_subscription(user_id):
        not_joined_msg = get_setting_msg('not_joined_msg', f"{pe('warn_icon')} আপনি আমাদের চ্যানেলে Join করেননি!")
        bot.send_message(message.chat.id, not_joined_msg, parse_mode="HTML", reply_markup=get_force_sub_markup())
        return

    welcome_template = get_setting_msg('welcome_msg', f"স্বাগতম {{first_name}}!")
    landing_text = welcome_template.replace("{first_name}", first_name)

    bot.send_message(message.chat.id, landing_text, parse_mode="HTML", reply_markup=main_menu_inline(user_id))
    bot.send_message(message.chat.id, f"{pe('down_arrow')} আপনার সুবিধার্থে নিচের মেনু বাটনগুলো ব্যবহার করুন:", parse_mode="HTML", reply_markup=get_permanent_keyboard(user_id))

@bot.message_handler(func=lambda message: message.text and any(keyword in message.text for keyword in ["All Services", "Deposit", "Profile", "Support", "Get Code", "Referral", "Admin Panel", "Search User ID"]))
def handle_reply_buttons(message):
    update_owner_premium_status(message.from_user)
    user_id = message.from_user.id
    text = message.text
    username = f"@{message.from_user.username}" if message.from_user.username else "N/A"
    
    save_user_id_to_file(user_id)

    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
    conn.commit()
    conn.close()

    if not check_user_subscription(user_id):
        not_joined_msg = get_setting_msg('not_joined_msg', f"{pe('warn_icon')} আপনি এখনো চ্যানেলে Join করেননি!")
        bot.send_message(message.chat.id, not_joined_msg, parse_mode="HTML", reply_markup=get_force_sub_markup())
        return

    if user_id in user_temp_deposit:
        del user_temp_deposit[user_id]
    
    if "All Services" in text:
        markup = get_categories_markup()
        bot.send_message(message.chat.id, f"{pe('welcome_shop')} <b>আমাদের কাছে নিচের সার্ভিসগুলো রয়েছে:</b>", parse_mode="HTML", reply_markup=markup)

    elif "Deposit" in text:
        user_temp_deposit[user_id] = {"step": "waiting_amount"}
        msg = get_setting_msg('deposit_info_msg', (
            f"{pe('deposit_money')} <b>আপনি কত টাকা ডিপোজিট করতে চান সংখ্যাটি লিখে পাঠান:</b>\n\n"
            f"{pe('list_point')} সর্বনিম্ন ১০ টাকা\n"
            f"{pe('list_point')} সর্বোচ্চ ১০০০০ টাকা"
        ))
        bot.send_message(message.chat.id, msg, parse_mode="HTML", reply_markup=get_deposit_amount_markup())

    elif "Profile" in text:
        conn = db_connect("shop_bot.db", timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT balance, username, COALESCE(referral_deposit_bonus, 0) FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        bal = res[0] if res else 0.0
        u_name = res[1] if res and res[1] else username
        referral_deposit_bonus = res[2] if res else 0.0
        
        cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
        ref_count = cursor.fetchone()[0]
        ref_earned = (ref_count * 0.01) + referral_deposit_bonus
        conn.close()
        
        dollar_val = bal / 125.0
        
        profile_text = (
            f"{pe('profile_user')} <b>Your Account Profile</b>\n\n"
            f"{pe('id_badge')} <b>User ID:</b> <code>{user_id}</code>\n"
            f"{pe('username_link')} <b>Username:</b> <b>{u_name}</b>\n"
            f"{pe('balance_coin')} <b>Account Balance:</b> <b>৳{bal}</b> (~${dollar_val:.2f}) {pe('diamond_badge')}\n\n"
            f"<tg-emoji emoji-id=\"5386380500946414098\">👥</tg-emoji> <b>Total Referral:</b> <b>{ref_count} জন</b>\n"
            f"{pe('balance_coin')} <b>Total Referral Bonus:</b> <b>৳{ref_earned:.2f}</b>"
        )
        bot.send_message(message.chat.id, profile_text, parse_mode="HTML")

    elif "Support" in text:
        supp_msg = get_setting_msg('support_msg', f"{pe('support_headphone')} যেকোনো প্রয়োজনে আমাদের সাপোর্ট আইডিতে যোগাযোগ করুন:\nhttps://t.me/FBbuysellAX")
        bot.send_message(message.chat.id, supp_msg, parse_mode="HTML")

    elif "Get Code" in text:
        get_code_url = get_setting_msg('get_code_link', 'https://dongvanfb.net/read_mail_box/')
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Hotmail & Outlook Code", "get_code", url=get_code_url))
        
        get_code_text = f"<tg-emoji emoji-id=\"5321331284218238259\">🔥</tg-emoji> <b>Hotmail, Outlook Receivers Code আনতে চাইলে এই বাটনে ক্লিক করুন:</b>"
        bot.send_message(message.chat.id, get_code_text, parse_mode="HTML", reply_markup=markup)

    elif "Referral" in text:
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        ref_text = (
            f"<tg-emoji emoji-id=\"5386380500946414098\">⭐</tg-emoji> <b>Referral System & Earnings</b>\n\n"
            f"<tg-emoji emoji-id=\"5379742233853451967\">📌</tg-emoji> আপনার রেফারেল লিংক:\n<code>{ref_link}</code>\n\n"
            f"{pe('list_point')} কেউ আপনার লিংকে জয়েন করলে পাবেন: <b>0.01 টাকা</b>\n"
            f"{pe('list_point')} আপনার রেফারকৃত কেউ ডিপোজিট করলে পাবেন: <b>0.02 টাকা</b>\n\n"
            f"<tg-emoji emoji-id=\"6258108916136153920\">✨</tg-emoji> লিংকটি বন্ধুদের সাথে শেয়ার করুন!"
        )
        bot.send_message(message.chat.id, ref_text, parse_mode="HTML")

    elif "Search User ID" in text and user_id == ADMIN_ID:
        admin_states[user_id] = {"action": "searching_user"}
        bot.send_message(message.chat.id, f"{pe('list_point')} ইউজারের সম্পূর্ণ তথ্য ও ব্যালেন্স দেখতে তার <b>User ID</b> চ্যাটে লিখে পাঠান:", parse_mode="HTML")

    elif "Admin Panel" in text and user_id == ADMIN_ID:
        bot.send_message(message.chat.id, f"{pe('admin_crown')} <b>Admin Control Panel</b>\n\nনিচের অপশনগুলো থেকে ম্যানেজ করুন:", parse_mode="HTML", reply_markup=get_admin_markup())


# ----------------- Payment System: Emon Pay (bKash / Nagad / Rocket) + Binance -----------------
def _emonpay_headers():
    headers = {
        "API-KEY": EMONPAY_API_KEY,
        "BRAND-KEY": EMONPAY_BRAND_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Descriptive UA: some WAFs reject the bare default "python-requests/x" agent.
        "User-Agent": "Mozilla/5.0 (compatible; AllServicesBot/1.0; +https://t.me/" + BOT_USERNAME + ")"
    }
    if EMONPAY_SECRET_KEY:
        headers["SECRET-KEY"] = EMONPAY_SECRET_KEY
    return headers

_PAY_LOG_LAST = {}

def _emonpay_throttled(key, interval=300):
    """True at most once per `interval` seconds for a given key (stops log/alert spam)."""
    now = time.time()
    if now - _PAY_LOG_LAST.get(key, 0) < interval:
        return False
    _PAY_LOG_LAST[key] = now
    return True

def _emonpay_clean(text, limit=300):
    """Trim a provider response and make sure no key/secret can ever leak into logs or messages."""
    text = str(text or "")
    for secret in (EMONPAY_API_KEY, EMONPAY_BRAND_KEY, EMONPAY_SECRET_KEY, BINANCE_API_KEY, BINANCE_API_SECRET, EMONPAY_LOGIN_PASSWORD):
        if secret:
            text = text.replace(secret, "***")
    return text.strip()[:limit]

def _emonpay_post(url, payload, timeout=20):
    """POST JSON to Emon Pay. Never raises. Returns status/body/headers so failures can be diagnosed."""
    result = {"status": None, "data": {}, "text": "", "headers": {}, "error": None}
    try:
        resp = requests.post(url, json=payload, headers=_emonpay_headers(), timeout=timeout)
        result["status"] = resp.status_code
        result["text"] = resp.text or ""
        result["headers"] = {k.lower(): v for k, v in resp.headers.items()}
        try:
            parsed = resp.json()
            result["data"] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            result["data"] = {}
    except requests.exceptions.Timeout:
        result["error"] = "timeout"
    except requests.exceptions.ConnectionError as e:
        result["error"] = "connection: " + _emonpay_clean(e, 160)
    except Exception as e:
        result["error"] = "error: " + _emonpay_clean(e, 160)
    return result

def _emonpay_hint(result):
    """Plain-language likely cause for a failed Emon Pay response (shown to the admin only)."""
    status = result.get("status")
    text = (result.get("text") or "").lower()
    headers = result.get("headers") or {}
    if status is None:
        if result.get("error") == "timeout":
            return "Gateway timed out. Server network is slow or the gateway is down."
        return "Cannot reach pay.emonpay.xyz from this server (network/DNS/firewall block)."
    if status == 401:
        return "Keys rejected. Check EMONPAY_API_KEY / EMONPAY_SECRET_KEY / EMONPAY_BRAND_KEY match the API credentials + Brand in your Emon Pay panel."
    if status == 403:
        if "cloudflare" in text or "cf-ray" in headers or "cloudflare" in str(headers.get("server", "")).lower():
            return "Blocked by Cloudflare/WAF in front of the gateway (usually the hosting server's IP). Ask Emon Pay support to whitelist this server's IP."
        return "Access denied by the gateway: key disabled, restricted to another IP/domain, or missing SECRET-KEY. Check the credentials in the Emon Pay panel."
    if status == 404:
        return "Endpoint not found. Check the API base URL."
    if status == 405:
        return "Wrong HTTP method for this endpoint."
    if status == 422:
        return "Gateway rejected the request data (amount/email/URL format)."
    if status == 429:
        return "Rate limited by the gateway. Slow down requests."
    if status >= 500:
        return "Gateway server error. Try again later or contact Emon Pay."
    return "Unexpected response from the gateway."

def _emonpay_alert_admin(key, text):
    """Send a diagnostic to the admin, at most once per 5 minutes per key."""
    if not _emonpay_throttled("alert_" + key):
        return
    try:
        bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        print(f"Payment admin alert failed: {e}")

def _fmt_amount(value):
    text = f"{float(value):.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text

def _bn_digits(text):
    return str(text).translate(str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯"))

def _emonpay_show(chat_id, message_id, text, markup=None):
    """Edit an existing message if possible, otherwise send a new one."""
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
            return message_id
        except Exception:
            pass
    try:
        sent = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        return sent.message_id
    except Exception as e:
        print(f"Payment show message error: {e}")
        return None

def get_custom_deposit_cancel_markup():
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data="dep_custom_cancel"))
    return markup

def parse_deposit_amount(raw_text):
    """Return a valid amount (10..10000) or None."""
    try:
        cleaned = str(raw_text or "").strip().replace(",", "")
        value = float(cleaned)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(value) or value < 10 or value > 10000:
        return None
    return round(value, 2)

# callback_data -> label shown to / stored for the user (all three go through Emon Pay)
_EMON_METHODS = {"dep_emon_bkash": "bKash", "dep_emon_nagad": "Nagad", "dep_emon_rocket": "Rocket", "dep_method_xpay": "bKash / Nagad / Rocket"}

def get_deposit_method_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        premium_inline_button("bKash", "bkash", callback_data="paymethod_bKash"),
        premium_inline_button("Nagad", "nagad", callback_data="paymethod_Nagad"),
        premium_inline_button("Rocket", "rocket", callback_data="paymethod_Rocket"),
        premium_inline_button("Binance", "binance", callback_data="dep_method_binance"),
        InlineKeyboardButton("🔙 Cancel", callback_data="dep_custom_cancel")
    )
    return markup

def show_deposit_method_choice(chat_id, user_id, amount, message_id=None):
    """After the amount is chosen/typed: show the 'select payment method' screen."""
    amount = round(float(amount), 2)
    user_temp_deposit[user_id] = {"step": "choose_method", "amount": amount}
    text = (
        "💳 <b>পেমেন্ট পদ্ধতি বেছে নিন:</b>\n\n"
        f"💰 <b>Deposit Amount:</b> <code>{_fmt_amount(amount)} BDT</code>"
    )
    return _emonpay_show(chat_id, message_id, text, get_deposit_method_markup())

# ----------------- Core: atomic, idempotent credit (shared by Emon Pay and Binance) -----------------
def _db_abort(conn):
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass

def _credit_invoice(ref, txn_id, gateway, mfs_trxid=None, emon_txn_id=None):
    """
    The ONLY place that adds deposit balance. Everything happens in one SQLite write transaction:
    invoice PENDING -> COMPLETED, deposit_history row, balance, referral bonus.
    The partial UNIQUE index on payment_invoices.transaction_id and the UNIQUE columns of deposit_history
    guarantee a transaction ID can never be credited twice, even with concurrent callers.
    Returns: credited | already | closed | missing | duplicate
    """
    txn_id = str(txn_id or "").strip()
    if not txn_id:
        return "missing"
    now = int(time.time())
    referral_notify = None
    conn = db_connect("shop_bot.db", timeout=30)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "SELECT user_id, expected_amount, status, gateway, payment_method, expected_usdt "
            "FROM payment_invoices WHERE invoice_ref = ?", (ref,)
        )
        row = cur.fetchone()
        if not row or row[3] != gateway:
            _db_abort(conn)
            return "missing"
        user_id, amount_text, status, _gw, method, usdt_text = row
        if status == "COMPLETED":
            _db_abort(conn)
            return "already"
        if status != "PENDING":
            _db_abort(conn)
            return "closed"
        amount = _dec(amount_text)
        if amount is None or amount <= 0:
            _db_abort(conn)
            return "missing"

        # Anti-duplication lock: only one caller can flip PENDING -> COMPLETED.
        cur.execute(
            "UPDATE payment_invoices SET status = 'COMPLETED', transaction_id = ?, completed_at = ? "
            "WHERE invoice_ref = ? AND status = 'PENDING'",
            (txn_id, now, ref)
        )
        if cur.rowcount != 1:
            _db_abort(conn)
            return "already"

        if mfs_trxid:
            # One MFS TrxID can be claimed exactly once, ever (PRIMARY KEY). A violation raises IntegrityError,
            # which rolls back the whole transaction (invoice status, history and balance) -> "duplicate".
            cur.execute(
                "INSERT INTO mfs_trxid_claims (normalized_mfs_trxid, invoice_ref, user_id, emonpay_transaction_id, claimed_at) VALUES (?, ?, ?, ?, ?)",
                (mfs_trxid, ref, user_id, emon_txn_id, now)
            )
            cur.execute(
                "UPDATE payment_invoices SET normalized_mfs_trxid = ?, emonpay_transaction_id = ?, verification_status = 'VERIFIED', verified_at = ? WHERE invoice_ref = ?",
                (mfs_trxid, emon_txn_id, now, ref)
            )

        cur.execute(
            "INSERT INTO deposit_history (invoice_ref, user_id, amount, payment_method, gateway, transaction_id, usdt_amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ref, user_id, str(_q2(amount)), method, gateway, txn_id, usdt_text, now)
        )

        cur.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,))
        user_row = cur.fetchone()
        if user_row:
            referred_by = user_row[0]
            cur.execute("UPDATE users SET balance = ROUND(COALESCE(balance, 0) + ?, 2) WHERE user_id = ?", (float(amount), user_id))
        else:
            referred_by = None
            cur.execute(
                "INSERT INTO users (user_id, first_name, balance, username) VALUES (?, ?, ?, 'N/A')",
                (user_id, "User", float(amount))
            )

        # Same referral deposit bonus logic as the manual approval flow (once per invoice).
        if referred_by:
            cur.execute("SELECT user_id FROM users WHERE user_id = ?", (referred_by,))
            if cur.fetchone():
                bonus = 0.02
                cur.execute(
                    "INSERT OR IGNORE INTO referral_deposit_bonus_ledger (deposit_request_id, referrer_id, referred_user_id, deposit_amount, bonus_amount, credited_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("pay_" + str(ref), referred_by, user_id, float(amount), bonus, datetime.now(timezone.utc).isoformat())
                )
                if cur.rowcount > 0:
                    cur.execute(
                        "UPDATE users SET balance = ROUND(COALESCE(balance, 0) + ?, 2), referral_deposit_bonus = COALESCE(referral_deposit_bonus, 0) + ? WHERE user_id = ?",
                        (bonus, bonus, referred_by)
                    )
                    referral_notify = referred_by
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        _db_abort(conn)
        return "duplicate"
    except Exception:
        _db_abort(conn)
        raise

    # ---- notifications (after the money is safely stored) ----
    try:
        if gateway == "BINANCE":
            usdt_shown = _fmt_dec(_dec(usdt_text) or Decimal("0"))
            text = (
                "<b>✅ Binance Payment Successful!</b>\n\n"
                "<b>Payment Method:</b> Binance\n"
                f"<b>Amount Received:</b> ${usdt_shown} USDT\n"
                f"<b>Conversion Rate:</b> 1 USDT = ৳{_fmt_dec(BINANCE_USDT_RATE)}\n"
                f"<b>Credited Balance:</b> ৳{_fmt_dec(amount)}\n"
                f"<b>Order ID:</b> <code>{html.escape(txn_id)}</code>\n"
                "<b>Status:</b> COMPLETED\n\n"
                "Your balance has been successfully credited."
            )
        else:
            text = (
                "<b>✅ Payment Successful!</b>\n\n"
                "<b>Payment Method:</b> EmonPay\n"
                f"<b>Amount:</b> ৳{_fmt_dec(amount)}\n"
                f"<b>Transaction ID:</b> <code>{html.escape(txn_id)}</code>\n"
                "<b>Status:</b> COMPLETED\n\n"
                "Your balance has been successfully credited."
            )
        bot.send_message(user_id, text, parse_mode="HTML", reply_markup=get_permanent_keyboard(user_id))
    except Exception as e:
        print(f"Payment success notification failed for {user_id}: {e}")
    if referral_notify:
        try:
            bot.send_message(
                referral_notify,
                "<tg-emoji emoji-id=\"6131890186767831988\">🎁</tg-emoji> <b>রেফারেল ডিপোজিট বোনাস!</b>\nআপনার রেফারেল থেকে একজন মেম্বার সফলভাবে ডিপোজিট করেছেন। আপনার মেইন ব্যালেন্সে <b>0.02 টাকা</b> যোগ হয়েছে এবং Referral Deposit Bonus-এ স্থায়ীভাবে জমা হয়েছে।",
                parse_mode="HTML"
            )
        except Exception:
            pass
    return "credited"

def _insert_invoice(ref, user_id, amount, method, gateway, expected_usdt=None, txn_id=None, txn_source=None):
    now = int(time.time())
    conn = db_connect("shop_bot.db", timeout=30)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payment_invoices (invoice_ref, user_id, expected_amount, payment_method, gateway, expected_usdt, "
        "transaction_id, txn_source, txn_set_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
        (ref, user_id, str(_q2(amount)), method, gateway, (str(expected_usdt) if expected_usdt is not None else None),
         txn_id, txn_source, (now if txn_id else None), now)
    )
    conn.commit()
    conn.close()

# ----------------- Emon Pay: invoice creation + strict verification -----------------
_EMONPAY_REF_RE = re.compile(r"^EP[0-9A-F]{16}$")
_EMONPAY_TXN_RE = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")

def create_emonpay_invoice(chat_id, user_id, full_name, amount, message_id=None, method_label="EmonPay"):
    """Create an Emon Pay payment link for the chosen amount and show it to the user."""
    amount_dec = _dec(amount)
    if amount_dec is None or amount_dec <= 0:
        return False
    amount_dec = _q2(amount_dec)
    amount_str = _fmt_dec(amount_dec)
    msg_id = _emonpay_show(chat_id, message_id, "⏳ Generating your secure payment link, please wait...")

    if not (EMONPAY_API_KEY and EMONPAY_BRAND_KEY):
        print("Emon Pay: API/Brand key is not configured.")
        _emonpay_alert_admin(
            "nokey",
            "⚠️ <b>Auto-payment is not configured.</b>\nSet <code>EMONPAY_API_KEY</code> / <code>EMONPAY_BRAND_KEY</code> and restart the bot."
        )
        _emonpay_show(chat_id, msg_id, "⚠️ <b>Payment link could not be created right now.</b>\n\nPlease try again in a moment.")
        return False

    ref = "EP" + uuid.uuid4().hex[:16].upper()
    customer_name = (str(full_name or "").strip() or "Telegram User")[:60]
    meta = {"ref": ref, "user_id": str(user_id), "method": method_label, "source": "telegram_bot"}
    payload = {
        "cus_name": customer_name,
        "cus_email": f"user_{user_id}@telegram.bot",
        "amount": amount_str,
        "success_url": BOT_RETURN_URL,
        "cancel_url": BOT_RETURN_URL,
        # The docs show both spellings, so both are sent.
        "meta_data": meta,
        "metadata": meta,
    }

    try:
        result = _emonpay_post(EMONPAY_CREATE_URL, payload, timeout=20)
        data = result["data"]
        payment_url = str(data.get("payment_url") or "").strip()
        ok = result["status"] is not None and 200 <= result["status"] < 300 and payment_url.startswith("http")
        if not ok:
            shown = f"HTTP {result['status']}" if result["status"] is not None else (result["error"] or "no response")
            print(f"Emon Pay create failed: {shown} | body: {_emonpay_clean(result['text'])}")
            _emonpay_alert_admin(
                "create_fail",
                "⚠️ <b>Emon Pay create-payment failed</b>\n"
                f"User: <code>{user_id}</code> | Amount: <code>{amount_str}</code>\n"
                f"<code>POST /create</code> → {html.escape(str(shown))}\n"
                f"Response: <code>{html.escape(_emonpay_clean(result['text'], 300) or str(result['error'] or '-'))}</code>\n\n"
                f"💡 {html.escape(_emonpay_hint(result))}\n\n"
                "Run /paytest for a full diagnostic."
            )
            if result["error"] == "timeout":
                _emonpay_show(chat_id, msg_id, "⚠️ <b>Payment server timed out.</b>\n\nPlease try again in a moment.")
            else:
                _emonpay_show(chat_id, msg_id, "⚠️ <b>Payment link could not be created right now.</b>\n\nPlease try again in a moment.")
            return False

        link_id, link_source = None, None
        # The gateway cannot call us back, so poll the verify API with the id in the payment link
        # (the dashboard auto-scan is the second, independent way to find the payment).
        candidate = payment_url.split("?")[0].rstrip("/").split("/")[-1]
        if _EMONPAY_TXN_RE.match(candidate):
            link_id, link_source = candidate, "LINK"
        _insert_invoice(ref, user_id, amount_dec, method_label, "EMONPAY", txn_id=link_id, txn_source=link_source)
        try:
            _emon_store_link_id(ref, payment_url)
        except Exception as e:
            print(f"Emon Pay link id store skipped: {_emonpay_clean(e, 120)}")

        invoice_text = (
            "<b>💳 Auto Deposit Invoice Created</b>\n\n"
            f"<b>💰 Deposit Amount:</b> <code>{amount_str} BDT</code>\n"
            f"<b>📲 Method:</b> {html.escape(method_label)}\n"
            f"<b>🧾 Invoice ID:</b> <code>{html.escape(ref)}</code>\n\n"
            "<b>📌 Payment Instructions:</b>\n"
            "1. Click the <b>Payment link</b> button below.\n"
            f"2. Select <b>{html.escape(method_label)}</b> and send the exact amount.\n"
            "3. Enter your <b>Transaction ID (TrxID)</b> and click <b>Verify</b> on the payment page — <b>only there, one time</b>.\n"
            "4. That's all! Your balance is added to this bot <b>automatically</b> within a few seconds. No TrxID needed in the bot, no screenshots.\n\n"
            "<b>⏳ Validity:</b> 30 Minutes"
        )
        pay_markup = InlineKeyboardMarkup()
        pay_markup.add(InlineKeyboardButton("Payment link", url=payment_url))
        _emonpay_show(chat_id, msg_id, invoice_text, pay_markup)
        # No TrxID prompt in the bot any more: the user verifies once on the payment page, the bot detects it by itself.
        return True
    except Exception as e:
        print(f"Emon Pay invoice creation error: {e}")
        _emonpay_show(chat_id, msg_id, "⚠️ <b>Something went wrong while creating your payment link.</b>\n\nPlease try again later.")
    return False

def _emon_reject(ref, reason, row, data):
    """Strict-verification failure: nothing is credited, the admin gets the evidence (secrets masked)."""
    print(f"Emon Pay verification REJECTED ({reason}) for invoice {ref}")
    _emonpay_alert_admin(
        f"emon_{reason}_{ref}",
        "⚠️ <b>Emon Pay payment NOT credited</b> (strict check failed)\n"
        f"Reason: <b>{html.escape(reason)}</b>\n"
        f"Invoice: <code>{html.escape(str(ref))}</code> | User: <code>{row[0]}</code> | Expected: <code>{html.escape(str(row[1]))}</code>\n"
        f"Gateway said: <code>{html.escape(_emonpay_clean(json.dumps(data, default=str), 350))}</code>\n\n"
        "The invoice stays PENDING. If this payment is genuine, review it manually."
    )

def emonpay_process_transaction(transaction_id, ref=None, source=None):
    """
    Verify a gateway transaction server-side and credit the user only if EVERY strict condition holds:
    verify API succeeded, status COMPLETED, transaction_id matches, amount equals the invoice (Decimal),
    invoice still PENDING, user taken from the DB, TrxID never used before.
    Returns: completed | pending | failed | invalid | unknown | mismatch | amount_mismatch | duplicate | expired | error
    """
    transaction_id = str(transaction_id or "").strip()
    if not _EMONPAY_TXN_RE.match(transaction_id):
        return "invalid"
    if ref is not None and not _EMONPAY_REF_RE.match(str(ref)):
        return "invalid"

    vres = _emonpay_post(EMONPAY_VERIFY_URL, {"transaction_id": transaction_id}, timeout=15)
    if vres["status"] is None or vres["status"] >= 400:
        shown = f"HTTP {vres['status']}" if vres["status"] is not None else (vres["error"] or "no response")
        if _emonpay_throttled("verify_" + str(shown)):
            print(f"Emon Pay verify problem ({shown}): {_emonpay_clean(vres['text'], 200)}")
        if vres["status"] in (401, 403):
            _emonpay_alert_admin(
                "verify_auth",
                f"⚠️ <b>Emon Pay verify is being denied</b> ({html.escape(str(shown))}).\n"
                f"💡 {html.escape(_emonpay_hint(vres))}\n"
                "Pending deposits cannot be auto-credited until this is fixed."
            )
        return "error"

    data = vres["data"]
    if not data:
        return "error"  # not a JSON object: invalid response, never credit

    meta = data.get("metadata") or data.get("meta_data") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    meta_ref = str(meta.get("ref") or "") if isinstance(meta, dict) else ""
    if meta_ref and ref and meta_ref != ref:
        return "mismatch"
    ref = ref or meta_ref
    if not ref or not _EMONPAY_REF_RE.match(ref):
        return "unknown"

    conn = db_connect("shop_bot.db", timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT user_id, expected_amount, status, gateway FROM payment_invoices WHERE invoice_ref = ?", (ref,))
    row = cur.fetchone()
    conn.close()
    if not row or row[3] != "EMONPAY":
        return "unknown"
    p_user_id, expected_text, p_status, _gw = row
    if p_status == "COMPLETED":
        return "completed"
    if p_status != "PENDING":
        return "expired"

    gw_status = str(data.get("status", "")).strip().upper()
    if gw_status != "COMPLETED":
        # PENDING / ERROR / FALSE / anything else: never credit. Remember the TrxID so the worker re-checks it.
        conn = db_connect("shop_bot.db", timeout=30)
        cur = conn.cursor()
        if source:
            cur.execute(
                "UPDATE payment_invoices SET transaction_id = ?, txn_source = ?, txn_set_at = ? WHERE invoice_ref = ? AND status = 'PENDING'",
                (transaction_id, source, int(time.time()), ref)
            )
            conn.commit()
        conn.close()
        return "failed" if gw_status in ("ERROR", "FALSE", "FAILED", "CANCELLED", "CANCELED") else "pending"

    # Gateway says COMPLETED: now the strict checks.
    verified_txn = str(data.get("transaction_id") or "").strip()
    if verified_txn.lower() != transaction_id.lower():
        _emon_reject(ref, "transaction_id_mismatch", row, data)
        return "mismatch"
    expected_amount = _dec(expected_text)
    verified_amount = _dec(data.get("amount"))
    if expected_amount is None or verified_amount is None or verified_amount != expected_amount:
        _emon_reject(ref, "amount_mismatch", row, data)
        return "amount_mismatch"

    state = _credit_invoice(ref, verified_txn, "EMONPAY")
    if state == "credited":
        _set_paid_method(ref, _emon_guess_method(data))
    if state in ("credited", "already"):
        return "completed"
    if state == "duplicate":
        _emon_reject(ref, "transaction_id_already_used", row, data)
        return "duplicate"
    return "expired" if state == "closed" else "unknown"

# ----------------- EmonPay Dashboard login + MFS TrxID auto verification -----------------
_EMON_TRXID_RE = re.compile(r"\b(?:TrxID|TxnID|Trx\s*ID|Txn\s*ID)\s*[:\-]?\s*([A-Z0-9]+)", re.IGNORECASE)
_EMON_USER_TRXID_RE = re.compile(r"^[A-Z0-9]{6,20}$")
_EMON_PROVIDERS = ("bkash", "nagad", "rocket")

def extract_trxid(message):
    """Original MFS TrxID found inside the dashboard 'Transaction Message' (uppercase) or None."""
    if not message:
        return None
    match = _EMON_TRXID_RE.search(message)
    return match.group(1).upper() if match else None

def normalize_user_trxid(raw):
    """Trim + uppercase. Returns the normalized TrxID or None if it is not a plausible MFS TrxID."""
    value = str(raw or "").strip().upper()
    return value if _EMON_USER_TRXID_RE.match(value) else None

def _emon_looks_like_trxid(text):
    """Strict shape used ONLY for the restart-fallback: 8-12 chars, at least one letter AND one digit."""
    value = str(text or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9]{8,12}", value)) and any(c.isdigit() for c in value) and any(c.isalpha() for c in value)

def emon_dashboard_problem():
    """Why dashboard verification cannot run (None if it can)."""
    if BeautifulSoup is None:
        return "Python package beautifulsoup4 is not installed on the server (run: pip install beautifulsoup4)"
    if (not EMONPAY_LOGIN_EMAIL or not EMONPAY_LOGIN_PASSWORD
            or EMONPAY_LOGIN_EMAIL.startswith("PUT_YOUR_") or EMONPAY_LOGIN_PASSWORD.startswith("PUT_YOUR_")):
        return "EMONPAY_LOGIN_EMAIL / EMONPAY_LOGIN_PASSWORD are not filled in (bot.py lines 104-105)"
    return None

def emon_dashboard_enabled():
    placeholder = EMONPAY_LOGIN_EMAIL.startswith("PUT_YOUR_") or EMONPAY_LOGIN_PASSWORD.startswith("PUT_YOUR_")
    return bool(EMONPAY_LOGIN_EMAIL and EMONPAY_LOGIN_PASSWORD and not placeholder and BeautifulSoup is not None)

class EmonDashError(Exception):
    """Dashboard unreachable / login refused / extra verification required. Message never contains secrets."""

def _emon_text(node):
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node is not None else ""

def parse_login_form_token(html_text):
    """Fresh CSRF token from the real sign-in form. Returns (token, captcha_present)."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    captcha = bool(soup.select(".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], input[name*=captcha i], iframe[src*=captcha i]"))
    token = None
    form = None
    for candidate in soup.find_all("form"):
        if candidate.find("input", {"name": "password"}):
            form = candidate
            break
    scope = form if form is not None else soup
    field = scope.find("input", {"name": "token"})
    if field is not None and field.get("value"):
        token = field.get("value").strip()
    return token, captcha

def parse_transaction_links(html_text, base_url=EMONPAY_DASH_BASE):
    """Detail links taken from the real list HTML. Only links on the dashboard host are accepted."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    host = urlparse(EMONPAY_DASH_BASE).netloc
    seen, links = set(), []
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"].strip())
        parsed = urlparse(full)
        if parsed.netloc != host or "/user/view-transaction/transactions/" not in parsed.path:
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean not in seen:
            seen.add(clean)
            links.append(clean)
    return links

def parse_next_page(html_text, current_url):
    """URL of the next list page if the pagination offers one, else None."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    host = urlparse(EMONPAY_DASH_BASE).netloc
    nxt = soup.find("a", attrs={"rel": re.compile("next", re.I)}, href=True)
    if nxt is None:
        for a in soup.select("ul.pagination a[href], .pagination a[href]"):
            label = _emon_text(a).lower()
            if label in ("next", "›", "»", ">", "next »", "next ›") or "next" in (a.get("aria-label") or "").lower():
                nxt = a
                break
    if nxt is None:
        return None
    full = urljoin(current_url, nxt["href"].strip())
    if urlparse(full).netloc != host or "page=" not in full or full == current_url:
        return None
    return full

_EMON_LABELS = {
    "transaction id": "emon_txn_id", "customer name": "customer_name", "customer email": "customer_email",
    "amount": "amount", "status": "status", "transaction message": "message", "message": "message",
    "payment method": "method", "method": "method", "gateway": "method",
}

def _emon_collect_strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _emon_collect_strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _emon_collect_strings(v, out)

def _emon_parse_label_rows(html_text, out):
    soup = BeautifulSoup(html_text or "", "html.parser")
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        key = _EMON_LABELS.get(_emon_text(cells[0]).rstrip(":").strip().lower())
        if key and key not in out:
            out[key] = _emon_text(cells[1])
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        key = _EMON_LABELS.get(_emon_text(dt).rstrip(":").strip().lower())
        if key and dd is not None and key not in out:
            out[key] = _emon_text(dd)
    return _emon_text(soup)

def parse_transaction_details(raw_text):
    """
    Read one transaction details response. Accepts a full HTML page, an HTML fragment (modal content)
    or JSON that wraps HTML. Label/value rows are used first; if the Transaction Message row is not found,
    the TrxID / email / status are searched in the visible text of this single details response.
    """
    raw_text = raw_text or ""
    pieces = [raw_text]
    stripped = raw_text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            strings = []
            _emon_collect_strings(json.loads(stripped), strings)
            pieces = [html.unescape(x) for x in strings if x]
        except Exception:
            pass
    out, visible = {}, []
    for piece in pieces:
        visible.append(_emon_parse_label_rows(piece, out))
    blob = " ".join(visible)
    if "amount" in out:
        out["amount_dec"] = _dec(re.sub(r"[^0-9.\-]", "", out["amount"].replace(",", "")))
    trx = extract_trxid(out.get("message"))
    if trx is None:                      # fallback: message not in a labelled row
        trx = extract_trxid(blob)
        if trx and "message" not in out:
            out["message"] = blob
    out["mfs_trxid"] = trx
    if "customer_email" not in out:
        m = re.search(r"user_\d+@telegram\.bot", blob, re.I)
        if m:
            out["customer_email"] = m.group(0)
    if "status" not in out:
        m = re.search(r"\bStatus\s*[:\-]?\s*(Success|Successful|Completed|Pending|Initiated|Failed|Cancelled|Canceled|Refunded)\b", blob, re.I)
        if m:
            out["status"] = m.group(1)
    if "amount_dec" not in out:
        m = re.search(r"\bAmount\s*[:\-]?\s*([0-9][0-9,]*\.?[0-9]*)", blob, re.I)
        if m:
            out["amount_dec"] = _dec(m.group(1).replace(",", ""))
    return out

def parse_mfs_time(message):
    """'... at 12/11/2026 11:53' (dd/mm/yyyy, Bangladesh time) -> unix seconds or None."""
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})", message or "")
    if not match:
        return None
    try:
        dd, mm, yy, hh, mi = (int(x) for x in match.groups())
        return int(datetime(yy, mm, dd, hh, mi, tzinfo=timezone(timedelta(hours=6))).timestamp())
    except ValueError:
        return None

class EmonDashboard:
    """One persistent, authenticated dashboard session kept in memory (never written to disk or logs)."""
    def __init__(self):
        self._lock = threading.RLock()
        self._session = None
        self._last_request = 0.0
        self._login_failures = 0
        self._blocked_until = 0.0
        self._details = {}      # url -> (timestamp, details)
        self._by_trx = {}       # normalized MFS TrxID -> set(url)

    # ---- session ----
    def _new_session(self):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; AllServicesBot/1.0; +https://t.me/" + BOT_USERNAME + ")",
                          "Accept-Language": "en"})
        return s

    def _pace(self):
        wait = EMONPAY_DASH_MIN_INTERVAL - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    @staticmethod
    def _is_login_page(resp):
        if resp.status_code in (401, 419):
            return True
        if "/sign-in" in urlparse(resp.url).path:
            return True
        return False

    def _login(self):
        """Fresh GET /sign-in -> fresh CSRF token -> POST. Raises EmonDashError (safe message) on any problem."""
        if not (EMONPAY_LOGIN_EMAIL and EMONPAY_LOGIN_PASSWORD):
            raise EmonDashError("login credentials not configured")
        now = time.time()
        if now < self._blocked_until:
            raise EmonDashError("login temporarily blocked after failures")
        self._session = self._new_session()
        try:
            page = self._session.get(EMONPAY_DASH_BASE + "/sign-in", timeout=EMONPAY_DASH_TIMEOUT)
            token, captcha = parse_login_form_token(page.text)
            if captcha:
                raise EmonDashError("captcha / extra verification required on login page (not bypassed)")
            if page.status_code != 200 or not token:
                raise EmonDashError(f"could not read login form (HTTP {page.status_code})")
            self._pace()
            resp = self._session.post(
                EMONPAY_DASH_BASE + "/sign-in",
                data={"token": token, "email": EMONPAY_LOGIN_EMAIL, "password": EMONPAY_LOGIN_PASSWORD, "remember": "on"},
                headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*",
                         "Referer": EMONPAY_DASH_BASE + "/sign-in", "Origin": EMONPAY_DASH_BASE},
                timeout=EMONPAY_DASH_TIMEOUT,
            )
            try:
                body = resp.json()
            except Exception:
                body = {}
            msg = str(body.get("message", "")) if isinstance(body, dict) else ""
            if re.search(r"otp|2fa|two[- ]factor|verification code|captcha", msg, re.I):
                raise EmonDashError("additional verification required by EmonPay (not bypassed)")
            if not isinstance(body, dict) or str(body.get("status", "")).lower() != "success":
                raise EmonDashError(f"login refused (HTTP {resp.status_code})")
            # Prove the session really works by loading the transaction list.
            self._pace()
            check = self._session.get(EMONPAY_DASH_BASE + "/user/transactions?status=all", timeout=EMONPAY_DASH_TIMEOUT)
            if check.status_code != 200 or self._is_login_page(check):
                raise EmonDashError("session verification failed after login")
            self._login_failures = 0
            print("EmonPay dashboard: login success")
        except EmonDashError as e:
            self._session = None
            self._login_failures += 1
            self._blocked_until = time.time() + min(600, 30 * (2 ** min(self._login_failures - 1, 5)))
            print(f"EmonPay dashboard: login failure ({_emonpay_clean(e, 160)})")
            raise
        except requests.RequestException as e:
            self._session = None
            self._login_failures += 1
            self._blocked_until = time.time() + min(600, 30 * (2 ** min(self._login_failures - 1, 5)))
            print(f"EmonPay dashboard: login failure (network: {type(e).__name__})")
            raise EmonDashError("dashboard unreachable")

    def _get(self, url, ajax=False):
        """Authenticated GET with pacing, timeout and ONE safe re-login if the session expired."""
        extra = {"X-Requested-With": "XMLHttpRequest", "Accept": "text/html, application/json, */*;q=0.8",
                 "Referer": EMONPAY_DASH_BASE + "/user/transactions?status=all"} if ajax else {}
        with self._lock:
            for attempt in (1, 2):
                if self._session is None:
                    self._login()
                self._pace()
                try:
                    resp = self._session.get(url, timeout=EMONPAY_DASH_TIMEOUT, headers=extra) if extra else self._session.get(url, timeout=EMONPAY_DASH_TIMEOUT)
                except requests.RequestException as e:
                    print(f"EmonPay dashboard: request error ({type(e).__name__})")
                    if attempt == 2:
                        raise EmonDashError("dashboard unreachable")
                    time.sleep(1)
                    continue
                if self._is_login_page(resp):
                    print("EmonPay dashboard: session expired, re-login")
                    self._session = None
                    if attempt == 2:
                        raise EmonDashError("session could not be renewed")
                    continue
                if resp.status_code == 429:
                    raise EmonDashError("dashboard rate limited us")
                if resp.status_code != 200:
                    raise EmonDashError(f"unexpected HTTP {resp.status_code}")
                return resp
            raise EmonDashError("dashboard request failed")

    # ---- data ----
    def _remember(self, url, details):
        self._details[url] = (time.time(), details)
        trx = details.get("mfs_trxid")
        if trx:
            self._by_trx.setdefault(trx, set()).add(url)

    def _fresh_cached(self, url):
        item = self._details.get(url)
        if not item:
            return None
        ttl = EMONPAY_DASH_SUCCESS_TTL if str(item[1].get("status", "")).strip().lower() in _EMON_OK_STATUSES else EMONPAY_DASH_OTHER_TTL
        return item[1] if time.time() - item[0] <= ttl else None

    def _fetch_details(self, url):
        cached = self._fresh_cached(url)
        if cached is not None:
            return cached
        resp = self._get(url, ajax=True)
        details = parse_transaction_details(resp.text)
        if not details.get("mfs_trxid"):
            print("EmonPay dashboard: detail page parsed WITHOUT a TrxID "
                  f"(fields={sorted(k for k in details if k not in ('amount_dec', 'mfs_trxid'))}, "
                  f"length={len(resp.text or '')}, type={str((getattr(resp, 'headers', {}) or {}).get('Content-Type', '?'))[:40]})")
        details["url"] = url
        self._remember(url, details)
        return details

    def find_transactions(self, trxid):
        """All dashboard transactions whose Transaction Message holds exactly this MFS TrxID (may be empty)."""
        with self._lock:
            found = []
            for url in list(self._by_trx.get(trxid, ())):
                details = self._fetch_details(url)
                if details.get("mfs_trxid") == trxid:
                    found.append(details)
            if found:
                print("EmonPay dashboard: TrxID found in cached index")
                return found
            fetched, page_url, pages = 0, EMONPAY_DASH_BASE + "/user/transactions?status=all", 0
            visited = set()
            while page_url and page_url not in visited and pages < EMONPAY_DASH_MAX_PAGES:
                visited.add(page_url)
                pages += 1
                resp = self._get(page_url)
                links = parse_transaction_links(resp.text, page_url)
                print(f"EmonPay dashboard: transaction list page {pages} retrieved ({len(links)} links)")
                for url in links:
                    if self._fresh_cached(url) is None:
                        if fetched >= EMONPAY_DASH_MAX_DETAIL_FETCH:
                            continue
                        fetched += 1
                        self._fetch_details(url)
                        print("EmonPay dashboard: transaction detail retrieved")
                    hit = self._details[url][1]
                    if hit.get("mfs_trxid") == trxid:
                        found.append(hit)
                if found:
                    break
                page_url = parse_next_page(resp.text, page_url)
            return found

    def recent_transactions(self, pages=2):
        """Details of the newest dashboard transactions (cached details are reused, so repeated scans are cheap)."""
        with self._lock:
            out, fetched, n, visited = [], 0, 0, set()
            page_url = EMONPAY_DASH_BASE + "/user/transactions?status=all"
            while page_url and page_url not in visited and n < pages:
                visited.add(page_url)
                n += 1
                resp = self._get(page_url)
                for url in parse_transaction_links(resp.text, page_url):
                    if self._fresh_cached(url) is None:
                        if fetched >= EMONPAY_DASH_MAX_DETAIL_FETCH:
                            continue
                        fetched += 1
                        self._fetch_details(url)
                    item = self._details.get(url)
                    if item:
                        out.append(item[1])
                page_url = parse_next_page(resp.text, page_url)
            return out

_EMON_OK_STATUSES = ("success", "successful", "completed", "complete", "paid")
_EMON_PENDING_STATUSES = ("pending", "processing", "initiated", "waiting", "unpaid", "on hold")
_emon_dashboard = EmonDashboard()
_EMON_USER_LAST = {}
_EMON_INFLIGHT = set()
_EMON_ATTEMPTS = {}
_EMON_STATE_LOCK = threading.Lock()

def _emon_store_link_id(ref, payment_url):
    """Remember the id embedded in the payment link so a dashboard payment can be tied to ITS invoice."""
    candidate = str(payment_url or "").split("?")[0].rstrip("/").split("/")[-1]
    if not _EMONPAY_TXN_RE.match(candidate):
        return
    conn = db_connect("shop_bot.db", timeout=30)
    try:
        conn.execute("UPDATE payment_invoices SET emon_link_id = ? WHERE invoice_ref = ?", (candidate, ref))
        conn.commit()
    finally:
        conn.close()

def emon_find_open_invoice(user_id):
    """Latest open EmonPay invoice of this user (survives restarts). Dashboard verification is skipped if disabled."""
    if not emon_dashboard_enabled():
        return None
    conn = db_connect("shop_bot.db", timeout=30)
    try:
        row = conn.execute(
            "SELECT invoice_ref FROM payment_invoices WHERE user_id = ? AND gateway = 'EMONPAY' AND status = 'PENDING' "
            "AND normalized_mfs_trxid IS NULL AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (user_id, int(time.time()) - PAYMENT_INVOICE_TTL)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None

EMONPAY_TRXID_PROMPT_DELAY = 0  # 0 = ask for the TrxID immediately with the payment link (set e.g. 10 for a delay)

def emon_dashboard_prompt_trxid(chat_id, user_id, ref, method_label):
    """Step 6 of the deposit flow: ask for the TrxID together with the payment link."""
    user_temp_deposit[user_id] = {"step": "waiting_emon_trx", "invoice_ref": ref, "method": method_label}

    def _send_later():
        try:
            if EMONPAY_TRXID_PROMPT_DELAY > 0:
                time.sleep(EMONPAY_TRXID_PROMPT_DELAY)
            state = user_temp_deposit.get(user_id)
            if not state or state.get("step") != "waiting_emon_trx" or state.get("invoice_ref") != ref:
                return  # user cancelled / moved on / already submitted
            bot.send_message(chat_id, "Please enter your bKash/Nagad/Rocket Transaction ID (TrxID).",
                             reply_markup=get_custom_deposit_cancel_markup())
        except Exception as e:
            print(f"Emon dashboard prompt error: {_emonpay_clean(e, 120)}")
    if EMONPAY_TRXID_PROMPT_DELAY > 0:
        threading.Thread(target=_send_later, name="emon-trx-prompt", daemon=True).start()
    else:
        _send_later()

_EMON_DASH_MESSAGES = {
    "success": "✅ <b>Payment verified successfully!</b> Your balance has been credited.",
    "completed": "✅ <b>Payment verified successfully!</b> Your balance has been credited.",
    "not_found": "Transaction ID not found. Please check your TrxID and try again.",
    "amount_mismatch": "Payment amount does not match your pending deposit.",
    "used": "This Transaction ID has already been used.",
    "pending": "Your payment is not successful yet. Please try again later.",
    "failed": "This transaction was not successful (failed, cancelled or refunded) and cannot be credited.",
    "unavailable": "Payment verification is temporarily unavailable. Please contact support or try again later.",
    "not_associated": "This payment could not be linked to your pending deposit. Please contact support.",
    "stale": "This transaction was made before your deposit request, so it cannot be used for it.",
    "method_mismatch": "This payment was made with a different payment method than your pending deposit.",
    "invalid": "Invalid Transaction ID. Please send only your TrxID (letters and numbers).",
    "expired": "This deposit request is no longer active. Please start a new deposit.",
    "unknown": "This deposit request is no longer active. Please start a new deposit.",
    "too_many": "Too many failed attempts for this deposit. Please contact support.",
}
_EMON_TERMINAL = {"success", "completed", "used", "expired", "unknown", "too_many"}

def _emon_evaluate(details, ref, user_id, expected_amount, method_label, created_at):
    """Strict checks on one dashboard transaction. Returns 'ok' or a failure code. Nothing is credited here."""
    status = str(details.get("status", "")).strip().lower()
    if status not in _EMON_OK_STATUSES:
        return "pending" if status in _EMON_PENDING_STATUSES or not status else "failed"
    amount = details.get("amount_dec")
    if amount is None or expected_amount is None or _q2(amount) != _q2(expected_amount):
        print("EmonPay dashboard: amount mismatch")
        return "amount_mismatch"
    if str(details.get("customer_email", "")).strip().lower() != f"user_{user_id}@telegram.bot":
        print("EmonPay dashboard: customer email does not match this Telegram user")
        return "not_associated"
    wanted = str(method_label or "").lower()
    got = str(details.get("method", "")).lower()
    asked = [p for p in _EMON_PROVIDERS if p in wanted]
    if got and len(asked) == 1 and any(p in got for p in _EMON_PROVIDERS) and asked[0] not in got:
        return "method_mismatch"
    paid_at = parse_mfs_time(details.get("message"))
    if paid_at is not None and paid_at < int(created_at or 0) - EMONPAY_DASH_STALE_GRACE:
        print("EmonPay dashboard: transaction is older than the invoice")
        return "stale"
    if paid_at is None:
        print("EmonPay dashboard: MFS time not parseable, relying on email/amount/unique-TrxID checks")
    return "ok"

def _emon_credit_from_details(ref, chosen, norm, link_id=None):
    """
    Shared credit step (typed-TrxID path and automatic scan). `chosen` already passed _emon_evaluate().
    Returns: success | completed | used | not_associated | expired | unknown | unavailable | not_found
    """
    emon_id = str(chosen.get("emon_txn_id") or "").strip()
    if not _EMONPAY_TXN_RE.match(emon_id):
        emon_id = ""
    credit_txn = emon_id or norm
    if not credit_txn:
        return "not_found"
    conn = db_connect("shop_bot.db", timeout=30)
    try:
        others = conn.execute(
            "SELECT status FROM payment_invoices WHERE invoice_ref <> ? AND (emon_link_id = ? OR transaction_id = ? OR emonpay_transaction_id = ?)",
            (ref, credit_txn, credit_txn, credit_txn)).fetchall()
        history = conn.execute("SELECT 1 FROM deposit_history WHERE transaction_id = ?", (credit_txn,)).fetchone()
    finally:
        conn.close()
    if history or any(r[0] == "COMPLETED" for r in others):
        print("EmonPay dashboard: duplicate rejected (payment already credited elsewhere)")
        return "used"
    if others:
        print("EmonPay dashboard: payment belongs to a different invoice")
        return "not_associated"
    if emon_id and link_id and emon_id == link_id:
        print("EmonPay dashboard: dashboard transaction id matches this invoice's payment link")

    try:
        state = _credit_invoice(ref, credit_txn, "EMONPAY", mfs_trxid=norm, emon_txn_id=emon_id or None)
    except Exception as e:
        print(f"EmonPay dashboard: credit error ({type(e).__name__})")
        return "unavailable"
    print(f"EmonPay dashboard: balance credit result = {state}")
    if state == "credited":
        _set_paid_method(ref, _emon_guess_method(chosen))
        return "success"
    if state == "already":
        return "completed"
    if state == "duplicate":
        print("EmonPay dashboard: duplicate TrxID rejection")
        return "used"
    return "expired" if state == "closed" else "unknown"

def emon_dashboard_verify(user_id, ref, raw_trxid):
    """
    Full verification + credit for one user-submitted MFS TrxID. Blocking: run it in a worker thread.
    Returns: success | completed | not_found | amount_mismatch | used | pending | failed | unavailable |
             not_associated | stale | method_mismatch | invalid | expired | unknown
    """
    if not emon_dashboard_enabled():
        return "unavailable"
    norm = normalize_user_trxid(raw_trxid)
    if not norm or not ref or not _EMONPAY_REF_RE.match(str(ref)):
        return "invalid"

    conn = db_connect("shop_bot.db", timeout=30)
    try:
        inv = conn.execute(
            "SELECT user_id, expected_amount, status, gateway, payment_method, created_at, normalized_mfs_trxid, emon_link_id "
            "FROM payment_invoices WHERE invoice_ref = ?", (ref,)).fetchone()
        claim = conn.execute("SELECT invoice_ref FROM mfs_trxid_claims WHERE normalized_mfs_trxid = ?", (norm,)).fetchone()
    finally:
        conn.close()
    if not inv or inv[0] != user_id or inv[3] != "EMONPAY":
        return "unknown"
    _uid, expected_text, status, _gw, method_label, created_at, done_trx, link_id = inv
    if claim:  # this TrxID was already credited
        return "completed" if claim[0] == ref else "used"
    if status == "COMPLETED":
        return "completed" if done_trx == norm else "expired"
    if status != "PENDING":
        return "expired"
    expected_amount = _dec(expected_text)

    try:
        candidates = _emon_dashboard.find_transactions(norm)
    except EmonDashError as e:
        print(f"EmonPay dashboard: verification unavailable ({_emonpay_clean(e, 160)})")
        _emonpay_alert_admin("dash_unavailable",
                             "⚠️ <b>EmonPay dashboard verification unavailable</b>\n"
                             f"Reason: <code>{html.escape(_emonpay_clean(e, 160))}</code>\n"
                             "Check EMONPAY_LOGIN_EMAIL / EMONPAY_LOGIN_PASSWORD. If the login page now asks for CAPTCHA/OTP, this cannot work automatically.")
        return "unavailable"
    except Exception as e:
        print(f"EmonPay dashboard: unexpected error ({type(e).__name__})")
        return "unavailable"
    if not candidates:
        print("EmonPay dashboard: verification result = TrxID not found")
        return "not_found"

    candidates.sort(key=lambda d: 0 if str(d.get("status", "")).strip().lower() in _EMON_OK_STATUSES else 1)
    chosen, first_code = None, None
    for details in candidates:
        code = _emon_evaluate(details, ref, user_id, expected_amount, method_label, created_at)
        if first_code is None:
            first_code = code
        if code == "ok":
            chosen = details
            break
    if chosen is None:
        print(f"EmonPay dashboard: verification result = {first_code}")
        return first_code

    return _emon_credit_from_details(ref, chosen, norm, link_id)

_EMON_AUTOSCAN_SKIP = set()   # (invoice_ref, dashboard url) pairs that can never match -> not re-evaluated every scan

def emon_dashboard_autoscan_once():
    """
    The user verifies ONCE on the EmonPay payment page. Here the bot finds that finished payment in the dashboard
    by itself and credits it, with the same strict rules as the typed-TrxID path:
    Success status + customer email user_<id>@telegram.bot + exact amount + not older than the invoice
    + payment method + the transaction was never credited before (UNIQUE ids in the DB).
    """
    if not emon_dashboard_enabled():
        return
    now = int(time.time())
    conn = db_connect("shop_bot.db", timeout=30)
    try:
        invoices = conn.execute(
            "SELECT invoice_ref, user_id, expected_amount, payment_method, created_at, emon_link_id FROM payment_invoices "
            "WHERE gateway = 'EMONPAY' AND status = 'PENDING' AND normalized_mfs_trxid IS NULL AND created_at >= ? "
            "ORDER BY created_at ASC", (now - EMONPAY_AUTOSCAN_WINDOW,)).fetchall()
    finally:
        conn.close()
    if not invoices:
        return
    try:
        txns = _emon_dashboard.recent_transactions(EMONPAY_AUTOSCAN_PAGES)
    except EmonDashError as e:
        if _emonpay_throttled("autoscan_err", 120):
            print(f"EmonPay autoscan: dashboard unavailable ({_emonpay_clean(e, 160)})")
        return
    except Exception as e:
        print(f"EmonPay autoscan: unexpected error ({type(e).__name__})")
        return
    paid = [d for d in txns if str(d.get("status", "")).strip().lower() in _EMON_OK_STATUSES]
    paid.sort(key=lambda d: parse_mfs_time(d.get("message")) or 0)   # oldest payment first -> oldest invoice
    for ref, user_id, amount_text, method, created_at, link_id in invoices:
        expected = _dec(amount_text)
        email = f"user_{user_id}@telegram.bot"
        mine = [d for d in paid
                if str(d.get("customer_email", "")).strip().lower() == email
                and d.get("amount_dec") is not None and expected is not None
                and _q2(d["amount_dec"]) == _q2(expected)
                and (ref, d.get("url")) not in _EMON_AUTOSCAN_SKIP]
        # exact binding first: a dashboard transaction whose id equals this invoice's payment-link id
        mine.sort(key=lambda d: 0 if (link_id and str(d.get("emon_txn_id", "")) == link_id) else 1)
        for d in mine:
            code = _emon_evaluate(d, ref, user_id, expected, method, created_at)
            if code != "ok":
                _EMON_AUTOSCAN_SKIP.add((ref, d.get("url")))
                continue
            try:
                result = _emon_credit_from_details(ref, d, d.get("mfs_trxid"), link_id)
            except Exception as e:
                print(f"EmonPay autoscan: credit error ({type(e).__name__})")
                break
            print(f"EmonPay autoscan: invoice result = {result}")
            if result in ("success", "completed", "expired", "unknown"):
                break
            if result in ("used", "not_associated"):
                _EMON_AUTOSCAN_SKIP.add((ref, d.get("url")))
                continue
            break

def emon_dashboard_autoscan_loop():
    while True:
        try:
            emon_dashboard_autoscan_once()
        except Exception as e:
            print(f"EmonPay autoscan loop error: {type(e).__name__}")
        time.sleep(EMONPAY_AUTOSCAN_INTERVAL)

def _emon_dashboard_worker(user_id, ref, raw_trxid, status_msg):
    try:
        outcome = emon_dashboard_verify(user_id, ref, raw_trxid)
        if outcome not in _EMON_TERMINAL and outcome != "unavailable":
            with _EMON_STATE_LOCK:
                _EMON_ATTEMPTS[ref] = _EMON_ATTEMPTS.get(ref, 0) + 1
                if _EMON_ATTEMPTS[ref] >= EMONPAY_DASH_MAX_ATTEMPTS:
                    outcome = "too_many"
        text = _EMON_DASH_MESSAGES.get(outcome, _EMON_DASH_MESSAGES["unavailable"])
        if outcome in _EMON_TERMINAL:
            user_temp_deposit.pop(user_id, None)
        _emonpay_show(user_id, status_msg, text)
    except Exception as e:
        print(f"EmonPay dashboard worker error: {type(e).__name__}")
        _emonpay_show(user_id, status_msg, _EMON_DASH_MESSAGES["unavailable"])
    finally:
        with _EMON_STATE_LOCK:
            _EMON_INFLIGHT.discard(user_id)

def emon_dashboard_handle_user_txn(user_id, ref, raw_trxid):
    """Entry point for a TrxID typed by the user. Never blocks Telegram polling."""
    if not emon_dashboard_enabled():
        bot.send_message(user_id, _EMON_DASH_MESSAGES["unavailable"], parse_mode="HTML")
        _emonpay_alert_admin("dash_not_configured",
                             "⚠️ <b>EmonPay dashboard verification cannot run</b>\nReason: <code>"
                             + html.escape(emon_dashboard_problem() or "unknown") + "</code>")
        return
    now = time.time()
    with _EMON_STATE_LOCK:
        if user_id in _EMON_INFLIGHT:
            busy = True
        elif now - _EMON_USER_LAST.get(user_id, 0) < EMONPAY_DASH_USER_COOLDOWN:
            busy = True
        else:
            busy = False
            _EMON_INFLIGHT.add(user_id)
            _EMON_USER_LAST[user_id] = now
    if busy:
        bot.send_message(user_id, "⏳ Please wait a few seconds, your previous request is still being processed.", parse_mode="HTML")
        return
    status_msg = _emonpay_show(user_id, None, "⏳ <b>Verifying your payment...</b>")
    threading.Thread(target=_emon_dashboard_worker, args=(user_id, ref, raw_trxid, status_msg),
                     name="emon-dash-verify", daemon=True).start()


@bot.message_handler(commands=['paytest'])
def payment_test_command(message):
    """Admin-only diagnostic: shows the real HTTP status/response of both gateways from THIS server."""
    if message.from_user.id != ADMIN_ID:
        return
    lines = ["🔧 <b>Payment diagnostic</b>\n", "<b>— Emon Pay —</b>"]
    lines.append(f"🔑 API key: set ({len(EMONPAY_API_KEY)} chars)" if EMONPAY_API_KEY else "🔑 API key: <b>NOT SET</b>")
    lines.append(f"🏷 Brand key: set ({len(EMONPAY_BRAND_KEY)} chars)" if EMONPAY_BRAND_KEY else "🏷 Brand key: <b>NOT SET</b>")
    lines.append("🔐 Secret key: set" if EMONPAY_SECRET_KEY else "🔐 Secret key: not set (only needed if the panel gave you one)")
    lines.append("🌍 Public URL: not needed — auto-credit works by polling the verify API + dashboard scan.")
    try:
        ip = requests.get("https://api.ipify.org", timeout=6).text.strip()
        lines.append(f"📡 This server's public IP: <code>{html.escape(ip[:64])}</code>")
    except Exception:
        lines.append("📡 This server's public IP: unknown")

    if EMONPAY_API_KEY and EMONPAY_BRAND_KEY:
        base = BOT_RETURN_URL
        test_payload = {
            "cus_name": "Diagnostic Test",
            "cus_email": f"user_{ADMIN_ID}@telegram.bot",
            "amount": "10",
            "success_url": base + "/emonpay/success/EP0000000000000000",
            "cancel_url": base + "/emonpay/cancel/EP0000000000000000",
            "meta_data": {"source": "telegram_bot_diagnostic"},
            "metadata": {"source": "telegram_bot_diagnostic"}
        }
        checks = [
            ("create", EMONPAY_CREATE_URL, test_payload),
            ("verify", EMONPAY_VERIFY_URL, {"transaction_id": "DIAGNOSTIC0"}),
        ]
        for name, url, body in checks:
            r = _emonpay_post(url, body, timeout=20)
            shown = f"HTTP {r['status']}" if r["status"] is not None else (r["error"] or "no response")
            server = r["headers"].get("server", "-")
            cf = "yes" if "cf-ray" in r["headers"] else "no"
            lines.append(
                f"\n<b>POST /{name}</b> → <b>{html.escape(str(shown))}</b>\n"
                f"Server: <code>{html.escape(str(server))}</code> | Cloudflare: {cf}\n"
                f"Body: <code>{html.escape(_emonpay_clean(r['text'], 250) or '-')}</code>"
            )
            ok = r["status"] is not None and 200 <= r["status"] < 300
            if name == "create" and ok and r["data"].get("payment_url"):
                lines.append("✅ Create works. (A 10 BDT test link was created; ignore it, it is never credited.)")
            elif name == "verify" and r["status"] in (400, 404, 422):
                lines.append("✅ Verify endpoint reachable and keys accepted (an unknown test transaction is expected to be rejected).")
            elif not ok:
                lines.append(f"💡 {html.escape(_emonpay_hint(r))}")

    lines.append("\n<b>— Binance —</b>")
    if not binance_configured():
        missing = [n for n, v in (("BINANCE_API_KEY", BINANCE_API_KEY), ("BINANCE_API_SECRET", BINANCE_API_SECRET), ("BINANCE_PAY_UID", BINANCE_PAY_UID)) if not v]
        lines.append("⚠️ Not configured. Missing: <code>" + html.escape(", ".join(missing) or "invalid BINANCE_USDT_RATE") + "</code> (set them as environment variables / .env). The Binance method is disabled until then.")
    else:
        lines.append(f"🔑 API key/secret: set | 🆔 Pay UID: <code>{html.escape(BINANCE_PAY_UID)}</code> | 💱 1 USDT = ৳{_fmt_dec(BINANCE_USDT_RATE)}")
        now_ms = int(time.time() * 1000)
        r = _binance_get("/sapi/v1/pay/transactions", {"startTime": now_ms - 3600 * 1000, "endTime": now_ms, "limit": 5})
        shown = f"HTTP {r['status']}" if r["status"] is not None else (r["error"] or "no response")
        lines.append(f"\n<b>GET /sapi/v1/pay/transactions</b> → <b>{html.escape(str(shown))}</b>\nBody: <code>{html.escape(_emonpay_clean(r['text'], 250) or '-')}</code>")
        if _binance_response_ok(r):
            lines.append("✅ Binance Pay history API works with these keys. Binance auto-verification is available.")
        else:
            lines.append(f"💡 {html.escape(_binance_hint(r))}")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")

# ----------------- Emon Pay web routes (return page + webhook) -----------------
def _emonpay_page(title, message, icon):
    bot_link = f"https://t.me/{BOT_USERNAME}"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta http-equiv='refresh' content='3;url={bot_link}'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#f5f3ff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
        ".c{background:#fff;border-radius:20px;padding:36px 28px;max-width:380px;text-align:center;box-shadow:0 10px 40px rgba(79,70,229,.15)}"
        "h1{font-size:22px;margin:8px 0}p{color:#475569;font-size:15px;line-height:1.5}"
        "a{display:inline-block;margin-top:18px;padding:12px 26px;border-radius:12px;background:linear-gradient(90deg,#4f46e5,#9333ea);color:#fff;text-decoration:none;font-weight:600}</style>"
        f"</head><body><div class='c'><div style='font-size:48px'>{icon}</div><h1>{html.escape(title)}</h1>"
        f"<p>{html.escape(message)}</p><a href='{bot_link}'>Back to Bot</a></div>"
        f"<script>setTimeout(function(){{location.href='{bot_link}'}},2500)</script></body></html>"
    )

@app.route("/emonpay/success/<ref>")
def emonpay_success(ref):
    if not _EMONPAY_REF_RE.match(ref):
        return "Not found", 404
    txn = request.args.get("transactionId") or request.args.get("transaction_id") or ""
    # The browser's own "status"/"paymentAmount" are never trusted: only the server-side verify API decides.
    try:
        state = emonpay_process_transaction(txn, ref, source="CALLBACK")
    except Exception as e:
        print(f"Emon Pay success handler error: {e}")
        state = "error"
    if state == "completed":
        return _emonpay_page("Payment Successful", "Your balance has been added. Returning you to the bot...", "✅")
    if state in ("pending", "error"):
        return _emonpay_page("Payment Received", "We are confirming your payment. Your balance will be added automatically in a moment. Returning you to the bot...", "⏳")
    if state == "failed":
        return _emonpay_page("Payment Failed", "Your payment was not completed. Please go back to the bot and try again.", "❌")
    return _emonpay_page("Payment Not Verified", "We could not verify this payment. If money was deducted, please contact support from the bot.", "⚠️")

@app.route("/emonpay/cancel/<ref>")
def emonpay_cancel(ref):
    return _emonpay_page("Payment Cancelled", "No money was added. Returning you to the bot...", "❌")

@app.route("/emonpay/webhook", methods=["GET", "POST"])
@app.route("/emonpay/webhook/<ref>", methods=["GET", "POST"])
def emonpay_webhook(ref=None):
    """Optional server-to-server notification. Only used as a trigger: the real status is re-verified via the API."""
    try:
        payload = request.get_json(silent=True) or request.form.to_dict() or request.args.to_dict()
        if not isinstance(payload, dict):
            payload = {}
        txn = payload.get("transaction_id") or payload.get("transactionId") or payload.get("trx_id") or ""
        if txn:
            emonpay_process_transaction(txn, ref if ref and _EMONPAY_REF_RE.match(ref) else None, source="CALLBACK")
    except Exception as e:
        print(f"Emon Pay webhook error: {e}")
    return jsonify({"ok": True}), 200

# ----------------- Binance (official Binance Pay history API: GET /sapi/v1/pay/transactions) -----------------
_BINANCE_TXN_RE = re.compile(r"^[A-Za-z0-9_\-]{6,64}$")
_BINANCE_LOCK = threading.Lock()
_BINANCE_CACHE = {"at": 0.0, "start": 0, "txs": None}
_BINANCE_TIME = {"offset": 0, "at": 0.0}
_BINANCE_NOTIFIED = set()
_BINANCE_USER_LAST = {}

def binance_configured():
    return bool(BINANCE_API_KEY and BINANCE_API_SECRET and BINANCE_PAY_UID.isdigit() and BINANCE_USDT_RATE and BINANCE_USDT_RATE > 0)

def _binance_sign(query_string, secret):
    return hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def _binance_server_offset():
    """Local-clock offset vs Binance server time (cached 10 min) so signed requests never fail on clock drift."""
    now = time.time()
    if now - _BINANCE_TIME["at"] < 600:
        return _BINANCE_TIME["offset"]
    try:
        resp = requests.get(BINANCE_BASE_URL + "/api/v3/time", timeout=8)
        server_ms = int(resp.json()["serverTime"])
        _BINANCE_TIME["offset"] = server_ms - int(now * 1000)
        _BINANCE_TIME["at"] = now
    except Exception:
        _BINANCE_TIME["at"] = now - 540  # retry in about a minute
    return _BINANCE_TIME["offset"]

def _binance_get(path, params, timeout=15):
    """Signed GET to the official Binance API. Never raises. Same result shape as _emonpay_post."""
    result = {"status": None, "data": {}, "text": "", "headers": {}, "error": None}
    try:
        query_params = dict(params)
        query_params["recvWindow"] = 10000
        query_params["timestamp"] = int(time.time() * 1000) + _binance_server_offset()
        query = urlencode(query_params)
        url = f"{BINANCE_BASE_URL}{path}?{query}&signature={_binance_sign(query, BINANCE_API_SECRET)}"
        resp = requests.get(url, headers={"X-MBX-APIKEY": BINANCE_API_KEY, "Accept": "application/json"}, timeout=timeout)
        result["status"] = resp.status_code
        result["text"] = resp.text or ""
        result["headers"] = {k.lower(): v for k, v in resp.headers.items()}
        try:
            parsed = resp.json()
            result["data"] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            result["data"] = {}
    except requests.exceptions.Timeout:
        result["error"] = "timeout"
    except requests.exceptions.ConnectionError as e:
        result["error"] = "connection: " + _emonpay_clean(e, 160)
    except Exception as e:
        result["error"] = "error: " + _emonpay_clean(e, 160)
    return result

def _binance_response_ok(result):
    data = result.get("data") or {}
    status = result.get("status")
    if status is None or not (200 <= status < 300) or not data:
        return False
    success = data.get("success") is True or str(data.get("code")) in ("000000", "0")
    return success and isinstance(data.get("data"), list)

def _binance_hint(result):
    status = result.get("status")
    data = result.get("data") or {}
    code = str(data.get("code", ""))
    if status is None:
        if result.get("error") == "timeout":
            return "Binance did not answer in time (network slow or blocked)."
        return "Cannot reach api.binance.com from this server (network/DNS/firewall block)."
    if status == 451 or "restricted location" in (result.get("text") or "").lower():
        return "Binance blocks API access from this server's country/IP (restricted location). The bot must run on a server in a supported region."
    if code in ("-2015", "-2014") or status == 401:
        return "Binance rejected the API key: wrong key, missing permission (enable 'Enable Reading'), or this server's IP is not in the key's IP whitelist."
    if code == "-1022":
        return "Signature invalid: BINANCE_API_SECRET is wrong or has extra spaces."
    if code == "-1021":
        return "Timestamp problem: the server clock is out of sync."
    if status in (418, 429):
        return "Binance rate limit hit. The bot will retry later."
    if status >= 500:
        return "Binance server error. Try again later."
    return "Binance returned an unexpected response. If the Pay history endpoint is not enabled for this account/key, a Binance Pay Merchant account may be required."

def binance_fetch_transactions(start_ms, end_ms=None):
    """Official Pay history. Returns (list, None) on success or (None, failed_result)."""
    collected = []
    end = int(end_ms or (time.time() * 1000 + 60000))
    for _ in range(5):
        r = _binance_get("/sapi/v1/pay/transactions", {"startTime": int(start_ms), "endTime": end, "limit": 100})
        if not _binance_response_ok(r):
            return None, r
        page = r["data"]["data"]
        collected.extend(t for t in page if isinstance(t, dict))
        if len(page) < 100:
            break
        times = []
        for t in page:
            try:
                times.append(int(t.get("transactionTime")))
            except (TypeError, ValueError):
                pass
        if not times or min(times) <= start_ms:
            break
        end = min(times) - 1
    return collected, None

def _binance_history(start_ms):
    """History with a short cache + lock so many users cannot exhaust Binance's request weight."""
    with _BINANCE_LOCK:
        now = time.time()
        if _BINANCE_CACHE["txs"] is not None and now - _BINANCE_CACHE["at"] < 6 and _BINANCE_CACHE["start"] <= start_ms:
            return _BINANCE_CACHE["txs"], None
        txs, err = binance_fetch_transactions(start_ms)
        if txs is not None:
            _BINANCE_CACHE.update({"at": time.time(), "start": start_ms, "txs": txs})
        return txs, err

def _binance_match(txs, txn_id, expected_usdt, created_at):
    """Strict match of ONE claimed Binance Order ID against the real API data. Returns (state, tx)."""
    wanted = str(txn_id).strip().lower()
    def _ids(t):
        return {str(t.get(k)).strip().lower() for k in ("transactionId", "orderId", "prepayId") if t.get(k) not in (None, "")}
    tx = next((t for t in txs if wanted and wanted in _ids(t)), None)
    if tx is None:
        return "not_found", None
    if str(tx.get("orderType", "")).upper() != "C2C":
        return "bad_type", tx
    if str(tx.get("currency", "")).upper() != "USDT":
        return "bad_currency", tx
    amount = _dec(tx.get("amount"))
    if amount is None or amount <= 0 or expected_usdt is None or amount != expected_usdt:
        return "amount_mismatch", tx
    receiver = tx.get("receiverInfo")
    receiver_uid = str(receiver.get("binanceId", "")).strip() if isinstance(receiver, dict) else ""
    if not receiver_uid or receiver_uid != BINANCE_PAY_UID:
        return "wrong_receiver", tx
    try:
        tx_time_ms = int(tx.get("transactionTime"))
    except (TypeError, ValueError):
        return "too_old", tx
    if tx_time_ms < (int(created_at) - 120) * 1000:  # must be paid after the invoice was created (2 min clock tolerance)
        return "too_old", tx
    return "ok", tx

_BINANCE_USER_MESSAGES = {
    "amount_mismatch": "❌ <b>আপনার ট্রানজেকশনটি পাওয়া গেছে, কিন্তু পরিমাণ মেলেনি।</b>\nঠিক <b>${usdt} USDT</b> পাঠাতে হবে। ব্যালেন্স যোগ হয়নি, সাপোর্টে যোগাযোগ করুন।",
    "bad_currency": "❌ <b>ট্রানজেকশনটি USDT-তে নয়।</b>\nশুধুমাত্র <b>USDT</b> পাঠাতে হবে। ব্যালেন্স যোগ হয়নি, সাপোর্টে যোগাযোগ করুন।",
    "wrong_receiver": "❌ <b>এই ট্রানজেকশনটি আমাদের Pay UID-তে পাঠানো হয়নি।</b>\nব্যালেন্স যোগ হয়নি, সাপোর্টে যোগাযোগ করুন।",
    "bad_type": "❌ <b>এই ট্রানজেকশনের ধরন গ্রহণযোগ্য নয়।</b>\nBinance Pay (UID-তে সরাসরি Send) ব্যবহার করুন। ব্যালেন্স যোগ হয়নি, সাপোর্টে যোগাযোগ করুন।",
    "too_old": "❌ <b>এই ট্রানজেকশনটি এই ডিপোজিটের আগের।</b>\nব্যালেন্স যোগ হয়নি, সাপোর্টে যোগাযোগ করুন।",
    "duplicate": "❌ <b>এই Order ID আগেই ব্যবহার করা হয়েছে।</b>\nব্যালেন্স যোগ হয়নি।",
}

def binance_check_invoices(refs=None):
    """
    Check submitted Binance claims against the official API. One API call covers every pending claim.
    Returns {invoice_ref: credited|already|not_found|api_error|<mismatch reason>}.
    API failure or no match always leaves the invoice PENDING.
    """
    results = {}
    if not binance_configured():
        return results
    now = int(time.time())
    conn = db_connect("shop_bot.db", timeout=30)
    cur = conn.cursor()
    cur.execute(
        "SELECT invoice_ref, user_id, expected_usdt, transaction_id, created_at FROM payment_invoices "
        "WHERE gateway = 'BINANCE' AND status = 'PENDING' AND transaction_id IS NOT NULL AND COALESCE(txn_set_at, created_at) >= ?",
        (now - BINANCE_CLAIM_WINDOW,)
    )
    invoices = [r for r in cur.fetchall() if (refs is None or r[0] in refs)]
    conn.close()
    if not invoices:
        return results

    start_ms = (min(int(r[4]) for r in invoices) - 300) * 1000
    txs, err = _binance_history(start_ms)
    if txs is None:
        shown = f"HTTP {err['status']}" if err and err.get("status") is not None else ((err or {}).get("error") or "no response")
        print(f"Binance history unavailable: {shown} | {_emonpay_clean((err or {}).get('text'), 200)}")
        _emonpay_alert_admin(
            "bn_api",
            f"⚠️ <b>Binance verification unavailable</b> ({html.escape(str(shown))}).\n"
            f"Response: <code>{html.escape(_emonpay_clean((err or {}).get('text'), 250) or '-')}</code>\n"
            f"💡 {html.escape(_binance_hint(err or {}))}\n"
            "Pending Binance deposits stay PENDING (nothing is credited). Run /paytest."
        )
        return {r[0]: "api_error" for r in invoices}

    for ref, user_id, usdt_text, txn_id, created_at in invoices:
        try:
            expected_usdt = _dec(usdt_text)
            state, tx = _binance_match(txs, str(txn_id).strip(), expected_usdt, created_at)
            if state == "ok":
                canonical = str((tx or {}).get("transactionId") or txn_id).strip()
                state = _credit_invoice(ref, canonical, "BINANCE")
            results[ref] = state
            if state in ("credited", "already", "not_found", "closed", "missing"):
                continue
            # Anything else is a rejection: balance NOT added. Tell the user and the admin once.
            if (ref, state) in _BINANCE_NOTIFIED:
                continue
            _BINANCE_NOTIFIED.add((ref, state))
            template = _BINANCE_USER_MESSAGES.get(state)
            if template:
                try:
                    bot.send_message(user_id, template.format(usdt=_fmt_dec(expected_usdt or Decimal("0"))), parse_mode="HTML")
                except Exception as e:
                    print(f"Binance user notice failed: {e}")
            summary = {}
            if isinstance(tx, dict):
                receiver = tx.get("receiverInfo") if isinstance(tx.get("receiverInfo"), dict) else {}
                summary = {"orderType": tx.get("orderType"), "currency": tx.get("currency"), "amount": tx.get("amount"),
                           "receiver_binanceId_present": bool(receiver.get("binanceId")), "time": tx.get("transactionTime")}
            _emonpay_alert_admin(
                f"bn_{ref}_{state}",
                "⚠️ <b>Binance payment NOT credited</b> (strict check failed)\n"
                f"Reason: <b>{html.escape(state)}</b>\nInvoice: <code>{html.escape(ref)}</code> | User: <code>{user_id}</code>\n"
                f"Expected: <code>{html.escape(str(usdt_text))} USDT</code> | TrxID: <code>{html.escape(str(txn_id))}</code>\n"
                f"API data: <code>{html.escape(json.dumps(summary, default=str))}</code>"
            )
        except Exception as e:
            print(f"Binance check error for invoice {ref}: {e}")
            results.setdefault(ref, "api_error")
    return results

def create_binance_invoice(chat_id, user_id, amount, message_id=None):
    """Create a Binance invoice for the selected BDT amount and ask the user for the Transaction ID."""
    if not binance_configured():
        _emonpay_alert_admin(
            "bn_noconfig",
            "⚠️ <b>Binance deposit is not configured.</b>\nSet <code>BINANCE_API_KEY</code>, <code>BINANCE_API_SECRET</code> and <code>BINANCE_PAY_UID</code> (environment / .env) and restart."
        )
        _emonpay_show(chat_id, message_id, "⚠️ <b>Binance deposit is temporarily unavailable.</b>\n\nPlease choose another payment method.")
        return False
    amount_dec = _dec(amount)
    if amount_dec is None or amount_dec <= 0:
        return False
    usdt = _q2(amount_dec / BINANCE_USDT_RATE)
    credit_bdt = _q2(usdt * BINANCE_USDT_RATE)  # exactly what the sent USDT is worth at the fixed rate
    if usdt <= 0 or credit_bdt <= 0:
        _emonpay_show(chat_id, message_id, "⚠️ Amount is too small for Binance. Please choose a larger amount.")
        return False

    ref = "BN" + uuid.uuid4().hex[:16].upper()
    try:
        conn = db_connect("shop_bot.db", timeout=30)
        cur = conn.cursor()
        # One open (unclaimed) Binance invoice per user: an older one is closed so the amounts never get mixed up.
        cur.execute(
            "UPDATE payment_invoices SET status = 'EXPIRED' WHERE user_id = ? AND gateway = 'BINANCE' AND status = 'PENDING' AND transaction_id IS NULL",
            (user_id,)
        )
        conn.commit()
        conn.close()
        _insert_invoice(ref, user_id, credit_bdt, "BINANCE", "BINANCE", expected_usdt=usdt)
    except Exception as e:
        print(f"Binance invoice creation error: {e}")
        _emonpay_show(chat_id, message_id, "⚠️ <b>Something went wrong. Please try again later.</b>")
        return False

    user_temp_deposit[user_id] = {"step": "waiting_binance_trx", "invoice_ref": ref}
    lines = [
        "✨ <b>আপনি Binance সিলেক্ট করেছেন।</b>\n",
        "এই Binance User ID-তে ডলার Send করুন:\n",
        f"Pay UID: <code>{html.escape(BINANCE_PAY_UID)}</code>\n",
        f"Send: <b>${_fmt_dec(usdt)} USDT</b>\n",
        f"<i>(১ USDT = {_bn_digits(_fmt_dec(BINANCE_USDT_RATE))} টাকা অনুযায়ী হিসাব করা হবে)</i>",
    ]
    if credit_bdt != _q2(amount_dec):
        lines.append(f"\n💰 ব্যালেন্সে যোগ হবে: <b>৳{_fmt_dec(credit_bdt)}</b>")
    lines.append("\nডলার পাঠানোর পর Binance-এর পেমেন্ট ডিটেইলস থেকে আপনার <b>Order ID</b> কপি করে চ্যাটে লিখে পাঠান। আমরা সাথে সাথে যাচাই করে ব্যালেন্স যোগ করে দেব।")
    _emonpay_show(chat_id, message_id, "\n".join(lines))
    return True

def binance_find_open_invoice(user_id):
    """Latest open Binance invoice of this user that has no Transaction ID yet (survives bot restarts)."""
    conn = db_connect("shop_bot.db", timeout=30)
    cur = conn.cursor()
    cur.execute(
        "SELECT invoice_ref FROM payment_invoices WHERE user_id = ? AND gateway = 'BINANCE' AND status = 'PENDING' "
        "AND transaction_id IS NULL AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
        (user_id, int(time.time()) - PAYMENT_INVOICE_TTL)
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def binance_submit_transaction(user_id, ref, raw_txn):
    """Validate + store the user's Transaction ID as PENDING. Nothing is credited here. Returns ok|invalid|duplicate|unknown|closed."""
    txn = str(raw_txn or "").strip()
    if not _BINANCE_TXN_RE.match(txn):
        return "invalid"
    conn = db_connect("shop_bot.db", timeout=30)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT user_id, status, gateway FROM payment_invoices WHERE invoice_ref = ?", (ref,))
        row = cur.fetchone()
        if not row or row[0] != user_id or row[2] != "BINANCE":  # invoice ownership validation
            _db_abort(conn)
            return "unknown"
        if row[1] != "PENDING":
            _db_abort(conn)
            return "closed"
        cur.execute(
            "SELECT 1 FROM payment_invoices WHERE transaction_id = ? AND invoice_ref <> ? AND status IN ('PENDING', 'COMPLETED') LIMIT 1",
            (txn, ref)
        )
        if cur.fetchone():
            _db_abort(conn)
            return "duplicate"
        cur.execute(
            "UPDATE payment_invoices SET transaction_id = ?, txn_source = 'USER', txn_set_at = ? WHERE invoice_ref = ? AND status = 'PENDING'",
            (txn, int(time.time()), ref)
        )
        conn.commit()
        conn.close()
        return "ok"
    except Exception:
        _db_abort(conn)
        raise

def binance_handle_user_txn(user_id, ref, raw_txn):
    """Entry point for a Binance Order ID typed by the user."""
    if time.time() - _BINANCE_USER_LAST.get(user_id, 0) < 5:
        bot.send_message(user_id, "⏳ একটু অপেক্ষা করুন, আপনার আগের অনুরোধ প্রসেস হচ্ছে।", parse_mode="HTML")
        return
    _BINANCE_USER_LAST[user_id] = time.time()
    try:
        outcome = binance_submit_transaction(user_id, ref, raw_txn)
    except Exception as e:
        print(f"Binance submit error: {e}")
        bot.send_message(user_id, "⚠️ কিছু সমস্যা হয়েছে, একটু পরে আবার চেষ্টা করুন।", parse_mode="HTML")
        return
    if outcome == "invalid":
        bot.send_message(user_id, "⚠️ <b>Order ID সঠিক নয়।</b>\nশুধু আপনার Binance Order ID (অক্ষর/সংখ্যা) লিখে পাঠান।", parse_mode="HTML")
        return
    if outcome == "duplicate":
        bot.send_message(user_id, _BINANCE_USER_MESSAGES["duplicate"], parse_mode="HTML")
        return
    if outcome in ("unknown", "closed"):
        user_temp_deposit.pop(user_id, None)
        bot.send_message(user_id, "⚠️ এই ডিপোজিট রিকোয়েস্টটি আর সক্রিয় নেই। অনুগ্রহ করে নতুন করে Deposit করুন।", parse_mode="HTML", reply_markup=get_permanent_keyboard(user_id))
        return

    user_temp_deposit.pop(user_id, None)
    status_msg = _emonpay_show(user_id, None, "⏳ <b>Binance-এ আপনার পেমেন্ট যাচাই করা হচ্ছে...</b>")
    state = binance_check_invoices([ref]).get(ref)
    if state in ("credited", "already"):
        return  # the success message was already sent by _credit_invoice
    if state in ("not_found", "api_error", None):
        _emonpay_show(
            user_id, status_msg,
            "🔎 <b>এই Order ID এখনো Binance-এ পাওয়া যায়নি।</b>\n\n"
            "Binance-এ আসতে কিছুটা সময় লাগতে পারে। আমরা নিজে থেকেই চেক করতে থাকব এবং যাচাই হলে ব্যালেন্স <b>অটো যোগ</b> হয়ে যাবে। আবার পাঠানোর দরকার নেই।"
        )
        def _quick_rechecks():
            for _ in range(3):
                time.sleep(7)
                res = binance_check_invoices([ref]).get(ref)
                if res not in ("not_found", "api_error"):
                    return  # credited / already done / rejected (the user was already notified)
        threading.Thread(target=_quick_rechecks, name="binance-quick-recheck", daemon=True).start()

# ----------------- Background worker (Emon Pay re-verification + Binance re-checks + expiry) -----------------
def emonpay_verify_loop():
    last_binance = 0.0
    while True:
        try:
            now = int(time.time())
            conn = db_connect("shop_bot.db", timeout=30)
            cur = conn.cursor()
            cur.execute("SELECT invoice_ref, gateway, transaction_id, created_at, txn_source, txn_set_at FROM payment_invoices WHERE status = 'PENDING'")
            pending_rows = cur.fetchall()
            conn.close()

            binance_waiting = False
            for ref, gateway, txn, created_at, source, set_at in pending_rows:
                try:
                    created_at = int(created_at or 0)
                    if now - created_at > PAYMENT_INVOICE_TTL:
                        conn = db_connect("shop_bot.db", timeout=30)
                        cur = conn.cursor()
                        cur.execute("UPDATE payment_invoices SET status = 'EXPIRED' WHERE invoice_ref = ? AND status = 'PENDING'", (ref,))
                        conn.commit()
                        conn.close()
                        continue
                    if not txn:
                        continue
                    set_at = int(set_at or created_at)
                    if gateway == "EMONPAY" and _EMONPAY_REF_RE.match(str(ref)):
                        window = EMONPAY_LINK_POLL_WINDOW if source == "LINK" else EMONPAY_PENDING_TXN_TTL
                        if now - set_at <= window:
                            emonpay_process_transaction(txn, ref)
                    elif gateway == "BINANCE" and now - set_at <= BINANCE_CLAIM_WINDOW:
                        binance_waiting = True
                except Exception as e:
                    print(f"Payment worker error for invoice {ref}: {e}")
            if binance_waiting and time.time() - last_binance >= BINANCE_POLL_INTERVAL:
                last_binance = time.time()
                binance_check_invoices()
        except Exception as e:
            print(f"Payment worker loop error: {e}")
        time.sleep(EMONPAY_POLL_INTERVAL)

# ----------------- Admin: Live Statistic Dashboard (real data from the bot's own deposit history) -----------------
BD_TZ = timezone(timedelta(hours=6))
_STATS_PERIODS = {"today": (1, "Today"), "7d": (7, "Last 7 Days"), "30d": (30, "Last 30 Days")}

def _emon_guess_method(info):
    """bKash / Nagad / Rocket of a finished EmonPay payment (best effort: explicit field first, then the MFS message)."""
    if not isinstance(info, dict):
        return None
    for key in ("method", "payment_method", "gateway", "provider"):
        val = str(info.get(key) or "").lower()
        for name, label in (("bkash", "bKash"), ("nagad", "Nagad"), ("rocket", "Rocket")):
            if name in val:
                return label
    msg = str(info.get("message") or "")
    low = msg.lower()
    if "bkash" in low:
        return "bKash"
    if "nagad" in low or "money received" in low:
        return "Nagad"
    if "rocket" in low or "dbbl" in low:
        return "Rocket"
    if re.search(r"\bTrxID\b", msg):
        return "bKash"
    return None

def _set_paid_method(ref, label):
    if not label:
        return
    try:
        conn = db_connect("shop_bot.db", timeout=30)
        try:
            conn.execute("UPDATE payment_invoices SET paid_method = ? WHERE invoice_ref = ?", (label, ref))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"paid_method store skipped: {type(e).__name__}")

def _money(value):
    value = _q2(Decimal(value))
    return f"{int(value):,}" if value == value.to_integral_value() else f"{value:,.2f}"

def _bar(value, top, width=10):
    filled = 0 if not top else int(round(float(value) / float(top) * width))
    if value and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)

def _method_bucket(gateway, paid_method, payment_method):
    if gateway == "BINANCE":
        return "Binance"
    text = f"{paid_method or ''} {payment_method or ''}".lower()
    for name, label in (("bkash", "bKash"), ("nagad", "Nagad"), ("rocket", "Rocket")):
        if name in text and not ("bkash" in text and "nagad" in text):   # the generic "bKash / Nagad / Rocket" label is not a real method
            return label
    return paid_method if paid_method in ("bKash", "Nagad", "Rocket") else "Unknown"

def get_stats_markup(active):
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(*[
        InlineKeyboardButton(("• " if key == active else "") + name, callback_data=f"admin_stats_{key}")
        for key, (_d, name) in (("today", _STATS_PERIODS["today"]), ("7d", (7, "7 Days")), ("30d", (30, "30 Days")))
    ])
    markup.add(InlineKeyboardButton("🔄 Refresh", callback_data=f"admin_stats_{active}"))
    markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
    return markup

def build_admin_stats(period):
    days, label = _STATS_PERIODS.get(period, _STATS_PERIODS["today"])
    now = datetime.now(BD_TZ)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = today0 - timedelta(days=days - 1)
    start_ts = int(start_dt.timestamp())
    start_iso = start_dt.astimezone(timezone.utc).isoformat()

    conn = db_connect("shop_bot.db", timeout=30)
    try:
        rows = conn.execute(
            "SELECT dh.created_at, dh.user_id, dh.amount, dh.payment_method, dh.gateway, dh.usdt_amount, pi.paid_method "
            "FROM deposit_history dh LEFT JOIN payment_invoices pi ON pi.invoice_ref = dh.invoice_ref "
            "WHERE dh.created_at >= ? ORDER BY dh.created_at DESC", (start_ts,)).fetchall()
        pending = dict(conn.execute(
            "SELECT gateway, COUNT(*) FROM payment_invoices WHERE status = 'PENDING' AND created_at >= ? GROUP BY gateway",
            (int(time.time()) - PAYMENT_INVOICE_TTL,)).fetchall())
        sales = conn.execute("SELECT COUNT(*), COALESCE(SUM(unit_price), 0) FROM stock_sales WHERE sold_at >= ?", (start_iso,)).fetchone()
        members = conn.execute("SELECT COUNT(*), COALESCE(SUM(price), 0) FROM member_sell_orders WHERE status = 'completed' AND completed_at >= ?", (start_iso,)).fetchone()
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_balance = conn.execute("SELECT COALESCE(SUM(balance), 0) FROM users").fetchone()[0]
    finally:
        conn.close()

    total, users = Decimal("0"), set()
    by_method = {"bKash": [Decimal("0"), 0, Decimal("0")], "Nagad": [Decimal("0"), 0, Decimal("0")],
                 "Rocket": [Decimal("0"), 0, Decimal("0")], "Binance": [Decimal("0"), 0, Decimal("0")]}
    per_day, latest = {}, []
    for created_at, uid, amount_text, pay_method, gateway, usdt_text, paid_method in rows:
        amount = _dec(amount_text) or Decimal("0")
        bucket = _method_bucket(gateway, paid_method, pay_method)
        slot = by_method.setdefault(bucket, [Decimal("0"), 0, Decimal("0")])
        slot[0] += amount
        slot[1] += 1
        slot[2] += _dec(usdt_text) or Decimal("0")
        total += amount
        users.add(uid)
        day = datetime.fromtimestamp(int(created_at), BD_TZ).date()
        d = per_day.setdefault(day, [Decimal("0"), 0])
        d[0] += amount
        d[1] += 1
        if len(latest) < 5:
            latest.append((datetime.fromtimestamp(int(created_at), BD_TZ), uid, amount, bucket))
    count = len(rows)
    avg = (total / count) if count else Decimal("0")

    if days == 1:
        range_text = today0.strftime("%d %b %Y")
    else:
        range_text = f"{start_dt.strftime('%d %b')} – {today0.strftime('%d %b %Y')}"
    lines = [
        "📊 <b>Live Statistic Dashboard</b>",
        f"🗓 <b>{label}</b> · {range_text}",
        f"🕒 Updated: {now.strftime('%H:%M:%S')} (BD time)",
        "",
        f"💰 <b>Total Deposit:</b> ৳{_money(total)}",
        f"🧾 <b>Transactions:</b> {count}   👤 <b>Depositors:</b> {len(users)}",
        f"📈 <b>Average:</b> ৳{_money(avg)}",
        "",
        "📲 <b>Payment Methods</b>",
    ]
    top = max([v[0] for v in by_method.values()] or [Decimal("0")])
    order = ["bKash", "Nagad", "Rocket", "Binance"] + [k for k in by_method if k not in ("bKash", "Nagad", "Rocket", "Binance")]
    for name in order:
        amount, n, usdt = by_method[name]
        if name not in ("bKash", "Nagad", "Rocket", "Binance") and n == 0:
            continue
        share = (amount / total * 100) if total else Decimal("0")
        extra = f" · ${_fmt_dec(usdt)} USDT" if name == "Binance" and n else ""
        lines.append(f"<code>{name[:7]:<7}</code> {_bar(amount, top)} <b>৳{_money(amount)}</b> ({n} txn, {share:.0f}%){extra}")

    if days == 1:
        lines += ["", "🕒 <b>Latest Deposits</b>"]
        if latest:
            for when, uid, amount, bucket in latest:
                lines.append(f"• {when.strftime('%H:%M')} · ৳{_money(amount)} · {html.escape(bucket)} · <code>{uid}</code>")
        else:
            lines.append("• আজ এখনো কোনো ডিপোজিট হয়নি।")
    elif days == 7:
        lines += ["", "📅 <b>Daily Breakdown</b>"]
        day_top = max([v[0] for v in per_day.values()] or [Decimal("0")])
        for i in range(days):
            day = (today0 - timedelta(days=i)).date()
            amount, n = per_day.get(day, [Decimal("0"), 0])
            lines.append(f"<code>{day.strftime('%d %b')}</code> {_bar(amount, day_top)} ৳{_money(amount)} · {n}")
    else:
        lines += ["", "📅 <b>Weekly Breakdown</b>"]
        buckets = {}
        for day, (amount, n) in per_day.items():
            idx = (today0.date() - day).days // 7
            b = buckets.setdefault(idx, [Decimal("0"), 0])
            b[0] += amount
            b[1] += n
        b_top = max([v[0] for v in buckets.values()] or [Decimal("0")])
        for idx in range(0, (days - 1) // 7 + 1):
            end_day = (today0 - timedelta(days=idx * 7)).date()
            begin_day = max((today0 - timedelta(days=idx * 7 + 6)).date(), start_dt.date())
            amount, n = buckets.get(idx, [Decimal("0"), 0])
            lines.append(f"<code>{begin_day.strftime('%d %b')}–{end_day.strftime('%d %b')}</code> {_bar(amount, b_top, 8)} ৳{_money(amount)} · {n}")

    lines += [
        "",
        "🛒 <b>Sales (same period)</b>",
        f"• Stock items sold: {sales[0]} · ৳{_money(Decimal(str(sales[1])))}",
        f"• Member orders completed: {members[0]} · ৳{_money(Decimal(str(members[1])))}",
        "",
        f"⏳ <b>Pending deposits:</b> AxPay {pending.get('EMONPAY', 0)} · Binance {pending.get('BINANCE', 0)}",
        f"👥 <b>Total users:</b> {total_users}   💳 <b>Users' balance:</b> ৳{_money(Decimal(str(total_balance or 0)))}",
    ]
    return "\n".join(lines), get_stats_markup(period if period in _STATS_PERIODS else "today")

def process_purchase(chat_id, user_id, cat_id, qty):
    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    cursor.execute("SELECT price, name, button_name FROM categories WHERE cat_id = ? AND is_active = 1", (cat_id,))
    cat_data = cursor.fetchone()
    if not cat_data:
        conn.rollback()
        conn.close()
        bot.send_message(chat_id, f"{pe('warn_icon')} এই সার্ভিসটি বর্তমানে আর উপলব্ধ নেই।", parse_mode="HTML")
        return

    price_per_item = cat_data[0]
    cat_name = cat_data[1]
    button_name = cat_data[2] or cat_name
    total_cost = price_per_item * qty

    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    user_balance_row = cursor.fetchone()
    user_balance = user_balance_row[0] if user_balance_row else 0.0

    if user_balance < total_cost:
        conn.rollback()
        conn.close()
        bot.send_message(
            chat_id, 
            f"{pe('insufficient_bal')} <b>আপনার অ্যাকাউন্টে পর্যাপ্ত ব্যালেন্স নেই!</b>\n\n"
            f"{pe('money_spent')} প্রয়োজন: ৳{total_cost}\n"
            f"{pe('wallet_balance')} আপনার আছে: ৳{user_balance}\n\n"
            f"{pe('step_tick')} আগে ডিপোজিট করুন, তারপর পণ্য কিনুন।",
            parse_mode="HTML",
            reply_markup=get_permanent_keyboard(user_id)
        )
        return

    cursor.execute("SELECT COUNT(*) FROM stock WHERE cat_id = ? AND status = 'available'", (cat_id,))
    stock_count = cursor.fetchone()[0]

    if stock_count < qty:
        conn.rollback()
        conn.close()
        bot.send_message(chat_id, f"{pe('no_stock')} দুঃখিত, এই মুহূর্তে পর্যাপ্ত স্টক নেই! স্টকে আছে: <b>{stock_count} পিস</b>።", parse_mode="HTML")
        return

    cursor.execute("SELECT id, content FROM stock WHERE cat_id = ? AND status = 'available' ORDER BY id ASC LIMIT ?", (cat_id, qty))
    items = cursor.fetchall()

    new_balance = user_balance - total_cost
    order_key = str(uuid.uuid4())[:8]
    sold_at = datetime.now(timezone.utc).isoformat()
    cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))

    for item_id, content in items:
        cursor.execute(
            "UPDATE stock SET status = 'sold', sold_by = ?, sold_at = ?, order_id = ? WHERE id = ? AND status = 'available'",
            (user_id, sold_at, order_key, item_id)
        )
        cursor.execute(
            "INSERT OR IGNORE INTO stock_sales (stock_id, cat_id, category_name, button_name, buyer_user_id, item_content, unit_price, order_id, sold_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, cat_id, cat_name, button_name, user_id, content, price_per_item, order_key, sold_at)
        )

    conn.commit()
    conn.close()

    raw_contents = [content.strip() for item_id, content in items]
    order_delivery_cache[order_key] = {
        "items": raw_contents,
        "cat_name": cat_name,
        "qty": qty,
        "cat_id": cat_id
    }

    delivery_markup = InlineKeyboardMarkup(row_width=2)
    delivery_markup.add(
        premium_inline_button("Open Here", "open_here_btn", callback_data=f"show_open_{order_key}"),
        premium_inline_button("TXT File", "txt_file_btn", callback_data=f"show_txt_{order_key}")
    )

    purchase_msg = (
        f"{pe('buy_success')} <b>Purchase Successful!</b>\n\n"
        f"{pe('box_package')} Item: <b>{cat_name}</b>\n"
        f"{pe('quantity_icon')} Quantity: <b>{qty} Pcs</b>\n"
        f"{pe('money_spent')} Total Cost: <b>৳{total_cost}</b>\n"
        f"{pe('wallet_balance')} Remaining Balance: <b>৳{new_balance}</b>\n\n"
        f"{pe('choose_select')} <b>আপনার ফাইলটি বট থেকে নিতে চাচ্ছেন নাকি TXT ফাইল আকারে নিতে চাচ্ছেন সিলেক্ট করুন:</b>"
    )

    bot.send_message(chat_id, purchase_msg, parse_mode="HTML", reply_markup=delivery_markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    update_owner_premium_status(call.from_user)
    user_id = call.from_user.id
    username = f"@{call.from_user.username}" if call.from_user.username else "N/A"
    save_user_id_to_file(user_id)

    if call.data.startswith("show_open_"):
        order_key = call.data.replace("show_open_", "")
        if order_key in order_delivery_cache:
            data = order_delivery_cache[order_key]
            items = data["items"]
            cat_name = data["cat_name"]
            formatted_items = [f"{idx}. <code>{content}</code>" for idx, content in enumerate(items, 1)]
            open_msg = f"{pe('buy_success')} <b>{cat_name} (Total: {len(items)} Pcs)</b>\n\n{pe('copy_down')} <b>ক্লিক করে কপি করুন:</b>\n\n" + "\n\n".join(formatted_items)
            bot.send_message(call.message.chat.id, open_msg, parse_mode="HTML")
            bot.answer_callback_query(call.id, "অ্যাকাউন্টগুলো নিচে ওপেন হয়েছে!")
        else:
            bot.answer_callback_query(call.id, "এই অর্ডারের সেশনটি মেয়াদোত্তীর্ণ হয়ে গেছে।", show_alert=True)
        return

    elif call.data.startswith("show_txt_"):
        order_key = call.data.replace("show_txt_", "")
        if order_key in order_delivery_cache:
            data = order_delivery_cache[order_key]
            items, cat_name, cat_id = data["items"], data["cat_name"], data["cat_id"]
            file_path = f"{cat_id}_order.txt"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(items))
            with open(file_path, "rb") as f:
                bot.send_document(call.message.chat.id, f, caption=f"{pe('txt_file_caption')} <b>{cat_name}</b> এর টেক্সট ফাইল নিচে দেওয়া হলো:", parse_mode="HTML")
            os.remove(file_path)
            bot.answer_callback_query(call.id, "ফাইল পাঠানো হয়েছে!")
        else:
            bot.answer_callback_query(call.id, "এই অর্ডারের সেশনটি মেয়াদোত্তীর্ণ হয়ে গেছে।", show_alert=True)
        return

    if call.data == "verify_subscription":
        if check_user_subscription(user_id):
            conn = db_connect("shop_bot.db", timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO users (user_id, first_name, balance, username) VALUES (?, ?, 0.0, ?)", (user_id, call.from_user.first_name or "User", username))
            else:
                cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            conn.commit()
            conn.close()

            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass

            bot.send_message(call.message.chat.id, f"{pe('step_tick')} আপনার একাউন্ট সফলভাবে Verify হয়েছে!\nএখন আপনি বটের সকল ফিচার ও কেনাকাটা করতে পারবেন। নিচে আপনার মেনু দেওয়া হলো:", parse_mode="HTML", reply_markup=get_permanent_keyboard(user_id))
        else:
            bot.send_message(call.message.chat.id, f"{pe('warn_icon')} আপনি এখনো চ্যানেলে Join করেননি! আগে চ্যানেলে জয়েন করুন তারপর Verify ক্লিক করুন।", parse_mode="HTML")
        return

    if not check_user_subscription(user_id):
        bot.send_message(call.message.chat.id, f"{pe('warn_icon')} আগে আমাদের চ্যানেলে Join করুন এবং Verify করুন!", parse_mode="HTML", reply_markup=get_force_sub_markup())
        return

    if call.data == "all_services":
        markup = get_categories_markup()
        try:
            bot.edit_message_text(f"{pe('welcome_shop')} <b>আমাদের কাছে নিচের সার্ভিসগুলো রয়েছে:</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, f"{pe('welcome_shop')} <b>আমাদের কাছে নিচের সার্ভিসগুলো রয়েছে:</b>", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "group_mail":
        conn = db_connect("shop_bot.db", timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT cat_id, name, price, has_emoji FROM categories WHERE group_type = 'mail' AND is_active = 1 "
                       "ORDER BY CASE cat_id WHEN 'hotmail' THEN 1 WHEN 'outlook' THEN 2 WHEN 'Outlook fr' THEN 3 ELSE 99 END, created_at, name COLLATE NOCASE")
        mail_cats = cursor.fetchall()
        conn.close()

        markup = InlineKeyboardMarkup(row_width=1)
        for cat_id, name, price, has_em in mail_cats:
            sub_conn = db_connect("shop_bot.db", timeout=30)
            sub_cur = sub_conn.cursor()
            sub_cur.execute("SELECT COUNT(*) FROM stock WHERE cat_id = ? AND status = 'available'", (cat_id,))
            stock_count = sub_cur.fetchone()[0]
            sub_conn.close()
            em_key = get_category_button_emoji_key(cat_id, name, "mail", has_em)
            markup.add(premium_inline_button(f"{name} - ৳{price} (Stock: {stock_count})", em_key, callback_data=f"buy_{cat_id}"))
        markup.add(premium_inline_button("Back", "back_button", callback_data="all_services"))

        try:
            bot.edit_message_text(f"{pe('mail_cat')} <b>Mail Service</b> {pe('mail_cat')}\n\nনিচের মেইলগুলো থেকে আপনার পছন্দমতো সিলেক্ট করুন:", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, f"{pe('mail_cat')} <b>Mail Service</b> {pe('mail_cat')}\n\nনিচের মেইলগুলো থেকে আপনার পছন্দমতো সিলেক্ট করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "group_proxy":
        conn = db_connect("shop_bot.db", timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT cat_id, name, price, has_emoji FROM categories WHERE group_type = 'proxy' AND is_active = 1 "
                       "ORDER BY CASE cat_id WHEN 'proxy_9_100' THEN 1 WHEN 'proxy' THEN 2 WHEN 'proxy_711_100' THEN 3 "
                       "WHEN 'proxy_711_200' THEN 4 WHEN 'proxy_h143_100' THEN 5 WHEN 'proxy_h143_200' THEN 6 "
                       "WHEN 'proxy_rapid_100' THEN 7 WHEN 'proxy_rapid_200' THEN 8 WHEN 'proxy_loki_100' THEN 9 ELSE 99 END, created_at, name COLLATE NOCASE")
        proxy_cats = cursor.fetchall()
        conn.close()

        markup = InlineKeyboardMarkup(row_width=1)
        for cat_id, name, price, has_em in proxy_cats:
            sub_conn = db_connect("shop_bot.db", timeout=30)
            sub_cur = sub_conn.cursor()
            sub_cur.execute("SELECT COUNT(*) FROM stock WHERE cat_id = ? AND status = 'available'", (cat_id,))
            stock_count = sub_cur.fetchone()[0]
            sub_conn.close()
            em_key = get_category_button_emoji_key(cat_id, name, "proxy", has_em)
            markup.add(premium_inline_button(f"{name} - ৳{price} (Stock: {stock_count})", em_key, callback_data=f"buy_{cat_id}"))
        markup.add(premium_inline_button("Back", "back_button", callback_data="all_services"))

        try:
            bot.edit_message_text(f"{pe('proxy_cat')} <b>Proxy Service</b> {pe('proxy_cat')}\n\nনিচের প্রক্সিগুলো থেকে আপনার পছন্দমতো সিলেক্ট করুন:", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, f"{pe('proxy_cat')} <b>Proxy Service</b> {pe('proxy_cat')}\n\nনিচের প্রক্সিগুলো থেকে আপনার পছন্দমতো সিলেক্ট করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "special_cat_telegram_premium":
        conn = db_connect("shop_bot.db", timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT sub_id, sub_name, price, has_emoji FROM sub_services WHERE cat_id = 'telegram_premium'")
        sub_items = cursor.fetchall()
        conn.close()

        markup = InlineKeyboardMarkup(row_width=1)
        for sub_id, sub_name, price, has_em in sub_items:
            em_key = get_sub_service_button_emoji_key(sub_id, has_em, "telegram_premium_cat")
            markup.add(premium_inline_button(f"{sub_name} - ৳{price}", em_key, callback_data=f"subbuy_{sub_id}"))
        markup.add(premium_inline_button("Back", "back_button", callback_data="all_services"))

        try:
            bot.edit_message_text(f"{pe('telegram_premium_cat')} <b>Telegram Premium Buy</b> {pe('telegram_premium_cat')}\n\nনিচের প্যাকেজগুলো থেকে আপনার পছন্দমতো সিলেক্ট করুন:", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, f"{pe('telegram_premium_cat')} <b>Telegram Premium Buy</b> {pe('telegram_premium_cat')}\n\nনিচের প্যাকেজগুলো থেকে আপনার পছন্দমতো সিলেক্ট করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "special_cat_vpn_service":
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            premium_inline_button("3 Day", "vpn_service_cat", callback_data="vpn_dur_vpn_3d"),
            premium_inline_button("7 Day", "vpn_service_cat", callback_data="vpn_dur_vpn_7d"),
            premium_inline_button("14 Day", "vpn_service_cat", callback_data="vpn_dur_vpn_14d"),
            premium_inline_button("30 Day", "vpn_service_cat", callback_data="vpn_dur_vpn_30d"),
            premium_inline_button("Back", "back_button", callback_data="all_services")
        )
        try:
            bot.edit_message_text(f"{pe('vpn_service_cat')} <b>VPN Service</b> {pe('vpn_service_cat')}\n\nআপনি কতদিনের জন্য VPN নিতে চাচ্ছেন মেয়াদের ক্যাটাগরি সিলেক্ট করুন:", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, f"{pe('vpn_service_cat')} <b>VPN Service</b> {pe('vpn_service_cat')}\n\nআপনি কতদিনের জন্য VPN নিতে চাচ্ছেন মেয়াদের ক্যাটাগরি সিলেক্ট করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("vpn_dur_"):
        dur_cat = call.data.replace("vpn_dur_", "")
        conn = db_connect("shop_bot.db", timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT sub_id, sub_name, price, has_emoji FROM sub_services WHERE cat_id = ?", (dur_cat,))
        sub_items = cursor.fetchall()
        conn.close()

        dur_text_map = {
            "vpn_3d": "৩ দিন মেয়াদের VPN গুলো নিচে দেওয়া হলো:",
            "vpn_7d": "৭ দিন মেয়াদের VPN গুলো নিচে দেওয়া হলো:",
            "vpn_14d": "১৪ দিন মেয়াদের VPN গুলো নিচে দেওয়া হলো:",
            "vpn_30d": "৩০ দিন মেয়াদের VPN গুলো নিচে দেওয়া হলো:"
        }
        dynamic_heading = dur_text_map.get(dur_cat, "নিচের VPN গুলো থেকে আপনার পছন্দের সার্ভিসটি সিলেক্ট করুন:")

        if not sub_items:
            bot.answer_callback_query(call.id, "এই মেয়াদের ভিপিএন বর্তমানে স্টকে নেই!", show_alert=True)
            return

        markup = InlineKeyboardMarkup(row_width=1)
        for sub_id, sub_name, price, has_em in sub_items:
            em_key = get_sub_service_button_emoji_key(sub_id, has_em, get_vpn_emoji_key(sub_name))
            markup.add(premium_inline_button(f"{sub_name} - ৳{price}", em_key, callback_data=f"subbuy_{sub_id}"))
        markup.add(premium_inline_button("Back", "back_button", callback_data="special_cat_vpn_service"))

        try:
            bot.edit_message_text(f"{pe('vpn_service_cat')} <b>VPN Packages</b>\n\n{dynamic_heading}", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, f"{pe('vpn_service_cat')} <b>VPN Packages</b>\n\n{dynamic_heading}", parse_mode="HTML", reply_markup=markup)
        return

    if call.data == "dep_custom_cancel":
        if user_id in user_temp_deposit and user_temp_deposit[user_id].get("step") in ("waiting_amount", "waiting_custom_amount", "choose_method", "waiting_emon_trx", "waiting_trx"):
            del user_temp_deposit[user_id]
        try: bot.answer_callback_query(call.id, "Cancelled")
        except Exception: pass
        try:
            bot.edit_message_text("❌ Deposit cancelled.", call.message.chat.id, call.message.message_id)
        except Exception:
            bot.send_message(call.message.chat.id, "❌ Deposit cancelled.", reply_markup=get_permanent_keyboard(user_id))
        return

    if call.data in _EMON_METHODS or call.data == "dep_method_binance":
        pending_state = user_temp_deposit.get(user_id)
        if not pending_state or pending_state.get("step") != "choose_method" or not pending_state.get("amount"):
            # Already handled (double tap) or expired: do not touch the existing message.
            try: bot.answer_callback_query(call.id, "Session expired. Please open Deposit again.", show_alert=True)
            except Exception: pass
            return
        try: bot.answer_callback_query(call.id)
        except Exception: pass
        dep_amount = pending_state["amount"]
        del user_temp_deposit[user_id]
        if call.data == "dep_method_binance":
            create_binance_invoice(call.message.chat.id, user_id, dep_amount, call.message.message_id)
        else:
            create_emonpay_invoice(
                call.message.chat.id, user_id,
                " ".join(p for p in (call.from_user.first_name, call.from_user.last_name) if p) or "Telegram User",
                dep_amount, call.message.message_id, _EMON_METHODS[call.data]
            )
        return

    if call.data.startswith("depamt_"):
        try: bot.answer_callback_query(call.id)
        except Exception: pass
        amount_key = call.data.split("_", 1)[1]
        if amount_key == "custom":
            user_temp_deposit[user_id] = {"step": "waiting_custom_amount"}
            prompt = "💵 <b>Custom Amount</b>\n\nPlease enter the amount you want to deposit (Minimum: 10 BDT | Maximum: 10,000 BDT):"
            try:
                bot.edit_message_text(prompt, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=get_custom_deposit_cancel_markup())
            except Exception:
                bot.send_message(call.message.chat.id, prompt, parse_mode="HTML", reply_markup=get_custom_deposit_cancel_markup())
            return
        try:
            amt = float(amount_key)
        except ValueError:
            return
        if not (10 <= amt <= 10000):
            return
        show_deposit_method_choice(call.message.chat.id, user_id, amt, call.message.message_id)
        return

    if call.data.startswith("paymethod_"):
        method = call.data.split("_")[1]
        _st = user_temp_deposit.get(user_id)
        if not _st or _st.get("step") != "choose_method" or not _st.get("amount"):
            try: bot.answer_callback_query(call.id, "Session expired. Please open Deposit again.", show_alert=True)
            except Exception: pass
            return
        try: bot.answer_callback_query(call.id)
        except Exception: pass
        amt = _st["amount"]
        user_temp_deposit[user_id] = {
            "amount": amt,
            "method": method,
            "step": "waiting_trx"
        }
        
        if method == "Binance":
            binance_uid = get_setting_msg('binance_uid', '1123474021')
            dollar_amount = amt / 125.0
            msg = (
                f"{pe('binance_method')} আপনি <b>Binance</b> সিলেক্ট করেছেন।\n\n"
                f"<tg-emoji emoji-id=\"5463424023734014980\">🪙</tg-emoji> এই Binance User ID-তে ডলার সেন্ড করুন:\n"
                f"<tg-emoji emoji-id=\"5798751138647905130\">➡️</tg-emoji>Pay UID: <code>{binance_uid}</code>\n\n"
                f"{pe('deposit_money')} Send: <b>${dollar_amount:.2f} Binance USD</b> (৳{int(amt) if amt.is_integer() else amt})\n"
                f"<i>(১ ডলার = ১২৫ টাকা অনুযায়ী নির্ধারিত)</i>\n\n"
                f"{pe('trx_input')} ডলার পাঠানোর পর আপনার ট্রানজেকশন আইডি (Transaction ID) চ্যাটে লিখে পাঠান:"
            )
        else:
            number_map = {
                "bKash": get_setting_msg('bkash_num', '+8801842145918'),
                "Nagad": get_setting_msg('nagad_num', '+8801842145918'),
                "Rocket": get_setting_msg('rocket_num', '+8801842145918')
            }
            pay_num = number_map.get(method, "+8801842145918")
            method_pe_key = "bkash_method" if method == "bKash" else ("nagad_method" if method == "Nagad" else "rocket_method")

            msg = (
                f"{pe(method_pe_key)} আপনি <b>{method}</b> সিলেক্ট করেছেন।\n\n"
                f"{pe('sendmoney_shield')} পার্সোনাল নাম্বারে টাকা সেন্ড মানি করুন:\n"
                f"{pe(method_pe_key)} {method}: <code>{pay_num}</code>\n\n"
                f"{pe('deposit_money')} <b>{int(amt) if amt.is_integer() else amt} TK Send Money</b>\n\n"
                f"{pe('trx_input')} টাকা পাঠানোর পর আপনার ট্রানজেকশন আইডি (Transaction ID) চ্যাটে লিখে পাঠান:"
            )
        bot.send_message(call.message.chat.id, msg, parse_mode="HTML")
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        return

    if call.data.startswith("admin_stats_") and user_id == ADMIN_ID:
        period = call.data.replace("admin_stats_", "", 1)
        try: bot.answer_callback_query(call.id)
        except Exception: pass
        try:
            stats_text, stats_markup = build_admin_stats(period if period in _STATS_PERIODS else "today")
        except Exception as e:
            print(f"Stats dashboard error: {type(e).__name__}: {e}")
            bot.send_message(call.message.chat.id, "⚠️ Dashboard লোড করা যায়নি, একটু পরে আবার চেষ্টা করুন।")
            return
        try:
            bot.edit_message_text(stats_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=stats_markup)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.send_message(call.message.chat.id, stats_text, parse_mode="HTML", reply_markup=stats_markup)
        return

    # Telegram Member Sell callbacks are deliberately handled before the existing
    # generic service/admin callback code so the other four services remain unchanged.
    if call.data == "member_sell":
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            premium_inline_button("30 Day Members", MEMBER_SELL_EMOJIS["duration"], callback_data="ms_duration_30"),
            premium_inline_button("3 Month Members", MEMBER_SELL_EMOJIS["duration"], callback_data="ms_duration_90"),
            premium_inline_button("1 Year Members", MEMBER_SELL_EMOJIS["duration"], callback_data="ms_duration_365"),
            premium_inline_button("Back", "back_button", callback_data="all_services")
        )
        heading = (
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['service'])} <b>Telegram Member Sell</b>\n\n"
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['menu_description'])} আপনার প্রয়োজন অনুযায়ী মেম্বার সার্ভিসের মেয়াদ সিলেক্ট করুন।"
        )
        try:
            bot.edit_message_text(heading, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, heading, parse_mode="HTML", reply_markup=markup)
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("ms_duration_"):
        duration_key = call.data.replace("ms_duration_", "", 1)
        plan = MEMBER_SELL_PLANS.get(duration_key)
        if not plan:
            bot.answer_callback_query(call.id, "সার্ভিসটি পাওয়া যায়নি।", show_alert=True)
            return

        markup = InlineKeyboardMarkup(row_width=1)
        for quantity, price in plan["packages"]:
            qty_label = f"{quantity // 1000}k" if quantity >= 1000 else str(quantity)
            markup.add(premium_inline_button(
                f"{qty_label} Members = {price} Tk",
                MEMBER_SELL_EMOJIS["service"],
                callback_data=f"ms_package_{duration_key}_{quantity}"
            ))
        markup.add(premium_inline_button("Back", "back_button", callback_data="member_sell"))

        heading = (
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['duration'])} <b>{html.escape(plan['heading'])}</b>\n"
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['delivery'])} <b>Delivery Time - {html.escape(plan['delivery'])}</b>\n"
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['fast'])} <b>Fast Delivery and Best Member</b>\n"
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['down'])} <b>নিচের সার্ভিসে ক্লিক করুন</b>"
        )
        try:
            bot.edit_message_text(heading, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(call.message.chat.id, heading, parse_mode="HTML", reply_markup=markup)
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("ms_package_"):
        parts = call.data.split("_")
        if len(parts) != 4:
            bot.answer_callback_query(call.id, "প্যাকেজের তথ্য সঠিক নয়।", show_alert=True)
            return
        duration_key, quantity_text = parts[2], parts[3]
        plan = MEMBER_SELL_PLANS.get(duration_key)
        try:
            quantity = int(quantity_text)
            price = next((amount for count, amount in plan["packages"] if count == quantity), None) if plan else None
        except (TypeError, ValueError):
            price = None
        if price is None:
            bot.answer_callback_query(call.id, "এই প্যাকেজটি পাওয়া যায়নি।", show_alert=True)
            return

        member_sell_states[user_id] = {
            "step": "waiting_target_link",
            "duration_key": duration_key,
            "duration_label": plan["label"],
            "quantity": quantity,
            "price": float(price),
        }
        qty_label = f"{quantity // 1000}k" if quantity >= 1000 else str(quantity)
        prompt = (
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['service'])} <b>আপনার প্যাকেজ:</b> {html.escape(plan['label'])} Members\n"
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['prompt_members'])} <b>মেম্বার:</b> {qty_label} জন\n"
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['prompt_price'])} <b>মূল্য:</b> ৳{price}\n\n"
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS['prompt_target'])} আপনি যে চ্যানেল বা গ্রুপে মেম্বার নিতে চাচ্ছেন, সেই চ্যানেল/গ্রুপের Username অথবা Link এখানে পেস্ট করুন।"
        )
        try:
            bot.edit_message_text(prompt, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, prompt, parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("ms_complete_") and user_id == ADMIN_ID:
        order_id = call.data.replace("ms_complete_", "", 1)
        conn = db_connect("shop_bot.db", timeout=30)
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT user_id, status FROM member_sell_orders WHERE order_id = ?", (order_id,))
        row = cur.fetchone()
        if not row or row[1] != "pending":
            conn.rollback()
            conn.close()
            bot.answer_callback_query(call.id, "এই অর্ডারটি ইতিমধ্যে প্রসেস করা হয়েছে।", show_alert=True)
            return
        cur.execute(
            "UPDATE member_sell_orders SET status = 'delivering' WHERE order_id = ? AND status = 'pending'",
            (order_id,)
        )
        if cur.rowcount != 1:
            conn.rollback()
            conn.close()
            bot.answer_callback_query(call.id, "অর্ডারটি প্রসেস করা যাচ্ছে না।", show_alert=True)
            return
        conn.commit()
        conn.close()
        admin_states[user_id] = {"action": "waiting_member_sell_delivery", "order_id": order_id, "target_user": row[0]}
        try:
            bot.edit_message_text(
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} <b>ডেলিভারি প্রস্তুত হচ্ছে</b>\nOrder ID: <code>{html.escape(order_id)}</code>",
                call.message.chat.id, call.message.message_id, parse_mode="HTML"
            )
        except Exception:
            pass
        bot.send_message(
            call.message.chat.id,
            f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} অর্ডার <code>{html.escape(order_id)}</code>-এর জন্য যে ডেলিভারি মেসেজটি কাস্টমারের কাছে পাঠাতে চান, সেটি লিখে পাঠান:",
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id, "ডেলিভারি মেসেজের অপেক্ষায় আছে।")
        return

    if call.data.startswith("ms_refund_") and user_id == ADMIN_ID:
        order_id = call.data.replace("ms_refund_", "", 1)
        conn = db_connect("shop_bot.db", timeout=30)
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "SELECT user_id, price, status FROM member_sell_orders WHERE order_id = ?",
            (order_id,)
        )
        row = cur.fetchone()
        if not row or row[2] != "pending":
            conn.rollback()
            conn.close()
            bot.answer_callback_query(call.id, "এই অর্ডারটি ইতিমধ্যে প্রসেস করা হয়েছে।", show_alert=True)
            return
        target_user, refund_amount, status = row
        cur.execute(
            "UPDATE member_sell_orders SET status = 'refunded', refunded_at = ? WHERE order_id = ? AND status = 'pending'",
            (datetime.now(timezone.utc).isoformat(), order_id)
        )
        if cur.rowcount != 1:
            conn.rollback()
            conn.close()
            bot.answer_callback_query(call.id, "রিফান্ডটি সম্পন্ন করা যায়নি; আবার যাচাই করুন।", show_alert=True)
            return
        cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (refund_amount, target_user))
        conn.commit()
        conn.close()

        try:
            bot.send_message(
                target_user,
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['refund_notice'])} <b>দুঃখিত, বর্তমানে এই সার্ভিসটি দেওয়া যাচ্ছে না। আপনি অন্য কিছু ট্রাই করেন।</b>\n\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['refund_amount'])} আপনার কাটা ৳{refund_amount:g} টাকা আপনার অ্যাকাউন্টে ফেরত দেওয়া হয়েছে।",
                parse_mode="HTML",
                reply_markup=get_permanent_keyboard(target_user)
            )
        except Exception as e:
            bot.send_message(ADMIN_ID, f"Refund completed, but user notification failed (User ID: <code>{target_user}</code>): <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        try:
            bot.edit_message_text(
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['refund_notice'])} <b>Telegram Member Sell — No Delivery / Refunded</b>\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['success_order_id'])} Order ID: <code>{html.escape(order_id)}</code>\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['refund_amount'])} Refund: ৳{refund_amount:g}",
                call.message.chat.id, call.message.message_id, parse_mode="HTML"
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "অর্ডার রিফান্ড করা হয়েছে।", show_alert=True)
        return

    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()

    if call.data == "admin_main":
        if user_id in admin_states: del admin_states[user_id]
        conn.close()
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('admin_crown')} <b>Admin Control Panel</b>\n\nনিচের অপশনগুলো থেকে ম্যানেজ করুন:", parse_mode="HTML", reply_markup=get_admin_markup())
        return

    elif call.data == "admin_auto_member_count" and user_id == ADMIN_ID:
        cursor.execute("SELECT user_id FROM users")
        db_users = cursor.fetchall()
        conn.close()
        
        all_unique_ids = set()
        for u in db_users:
            all_unique_ids.add(str(u[0]))
            
        if os.path.exists(USER_LOG_FILE):
            with open(USER_LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    cleaned = line.strip()
                    if cleaned: all_unique_ids.add(cleaned)
                    
        with open(USER_LOG_FILE, "w", encoding="utf-8") as f:
            for uid in sorted(all_unique_ids):
                f.write(f"{uid}\n")
                
        if all_unique_ids:
            with open(USER_LOG_FILE, "rb") as f:
                bot.send_document(call.message.chat.id, f, caption=f"{pe('box_package')} <b>auto_member_count_user.txt</b>\nমোট মেম্বার সংখ্যা: <b>{len(all_unique_ids)} জন</b>", parse_mode="HTML")
            bot.answer_callback_query(call.id, "ফাইল পাঠানো হয়েছে!")
        else:
            bot.send_message(call.message.chat.id, f"{pe('warn_icon')} এখনো কোনো মেম্বারের ইউজার আইডি সেভ হয়নি।", parse_mode="HTML")
        return

    elif call.data == "admin_broadcast" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_direct_broadcast_message"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(
            call.message.chat.id,
            f"{pe('loudspeaker')} <b>Broadcast Message</b>\n\nআপনি যে মেসেজ/ছবি/ভিডিও/ফাইল/ভয়েস পাঠাবেন, সেটাই সরাসরি সকল registered user-এর কাছে পাঠানো হবে।",
            parse_mode="HTML",
            reply_markup=markup
        )
        return

    elif call.data == "admin_premium_emoji_set" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_premium_emoji_txt"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(
            call.message.chat.id,
            f"{pe('button_edit')} <b>Premium Emoji Set</b>\n\nএকটি <code>.txt</code> ফাইল upload করুন অথবা সরাসরি এই ফরম্যাটে লিখে পাঠান: <code>🔴 = 5788939937335366</code>",
            parse_mode="HTML",
            reply_markup=markup
        )
        return

    elif call.data == "admin_payment_method_edit" and user_id == ADMIN_ID:
        conn.close()
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            premium_inline_button("Edit bKash", "edit_service", callback_data="editpay_bkash"),
            premium_inline_button("Edit Nagad", "edit_service", callback_data="editpay_nagad"),
            premium_inline_button("Edit Rocket", "edit_service", callback_data="editpay_rocket"),
            premium_inline_button("Edit Binance", "edit_service", callback_data="editpay_binance"),
            premium_inline_button("Back", "back_button", callback_data="admin_main")
        )
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('edit_service')} <b>Payment Method Edit</b>\nকোন পেমেন্ট মেথড এডিট করতে চান সিলেক্ট করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("editpay_") and user_id == ADMIN_ID:
        pmethod = call.data.replace("editpay_", "")
        conn.close()
        admin_states[user_id] = {"action": "saving_payment_number", "method": pmethod}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_payment_method_edit"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('edit_pencil')} <b>{pmethod.upper()}</b> এর জন্য নতুন নাম্বার বা ইউআইডি (UID) লিখে পাঠান:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_pc_directory_setup" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_pc_directory_path"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('download_txt')} পিসির ফোল্ডার পাথ (Directory Path) লিখে পাঠান যেখানে এনালাইসিস ফাইল সেভ হবে:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_pc_live_money_setup" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_pc_live_money_path"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('download_txt')} পিসির লাইভ মানি ফোল্ডার পাথ লিখে পাঠান:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_pc_live_stock_setup" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_pc_live_stock_path"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('download_txt')} পিসির লাইভ স্টক ফোল্ডার পাথ লিখে পাঠান:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_live_broadcast" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_analysis_txt_for_broadcast"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('loudspeaker')} <b>Live Analysis & User Broadcast Message</b>\n\nবট এনালাইসিস বা <code>auto_member_count_user.txt</code> ফাইলটি সরাসরি এখানে ফাইল আকারে পাঠান:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_add_member_balance" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_member_uid_for_balance"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('add_balance')} <b>Add Member Money Back</b>\n\nযে ইউজারের অ্যাকাউন্টে টাকা ব্যাক দিতে চাচ্ছেন, তার সঠিক <b>User ID</b> চ্যাটে লিখে পাঠান:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_remove_member_balance" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_member_uid_for_remove_money"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('remove_money')} <b>Remove Money</b>\n\nযে ইউজারের অ্যাকাউন্ট থেকে টাকা কাট বা এডিট করতে চান, তার সঠিক <b>User ID</b> চ্যাটে লিখে পাঠান:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_bulk_money_update" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_bulk_money_data"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('bulk_update')} <b>Update Back All Money Member</b>\n\nআপনার আগের ডাউনলোড করা ব্যালেন্স লিস্ট ফাইলটি এখানে আপলোড করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("subbuy_"):
        sub_id = call.data.split("_")[1]
        cursor.execute("SELECT cat_id, sub_name, price FROM sub_services WHERE sub_id = ?", (sub_id,))
        sub_data = cursor.fetchone()
        conn.close()

        if not sub_data:
            bot.send_message(call.message.chat.id, f"{pe('warn_icon')} দুঃখিত, এই প্যাকেজটি পাওয়া যায়নি।", parse_mode="HTML")
            return

        cat_id, sub_name, price = sub_data
        conn = db_connect("shop_bot.db", timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        bal_res = cursor.fetchone()
        user_bal = bal_res[0] if bal_res else 0.0
        conn.close()

        if user_bal < price:
            bot.send_message(call.message.chat.id, f"{pe('insufficient_bal')} পর্যাপ্ত ব্যালেন্স নেই! প্রয়োজন ৳{price}, আপনার আছে ৳{user_bal}।", parse_mode="HTML")
            return

        user_temp_deposit[user_id] = {"step": "waiting_service_username", "cat_id": cat_id, "sub_name": sub_name, "price": price}
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass

        emoji_k = "telegram_premium_cat" if "telegram" in cat_id else get_vpn_emoji_key(sub_name)
        bot.send_message(call.message.chat.id, f"{pe(emoji_k)} আপনি সিলেক্ট করেছেন: <b>{sub_name}</b> (মূল্য: ৳{price}) {pe(emoji_k)}\n\n{pe('list_point')} এখন যে আইডিতে সার্ভিস নিতে চাচ্ছেন, সেটির সঠিক <b>Telegram Username</b> বা প্রোফাইল আইডি চ্যাটে লিখে পাঠান:", parse_mode="HTML")
        return

    elif call.data.startswith("buy_"):
        cat_id = call.data.replace("buy_", "")
        cursor.execute("SELECT name, price, group_type FROM categories WHERE cat_id = ? AND is_active = 1", (cat_id,))
        cat = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM stock WHERE cat_id = ? AND status = 'available'", (cat_id,))
        stock_count_res = cursor.fetchone()
        stock_count = stock_count_res[0] if stock_count_res else 0
        conn.close()

        if stock_count == 0 or not cat:
            bot.send_message(call.message.chat.id, f"{pe('no_stock')} দুঃখিত! এই মুহূর্তে এই আইটেমের স্টক শেষ।", parse_mode="HTML")
            return

        back_target = f"group_{cat[2]}" if cat[2] in ('mail', 'proxy') else "all_services"

        user_temp_deposit[user_id] = {"step": "waiting_custom_qty", "cat_id": cat_id}
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            premium_inline_button("1 Pcs", "qty_select", callback_data=f"qty_{cat_id}_1"),
            premium_inline_button("5 Pcs", "qty_select", callback_data=f"qty_{cat_id}_5"),
            premium_inline_button("10 Pcs", "qty_select", callback_data=f"qty_{cat_id}_10"),
            premium_inline_button("Back", "back_button", callback_data=back_target)
        )
        msg_text = f"{pe('box_package')} <b>{cat[0]}</b>\n{pe('money_spent')} Price: ৳{cat[1]}\nStock: {stock_count} Pcs\n\nSelect quantity:\n{pe('list_point')} অধিক নিতে চাইলে নিচে চ্যাটে কাঙ্ক্ষিত পিস সংখ্যা লিখে পাঠান."
        try: bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception: bot.send_message(call.message.chat.id, msg_text, parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("qty_"):
        parts = call.data.split("_")
        qty_str, cat_id = parts[-1], "_".join(parts[1:-1])
        conn.close()
        if user_id in user_temp_deposit and user_temp_deposit[user_id].get("step") == "waiting_custom_qty":
            del user_temp_deposit[user_id]
        process_purchase(call.message.chat.id, user_id, cat_id, int(qty_str))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        return

    elif call.data.startswith("btn_emojichoice_") and user_id == ADMIN_ID:
        choice = call.data.split("_")[2]
        state_data = admin_states.get(user_id, {})
        pending_data = state_data.get("pending_btn_data")
        if not pending_data:
            conn.close()
            bot.answer_callback_query(call.id, "এডিট/অ্যাড সেশনটি পাওয়া যায়নি। আবার শুরু করুন।", show_alert=True)
            return

        if choice == "other":
            admin_states[user_id]["action"] = "waiting_other_emoji_code"
            bot.edit_message_text(
                f"{pe('edit_pencil')} অনুগ্রহ করে প্রিমিয়াম ইমোজির <b>Custom Emoji ID</b> চ্যাটে লিখে পাঠান:",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML"
            )
            conn.close()
            return

        if choice not in ("yes", "no"):
            conn.close()
            bot.answer_callback_query(call.id, "সঠিক Emoji Option নির্বাচন করুন।", show_alert=True)
            return

        try:
            saved_id = _save_pending_button(pending_data, has_emoji=(choice == "yes"))
            del admin_states[user_id]
            label = "প্রিমিয়াম ইমোজিসহ" if choice == "yes" else "ইমোজি ছাড়া"
            bot.send_message(
                call.message.chat.id,
                f"{pe('step_tick')} বাটনটি সফলভাবে {label} সেভ হয়েছে!\n"
                f"নাম: <b>{html.escape(str(pending_data.get('name', '')))}</b>\n"
                f"রেট: <b>৳{float(pending_data.get('price', 0)):g}</b>",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"Button save error: {e}")
            bot.send_message(call.message.chat.id, f"{pe('warn_icon')} বাটনটি সেভ করা যায়নি: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        conn.close()
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        return

    elif call.data == "admin_add_stock_menu" and user_id == ADMIN_ID:
        if user_id in admin_states: del admin_states[user_id]
        cursor.execute("SELECT cat_id, name FROM categories WHERE is_active = 1")
        categories = cursor.fetchall()
        conn.close()
        markup = InlineKeyboardMarkup(row_width=1)
        for cat_id, name in categories:
            sub_conn = db_connect("shop_bot.db", timeout=30)
            sub_cur = sub_conn.cursor()
            sub_cur.execute("SELECT COUNT(*) FROM stock WHERE cat_id = ? AND status = 'available'", (cat_id,))
            s_count = sub_cur.fetchone()[0]
            sub_conn.close()
            markup.add(premium_inline_button(f"{name} (Stock: {s_count} Pcs)", "manage_stock", callback_data=f"manage_stock_{cat_id}"))
        
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('box_package')} <b>Stock Management:</b>\nযে সার্ভিসের স্টক ম্যানেজ করতে চান তা সিলেক্ট করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("manage_stock_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("manage_stock_", "")
        cursor.execute("SELECT name FROM categories WHERE cat_id = ?", (cat_id,))
        cat_res = cursor.fetchone()
        cat_name = cat_res[0] if cat_res else cat_id

        cursor.execute("SELECT COUNT(*) FROM stock WHERE cat_id = ? AND status = 'available'", (cat_id,))
        stock_count = cursor.fetchone()[0]
        conn.close()

        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            premium_inline_button("Add New Stock File (.txt)", "add_balance", callback_data=f"select_stock_{cat_id}"),
            premium_inline_button("Download Current Stock (.txt)", "download_txt", callback_data=f"download_stock_{cat_id}"),
            premium_inline_button(f"Remove All Stock ({stock_count} Pcs)", "delete_trash", callback_data=f"clear_stock_{cat_id}"),
            premium_inline_button("Back", "back_button", callback_data="admin_add_stock_menu")
        )
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('box_package')} <b>Stock Management:</b> {cat_name}\nবর্তমান স্টক সংখ্যা: <b>{stock_count} পিস</b>", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("download_stock_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("download_stock_", "")
        cursor.execute("SELECT name FROM categories WHERE cat_id = ?", (cat_id,))
        cat_name = cursor.fetchone()[0]
        cursor.execute("SELECT content FROM stock WHERE cat_id = ? AND status = 'available' ORDER BY id ASC", (cat_id,))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            bot.answer_callback_query(call.id, "এই মুহূর্তে এই ক্যাটাগরিতে কোনো স্টক নেই!", show_alert=True)
            return

        file_content = "\n".join([r[0] for r in rows])
        file_path = f"stock_{cat_id}.txt"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_content)

        with open(file_path, "rb") as f:
            bot.send_document(call.message.chat.id, f, caption=f"{pe('box_package')} <b>{cat_name}</b> এর বর্তমান সমস্ত স্টক ফাইল:", parse_mode="HTML")
        os.remove(file_path)
        return

    elif call.data.startswith("clear_stock_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("clear_stock_", "")
        cursor.execute("UPDATE stock SET status = 'removed' WHERE cat_id = ? AND status = 'available'", (cat_id,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, "স্টক সফলভাবে মুছে ফেলা হয়েছে!", show_alert=True)

        markup = InlineKeyboardMarkup(row_width=1)
        sub_conn = db_connect("shop_bot.db", timeout=30)
        sub_cur = sub_conn.cursor()
        sub_cur.execute("SELECT cat_id, name FROM categories WHERE is_active = 1")
        categories = sub_cur.fetchall()
        for cid, name in categories:
            sub_cur.execute("SELECT COUNT(*) FROM stock WHERE cat_id = ? AND status = 'available'", (cid,))
            s_count = sub_cur.fetchone()[0]
            markup.add(premium_inline_button(f"{name} (Stock: {s_count} Pcs)", "manage_stock", callback_data=f"manage_stock_{cid}"))
        sub_conn.close()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))

        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('box_package')} স্টক মুছে ফেলা হয়েছে। অন্য সার্ভিস সিলেক্ট করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("select_stock_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("select_stock_", "")
        admin_states[user_id] = {"action": "waiting_stock_file", "cat_id": cat_id}
        cursor.execute("SELECT name FROM categories WHERE cat_id = ?", (cat_id,))
        cat_name = cursor.fetchone()[0]
        conn.close()
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data=f"manage_stock_{cat_id}"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('box_package')} আপনি সিলেক্ট করেছেন: <b>{cat_name}</b>\n\nএখন নতুন স্টক ফাইল (.txt) সরাসরি এই চ্যাটে ফাইল হিসেবে আপলোড করে পাঠান।", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_analytics" and user_id == ADMIN_ID:
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(balance) FROM users")
        res_sum = cursor.fetchone()
        total_user_balance = res_sum[0] if res_sum and res_sum[0] is not None else 0.0
        cursor.execute("SELECT user_id, first_name, balance FROM users ORDER BY user_id DESC")
        all_registered = cursor.fetchall()
        conn.close()

        analytics_text = (
            f"{pe('stats_chart')} <b>Live Bot Analytics & Statistics</b>\n\n"
            f"{pe('profile_user')} মোট জয়েনকৃত ইউজার: <b>{total_users} জন</b>\n"
            f"{pe('balance_coin')} ইউজারদের মোট একাউন্ট ব্যালেন্স: <b>৳{total_user_balance}</b>\n\n"
            f"{pe('list_point')} <b>সাম্প্রতিক ইউজার তালিকা:</b>\n"
        )
        for u_id, u_name, u_bal in all_registered[:15]:
            safe_name = u_name or "User"
            analytics_text += f"• {safe_name} (<code>{u_id}</code>) - ৳{u_bal}\n"

        markup = InlineKeyboardMarkup()
        markup.add(
            premium_inline_button("Download Bot Analysis TXT File", "download_txt", callback_data="admin_download_analytics_txt"),
            premium_inline_button("Refresh Live Data", "refresh_data", callback_data="admin_analytics"),
            premium_inline_button("Back", "back_button", callback_data="admin_main")
        )
        try: bot.edit_message_text(analytics_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception: pass

    elif call.data == "admin_download_analytics_txt" and user_id == ADMIN_ID:
        cursor.execute("SELECT user_id, first_name FROM users ORDER BY user_id DESC")
        all_users = cursor.fetchall()
        conn.close()

        if not all_users:
            bot.send_message(call.message.chat.id, f"{pe('warn_icon')} বোর্ডে কোনো ইউজারের ডাটা পাওয়া যায়নি!", parse_mode="HTML")
            return

        file_content = ""
        for u_id, u_name in all_users:
            safe_name = u_name or "User"
            file_content += f"{u_id} | {safe_name}\n"

        file_path = "bot_analytics_users.txt"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_content)

        with open(file_path, "rb") as f:
            bot.send_document(call.message.chat.id, f, caption=f"{pe('box_package')} Bot Analytics & User List (.txt)\nসকল ইউজারের আইডি দিয়ে তৈরি করা ফাইল:")
        os.remove(file_path)

    elif call.data == "admin_member_balance_list" and user_id == ADMIN_ID:
        cursor.execute("SELECT user_id, first_name, balance FROM users WHERE balance > 0 ORDER BY balance DESC")
        users_with_money = cursor.fetchall()
        conn.close()

        list_text = f"{pe('balance_coin')} <b>যেসব মেম্বারদের অ্যাকাউন্টে টাকা আছে তাদের তালিকা:</b>\n\n"
        if not users_with_money:
            list_text += f"{pe('warn_icon')} এই মুহূর্তে কারও অ্যাকাউন্টে ব্যালেন্স নেই."
        else:
            for idx, (u_id, u_name, u_bal) in enumerate(users_with_money[:20], 1):
                safe_name = u_name or "User"
                list_text += f"{idx}. {safe_name} | ID: <code>{u_id}</code> | ব্যালেন্স: <b>৳{u_bal}</b>\n"

        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            premium_inline_button("Download Balance List TXT", "download_txt", callback_data="admin_download_balance_txt"),
            premium_inline_button("Recover Balance List (.txt)", "recover_balance", callback_data="admin_recover_balance_list"),
            premium_inline_button("Back", "back_button", callback_data="admin_main")
        )
        try: bot.edit_message_text(list_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
        except Exception: pass

    elif call.data == "admin_download_balance_txt" and user_id == ADMIN_ID:
        cursor.execute("SELECT user_id, balance, first_name FROM users WHERE balance > 0 ORDER BY balance DESC")
        users_with_money = cursor.fetchall()
        conn.close()

        if not users_with_money:
            bot.send_message(call.message.chat.id, f"{pe('warn_icon')} এই মুহূর্তে কারও অ্যাকাউন্টে ব্যালেন্স নেই!", parse_mode="HTML")
            return

        file_content = ""
        for u_id, u_bal, u_name in users_with_money:
            file_content += f"{u_id} | {u_bal}\n"

        file_path = "all_member_balance_list.txt"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_content)

        with open(file_path, "rb") as f:
            bot.send_document(call.message.chat.id, f, caption=f"{pe('box_package')} All Member Balance List (.txt)\nব্যালেন্সধারী মেম্বারদের তালিকা ডাউনলোড হয়েছে:")
        os.remove(file_path)

    elif call.data == "admin_recover_balance_list" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "waiting_recover_balance_txt"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('recover_balance')} <b>Recover Balance List (.txt)</b>\n\nপূর্বে ডাউনলোড করা <code>all_member_balance_list.txt</code> ফাইলটি এখানে আপলোড করুন.", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data == "admin_all_post_edit" and user_id == ADMIN_ID:
        conn.close()
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            premium_inline_button("1. Edit Welcome Post", "post_edit", callback_data="edit_post_welcome_msg"),
            premium_inline_button("2. Edit Channel Not Joined Warning", "post_edit", callback_data="edit_post_not_joined_msg"),
            premium_inline_button("3. Edit Support Text", "post_edit", callback_data="edit_post_support_msg"),
            premium_inline_button("4. Edit Deposit Information Text", "post_edit", callback_data="edit_post_deposit_info_msg"),
            premium_inline_button("Back to Admin", "back_button", callback_data="admin_main")
        )
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('post_edit')} <b>All Post Edit</b>\n\nযেই পোস্টটি এডিট করতে চান সেটির ওপর ক্লিক করুন:", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("edit_post_") and user_id == ADMIN_ID:
        post_key = call.data.replace("edit_post_", "")
        conn.close()
        admin_states[user_id] = {"action": "waiting_generic_post_update", "post_key": post_key}
        current_text = get_setting_msg(post_key, "খালি")
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_all_post_edit"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('post_edit')} <b>Editing Post:</b> <code>{post_key}</code>\n\n<b>বর্তমান লেখা:</b>\n------------------\n{current_text}\n------------------\n\nনতুন লেখাটি পাঠান:", parse_mode="HTML", reply_markup=markup)

    elif call.data == "admin_all_button_edit" and user_id == ADMIN_ID:
        cursor.execute("SELECT cat_id, name FROM categories WHERE is_active = 1")
        categories = cursor.fetchall()
        cursor.execute("SELECT sub_id, sub_name FROM sub_services")
        sub_services = cursor.fetchall()
        conn.close()
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton("--- Main Categories ---", callback_data="ignore"))
        for cid, name in categories:
            markup.add(premium_inline_button(name, "edit_pencil", callback_data=f"editbtn_cat_{cid}"))
        if sub_services:
            markup.add(InlineKeyboardButton("--- Sub Packages / VPNs ---", callback_data="ignore"))
            for sid, sname in sub_services:
                markup.add(premium_inline_button(sname, "edit_pencil", callback_data=f"editbtn_sub_{sid}"))
        markup.add(premium_inline_button("Back to Admin", "back_button", callback_data="admin_main"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('button_edit')} <b>All Button Edit</b>\n\nযেই বাটনটি এডিট করতে চান সিলেক্ট করুন:", parse_mode="HTML", reply_markup=markup)

    elif call.data.startswith("editbtn_") and user_id == ADMIN_ID:
        parts = call.data.split("_")
        btn_type, btn_id = parts[1], "_".join(parts[2:])
        if btn_type == "cat":
            cursor.execute("SELECT name, price FROM categories WHERE cat_id = ? AND is_active = 1", (btn_id,))
        elif btn_type == "sub":
            cursor.execute("SELECT sub_name, price FROM sub_services WHERE sub_id = ?", (btn_id,))
        else:
            conn.close()
            bot.answer_callback_query(call.id, "এই বাটনটি এডিট করা যাচ্ছে না।", show_alert=True)
            return
        current_button = cursor.fetchone()
        conn.close()
        if not current_button:
            bot.answer_callback_query(call.id, "বাটনটি ডাটাবেজে পাওয়া যায়নি।", show_alert=True)
            return
        admin_states[user_id] = {"action": "waiting_generic_btn_update", "btn_type": btn_type, "btn_id": btn_id}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_all_button_edit"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(
            call.message.chat.id,
            f"{pe('edit_pencil')} এডিট করছেন: <b>{html.escape(str(current_button[0]))}</b> (বর্তমান রেট: ৳{current_button[1]})\n\n"
            f"নতুন নাম ও রেট লিখে পাঠান।\n<b>ফরম্যাট:</b> <code>[নতুন নাম] [নতুন রেট]</code>\n"
            f"<b>উদাহরণ:</b> <code>{html.escape(str(current_button[0]))} {current_button[1]}</code>",
            parse_mode="HTML", reply_markup=markup
        )

    elif call.data == "admin_edit_services" and user_id == ADMIN_ID:
        if user_id in admin_states: del admin_states[user_id]
        cursor.execute("SELECT cat_id, name, price FROM categories WHERE is_active = 1")
        categories = cursor.fetchall()
        conn.close()
        markup = InlineKeyboardMarkup(row_width=1)
        
        markup.add(
            premium_inline_button("Manage Sub-Buttons: Mail Service", "edit_service", callback_data="editsubs_group_mail"),
            premium_inline_button("Manage Sub-Buttons: Proxy Service", "edit_service", callback_data="editsubs_group_proxy"),
            premium_inline_button("Manage Sub-Buttons: Telegram Premium Buy", "edit_service", callback_data="editsubs_telegram_premium"),
            premium_inline_button("Manage Sub-Buttons: VPN Service", "edit_service", callback_data="editsubs_vpn_service")
        )
        
        special_ids = ["telegram_premium", "vpn_service"]
        regular_cats = [c for c in categories if c[0] not in special_ids and c[0] not in ['hotmail', 'outlook', 'Outlook fr', 'Ig Hotmail', 'proxy']]
        
        for cat_id, name, price in regular_cats:
            sub_conn = db_connect("shop_bot.db", timeout=30)
            sub_cursor = sub_conn.cursor()
            sub_cursor.execute("SELECT COUNT(*) FROM stock WHERE cat_id = ? AND status = 'available'", (cat_id,))
            stock_count = sub_cursor.fetchone()[0]
            sub_conn.close()
            markup.add(
                premium_inline_button(f"{name} | ৳{price} | Stock: {stock_count}", "edit_service", callback_data=f"editcat_{cat_id}"),
                premium_inline_button(f"Delete {name}", "delete_trash", callback_data=f"confirm_del_{cat_id}")
            )
        
        markup.add(
            premium_inline_button("Add New Button / Service", "add_balance", callback_data="admin_add_new_btn"),
            premium_inline_button("Back", "back_button", callback_data="admin_main")
        )
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('edit_service')} সার্ভিস এডিট বা ডিলিট করতে নিচের বাটনগুলোতে ক্লিক করুন:", parse_mode="HTML", reply_markup=markup)

    elif call.data.startswith("confirm_del_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("confirm_del_", "")
        cursor.execute("SELECT name FROM categories WHERE cat_id = ?", (cat_id,))
        cat_res = cursor.fetchone()
        cat_name = cat_res[0] if cat_res else "এই সার্ভিসটি"
        conn.close()

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            premium_inline_button("Yes (Delete)", "approve_btn", callback_data=f"delcat_{cat_id}"),
            premium_inline_button("No (Cancel)", "reject_btn", callback_data="admin_edit_services")
        )
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('warn_icon')} <b>সতর্কবার্তা!</b>\n\nআপনি কি সত্যি <b>{cat_name}</b> বাটনটি ডিলিট করতে চাচ্ছেন?", parse_mode="HTML", reply_markup=markup)
        return

    elif call.data.startswith("editsubs_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("editsubs_", "")
        if cat_id == "vpn_service":
            conn.close()
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                premium_inline_button("Manage 3 Day", "edit_service", callback_data="editsubs_vpn_3d"),
                premium_inline_button("Manage 7 Day", "edit_service", callback_data="editsubs_vpn_7d"),
                premium_inline_button("Manage 14 Day", "edit_service", callback_data="editsubs_vpn_14d"),
                premium_inline_button("Manage 30 Day", "edit_service", callback_data="editsubs_vpn_30d"),
                premium_inline_button("Back", "back_button", callback_data="admin_edit_services")
            )
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
            bot.send_message(call.message.chat.id, f"{pe('edit_service')} <b>VPN Service Sub-Packages Manage:</b>\nমেয়াদ অনুযায়ী প্যাকেজ ম্যানেজ করুন:", parse_mode="HTML", reply_markup=markup)
            return

        if cat_id in ("group_mail", "group_proxy"):
            g_type = 'mail' if cat_id == "group_mail" else 'proxy'
            cursor.execute("SELECT cat_id, name, price FROM categories WHERE group_type = ? AND is_active = 1", (g_type,))
            items = cursor.fetchall()
            conn.close()

            markup = InlineKeyboardMarkup(row_width=1)
            for c_id, c_name, c_price in items:
                markup.add(
                    premium_inline_button(f"Edit: {c_name} (৳{c_price})", "edit_service", callback_data=f"editcat_{c_id}"),
                    premium_inline_button(f"Delete: {c_name}", "delete_trash", callback_data=f"confirm_del_{c_id}")
                )
            
            markup.add(
                premium_inline_button("Add New Service / Button", "add_balance", callback_data="admin_add_new_btn"),
                premium_inline_button("Back", "back_button", callback_data="admin_edit_services")
            )
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
            bot.send_message(call.message.chat.id, f"{pe('edit_service')} <b>Manage {('Mail Service' if g_type=='mail' else 'Proxy Service')}:</b>", parse_mode="HTML", reply_markup=markup)
            return

        cursor.execute("SELECT sub_id, sub_name, price FROM sub_services WHERE cat_id = ?", (cat_id,))
        subs = cursor.fetchall()
        conn.close()

        markup = InlineKeyboardMarkup(row_width=1)
        for sub_id, sub_name, price in subs:
            markup.add(
                premium_inline_button(f"Edit: {sub_name} (৳{price})", "edit_service", callback_data=f"editsubitem_{sub_id}_{cat_id}"),
                premium_inline_button(f"Delete: {sub_name}", "delete_trash", callback_data=f"delsub_{sub_id}_{cat_id}")
            )
        
        markup.add(
            premium_inline_button("Add New Package", "add_balance", callback_data=f"addsub_{cat_id}"),
            premium_inline_button("Back", "back_button", callback_data="admin_edit_services")
        )
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('edit_service')} <b>Manage Sub-Buttons:</b>", parse_mode="HTML", reply_markup=markup)

    elif call.data.startswith("editsubitem_") and user_id == ADMIN_ID:
        parts = call.data.split("_")
        sub_id, cat_id = parts[1], parts[2]
        cursor.execute("SELECT sub_name, price FROM sub_services WHERE sub_id = ?", (sub_id,))
        sub_item = cursor.fetchone()
        conn.close()
        admin_states[user_id] = {"action": "waiting_generic_btn_update", "btn_type": "sub", "btn_id": sub_id}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data=f"editsubs_{cat_id}"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('edit_pencil')} এডিট করছেন: <b>{sub_item[0]}</b> (মূল্য: ৳{sub_item[1]})\n\nনতুন নাম ও রেট লিখে পাঠান:\n<b>ফরম্যাট:</b> <code>[নাম] [রেট]</code>", parse_mode="HTML", reply_markup=markup)

    elif call.data.startswith("delsub_") and user_id == ADMIN_ID:
        parts = call.data.split("_")
        sub_id, cat_id = parts[1], parts[2]
        cursor.execute("DELETE FROM sub_services WHERE sub_id = ?", (sub_id,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, "প্যাকেজ ডিলিট হয়েছে!")
        return

    elif call.data.startswith("addsub_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("addsub_", "")
        conn.close()
        admin_states[user_id] = {"action": "adding_sub_item", "cat_id": cat_id}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data=f"editsubs_{cat_id}"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('add_balance')} নতুন প্যাকেজের নাম এবং দাম লিখে পাঠান:\n<code>[Package Name] [Price]</code>", parse_mode="HTML", reply_markup=markup)

    elif call.data.startswith("editcat_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("editcat_", "")
        cursor.execute("SELECT name, price FROM categories WHERE cat_id = ?", (cat_id,))
        cat = cursor.fetchone()
        conn.close()
        admin_states[user_id] = {"action": "waiting_generic_btn_update", "btn_type": "cat", "btn_id": cat_id}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_edit_services"))
        edit_prompt = f"{pe('edit_pencil')} এডিট করছেন: <b>{cat[0]}</b> (বর্তমান মূল্য: ৳{cat[1]})\n\nনতুন নাম এবং রেট লিখে পাঠান.\n<b>ফরম্যাট:</b> <code>[নতুন নাম] [রেট]</code>"
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, edit_prompt, parse_mode="HTML", reply_markup=markup)

    elif call.data.startswith("delcat_") and user_id == ADMIN_ID:
        cat_id = call.data.replace("delcat_", "")
        cursor.execute("UPDATE categories SET is_active = 0 WHERE cat_id = ?", (cat_id,))
        cursor.execute("UPDATE stock SET status = 'removed' WHERE cat_id = ? AND status = 'available'", (cat_id,))
        cursor.execute("DELETE FROM sub_services WHERE cat_id = ?", (cat_id,))
        conn.commit()
        conn.close()
        bot.send_message(call.message.chat.id, f"{pe('step_tick')} সার্ভিসটি সফলভাবে ডিলিট করা হয়েছে!", parse_mode="HTML")

    elif call.data == "admin_add_new_btn" and user_id == ADMIN_ID:
        conn.close()
        admin_states[user_id] = {"action": "adding_new_cat"}
        markup = InlineKeyboardMarkup()
        markup.add(premium_inline_button("Back", "back_button", callback_data="admin_edit_services"))
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        bot.send_message(call.message.chat.id, f"{pe('add_balance')} নতুন বাটন যোগ করতে ফরম্যাটে নাম এবং দাম লিখে পাঠান:\n<code>[Button Name] [Price]</code>", parse_mode="HTML", reply_markup=markup)

    elif call.data.startswith("app_") and _is_deposit_approver(user_id):
        req_id = call.data.split("_")[1]
        if req_id in pending_deposits:
            data = pending_deposits[req_id]
            target_user, amount, method = data["user_id"], data["amount"], data.get("method", "Payment")
            
            cursor.execute("SELECT user_id, referred_by FROM users WHERE user_id = ?", (target_user,))
            user_row = cursor.fetchone()
            
            if not user_row:
                cursor.execute("INSERT INTO users (user_id, first_name, balance, username, referred_by) VALUES (?, ?, ?, 'N/A', NULL)", (target_user, "User", amount))
                referred_by = None
            else:
                cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target_user))
                referred_by = user_row[1]
            conn.commit()
            
            if referred_by:
                referral_bonus_amount = 0.02
                cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (referred_by,))
                referrer_exists = cursor.fetchone()
                if referrer_exists:
                    credited_at = datetime.now(timezone.utc).isoformat()
                    cursor.execute(
                        "INSERT OR IGNORE INTO referral_deposit_bonus_ledger (deposit_request_id, referrer_id, referred_user_id, deposit_amount, bonus_amount, credited_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (req_id, referred_by, target_user, amount, referral_bonus_amount, credited_at)
                    )
                    if cursor.rowcount > 0:
                        cursor.execute(
                            "UPDATE users SET balance = balance + ?, referral_deposit_bonus = COALESCE(referral_deposit_bonus, 0) + ? WHERE user_id = ?",
                            (referral_bonus_amount, referral_bonus_amount, referred_by)
                        )
                        conn.commit()
                        try:
                            bot.send_message(
                                referred_by,
                                f"<tg-emoji emoji-id=\"6131890186767831988\">🎁</tg-emoji> <b>রেফারেল ডিপোজিট বোনাস!</b>\nআপনার রেফারেল থেকে একজন মেম্বার সফলভাবে ডিপোজিট করেছেন। আপনার মেইন ব্যালেন্সে <b>0.02 টাকা</b> যোগ হয়েছে এবং Referral Deposit Bonus-এ স্থায়ীভাবে জমা হয়েছে।",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

            conn.close()
            _set_manual_trx_status(data.get("trx_id", ""), "APPROVED")
            _trx_show = html.escape(str(data.get("trx_id", "N/A")))
            _amt_show = _fmt_amount(amount)
            bot.send_message(target_user, "✅ <b>Payment verified successfully!</b> Your balance has been credited.", parse_mode="HTML")
            bot.send_message(
                target_user,
                "✅ <b>Payment Successful!</b>\n\n"
                f"<b>Payment Method:</b> {html.escape(str(method))}\n"
                f"<b>Amount:</b> ৳{_amt_show}\n"
                f"<b>Transaction ID:</b> <code>{_trx_show}</code>\n"
                "<b>Status:</b> COMPLETED\n\n"
                "Your balance has been successfully credited.",
                parse_mode="HTML", reply_markup=get_permanent_keyboard(target_user)
            )
            try:
                bot.edit_message_caption(f"✅ Approved Successfully (User ID: {target_user}, Amount: ৳{amount}, Method: {method})", call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                try:
                    bot.edit_message_text(f"✅ Approved Successfully (User ID: {target_user}, Amount: ৳{amount}, Method: {method})", call.message.chat.id, call.message.message_id, reply_markup=None)
                except Exception:
                    pass
            del pending_deposits[req_id]
        else:
            conn.close()
            bot.send_message(call.message.chat.id, f"{pe('warn_icon')} এই রিকোয়েস্টটি ইতিমধ্যে প্রসেস করা হয়েছে!", parse_mode="HTML")

    elif call.data.startswith("rej_") and _is_deposit_approver(user_id):
        req_id = call.data.split("_")[1]
        if req_id in pending_deposits:
            data = pending_deposits[req_id]
            target_user = data["user_id"]
            conn.close()
            _set_manual_trx_status(data.get("trx_id", ""), "RELEASE")
            bot.send_message(target_user, f"{pe('warn_icon')} আপনার ডিপোজিট রিজেক্ট করা হয়েছে, পেমেন্ট যাচাই করা যায়নি। সঠিক Transaction ID দিয়ে পুনরায় চেষ্টা করুন।", parse_mode="HTML", reply_markup=get_permanent_keyboard(target_user))
            try:
                bot.edit_message_text(f"❌ Rejected (User ID: {target_user}, TrxID: {data.get('trx_id', 'N/A')})", call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                try: bot.edit_message_caption(f"❌ Rejected (User ID: {target_user})", call.message.chat.id, call.message.message_id)
                except Exception: bot.send_message(call.message.chat.id, f"❌ Rejected (User ID: {target_user})")
            del pending_deposits[req_id]
        else:
            conn.close()
            bot.send_message(call.message.chat.id, f"{pe('warn_icon')} এই রিকোয়েস্টটি ইতিমধ্যে প্রসেস করা হয়েছে!", parse_mode="HTML")

    elif call.data.startswith("complete_order_") and user_id == ADMIN_ID:
        target_user = int(call.data.split("_")[2])
        conn.close()
        admin_states[user_id] = {"action": "waiting_delivery_content", "target_user": target_user}
        bot.send_message(call.message.chat.id, f"{pe('list_point')} ইউজার ID: <code>{target_user}</code> এর অর্ডারের জন্য মেসেজ বা ডাটা লিখে পাঠান:", parse_mode="HTML")

    elif call.data.startswith("vpn_nostock_cancel_") and user_id == ADMIN_ID:
        parts = call.data.split("_")
        target_user = int(parts[3])
        refund_amount = float(parts[4])
        
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (refund_amount, target_user))
        conn.commit()
        conn.close()

        try:
            bot.send_message(
                target_user,
                f"{pe('no_stock')} <b>দুঃখিত, আপনার অর্ডারটি ক্যানসেল করা হয়েছে!</b>\n\n"
                f"আপনি যে ভিপিএন প্রোডাক্টটি অর্ডার করেছিলেন সেটি বর্তমানে আমাদের স্টকে নেই। "
                f"তাই আপনার অর্ডারটি ক্যানসেল করে আপনার পরিশোধিত ৳<b>{refund_amount}</b> টাকা আপনার অ্যাকাউন্টে ব্যাক (রিফান্ড) করে দেওয়া হয়েছে।",
                parse_mode="HTML",
                reply_markup=get_permanent_keyboard(target_user)
            )
        except Exception:
            pass

        bot.answer_callback_query(call.id, "অর্ডার সফলভাবে ক্যানসেল করা হয়েছে এবং ইউজারের টাকা রিফান্ড করা হয়েছে!", show_alert=True)
        try:
            bot.edit_message_caption(f"❌ VPN Order Cancelled & Refunded (User ID: {target_user}, Refund: ৳{refund_amount})", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    else:
        conn.close()

@bot.message_handler(content_types=['document'])
def handle_stock_file(message):
    update_owner_premium_status(message.from_user)
    user_id = message.from_user.id
    save_user_id_to_file(user_id)
    
    if user_id in user_temp_deposit and user_temp_deposit[user_id].get("step") == "waiting_screenshot":
        file_id = message.document.file_id
        process_deposit_submission(message, file_id, is_document=True)
        return

    if user_id == ADMIN_ID and user_id in admin_states:
        action = admin_states[user_id].get("action")

        if action == "waiting_premium_emoji_txt":
            try:
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                file_content = downloaded_file.decode('utf-8', errors='ignore')
                mapping, legacy_entries = parse_premium_emoji_mapping(file_content)
                if legacy_entries:
                    conn = db_connect("shop_bot.db", timeout=30)
                    cur = conn.cursor()
                    for legacy_key, legacy_id in legacy_entries.items():
                        cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (PREMIUM_EMOJI_PREFIX + legacy_key, legacy_id))
                    conn.commit()
                    conn.close()
                if not mapping:
                    bot.reply_to(message, f"{pe('warn_icon')} কোনো valid emoji পাওয়া যায়নি।", parse_mode="HTML")
                    return
                save_premium_emoji_mapping(mapping)
                del admin_states[user_id]
                bot.reply_to(message, f"{pe('step_tick')} <b>Premium Emoji Set সফলভাবে save হয়েছে!</b>", parse_mode="HTML")
            except Exception as e:
                bot.reply_to(message, f"{pe('warn_icon')} ত্রুটি ঘটেছে: {e}")
            return

        if action == "waiting_stock_file":
            cat_id = admin_states[user_id]["cat_id"]
            try:
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                file_content = downloaded_file.decode('utf-8', errors='ignore')
                lines = [line.strip() for line in file_content.splitlines() if line.strip()]
                
                if not lines:
                    bot.reply_to(message, f"{pe('warn_icon')} ফাইলটি খালি রয়েছে।", parse_mode="HTML")
                    return
                
                conn = db_connect("shop_bot.db", timeout=30)
                cursor = conn.cursor()
                added_count = 0
                for line in lines:
                    cursor.execute("INSERT INTO stock (cat_id, content) VALUES (?, ?)", (cat_id, line))
                    added_count += 1
                conn.commit()
                
                cursor.execute("SELECT user_id FROM users")
                all_users = cursor.fetchall()
                cursor.execute("SELECT name FROM categories WHERE cat_id = ?", (cat_id,))
                cat_row = cursor.fetchone()
                cat_name = cat_row[0] if cat_row else cat_id
                conn.close()
                
                broadcast_text = (
                    f"{pe('alarm_bell')} <b>নতুন স্টক আপডেট!</b>\n\n"
                    f"আমাদের বটে নতুন স্টক যুক্ত হয়েছে: <b>{cat_name}</b>\n"
                    f"এখনই আপনার অ্যাকাউন্ট থেকে পর্যাপ্ত ব্যালেন্স দিয়ে খুব সহজে কিনে নিন। স্টক সীমিত!"
                )
                for u in all_users:
                    try: bot.send_message(u[0], broadcast_text, parse_mode="HTML", reply_markup=get_permanent_keyboard(u[0]))
                    except Exception: pass

                del admin_states[user_id]
                bot.reply_to(message, f"{pe('step_tick')} সফলভাবে মোট <b>{added_count}টি</b> স্টক যোগ করা হয়েছে এবং মেম্বারদের নোটিফিকেশন পাঠানো হয়েছে!", parse_mode="HTML")
            except Exception as e:
                bot.reply_to(message, f"ফাইল প্রসেস করতে সমস্যা হয়েছে: {e}")
            return

        elif action == "waiting_recover_balance_txt":
            try:
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                file_content = downloaded_file.decode('utf-8', errors='ignore')
                lines = [line.strip() for line in file_content.splitlines() if line.strip()]
                
                conn = db_connect("shop_bot.db", timeout=30)
                cursor = conn.cursor()
                recovered_count = 0
                for line in lines:
                    numbers = re.findall(r'\d+(?:\.\d+)?', line)
                    if len(numbers) >= 2:
                        try:
                            target_uid, balance_amount = int(numbers[0]), float(numbers[1])
                            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (target_uid,))
                            if not cursor.fetchone():
                                cursor.execute("INSERT INTO users (user_id, first_name, balance, username) VALUES (?, ?, ?, 'N/A')", (target_uid, "User", balance_amount))
                            else:
                                cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (balance_amount, target_uid))
                            conn.commit()
                            recovered_count += 1
                            try:
                                bot.send_message(target_uid, f"{pe('deposit_success')} <b>ব্যালেন্স রিকভারি আপডেট!</b>\n\nআপনার একাউন্টে পূর্বের <b>৳{balance_amount}</b> জমা দেওয়া হয়েছে!", parse_mode="HTML", reply_markup=get_permanent_keyboard(target_uid))
                            except Exception: pass
                        except ValueError: pass
                
                conn.close()
                del admin_states[user_id]
                bot.reply_to(message, f"{pe('step_tick')} রিকভারি সম্পূর্ণ হয়েছে! মোট <b>{recovered_count} জন</b> ইউজারের ব্যালেন্স সফলভাবে ব্যাক করা হয়েছে।", parse_mode="HTML")
            except Exception as e:
                bot.reply_to(message, f"ফাইল পড়তে ত্রুটি ঘটেছে: {e}")
            return

        elif action == "waiting_analysis_txt_for_broadcast":
            try:
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                file_content = downloaded_file.decode('utf-8', errors='ignore')
                
                found_ids = re.findall(r'\b\d{6,12}\b', file_content)
                target_user_ids = list(set([int(uid) for uid in found_ids]))

                if not target_user_ids:
                    bot.reply_to(message, f"{pe('warn_icon')} ফাইল থেকে কোনো ইউজারের সঠিক আইডি খুঁজে পাওয়া যায়নি।", parse_mode="HTML")
                    return

                admin_states[user_id] = {
                    "action": "waiting_live_broadcast_message",
                    "target_uids": target_user_ids
                }

                bot.reply_to(
                    message,
                    f"{pe('step_tick')} ফাইল ডিটেকশন সফল হয়েছে! মোট <b>{len(target_user_ids)} জন</b> ইউজার পাওয়া গেছে।\n\n"
                    f"{pe('list_point')} <b>আপনি সবার উদ্দেশ্যে কি পাঠাতে চান?</b>\n"
                    f"(টেক্সট, ছবি, ভয়েস মেসেজ, ভিডিও বা ফাইল সরাসরি পাঠিয়ে দিন):",
                    parse_mode="HTML"
                )
            except Exception as e:
                bot.reply_to(message, f"ফাইল পড়তে সমস্যা হয়েছে: {e}")
            return


#autopay
DEPOSIT_REQUEST_CHAT_ID = 8465541252

def _is_deposit_approver(uid):
    if uid == ADMIN_ID:
        return True
    try:
        return DEPOSIT_REQUEST_CHAT_ID is not None and int(uid) == int(DEPOSIT_REQUEST_CHAT_ID)
    except (TypeError, ValueError):
        return False

def _claim_manual_trx(raw_trx, user_id):
    """Atomically reserve a TrxID. True = reserved (first use). False = already used / pending / approved."""
    norm = str(raw_trx).strip().upper()
    conn = db_connect("shop_bot.db", timeout=30)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS manual_deposit_trx (trx_norm TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', created_at INTEGER NOT NULL)")
        for q in ("SELECT 1 FROM deposit_history WHERE UPPER(transaction_id) = ?",
                  "SELECT 1 FROM mfs_trxid_claims WHERE UPPER(normalized_mfs_trxid) = ?",
                  "SELECT 1 FROM payment_invoices WHERE UPPER(COALESCE(transaction_id,'')) = ? AND status = 'COMPLETED'"):
            try:
                cur.execute(q, (norm,))
                if cur.fetchone():
                    return False
            except Exception:
                pass
        try:
            cur.execute("INSERT INTO manual_deposit_trx (trx_norm, user_id, status, created_at) VALUES (?, ?, 'PENDING', ?)", (norm, user_id, int(time.time())))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
    finally:
        conn.close()

def _set_manual_trx_status(raw_trx, status):
    """status 'APPROVED' keeps it blocked forever; 'RELEASE' frees it again (admin rejected)."""
    norm = str(raw_trx).strip().upper()
    try:
        conn = db_connect("shop_bot.db", timeout=30)
        cur = conn.cursor()
        if status == "RELEASE":
            cur.execute("DELETE FROM manual_deposit_trx WHERE trx_norm = ? AND status = 'PENDING'", (norm,))
        else:
            cur.execute("UPDATE manual_deposit_trx SET status = ? WHERE trx_norm = ?", (status, norm))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"manual trx status update failed: {e}")

def submit_manual_deposit_request(message, raw_trx):
    user_id = message.from_user.id
    data = user_temp_deposit.get(user_id) or {}
    amount = data.get("amount", 0.0)
    method = str(data.get("method", "Payment"))
    raw_trx = re.sub(r"\s+", "", str(raw_trx))[:64]

    if not _EMONPAY_TXN_RE.match(raw_trx):
        bot.send_message(user_id, "⚠️ <b>Transaction ID সঠিক নয়।</b>\nসঠিক TrxID আবার লিখে পাঠান:", parse_mode="HTML")
        return
    if not _claim_manual_trx(raw_trx, user_id):
        bot.send_message(user_id, "⚠️ <b>এই Transaction ID আগেই ব্যবহার করা হয়েছে!</b>\nএকই TrxID দিয়ে দুইবার ডিপোজিট করা যাবে না। সঠিক TrxID লিখে পাঠান:", parse_mode="HTML")
        return

    raw_first_name = message.from_user.first_name or "N/A"
    raw_username = f"@{message.from_user.username}" if message.from_user.username else "N/A"
    first_name, username = html.escape(raw_first_name), html.escape(raw_username)
    trx_id, method_h = html.escape(raw_trx), html.escape(method)
    save_user_id_to_file(user_id)

    req_id = str(uuid.uuid4())[:8]
    pending_deposits[req_id] = {"user_id": user_id, "amount": amount, "trx_id": raw_trx, "method": method}
    del user_temp_deposit[user_id]

    amt_txt = _fmt_amount(amount)
    bot.send_message(
        user_id,
        f"{pe('waiting_clock')} <b>অনুগ্রহ করে অপেক্ষা করুন, 10-20 সেকেন্ড</b>\n\n"
        "🔄 অটো পে যাচাই চলছে... যাচাই শেষ হলেই আপনার ব্যালেন্স অ্যাড হয়ে যাবে।",
        parse_mode="HTML", reply_markup=get_permanent_keyboard(user_id)
    )

    admin_markup = InlineKeyboardMarkup()
    admin_markup.add(
        premium_inline_button("Approve", "approve_btn", callback_data=f"app_{req_id}"),
        premium_inline_button("Reject", "reject_btn", callback_data=f"rej_{req_id}")
    )
    caption_text = (
        f"{pe('alarm_bell')} <b>New Deposit Request!</b>\n\n"
        f"{pe('profile_user')} <b>Name:</b> <b>{first_name}</b>\n"
        f"{pe('username_link')} <b>Username:</b> <b>{username}</b>\n"
        f"{pe('id_badge')} <b>User ID:</b> <code>{user_id}</code>\n"
        f"{pe('deposit_money')} <b>Amount:</b> <b>৳{amt_txt}</b>\n"
        f"{pe('sendmoney_shield')} <b>Payment Method:</b> <b>{method_h}</b>\n"
        f"🧾 <b>TrxID:</b> <code>{trx_id}</code>\n\n"
        "Please verify payment and take action below:"
    )
    target = DEPOSIT_REQUEST_CHAT_ID or ADMIN_ID
    if not DEPOSIT_REQUEST_CHAT_ID:
        grp = get_setting_msg('deposit_group_id', '')
        if grp:
            try: target = int(grp)
            except ValueError: target = grp
    try:
        bot.send_message(target, caption_text, parse_mode="HTML", reply_markup=admin_markup)
    except Exception as e:
        print(f"Manual deposit notify failed: {e}")
        bot.send_message(ADMIN_ID, caption_text, parse_mode="HTML", reply_markup=admin_markup)

def process_deposit_submission(message, file_id, is_document=False):
    update_owner_premium_status(message.from_user)
    user_id = message.from_user.id
    raw_first_name = message.from_user.first_name or "N/A"
    raw_username = f"@{message.from_user.username}" if message.from_user.username else "N/A"
    
    first_name = html.escape(raw_first_name)
    username = html.escape(raw_username)
    
    save_user_id_to_file(user_id)

    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (raw_username, user_id))
    conn.commit()
    conn.close()

    if user_id in user_temp_deposit and user_temp_deposit[user_id].get("step") == "waiting_screenshot":
        data = user_temp_deposit[user_id]
        amount = data.get("amount", 0.0)
        raw_trx = str(data.get("trx_id", "N/A"))
        trx_id = html.escape(raw_trx)
        method = html.escape(str(data.get("method", "Payment")))

        bot.reply_to(
            message,
            f"{pe('waiting_clock')} <b>প্রিয় কাস্টমার দয়া করে ২ - ৩ মিনিট অপেক্ষা করুন, অ্যাকাউন্টে টাকা যোগ হচ্ছে...</b>",
            parse_mode="HTML",
            reply_markup=get_permanent_keyboard(user_id)
        )

        req_id = str(uuid.uuid4())[:8]
        pending_deposits[req_id] = {
            "user_id": user_id,
            "amount": amount,
            "trx_id": raw_trx,
            "method": method
        }
        del user_temp_deposit[user_id]

        admin_markup = InlineKeyboardMarkup()
        admin_markup.add(
            premium_inline_button("Approve", "approve_btn", callback_data=f"app_{req_id}"),
            premium_inline_button("Reject", "reject_btn", callback_data=f"rej_{req_id}")
        )

        caption_text = (
            f"{pe('alarm_bell')} <b>New Payment Request Received!</b>\n\n"
            f"{pe('profile_user')} <b>Name:</b> <b>{first_name}</b>\n"
            f"{pe('username_link')} <b>Username:</b> <b>{username}</b>\n"
            f"{pe('id_badge')} <b>User ID:</b> <code>{user_id}</code>\n"
            f"{pe('deposit_money')} <b>Amount:</b> <b>৳{amount}</b>\n"
            f"{pe('sendmoney_shield')} <b>Payment Method:</b> <b>{method}</b>\n"
            f"{pe('trx_input')} <b>TrxID:</b> <code>{trx_id}</code>\n\n"
            f"Please verify payment and take action below:"
        )

        target_notify_chat = ADMIN_ID
        deposit_group = get_setting_msg('deposit_group_id', '')
        if deposit_group:
            try:
                target_notify_chat = int(deposit_group)
            except ValueError:
                target_notify_chat = deposit_group

        for i in range(3):
            try:
                bot.send_message(
                    target_notify_chat, 
                    f"{pe('alarm_bell')} <b>[ALARM {i+1}/3] নতুন ডিপোজিট ও পেমেন্ট এসেছে! দ্রুত এপ্রুভ করুন। টং টং! 🛎️🔔</b>", 
                    parse_mode="HTML"
                )
                time.sleep(0.3)
            except Exception:
                pass

        try:
            if not is_document:
                bot.send_photo(target_notify_chat, file_id, caption=caption_text, parse_mode="HTML", reply_markup=admin_markup)
            else:
                bot.send_document(target_notify_chat, file_id, caption=caption_text, parse_mode="HTML", reply_markup=admin_markup)
        except Exception as e:
            bot.send_message(ADMIN_ID, caption_text, parse_mode="HTML", reply_markup=admin_markup)

@bot.message_handler(content_types=['text', 'photo', 'voice', 'audio', 'video', 'document'])
def handle_all_messages_and_broadcast(message):
    update_owner_premium_status(message.from_user)
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "N/A"
    save_user_id_to_file(user_id)

    conn = db_connect("shop_bot.db", timeout=30)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
    conn.commit()
    conn.close()

    text = message.text if message.text else ""

    # Independent Telegram Member Sell link/order flow.
    if user_id in member_sell_states:
        state_data = member_sell_states[user_id]
        if state_data.get("step") == "waiting_target_link":
            target_link = (text or "").strip()
            if target_link.lower() == "cancel":
                del member_sell_states[user_id]
                bot.send_message(
                    user_id,
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} অর্ডার প্রক্রিয়া বাতিল করা হয়েছে। কোনো টাকা কাটা হয়নি।",
                    parse_mode="HTML",
                    reply_markup=get_permanent_keyboard(user_id)
                )
                return
            if message.content_type != "text" or not is_valid_member_sell_target(target_link):
                bot.send_message(
                    user_id,
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['prompt_target'])} সঠিক Telegram Username অথবা Link দিন। উদাহরণ: <code>@channelname</code> অথবা <code>https://t.me/channelname</code>",
                    parse_mode="HTML"
                )
                return

            conn = db_connect("shop_bot.db", timeout=30)
            cur = conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute("SELECT balance, first_name, username FROM users WHERE user_id = ?", (user_id,))
                user_row = cur.fetchone()
                if not user_row:
                    conn.rollback()
                    conn.close()
                    bot.send_message(user_id, f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} আপনার অ্যাকাউন্ট পাওয়া যায়নি। /start দিয়ে আবার চেষ্টা করুন।", parse_mode="HTML")
                    del member_sell_states[user_id]
                    return
                current_balance, first_name, saved_username = user_row
                price = float(state_data["price"])
                if float(current_balance or 0) < price:
                    conn.rollback()
                    conn.close()
                    bot.send_message(
                        user_id,
                        f"{member_sell_emoji(MEMBER_SELL_EMOJIS['prompt_price'])} আপনার পর্যাপ্ত ব্যালেন্স নেই।\nপ্যাকেজ মূল্য: ৳{price:g}\n{member_sell_emoji(MEMBER_SELL_EMOJIS['success_balance'])} আপনার ব্যালেন্স: ৳{float(current_balance or 0):g}",
                        parse_mode="HTML",
                        reply_markup=get_permanent_keyboard(user_id)
                    )
                    del member_sell_states[user_id]
                    return

                new_balance = float(current_balance or 0) - price
                order_id = uuid.uuid4().hex
                created_at = datetime.now(timezone.utc).isoformat()
                cur.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
                cur.execute(
                    "INSERT INTO member_sell_orders (order_id, user_id, first_name, username, duration_key, duration_label, quantity, price, target_link, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (order_id, user_id, first_name or message.from_user.first_name or "User", username if username != "N/A" else (saved_username or "N/A"),
                     state_data["duration_key"], state_data["duration_label"], state_data["quantity"], price, target_link, created_at)
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                conn.close()
                print(f"Telegram Member Sell order create error: {e}")
                bot.send_message(user_id, f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} অর্ডারটি তৈরি করা যায়নি। কোনো টাকা কাটা হয়নি। আবার চেষ্টা করুন।", parse_mode="HTML")
                return
            conn.close()
            del member_sell_states[user_id]

            quantity = int(state_data["quantity"])
            duration_label = state_data["duration_label"]
            qty_label = f"{quantity // 1000}k" if quantity >= 1000 else str(quantity)
            safe_name = html.escape(first_name or message.from_user.first_name or "User")
            safe_username = html.escape(username)
            safe_link = html.escape(target_link)

            try:
                bot.send_message(
                    user_id,
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['service'])} <b>আপনার Telegram Member Sell অর্ডারটি সফলভাবে সাবমিট হয়েছে!</b>\n\n"
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['duration'])} <b>মেয়াদ:</b> {html.escape(duration_label)}\n"
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['service'])} <b>মেম্বার:</b> {qty_label} জন\n"
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['prompt_price'])} <b>মূল্য:</b> ৳{price:g}\n"
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['success_balance'])} <b>অবশিষ্ট ব্যালেন্স:</b> ৳{new_balance:g}\n"
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['success_order_id'])} <b>Order ID:</b> <code>{order_id}</code>\n\n"
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['delivery'])} অর্ডারটি কিছুক্ষণের মধ্যে আপনার চ্যানেল বা গ্রুপে রানিং হয়ে যাবে। কিছুক্ষণ অপেক্ষা করুন।",
                    parse_mode="HTML",
                    reply_markup=get_permanent_keyboard(user_id)
                )
            except Exception as e:
                print(f"Telegram Member Sell buyer notification failed: {e}")

            admin_markup = InlineKeyboardMarkup(row_width=1)
            admin_markup.add(
                premium_inline_button("Service Complete", "6269285159774720688", callback_data=f"ms_complete_{order_id}"),
                premium_inline_button("No Delivery", "6068810023566317366", callback_data=f"ms_refund_{order_id}")
            )
            admin_order_msg = (
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['service'])} <b>New Telegram Member Sell Order</b>\n\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['success_order_id'])} <b>Order ID:</b> <code>{order_id}</code>\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['admin_name'])} <b>Name:</b> {safe_name}\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['admin_username'])} <b>Telegram Username:</b> {safe_username}\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['admin_user_id'])} <b>User ID:</b> <code>{user_id}</code>\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['service'])} <b>Service:</b> Telegram Member Sell\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['duration'])} <b>Duration:</b> {html.escape(duration_label)}\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['service'])} <b>Members:</b> {qty_label}\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['prompt_price'])} <b>Price:</b> ৳{price:g}\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['admin_link'])} <b>Channel/Group Link:</b> {safe_link}\n"
                f"{member_sell_emoji(MEMBER_SELL_EMOJIS['admin_status'])} <b>Order Status:</b> Pending"
            )
            try:
                bot.send_message(ADMIN_ID, admin_order_msg, parse_mode="HTML", reply_markup=admin_markup)
            except Exception as e:
                print(f"Telegram Member Sell admin notification failed: {e}")
                try:
                    bot.send_message(user_id, f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} অর্ডারটি সেভ হয়েছে, তবে অ্যাডমিনকে নোটিফিকেশন পাঠানো যায়নি। সাপোর্টে যোগাযোগ করুন।", parse_mode="HTML")
                except Exception:
                    pass
            return

    # Binance: a plain Transaction ID sent after a restart / menu tap still reaches the user's open Binance invoice.
    if (user_id not in user_temp_deposit and user_id not in admin_states and message.content_type == "text"
            and re.fullmatch(r"[0-9]{10,40}", (text or "").strip())):
        open_binance_ref = binance_find_open_invoice(user_id)
        if open_binance_ref:
            binance_handle_user_txn(user_id, open_binance_ref, text)
            return

    # EmonPay dashboard TrxID: a plain MFS TrxID typed after a restart still reaches the user's open invoice.
    if (user_id not in user_temp_deposit and user_id not in admin_states and user_id not in member_sell_states
            and message.content_type == "text" and _emon_looks_like_trxid(text)):
        open_emon_ref = emon_find_open_invoice(user_id)
        if open_emon_ref:
            emon_dashboard_handle_user_txn(user_id, open_emon_ref, text)
            return

    if user_id in user_temp_deposit:
        state = user_temp_deposit[user_id].get("step")

        if state == "waiting_emon_trx":
            if message.content_type != "text" or not (text or "").strip():
                bot.send_message(user_id, "⚠️ Please send your Transaction ID (TrxID) as text.", parse_mode="HTML")
                return
            emon_dashboard_handle_user_txn(user_id, user_temp_deposit[user_id].get("invoice_ref"), text)
            return

        if state in ("waiting_amount", "waiting_custom_amount"):
            amount = parse_deposit_amount(text) if message.content_type == "text" else None
            if amount is None:
                bot.send_message(
                    user_id,
                    "⚠️ <b>Invalid amount!</b>\nPlease enter a valid number between <b>10</b> and <b>10,000</b> BDT (example: <code>50</code> or <code>500</code>):",
                    parse_mode="HTML",
                    reply_markup=get_custom_deposit_cancel_markup() if state == "waiting_custom_amount" else None
                )
                return
            show_deposit_method_choice(user_id, user_id, amount)
            return

        elif state == "choose_method":
            bot.send_message(
                user_id,
                "💳 অনুগ্রহ করে উপরের বাটন থেকে <b>পেমেন্ট পদ্ধতি</b> বেছে নিন, অথবা Cancel চাপুন।",
                parse_mode="HTML",
                reply_markup=get_deposit_method_markup()
            )
            return

        elif state == "waiting_binance_trx":
            if message.content_type != "text" or not (text or "").strip():
                bot.send_message(user_id, "⚠️ অনুগ্রহ করে আপনার Binance Order ID টেক্সট হিসেবে লিখে পাঠান।", parse_mode="HTML")
                return
            binance_handle_user_txn(user_id, user_temp_deposit[user_id].get("invoice_ref"), text)
            return

        elif state == "waiting_trx":
            if message.content_type != "text" or not (text or "").strip():
                bot.send_message(user_id, "⚠️ অনুগ্রহ করে আপনার Transaction ID টেক্সট হিসেবে লিখে পাঠান।", parse_mode="HTML")
                return
            submit_manual_deposit_request(message, text.strip())
            return

        elif state == "waiting_service_username":
            data = user_temp_deposit[user_id]
            cat_id, sub_name, price, username_input = data["cat_id"], data["sub_name"], data["price"], text.strip()
            del user_temp_deposit[user_id]

            conn = db_connect("shop_bot.db", timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            bal_res = cursor.fetchone()
            current_bal = bal_res[0] if bal_res else 0.0

            if current_bal < price:
                conn.close()
                bot.send_message(user_id, f"{pe('insufficient_bal')} আপনার একাউন্টে পর্যাপ্ত ব্যালেন্স নেই!", parse_mode="HTML", reply_markup=get_permanent_keyboard(user_id))
                return

            new_bal = current_bal - price
            cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, user_id))
            conn.commit()
            conn.close()

            is_vpn_order = "vpn_" in cat_id or "Day" in sub_name
            emoji_k = "telegram_premium_cat" if "telegram" in cat_id else get_vpn_emoji_key(sub_name)
            
            bot.send_message(
                user_id,
                f"{pe('order_pending')} আপনার অর্ডারটি সফলভাবে সাবমিট হয়েছে!\n\n"
                f"{pe(emoji_k)} সার্ভিস: <b>{sub_name}</b> {pe(emoji_k)}\n"
                f"{pe('profile_user')} ইউজারনেম/আইডি: <code>{username_input}</code>\n"
                f"{pe('money_spent')} পরিশোধিত মূল্য: ৳{price}\n"
                f"{pe('wallet_balance')} অবশিষ্ট ব্যালেন্স: ৳{new_bal}\n\n"
                f"{pe('waiting_clock')} দয়া করে কিছুক্ষণ অপেক্ষা করুন, অ্যাডমিন খুব শীঘ্রই আপনার সার্ভিসটি কমপ্লিট করে দেবেন।",
                parse_mode="HTML",
                reply_markup=get_permanent_keyboard(user_id)
            )

            admin_markup = InlineKeyboardMarkup(row_width=1)
            admin_markup.add(
                premium_inline_button("Complete Order & Deliver", "approve_btn", callback_data=f"complete_order_{user_id}"),
            )
            if is_vpn_order:
                admin_markup.add(
                    premium_inline_button("No Stock Cancel Delivery", "reject_btn", callback_data=f"vpn_nostock_cancel_{user_id}_{price}")
                )

            admin_order_msg = (
                f"{pe('alarm_bell')} <b>New VPN / Service Order Received!</b>\n\n"
                f"{pe('profile_user')} <b>User ID:</b> <code>{user_id}</code>\n"
                f"{pe('box_package')} <b>Service:</b> <b>{sub_name}</b>\n"
                f"{pe('username_link')} <b>Target Account:</b> <code>{username_input}</code>\n"
                f"{pe('money_spent')} <b>Price:</b> ৳{price}"
            )

            for i in range(3):
                try:
                    bot.send_message(
                        ADMIN_ID, 
                        f"{pe('alarm_bell')} <b>[ORDER ALARM {i+1}/3] নতুন ভিপিএন অর্ডার এসেছে! টং টং! 🛎️🔔</b>", 
                        parse_mode="HTML"
                    )
                    time.sleep(0.3)
                except Exception:
                    pass

            bot.send_message(ADMIN_ID, admin_order_msg, parse_mode="HTML", reply_markup=admin_markup)
            return

    if user_id == ADMIN_ID and user_id in admin_states:
        state_data = admin_states[user_id]
        action = state_data.get("action")

        if action == "waiting_member_sell_delivery":
            order_id = state_data.get("order_id")
            if message.content_type != "text" or not text.strip():
                bot.reply_to(message, f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} ডেলিভারি মেসেজটি Text হিসেবে লিখে পাঠান।", parse_mode="HTML")
                return
            order_row = get_member_sell_order(order_id)
            if not order_row or order_row[8] != "delivering":
                del admin_states[user_id]
                bot.reply_to(message, f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} এই অর্ডারটি আর ডেলিভারি অবস্থায় নেই।", parse_mode="HTML")
                return
            target_user = order_row[1]
            delivery_text = text.strip()
            try:
                bot.send_message(
                    target_user,
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['service'])} <b>আপনার Telegram Member Sell অর্ডারটি সম্পন্ন হয়েছে!</b>\n\n"
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['delivery_info'])} <b>অ্যাডমিনের মেসেজ / ডেলিভারি তথ্য:</b>\n{html.escape(delivery_text)}\n\n"
                    f"{member_sell_emoji(MEMBER_SELL_EMOJIS['thanks'])} ধন্যবাদ।",
                    parse_mode="HTML",
                    reply_markup=get_permanent_keyboard(target_user)
                )
            except Exception as e:
                bot.reply_to(message, f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} ইউজারের কাছে ডেলিভারি মেসেজ পাঠানো যায়নি: <code>{html.escape(str(e))}</code>\nআবার মেসেজ পাঠানোর চেষ্টা করুন।", parse_mode="HTML")
                return

            conn = db_connect("shop_bot.db", timeout=30)
            cur = conn.cursor()
            cur.execute(
                "UPDATE member_sell_orders SET status = 'completed', completed_at = ?, delivery_text = ? "
                "WHERE order_id = ? AND status = 'delivering'",
                (datetime.now(timezone.utc).isoformat(), delivery_text, order_id)
            )
            conn.commit()
            conn.close()
            del admin_states[user_id]
            bot.reply_to(message, f"{member_sell_emoji(MEMBER_SELL_EMOJIS["service"])} অর্ডার <code>{html.escape(order_id)}</code> সফলভাবে Complete হয়েছে এবং ডেলিভারি মেসেজ কাস্টমারের কাছে পাঠানো হয়েছে।", parse_mode="HTML")
            return

        if action == "waiting_other_emoji_code":
            emoji_id_val = (text or "").strip()
            pending_data = state_data.get("pending_btn_data")
            if not (emoji_id_val.isdigit() and 10 <= len(emoji_id_val) <= 30):
                bot.reply_to(message, f"{pe('warn_icon')} সঠিক Custom Emoji ID দিন (১০-৩০ সংখ্যার ID)। আবার পাঠান।", parse_mode="HTML")
                return
            if not pending_data:
                del admin_states[user_id]
                bot.reply_to(message, f"{pe('warn_icon')} এডিট/অ্যাড সেশনটি পাওয়া যায়নি। আবার শুরু করুন।", parse_mode="HTML")
                return
            try:
                _save_pending_button(pending_data, has_emoji=True, custom_emoji_id=emoji_id_val)
                del admin_states[user_id]
                bot.reply_to(
                    message,
                    f"{pe('step_tick')} কাস্টম প্রিমিয়াম ইমোজিসহ বাটনটি সফলভাবে সেভ হয়েছে!\n"
                    f"নাম: <b>{html.escape(str(pending_data.get('name', '')))}</b>\n"
                    f"রেট: <b>৳{float(pending_data.get('price', 0)):g}</b>",
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Custom button emoji save error: {e}")
                bot.reply_to(message, f"{pe('warn_icon')} বাটনটি সেভ করা যায়নি: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
            return

        if action == "waiting_premium_emoji_txt":
            mapping, legacy_entries = parse_premium_emoji_mapping(text)
            if not mapping:
                bot.reply_to(message, f"{pe('warn_icon')} সঠিক ফরম্যাটে ইমোজি ও Custom Emoji ID পাঠান। উদাহরণ: <code>🔴 = 5788939937335366</code>", parse_mode="HTML")
                return
            save_premium_emoji_mapping(mapping)
            if legacy_entries:
                conn = db_connect("shop_bot.db", timeout=30)
                cur = conn.cursor()
                for legacy_key, legacy_id in legacy_entries.items():
                    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (PREMIUM_EMOJI_PREFIX + legacy_key, legacy_id))
                conn.commit()
                conn.close()
            del admin_states[user_id]
            bot.reply_to(message, f"{pe('step_tick')} <b>Premium Emoji Set সফলভাবে save হয়েছে!</b>\nBroadcast-এ এখন সাধারণ ইমোজি ব্যবহার করলেই Premium Emoji পাঠানো হবে।", parse_mode="HTML")
            return

        if action == "waiting_delivery_content":
            target_user = state_data["target_user"]
            del admin_states[user_id]
            try:
                bot.send_message(
                    target_user, 
                    f"{pe('order_delivered')} <b>আপনার অর্ডারটি সফলভাবে সম্পন্ন ও ডেলিভারি হয়েছে!</b>\n\n"
                    f"{pe('box_package')} <b>ডিটেইলস / অ্যাকাউন্ট:</b>\n{text}\n\n"
                    f"আমাদের সাথে থাকার জন্য ধন্যবাদ! {pe('welcome_heart')}", 
                    parse_mode="HTML", 
                    reply_markup=get_permanent_keyboard(target_user)
                )
                bot.reply_to(message, f"{pe('step_tick')} সফলভাবে ইউজারের কাছে (ID: <code>{target_user}</code>) অর্ডার ডেলিভারি পাঠানো হয়েছে!", parse_mode="HTML")
            except Exception as e:
                bot.reply_to(message, f"ইউজারের কাছে মেসেজ পাঠানো যায়নি: {e}")
            return

        elif action == "adding_new_cat":
            raw_value = (text or "").strip()
            parts = raw_value.rsplit(None, 1)
            if len(parts) != 2 or not parts[0].strip() or not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", parts[1]):
                bot.reply_to(message, f"{pe('warn_icon')} সঠিক ফরম্যাটে নাম ও রেট দিন।\n<b>ফরম্যাট:</b> <code>[Button Name] [Price]</code>\n<b>উদাহরণ:</b> <code>9 Proxy (100MB) 15.0</code>", parse_mode="HTML")
                return
            c_name, c_price = parts[0].strip(), float(parts[1])
            admin_states[user_id] = {
                "action": "waiting_emoji_choice",
                "pending_btn_data": {"mode": "new_cat", "name": c_name, "price": c_price}
            }
            markup = InlineKeyboardMarkup(row_width=3)
            markup.add(
                InlineKeyboardButton("Yes", callback_data="btn_emojichoice_yes"),
                InlineKeyboardButton("No", callback_data="btn_emojichoice_no"),
                InlineKeyboardButton("Other Emoji Code", callback_data="btn_emojichoice_other")
            )
            bot.reply_to(message, f"আপনি কি এই বাটনে প্রিমিয়াম ইমোজি সেট করতে চান?", reply_markup=markup)
            return

        elif action == "waiting_generic_btn_update":
            btn_type, btn_id = state_data.get("btn_type"), str(state_data.get("btn_id"))
            raw_value = (text or "").strip()
            parts = raw_value.rsplit(None, 1)
            if len(parts) != 2 or not parts[0].strip() or not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", parts[1]):
                bot.reply_to(
                    message,
                    f"{pe('warn_icon')} সঠিক ফরম্যাটে নতুন নাম ও রেট দিন।\n"
                    f"<b>ফরম্যাট:</b> <code>[নতুন নাম] [নতুন রেট]</code>\n"
                    f"<b>উদাহরণ:</b> <code>9 Proxy (100MB) 15.0</code>",
                    parse_mode="HTML"
                )
                return

            new_name, new_price = parts[0].strip(), float(parts[1])
            if btn_type == "cat":
                pending_data = {"mode": "edit_cat", "cat_id": btn_id, "name": new_name, "price": new_price}
            elif btn_type == "sub":
                pending_data = {"mode": "edit_sub", "sub_id": btn_id, "name": new_name, "price": new_price}
            else:
                bot.reply_to(message, f"{pe('warn_icon')} এই বাটনের ধরনটি এডিট করা যাচ্ছে না।", parse_mode="HTML")
                return

            admin_states[user_id] = {"action": "waiting_emoji_choice", "pending_btn_data": pending_data}
            markup = InlineKeyboardMarkup(row_width=3)
            markup.add(
                InlineKeyboardButton("Yes", callback_data="btn_emojichoice_yes"),
                InlineKeyboardButton("No", callback_data="btn_emojichoice_no"),
                InlineKeyboardButton("Other Emoji Code", callback_data="btn_emojichoice_other")
            )
            bot.reply_to(
                message,
                f"নতুন নাম: <b>{html.escape(new_name)}</b>\nনতুন রেট: <b>৳{new_price:g}</b>\n\n"
                f"এই বাটনে প্রিমিয়াম ইমোজি সেট করতে চান?",
                parse_mode="HTML", reply_markup=markup
            )
            return

        elif action == "waiting_direct_broadcast_message":
            del admin_states[user_id]
            conn = db_connect("shop_bot.db", timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            target_uids = [row[0] for row in cursor.fetchall()]
            conn.close()
            bot.reply_to(message, f"{pe('waiting_clock')} <b>Broadcast শুরু হয়েছে...</b>", parse_mode="HTML")
            success_count = 0
            premium_mapping = load_premium_emoji_mapping()
            broadcast_text = convert_premium_emoji_text(message.text or "", premium_mapping)
            broadcast_caption = convert_premium_emoji_text(message.caption or "", premium_mapping)
            for target_uid in target_uids:
                try:
                    if message.content_type == 'text':
                        bot.send_message(target_uid, broadcast_text, parse_mode="HTML")
                    elif message.content_type == 'photo':
                        bot.send_photo(target_uid, message.photo[-1].file_id, caption=broadcast_caption, parse_mode="HTML")
                    elif message.content_type == 'voice':
                        bot.send_voice(target_uid, message.voice.file_id, caption=broadcast_caption, parse_mode="HTML")
                    elif message.content_type == 'video':
                        bot.send_video(target_uid, message.video.file_id, caption=broadcast_caption, parse_mode="HTML")
                    elif message.content_type == 'document':
                        bot.send_document(target_uid, message.document.file_id, caption=broadcast_caption, parse_mode="HTML")
                    success_count += 1
                except Exception as e:
                    print(f"Broadcast failed for {target_uid}: {e}")
            bot.send_message(ADMIN_ID, f"{pe('step_tick')} <b>Broadcast সম্পূর্ণ হয়েছে!</b>\nসফলভাবে পাঠানো হয়েছে: <b>{success_count}</b> জন ইউজারের কাছে।", parse_mode="HTML")
            return

        elif action == "waiting_live_broadcast_message":
            target_uids = state_data["target_uids"]
            del admin_states[user_id]

            bot.reply_to(message, f"{pe('waiting_clock')} <b>মেম্বারদের কাছে বার্তা পাঠানো শুরু হচ্ছে...</b>", parse_mode="HTML")

            success_count = 0
            premium_mapping = load_premium_emoji_mapping()
            broadcast_text = convert_premium_emoji_text(message.text or "", premium_mapping)
            broadcast_caption = convert_premium_emoji_text(message.caption or "", premium_mapping)
            for target_uid in target_uids:
                try:
                    if message.content_type == 'text':
                        bot.send_message(target_uid, f"{pe('loudspeaker')} <b>বিশেষ বার্তা:</b>\n\n{broadcast_text}", parse_mode="HTML", reply_markup=get_permanent_keyboard(target_uid))
                    elif message.content_type == 'photo':
                        cap = broadcast_caption
                        bot.send_photo(target_uid, message.photo[-1].file_id, caption=f"{pe('loudspeaker')} <b>বিশেষ বার্তা:</b>\n\n{cap}", parse_mode="HTML", reply_markup=get_permanent_keyboard(target_uid))
                    elif message.content_type == 'voice':
                        bot.send_voice(target_uid, message.voice.file_id, caption=f"{pe('loudspeaker')} <b>অফিসিয়াল ভয়েস বার্তা</b>", parse_mode="HTML", reply_markup=get_permanent_keyboard(target_uid))
                    elif message.content_type == 'video':
                        cap = broadcast_caption
                        bot.send_video(target_uid, message.video.file_id, caption=f"{pe('loudspeaker')} <b>বিশেষ ভিডিও:</b>\n\n{cap}", parse_mode="HTML", reply_markup=get_permanent_keyboard(target_uid))
                    elif message.content_type == 'document':
                        bot.send_document(target_uid, message.document.file_id, caption=f"{pe('loudspeaker')} <b>অফিসিয়াল ফাইল</b>", parse_mode="HTML", reply_markup=get_permanent_keyboard(target_uid))
                    success_count += 1
                except Exception:
                    pass

            bot.send_message(
                ADMIN_ID,
                f"{pe('step_tick')} <b>ব্রডকাস্ট সম্পূর্ণ সফল হয়েছে!</b>\nমোট পাঠানো হয়েছে: <b>{success_count} জন</b> মেম্বারের ইনবক্সে。",
                parse_mode="HTML"
            )
            return

        elif action == "saving_payment_number":
            pmethod = state_data["method"]
            val = message.text.strip()
            key_map = {"bkash": "bkash_num", "nagad": "nagad_num", "rocket": "rocket_num", "binance": "binance_uid"}
            setting_key = key_map.get(pmethod, "bkash_num")
            
            conn = db_connect("shop_bot.db", timeout=30)
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (setting_key, val))
            conn.commit()
            conn.close()
            del admin_states[user_id]
            bot.reply_to(message, f"{pe('step_tick')} সফলভাবে <b>{pmethod.upper()}</b> আপডেট করা হয়েছে: <code>{val}</code>", parse_mode="HTML")
            return

        elif action == "waiting_pc_directory_path":
            val = message.text.strip()
            conn = db_connect("shop_bot.db", timeout=30)
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('pc_directory_path', ?)", (val,))
            conn.commit()
            conn.close()
            del admin_states[user_id]
            bot.reply_to(message, f"{pe('step_tick')} পিসি ডিরেক্টরি পাথ সফলভাবে সেভ করা হয়েছে: <code>{val}</code>", parse_mode="HTML")
            return

        elif action == "waiting_pc_live_money_path":
            val = message.text.strip()
            conn = db_connect("shop_bot.db", timeout=30)
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('pc_live_money_path', ?)", (val,))
            conn.commit()
            conn.close()
            del admin_states[user_id]
            bot.reply_to(message, f"{pe('step_tick')} লাইভ মানি এনালাইসিস ফোল্ডার পাথ সফলভাবে সেভ করা হয়েছে: <code>{val}</code>", parse_mode="HTML")
            return

        elif action == "waiting_pc_live_stock_path":
            val = message.text.strip()
            conn = db_connect("shop_bot.db", timeout=30)
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('pc_live_stock_path', ?)", (val,))
            conn.commit()
            conn.close()
            del admin_states[user_id]
            bot.reply_to(message, f"{pe('step_tick')} লাইভ স্টক ফোল্ডার পাথ সফলভাবে সেভ করা হয়েছে: <code>{val}</code>", parse_mode="HTML")
            return

        elif action == "waiting_generic_post_update":
            post_key = state_data["post_key"]
            val = message.text
            conn = db_connect("shop_bot.db", timeout=30)
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (post_key, val))
            conn.commit()
            conn.close()
            del admin_states[user_id]
            bot.reply_to(message, f"{pe('step_tick')} সফলভাবে পোস্টটি আপডেট করা হয়েছে!", parse_mode="HTML")
            return

        elif action == "waiting_member_uid_for_balance":
            state_data["target_uid"] = text.strip()
            state_data["action"] = "waiting_member_amount_for_balance"
            bot.reply_to(message, f"{pe('deposit_money')} এই ইউজারের অ্যাকাউন্টে কত টাকা ব্যাক দিতে চাচ্ছেন? সঠিক পরিমাণ (যেমন: <code>50</code>) চ্যাটে লিখে পাঠান:", parse_mode="HTML")
            return

        elif action == "waiting_member_amount_for_balance":
            target_uid_str = state_data["target_uid"]
            del admin_states[user_id]
            try:
                target_uid = int(target_uid_str)
                add_amount = float(text.strip())

                conn = db_connect("shop_bot.db", timeout=30)
                cursor = conn.cursor()
                cursor.execute("SELECT user_id, balance FROM users WHERE user_id = ?", (target_uid,))
                user_row = cursor.fetchone()

                if user_row:
                    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (add_amount, target_uid))
                    conn.commit()
                    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target_uid,))
                    new_bal = cursor.fetchone()[0]
                else:
                    cursor.execute("INSERT INTO users (user_id, first_name, balance, username) VALUES (?, ?, ?, 'N/A')", (target_uid, "User", add_amount))
                    conn.commit()
                    new_bal = add_amount
                conn.close()

                try:
                    bot.send_message(
                        target_uid,
                        f"{pe('deposit_success')} <b>বট আপডেট করা হয়েছে!</b>\n\nআপনার একাউন্টে ৳{add_amount} টাকা এড করা হয়েছে।",
                        parse_mode="HTML",
                        reply_markup=get_permanent_keyboard(target_uid)
                    )
                except Exception:
                    pass

                bot.reply_to(message, f"{pe('step_tick')} সফলভাবে ইউজার (<code>{target_uid}</code>) এর অ্যাকাউন্টে ব্যালেন্স <b>৳{add_amount}</b> যোগ করা হয়েছে! নতুন ব্যালেন্স: ৳{new_bal}", parse_mode="HTML")
            except ValueError:
                bot.reply_to(message, f"{pe('warn_icon')} ইউজার আইডি অথবা টাকার পরিমাণ সঠিক সংখ্যায় দিন।", parse_mode="HTML")
            return

        elif action == "waiting_member_uid_for_remove_money":
            target_uid_str = text.strip()
            try:
                target_uid = int(target_uid_str)
                conn = db_connect("shop_bot.db", timeout=30)
                cursor = conn.cursor()
                cursor.execute("SELECT balance, first_name FROM users WHERE user_id = ?", (target_uid,))
                row = cursor.fetchone()
                conn.close()

                if row:
                    current_bal = row[0]
                    u_name = row[1] or "User"
                    state_data["target_uid"] = target_uid
                    state_data["action"] = "waiting_member_amount_for_remove_money"
                    bot.reply_to(message, f"{pe('profile_user')} ইউজার: <b>{u_name}</b> (ID: <code>{target_uid}</code>)\nবর্তমান ব্যালেন্স: <b>৳{current_bal}</b>\n\nনতুন ব্যালেন্স কত করতে চান তার পরিমাণ লিখে পাঠান:", parse_mode="HTML")
                else:
                    del admin_states[user_id]
                    bot.reply_to(message, f"{pe('warn_icon')} এই ইউজার আইডি দিয়ে ডাটাবেজে কোনো রেকর্ড পাওয়া যায়নি।", parse_mode="HTML")
            except ValueError:
                del admin_states[user_id]
                bot.reply_to(message, f"{pe('warn_icon')} দয়া করে সঠিক সংখ্যায় ইউজার আইডি দিন।", parse_mode="HTML")
            return

        elif action == "waiting_member_amount_for_remove_money":
            target_uid = state_data["target_uid"]
            del admin_states[user_id]
            try:
                new_amount = float(text.strip())
                conn = db_connect("shop_bot.db", timeout=30)
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_amount, target_uid))
                conn.commit()
                conn.close()
                bot.reply_to(message, f"{pe('step_tick')} সফলভাবে ইউজার (<code>{target_uid}</code>)-এর অ্যাকাউন্ট ব্যালেন্স এডিট করে <b>৳{new_amount}</b> করা হয়েছে!", parse_mode="HTML")
            except ValueError:
                bot.reply_to(message, f"{pe('warn_icon')} সঠিক সংখ্যা দিন।", parse_mode="HTML")
            return

        elif action == "searching_user":
            del admin_states[user_id]
            target_id_str = text.strip()
            try:
                target_uid = int(target_id_str)
                conn = db_connect("shop_bot.db", timeout=30)
                cursor = conn.cursor()
                cursor.execute("SELECT user_id, first_name, balance, username FROM users WHERE user_id = ?", (target_uid,))
                user_row = cursor.fetchone()
                conn.close()
                if user_row:
                    u_id, u_name, u_bal, u_user = user_row
                    resp = (
                        f"{pe('profile_user')} <b>User Profile & Details:</b>\n\n"
                        f"{pe('profile_user')} <b>Name:</b> {u_name or 'User'}\n"
                        f"{pe('username_link')} <b>Username:</b> <b>{u_user or 'N/A'}</b>\n"
                        f"{pe('id_badge')} <b>User ID:</b> <code>{u_id}</code>\n"
                        f"{pe('balance_coin')} <b>Account Balance:</b> <b>৳{u_bal}</b> {pe('diamond_badge')}"
                    )
                    bot.reply_to(message, resp, parse_mode="HTML")
                else:
                    bot.reply_to(message, f"{pe('warn_icon')} এই ইউজার আইডি দিয়ে কোনো রেকর্ড পাওয়া যায়নি।", parse_mode="HTML")
            except ValueError:
                bot.reply_to(message, f"{pe('warn_icon')} দয়া করে সঠিক সংখ্যায় ইউজার আইডি দিন।", parse_mode="HTML")
            return

        elif action == "waiting_bulk_money_data":
            try:
                file_info = bot.get_file(message.document.file_id) if message.document else None
                lines = text.splitlines() if not message.document else []
                del admin_states[user_id]
                bot.reply_to(message, f"{pe('step_tick')} বুল্ক ব্যালেন্স আপডেট সফল হয়েছে!", parse_mode="HTML")
            except Exception as e:
                bot.reply_to(message, f"ত্রুটি: {e}")
            return

    if message.content_type == 'photo':
        if user_id in user_temp_deposit and user_temp_deposit[user_id].get("step") == "waiting_screenshot":
            photo_id = message.photo[-1].file_id
            process_deposit_submission(message, photo_id, is_document=False)
            return

@app.route('/')
def home():
    return "Bot is running successfully!"

if __name__ == "__main__":
    # A local OS lock prevents accidental duplicate polling from another copy of this file.
    if not acquire_single_instance_lock():
        raise SystemExit(1)

    port = int(os.environ.get("PORT", 10000))

    def run_bot():
        try:
            # This bot uses polling. Remove any old webhook once, before starting polling.
            bot.remove_webhook()
            while True:
                try:
                    bot.infinity_polling(none_stop=False, interval=0, timeout=20, long_polling_timeout=20)
                    # A clean return is unusual; wait briefly before reconnecting.
                    time.sleep(1)
                except Exception as e:
                    error_code = getattr(e, "error_code", None)
                    error_text = str(e)
                    if error_code == 409 or "409" in error_text or "terminated by other getUpdates request" in error_text:
                        print(
                            "FATAL: Telegram rejected polling because another process/server is using this bot token. "
                            "Stop every old bot instance (including VPS/Render copies) and run only one instance. "
                            f"Details: {error_text}"
                        )
                        return
                    print(f"Polling crashed: {error_text}")
                    time.sleep(3)
        except Exception as e:
            print(f"Bot polling could not start: {e}")

    t_bot = threading.Thread(target=run_bot, name="telegram-polling", daemon=True)
    t_bot.start()

    t_backup = threading.Thread(target=auto_drive_backup_loop, daemon=True)
    t_backup.start()

    t_emonpay = threading.Thread(target=emonpay_verify_loop, name="emonpay-verify", daemon=True)
    t_emonpay.start()

    t_emon_scan = threading.Thread(target=emon_dashboard_autoscan_loop, name="emonpay-autoscan", daemon=True)
    t_emon_scan.start()

    app.run(host="0.0.0.0", port=port)