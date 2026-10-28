# language: Python 3.11+, file: dossier_bot.py
# pip install aiogram aiosqlite aiohttp beautifulsoup4 phonenumbers pyrogram tgcrypto dnspython
import asyncio
import aiosqlite
import aiohttp
import time
import io
import csv
import json
import re
import socket
import phonenumbers
import logging
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, BufferedInputFile, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pyrogram import Client
from pyrogram.errors import FloodWait

# --- КОНФИГ ---
BOT_TOKEN = "9044270268:AAFZPoaFhbDL2J0HP5wpb-1G64MYQ-hWVTM"
SUPER_ADMIN_ID = 8454036760
DB_PATH = "dossier.db"

# ВСТАВЬ СЮДА КЛЮЧИ
API_ID = None  
API_HASH = ""  

# Настройка логирования в файл (для Render)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename='bot.log', filemode='a')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
cooldowns = {}
bomber_tasks = {}
admin_states = {}
parsing_states = {}
ip_states = {}

app = Client("osint_session", api_id=API_ID, api_hash=API_HASH) if API_ID and API_HASH else None

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS targets (
                id TEXT PRIMARY KEY, username TEXT, fio TEXT, bio TEXT,
                last_seen TEXT, avatar_url TEXT, geo TEXT, psycho TEXT,
                interests TEXT, connections TEXT, clones TEXT, risks TEXT,
                forced_info TEXT, phone TEXT, collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, target TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, 0)", (SUPER_ADMIN_ID,))
        await db.commit()

async def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID: return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)) as cursor:
            return bool(await cursor.fetchone())

# --- УТИЛИТЫ ---
async def check_phone(phone: str) -> str:
    try:
        p = phonenumbers.parse(phone, None)
        if phonenumbers.is_valid_number(p):
            carrier = phonenumbers.carrier.name_for_number(p, "ru")
            region = phonenumbers.region_code_for_number(p)
            return f"✅ Валидный\nРегион: {region}\nОператор: {carrier or 'Неизвестно'}"
        return "❌ Невалидный номер"
    except: return "⚠️ Ошибка парсинга номера"

async def get_ip_info(ip_or_domain: str) -> str:
    try:
        # Если домен, резолвим в IP
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip_or_domain):
            ip = socket.gethostbyname(ip_or_domain)
        else:
            ip = ip_or_domain

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org,as,query", timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "success":
                        return (f"🌐 **IP:** {data.get('query')}\n"
                                f"🏳️ **Страна:** {data.get('country')}\n"
                                f"🏙️ **Город:** {data.get('city')}\n"
                                f"📡 **Провайдер:** {data.get('isp')}\n"
                                f"🏢 **Организация:** {data.get('org')}")
                return "❌ Не удалось получить данные."
    except Exception as e:
        return f"❌ Ошибка: {e}"

# --- СБОР ДАННЫХ ---
async def resolve_username_web(username: str) -> dict:
    username = username.lstrip("@")
    url = f"https://t.me/{username}"
    data = {"bio": "Нет", "title": "Неизвестно", "avatar_url": "Не найдено"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5) as resp:
                if resp.status == 200:
                    soup = BeautifulSoup(await resp.text(), "html.parser")
                    if meta_img := soup.find("meta", property="og:image"): data["avatar_url"] = meta_img["content"]
                    if meta_desc := soup.find("meta", property="og:description"): data["bio"] = meta_desc["content"]
                    if meta_title := soup.find("meta", property="og:title"): data["title"] = meta_title["content"]
    except: pass
    return data

