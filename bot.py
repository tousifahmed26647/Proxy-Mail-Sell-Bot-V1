#!/usr/bin/env python3
# coding: utf-8
"""
Full ready-to-run bot.py
- Replace API_TOKEN and ADMIN_ID with your values
- Uses zoneinfo (Python 3.9+) or falls back to pytz if needed
Requirements (example):
  pip install aiogram pandas openpyxl pytz
"""

import logging
import sqlite3
import pandas as pd
import os
import asyncio
from datetime import datetime, timedelta
try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except Exception:
    import pytz
    _HAS_ZONEINFO = False

from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# --- Config (PUT YOURS HERE) ---
API_TOKEN = "8341197247:AAHRV88aukzqut76TpCLDGWK_EP9gZVhQVw"   # <- replace with your bot token
ADMIN_ID = 8763191242                 # <- replace with your numeric Telegram ID

# set Bangladesh timezone
if _HAS_ZONEINFO:
    TZ = ZoneInfo("Asia/Dhaka")
else:
    TZ = pytz.timezone("Asia/Dhaka")

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Bot init ---
bot = Bot(token=API_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# --- Database ---
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance REAL DEFAULT 0,
    purchased INTEGER DEFAULT 0,
    last_active TEXT
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS stock(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT,
    emailpass TEXT
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS deposits(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    method TEXT,
    number TEXT,
    amount REAL,
    txid TEXT,
    status TEXT DEFAULT 'pending'
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
)""")

# Feature 1: Dynamic Product Price System
cursor.execute("""CREATE TABLE IF NOT EXISTS product_prices(
    product TEXT PRIMARY KEY,
    price REAL
)""")

DEFAULT_PRODUCTS = {
    "Gmail (6-12 Hours)": 9.00,
    "Hotmail (12-36 Months)": 0.85,
    "Outlook (7-12 Months)": 0.85,
    "Login Gmail": 20.00,
    "Owl Proxy": 10.00
}

for prod, prc in DEFAULT_PRODUCTS.items():
    cursor.execute("INSERT OR IGNORE INTO product_prices(product, price) VALUES(?,?)", (prod, prc))

conn.commit()

# Feature 2: Helper Functions
def get_price(product: str) -> float:
    cursor.execute("SELECT price FROM product_prices WHERE product=?", (product,))
    row = cursor.fetchone()
    if row:
        return row[0]
    return DEFAULT_PRODUCTS.get(product, 0.0)

def set_price(product: str, price: float):
    cursor.execute("INSERT OR REPLACE INTO product_prices(product, price) VALUES(?,?)", (product, price))
    conn.commit()

# --- State trackers ---
admin_price_state = {}
admin_stock_state = {}

# --- Helpers ---
def now_utc():
    return datetime.utcnow()

def now_bangla():
    """Return current datetime in Bangladesh tz as a datetime object."""
    if _HAS_ZONEINFO:
        return datetime.now(TZ)
    else:
        return datetime.now(pytz.utc).astimezone(TZ)

def to_iso(dt: datetime):
    return dt.isoformat()

def from_iso(s: str):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        # fallback: naive parse
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
        except Exception:
            return None

def fmt_time_12(dt: datetime):
    """12-hour format with AM/PM in Bangladesh time."""
    if dt is None:
        return "-"
    # ensure dt is timezone-aware in Bangladesh
    if _HAS_ZONEINFO:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt = dt.astimezone(TZ)
    else:
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        dt = dt.astimezone(TZ)
    return dt.strftime("%I:%M:%S %p")  # 12-hour

def fmt_date(dt: datetime):
    if dt is None:
        return "-"
    if _HAS_ZONEINFO:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt = dt.astimezone(TZ)
    else:
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        dt = dt.astimezone(TZ)
    return dt.strftime("%Y-%m-%d")

def set_last_active(uid: int, username: str = None):
    uname = username or "NoUsername"
    ts = to_iso(now_utc())
    cursor.execute("INSERT OR IGNORE INTO users(user_id, username, last_active) VALUES(?,?,?)", (uid, uname, ts))
    cursor.execute("UPDATE users SET username=?, last_active=? WHERE user_id=?", (uname, ts, uid))
    conn.commit()

# async notifier
async def async_notify_all(text):
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    sent = 0
    failed = 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    logger.info(f"Notify finished. Sent: {sent}, Failed: {failed}")

def notify_all_users(text):
    try:
        asyncio.create_task(async_notify_all(text))
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.create_task(async_notify_all(text))

# --- Menus ---
def main_menu(is_admin=False):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)

    if is_admin:
        kb.add(KeyboardButton("⚙️Admin Panel⚙️"))

    kb.add(KeyboardButton("📮 Buy Mail"), KeyboardButton("📧 Code Box"))
    kb.add(KeyboardButton("🛡️ Proxy Panel"))
    kb.add(KeyboardButton("💲 Deposit"), KeyboardButton("💳 Balance"))
    kb.add(KeyboardButton("🤖 Support"))

    return kb

def admin_panel_markup():
    kb = InlineKeyboardMarkup(row_width=2)

    kb.add(
        InlineKeyboardButton("📂 Upload Stock", callback_data="admin_stock"),
        InlineKeyboardButton("🗑 Remove Stock", callback_data="admin_removestock")
    )

    kb.add(
        InlineKeyboardButton("💲 Update Product Price", callback_data="admin_price"),
        InlineKeyboardButton("📂 Pending Deposits", callback_data="admin_deposits")
    )

    kb.add(
        InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
        InlineKeyboardButton("👥 Active Users", callback_data="admin_users")
    )

    kb.add(
        InlineKeyboardButton("👤 User List", callback_data="admin_userbalances"),
        InlineKeyboardButton("⚡ Balance Control", callback_data="admin_usercontrol")
    )

    kb.add(
        InlineKeyboardButton("🆘 Support", callback_data="admin_support"),
        InlineKeyboardButton("📣 Broadcast", callback_data="admin_broadcast")
    )

    return kb

# --- Handlers ---
@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    uid = message.from_user.id
    uname = message.from_user.username or "NoUsername"
    set_last_active(uid, uname)
    await message.answer("👋 স্বাগতম! আপনার কি Products চাই?", reply_markup=main_menu(uid == ADMIN_ID))

# Balance
@dp.message_handler(lambda m: m.text == "💳 Balance")
async def balance_cmd(message: types.Message):
    set_last_active(message.from_user.id, message.from_user.username)

    uid = message.from_user.id
    username = message.from_user.username or "NoUsername"
    fullname = message.from_user.full_name

    cursor.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    row = cursor.fetchone()
    bal = row[0] if row else 0.0

    text = (
        f"💳 <b>Your Balance</b>\n\n"
        f"👤 <b>Name:</b> {fullname}\n"
        f"📛 <b>Username:</b> @{username}\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n\n"
        f"💰 <b>Real Balance:</b> <code>{bal:.2f}</code> TK"
    )

    await message.answer(text)

# ==========================
# Deposit
# ==========================

@dp.message_handler(lambda m: m.text == "💲 Deposit")
async def deposit_cmd(message: types.Message):
    set_last_active(message.from_user.id, message.from_user.username)

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📲 bKash", callback_data="dep_bkash"))
    kb.add(InlineKeyboardButton("📲 Nagad", callback_data="dep_nagad"))

    await message.answer(
        "💳 <b>Select Payment Method</b>\n\n"
        "⚠️ Minimum Deposit: <b>50 TK</b>",
        reply_markup=kb
    )


user_deposit = {}


# ==========================
# Payment Method
# ==========================

@dp.callback_query_handler(lambda c: c.data.startswith("dep_"))
async def dep_method(call: types.CallbackQuery):

    uid = call.from_user.id

    method = call.data.replace("dep_", "")

    user_deposit[uid] = {
        "method": method
    }

    number = "01767916048" if method == "bkash" else "01576978543"

    await call.message.answer(

        f"💳 <b>{method.title()} Payment</b>\n\n"

        f"📲 Send Money To\n"
        f"<code>{number}</code>\n\n"

        "💵 এখন Deposit Amount লিখুন:"
    )

    await call.answer()


# ==========================
# Amount
# ==========================

@dp.message_handler(lambda m: m.from_user.id in user_deposit and "amount" not in user_deposit[m.from_user.id])
async def dep_amount(message: types.Message):

    uid = message.from_user.id

    try:
        amount = float(message.text.strip())
    except:
        await message.answer("❌ সঠিক Amount লিখুন।")
        return

    if amount < 50:
        await message.answer("❌ Minimum Deposit is 50 TK.")
        return

    user_deposit[uid]["amount"] = amount

    await message.answer(
        "🔑 এখন Transaction ID পাঠান:"
    )


# ==========================
# Transaction ID
# ==========================

@dp.message_handler(lambda m: m.from_user.id in user_deposit and "txid" not in user_deposit[m.from_user.id])
async def dep_txid(message: types.Message):

    uid = message.from_user.id

    data = user_deposit[uid]

    txid = message.text.strip()

    data["txid"] = txid

    cursor.execute(
        "INSERT INTO deposits(user_id, method, number, amount, txid) VALUES(?,?,?,?,?)" ,
        (
            uid,
            data["method"],
            "",
            data["amount"],
            txid
        )
    )

    conn.commit()

    dep_id = cursor.lastrowid

    kb = InlineKeyboardMarkup(row_width=2)

    kb.add(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"approve_{dep_id}"
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject_{dep_id}"
        )
    )

    fullname = message.from_user.full_name
    username = message.from_user.username or "NoUsername"

    admin_text = (
        "💰 <b>NEW DEPOSIT REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"👤 <b>Name :</b> {fullname}\n"
        f"📛 <b>Username :</b> @{username}\n"
        f"🆔 <b>User ID :</b> <code>{uid}</code>\n\n"

        "💳 <b>Payment Information</b>\n"

        f"├ Method : <b>{data['method'].title()}</b>\n"
        f"├ Amount : <b>{data['amount']:.2f} TK</b>\n"
        f"├ TxID : <code>{txid}</code>\n"
        f"└ Deposit ID : <code>#{dep_id}</code>\n\n"

        "👇 <b>Select an action</b>"
    )

    await bot.send_message(
        ADMIN_ID,
        admin_text,
        reply_markup=kb
    )

    await message.answer(
        "✅ Deposit Request Sent Successfully.\n\n"
        "⏳ Please wait for Approval."
    )

    del user_deposit[uid]


# ==========================
# Approve / Reject
# ==========================

@dp.callback_query_handler(lambda c: c.data.startswith("approve_") or c.data.startswith("reject_"))
async def dep_admin(call: types.CallbackQuery):

    dep_id = int(call.data.split("_")[1])

    cursor.execute(
        "SELECT user_id, amount FROM deposits WHERE id=?",
        (dep_id,)
    )

    dep = cursor.fetchone()

    if not dep:
        await call.answer("Deposit Not Found")
        return

    uid, amount = dep

    if call.data.startswith("approve"):

        cursor.execute(
            "UPDATE deposits SET status='approved' WHERE id=?",
            (dep_id,)
        )

        cursor.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (amount, uid)
        )

        conn.commit()

        try:
            await bot.send_message(
                uid,
                f"✅ Your Deposit of {amount:.2f} TK has been Approved."
            )
        except:
            pass

        await call.message.edit_text("✅ Deposit Approved.")

    else:

        cursor.execute(
            "UPDATE deposits SET status='rejected' WHERE id=?",
            (dep_id,)
        )

        conn.commit()

        try:
            await bot.send_message(
                uid,
                "❌ Your Deposit has been Rejected."
            )
        except:
            pass

        await call.message.edit_text("❌ Deposit Rejected.")

    await call.answer()

# Get Mail / buy flows
@dp.message_handler(lambda m: m.text == "📮 Buy Mail")
async def get_mail(message: types.Message):
    set_last_active(message.from_user.id, message.from_user.username)
    kb = InlineKeyboardMarkup()
    mail_products = [
        "Gmail (6-12 Hours)",
        "Hotmail (12-36 Months)",
        "Outlook (7-12 Months)",
        "Login Gmail"
    ]
    for service in mail_products:
        price = get_price(service)
        cursor.execute("SELECT COUNT(*) FROM stock WHERE service=?", (service,))
        stock = cursor.fetchone()[0]
        kb.add(InlineKeyboardButton(f"{service} | {price} tk | Stock: {stock}", callback_data=f"buy_{service}"))
    kb.add(InlineKeyboardButton("🛒 Multiple Purchase", callback_data="multi_purchase"))
    await message.answer("📧 Available Mail:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("buy_") and c.data != "buy_proxy")
async def buy_one(call: types.CallbackQuery):
    uid = call.from_user.id
    set_last_active(uid, call.from_user.username)
    service = call.data.replace("buy_", "")
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    res = cursor.fetchone()
    bal = res[0] if res else 0.0
    price = get_price(service)
    if bal < price:
        await call.message.answer("❌ Not enough balance!")
        await call.answer()
        return
    cursor.execute("SELECT id,emailpass FROM stock WHERE service=? ORDER BY RANDOM() LIMIT 1", (service,))
    item = cursor.fetchone()
    if not item:
        await call.message.answer("❌ Out of stock!")
        await call.answer()
        return
    stock_id, emailpass = item
    cursor.execute("DELETE FROM stock WHERE id=?", (stock_id,))
    cursor.execute("UPDATE users SET balance=balance-?, purchased=purchased+1 WHERE user_id=?", (price, uid))
    conn.commit()
    await call.message.answer(f"✅ Purchase successful!\n\n<code>{emailpass}</code>")
    await call.answer()

# Multi purchase
multi_step = {}

@dp.callback_query_handler(lambda c: c.data == "multi_purchase")
async def multi_start(call: types.CallbackQuery):
    uid = call.from_user.id
    set_last_active(uid, call.from_user.username)
    kb = InlineKeyboardMarkup()
    mail_products = [
        "Gmail (6-12 Hours)",
        "Hotmail (12-36 Months)",
        "Outlook (7-12 Months)",
        "Login Gmail"
    ]
    for s in mail_products:
        kb.add(InlineKeyboardButton(s, callback_data=f"multi_{s}"))
    await call.message.answer("🛒 Select service for multiple purchase:", reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("multi_"))
async def multi_service(call: types.CallbackQuery):
    uid = call.from_user.id
    set_last_active(uid, call.from_user.username)
    service = call.data.replace("multi_", "")
    multi_step[uid] = {"service": service}
    await call.message.answer("✍️ Enter how many mails you want:")
    await call.answer()

@dp.message_handler(lambda m: m.from_user.id in multi_step and "count" not in multi_step[m.from_user.id])
async def multi_count(message: types.Message):
    uid = message.from_user.id
    set_last_active(uid, message.from_user.username)
    if not message.text.isdigit():
        await message.answer("❌ Enter a valid number.")
        return
    count = int(message.text)
    multi_step[uid]["count"] = count
    service = multi_step[uid]["service"]
    price = get_price(service) * count
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    res = cursor.fetchone()
    bal = res[0] if res else 0.0
    cursor.execute("SELECT id,emailpass FROM stock WHERE service=? LIMIT ?", (service, count))
    items = cursor.fetchall()
    if len(items) < count:
        await message.answer("❌ Not enough stock!")
        del multi_step[uid]
        return
    if bal < price:
        await message.answer("❌ Not enough balance!")
        del multi_step[uid]
        return
    # deduct stock & update user
    mails = []
    for stock_id, emailpass in items:
        cursor.execute("DELETE FROM stock WHERE id=?", (stock_id,))
        mails.append(emailpass)
    cursor.execute("UPDATE users SET balance=balance-?, purchased=purchased+? WHERE user_id=?", (price, count, uid))
    conn.commit()
    # send file if many
    if count >= 5:
        fname = f"{uid}_mails.txt"
        with open(fname, "w", encoding="utf-8") as f:
            f.write("\n".join(mails))
        await message.answer_document(open(fname, "rb"), caption=f"✅ Purchased {count} {service}")
        try:
            os.remove(fname)
        except: pass
    else:
        text = "\n".join([f"<code>{m}</code>" for m in mails])
        await message.answer(f"✅ Purchased {count} {service}\n\n{text}")
    del multi_step[uid]

# Proxy Panel
@dp.message_handler(lambda m: m.text == "🛡️ Proxy Panel")
async def proxy_panel(message: types.Message):
    set_last_active(message.from_user.id, message.from_user.username)

    cursor.execute("SELECT COUNT(*) FROM stock WHERE service=?", ("Owl Proxy",))
    stock = cursor.fetchone()[0]

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            f"🦉 Owl Proxy | Stock: {stock}",
            callback_data="buy_proxy"
        )
    )

    await message.answer(
        "🛡️ <b>Proxy Panel</b>\n\n"
        "Select a service:",
        reply_markup=kb
    )

# Proxy Buying Logic
@dp.callback_query_handler(lambda c: c.data == "buy_proxy")
async def buy_proxy(call: types.CallbackQuery):
    uid = call.from_user.id
    set_last_active(uid, call.from_user.username)
    service = "Owl Proxy"
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    res = cursor.fetchone()
    bal = res[0] if res else 0.0
    price = get_price(service)
    if bal < price:
        await call.message.answer("❌ Not enough balance!")
        await call.answer()
        return
    cursor.execute("SELECT id,emailpass FROM stock WHERE service=? ORDER BY RANDOM() LIMIT 1", (service,))
    item = cursor.fetchone()
    if not item:
        await call.message.answer("❌ Out of stock!")
        await call.answer()
        return
    stock_id, emailpass = item
    cursor.execute("DELETE FROM stock WHERE id=?", (stock_id,))
    cursor.execute("UPDATE users SET balance=balance-?, purchased=purchased+1 WHERE user_id=?", (price, uid))
    conn.commit()
    await call.message.answer(f"✅ Purchase successful!\n\n<code>{emailpass}</code>")
    await call.answer()

# Inbox / Support / Tutorial
@dp.message_handler(lambda m: m.text == "📧 Code Box")
async def inbox(message: types.Message):
    set_last_active(message.from_user.id, message.from_user.username)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📧 Gmail Inbox", callback_data="inbox_gmail"))
    kb.add(InlineKeyboardButton("📧 Hotmail Inbox", callback_data="inbox_hotmail"))
    kb.add(InlineKeyboardButton("📧 Outlook Inbox", callback_data="inbox_outlook"))
    await message.answer("📥 কোন ইনবক্স চেক করবেন?", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("inbox_"))
async def inbox_links(call: types.CallbackQuery):
    if call.data == "inbox_gmail":
        await call.message.answer("➡️ Gmail কোড পাওয়ার জন্য যান: @CodeReciever71bot")
    else:
        await call.message.answer("➡️ Hotmail/Outlook কোডের জন্য যান:\nhttps://dongvanfb.net/read_mail_box/")
    await call.answer()

@dp.message_handler(lambda m: m.text == "📚 Tutorial")
async def tutorial(message: types.Message):
    set_last_active(message.from_user.id, message.from_user.username)
    cursor.execute("SELECT value FROM settings WHERE key='tutorial_link'")
    row = cursor.fetchone()
    if row:
        link = row[0]
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("📚 Mail Bot Tutorial", url=link))
        await message.answer("📚 Mail Bot Tutorial\n\nনিচের বাটনে ক্লিক করে Tutorial Video দেখুন! 🎯", reply_markup=kb)
    else:
        await message.answer("❌ Tutorial link not set. Admin দয়া করে /admin panel থেকে সেট করবেন।")

@dp.message_handler(lambda m: m.text == "🤖 Support")
async def support(message: types.Message):
    set_last_active(message.from_user.id, message.from_user.username)
    cursor.execute("SELECT value FROM settings WHERE key='support_username'")
    row = cursor.fetchone()
    if row:
        uname = row[0]
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🤖 Support", url=f"https://t.me/{uname}"))
        await message.answer("যদি কোনো সমস্যায় পড়েন, নিচের Mail Bot Support বাটনে ক্লিক করুন", reply_markup=kb)
    else:
        await message.answer("❌ Support not set by admin.")

# Admin panel entry
@dp.message_handler(lambda m: m.text == "⚙️Admin Panel⚙️" and m.from_user.id == ADMIN_ID)
async def admin_panel(message: types.Message):
    set_last_active(message.from_user.id, message.from_user.username)
    await message.answer("⚙️ Admin Panel ⚙️", reply_markup=admin_panel_markup())

# Feature 5 & 6: Update Product Price Handlers
@dp.callback_query_handler(lambda c: c.data == "admin_price" and c.from_user.id == ADMIN_ID)
async def admin_price_panel(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📧 Mail Panel", callback_data="price_cat_mail"),
        InlineKeyboardButton("🛡 Proxy Panel", callback_data="price_cat_proxy")
    )
    await call.message.answer("Choose Category for Price Update:", reply_markup=kb)
    await call.answer()

# Feature 7: Price - Mail Panel
@dp.callback_query_handler(lambda c: c.data == "price_cat_mail" and c.from_user.id == ADMIN_ID)
async def price_mail_panel(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📩 Gmail (6-12 Hours)", callback_data="setpr_Gmail (6-12 Hours)"),
        InlineKeyboardButton("📩 Hotmail (12-36 Months)", callback_data="setpr_Hotmail (12-36 Months)"),
        InlineKeyboardButton("📩 Outlook (7-12 Months)", callback_data="setpr_Outlook (7-12 Months)"),
        InlineKeyboardButton("📩 Login Gmail", callback_data="setpr_Login Gmail")
    )
    await call.message.answer("Select Mail Product:", reply_markup=kb)
    await call.answer()

# Feature 9: Price - Proxy Panel
@dp.callback_query_handler(lambda c: c.data == "price_cat_proxy" and c.from_user.id == ADMIN_ID)
async def price_proxy_panel(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🦉 Owl Proxy", callback_data="setpr_Owl Proxy")
    )
    await call.message.answer("Select Proxy Product:", reply_markup=kb)
    await call.answer()

# Feature 8: Price Selection & Input Prompt
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("setpr_") and c.from_user.id == ADMIN_ID)
async def price_select_product(call: types.CallbackQuery):
    product = call.data.replace("setpr_", "")
    curr_price = get_price(product)
    admin_price_state[call.from_user.id] = {
        "product": product,
        "old_price": curr_price
    }
    await call.message.answer(f"Current Price : {curr_price:.2f} TK\nSend new price.")
    await call.answer()

# Feature 10: Upload Stock Category Selection
@dp.callback_query_handler(lambda c: c.data == "admin_stock" and c.from_user.id == ADMIN_ID)
async def admin_stock_category(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📧 Mail Panel", callback_data="stock_cat_mail"),
        InlineKeyboardButton("🛡 Proxy Panel", callback_data="stock_cat_proxy")
    )
    await call.message.answer("Choose Category", reply_markup=kb)
    await call.answer()

# Feature 11: Upload Stock - Mail Panel
@dp.callback_query_handler(lambda c: c.data == "stock_cat_mail" and c.from_user.id == ADMIN_ID)
async def stock_mail_panel(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📩 Gmail (6-12 Hours)", callback_data="upstk_Gmail (6-12 Hours)"),
        InlineKeyboardButton("📩 Hotmail (12-36 Months)", callback_data="upstk_Hotmail (12-36 Months)"),
        InlineKeyboardButton("📩 Outlook (7-12 Months)", callback_data="upstk_Outlook (7-12 Months)"),
        InlineKeyboardButton("📩 Login Gmail", callback_data="upstk_Login Gmail")
    )
    await call.message.answer("Select Product to Upload Stock:", reply_markup=kb)
    await call.answer()

# Feature 12: Upload Stock - Proxy Panel
@dp.callback_query_handler(lambda c: c.data == "stock_cat_proxy" and c.from_user.id == ADMIN_ID)
async def stock_proxy_panel(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🦉 Owl Proxy", callback_data="upstk_Owl Proxy")
    )
    await call.message.answer("Select Product to Upload Stock:", reply_markup=kb)
    await call.answer()

# Upload Stock Product Selected
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("upstk_") and c.from_user.id == ADMIN_ID)
async def stock_product_select(call: types.CallbackQuery):
    product = call.data.replace("upstk_", "")
    admin_stock_state[call.from_user.id] = product
    await call.message.answer(f"Please send TXT file for <b>{product}</b>.")
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_removestock")
async def admin_removestock_prompt(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup()
    products = [
        "Gmail (6-12 Hours)",
        "Hotmail (12-36 Months)",
        "Outlook (7-12 Months)",
        "Login Gmail",
        "Owl Proxy"
    ]
    for service in products:
        kb.add(InlineKeyboardButton(service, callback_data=f"rem_{service}"))
    await call.message.answer("🗑 Select product to clear stock:", reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("rem_"))
async def rem_stock(call: types.CallbackQuery):
    service = call.data.replace("rem_", "")
    cursor.execute("DELETE FROM stock WHERE service=?", (service,))
    conn.commit()
    await call.message.answer(f"🗑 All stock removed for {service}.")
    notify_text = f"📢 Notice: সব স্টক মুছে দেয়া হয়েছে - <b>{service}</b>।"
    notify_all_users(notify_text)
    await call.answer()

@dp.message_handler(content_types=['document'])
async def handle_file(message: types.Message):
    # Only admin should upload stock via document
    if message.from_user.id != ADMIN_ID:
        return
    
    # Check if admin selected product via category menu
    service = admin_stock_state.get(message.from_user.id)
    if not service:
        # Fallback to caption if available
        if message.caption:
            service = message.caption.strip()
        else:
            await message.answer("❌ Please select a product from Upload Stock menu first!")
            return

    # download file
    file = await message.document.download()
    filename = file.name
    try:
        if filename.lower().endswith(".txt"):
            with open(filename, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
        elif filename.lower().endswith(".csv"):
            df = pd.read_csv(filename, header=None, encoding='utf-8', dtype=str)
            lines = df.iloc[:, 0].dropna().astype(str).tolist()
        elif filename.lower().endswith(".xlsx") or filename.lower().endswith(".xls"):
            df = pd.read_excel(filename, header=None)
            lines = df.iloc[:, 0].dropna().astype(str).tolist()
        else:
            await message.answer("❌ Unsupported file type. Use .txt/.csv/.xlsx")
            return

        for line in lines:
            cursor.execute("INSERT INTO stock(service,emailpass) VALUES(?,?)", (service, line.strip()))
        conn.commit()

        # Clear state
        if message.from_user.id in admin_stock_state:
            del admin_stock_state[message.from_user.id]

        await message.answer(f"✅ Uploaded {len(lines)} stock for {service}.\n📢 Sending notifications to users...")
        notify_text = f"📢 নতুন স্টক আপলোড হয়েছে: <b>{service}</b>\nযাচাই করতে /📧 Get Mail এ যান।"
        notify_all_users(notify_text)
    except Exception as e:
        logging.exception("Error processing file")
        await message.answer(f"❌ Error while processing file: {e}")

# Admin: deposits view
@dp.callback_query_handler(lambda c: c.data == "admin_deposits")
async def admin_deposits(call: types.CallbackQuery):
    cursor.execute("SELECT id,user_id,method,number,amount,txid,status FROM deposits ORDER BY id DESC")
    rows = cursor.fetchall()
    if not rows:
        await call.message.answer("✅ No deposits found.")
        await call.answer()
        return
    for r in rows:
        dep_id, uid, method, number, amount, txid, status = r
        kb = InlineKeyboardMarkup()
        if status == "pending":
            kb.add(InlineKeyboardButton("✅ Approve", callback_data=f"approve_{dep_id}"),
                   InlineKeyboardButton("❌ Reject", callback_data=f"reject_{dep_id}"))
        await call.message.answer(f"🆔 Deposit ID: {dep_id}\n👤 UserID: {uid}\n💳 Method: {method}\n📞 Number: {number}\n💵 Amount: {amount}\n🔑 TxID: {txid}\nStatus: {status}", reply_markup=kb)
    await call.answer()

# Admin: user balances / list
@dp.callback_query_handler(lambda c: c.data == "admin_userbalances")
async def admin_userbalances(call: types.CallbackQuery):
    cursor.execute("SELECT user_id,username,balance FROM users ORDER BY balance DESC LIMIT 50")
    rows = cursor.fetchall()
    if not rows:
        await call.message.answer("No users found.")
        await call.answer()
        return
    text = "👥 Top Users (by balance):\n\n"
    for uid, uname, bal in rows:
        text += f"@{uname} ({uid}) - {bal:.2f} tk\n"
    await call.message.answer(text)
    await call.answer()

# Admin: balance control
@dp.message_handler(commands=['addbal', 'setbal', 'delbal'])
async def bal_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.answer("Usage: /addbal user_id amount  OR /setbal user_id amount  OR /delbal user_id amount")
            return
        cmd, uid_s, amt_s = parts
        uid = int(uid_s)
        amt = float(amt_s)
        if cmd == "/addbal":
            cursor.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
        elif cmd == "/setbal":
            cursor.execute("UPDATE users SET balance=? WHERE user_id=?", (amt, uid))
        elif cmd == "/delbal":
            cursor.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amt, uid))
        conn.commit()
        await message.answer("✅ Balance updated.")
    except Exception as e:
        await message.answer(f"❌ Error: {e}")

# Admin: set support / tutorial
@dp.callback_query_handler(lambda c: c.data == "admin_support")
async def admin_support(call: types.CallbackQuery):
    await call.message.answer("✍️ Send new support username (without @)")
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_tutorial")
async def admin_tutorial(call: types.CallbackQuery):
    await call.message.answer("✍️ Send new tutorial link (http:// or https://)")
    await call.answer()

@dp.message_handler(lambda m: m.from_user.id == ADMIN_ID and m.text and not m.text.startswith("/"))
async def admin_set_text(message: types.Message):
    # Handle price update text input
    if message.from_user.id in admin_price_state:
        st = admin_price_state[message.from_user.id]
        try:
            new_price = float(message.text.strip())
        except ValueError:
            await message.answer("❌ Invalid price. Please enter a valid number.")
            return
        
        product = st["product"]
        old_price = st["old_price"]
        set_price(product, new_price)
        del admin_price_state[message.from_user.id]
        
        await message.answer(
            f"✅ Price Updated\n\n"
            f"Product: {product}\n"
            f"Old Price: {old_price:.2f} TK\n"
            f"New Price: {new_price:.2f} TK"
        )
        return

    # If a broadcast is pending, broadcast handler will use this text so we skip handling here
    cursor.execute("SELECT value FROM settings WHERE key='awaiting_broadcast'")
    row_flag = cursor.fetchone()
    if row_flag and row_flag[0] == '1':
        return
    text = message.text.strip()
    if text.startswith("http"):
        cursor.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('tutorial_link',?)", (text,))
        await message.answer("✅ Tutorial link updated.")
    else:
        uname = text.replace("@", "").strip()
        cursor.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('support_username',?)", (uname,))
        await message.answer("✅ Support username updated.")
    conn.commit()

# Admin: broadcast
@dp.callback_query_handler(lambda c: c.data == "admin_broadcast")
async def admin_broadcast_trigger(call: types.CallbackQuery):
    await call.message.answer("✍️ Send the message you want to broadcast to ALL users.\n\n(Tip: plain text only.)")
    cursor.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('awaiting_broadcast','1')")
    conn.commit()
    await call.answer()

@dp.message_handler(lambda m: m.from_user.id == ADMIN_ID, content_types=['text'])
async def catch_admin_broadcast_message(message: types.Message):
    cursor.execute("SELECT value FROM settings WHERE key='awaiting_broadcast'")
    row = cursor.fetchone()
    if not (row and row[0] == '1'):
        return
    text = message.text
    cursor.execute("DELETE FROM settings WHERE key='awaiting_broadcast'")
    conn.commit()
    await message.answer("📣 Broadcast started. Sending to all users...")
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    sent = 0
    failed = 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, f"📣 Broadcast from Admin:\n\n{text}", disable_web_page_preview=True)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Broadcast finished. Sent: {sent}, Failed: {failed}")

# Admin: Active Users (realtime) & Bot Stats (Bangladesh time, 12-hour)
@dp.callback_query_handler(lambda c: c.data == "admin_users")
async def active_users(call: types.CallbackQuery):
    cursor.execute("SELECT last_active FROM users")
    rows = cursor.fetchall()
    total = 0
    new_today = 0
    online = 0
    active15 = 0
    active60 = 0
    now = now_utc()
    for (last,) in rows:
        if last:
            dt = from_iso(last)
            if dt:
                total += 1
                # new today using Bangladesh date
                # convert dt to Bangladesh tz and compare date
                if _HAS_ZONEINFO:
                    d_b = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
                else:
                    if dt.tzinfo is None:
                        dt = pytz.utc.localize(dt)
                    d_b = dt.astimezone(TZ)
                if d_b.date() == now_bangla().date():
                    new_today += 1
                delta = now - dt
                if delta <= timedelta(minutes=5):
                    online += 1
                if delta <= timedelta(minutes=15):
                    active15 += 1
                if delta <= timedelta(minutes=60):
                    active60 += 1
    # include users with NULL last_active
    cursor.execute("SELECT COUNT(*) FROM users WHERE last_active IS NULL")
    null_count = cursor.fetchone()[0]
    total += null_count

    text = (f"👥 Active Users (Realtime)\n\n"
            f"Total Users: {total}\n"
            f"New Today: {new_today}\n"
            f"🟢 Online (≤5m): {online}\n"
            f"🟡 Active (≤15m): {active15}\n"
            f"🔵 Active (≤60m): {active60}")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="admin_users"))
    kb.add(InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="back_admin"))
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_stats")
async def bot_stats(call: types.CallbackQuery):
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    # compute new today by last_active in Bangla date
    cursor.execute("SELECT last_active FROM users")
    rows = cursor.fetchall()
    new_today = 0
    for (last,) in rows:
        if last:
            dt = from_iso(last)
            if dt:
                if _HAS_ZONEINFO:
                    dt_b = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
                else:
                    if dt.tzinfo is None:
                        dt = pytz.utc.localize(dt)
                    dt_b = dt.astimezone(TZ)
                if dt_b.date() == now_bangla().date():
                    new_today += 1
    now_b = now_bangla()
    text = (f"🏹 BOT STATISTICS DASHBOARD 🏹\n"
            f"🕒 Current Time: {now_b.strftime('%I:%M:%S %p')}\n"
            f"📅 Date: {now_b.strftime('%Y-%m-%d')}\n"
            f"🔁 Last Updated: {now_b.strftime('%I:%M:%S %p')}\n"
            f"——————————————\n"
            f"👥 USER STATISTICS\n"
            f"Today (active): {new_today}\n"
            f"Total users: {total_users}\n"
            f"New Today: {new_today}")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats"))
    kb.add(InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="back_admin"))
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "back_admin")
async def back_admin(call: types.CallbackQuery):
    try:
        await call.message.edit_text("⚙️ Admin Panel ⚙️", reply_markup=admin_panel_markup())
    except:
        await call.message.answer("⚙️ Admin Panel ⚙️", reply_markup=admin_panel_markup())
    await call.answer()

# simple admin convenience
@dp.message_handler(commands=['users'])
async def cmd_users(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]
    await message.answer(f"Total users: {total}")

# Run
if __name__ == "__main__":
    try:
        logger.info("Starting bot...")
        executor.start_polling(dp, skip_updates=True)
    finally:
        conn.close()
