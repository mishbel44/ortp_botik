import logging
import asyncio
import time
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import aiohttp
import mysql.connector
from datetime import datetime, timedelta
import os
import random
import re
import smtplib
from email.message import EmailMessage
from aiohttp import web
import hmac
import hashlib
from dotenv import load_dotenv

load_dotenv()

# === Конфигурация ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
JIRA_URL = os.getenv("JIRA_URL")
BEARER_TOKEN = os.getenv("BEARER_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")
PHOTOS_DIR = "photos"
ADMIN_ID = int(os.getenv("ADMIN_ID"))
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SERVER_HOST = "0.0.0.0"
WEBHOOK_SERVER_PORT = 8080
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# MySQL конфигурация
MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))  # 3306 как значение по умолчанию
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE")

# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Создаем директорию для фото
os.makedirs(PHOTOS_DIR, exist_ok=True)

priority_translation_map = {
    "Low": "Низкий",
    "Medium": "Средний",
    "High": "Высокий"
}

# База данных
def execute_query(query, params=(), fetch=False):
    try:
        conn = mysql.connector.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            port=MYSQL_PORT
        )
        cursor = conn.cursor()
        cursor.execute(query, params)
        if fetch:
            result = cursor.fetchall()
        else:
            result = None
        conn.commit()
        cursor.close()
        conn.close()
        return result
    except mysql.connector.Error as e:
        logging.error(f"Ошибка MySQL: {e}")
        raise

# Создаем таблицы
execute_query('''
    CREATE TABLE IF NOT EXISTS requests (
        user_id BIGINT,
        issue_key VARCHAR(50) PRIMARY KEY,
        title TEXT,
        status VARCHAR(50),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')

execute_query('''
    CREATE TABLE IF NOT EXISTS team (
        id INTEGER PRIMARY KEY AUTO_INCREMENT,
        position VARCHAR(100) NOT NULL,
        last_name VARCHAR(100) NOT NULL,
        first_name VARCHAR(100) NOT NULL,
        middle_name VARCHAR(100) NOT NULL,
        photo_path TEXT,
        description TEXT NOT NULL
    )
''')

execute_query('''
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        email VARCHAR(255) UNIQUE,
        is_verified BOOLEAN DEFAULT FALSE
    )
''')

execute_query('''
    CREATE TABLE IF NOT EXISTS verification_codes (
        user_id BIGINT PRIMARY KEY,
        code VARCHAR(10),
        expires_at DATETIME,
        last_request_at DATETIME
    )
''')

execute_query('''
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT,
        issue_key VARCHAR(50),
        event_type VARCHAR(50),
        message_text TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_read BOOLEAN DEFAULT FALSE
    )
