import asyncio
import sqlite3
import io
import csv
import pyotp
import re
import imaplib
import email
import pandas as pd
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, WebAppInfo
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openpyxl import Workbook

# 🤖 আপনার টোকেন ও অ্যাডমিন আইডি
TOKEN = "8341197247:AAEVT80O1BBuhwaqJQXdYEdwuBhhzdDflDg"
ADMIN_ID = 8763191242

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- ডাটাবেজ সেটআপ ও টেবিল নিশ্চিতকরণ ---
conn = sqlite3.connect("bot_shop.db", check_same_thread=False)
cursor = conn.cursor()

def db_setup():
    # প্রোডাক্ট টেবিল
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        price INTEGER
    )
    """)
    # স্টক টেবিল
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS stocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_name TEXT,
        account_data TEXT
    )
    """)
    # ইউজার টেবিল
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0
    )
    """)
    # সেটিংস টেবিল
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    # ডিপোজিট মেথড টেবিল 
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payment_methods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        details TEXT
    )
    """)
    # বিক্রিত অ্যাকাউন্টের হিস্টোরি 
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sold_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        email TEXT,
        password TEXT,
        full_data TEXT,
        sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # প্রক্সি ক্যাটাগরি টেবিল 
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS proxy_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        price INTEGER
    )
    """)
    # অফার ক্যাটাগরি টেবিল 
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS offer_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        price INTEGER
    )
    """)
    # ভিপিএন ক্যাটাগরি টেবিল
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vpn_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        price INTEGER
    )
    """)
    
    # ব্যালেন্স বা ব্যান কলাম না থাকলে তা অ্যাড করা (সেফটি মেজার)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass # ইতিমধ্যে কলাম থাকলে এরর ইগনোর করবে
        
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('support_link', 'https://t.me/Jihad')")
    conn.commit()

db_setup()

# --- ব্যান চেক ফিল্টার ---
def is_user_banned(user_id: int) -> bool:
    try:
        cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        if res and res[0] == 1:
            return True
    except Exception:
        pass
    return False

# --- অটোমেটিক নোটিফিকেশন ব্রডকাস্ট ফাংশন ---
async def notify_all_members(product_name: str, count: int):
    cursor.execute("SELECT user_id FROM users")
    all_users = cursor.fetchall()
    
    formatted_time = datetime.now().strftime('%d-%m-%Y, %I:%M %p')
    notification_text = (
        f"📢 **নতুন স্টক যুক্ত করা হয়েছে!**\n\n"
        f"📦 **প্রোডাক্টের নাম:** {product_name}\n"
        f"📊 **পরিমাণ:** {count} পিস\n"
        f"📅 **সময়:** {formatted_time}\n\n"
        f"🛒 এখনই কিনতে বটের /start মেনু থেকে **Buy Now** বাটনে ক্লিক করুন!"
    )
    
    # ব্যাকগ্রাউন্ডে এসিনক্রোনাসলি মেসেজ পাঠাবে যেন মেইন প্রসেস আটকে না যায়
    async def send_to_user(uid):
        try:
            await bot.send_message(chat_id=uid, text=notification_text, parse_mode="Markdown")
        except Exception:
            pass

    tasks = [send_to_user(row[0]) for row in all_users]
    await asyncio.gather(*tasks)

# --- FSM States ---
class AdminStates(StatesGroup):
    waiting_for_product_name = State()
    waiting_for_product_price = State()
    waiting_for_stock_file = State()
    waiting_for_user_id = State()
    waiting_for_amount = State()
    waiting_for_support_link = State()
    waiting_for_method_name = State()
    waiting_for_method_details = State()
    
    waiting_for_proxy_name = State()
    waiting_for_proxy_price = State()
    waiting_for_proxy_stock_file = State()
    
    waiting_for_offer_name = State()
    waiting_for_offer_price = State()
    waiting_for_offer_stock_file = State()
    
    waiting_for_vpn_name = State()
    waiting_for_vpn_price = State()
    waiting_for_vpn_stock_file = State()

    waiting_for_broadcast_msg = State()
    waiting_for_new_price = State()
    
    waiting_for_ban_user_id = State()
    waiting_for_unban_user_id = State()

class UserStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_format_selection = State()
    waiting_for_2fa_key = State()
    waiting_for_deposit_amount = State()
    waiting_for_deposit_trxid = State()
    waiting_for_deposit_screenshot = State()
    waiting_for_mail_to_code = State()

# --- মেইন কিবোর্ড মেনু ---
def get_main_keyboard(user_id):
    buttons = [
        [KeyboardButton(text="🛒 Buy Now"), KeyboardButton(text="🔑 Mail Code")],
        [KeyboardButton(text="🛡️ 2FA Code")],
        [KeyboardButton(text="💵 Deposit"), KeyboardButton(text="📊 My Account")],
        [KeyboardButton(text="💬 Support")]
    ]
    if user_id == ADMIN_ID:
        buttons.append([KeyboardButton(text="🛠️ Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# --- অ্যাডমিন ইনলাইন প্যানেল ---
def get_admin_inline():
    builder = InlineKeyboardBuilder()
    
    # মেইল প্রোডাক্টস
    builder.add(InlineKeyboardButton(text="➕ নতুন মেইল যোগ", callback_data="add_product"))
    builder.add(InlineKeyboardButton(text="❌ মেইল ডিলিট", callback_data="delete_product_list"))
    builder.add(InlineKeyboardButton(text="📁 মেইল স্টক আপলোড", callback_data="upload_stock_file_route"))
    
    # প্রক্সি
    builder.add(InlineKeyboardButton(text="🌐 ➕ প্রক্সি ক্যাটাগরি যোগ", callback_data="add_proxy_cat"))
    builder.add(InlineKeyboardButton(text="🌐 🗑️ প্রক্সি ক্যাটাগরি ডিলিট", callback_data="delete_proxy_cat_list"))
    builder.add(InlineKeyboardButton(text="🌐 📁 প্রক্সি স্টক আপলোড", callback_data="upload_proxy_stock_route"))
    
    # অফার
    builder.add(InlineKeyboardButton(text="🎁 ➕ অফার ক্যাটাগরি যোগ", callback_data="add_offer_cat"))
    builder.add(InlineKeyboardButton(text="🎁 🗑️ অফার ক্যাটাগরি ডিলিট", callback_data="delete_offer_cat_list"))
    builder.add(InlineKeyboardButton(text="🎁 📁 অফার স্টক আপলোড", callback_data="upload_offer_stock_route"))
    
    # ভিপিএন
    builder.add(InlineKeyboardButton(text="🔑 ➕ ভিপিএন ক্যাটাগরি যোগ", callback_data="add_vpn_cat"))
    builder.add(InlineKeyboardButton(text="🔑 🗑️ ভিপিএন ক্যাটাগরি ডিলিট", callback_data="delete_vpn_cat_list"))
    builder.add(InlineKeyboardButton(text="🔑 📁 ভিপিএন স্টক আপলোড", callback_data="upload_vpn_stock_route"))
    
    # কাস্টমাইজেশন
    builder.add(InlineKeyboardButton(text="💰 প্রোডাক্টের রেট পরিবর্তন", callback_data="admin_change_price_route"))
    builder.add(InlineKeyboardButton(text="🗑️ ক্যাটাগরি স্টক ক্লিয়ার", callback_data="admin_clear_stock_route"))
    
    # ইউজারের ব্যালেন্স ও পেমেন্ট
    builder.add(InlineKeyboardButton(text="💵 ইউজারের ব্যালেন্স অ্যাড", callback_data="add_balance_admin"))
    builder.add(InlineKeyboardButton(text="➕ পেমেন্ট মেথড যোগ", callback_data="add_payment_method"))
    builder.add(InlineKeyboardButton(text="🗑️ পেমেন্ট মেথড ডিলিট", callback_data="delete_payment_method_list"))
    builder.add(InlineKeyboardButton(text="🔗 সাপোর্ট লিংক পরিবর্তন", callback_data="change_support_link_route"))
    
    # ইউজার ম্যানেজমেন্ট ও ব্রডকাস্ট
    builder.add(InlineKeyboardButton(text="👥 মেম্বার ও ব্যান সেটিংস", callback_data="admin_user_mgmt"))
    builder.add(InlineKeyboardButton(text="📢 ব্রডকাস্ট মেসেজ পাঠান", callback_data="admin_broadcast"))

    builder.adjust(3, 3, 3, 3, 2, 2, 2, 1, 1)
    return builder.as_markup()

# --- হেল্পার ফাংশনসমূহ ---
def extract_only_email(account_string):
    try:
        for separator in ["|", ":", "/"]:
            if separator in account_string:
                parts = account_string.split(separator)
                email_candidate = parts[0].strip()
                if "@" in email_candidate or "." in email_candidate:
                    return email_candidate
        parts = account_string.split()
        if parts:
            return parts[0].strip()
    except Exception:
        pass
    return account_string

def extract_only_password(account_string):
    try:
        for separator in ["|", ":", "/"]:
            if separator in account_string:
                parts = account_string.split(separator)
                if len(parts) > 1:
                    return parts[1].strip()
    except Exception:
        pass
    return "N/A"

# --- স্টার্ট হ্যান্ডলার ---
@dp.message(CommandStart())
async def start(message: types.Message):
    if is_user_banned(message.from_user.id):
        await message.answer("❌ দুঃখিত, আপনাকে এই বট থেকে ব্যান করা হয়েছে।")
        return
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
    conn.commit()
    await message.answer("✅ **আসসালামু আলাইকুম! মূল মেনু থেকে আপনার সার্ভিসটি সিলেক্ট করুন:**", reply_markup=get_main_keyboard(message.from_user.id))

# --- 📊 DASHBOARD হ্যান্ডলার ---
@dp.message(F.text == "📊 My Account")
async def dashboard_handler(message: types.Message):
    if is_user_banned(message.from_user.id): return
    user_id = message.from_user.id
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="👤 My Account", callback_data="db_my_account"))
    if user_id == ADMIN_ID:
        builder.add(InlineKeyboardButton(text="📢 Broadcast System", callback_data="admin_broadcast"))
    builder.adjust(1)
    await message.answer("📊 **ড্যাশবোর্ড (Dashboard)**\n\nনিচের বাটনে ক্লিক করে আপনার অ্যাকাউন্টের সম্পূর্ণ তথ্য বা অ্যাডমিন অ্যাকশন পরিচালনা করুন।", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "db_my_account")
async def my_account_handler(callback: types.CallbackQuery):
    if is_user_banned(callback.from_user.id): return
    user_id = callback.from_user.id
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    balance = res[0] if res else 0
    cursor.execute("SELECT COUNT(*) FROM sold_accounts WHERE user_id = ?", (user_id,))
    purchased_count = cursor.fetchone()[0]

    account_text = (
        f"👤 **মাই অ্যাকাউন্ট (My Account)**\n\n"
        f"🆔 **ইউজার আইডি:** `{user_id}`\n"
        f"💵 **বর্তমান ব্যালেন্স:** {balance} BDT\n"
        f"🛒 **মোট কেনাকাটা:** {purchased_count} পিস\n"
        f"📅 **তারিখ ও সময়:** {datetime.now().strftime('%d-%m-%Y, %I:%M %p')}\n\n"
        f"আমাদের বট ব্যবহার করার জন্য আপনাকে ধন্যবাদ!"
    )
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="⬅️ ফিরে যান", callback_data="back_to_dashboard"))
    await callback.message.edit_text(account_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "back_to_dashboard")
async def back_to_dashboard(callback: types.CallbackQuery):
    if is_user_banned(callback.from_user.id): return
    user_id = callback.from_user.id
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="👤 My Account", callback_data="db_my_account"))
    if user_id == ADMIN_ID:
        builder.add(InlineKeyboardButton(text="📢 Broadcast System", callback_data="admin_broadcast"))
    builder.adjust(1)
    await callback.message.edit_text("📊 **ড্যাশবোর্ড (Dashboard)**\n\nনিচের বাটনে ক্লিক করে আপনার অ্যাকাউন্টের সম্পূর্ণ তথ্য বা অ্যাডমিন অ্যাকশন পরিচালনা করুন।", reply_markup=builder.as_markup())
    await callback.answer()

# --- 📢 BROADCAST SYSTEM ---
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("📢 **ব্রডকাস্ট সিস্টেম चालू হয়েছে**\n\nসব বটের মেম্বারদের কাছে পাঠাতে চান এমন মেসেজটি এখানে লিখুন:\n*(অ্যাকশনটি বাতিল করতে /cancel লিখুন)*")
    await state.set_state(AdminStates.waiting_for_broadcast_msg)
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast_msg)
async def process_broadcast_message(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await message.answer("❌ ব্রডকাস্ট বাতিল করা হয়েছে।")
        await state.clear()
        return
    broadcast_text = message.text
    cursor.execute("SELECT user_id FROM users")
    all_users = cursor.fetchall()
    status_msg = await message.answer("⏳ মেসেজ পাঠানো শুরু হয়েছে, দয়া করে অপেক্ষা করুন...")
    success_count, fail_count = 0, 0
    for row in all_users:
        target_uid = row[0]
        try:
            await bot.send_message(chat_id=target_uid, text=f"📢 **নতুন নোটিফিকেশন!**\n\n{broadcast_text}")
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail_count += 1
    await status_msg.edit_text(f"📢 **ব্রডকাস্ট সম্পন্ন হয়েছে!**\n\n🟢 সফলভাবে পাঠানো হয়েছে: {success_count} জনের কাছে\n🔴 ব্যর্থ হয়েছে: {fail_count} জন")
    await state.clear()

# --- 💬 SUPPORT HANDLER ---
@dp.message(F.text == "💬 Support")
async def support_handler(message: types.Message):
    if is_user_banned(message.from_user.id): return
    cursor.execute("SELECT value FROM settings WHERE key = 'support_link'")
    res = cursor.fetchone()
    current_support_link = res[0] if res else "https://t.me/Tousif404"
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🧑‍💻 কন্টাক্ট এডমিন", url=current_support_link))
    await message.answer("💬 **আমাদের সাপোর্ট সার্ভিস**\n\nবট ব্যবহার বা যেকোনো কেনাকাটার সমস্যায় আমাদের সাথে যোগাযোগ করতে নিচের বোতামে ক্লিক করুন:", reply_markup=builder.as_markup())

# --- 🛡️ 2FA CODE GENERATOR ---
@dp.message(F.text == "🛡️ 2FA Code")
async def two_fa_start(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id): return
    await message.answer("🛡️ আপনার টু-ফ্যাক্টর অথেনটিকেশন (2FA) এর **Secret Key** বা কোডটি পেস্ট করুন:")
    await state.set_state(UserStates.waiting_for_2fa_key)

@dp.message(UserStates.waiting_for_2fa_key)
async def two_fa_generate(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id): return
    secret_key = message.text.replace(" ", "").strip()
    try:
        totp = pyotp.TOTP(secret_key)
        current_code = totp.now()
        time_remaining = totp.interval - (asyncio.get_event_loop().time() % totp.interval)
        response_text = f"🛡️ **2FA Authenticator Code**\n\n🔑 সিক্রেট কি: `{secret_key}`\n🔢 ওটিপি কোড: `{current_code}`\n⏳ মেয়াদ শেষ হবে: {int(time_remaining)} সেকেন্ড পর"
        await message.answer(response_text, parse_mode="Markdown")
    except Exception:
        await message.answer("❌ সঠিক Secret Key টি পুনরায় দিন।")
    finally:
        await state.clear()

# --- 💵 DEPOSIT SYSTEM ---
@dp.message(F.text == "💵 Deposit")
async def deposit_start(message: types.Message):
    if is_user_banned(message.from_user.id): return
    cursor.execute("SELECT id, name FROM payment_methods")
    methods = cursor.fetchall()
    if not methods:
        await message.answer("📭 বর্তমানে কোনো পেমেন্ট মেথড উপলব্ধ নেই।")
        return
    builder = InlineKeyboardBuilder()
    for method_id, method_name in methods:
        builder.add(InlineKeyboardButton(text=method_name, callback_data=f"depmethod_{method_id}"))
    builder.adjust(2)
    await message.answer("💵 **ডিপোজিট মেথড সিলেক্ট করুন:**", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("depmethod_"))
async def deposit_method_selected(callback: types.CallbackQuery, state: FSMContext):
    if is_user_banned(callback.from_user.id): return
    method_id = int(callback.data.split("_")[1])
    cursor.execute("SELECT name, details FROM payment_methods WHERE id = ?", (method_id,))
    res = cursor.fetchone()
    if not res:
        await callback.message.answer("❌ মেথডটি এখন উপলব্ধ নেই।")
        await callback.answer()
        return
    method_name, method_details = res
    await state.update_data(dep_method=method_name, dep_details=method_details)
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="⬅️ Back (বাতিল করুন)", callback_data="cancel_deposit"))
    await callback.message.delete()
    await callback.message.answer(f"💳 **পেমেন্ট মেথড:** {method_name}\n📝 **নিয়মাবলী:**\n{method_details}\n\n✍️ আপনি কত টাকা ডিপোজিট করতে চান? পরিমাণটি শুধুমাত্র সংখ্যায় লিখুন (যেমন: 500):", reply_markup=builder.as_markup())
    await state.set_state(UserStates.waiting_for_deposit_amount)
    await callback.answer()

@dp.message(UserStates.waiting_for_deposit_amount)
async def deposit_amount_received(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id): return
    try:
        amount = int(message.text)
        if amount <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ ভুল পরিমাণ! দয়া করে শুধুমাত্র সংখ্যা লিখুন।")
        return
    await state.update_data(dep_amount=amount)
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="⬅️ Back (বাতিল করুন)", callback_data="cancel_deposit"))
    await message.answer(f"💰 **পরিমাণ:** {amount} BDT\n\n✍️ এবার আপনার পেমেন্টের **Transaction ID (TrxID)** টি এখানে দিন:", reply_markup=builder.as_markup())
    await state.set_state(UserStates.waiting_for_deposit_trxid)

@dp.message(UserStates.waiting_for_deposit_trxid)
async def deposit_trxid_received(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id): return
    trxid = message.text.strip()
    await state.update_data(dep_trxid=trxid)
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="⬅️ Back (বাতিল করুন)", callback_data="cancel_deposit"))
    await message.answer(f"🔑 **TrxID:** `{trxid}`\n\n📸 পেমেন্টের কনফার্মেশন স্ক্রিনশট বা প্রুফ ফটোটি দিন:")
    await state.set_state(UserStates.waiting_for_deposit_screenshot)

@dp.message(UserStates.waiting_for_deposit_screenshot, F.photo)
async def deposit_screenshot_received(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id): return
    photo_file_id = message.photo[-1].file_id
    data = await state.get_data()
    method_name, amount, trxid = data['dep_method'], data['dep_amount'], data['dep_trxid']
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "N/A"
    
    approve_builder = InlineKeyboardBuilder()
    approve_builder.add(InlineKeyboardButton(text="✅ Approve", callback_data=f"depapprove_{user_id}_{amount}"))
    approve_builder.add(InlineKeyboardButton(text="❌ Reject", callback_data=f"depreject_{user_id}"))
    approve_builder.adjust(2)
    
    admin_msg_text = f"📥 **নতুন ডিপোজিট রিকোয়েস্ট!**\n\n👤 **ইউজার আইডি:** `{user_id}`\n👤 **ইউজারনেম:** {username}\n💳 **মেথড:** {method_name}\n💰 **টাকা:** {amount} BDT\n🔑 **TrxID:** `{trxid}`\n"
    await bot.send_photo(chat_id=ADMIN_ID, photo=photo_file_id, caption=admin_msg_text, reply_markup=approve_builder.as_markup())
    await message.answer("✅ **আপনার ডিপোজিট রিকোয়েস্টটি সাবমিট হয়েছে!** যাচাই করে ব্যালেন্স অ্যাড করে দেওয়া হবে।")
    await state.clear()

@dp.callback_query(F.data == "cancel_deposit")
async def cancel_deposit_callback(callback: types.CallbackQuery, state: FSMContext):
    if is_user_banned(callback.from_user.id): return
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("❌ ডিপোজিট বাতিল করা হয়েছে। আপনি মূল মেনুতে ফেরত এসেছেন।")
    await callback.answer()

@dp.callback_query(F.data.startswith("depapprove_"))
async def admin_approve_deposit(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    _, user_id, amount = callback.data.split("_")
    user_id, amount = int(user_id), int(amount)
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    if not res: cursor.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, amount))
    else: cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    try: await bot.send_message(chat_id=user_id, text=f"🎉 অভিনন্দন! আপনার পাঠানো **{amount} BDT** ডিপোজিট রিকোয়েস্টটি অ্যাপ্রুভ করা হয়েছে।")
    except Exception: pass
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n🟢 **APPROVED**")
    await callback.answer()

@dp.callback_query(F.data.startswith("depreject_"))
async def admin_reject_deposit(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    _, user_id = callback.data.split("_")
    user_id = int(user_id)
    try: await bot.send_message(chat_id=user_id, text="❌ দুঃখিত, আপনার ডিপোজিট রিকোয়েস্টটি রিজেক্ট করা হয়েছে।")
    except Exception: pass
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n🔴 **REJECTED**")
    await callback.answer()

# --- 🛒 BUY NOW SYSTEM ---
@dp.message(F.text == "🛒 Buy Now")
async def buy_now_grid(message: types.Message):
    if is_user_banned(message.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✉️ Hotmail", callback_data="buygrid_Hotmail"))
    builder.add(InlineKeyboardButton(text="✉️ Outlook", callback_data="buygrid_Outlook"))
    builder.add(InlineKeyboardButton(text="✉️ FR_Outlook", callback_data="buygrid_FR_Outlook"))
    builder.add(InlineKeyboardButton(text="🔑 VPN", callback_data="buygrid_VPN"))
    builder.add(InlineKeyboardButton(text="⚫ Proxy~IP", callback_data="buygrid_Proxy"))
    builder.add(InlineKeyboardButton(text="✉️ Edu Mail", callback_data="buygrid_Edu"))
    builder.add(InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_action"))
    builder.adjust(2, 2, 2, 1)
    await message.answer("🎯 **প্যাকেজ সিলেক্ট করুন** ✅", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("buygrid_"))
async def process_grid_selection(callback: types.CallbackQuery, state: FSMContext):
    if is_user_banned(callback.from_user.id): return
    selection = callback.data.split("_")[1]
    if selection == "Proxy":
        cursor.execute("SELECT name, price FROM proxy_categories")
        all_proxies = cursor.fetchall()
        builder = InlineKeyboardBuilder()
        for name, price in all_proxies:
            cursor.execute("SELECT COUNT(*) FROM stocks WHERE product_name = ?", (name,))
            stock_count = cursor.fetchone()[0]
            builder.add(InlineKeyboardButton(text=f"⚫ {name} [{stock_count} pc] - {price} BDT", callback_data=f"userbuy_{name}"))
        builder.add(InlineKeyboardButton(text="🎁 Offers", callback_data="show_offers"))
        builder.add(InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_main_grid"))
        builder.adjust(1)
        await callback.message.edit_text("•  **Proxy~IP 💥**\n\n•  **সাব-প্যাকেজ সিলেক্ট করুন** 🛡️", reply_markup=builder.as_markup())
        await callback.answer()
        return

    if selection == "VPN":
        cursor.execute("SELECT name, price FROM vpn_categories")
        all_vpns = cursor.fetchall()
        builder = InlineKeyboardBuilder()
        for name, price in all_vpns:
            cursor.execute("SELECT COUNT(*) FROM stocks WHERE product_name = ?", (name,))
            stock_count = cursor.fetchone()[0]
            builder.add(InlineKeyboardButton(text=f"🔑 {name} [{stock_count} pc] - {price} BDT", callback_data=f"userbuy_{name}"))
        builder.add(InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_main_grid"))
        builder.adjust(1)
        await callback.message.edit_text("•  **VPN Service 🔐**\n\n•  **পছন্দসই ভিপিএন ব্র্যান্ড সিলেক্ট করুন** 🛡️", reply_markup=builder.as_markup())
        await callback.answer()
        return

    cursor.execute("SELECT name, price FROM products WHERE name LIKE ?", (f"%{selection}%",))
    product = cursor.fetchone()
    if not product:
        await callback.message.answer(f"❌ দুঃখিত, বর্তমানে '{selection}' এর কোনো প্যাকেজ তৈরি করা নেই।")
        await callback.answer()
        return
    product_name, price = product
    cursor.execute("SELECT COUNT(*) FROM stocks WHERE product_name = ?", (product_name,))
    stock_count = cursor.fetchone()[0]
    if stock_count == 0:
        await callback.message.answer(f"❌ দুঃখিত, {product_name} বর্তমানে স্টকে নেই!")
        await callback.answer()
        return
    await state.update_data(buy_prod=product_name, buy_price=price, max_stock=stock_count)
    await callback.message.answer(f"📦 **প্রোডাক্ট:** {product_name}\n💵 **মূল্য:** {price} BDT\n📊 **স্টক:** {stock_count} পিস\n\n✍️ কত পিস কিনতে চান? সংখ্যাটি লিখুন:")
    await state.set_state(UserStates.waiting_for_quantity)
    await callback.answer()

@dp.callback_query(F.data == "show_offers")
async def show_offers(callback: types.CallbackQuery):
    if is_user_banned(callback.from_user.id): return
    cursor.execute("SELECT name, price FROM offer_categories")
    all_offers = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    has_offers = False
    for name, price in all_offers:
        has_offers = True
        cursor.execute("SELECT COUNT(*) FROM stocks WHERE product_name = ?", (name,))
        stock_count = cursor.fetchone()[0]
        builder.add(InlineKeyboardButton(text=f"🔥 {name} [{stock_count} pc] - {price} BDT", callback_data=f"userbuy_{name}"))
    builder.add(InlineKeyboardButton(text="⬅️ Back", callback_data="buygrid_Proxy"))
    builder.adjust(1)
    if not has_offers: await callback.message.edit_text("🎁 **Special Proxy Offers** 🎁\n\n📭 বর্তমানে কোনো স্পেশাল অফার প্রক্সি বরাদ্দ নেই।", reply_markup=builder.as_markup())
    else: await callback.message.edit_text("🎁 **Special Proxy Offers** 🎁\n\n🔥 আজকের সেরা ধামাকা অফার প্রক্সি প্যাকেজসমূহ:", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "cancel_action")
async def cancel_action(callback: types.CallbackQuery, state: FSMContext):
    if is_user_banned(callback.from_user.id): return
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("❌ অ্যাকশন বাতিল করা হয়েছে।")
    await callback.answer()

@dp.callback_query(F.data == "back_to_main_grid")
async def back_to_main_grid(callback: types.CallbackQuery):
    if is_user_banned(callback.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✉️ Hotmail", callback_data="buygrid_Hotmail"))
    builder.add(InlineKeyboardButton(text="✉️ Outlook", callback_data="buygrid_Outlook"))
    builder.add(InlineKeyboardButton(text="✉️ FR_Outlook", callback_data="buygrid_FR_Outlook"))
    builder.add(InlineKeyboardButton(text="🔑 VPN", callback_data="buygrid_VPN"))
    builder.add(InlineKeyboardButton(text="⚫ Proxy~IP", callback_data="buygrid_Proxy"))
    builder.add(InlineKeyboardButton(text="✉️ Edu Mail", callback_data="buygrid_Edu"))
    builder.add(InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_action"))
    builder.adjust(2, 2, 2, 1)
    await callback.message.edit_text("🎯 **প্যাকেজ সিলেক্ট করুন** ✅", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("userbuy_"))
async def user_buy_click(callback: types.CallbackQuery, state: FSMContext):
    if is_user_banned(callback.from_user.id): return
    product_name = callback.data.split("_")[1]
    price = 0
    cursor.execute("SELECT price FROM proxy_categories WHERE name = ?", (product_name,))
    res = cursor.fetchone()
    if res: price = res[0]
    else:
        cursor.execute("SELECT price FROM offer_categories WHERE name = ?", (product_name,))
        res_off = cursor.fetchone()
        if res_off: price = res_off[0]
        else:
            cursor.execute("SELECT price FROM vpn_categories WHERE name = ?", (product_name,))
            res_vpn = cursor.fetchone()
            if res_vpn: price = res_vpn[0]
            else:
                cursor.execute("SELECT price FROM products WHERE name = ?", (product_name,))
                res_prod = cursor.fetchone()
                price = res_prod[0] if res_prod else 0
    
    cursor.execute("SELECT COUNT(*) FROM stocks WHERE product_name = ?", (product_name,))
    stock_count = cursor.fetchone()[0]
    if stock_count == 0:
        await callback.message.answer("❌ দুঃখিত, এই প্রোডাক্টটি বর্তমানে স্টকে নেই!")
        await callback.answer()
        return
    await state.update_data(buy_prod=product_name, buy_price=price, max_stock=stock_count)
    await callback.message.answer(f"📦 **প্রোডাক্ট:** {product_name}\n💵 **প্রতি পিসের মূল্য:** {price} BDT\n📊 **স্টক আছে:** {stock_count} পিস\n\n✍️ আপনি কত পিস কিনতে চান? সংখ্যাটি লিখুন:")
    await state.set_state(UserStates.waiting_for_quantity)
    await callback.answer()

@dp.message(UserStates.waiting_for_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id): return
    try:
        qty = int(message.text)
        if qty <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ দয়া করে একটি সঠিক সংখ্যা লিখুন (যেমন: 1, 10, 50)।")
        return
    data = await state.get_data()
    prod_name, price_per_pc, max_stock = data['buy_prod'], data['buy_price'], data['max_stock']
    if qty > max_stock:
        await message.answer(f"❌ স্টকে এত পরিমাণ নেই! সর্বোচ্চ {max_stock} পিস আছে।")
        return
    total_cost = qty * price_per_pc
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (message.from_user.id,))
    user_balance = cursor.fetchone()[0]
    if user_balance < total_cost:
        await message.answer(f"❌ আপনার পর্যাপ্ত পরিমাণ ব্যালেন্স নেই!\n💰 আপনার ব্যালেন্স: {user_balance} BDT\n🛒 মোট খরচ: {total_cost} BDT")
        await state.clear()
        return
    await state.update_data(buy_qty=qty, total_cost=total_cost, user_balance=user_balance)
    
    if qty == 1:
        cursor.execute("SELECT id, account_data FROM stocks WHERE product_name = ? LIMIT 1", (prod_name,))
        item = cursor.fetchone()
        if not item:
            await message.answer("❌ স্টক পরিবর্তিত হয়েছে, আবার চেষ্টা করুন।")
            await state.clear()
            return
        item_id, full_data = item
        only_email, only_password = extract_only_email(full_data), extract_only_password(full_data)
        cursor.execute("INSERT INTO sold_accounts (user_id, email, password, full_data) VALUES (?, ?, ?, ?)", (message.from_user.id, only_email, only_password, full_data))
        cursor.execute("DELETE FROM stocks WHERE id = ?", (item_id,))
        new_balance = user_balance - total_cost
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, message.from_user.id))
        conn.commit()
        
        single_delivery_text = (
            f"✅ **অর্ডার সম্পন্ন** ✨\n\n🛄 **Delivery Time:** {datetime.now().strftime('%d-%m-%Y, %I:%M %p')}\n\n"
            f"🎰 **Package:** {prod_name}\n\n   🔹 **আপনার প্রোডাক্ট:** ✨\n\n🌐 **মেইল/আইপি:** `{only_email}` ✨\n"
            f"🔑 **পাসওয়ার্ড/পোর্ট (যদি থাকে):** `{only_password}` ✨\n\n"
            f"🖍️ **ডিটেইলস** 🔹\n`{full_data}`"
        )
        await message.answer(single_delivery_text, parse_mode="Markdown")
        await state.clear()
        return

    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📊 Excel (.xlsx)", callback_data="format_excel"))
    builder.add(InlineKeyboardButton(text="📄 CSV File (.csv)", callback_data="format_csv"))
    builder.adjust(2)
    await message.answer("📂 **ফাইল ফরম্যাট সিলেক্ট করুন:**", reply_markup=builder.as_markup())
    await state.set_state(UserStates.waiting_for_format_selection)

@dp.callback_query(UserStates.waiting_for_format_selection)
async def deliver_file(callback: types.CallbackQuery, state: FSMContext):
    if is_user_banned(callback.from_user.id): return
    file_format = "excel" if callback.data == "format_excel" else "csv"
    data = await state.get_data()
    prod_name, qty, total_cost = data['buy_prod'], data['buy_qty'], data['total_cost']
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (callback.from_user.id,))
    current_balance = cursor.fetchone()[0]
    if current_balance < total_cost:
        await callback.message.answer("❌ পর্যাপ্ত ব্যালেন্স নেই।")
        await state.clear()
        await callback.answer()
        return
    cursor.execute("SELECT id, account_data FROM stocks WHERE product_name = ? LIMIT ?", (prod_name, qty))
    items = cursor.fetchall()
    if len(items) < qty:
        await callback.message.answer("❌ স্টক পরিবর্তিত হয়েছে, আবার চেষ্টা করুন।")
        await state.clear()
        await callback.answer()
        return

    final_sheet_data = []
    for item in items:
        item_id, full_data = item
        only_email, only_password = extract_only_email(full_data), extract_only_password(full_data)
        final_sheet_data.append((only_email, full_data))
        cursor.execute("INSERT INTO sold_accounts (user_id, email, password, full_data) VALUES (?, ?, ?, ?)", (callback.from_user.id, only_email, only_password, full_data))
        cursor.execute("DELETE FROM stocks WHERE id = ?", (item_id,))
    
    new_balance = current_balance - total_cost
    cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, callback.from_user.id))
    conn.commit()

    file_bytes = io.BytesIO()
    if file_format == "excel":
        wb = Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(["Mail/IP/VPN", "All Access Data"])
        for only_email, full_data in final_sheet_data: ws.append([only_email, full_data])
        wb.save(file_bytes)
        file_bytes.seek(0)
        filename = f"{prod_name.replace(' ', '_')}_{qty}pcs.xlsx"
    else:
        text_stream = io.StringIO()
        csv_writer = csv.writer(text_stream)
        csv_writer.writerow(["Mail/IP/VPN", "All Access Data"])
        for only_email, full_data in final_sheet_data: csv_writer.writerow([only_email, full_data])
        file_bytes.write(text_stream.getvalue().encode('utf-8-sig'))
        file_bytes.seek(0)
        filename = f"{prod_name.replace(' ', '_')}_{qty}pcs.csv"

    document_file = BufferedInputFile(file_bytes.read(), filename=filename)
    caption_text = f"✅ **কেনাকাটা সফল হয়েছে!**\n🛒 **প্যাকেজ:** {prod_name}\n📊 **পরিমাণ:** {qty} পিস\n💰 **টোটাল খরচ:** {total_cost} BDT\n💵 **বর্তমান ব্যালেন্স:** {new_balance} BDT\n\n📂 ফাইল পাঠানো হলো। ধন্যবাদ।"
    await callback.message.answer_document(document=document_file, caption=caption_text, parse_mode="Markdown")
    await state.clear()
    await callback.answer()

# --- 🔑 MAIL CODE SYSTEM ---
@dp.message(F.text == "🔑 Mail Code")
async def mail_code_start(message: types.Message):
    if is_user_banned(message.from_user.id): return
    web_app_url = "https://dongvanfb.net/read_mail_box/"
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🌐 Open Mailbox System", web_app=WebAppInfo(url=web_app_url)))
    await message.answer("✨ নিচের বোতামে ক্লিক করে মেইলবক্স ব্যবহার করুন:", reply_markup=builder.as_markup())

# ==================== 🛠️ ADMIN PANEL FUNCTIONS ====================

@dp.message(F.text == "🛠️ Admin Panel")
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("⚙️ **অ্যাডমিন কন্ট্রোল প্যানেল**\n\nআপনার কাঙ্ক্ষিত ক্যাটাগরির সেটিংস কনফিগার করতে নিচে ক্লিক করুন:", reply_markup=get_admin_inline())

# 👥 মেম্বার ও ব্যান সেটিংস
@dp.callback_query(F.data == "admin_user_mgmt")
async def admin_user_management(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    cursor.execute("SELECT COUNT(*) FROM users")
    total_members = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned_members = cursor.fetchone()[0]
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🚫 ইউজার ব্যান করুন", callback_data="ban_user_start"))
    builder.add(InlineKeyboardButton(text="🟢 ইউজার আনব্যান করুন", callback_data="unban_user_start"))
    builder.adjust(1)
    
    await callback.message.answer(
        f"👥 **মেম্বার এবং ইউজার ম্যানেজমেন্ট**\n\n"
        f"📊 **মোট রেজিস্টার্ড মেম্বার:** {total_members} জন\n"
        f"🚫 **মোট ব্যান মেম্বার:** {banned_members} জন\n\n"
        f"যেকোনো মেম্বারকে ব্যান বা আনব্যান করতে নিচের বাটন ব্যবহার করুন।",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "ban_user_start")
async def ban_user_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ আপনি যে মেম্বারকে ব্যান করতে চান তার **টেলিগ্রাম User ID** দিন:")
    await state.set_state(AdminStates.waiting_for_ban_user_id)
    await callback.answer()

@dp.message(AdminStates.waiting_for_ban_user_id)
async def process_ban_user(message: types.Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ভুল আইডি ফরম্যাট! অনুগ্রহ করে আইডি হিসেবে শুধু সংখ্যা পাঠান।")
        return
        
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (target_id,))
    user = cursor.fetchone()
    if not user:
        cursor.execute("INSERT INTO users (user_id, balance, is_banned) VALUES (?, 0, 1)", (target_id,))
    else:
        cursor.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,))
    conn.commit()
    
    await message.answer(f"✅ ইউজার `{target_id}` সফলভাবে বট থেকে **ব্যান করা হয়েছে**।")
    try:
        await bot.send_message(chat_id=target_id, text="❌ আপনাকে এই বট থেকে ব্যান করা হয়েছে।")
    except Exception:
        pass
    await state.clear()

@dp.callback_query(F.data == "unban_user_start")
async def unban_user_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ আপনি যে মেম্বারকে আনব্যান করতে চান তার **টেলিগ্রাম User ID** দিন:")
    await state.set_state(AdminStates.waiting_for_unban_user_id)
    await callback.answer()

@dp.message(AdminStates.waiting_for_unban_user_id)
async def process_unban_user(message: types.Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ভুল আইডি ফরম্যাট! অনুগ্রহ করে আইডি হিসেবে শুধু সংখ্যা পাঠান।")
        return
        
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (target_id,))
    user = cursor.fetchone()
    if not user:
        await message.answer("❌ এই আইডি-র কোনো মেম্বার ডাটাবেজে পাওয়া যায়নি!")
        await state.clear()
        return
        
    cursor.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,))
    conn.commit()
    
    await message.answer(f"✅ ইউজার `{target_id}` সফলভাবে **আনব্যান করা হয়েছে**।")
    try:
        await bot.send_message(chat_id=target_id, text="🎉 অভিনন্দন! আপনাকে বট ব্যবহারের অনুমতি পুনরায় দেওয়া হয়েছে।")
    except Exception:
        pass
    await state.clear()

# 💰 রেট পরিবর্তনের অপশন
@dp.callback_query(F.data == "admin_change_price_route")
async def admin_change_price_route(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    builder = InlineKeyboardBuilder()
    
    cursor.execute("SELECT name, price FROM products")
    for row in cursor.fetchall():
        builder.add(InlineKeyboardButton(text=f"✉️ {row[0]} ({row[1]} BDT)", callback_data=f"chrate_prod_{row[0]}"))
        
    cursor.execute("SELECT name, price FROM proxy_categories")
    for row in cursor.fetchall():
        builder.add(InlineKeyboardButton(text=f"🌐 {row[0]} ({row[1]} BDT)", callback_data=f"chrate_proxy_{row[0]}"))
        
    cursor.execute("SELECT name, price FROM offer_categories")
    for row in cursor.fetchall():
        builder.add(InlineKeyboardButton(text=f"🎁 {row[0]} ({row[1]} BDT)", callback_data=f"chrate_offer_{row[0]}"))
        
    cursor.execute("SELECT name, price FROM vpn_categories")
    for row in cursor.fetchall():
        builder.add(InlineKeyboardButton(text=f"🔑 {row[0]} ({row[1]} BDT)", callback_data=f"chrate_vpn_{row[0]}"))
        
    builder.adjust(1)
    await callback.message.answer("💰 **কোন ক্যাটাগরির রেট পরিবর্তন করবেন? সিলেক্ট করুন:**", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("chrate_"))
async def admin_change_price_selected(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    cat_type = parts[1]
    target_name = "_".join(parts[2:])
    
    await state.update_data(ch_type=cat_type, ch_name=target_name)
    await callback.message.answer(f"✍️ **{target_name}** এর জন্য নতুন মূল্য কত দিতে চান? শুধুমাত্র সংখ্যাটি লিখুন:")
    await state.set_state(AdminStates.waiting_for_new_price)
    await callback.answer()

@dp.message(AdminStates.waiting_for_new_price)
async def admin_change_price_save(message: types.Message, state: FSMContext):
    try:
        new_price = int(message.text)
        if new_price < 0: raise ValueError
    except ValueError:
        await message.answer("❌ ভুল ইনপুট! মূল্য অবশ্যই একটি ধনাত্মক সংখ্যা হতে হবে। পুনরায় দিন:")
        return
        
    data = await state.get_data()
    cat_type, target_name = data['ch_type'], data['ch_name']
    
    if cat_type == "prod":
        cursor.execute("UPDATE products SET price = ? WHERE name = ?", (new_price, target_name))
    elif cat_type == "proxy":
        cursor.execute("UPDATE proxy_categories SET price = ? WHERE name = ?", (new_price, target_name))
    elif cat_type == "offer":
        cursor.execute("UPDATE offer_categories SET price = ? WHERE name = ?", (new_price, target_name))
    elif cat_type == "vpn":
        cursor.execute("UPDATE vpn_categories SET price = ? WHERE name = ?", (new_price, target_name))
        
    conn.commit()
    await message.answer(f"✅ সফলভাবে **{target_name}** এর নতুন রেট **{new_price} BDT** সেট করা হয়েছে!")
    await state.clear()

# 🗑️ স্টক ক্লিয়ার
@dp.callback_query(F.data == "admin_clear_stock_route")
async def admin_clear_stock_route(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    builder = InlineKeyboardBuilder()
    cursor.execute("SELECT DISTINCT product_name FROM stocks")
    active_stocks = cursor.fetchall()
    
    if not active_stocks:
        await callback.message.answer("📭 বর্তমানে কোনো স্টক নেই।")
        await callback.answer()
        return
        
    for row in active_stocks:
        cursor.execute("SELECT COUNT(*) FROM stocks WHERE product_name = ?", (row[0],))
        cnt = cursor.fetchone()[0]
        builder.add(InlineKeyboardButton(text=f"🗑️ Clear {row[0]} ({cnt} pc)", callback_data=f"clrstk_{row[0]}"))
        
    builder.adjust(1)
    await callback.message.answer("⚠️ **কোন প্যাকেজের স্টক সম্পূর্ণ ডিলিট করবেন? সিলেক্ট করুন:**", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("clrstk_"))
async def admin_clear_stock_confirm(callback: types.CallbackQuery):
    target_name = callback.data.split("_")[1]
    cursor.execute("DELETE FROM stocks WHERE product_name = ?", (target_name,))
    conn.commit()
    await callback.message.answer(f"✅ সফলভাবে **{target_name}** এর সমস্ত স্টক সম্পূর্ণ ক্লিয়ার করা হয়েছে।")
    await callback.answer()

# 🔑 ভিপিএন ক্যাটাগরি তৈরি
@dp.callback_query(F.data == "add_vpn_cat")
async def add_vpn_category_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ নতুন ভিপিএন ব্র্যান্ডের নাম দিন:")
    await state.set_state(AdminStates.waiting_for_vpn_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_vpn_name)
async def add_vpn_category_name(message: types.Message, state: FSMContext):
    await state.update_data(vpn_name=message.text.strip())
    await message.answer("✍️ প্রতি পিসের রেট কত হবে? (শুধু সংখ্যা দিন):")
    await state.set_state(AdminStates.waiting_for_vpn_price)

@dp.message(AdminStates.waiting_for_vpn_price)
async def add_vpn_category_price(message: types.Message, state: FSMContext):
    try: price = int(message.text)
    except ValueError: return
    data = await state.get_data()
    name = data['vpn_name']
    cursor.execute("INSERT OR REPLACE INTO vpn_categories (name, price) VALUES (?, ?)", (name, price))
    conn.commit()
    await message.answer(f"✅ ভিপিএন ক্যাটাগরি **{name}** সফলভাবে তৈরি হয়েছে!")
    await state.clear()

@dp.callback_query(F.data == "delete_vpn_cat_list")
async def delete_vpn_cat_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT name FROM vpn_categories")
    vpns = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for vpn in vpns: builder.add(InlineKeyboardButton(text=f"🗑️ {vpn[0]}", callback_data=f"delvpncat_{vpn[0]}"))
    builder.adjust(1)
    await callback.message.answer("🗑️ কোন ভিপিএন ক্যাটাগরি ডিলিট করতে চান?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("delvpncat_"))
async def delete_vpn_cat_confirm(callback: types.CallbackQuery):
    cat_name = callback.data.split("_")[1]
    cursor.execute("DELETE FROM vpn_categories WHERE name = ?", (cat_name,))
    cursor.execute("DELETE FROM stocks WHERE product_name = ?", (cat_name,))
    conn.commit()
    await callback.message.answer(f"🗑️ ভিপিএন ক্যাটাগরি '{cat_name}' ডিলিট করা হয়েছে।")
    await callback.answer()

@dp.callback_query(F.data == "upload_vpn_stock_route")
async def upload_vpn_stock_route(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT name FROM vpn_categories")
    vpns = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for vpn in vpns: builder.add(InlineKeyboardButton(text=vpn[0], callback_data=f"vpnstk_{vpn[0]}"))
    builder.adjust(1)
    await callback.message.answer("📁 কোন ভিপিএন ক্যাটাগরির জন্য স্টক ফাইল আপলোড করবেন?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("vpnstk_"))
async def add_vpn_stock_select(callback: types.CallbackQuery, state: FSMContext):
    cat_name = callback.data.split("_")[1]
    await state.update_data(stock_prod=cat_name)
    await callback.message.answer(f"📤 **{cat_name}** এর জন্য স্টক ফাইল (.xlsx বা .csv) পাঠান।")
    await state.set_state(AdminStates.waiting_for_vpn_stock_file)
    await callback.answer()

@dp.message(AdminStates.waiting_for_vpn_stock_file, F.document)
async def process_vpn_stock_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    prod_name = data['stock_prod']
    document = message.document
    file_id = document.file_id
    file_name = document.file_name.lower()
    
    file_in_memory = io.BytesIO()
    await bot.download(file=file_id, destination=file_in_memory)
    file_in_memory.seek(0)
    
    inserted_count = 0
    try:
        if file_name.endswith('.xlsx'): df = pd.read_excel(file_in_memory, header=None)
        else: df = pd.read_csv(file_in_memory, header=None)
        for row_data in df[0].dropna().astype(str).tolist():
            cursor.execute("INSERT INTO stocks (product_name, account_data) VALUES (?, ?)", (prod_name, row_data.strip()))
            inserted_count += 1
        conn.commit()
        await message.answer(f"✅ সফলভাবে ভিপিএন ফাইলে **{inserted_count} পিস** স্টক যুক্ত হয়েছে!")
        # অটোমেটিক সবাইকে নোটিফাই করার জন্য
        await notify_all_members(prod_name, inserted_count)
    except Exception as e:
        await message.answer(f"❌ এরর: {str(e)}")
    await state.clear()

# --- অফার ক্যাটাগরি কন্ট্রোল ---
@dp.callback_query(F.data == "add_offer_cat")
async def add_offer_category_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ নতুন অফার প্রক্সি প্যাকেজের নাম দিন:")
    await state.set_state(AdminStates.waiting_for_offer_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_offer_name)
async def add_offer_category_name(message: types.Message, state: FSMContext):
    await state.update_data(offer_name=message.text.strip())
    await message.answer("✍️ ডিসকাউন্ট মূল্য/রেট কত হবে?:")
    await state.set_state(AdminStates.waiting_for_offer_price)

@dp.message(AdminStates.waiting_for_offer_price)
async def add_offer_category_price(message: types.Message, state: FSMContext):
    try: price = int(message.text)
    except ValueError: return
    data = await state.get_data()
    name = data['offer_name']
    cursor.execute("INSERT OR REPLACE INTO offer_categories (name, price) VALUES (?, ?)", (name, price))
    conn.commit()
    await message.answer(f"✅ অফার ক্যাটাগরি **{name}** তৈরি হয়েছে।")
    await state.clear()

@dp.callback_query(F.data == "delete_offer_cat_list")
async def delete_offer_cat_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT name FROM offer_categories")
    offers = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for off in offers: builder.add(InlineKeyboardButton(text=f"🗑️ {off[0]}", callback_data=f"deloffcat_{off[0]}"))
    builder.adjust(1)
    await callback.message.answer("🗑️ কোন অফার ক্যাটাগরি ডিলিট করবেন?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("deloffcat_"))
async def delete_offer_cat_confirm(callback: types.CallbackQuery):
    cat_name = callback.data.split("_")[1]
    cursor.execute("DELETE FROM offer_categories WHERE name = ?", (cat_name,))
    cursor.execute("DELETE FROM stocks WHERE product_name = ?", (cat_name,))
    conn.commit()
    await callback.message.answer(f"🗑️ অফার ক্যাটাগরি '{cat_name}' ডিলিট করা হয়েছে।")
    await callback.answer()

@dp.callback_query(F.data == "upload_offer_stock_route")
async def upload_offer_stock_route(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT name FROM offer_categories")
    offers = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for off in offers: builder.add(InlineKeyboardButton(text=off[0], callback_data=f"offstk_{off[0]}"))
    builder.adjust(1)
    await callback.message.answer("📁 কোন অফার ক্যাটাগরির জন্য স্টক ফাইল আপলোড করবেন?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("offstk_"))
async def add_offer_stock_select(callback: types.CallbackQuery, state: FSMContext):
    cat_name = callback.data.split("_")[1]
    await state.update_data(stock_prod=cat_name)
    await callback.message.answer(f"📤 **{cat_name}** এর জন্য স্টক ফাইল পাঠান।")
    await state.set_state(AdminStates.waiting_for_offer_stock_file)
    await callback.answer()

@dp.message(AdminStates.waiting_for_offer_stock_file, F.document)
async def process_offer_stock_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    prod_name = data['stock_prod']
    document = message.document
    file_id = document.file_id
    file_name = document.file_name.lower()
    
    file_in_memory = io.BytesIO()
    await bot.download(file=file_id, destination=file_in_memory)
    file_in_memory.seek(0)
    
    inserted_count = 0
    try:
        if file_name.endswith('.xlsx'): df = pd.read_excel(file_in_memory, header=None)
        else: df = pd.read_csv(file_in_memory, header=None)
        for row_data in df[0].dropna().astype(str).tolist():
            cursor.execute("INSERT INTO stocks (product_name, account_data) VALUES (?, ?)", (prod_name, row_data.strip()))
            inserted_count += 1
        conn.commit()
        await message.answer(f"✅ সফলভাবে **{inserted_count} পিস** স্টক যুক্ত হয়েছে!")
        # অটোমেটিক সবাইকে নোটিফাই করার জন্য
        await notify_all_members(prod_name, inserted_count)
    except Exception as e:
        await message.answer(f"❌ এরর: {str(e)}")
    await state.clear()

# --- রেগুলার প্রক্সি কন্ট্রোল ---
@dp.callback_query(F.data == "add_proxy_cat")
async def add_proxy_category_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ প্রক্সি ক্যাটাগরির নাম দিন:")
    await state.set_state(AdminStates.waiting_for_proxy_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_proxy_name)
async def add_proxy_category_name(message: types.Message, state: FSMContext):
    await state.update_data(proxy_name=message.text.strip())
    await message.answer("✍️ প্রতি পিসের রেট কত হবে?:")
    await state.set_state(AdminStates.waiting_for_proxy_price)

@dp.message(AdminStates.waiting_for_proxy_price)
async def add_proxy_category_price(message: types.Message, state: FSMContext):
    try: price = int(message.text)
    except ValueError: return
    data = await state.get_data()
    name = data['proxy_name']
    cursor.execute("INSERT OR REPLACE INTO proxy_categories (name, price) VALUES (?, ?)", (name, price))
    conn.commit()
    await message.answer(f"✅ প্রক্সি ক্যাটাগরি **{name}** তৈরি হয়েছে।")
    await state.clear()

@dp.callback_query(F.data == "delete_proxy_cat_list")
async def delete_proxy_cat_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT name FROM proxy_categories")
    proxies = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for proxy in proxies: builder.add(InlineKeyboardButton(text=f"🗑️ {proxy[0]}", callback_data=f"delproxycat_{proxy[0]}"))
    builder.adjust(1)
    await callback.message.answer("🗑️ কোন প্রক্সি ক্যাটাগরি ডিলিট করতে চান?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("delproxycat_"))
async def delete_proxy_cat_confirm(callback: types.CallbackQuery):
    cat_name = callback.data.split("_")[1]
    cursor.execute("DELETE FROM proxy_categories WHERE name = ?", (cat_name,))
    cursor.execute("DELETE FROM stocks WHERE product_name = ?", (cat_name,))
    conn.commit()
    await callback.message.answer(f"🗑️ প্রক্সি ক্যাটাগরি '{cat_name}' ডিলিট করা হয়েছে।")
    await callback.answer()

@dp.callback_query(F.data == "upload_proxy_stock_route")
async def upload_proxy_stock_route(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT name FROM proxy_categories")
    proxies = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for proxy in proxies: builder.add(InlineKeyboardButton(text=proxy[0], callback_data=f"proxystk_{proxy[0]}"))
    builder.adjust(1)
    await callback.message.answer("📁 কোন প্রক্সি ক্যাটাগরির জন্য স্টক ফাইল আপলোড করবেন?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("proxystk_"))
async def add_proxy_stock_select(callback: types.CallbackQuery, state: FSMContext):
    cat_name = callback.data.split("_")[1]
    await state.update_data(stock_prod=cat_name)
    await callback.message.answer(f"📤 **{cat_name}** এর জন্য ফাইল পাঠান।")
    await state.set_state(AdminStates.waiting_for_proxy_stock_file)
    await callback.answer()

@dp.message(AdminStates.waiting_for_proxy_stock_file, F.document)
async def process_proxy_stock_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    prod_name = data['stock_prod']
    document = message.document
    file_id = document.file_id
    file_name = document.file_name.lower()
    
    file_in_memory = io.BytesIO()
    await bot.download(file=file_id, destination=file_in_memory)
    file_in_memory.seek(0)
    
    inserted_count = 0
    try:
        if file_name.endswith('.xlsx'): df = pd.read_excel(file_in_memory, header=None)
        else: df = pd.read_csv(file_in_memory, header=None)
        for row_data in df[0].dropna().astype(str).tolist():
            cursor.execute("INSERT INTO stocks (product_name, account_data) VALUES (?, ?)", (prod_name, row_data.strip()))
            inserted_count += 1
        conn.commit()
        await message.answer(f"✅ সফলভাবে **{inserted_count} পিস** স্টক যুক্ত হয়েছে!")
        # অটোমেটিক সবাইকে নোটিফাই করার জন্য
        await notify_all_members(prod_name, inserted_count)
    except Exception as e:
        await message.answer(f"❌ এরর: {str(e)}")
    await state.clear()

# --- রেগুলার মেইল প্রোডাক্ট কন্ট্রোল ---
@dp.callback_query(F.data == "add_product")
async def add_product(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ নতুন মেইল প্রোডাক্টের নাম দিন:")
    await state.set_state(AdminStates.waiting_for_product_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_product_name)
async def add_prod_name(message: types.Message, state: FSMContext):
    await state.update_data(new_name=message.text)
    await message.answer("✍️ মূল্য দিন (শুধু সংখ্যা):")
    await state.set_state(AdminStates.waiting_for_product_price)

@dp.message(AdminStates.waiting_for_product_price)
async def add_prod_price(message: types.Message, state: FSMContext):
    try: price = int(message.text)
    except ValueError: return
    data = await state.get_data()
    name = data['new_name']
    cursor.execute("INSERT OR REPLACE INTO products (name, price) VALUES (?, ?)", (name, price))
    conn.commit()
    await message.answer(f"✅ প্রোডাক্ট **{name}** তৈরি হয়েছে।")
    await state.clear()

@dp.callback_query(F.data == "delete_product_list")
async def delete_product_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT name FROM products")
    products = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for prod in products: builder.add(InlineKeyboardButton(text=f"❌ {prod[0]}", callback_data=f"delprod_{prod[0]}"))
    builder.adjust(1)
    await callback.message.answer("🗑️ কোন প্রোডাক্টটি ডিলিট করতে চান?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("delprod_"))
async def delete_product_confirm(callback: types.CallbackQuery):
    product_name = callback.data.split("_")[1]
    cursor.execute("DELETE FROM products WHERE name = ?", (product_name,))
    conn.commit()
    await callback.message.answer(f"🗑️ প্রোডাক্ট '{product_name}' ডিলিট হয়েছে।")
    await callback.answer()

@dp.callback_query(F.data == "upload_stock_file_route")
async def upload_stock_file_route(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT name FROM products")
    products = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for prod in products: builder.add(InlineKeyboardButton(text=prod[0], callback_data=f"filestock_{prod[0]}"))
    builder.adjust(1)
    await callback.message.answer("📁 কোন প্রোডাক্টের জন্য স্টক ফাইল আপলোড করবেন?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("filestock_"))
async def add_stock_file_select_old(callback: types.CallbackQuery, state: FSMContext):
    product_name = callback.data.split("_")[1]
    await state.update_data(stock_prod=product_name)
    await callback.message.answer(f"📤 **{product_name}** এর জন্য স্টক ফাইল পাঠান।")
    await state.set_state(AdminStates.waiting_for_stock_file)
    await callback.answer()

@dp.message(AdminStates.waiting_for_stock_file, F.document)
async def process_stock_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    prod_name = data['stock_prod']
    document = message.document
    file_id = document.file_id
    file_name = document.file_name.lower()
    
    file_in_memory = io.BytesIO()
    await bot.download(file=file_id, destination=file_in_memory)
    file_in_memory.seek(0)
    
    inserted_count = 0
    try:
        if file_name.endswith('.xlsx'): df = pd.read_excel(file_in_memory, header=None)
        else: df = pd.read_csv(file_in_memory, header=None)
        for row_data in df[0].dropna().astype(str).tolist():
            cursor.execute("INSERT INTO stocks (product_name, account_data) VALUES (?, ?)", (prod_name, row_data.strip()))
            inserted_count += 1
        conn.commit()
        await message.answer(f"✅ **{inserted_count} পিস** স্টক যুক্ত হয়েছে!")
        # অটোমেটিক সবাইকে নোটিফাই করার জন্য
        await notify_all_members(prod_name, inserted_count)
    except Exception as e:
        await message.answer(f"❌ এরর: {str(e)}")
    await state.clear()

@dp.callback_query(F.data == "add_payment_method")
async def add_payment_method(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ নতুন পেমেন্ট মেথডটির নাম দিন:")
    await state.set_state(AdminStates.waiting_for_method_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_method_name)
async def add_method_name(message: types.Message, state: FSMContext):
    await state.update_data(method_name=message.text.strip())
    await message.answer("✍️ এবার নিয়মাবলী ও নম্বর দিন:")
    await state.set_state(AdminStates.waiting_for_method_details)

@dp.message(AdminStates.waiting_for_method_details)
async def add_method_details(message: types.Message, state: FSMContext):
    data = await state.get_data()
    method_name, method_details = data['method_name'], message.text.strip()
    cursor.execute("INSERT OR REPLACE INTO payment_methods (name, details) VALUES (?, ?)", (method_name, method_details))
    conn.commit()
    await message.answer(f"✅ নতুন ডিপোজিট মেথড **{method_name}** তৈরি হয়েছে!")
    await state.clear()

@dp.callback_query(F.data == "delete_payment_method_list")
async def delete_payment_method_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cursor.execute("SELECT id, name FROM payment_methods")
    methods = cursor.fetchall()
    builder = InlineKeyboardBuilder()
    for method_id, method_name in methods: builder.add(InlineKeyboardButton(text=f"🗑️ {method_name}", callback_data=f"delmethod_{method_id}"))
    builder.adjust(1)
    await callback.message.answer("🗑️ কোন পেমেন্ট মেথডটি ডিলিট করতে চান?", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("delmethod_"))
async def delete_payment_method_confirm(callback: types.CallbackQuery):
    method_id = int(callback.data.split("_")[1])
    cursor.execute("DELETE FROM payment_methods WHERE id = ?", (method_id,))
    conn.commit()
    await callback.message.answer("🗑️ পেমেন্ট মেথডটি ডিলিট করা হয়েছে।")
    await callback.answer()

@dp.callback_query(F.data == "change_support_link_route")
async def change_support_link_route(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ নতুন সাপোর্ট লিংকটি দিন:")
    await state.set_state(AdminStates.waiting_for_support_link)
    await callback.answer()

@dp.message(AdminStates.waiting_for_support_link)
async def process_new_support_link(message: types.Message, state: FSMContext):
    new_link = message.text.strip()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('support_link', ?)", (new_link,))
    conn.commit()
    await message.answer(f"✅ সফলভাবে সাপোর্ট লিংক পরিবর্তন করা হয়েছে!")
    await state.clear()

@dp.callback_query(F.data == "add_balance_admin")
async def add_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("✍️ ইউজারের টেলিগ্রাম User ID দিন:")
    await state.set_state(AdminStates.waiting_for_user_id)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_id)
async def add_balance_uid(message: types.Message, state: FSMContext):
    try: uid = int(message.text)
    except ValueError: return
    await state.update_data(target_uid=uid)
    await message.answer("✍️ কত ব্যালেন্স (BDT) যোগ করতে চান লিখুন:")
    await state.set_state(AdminStates.waiting_for_amount)

@dp.message(AdminStates.waiting_for_amount)
async def add_balance_finish(message: types.Message, state: FSMContext):
    try: amount = int(message.text)
    except ValueError: return
    data = await state.get_data()
    uid = data['target_uid']
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
    res = cursor.fetchone()
    if not res: cursor.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (uid, amount))
    else: cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, uid))
    conn.commit()
    await message.answer(f"✅ সফলভাবে ব্যালেন্স যোগ করা হয়েছে।")
    await state.clear()

async def main():
    # পুরোনো পেন্ডিং আপডেট দূর করে ফ্রেশ পোলিং শুরু করা
    await bot.delete_webhook(drop_pending_updates=True)
    try: 
        await dp.start_polling(bot)
    finally: 
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