async def gather_dossier(target: str) -> dict:
    dossier = {
        "id": target if target.isdigit() else "Неизвестно", "username": f"@{target.lstrip('@')}" if not target.isdigit() else "Неизвестно",
        "fio": "Неизвестно", "bio": "Нет данных", "last_seen": "Неизвестно", "avatar_url": "Не найдено",
        "geo": "Не определено", "psycho": "Анализ недоступен", "interests": "Нет данных", 
        "connections": "Неизвестно", "clones": "Не проверялись", "risks": "Неизвестно", "forced_info": "Нет", "phone": "Нет данных", "cache": False
    }
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM targets WHERE id = ? OR username = ?", (target, target.lstrip("@"))) as cursor:
            row = await cursor.fetchone()
            if row:
                keys = ["id","username","fio","bio","last_seen","avatar_url","geo","psycho","interests","connections","clones","risks","forced_info","phone"]
                dossier.update(dict(zip(keys, row[:14]))); dossier["cache"] = True; return dossier

    if app and app.is_connected:
        try:
            user = await app.get_users(int(target) if target.isdigit() else target)
            dossier.update({"id": user.id, "username": f"@{user.username}" if user.username else "Скрыт",
                            "fio": f"{user.first_name or ''} {user.last_name or ''}".strip(),
                            "bio": user.bio or "Нет", "last_seen": str(user.status) if user.status else "Неизвестно"})
        except FloodWait as e: await asyncio.sleep(e.value)
        except Exception as e: logger.error(f"API Error: {e}")

    if not target.isdigit():
        parsed = await resolve_username_web(target)
        if dossier["avatar_url"] == "Не найдено": dossier["avatar_url"] = parsed["avatar_url"]
        if dossier["bio"] == "Нет данных": dossier["bio"] = parsed["bio"]
        if dossier["fio"] == "Неизвестно": dossier["fio"] = parsed["title"]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO targets (id, username, fio, bio, last_seen, avatar_url, geo, psycho, interests, connections, clones, risks, forced_info, phone)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (dossier["id"], dossier["username"], dossier["fio"], dossier["bio"], dossier["last_seen"], dossier["avatar_url"], dossier["geo"], dossier["psycho"], dossier["interests"], dossier["connections"], dossier["clones"], dossier["risks"], dossier["forced_info"], dossier["phone"]))
        await db.commit()
    return dossier

def format_dossier(d: dict) -> str:
    return (f"👤 **ДОСЬЕ:** {d['username']}\n🆔 **ID:** {d['id']}\n\n"
            f"📝 **ФИО/Ник:** {d['fio']}\n📍 **Гео:** {d['geo']}\n🧠 **Психо:** {d['psycho']}\n"
            f"📝 **Био:** {d['bio']}\n🕒 **Онлайн:** {d['last_seen']}\n\n"
            f"📊 **ИНТЕРЕСЫ:**\n{d['interests']}\n\n🔗 **Связи:** {d['connections']}\n"
            f"👥 **Клоны:** {d['clones']}\n🚨 **Риски:** {d['risks']}\n"
            f"📞 **Телефон:** {d['phone']}\n📌 **ПРИНУД. ИНФА:**\n{d['forced_info']}\n"
            + ("\n💾 *Кэш*" if d.get("cache") else "\n🌐 *Свежий сбор*"))

# --- МАССПАРСИНГ ---
async def run_mass_parser(chat_id: str, admin_id: int, limit: int = 50):
    if not app: return await bot.send_message(admin_id, "❌ Нужен API.")
    await bot.send_message(admin_id, f"🔄 Парсинг `{chat_id}`...")
    count = 0
    try:
        async for member in app.get_chat_members(chat_id, limit=limit):
            if member.user.is_bot or member.user.is_deleted: continue
            await gather_dossier(str(member.user.id))
            count += 1
            if count % 10 == 0: await asyncio.sleep(2)
        await bot.send_message(admin_id, f"✅ Парсинг завершен. Собрано: {count}.")
    except FloodWait as e:
        await bot.send_message(admin_id, f"⚠️ FloodWait: {e.value} сек...")
        await asyncio.sleep(e.value)
        await run_mass_parser(chat_id, admin_id, limit - count)
    except Exception as e:
        await bot.send_message(admin_id, f"❌ Ошибка парсинга: {e}")

# --- БОМБЕР ---
SMS_GATEWAYS = [{"name": "Test", "url": "https://httpbin.org/post", "data": {"phone": "{}"}}]

async def send_sms_request(session, gateway, phone):
    try:
        async with session.post(gateway["url"], data={k: v.format(phone) for k, v in gateway["data"].items()}, timeout=5) as resp:
            return resp.status == 200
    except: return False