''')

# === Состояния FSM ===
class BotStates(StatesGroup):
    create_title = State()
    create_description = State()
    create_priority = State()
    add_comment = State()
    view_team_member = State()
    add_team_member = State()
    verify_email = State()
    verify_code = State()

# === Логирование ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# === Класс для работы с Jira ===
class JiraClient:
    def __init__(self, url, token, project_key):
        self.url = url
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.project_key = project_key

    async def get_priorities(self):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.url}/rest/api/2/priority", headers=self.headers) as response:
                response.raise_for_status()
                priorities = await response.json()
                return {p["name"]: p["id"] for p in priorities if p["name"].lower() in ["high", "medium", "low"]}

    async def create_issue(self, summary, description, priority):
        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "description": description,
                "issuetype": {"name": "Task"},
                "priority": {"name": priority},
            }
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.url}/rest/api/2/issue", headers=self.headers, json=payload) as response:
                response.raise_for_status()
                return (await response.json())["key"]

    async def get_issue_status(self, issue_key):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.url}/rest/api/2/issue/{issue_key}", headers=self.headers) as response:
                if response.status == 404:
                    raise Exception("Заявка не найдена")
                response.raise_for_status()
                data = await response.json()
                return {
                    "status": data["fields"]["status"]["name"],
                    "summary": data["fields"]["summary"],
                    "priority": data["fields"]["priority"]["name"]
                }

    async def get_issue_comments(self, issue_key):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.url}/rest/api/2/issue/{issue_key}/comment",
                headers=self.headers
            ) as response:
                if response.status == 404:
                    raise Exception("Заявка не найдена")
                response.raise_for_status()
                data = await response.json()
                comments = data.get("comments", [])
                return [{"body": comment["body"], "author": comment["author"]["displayName"]} for comment in comments] if comments else []

    async def add_comment_to_issue(self, issue_key, comment):
        try:
            issue_details = await self.get_issue_details(issue_key)
            if issue_details is None:
                raise Exception(f"Задача {issue_key} не найдена")
            status = issue_details.get("status", "")
            if status.lower() in ["готово", "done"]:
                raise Exception(f"Задача {issue_key} в статусе 'Готово'. Комментарий не может быть добавлен.")
            payload = {"body": comment}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.url}/rest/api/2/issue/{issue_key}/comment",
                    headers=self.headers,
                    json=payload
                ) as response:
                    if response.status == 403:
                        raise Exception("Нет прав на добавление комментария к задаче")
                    response.raise_for_status()
                    return await response.json()
        except Exception as e:
            logging.error(f"Ошибка при добавлении комментария к задаче {issue_key}: {e}")
            raise e

    async def get_issue_details(self, issue_key):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.url}/rest/api/2/issue/{issue_key}", headers=self.headers) as response:
                    if response.status == 404:
                        logging.error(f"Задача {issue_key} не найдена в Jira")
                        return None
                    response.raise_for_status()
                    data = await response.json()
                    fields = data.get("fields", {})
                    return {
                        "summary": fields.get("summary", "Нет заголовка"),
                        "description": fields.get("description", "Нет описания"),
                        "assignee": (fields.get("assignee") or {}).get("displayName", "Не назначен"),
                        "status": fields.get("status", {}).get("name", "Неизвестно"),
                        "priority": fields.get("priority", {}).get("name", "Неизвестно"),
                        "created": fields.get("created", "Нет данных"),
                        "updated": fields.get("updated", "Нет данных"),
                    }
        except Exception as e:
            logging.error(f"Ошибка при получении данных задачи {issue_key}: {e}")
            return None

    async def register_webhook(self, webhook_url):
        payload = {
            "name": "Telegram Bot Status Change Webhook",
            "url": webhook_url,
            "enabled": True,
            "events": ["jira:issue_updated", "comment_created"],
            "filters": {
                "issue-related-events-section": f"project = {self.project_key}"
            },
            "excludeBody": False,
            "secret": WEBHOOK_SECRET
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.url}/rest/webhooks/1.0/webhook", headers=self.headers, json=payload) as response:
                response_text = await response.text()
                logging.info(f"Webhook registration response: {response.status} - {response_text}")
                if response.status != 200:
                    logging.error(f"Failed to register webhook: {response_text}")
                    return None
                return await response.json()

jira_client = JiraClient(JIRA_URL, BEARER_TOKEN, JIRA_PROJECT_KEY)

# === Генерация клавиатур ===
main_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Создать заявку", callback_data="create_request")],
    [InlineKeyboardButton(text="Мои заявки", callback_data="my_requests")],
    [InlineKeyboardButton(text="Уведомления", callback_data="notifications")]
])

cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️", callback_data="cancel")]
])

back_to_title_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️", callback_data="back_to_title")]
])

back_to_description_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️", callback_data="back_to_description")]
])

back_to_requests_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️ Назад к списку", callback_data="back_to_requests")]
])

back_to_main_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️", callback_data="back")]
])

hide_notification_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Скрыть", callback_data="hide_notification")]
])

# === Обработка отмены ===
async def handle_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("💆‍♂️  Главное меню  💆‍♀️", reply_markup=main_keyboard)
    await callback.answer()

# === Обработка кнопки "Назад" ===
async def handle_back(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state == BotStates.create_description.state:
        await callback.message.edit_text("👩‍🏫 Введите тему заявки:", reply_markup=cancel_keyboard)
        await state.set_state(BotStates.create_title)
    elif current_state == BotStates.create_priority.state:
        data = await state.get_data()
        await callback.message.edit_text(
            "👩‍🎨 Введите описание заявки. \n\nЕсли нужно прикрепить медиафайл - преобразуйте его в ссылку: сервисы для <a href='https://ru.imgbb.com/'>фото</a> и <a href='https://wdfiles.ru/'>видео</a>",
            disable_web_page_preview=True,
            parse_mode="HTML",
            reply_markup=back_to_title_keyboard
        )
        await state.set_state(BotStates.create_description)
    else:
        await state.clear()
        await callback.message.edit_text("💆‍♂️  Главное меню  💆‍♀️", reply_markup=main_keyboard)
    await callback.answer()

# === Обработка кнопки "Назад к заявкам" ===
async def handle_back_to_requests(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_my_requests(callback, 1)
    await callback.answer()

# === Обработка кнопки "Назад к заголовку" ===
async def handle_back_to_title(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("👩‍🏫 Введите тему заявки:", reply_markup=cancel_keyboard)
    await state.set_state(BotStates.create_title)
    await callback.answer()

# === Обработка кнопки "Назад к описанию" ===
async def handle_back_to_description(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "👩‍🎨 Введите описание заявки. \n\nЕсли нужно прикрепить медиафайл - преобразуйте его в ссылку: сервисы для <a href='https://ru.imgbb.com/'>фото</a> и <a href='https://wdfiles.ru/'>видео</a>",
        disable_web_page_preview=True,
        parse_mode="HTML",
        reply_markup=back_to_title_keyboard
    )
    await state.set_state(BotStates.create_description)
    await callback.answer()

# === Анимация загрузки ===
async def send_progress_animation(message: types.Message):
    progress_message = await message.answer("⏳ Обработка...")
    return progress_message

# Функция для отправки кода на почту
def send_verification_code(email: str, code: str) -> bool:
    try:
        msg = EmailMessage()
        msg["Subject"] = "Код подтверждения для бота"
        msg["From"] = SMTP_USER
        msg["To"] = email
        msg.set_content(f"Ваш код подтверждения: {code}\n\nКод действителен 10 минут.")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        logging.error(f"Ошибка отправки письма: {e}")
        return False

# Функция проверки почты
def is_valid_email(email: str) -> bool:
    return re.match(r"^[a-zA-Z0-9_.+-]+@pari\.ru$", email) is not None

# Генерация кода подтверждения
def generate_verification_code() -> str:
    return str(random.randint(100000, 999999))

# Вспомогательная функция для удаления сообщения через задержку
async def delete_after_delay(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщения {message_id}: {e}")

# Определение, нужно ли отправлять уведомление
def should_notify(*args, **kwargs) -> bool:
    return True

# === Хендлеры ===
@dp.message(F.text == "/start")
async def start_command(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    result = execute_query('SELECT is_verified FROM users WHERE user_id = %s', (user_id,), fetch=True)
    if result and result[0][0]:
        await message.answer(
            f"🙋‍♂️ Привет, {message.from_user.first_name}! 🙋‍♀️\n\n"
            "Выбери действие из меню ниже:",
            reply_markup=main_keyboard
        )
    else:
        await state.set_state(BotStates.verify_email)
        sent_message = await message.answer("👨‍💼 Введите свою корпоративную почту 👩‍💼\n\n👉 @pari.ru")
        await state.update_data(bot_message_id=sent_message.message_id)

@dp.message(BotStates.verify_email)
async def process_email(message: types.Message, state: FSMContext):
    email = message.text.strip()
    data = await state.get_data()
    bot_message_id = data.get('bot_message_id')
    
    await message.delete()

    if not bot_message_id:
        sent_message = await message.answer("❌ Ошибка. Попробуйте снова.")
        await state.update_data(bot_message_id=sent_message.message_id)
        return

    progress_message = await bot.send_message(
        chat_id=message.chat.id,
        text="⏳ Обработка..."
    )

    if not is_valid_email(email):
        await bot.delete_message(
            chat_id=message.chat.id,
            message_id=progress_message.message_id
        )
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="🙆‍♂️ Почта не подходит 🙆‍♀️\n\nВведите корпоративную почту \n👉 @pari.ru"
        )
        return

    user_id = message.from_user.id
    result = execute_query('SELECT user_id FROM users WHERE email = %s', (email,), fetch=True)
    if result and result[0][0] != user_id:
        await bot.delete_message(
            chat_id=message.chat.id,
            message_id=progress_message.message_id
        )
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="🙆‍♂️ Эта почта уже используется другим пользователем🙆‍♀️\n\nУкажите другую 👇🏾"
        )
        return

    execute_query(
        'INSERT INTO users (user_id, email, is_verified) VALUES (%s, %s, FALSE) ON DUPLICATE KEY UPDATE email = %s, is_verified = FALSE',
        (user_id, email, email)
    )
    code = generate_verification_code()
    expires_at = datetime.now() + timedelta(minutes=10)
    last_request_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        'INSERT INTO verification_codes (user_id, code, expires_at, last_request_at) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE code = %s, expires_at = %s, last_request_at = %s',
        (user_id, code, expires_at.strftime("%Y-%m-%d %H:%M:%S"), last_request_at, code, expires_at.strftime("%Y-%m-%d %H:%M:%S"), last_request_at)
    )
    if send_verification_code(email, code):
        await bot.delete_message(
            chat_id=message.chat.id,
            message_id=progress_message.message_id
        )
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="📩 Код отправлен на почту. Введите его:",
            reply_markup=cancel_keyboard
        )
        await state.set_state(BotStates.verify_code)
    else:
        await bot.delete_message(
            chat_id=message.chat.id,
            message_id=progress_message.message_id
        )
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="❌ Ошибка отправки кода. Попробуйте снова:",
            reply_markup=cancel_keyboard
        )

async def delayed_edit_message(chat_id: int, message_id: int, sleep_time: float, is_expired: bool):
    await asyncio.sleep(sleep_time)
    
    # Проверяем, зарегистрирован ли пользователь
    result = execute_query('SELECT is_verified FROM users WHERE user_id = %s', (chat_id,), fetch=True)
    if result and result[0][0]:
        return  # Пользователь зарегистрирован, не меняем сообщение
    
    new_text = "❌ Код устарел или неверен. Запросите новый." if is_expired else "🙆‍♂️ Неверный код 🙆‍♀️\n\n Попробуйте ввести его снова, либо запросите новый."
    new_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить код заново", callback_data="resend_code")]
    ])
    try:
        await bot.edit_message_text(
            text=new_text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=new_keyboard
        )
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения после таймаута: {e}")

@dp.message(BotStates.verify_code)
async def process_code(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    input_code = message.text.strip()
    data = await state.get_data()
    bot_message_id = data.get('bot_message_id')

    await message.delete()

    if not bot_message_id:
        sent_message = await message.answer("❌ Ошибка. Попробуйте снова.", reply_markup=cancel_keyboard)
        await state.update_data(bot_message_id=sent_message.message_id)
        return

    result = execute_query(
        'SELECT code, last_request_at FROM verification_codes WHERE user_id = %s AND expires_at > NOW()',
        (user_id,),
        fetch=True
    )
    is_expired = False
    if not result:
        is_expired = True
        msg_base = "❌ Код устарел или неверен."
    else:
        stored_code, _ = result[0]
        if input_code == stored_code:
            execute_query('UPDATE users SET is_verified = TRUE WHERE user_id = %s', (user_id,))
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=bot_message_id,
                text="✅ Регистрация пройдена успешно!\n\n"
                     f"🙋‍♂️ Привет, {message.from_user.first_name}! 🙋‍♀️\n"
                     "Выбери действие из меню ниже:",
                reply_markup=main_keyboard
            )
            await state.clear()
            return
        msg_base = "🙆‍♂️ Неверный код 🙆‍♀️\n\n"

    keyboard, in_cooldown, remaining = await get_resend_keyboard_and_status(user_id)
    if in_cooldown:
        full_text = f"{msg_base} {'Попробуйте ввести код снова.' if not is_expired else ''}\n Либо запросите его повторно, кнопка появится в течение минуты."
    else:
        full_text = f"{msg_base} {'Попробуйте снова или запросите новый.' if not is_expired else 'Запросите новый.'}"

    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=full_text,
        reply_markup=keyboard
    )

    if in_cooldown:
        asyncio.create_task(delayed_edit_message(message.chat.id, bot_message_id, remaining, is_expired))

async def get_resend_keyboard_and_status(user_id: int) -> tuple[InlineKeyboardMarkup, bool, float]:
    result = execute_query('SELECT last_request_at FROM verification_codes WHERE user_id = %s', (user_id,), fetch=True)
    if result and result[0][0]:
        last_request_at = datetime.strptime(str(result[0][0]), "%Y-%m-%d %H:%M:%S")
        time_since_last_request = (datetime.now() - last_request_at).total_seconds()
        if time_since_last_request < 60:
            return InlineKeyboardMarkup(inline_keyboard=[]), True, 60 - time_since_last_request
        else:
            return InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Отправить код заново", callback_data="resend_code")]
            ]), False, 0
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить код заново", callback_data="resend_code")]
    ]), False, 0

@dp.callback_query(F.data == "resend_code")
async def resend_verification_code(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    bot_message_id = data.get('bot_message_id')

    if not bot_message_id:
        sent_message = await callback.message.answer("❌ Ошибка. Попробуйте снова.", reply_markup=cancel_keyboard)
        await state.update_data(bot_message_id=sent_message.message_id)
        await callback.answer()
        return

    result = execute_query('SELECT email, last_request_at FROM users u JOIN verification_codes v ON u.user_id = v.user_id WHERE u.user_id = %s', (user_id,), fetch=True)
    if not result:
        await callback.message.edit_text("❌ Пользователь не найден.", reply_markup=cancel_keyboard)
        await callback.answer()
        return
    email, last_request_at_str = result[0]
    if last_request_at_str:
        last_request_at = datetime.strptime(str(last_request_at_str), "%Y-%m-%d %H:%M:%S")
        time_since_last_request = (datetime.now() - last_request_at).total_seconds()
        if time_since_last_request < 60:
            await callback.message.edit_text(
                "❌ Вы сможете запросить код повторно через минуту.",
                reply_markup=cancel_keyboard
            )
            await callback.answer()
            return
    code = generate_verification_code()
    expires_at = datetime.now() + timedelta(minutes=10)
    last_request_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        'INSERT INTO verification_codes (user_id, code, expires_at, last_request_at) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE code = %s, expires_at = %s, last_request_at = %s',
        (user_id, code, expires_at.strftime("%Y-%m-%d %H:%M:%S"), last_request_at, code, expires_at.strftime("%Y-%m-%d %H:%M:%S"), last_request_at)
    )
    if send_verification_code(email, code):
        await callback.message.edit_text(
            "📩 Код отправлен заново. Введите его:",
            reply_markup=cancel_keyboard
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка отправки кода. Попробуйте снова:",
            reply_markup=cancel_keyboard
        )
    await callback.answer()

@dp.callback_query(F.data == "create_request")
async def create_request_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    result = execute_query('SELECT is_verified FROM users WHERE user_id = %s', (user_id,), fetch=True)
    if not result or not result[0][0]:
        await callback.message.edit_text("Вы не зарегистрированы. Используйте /start.")
        await callback.answer()
        return
    await callback.message.edit_text("👩‍🏫 Введите тему заявки:", reply_markup=cancel_keyboard)
    await state.update_data(bot_message_id=callback.message.message_id)
    await state.set_state(BotStates.create_title)
    await callback.answer()

@dp.message(BotStates.create_title, F.text)
async def process_title(message: types.Message, state: FSMContext):
    await message.delete()
    data = await state.get_data()
    bot_message_id = data['bot_message_id']
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text="👩‍🎨 Введите описание заявки. \n\nЕсли нужно прикрепить медиафайл - преобразуйте его в ссылку: сервисы для <a href='https://ru.imgbb.com/'>фото</a> и <a href='https://wdfiles.ru/'>видео</a>",
        disable_web_page_preview=True,
        parse_mode="HTML",
        reply_markup=back_to_title_keyboard
    )
    await state.update_data(title=message.text)
    await state.set_state(BotStates.create_description)

@dp.message(BotStates.create_title)
async def process_invalid_title(message: types.Message, state: FSMContext):
    await message.delete()
    data = await state.get_data()
    bot_message_id = data.get('bot_message_id')
    await bot.send_message(
        chat_id=message.chat.id,
        text="Введите запрашиваемую информацию текстом",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[])
    ).then(lambda msg: asyncio.create_task(delete_after_delay(message.chat.id, msg.message_id, 3)))
    # Keep the original message and state intact
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text="👩‍🏫 Введите тему заявки:",
        reply_markup=cancel_keyboard
    )

@dp.message(BotStates.create_description, F.text)
async def process_description(message: types.Message, state: FSMContext):
    await message.delete()
    data = await state.get_data()
    bot_message_id = data['bot_message_id']
    await state.update_data(description=message.text)
    await state.set_state(BotStates.create_priority)
    priorities = await jira_client.get_priorities()
    buttons = [
        InlineKeyboardButton(text=russian_name, callback_data=f"priority_{id}_{int(time.time())}")
        for english_name, id in priorities.items()
        for russian_name, mapped_english in priority_translation_map.items()
        if mapped_english == english_name
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        buttons,
        [InlineKeyboardButton(text="↩️", callback_data="back_to_description")]
    ])
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text="🧖🏾‍♀ Выберите приоритет:",
        reply_markup=keyboard
    )

@dp.message(BotStates.create_description)
async def process_invalid_description(message: types.Message, state: FSMContext):
    await message.delete()
    data = await state.get_data()
    bot_message_id = data.get('bot_message_id')
    await bot.send_message(
        chat_id=message.chat.id,
        text="Введите запрашиваемую информацию текстом",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[])
    ).then(lambda msg: asyncio.create_task(delete_after_delay(message.chat.id, msg.message_id, 3)))
    # Keep the original message and state intact
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text="👩‍🎨 Введите описание заявки. \n\nЕсли нужно прикрепить медиафайл - преобразуйте его в ссылку: сервисы для <a href='https://ru.imgbb.com/'>фото</a> и <a href='https://wdfiles.ru/'>видео</a>",
        disable_web_page_preview=True,
        parse_mode="HTML",
        reply_markup=back_to_title_keyboard
    )

# Словарь для маппинга русских и английских названий приоритетов
priority_translation_map = {
    "🛌": "Low",
    "🚶‍♀️": "Medium",
    "🏃‍♀️": "High"
}

@dp.callback_query(F.data.startswith("priority_"))
async def process_priority(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data_parts = callback.data.split("_")
    priority_id = data_parts[1]
    timestamp = int(data_parts[2])
    if time.time() - timestamp > 60:
        await callback.answer("❌ Это действие больше не актуально", show_alert=True)
        return
    priorities = await jira_client.get_priorities()
    priority_name = next(name for name, id in priorities.items() if id == priority_id)
    await state.update_data(priority=priority_name)
    try:
        await callback.message.delete()
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщения с приоритетами: {e}")
    progress_message = await callback.message.answer("⏳ Обработка...")
    data = await state.get_data()
    try:
        # Получаем email пользователя из базы данных
        user_id = callback.from_user.id
        user_email = execute_query('SELECT email FROM users WHERE user_id = %s', (user_id,), fetch=True)
        email = user_email[0][0] if user_email else "неизвестная почта"
        # Добавляем email к описанию
        description_with_email = f"{data['description']}\n\nЗаявка от {email}"
        issue_key = await jira_client.create_issue(
            summary=data['title'],
            description=description_with_email,
            priority=data['priority']
        )
        execute_query('''
            INSERT INTO requests (user_id, issue_key, title, status, created_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE title = %s, status = %s
        ''', (callback.from_user.id, issue_key, data['title'], "To Do", data['title'], "To Do"))
        issue_url = f"{JIRA_URL}/browse/{issue_key}"
        await progress_message.edit_text(
            "💆‍♂️  Главное меню  💆‍♀️",
            reply_markup=main_keyboard
        )
        success_message = await callback.message.answer(
            f"✅ Заявка успешно создана!\n"
            f"🔑 Ключ: <code>{issue_key}</code>\n"
            f"📊 Статус: Можете отслеживать в разделе 'Мои заявки'",
            parse_mode="HTML"
        )
        await asyncio.sleep(3)
        await success_message.delete()
    except Exception as e:
        await progress_message.edit_text(f"❌ Ошибка: {str(e)}")
    finally:
        await state.clear()

# Словари для маппинга эмодзи и русских названий статусов
status_emoji_map = {
    "To Do": "🔵",
    "In Progress": "🟡",
    "Testing": "🟠",
    "Declined": "🔴",
    "Stopped": "⚪",
    "Done": "🟢",
    "Backlog": "⚫"
}

status_translation_map = {
    "To Do": "К выполнению",
    "In Progress": "В работе",
    "Testing": "Тестирование",
    "Declined": "Отменена",
    "Stopped": "Приостановлена",
    "Done": "Выполнена",
    "Backlog": "В ожидании"
}

event_type_translation_map = {
    "comment_added": "комментарий",
    "status_changed": "статус",
    "assignee_changed": "исполнитель"
}

@dp.callback_query(F.data == "my_requests")
async def show_my_requests(callback: types.CallbackQuery, page: int = 1):
    user_id = callback.from_user.id
    result = execute_query('SELECT is_verified FROM users WHERE user_id = %s', (user_id,), fetch=True)
    if not result or not result[0][0]:
        await callback.message.edit_text("Вы не зарегистрированы. Используйте /start.")
        await callback.answer()
        return
    count = execute_query('''
        SELECT COUNT(*) 
        FROM requests 
        WHERE user_id = %s 
        AND (status != 'Done' OR created_at >= DATE_SUB(NOW(), INTERVAL 3 MONTH))
    ''', (user_id,), fetch=True)[0][0]
    count = min(count, 30)  # Ограничиваем максимум 30 заявок
    if count == 0:
        await callback.message.edit_text(
            "🙅‍♂️ У вас нет активных заявок 🙅‍♀️",
            parse_mode="HTML",
            reply_markup=back_to_main_keyboard
        )
        await callback.answer()
        return
    per_page = 5
    total_pages = (count + per_page - 1) // per_page
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    requests = execute_query('''
        SELECT issue_key, title, status, created_at
        FROM requests 
        WHERE user_id = %s 
        AND (status != 'Done' OR created_at >= DATE_SUB(NOW(), INTERVAL 3 MONTH))
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    ''', (user_id, per_page, offset), fetch=True)
    
    rows = []
    for issue_key, title, status, created_at in requests:
        emoji = status_emoji_map.get(status, "")
        formatted_date = datetime.strptime(str(created_at), '%Y-%m-%d %H:%M:%S').strftime('%d.%m')
        short_title = title[:20] + "..." if len(title) > 20 else title
        button_text = f"{emoji} {issue_key} | {short_title} | {formatted_date} "
        rows.append([InlineKeyboardButton(
            text=button_text,
            callback_data=f"task_{issue_key}_{int(time.time())}_{page}"
        )])
    
    if count > per_page:
        pagination_buttons = []
        if page > 1:
            pagination_buttons.append(InlineKeyboardButton(text="👈", callback_data=f"request_page_{page-1}"))
        pagination_buttons.append(InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data=f"request_page_{page}"))
        if page < total_pages:
            pagination_buttons.append(InlineKeyboardButton(text="👉", callback_data=f"request_page_{page+1}"))
        if pagination_buttons:
            rows.append(pagination_buttons)
    
    # Добавляем кнопки "Инфо" и "Назад" в одном ряду на первой странице, если есть заявки
    if count > 0 and page == 1:
        rows.append([
            InlineKeyboardButton(text="💡", callback_data="info_button"),
            InlineKeyboardButton(text="↩️", callback_data="back")
        ])
    else:
        rows.append([InlineKeyboardButton(text="↩️", callback_data="back")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    
    try:
        await callback.message.edit_text(
            "💁‍♀️ Ваши заявки:",
            reply_markup=keyboard
        )
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения со списком заявок: {e}")
        await callback.message.delete()
        await bot.send_message(
            chat_id=callback.from_user.id,
            text="💁‍♀️ Ваши заявки:",
            reply_markup=keyboard
        )
    await callback.answer()

@dp.callback_query(F.data == "info_button")
async def info_button_handler(callback: types.CallbackQuery):
    await callback.answer("🔵 - К выполнению\n🟡 - В работе\n🟠 - Тестируется\n🔴 - Отменена\n⚪ - Приостановлена\n🟢 - Выполнена\n⚫ - В ожидании", show_alert=True)

@dp.callback_query(F.data.startswith("request_page_"))
async def request_page_handler(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[2])
    await show_my_requests(callback, page)

@dp.callback_query(F.data.startswith("task_"))
async def handle_task_click(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split("_")
    issue_key = parts[1]
    timestamp = int(parts[2])
    page = int(parts[3])
    if time.time() - timestamp > 60:
        await callback.answer("❌ Это действие больше не актуально", show_alert=True)
        return
    try:
        issue_details = await jira_client.get_issue_details(issue_key)
        if issue_details is None:
            error_msg = await bot.send_message(
                chat_id=callback.from_user.id,
                text=f"❌ Задача {issue_key} не найдена в Jira"
            )
            await asyncio.sleep(2)
            await error_msg.delete()
            await show_my_requests(callback, page)
            return
        comments = await jira_client.get_issue_comments(issue_key)
        if comments:
            last_comment = comments[-1]["body"]
            last_comment_author = comments[-1]["author"]
            last_comment_author_display = "Вас" if last_comment_author == "ORTP Bot" else last_comment_author
            comment_text = f"💬 Последний комментарий от <b>{last_comment_author_display}</b>: <b>{last_comment}</b>\n\n"
        else:
            comment_text = f"💬 Последний комментарий: <b>Нет комментариев</b>\n\n"
        created = datetime.strptime(issue_details['created'], "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%d.%m.%Y %H:%M")
        updated = datetime.strptime(issue_details['updated'], "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%d.%m.%Y %H:%M")
        message_text = (
            f"🔑 Ключ задачи: <code>{issue_key}</code>\n"
            f"📝 Тема: <b>{issue_details['summary']}</b>\n"
            f"📋 Описание: <b>{issue_details['description']}</b>\n"
            f"👤 Исполнитель: <b>{issue_details['assignee']}</b>\n"
            f"📊 Статус: <b>{status_translation_map.get(issue_details['status'], issue_details['status'])}</b>\n"
            #f"📅 Создана: <b>{created}</b>\n"
            #f"🔄 Обновлена: <b>{updated}</b>\n"
            f"{comment_text}"
            f"🗣 Оставьте комментарий к задаче:"
        )
        task_message_id = None
        try:
            await callback.message.edit_text(
                message_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️", callback_data=f"request_page_{page}")]
                ])
            )
            task_message_id = callback.message.message_id  # ID после редактирования остаётся тем же
        except Exception as e:
            logging.error(f"Ошибка при редактировании сообщения: {e}")
            try:
                await callback.message.delete()
                new_message = await bot.send_message(
                    chat_id=callback.from_user.id,
                    text=message_text,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="↩️", callback_data=f"request_page_{page}")]
                    ])
                )
                task_message_id = new_message.message_id  # ID нового сообщения
            except Exception as e:
                logging.error(f"Ошибка при отправке нового сообщения: {e}")
                await bot.send_message(
                    chat_id=callback.from_user.id,
                    text="❌ Ошибка при отображении задачи. Пожалуйста, попробуйте снова.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="↩️", callback_data=f"request_page_{page}")]
                    ])
                )
        if task_message_id:
            await state.update_data(task_message_id=task_message_id)  # Сохраняем ID в state
        await state.update_data(issue_key=issue_key)
        await state.set_state(BotStates.add_comment)
    except Exception as e:
        logging.error(f"Ошибка при получении данных задачи {issue_key}: {e}")

@dp.message(BotStates.add_comment)
async def process_comment(message: types.Message, state: FSMContext):
    await message.delete()  # Удаляем сообщение пользователя с текстом комментария
    data = await state.get_data()
    issue_key = data["issue_key"]
    task_message_id = data.get("task_message_id")  # Получаем сохранённый ID сообщения с деталями
    comment = message.text
    progress_message = await send_progress_animation(message)
    try:
        await jira_client.add_comment_to_issue(issue_key, comment)
        await progress_message.edit_text(
            f"✅ Комментарий добавлен к задаче {issue_key}",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_after_delay(message.chat.id, progress_message.message_id, 3))  # Удаляем сообщение об успехе через 3 секунды
        
        # После успеха: редактируем сообщение с деталями на главное меню
        if task_message_id:
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=task_message_id,
                    text="💆‍♂️  Главное меню  💆‍♀️",
                    reply_markup=main_keyboard
                )
            except Exception as e:
                logging.error(f"Ошибка при редактировании сообщения с деталями задачи: {e}")
                # Если не удалось отредактировать, отправляем новое
                await bot.send_message(
                    chat_id=message.chat.id,
                    text="💆‍♂️  Главное меню  💆‍♀️",
                    reply_markup=main_keyboard
                )
    except Exception as e:
        await progress_message.edit_text(f"❌ Ошибка: {str(e)}")
        asyncio.create_task(delete_after_delay(message.chat.id, progress_message.message_id, 5))  # Удаляем сообщение об ошибке через 5 секунд
    finally:
        await state.clear()

# === Комментируем функционал команды ORTP ===
"""
@dp.callback_query(F.data == "team_ortp")
async def team_ortp_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    result = execute_query('SELECT is_verified FROM users WHERE user_id = %s', (user_id,), fetch=True)
    if not result or not result[0][0]:
        await callback.message.edit_text("Вы не зарегистрированы. Используйте /start.")
        await callback.answer()
        return
    team_members = execute_query('SELECT position, last_name, first_name, middle_name FROM team', fetch=True)
    if not team_members:
        if callback.from_user.id == ADMIN_ID:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать сотрудника", callback_data="create_team_member")],
                [InlineKeyboardButton(text="↩️", callback_data="back")]
            ])
            await callback.message.edit_text("❌ Нет данных о команде", reply_markup=keyboard)
        else:
            await callback.message.edit_text("❌ Нет данных о команде", reply_markup=back_to_requests_keyboard)
        return
    if callback.from_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{position} {last_name} {first_name[0]}. {middle_name[0]}.",
                callback_data=f"team_{idx}"
            )] for idx, (position, last_name, first_name, middle_name) in enumerate(team_members)
        ] + [
            [InlineKeyboardButton(text="➕ Создать сотрудника", callback_data="create_team_member")],
            [InlineKeyboardButton(text="↩️", callback_data="back")]
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{position} {last_name} {first_name[0]}. {middle_name[0]}.",
                callback_data=f"team_{idx}"
            )] for idx, (position, last_name, first_name, middle_name) in enumerate(team_members)
        ] + [[InlineKeyboardButton(text="↩️", callback_data="back")]])
    await callback.message.edit_text("👥 Команда ORTP:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "create_team_member")
async def create_team_member_handler(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ У вас нет прав для этого действия")
        return
    await state.set_state(BotStates.add_team_member)
    await callback.message.edit_text(
        "📝 Введите данные сотрудника в формате:\n"
        "<b>Должность Фамилия Имя Отчество Описание</b>\n\n"
        "Пример:\n"
        "<i>Менеджер Иванов Иван Иванович Опытный специалист с 10-летним стажем</i>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard
    )
    await callback.answer()

@dp.message(BotStates.add_team_member)
async def process_team_member_data(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У вас нет прав для этого действия")
        await state.clear()
        return
    try:
        parts = message.text.split(maxsplit=4)
        if len(parts) < 5:
            raise ValueError("Недостаточно данных")
        position, last_name, first_name, middle_name, description = parts
        execute_query('''
            INSERT INTO team (position, last_name, first_name, middle_name, description)
            VALUES (%s, %s, %s, %s, %s)
        ''', (position, last_name, first_name, middle_name, description))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️", callback_data="team_ortp")]
        ])
        await message.answer(
            "✅ Сотрудник был успешно добавлен",
            reply_markup=keyboard
        )
    except Exception as e:
        logging.error(f"Ошибка при добавлении сотрудника: {e}")
        await message.answer(
            "❌ Ошибка при добавлении сотрудника. Проверьте формат ввода и попробуйте снова.",
            reply_markup=cancel_keyboard
        )
    finally:
        await state.clear()

@dp.callback_query(F.data.startswith("team_"))
async def show_team_member(callback: types.CallbackQuery, state: FSMContext):
    member_idx = int(callback.data.split("_")[1])
    team_members = execute_query('SELECT position, last_name, first_name, middle_name, photo_path, description FROM team', fetch=True)
    if not team_members or member_idx >= len(team_members):
        await callback.answer("❌ Член команды не найден")
        return
    position, last_name, first_name, middle_name, photo_path, description = team_members[member_idx]
    message_text = (
        f"👤 <b>{position} {last_name} {first_name} {middle_name}</b>\n\n"
        f"📝 <i>{description}</i>"
    )
    back_to_list_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️", callback_data="back_to_team_list")]
    ])
    try:
        await callback.message.delete()
        if photo_path and os.path.exists(photo_path):
            with open(photo_path, 'rb') as photo_file:
                await callback.message.answer_photo(
                    photo=types.BufferedInputFile(photo_file.read(), filename=os.path.basename(photo_path)),
                    caption=message_text,
                    parse_mode="HTML",
                    reply_markup=back_to_list_keyboard
                )
        else:
            await callback.message.answer(
                message_text,
                parse_mode="HTML",
                reply_markup=back_to_list_keyboard
            )
    except Exception as e:
        logging.error(f"Ошибка при отправке фото: {e}")
        await callback.message.answer(
            message_text,
            parse_mode="HTML",
            reply_markup=back_to_list_keyboard
        )
    await state.set_state(BotStates.view_team_member)
    await callback.answer()

@dp.callback_query(F.data == "back_to_team_list")
async def back_to_team_list_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    team_members = execute_query('SELECT position, last_name, first_name, middle_name FROM team', fetch=True)
    if not team_members:
        if callback.from_user.id == ADMIN_ID:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать сотрудника", callback_data="create_team_member")],
                [InlineKeyboardButton(text="↩️", callback_data="back")]
            ])
            await callback.message.answer("❌ Нет данных о команде", reply_markup=keyboard)
        else:
            await callback.message.answer("❌ Нет данных о команде", reply_markup=back_to_requests_keyboard)
        return
    if callback.from_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{position} {last_name} {first_name[0]}. {middle_name[0]}.",
                callback_data=f"team_{idx}"
            )] for idx, (position, last_name, first_name, middle_name) in enumerate(team_members)
        ] + [
            [InlineKeyboardButton(text="➕ Создать сотрудника", callback_data="create_team_member")],
            [InlineKeyboardButton(text="↩️", callback_data="back")]
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{position} {last_name} {first_name[0]}. {middle_name[0]}.",
                callback_data=f"team_{idx}"
            )] for idx, (position, last_name, first_name, middle_name) in enumerate(team_members)
        ] + [[InlineKeyboardButton(text="↩️", callback_data="back")]])
    try:
        await callback.message.delete()
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщения: {e}")
    await callback.message.answer("👥 Команда ORTP:", reply_markup=keyboard)
    await callback.answer()
"""

@dp.callback_query(F.data == "notifications")
async def notifications_handler(callback: types.CallbackQuery):
    await show_notifications(callback, 1)

@dp.callback_query(F.data.startswith("notif_page_"))
async def notif_page_handler(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[2])
    await show_notifications(callback, page)

async def show_notifications(callback: types.CallbackQuery, page: int = 1):
    user_id = callback.from_user.id
    result = execute_query('SELECT is_verified FROM users WHERE user_id = %s', (user_id,), fetch=True)
    if not result or not result[0][0]:
        await callback.message.edit_text("Вы не зарегистрированы. Используйте /start.")
        await callback.answer()
        return
    count = execute_query('SELECT COUNT(*) FROM notifications WHERE user_id = %s', (user_id,), fetch=True)[0][0]
    if count == 0:
        try:
            await callback.message.edit_text(
                "🙅‍♂️ У вас нет уведомлений 🙅‍♀️",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️", callback_data="back")]
                ])
            )
        except Exception as e:
            logging.error(f"Ошибка при редактировании сообщения (нет уведомлений): {e}")
            await bot.send_message(
                chat_id=callback.from_user.id,
                text="🙅‍♂️ У вас нет уведомлений 🙅‍♀️",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️", callback_data="back")]
                ])
            )
        await callback.answer()
        return
    per_page = 8
    total_pages = (count + per_page - 1) // per_page
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    notifications = execute_query('''
        SELECT id, issue_key, event_type, message_text, timestamp, is_read 
        FROM notifications 
        WHERE user_id = %s 
        ORDER BY timestamp DESC
        LIMIT %s OFFSET %s
    ''', (user_id, per_page, offset), fetch=True)
    rows = [
        [InlineKeyboardButton(
            text=f"{'🔘 ' if not is_read else ''}{issue_key} {event_type_translation_map.get(event_type, event_type)} {datetime.strptime(str(timestamp), '%Y-%m-%d %H:%M:%S').strftime('%d.%m %H:%M')}",
            callback_data=f"notif_{notif_id}_{int(time.time())}_{page}"
        )] for notif_id, issue_key, event_type, message_text, timestamp, is_read in notifications
    ]
    if count > per_page:
        pagination_buttons = []
        if page > 1:
            pagination_buttons.append(InlineKeyboardButton(text="👈", callback_data=f"notif_page_{page-1}"))
        pagination_buttons.append(InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data=f"notif_page_{page}"))
        if page < total_pages:
            pagination_buttons.append(InlineKeyboardButton(text="👉", callback_data=f"notif_page_{page+1}"))
        if pagination_buttons:
            rows.append(pagination_buttons)
    rows.append([InlineKeyboardButton(text="↩️", callback_data="back")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(
            text="🤳 Список уведомлений:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения со списком уведомлений: {e}")
        await bot.send_message(
            chat_id=callback.from_user.id,
            text="🤳 Список уведомлений:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("notif_delete_"))
async def delete_notification(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    notif_id = parts[2]
    page = int(parts[3])
    user_id = callback.from_user.id
    logging.info(f"Обработка callback notif_delete_{notif_id} для пользователя {user_id}")
    
    execute_query('DELETE FROM notifications WHERE id = %s', (notif_id,))
    logging.info(f"Уведомление с ID {notif_id} удалено из базы")
    
    count = execute_query(
        'SELECT COUNT(*) FROM notifications WHERE user_id = %s',
        (user_id,),
        fetch=True
    )[0][0]
    
    if count == 0:
        try:
            await callback.message.edit_text(
                "🙅‍♂️ У вас нет уведомлений 🙅‍♀️",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️", callback_data="back")]
                ])
            )
            logging.info(f"Последнее уведомление удалено, отредактировано сообщение для пользователя {user_id}")
        except Exception as e:
            logging.error(f"Ошибка при редактировании сообщения (последнее уведомление): {e}")
            await bot.send_message(
                chat_id=user_id,
                text="🙅‍♂️ У вас нет уведомлений 🙅‍♀️",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️", callback_data="back")]
                ])
            )
    else:
        per_page = 8
        total_pages = (count + per_page - 1) // per_page
        if (page - 1) * per_page >= count:
            page = total_pages
        await show_notifications(callback, page)
        logging.info(f"Остались другие уведомления, показан обновлённый список для пользователя {user_id}")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("notif_"))
async def show_notification_details(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    notif_id, timestamp, page = parts[1], parts[2], parts[3]
    logging.info(f"Обработка callback notif_{notif_id}_{timestamp}")
    if time.time() - int(timestamp) > 60:
        logging.info(f"Callback notif_{notif_id}_{timestamp} устарел")
        try:
            await callback.message.edit_text(
                text="❌ Это действие больше не актуально",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад к уведомлениям", callback_data="notifications")]
                ])
            )
        except Exception as e:
            logging.error(f"Ошибка при редактировании сообщения для устаревшего callback: {e}")
            await bot.send_message(
                chat_id=callback.from_user.id,
                text="❌ Это действие больше не актуально",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад к уведомлениям", callback_data="notifications")]
                ])
            )
        await callback.answer()
        return
    notification = execute_query('SELECT message_text FROM notifications WHERE id = %s', (notif_id,), fetch=True)
    if not notification:
        logging.info(f"Уведомление с ID {notif_id} не найдено")
        try:
            await callback.message.edit_text(
                text="❌ Уведомление не найдено или было удалено",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад к уведомлениям", callback_data="notifications")]
                ])
            )
        except Exception as e:
            logging.error(f"Ошибка при редактировании сообщения для несуществующего уведомления {notif_id}: {e}")
            await bot.send_message(
                chat_id=callback.from_user.id,
                text="❌ Уведомление не найдено или было удалено",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад к уведомлениям", callback_data="notifications")]
                ])
            )
        await callback.answer()
        return
    message_text = notification[0][0]
    execute_query('UPDATE notifications SET is_read = TRUE WHERE id = %s', (notif_id,))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️", callback_data=f"notif_delete_{notif_id}_{page}")],
        [InlineKeyboardButton(text="↩️", callback_data=f"notif_page_{page}")]
    ])
    try:
        await callback.message.edit_text(
            f"🧏‍♀️ Уведомление:\n\n{message_text}",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения с уведомлением {notif_id}: {e}")
        await bot.send_message(
            chat_id=callback.from_user.id,
            text=f"🧏‍♀️ Уведомление:\n\n{message_text}",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    await callback.answer()

@dp.callback_query(F.data == "back")
async def back_button_handler(callback: types.CallbackQuery, state: FSMContext):
    await handle_back(callback, state)

@dp.callback_query(F.data == "back_to_requests")
async def back_to_requests_button_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_my_requests(callback, 1)
    await callback.answer()

@dp.callback_query(F.data == "back_to_title")
async def back_to_title_button_handler(callback: types.CallbackQuery, state: FSMContext):
    await handle_back_to_title(callback, state)

@dp.callback_query(F.data == "back_to_description")
async def back_to_description_button_handler(callback: types.CallbackQuery, state: FSMContext):
    await handle_back_to_description(callback, state)

@dp.callback_query(F.data == "cancel")
async def cancel_button_handler(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state == BotStates.verify_code.state:
        await callback.message.edit_text(
            "👨‍💼 Введите свою корпоративную почту 👩‍💼\n\n👉 @pari.ru"
        )
        await state.set_state(BotStates.verify_email)
    else:
        await handle_cancel(callback, state)
    await callback.answer()

@dp.callback_query(F.data == "hide_notification")
async def hide_notification(callback: types.CallbackQuery):
    await callback.answer()
    new_text = '📳 Сообщение скрыто и будет доступно во вкладке "Уведомления"'
    empty_keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    await callback.message.edit_text(new_text, reply_markup=empty_keyboard)
    asyncio.create_task(delete_after_delay(callback.message.chat.id, callback.message.message_id, 5))

# === Webhook handler для уведомлений от Jira ===
async def jira_webhook_handler(request: web.Request):
    try:
        secret = WEBHOOK_SECRET.encode('utf-8')
        signature = request.headers.get('X-Hub-Signature')
        if signature:
            method, signature = signature.split('=')
            body = await request.read()
            expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
            logging.info(f"Signature: {signature}, Expected: {expected}")
            if not hmac.compare_digest(signature, expected):
                logging.error("Неверная подпись webhook")
                return web.Response(status=401)
        
        data = await request.json()
        logging.info(f"Получен webhook: {data}")

        event = data.get('event')
        if not event:
            logging.info("Webhook не содержит события")
            return web.Response(status=200)

        issue_key = data.get('issue_key')
        if not issue_key:
            logging.info("Webhook не содержит ключа задачи")
            return web.Response(status=200)

        result = execute_query(
            'SELECT user_id, status FROM requests WHERE issue_key = %s',
            (issue_key,), fetch=True
        )
        if not result:
            logging.info(f"Задача {issue_key} не найдена в базе")
            return web.Response(status=200)
        user_id, last_status = result[0]

        try:
            issue_info = await jira_client.get_issue_status(issue_key)
            current_status = issue_info['status']
            priority = issue_info['priority']
        except Exception as e:
            logging.error(f"Ошибка при получении статуса/приоритета для {issue_key}: {e}")
            return web.Response(status=200)


        if event == 'status_changed':
            from_status = data.get('status', {}).get('from', 'Неизвестно')
            to_status = data.get('status', {}).get('to', 'Неизвестно')
            from_translated = status_translation_map.get(from_status, from_status)
            to_translated = status_translation_map.get(to_status, to_status)
            if to_status == last_status:
                logging.info(f"Статус задачи {issue_key} не изменился")
                return web.Response(status=200)
            if should_notify():
                message_text = f"🙋‍♀️ Статус вашей заявки 🔑{issue_key} изменился с '{from_translated}' на '{to_translated}'"
                sent_message = await bot.send_message(chat_id=user_id, text=message_text, parse_mode="HTML", reply_markup=hide_notification_keyboard)
                execute_query(
                    'INSERT INTO notifications (user_id, issue_key, event_type, message_text) VALUES (%s, %s, %s, %s)',
                    (user_id, issue_key, event, message_text)
                )
                execute_query('''
                    DELETE n
                    FROM notifications n
                    LEFT JOIN (
                        SELECT id
                        FROM notifications
                        WHERE user_id = %s
                        ORDER BY timestamp DESC
                        LIMIT 100
                    ) AS keep ON n.id = keep.id
                    WHERE n.user_id = %s
                    AND keep.id IS NULL
                ''', (user_id, user_id))
                logging.info(f"Отправлено уведомление о смене статуса для {issue_key} пользователю {user_id}")
            #current_status = status_translation_map.get(current_status, current_status)  # Перевод для базы (опционально, удали если не нужно)
            execute_query('UPDATE requests SET status = %s WHERE issue_key = %s', (to_status, issue_key))

        
        elif event == 'comment_added':
            initiator = data.get('initiator', 'Неизвестный')
            initiator_displayName = data.get('initiator_displayName', 'Неизвестный')
            comment = data.get('comment', 'Нет текста')
            if initiator != 'ortp_bot' and should_notify():
                message_text = f"💁‍♀️ Новый комментарий к вашей заявке 🔑{issue_key} от 👩‍💼 {initiator_displayName}: {comment}. \n\nЕсли хотите ответить - перейдите в раздел \"Мои заявки\" и выберите заявку 🔑{issue_key}."
                sent_message = await bot.send_message(chat_id=user_id, text=message_text, parse_mode="HTML", reply_markup=hide_notification_keyboard)
                execute_query(
                    'INSERT INTO notifications (user_id, issue_key, event_type, message_text) VALUES (%s, %s, %s, %s)',
                    (user_id, issue_key, event, message_text)
                )
                execute_query('''
                    DELETE n
                    FROM notifications n
                    LEFT JOIN (
                        SELECT id
                        FROM notifications
                        WHERE user_id = %s
                        ORDER BY timestamp DESC
                        LIMIT 100
                    ) AS keep ON n.id = keep.id
                    WHERE n.user_id = %s
                    AND keep.id IS NULL
                ''', (user_id, user_id))
                logging.info(f"Отправлено уведомление о новом комментарии для {issue_key} пользователю {user_id}")
        

        elif event == 'assignee_changed':
            from_assignee = data.get('assignee', {}).get('from', 'Не назначен')
            to_assignee = data.get('assignee', {}).get('to', 'Не назначен') or 'Не назначен'
            if should_notify():
                message_text = f"👩‍💼 Новый исполнитель вашей заявки 🔑{issue_key} - 🙋‍♀️ {to_assignee}"
                sent_message = await bot.send_message(chat_id=user_id, text=message_text, parse_mode="HTML", reply_markup=hide_notification_keyboard)
                execute_query(
                    'INSERT INTO notifications (user_id, issue_key, event_type, message_text) VALUES (%s, %s, %s, %s)',
                    (user_id, issue_key, event, message_text)
                )
                execute_query('''
                    DELETE n
                    FROM notifications n
                    LEFT JOIN (
                        SELECT id
                        FROM notifications
                        WHERE user_id = %s
                        ORDER BY timestamp DESC
                        LIMIT 100
                    ) AS keep ON n.id = keep.id
                    WHERE n.user_id = %s
                    AND keep.id IS NULL
                ''', (user_id, user_id))
                logging.info(f"Отправлено уведомление о смене исполнителя для {issue_key} пользователю {user_id}")

        else:
            logging.info(f"Неизвестное событие: {event}")
        
        return web.Response(status=200)
    except Exception as e:
        logging.error(f"Ошибка в webhook handler: {e}")
        return web.Response(status=500)

# === Запуск бота ===
async def main():
    logging.info("🤖 Бот запущен")
    app = web.Application()
    app.add_routes([web.post(WEBHOOK_PATH, jira_webhook_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_SERVER_HOST, WEBHOOK_SERVER_PORT)
    await site.start()
    logging.info(f"Webhook сервер запущен на {WEBHOOK_SERVER_HOST}:{WEBHOOK_SERVER_PORT}")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())