async def run_bomber(phone: str, duration: int, chat_id: int):
    end_time = time.time() + duration
    count = 0
    async with aiohttp.ClientSession() as session:
        while time.time() < end_time and phone in bomber_tasks:
            count += sum(await asyncio.gather(*[send_sms_request(session, gw, phone) for gw in SMS_GATEWAYS]))
            await asyncio.sleep(1)
    if phone in bomber_tasks:
        del bomber_tasks[phone]
        await bot.send_message(chat_id, f"✅ Бомбер `{phone}` завершён. Запросов: {count}")

# --- КЛАВИАТУРЫ ---
def kb_main(is_super=False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🖼 Аватар", callback_data="act_avatar")
    b.button(text="📁 CSV", callback_data="act_export")
    b.button(text="💾 JSON", callback_data="act_export_json")
    b.button(text="🌐 WHOIS", callback_data="act_whois")
    b.button(text="📡 IP Инфо", callback_data="act_ip")
    b.button(text="💣 Бомбер", callback_data="bomber_menu")
    b.button(text="🕵️ Парсинг чата", callback_data="parse_menu")
    b.button(text="📧 Почта", callback_data="email_menu")
    if is_super:
        b.button(text="👑 Админ-панель", callback_data="admin_panel")
        b.button(text="📊 Статистика", callback_data="stats")
    b.adjust(2)
    return b.as_markup()

def kb_admin() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Выдать админа", callback_data="admin_add")
    b.button(text="➖ Снять админа", callback_data="admin_remove")
    b.button(text="📜 Список админов", callback_data="admin_list")
    b.button(text="📦 Бэкап БД", callback_data="admin_backup")
    b.button(text="🗑 Очистить базу", callback_data="admin_wipe")
    b.button(text="🔙 Назад", callback_data="act_menu")
    b.adjust(1)
    return b.as_markup()

def kb_whois() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад", callback_data="act_menu")
    return b.as_markup()

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not await is_admin(message.from_user.id): return await message.answer("Доступ закрыт.")
    await message.answer("Бот готов (v9.0). Отправь @username, ID, номер (+7...), домен или IP.", reply_markup=kb_main(message.from_user.id == SUPER_ADMIN_ID))

@dp.callback_query(F.data == "act_ip")
async def cb_ip(callback: types.CallbackQuery):
    ip_states[callback.from_user.id] = "waiting_ip"
    await callback.message.edit_text("📡 Отправь IP-адрес или домен для анализа:", reply_markup=kb_whois())
    await callback.answer()

@dp.message(F.text.regexp(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$|^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"))
async def handle_ip_or_domain(message: types.Message):
    uid = message.from_user.id
    if not await is_admin(uid): return
    
    if uid in ip_states and ip_states[uid] == "waiting_ip":
        del ip_states[uid]
        msg = await message.answer("🔄 Запрос данных...")
        res = await get_ip_info(message.text.strip())
        await msg.edit_text(res, parse_mode="Markdown")
        return

@dp.message(F.text.regexp(r"^\+?[\d\s\-\(\)]{10,}$"))
async def handle_phone(message: types.Message):
    if not await is_admin(message.from_user.id): return
    res = await check_phone(message.text.strip())
    await message.answer(f"📞 **Проверка номера:**\n{res}", parse_mode="Markdown")

@dp.message(F.text.regexp(r"^\+?\d{10,15}\s+\d+$"))
async def handle_bomber_start(message: types.Message):
    if not await is_admin(message.from_user.id): return
    parts = message.text.strip().split()
    phone, duration = parts[0], int(parts[1])
    if phone in bomber_tasks: return await message.answer("Уже бомбится.")
    task = asyncio.create_task(run_bomber(phone, duration, message.chat.id))
    bomber_tasks[phone] = task
    await message.answer(f"🚀 Запущен бомбер на `{phone}` ({duration} сек).", parse_mode="Markdown")

async def main():
    await init_db()
    if app:
        await app.start()
        logger.info("Pyrogram started.")
    else:
        logger.warning("Pyrogram disabled (no API).")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
