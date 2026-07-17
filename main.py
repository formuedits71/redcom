"""
Load Confirmation Telegram Bot
==============================

A production-ready Telegram bot that:
  * Stores a per-user Load Confirmation template (forever, in PostgreSQL).
  * Accepts a PDF rate confirmation / load document.
  * Extracts shipment data from the PDF (PyMuPDF, falls back to pdfplumber).
  * Calculates driving miles between pickup and delivery using OpenRouteService.
  * Sends the template + extracted data to Google Gemini, which fills the
    template with the new values while preserving the user's exact formatting.
  * Supports English, Russian and Uzbek (auto-detected from Telegram).
  * Has a full admin panel (stats, broadcast, search, export, backup, cache).

Everything required to run the bot lives in this single file, as requested.

Run:
    pip install -r requirements.txt
    python main.py
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import signal
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import asyncpg
import fitz  # PyMuPDF
import pdfplumber
from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ======================================================================================
# CONFIGURATION
# ======================================================================================

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
ADMIN_ID_RAW: str = os.getenv("ADMIN_ID", "0").strip()

try:
    ADMIN_ID: int = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 0
except ValueError:
    ADMIN_ID = 0

REQUIRED_ENV = {
    "BOT_TOKEN": BOT_TOKEN,
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "DATABASE_URL": DATABASE_URL,
}

# ======================================================================================
# LOGGING
# ======================================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("loadconf-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# ======================================================================================
# TRANSLATIONS
# ======================================================================================

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "start_welcome": "👋 Welcome to Load Confirmation Bot!\n\nI turn your PDF rate confirmations into a finished Load Confirmation, formatted exactly the way you like it.",
        "ask_template": "📝 Please send me your Load Confirmation template as plain text.\n\nThis is a one-time setup — I will remember it forever and reuse it for every load.",
        "template_saved": "✅ Your template has been saved!\n\nNow please send me your PDF (rate confirmation / load document).",
        "ask_pdf": "📄 Please send me your PDF (rate confirmation / load document).",
        "help_text": (
            "🤖 <b>Load Confirmation Bot — Help</b>\n\n"
            "<b>/start</b> — start / restart the bot\n"
            "<b>/help</b> — show this help message\n"
            "<b>/template</b> — view or replace your saved template\n"
            "<b>/delete</b> — delete your saved template\n\n"
            "Just send a PDF any time and I will generate your Load Confirmation."
        ),
        "template_current": "📋 Your current template:\n\n<code>{template}</code>\n\nSend a new template to replace it, or use /delete to remove it.",
        "template_none": "You don't have a saved template yet. Please send me your Load Confirmation template as plain text.",
        "template_deleted": "🗑️ Your template has been deleted. Send a new one whenever you're ready.",
        "template_none_to_delete": "You don't have a saved template to delete.",
        "admin_denied": "⛔ This command is for administrators only.",
        "processing_pdf": "⏳ Processing your PDF... this can take up to a minute.",
        "pdf_parse_error": "❌ I couldn't read that PDF. Please make sure it's a valid, non-corrupted PDF file and try again.",
        "not_a_pdf": "📎 That doesn't look like a PDF. Please send a PDF document.",
        "need_template_first": "First, let's set up your template. Please send me your Load Confirmation template as plain text.",
        "ors_error": "❌ I couldn't calculate the mileage for these addresses. Please check the pickup/delivery addresses in the PDF and try again.",
        "gemini_error": "❌ I couldn't generate the Load Confirmation right now (AI service error). Please try again in a moment.",
        "generic_error": "❌ Something went wrong while processing your request. Please try again.",
        "result_ready": "✅ Here is your Load Confirmation:",
        "extracted_missing": "⚠️ Some fields could not be found in the PDF and were left blank. Please double-check the result.",
        "admin_panel_title": "🛠️ <b>Admin Panel</b>",
        "cache_cleared": "✅ Cache cleared.",
        "broadcast_prompt": "📢 Send me the message you want to broadcast to all users.",
        "broadcast_done": "✅ Broadcast finished. Sent: {sent}, Failed: {failed}.",
        "search_prompt": "🔎 Send me a Telegram ID or username to search for.",
        "search_not_found": "No user found matching '{query}'.",
        "delete_template_prompt": "Send the Telegram ID of the user whose template you want to delete.",
        "view_template_prompt": "Send the Telegram ID of the user whose template you want to view.",
        "admin_action_done": "✅ Done.",
        "choose_language": "🌐 Choose language / Выберите язык / Tilni tanlang",
        "language_set": "✅ Language set to {lang}",
        "subscription_required": "⛔ You must subscribe to {channel} to use this bot.",
        "admin_menu": "🛠️ <b>Admin Panel</b>\n\n<b>Subscriptions:</b>\n/add_sub - add mandatory subscription\n/remove_sub - remove subscription\n/list_subs - list subscriptions\n\n<b>Other:</b>\n/broadcast - broadcast message\n/search - search user\n/clearcache - clear cache",
        "add_sub_prompt": "📌 Send chat_id of channel/group to add as mandatory subscription.",
        "remove_sub_prompt": "🗑️ Send chat_id to remove from subscriptions.",
        "sub_added": "✅ Subscription added: {chat_id}",
        "sub_removed": "✅ Subscription removed: {chat_id}",
        "list_subs": "📋 Mandatory subscriptions:\n{subs}",
    },
    "ru": {
        "start_welcome": "👋 Добро пожаловать в Load Confirmation Bot!\n\nЯ превращаю ваши PDF rate confirmation в готовый Load Confirmation в вашем собственном формате.",
        "ask_template": "📝 Пожалуйста, пришлите ваш шаблон Load Confirmation в виде обычного текста.\n\nЭто нужно сделать один раз — я запомню его навсегда.",
        "template_saved": "✅ Ваш шаблон сохранён!\n\nТеперь отправьте ваш PDF (rate confirmation / load document).",
        "ask_pdf": "📄 Пожалуйста, отправьте ваш PDF (rate confirmation / load document).",
        "help_text": (
            "🤖 <b>Load Confirmation Bot — Помощь</b>\n\n"
            "<b>/start</b> — запустить / перезапустить бота\n"
            "<b>/help</b> — показать это сообщение\n"
            "<b>/template</b> — посмотреть или заменить шаблон\n"
            "<b>/delete</b> — удалить сохранённый шаблон\n\n"
            "Просто отправьте PDF в любое время, и я сгенерирую Load Confirmation."
        ),
        "template_current": "📋 Ваш текущий шаблон:\n\n<code>{template}</code>\n\nОтправьте новый шаблон, чтобы заменить его, или используйте /delete.",
        "template_none": "У вас ещё нет сохранённого шаблона. Пожалуйста, пришлите ваш шаблон Load Confirmation.",
        "template_deleted": "🗑️ Ваш шаблон удалён. Отправьте новый, когда будете готовы.",
        "template_none_to_delete": "У вас нет сохранённого шаблона для удаления.",
        "admin_denied": "⛔ Эта команда доступна только администраторам.",
        "processing_pdf": "⏳ Обрабатываю ваш PDF... это может занять до минуты.",
        "pdf_parse_error": "❌ Не удалось прочитать этот PDF. Убедитесь, что файл корректный, и попробуйте снова.",
        "not_a_pdf": "📎 Это не похоже на PDF. Пожалуйста, отправьте PDF-документ.",
        "need_template_first": "Сначала настроим шаблон. Пришлите ваш шаблон Load Confirmation в виде текста.",
        "ors_error": "❌ Не удалось рассчитать пробег для этих адресов. Проверьте адреса погрузки/выгрузки в PDF.",
        "gemini_error": "❌ Не удалось сгенерировать Load Confirmation (ошибка ИИ-сервиса). Попробуйте ещё раз.",
        "generic_error": "❌ Произошла ��шибка при обработке запроса. Попробуйте снова.",
        "result_ready": "✅ Ваш Load Confirmation готов:",
        "extracted_missing": "⚠️ Некоторые поля не найдены в PDF и оставлены пустыми. Проверьте результат.",
        "admin_panel_title": "🛠️ <b>Панель администратора</b>",
        "cache_cleared": "✅ Кэш очищен.",
        "broadcast_prompt": "📢 Отправьте сообщение для рассылки всем пользователям.",
        "broadcast_done": "✅ Рассылка завершена. Отправлено: {sent}, Ошибок: {failed}.",
        "search_prompt": "🔎 Отправьте Telegram ID или username для поиска.",
        "search_not_found": "Пользователь по запросу '{query}' не найден.",
        "delete_template_prompt": "Отправьте Telegram ID пользователя, чей шаблон нужно удалить.",
        "view_template_prompt": "Отправьте Telegram ID пользователя, чей шаблон нужно посмотреть.",
        "admin_action_done": "✅ Готово.",
        "choose_language": "🌐 Выберите язык / Choose language / Tilni tanlang",
        "language_set": "✅ Язык установлен на {lang}",
        "subscription_required": "⛔ Вы должны подписаться на {channel} для использования этого бота.",
        "admin_menu": "🛠️ <b>Админ-панель</b>\n\n<b>Подписки:</b>\n/add_sub - добавить обязательную подписку\n/remove_sub - удалить подписку\n/list_subs - список подписок\n\n<b>Другое:</b>\n/broadcast - рассылка\n/search - поиск пользователя\n/clearache - очистить кэш",
        "add_sub_prompt": "📌 Отправьте chat_id канала/группы для добавления как обязательную подписку.",
        "remove_sub_prompt": "🗑️ Отправьте chat_id для удаления из подписок.",
        "sub_added": "✅ Подписка добавлена: {chat_id}",
        "sub_removed": "✅ Подписка удалена: {chat_id}",
        "list_subs": "📋 Обязательные подписки:\n{subs}",
    },
    "uz": {
        "start_welcome": "👋 Load Confirmation Bot-ga xush kelibsiz!\n\nMen sizning PDF rate confirmation hujjatingizni o'z formatingizda tayyor Load Confirmation-ga aylantiraman.",
        "ask_template": "📝 Iltimos, Load Confirmation shablonini oddiy matn ko'rinishida yuboring.\n\nBuni bir marta qilamiz — men uni abadiy eslab qolaman.",
        "template_saved": "✅ Shablon saqlandi!\n\nEndi PDF (rate confirmation / load document) yuboring.",
        "ask_pdf": "📄 Iltimos, PDF (rate confirmation / load document) yuboring.",
        "help_text": (
            "🤖 <b>Load Confirmation Bot — Yordam</b>\n\n"
            "<b>/start</b> — botni ishga tushirish / qayta ishga tushirish\n"
            "<b>/help</b> — ushbu xabarni ko'rsatish\n"
            "<b>/template</b> — shablonni ko'rish yoki almashtirish\n"
            "<b>/delete</b> — saqlangan shablonni o'chirish\n\n"
            "Istalgan vaqtda PDF yuboring — men Load Confirmation yarataman."
        ),
        "template_current": "📋 Sizning joriy shabloningiz:\n\n<code>{template}</code>\n\nYangi shablon yuboring yoki /delete buyrug'idan foydalaning.",
        "template_none": "Sizda hali saqlangan shablon yo'q. Iltimos, Load Confirmation shablonini matn ko'rinishida yuboring.",
        "template_deleted": "🗑️ Shablon o'chirildi. Tayyor bo'lganingizda yangisini yuboring.",
        "template_none_to_delete": "O'chirish uchun saqlangan shablon yo'q.",
        "admin_denied": "⛔ Bu buyruq faqat administratorlar uchun.",
        "processing_pdf": "⏳ PDF qayta ishlanmoqda... bu bir daqiqagacha vaqt olishi mumkin.",
        "pdf_parse_error": "❌ Ushbu PDF-ni o'qib bo'lmadi. Fayl to'g'ri ekanligiga ishonch hosil qiling.",
        "not_a_pdf": "📎 Bu PDF-ga o'xshamayapti. Iltimos, PDF hujjat yuboring.",
        "need_template_first": "Avval shablonni sozlaymiz. Load Confirmation shablonini matn ko'rinishida yuboring.",
        "ors_error": "❌ Ushbu manzillar uchun masofani hisoblab bo'lmadi. PDF-dagi manzillarni tekshiring.",
        "gemini_error": "❌ Hozircha Load Confirmation yaratib bo'lmadi (AI xizmati xatosi). Birozdan keyin urinib ko'ring.",
        "generic_error": "❌ So'rovni qayta ishlashda xatolik yuz berdi. Qayta urinib ko'ring.",
        "result_ready": "✅ Sizning Load Confirmation-ingiz tayyor:",
        "extracted_missing": "⚠️ Ba'zi maydonlar PDF-da topilmadi va bo'sh qoldirildi. Natijani tekshirib chiqing.",
        "admin_panel_title": "🛠️ <b>Administrator paneli</b>",
        "cache_cleared": "✅ Kesh tozalandi.",
        "broadcast_prompt": "📢 Barcha foydalanuvchilarga yuborish uchun xabar yuboring.",
        "broadcast_done": "✅ Xabar tarqatildi. Yuborildi: {sent}, Xato: {failed}.",
        "search_prompt": "🔎 Qidirish uchun Telegram ID yoki username yuboring.",
        "search_not_found": "'{query}' bo'yicha foydalanuvchi topilmadi.",
        "delete_template_prompt": "Shablonini o'chirish kerak bo'lgan foydalanuvchining Telegram ID sini yuboring.",
        "view_template_prompt": "Shablonini ko'rish kerak bo'lgan foydalanuvchining Telegram ID sini yuboring.",
        "admin_action_done": "✅ Bajarildi.",
        "choose_language": "🌐 Tilni tanlang / Choose language / Выберите язык",
        "language_set": "✅ Til {lang} ga o'rnatildi",
        "subscription_required": "⛔ Ushbu botdan foydalanish uchun {channel} ga obuna bo'lishingiz kerak.",
        "admin_menu": "🛠️ <b>Admin paneli</b>\n\n<b>Obunalar:</b>\n/add_sub - majburiy obuna qo'shish\n/remove_sub - obunani o'chirish\n/list_subs - obunalar ro'yxati\n\n<b>Boshqalar:</b>\n/broadcast - tarqatib yuborish\n/search - foydalanuvchini qidirish\n/clearcache - keshni tozalash",
        "add_sub_prompt": "📌 Kanal/guruhning chat_id sini yuboring majburiy obuna qo'shish uchun.",
        "remove_sub_prompt": "🗑️ Obunadan o'chirish uchun chat_id yuboring.",
        "sub_added": "✅ Obuna qo'shildi: {chat_id}",
        "sub_removed": "✅ Obuna o'chirildi: {chat_id}",
        "list_subs": "📋 Majburiy obunalar:\n{subs}",
    },
}

SUPPORTED_LANGUAGES = ("en", "ru", "uz")


def t(lang: Optional[str], key: str, **kwargs: Any) -> str:
    """Translate `key` into `lang`, falling back to English for missing
    languages or missing keys."""
    lang = lang if lang in SUPPORTED_LANGUAGES else "en"
    text = TEXTS.get(lang, TEXTS["en"]).get(key) or TEXTS["en"].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def normalize_language(language_code: Optional[str]) -> str:
    if not language_code:
        return "en"
    code = language_code.lower()
    if code.startswith("ru"):
        return "ru"
    if code.startswith("uz"):
        return "uz"
    return "en"


# ======================================================================================
# DATABASE LAYER
# ======================================================================================


class Database:
    """Thin async wrapper around an asyncpg connection pool with automatic
    reconnection and table creation."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self, retries: int = 5, delay_seconds: int = 3) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                self.pool = await asyncpg.create_pool(
                    dsn=self.dsn,
                    min_size=1,
                    max_size=10,
                    command_timeout=30,
                )
                logger.info("Database pool created successfully.")
                await self._create_tables()
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.error("DB connect attempt %s/%s failed: %s", attempt, retries, exc)
                await asyncio.sleep(delay_seconds)
        raise RuntimeError(f"Could not connect to database after {retries} attempts: {last_error}")

    async def _create_tables(self) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    language TEXT DEFAULT 'en',
                    joined_at TIMESTAMP DEFAULT NOW(),
                    last_active TIMESTAMP DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS templates (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    template TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    chat_type TEXT NOT NULL,
                    required BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_languages (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    language TEXT DEFAULT 'en',
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            )
        logger.info("Database tables verified/created.")

    async def ensure_connected(self) -> None:
        if self.pool is None or self.pool._closed:  # noqa: SLF001
            logger.warning("Database pool not available, reconnecting...")
            await self.connect()

    async def _execute_with_retry(self, coro_factory):
        """Run a DB operation, reconnecting once and retrying on connection errors."""
        try:
            await self.ensure_connected()
            return await coro_factory()
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError) as exc:
            logger.error("Database connection error, reconnecting and retrying once: %s", exc)
            await self.connect()
            return await coro_factory()

    # ---------------------------------------------------------------- users --

    async def upsert_user(
        self,
        telegram_id: int,
        username: Optional[str],
        first_name: Optional[str],
        language: str,
    ) -> None:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                await conn.execute(
                    """
                    INSERT INTO users (telegram_id, username, first_name, language, joined_at, last_active)
                    VALUES ($1, $2, $3, $4, NOW(), NOW())
                    ON CONFLICT (telegram_id) DO UPDATE
                    SET username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        language = EXCLUDED.language,
                        last_active = NOW();
                    """,
                    telegram_id,
                    username,
                    first_name,
                    language,
                )

        await self._execute_with_retry(_run)

    async def touch_user(self, telegram_id: int) -> None:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                await conn.execute(
                    "UPDATE users SET last_active = NOW() WHERE telegram_id = $1;",
                    telegram_id,
                )

        await self._execute_with_retry(_run)

    async def get_user_language(self, telegram_id: int) -> str:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                row = await conn.fetchrow(
                    "SELECT language FROM users WHERE telegram_id = $1;", telegram_id
                )
                return row["language"] if row else "en"

        return await self._execute_with_retry(_run)

    async def all_user_ids(self) -> list[int]:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                rows = await conn.fetch("SELECT telegram_id FROM users;")
                return [r["telegram_id"] for r in rows]

        return await self._execute_with_retry(_run)

    async def find_user(self, query: str) -> Optional[asyncpg.Record]:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                if query.lstrip("-").isdigit():
                    return await conn.fetchrow(
                        "SELECT * FROM users WHERE telegram_id = $1;", int(query)
                    )
                username = query.lstrip("@")
                return await conn.fetchrow(
                    "SELECT * FROM users WHERE username ILIKE $1;", username
                )

        return await self._execute_with_retry(_run)

    async def count_users(self) -> int:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                return await conn.fetchval("SELECT COUNT(*) FROM users;")

        return await self._execute_with_retry(_run)

    async def count_users_today(self) -> int:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                return await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE joined_at::date = NOW()::date;"
                )

        return await self._execute_with_retry(_run)

    # ---------------------------------------------------------- subscriptions --

    async def add_subscription(self, chat_id: int, chat_type: str, required: bool = False) -> None:
        """Add a mandatory subscription channel or group."""
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                await conn.execute(
                    """
                    INSERT INTO subscriptions (chat_id, chat_type, required)
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING;
                    """,
                    chat_id, chat_type, required
                )
        return await self._execute_with_retry(_run)

    async def remove_subscription(self, chat_id: int) -> None:
        """Remove a subscription."""
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                await conn.execute("DELETE FROM subscriptions WHERE chat_id = $1;", chat_id)
        return await self._execute_with_retry(_run)

    async def get_subscriptions(self) -> list[dict]:
        """Get all subscriptions."""
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                rows = await conn.fetch("SELECT * FROM subscriptions;")
                return [dict(row) for row in rows]
        return await self._execute_with_retry(_run)

    async def get_subscriptions_by_type(self, required: bool) -> list[int]:
        """Get all subscription chat IDs by type (required or optional)."""
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                rows = await conn.fetch(
                    "SELECT chat_id FROM subscriptions WHERE required = $1;", required
                )
                return [r["chat_id"] for r in rows]
        return await self._execute_with_retry(_run)

    # ---------------------------------------------------------- user languages --

    async def set_user_language(self, telegram_id: int, language: str) -> None:
        """Set user's language preference."""
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                await conn.execute(
                    """
                    INSERT INTO user_languages (telegram_id, language)
                    VALUES ($1, $2)
                    ON CONFLICT (telegram_id) DO UPDATE
                    SET language = EXCLUDED.language, updated_at = NOW();
                    """,
                    telegram_id, language
                )
        return await self._execute_with_retry(_run)

    async def get_user_language_pref(self, telegram_id: int) -> str:
        """Get user's preferred language."""
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                row = await conn.fetchrow(
                    "SELECT language FROM user_languages WHERE telegram_id = $1;", telegram_id
                )
                return row["language"] if row else "en"
        return await self._execute_with_retry(_run)

    async def export_users(self) -> list[asyncpg.Record]:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                return await conn.fetch("SELECT * FROM users ORDER BY id;")

        return await self._execute_with_retry(_run)

    # ------------------------------------------------------------- templates --

    async def get_template(self, telegram_id: int) -> Optional[str]:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                row = await conn.fetchrow(
                    "SELECT template FROM templates WHERE telegram_id = $1;", telegram_id
                )
                return row["template"] if row else None

        return await self._execute_with_retry(_run)

    async def save_template(self, telegram_id: int, template: str) -> None:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                await conn.execute(
                    """
                    INSERT INTO templates (telegram_id, template, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (telegram_id) DO UPDATE
                    SET template = EXCLUDED.template,
                        updated_at = NOW();
                    """,
                    telegram_id,
                    template,
                )

        await self._execute_with_retry(_run)

    async def delete_template(self, telegram_id: int) -> bool:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                result = await conn.execute(
                    "DELETE FROM templates WHERE telegram_id = $1;", telegram_id
                )
                return result.endswith("1")

        return await self._execute_with_retry(_run)

    async def count_templates(self) -> int:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                return await conn.fetchval("SELECT COUNT(*) FROM templates;")

        return await self._execute_with_retry(_run)

    async def export_templates(self) -> list[asyncpg.Record]:
        async def _run():
            async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                return await conn.fetch("SELECT * FROM templates ORDER BY id;")

        return await self._execute_with_retry(_run)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            logger.info("Database pool closed.")


# ======================================================================================
# IN-MEMORY TEMPLATE CACHE
# ======================================================================================


class TemplateCache:
    """Simple in-RAM cache so we don't hit PostgreSQL for every PDF."""

    def __init__(self) -> None:
        self._store: dict[int, str] = {}
        self._lock = asyncio.Lock()

    async def get(self, telegram_id: int) -> Optional[str]:
        async with self._lock:
            return self._store.get(telegram_id)

    async def set(self, telegram_id: int, template: str) -> None:
        async with self._lock:
            self._store[telegram_id] = template

    async def delete(self, telegram_id: int) -> None:
        async with self._lock:
            self._store.pop(telegram_id, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    async def size(self) -> int:
        async with self._lock:
            return len(self._store)


# ======================================================================================
# SHIPMENT DATA MODEL
# ======================================================================================


@dataclass
class ShipmentData:
    load_number: str = ""
    pickup_date: str = ""
    delivery_date: str = ""
    pickup_time: str = ""
    delivery_time: str = ""
    pickup_address: str = ""
    delivery_address: str = ""
    weight: str = ""
    equipment_type: str = ""
    reference_numbers: str = ""
    customer: str = ""
    broker: str = ""
    extra_notes: str = ""
    miles_text: str = ""
    missing_fields: list[str] = field(default_factory=list)

    def as_prompt_block(self) -> str:
        return (
            f"Load Number: {self.load_number}\n"
            f"Pickup Date: {self.pickup_date}\n"
            f"Pickup Time: {self.pickup_time}\n"
            f"Delivery Date: {self.delivery_date}\n"
            f"Delivery Time: {self.delivery_time}\n"
            f"Pickup Address: {self.pickup_address}\n"
            f"Delivery Address: {self.delivery_address}\n"
            f"Weight: {self.weight}\n"
            f"Equipment Type: {self.equipment_type}\n"
            f"Reference Numbers: {self.reference_numbers}\n"
            f"Customer: {self.customer}\n"
            f"Broker: {self.broker}\n"
            f"Miles: {self.miles_text}\n"
            f"Additional shipment info: {self.extra_notes}"
        )


# ======================================================================================
# PDF TEXT + REGEX EXTRACTION (FALLBACK ONLY)
# ======================================================================================
# NOTE: This regex-based extractor is NOT the primary extraction method. The
# primary method is Gemini's native PDF document understanding, defined below
# in `extract_shipment_via_gemini_pdf()`. The functions in this section are
# only used as a fallback if that Gemini call fails (bad JSON, timeout,
# repeated API error, etc.), so the bot can still produce a best-effort result
# instead of crashing or giving up entirely.

FIELD_PATTERNS: dict[str, list[str]] = {
    "load_number": [
        r"load\s*#?\s*(?:number|no\.?)?\s*[:\-]?\s*([A-Za-z0-9\-]+)",
        r"order\s*#?\s*[:\-]?\s*([A-Za-z0-9\-]+)",
        r"shipment\s*#?\s*[:\-]?\s*([A-Za-z0-9\-]+)",
    ],
    "pickup_date": [
        r"pick\s*-?\s*up\s*date\s*[:\-]?\s*([0-9/\-\.]+(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)?)",
        r"pu\s*date\s*[:\-]?\s*([0-9/\-\.]+)",
        r"ship\s*date\s*[:\-]?\s*([0-9/\-\.]+)",
    ],
    "delivery_date": [
        r"delivery\s*date\s*[:\-]?\s*([0-9/\-\.]+(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)?)",
        r"del\s*date\s*[:\-]?\s*([0-9/\-\.]+)",
        r"drop\s*(?:off)?\s*date\s*[:\-]?\s*([0-9/\-\.]+)",
    ],
    "weight": [
        r"weight\s*[:\-]?\s*([0-9,\.]+)\s*(?:lbs?|pounds)?",
        r"gross\s*weight\s*[:\-]?\s*([0-9,\.]+)",
    ],
    "equipment_type": [
        r"equipment\s*(?:type)?\s*[:\-]?\s*([A-Za-z0-9 \-\/]+?)(?:\n|$)",
        r"trailer\s*type\s*[:\-]?\s*([A-Za-z0-9 \-\/]+?)(?:\n|$)",
    ],
    "reference_numbers": [
        r"ref(?:erence)?\s*#?\s*(?:number)?s?\s*[:\-]?\s*([A-Za-z0-9\-,\s]+?)(?:\n|$)",
        r"po\s*#?\s*[:\-]?\s*([A-Za-z0-9\-,\s]+?)(?:\n|$)",
    ],
    "customer": [
        r"customer\s*(?:name)?\s*[:\-]?\s*([A-Za-z0-9 &.,\-]+?)(?:\n|$)",
        r"bill\s*to\s*[:\-]?\s*([A-Za-z0-9 &.,\-]+?)(?:\n|$)",
        r"broker\s*[:\-]?\s*([A-Za-z0-9 &.,\-]+?)(?:\n|$)",
    ],
}

ADDRESS_LABELS = {
    "pickup_address": [
        r"pick\s*-?\s*up\s*(?:address|location)?\s*[:\-]?\s*(.+)",
        r"origin\s*[:\-]?\s*(.+)",
        r"shipper\s*(?:address)?\s*[:\-]?\s*(.+)",
    ],
    "delivery_address": [
        r"delivery\s*(?:address|location)?\s*[:\-]?\s*(.+)",
        r"destination\s*[:\-]?\s*(.+)",
        r"consignee\s*(?:address)?\s*[:\-]?\s*(.+)",
        r"drop\s*(?:off)?\s*(?:address|location)?\s*[:\-]?\s*(.+)",
    ],
}

INVALID_ADDRESS_KEYWORDS = {
    "date", "time", "address", "city", "state", "zip", "unknown", "null", "n/a", "empty", 
    "not provided", "tbd", "pending", "to be", "none", "—", "–", "-", "..."
}


def extract_text_pymupdf(pdf_bytes: bytes) -> str:
    text_parts: list[str] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            text_parts.append(page.get_text("text"))
    return "\n".join(text_parts).strip()


def extract_text_pdfplumber(pdf_bytes: bytes) -> str:
    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    return "\n".join(text_parts).strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text with PyMuPDF first; fall back to pdfplumber. Never raises
    for extraction quality reasons -- only if both engines truly fail."""
    text = ""
    try:
        text = extract_text_pymupdf(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("PyMuPDF extraction failed: %s", exc)

    if not text or len(text) < 20:
        try:
            fallback_text = extract_text_pdfplumber(pdf_bytes)
            if len(fallback_text) > len(text):
                text = fallback_text
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdfplumber extraction failed: %s", exc)

    if not text:
        raise ValueError("Both PyMuPDF and pdfplumber failed to extract any text from this PDF.")

    return text


def _search_first(patterns: list[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip(" \t:-")
            if value:
                return value
    return ""


def format_weight(raw_weight: str) -> str:
    """Convert a raw weight string like '39847' or '39,847 lbs' into '39k lbs' (always floor).
    Returns empty string if parsing fails."""
    if not raw_weight:
        return ""
    digits = re.sub(r"[^\d.]", "", raw_weight)
    if not digits:
        return ""
    try:
        value = float(digits)
    except ValueError:
        return ""
    # Always floor the thousands value
    thousands = int(value // 1000)
    return f"{thousands}k lbs" if thousands > 0 else ""


def is_valid_address(address: str) -> bool:
    """Check if an address looks real (not a placeholder like 'DATE', 'ADDRESS', etc.)."""
    if not address or len(address.strip()) < 5:
        return False
    normalized = address.strip().upper()
    # Check if address is just one of the invalid keywords
    for keyword in INVALID_ADDRESS_KEYWORDS:
        if normalized == keyword.upper():
            return False
    # Reject addresses that are only keywords/short phrases
    words = normalized.split()
    if len(words) == 1 and words[0] in {kw.upper() for kw in INVALID_ADDRESS_KEYWORDS}:
        return False
    # Real addresses typically have numbers (ZIP, street number) or multiple meaningful words
    if not any(c.isdigit() for c in address):
        # Allow word-only addresses only if they're long enough (multiple words)
        if len(words) < 3:
            return False
    return True


def _extract_section(text: str, section_keyword: str, next_section_keyword: str = "") -> str:
    """Extract a specific section from PDF text based on keywords.
    
    Returns text between section_keyword and next_section_keyword (or end of text).
    Used to isolate Pickup vs Delivery sections.
    """
    text_upper = text.upper()
    start_idx = text_upper.find(section_keyword.upper())
    if start_idx == -1:
        return ""
    
    # Start search after the keyword
    start_idx += len(section_keyword)
    
    # Find the next section
    if next_section_keyword:
        end_idx = text_upper.find(next_section_keyword.upper(), start_idx)
        if end_idx == -1:
            end_idx = len(text)
    else:
        end_idx = len(text)
    
    return text[start_idx:end_idx]


def _extract_time_from_section(section_text: str, time_patterns: list[str]) -> str:
    """Extract time from a section text using provided patterns.
    
    Filters out unrelated time types (ETA, Created, Tendered, Dispatch, Appointment Created, etc).
    Prefers Appointment Time over other times.
    """
    if not section_text:
        return ""
    
    # Keywords to AVOID (unrelated times)
    unrelated_keywords = {
        "eta", "created", "tendered", "dispatch", "appointment created",
        "appointment confirmation", "appointment booking", "confirmed", "updated",
        "modified", "received", "submitted"
    }
    
    # Check if this section contains only unrelated time info
    section_lower = section_text.lower()
    
    # First, look for Appointment Time specifically (preferred)
    appointment_patterns = [
        r"appointment\s*time\s*[:\-]?\s*(\d{1,2}:\d{2}\s*(?:AM|PM)?|\d{1,2}:\d{2})",
        r"appt\s*time\s*[:\-]?\s*(\d{1,2}:\d{2}\s*(?:AM|PM)?|\d{1,2}:\d{2})",
    ]
    for pattern in appointment_patterns:
        match = re.search(pattern, section_text, re.IGNORECASE)
        if match:
            logger.info("[Time] Found Appointment Time: %s", match.group(1))
            return match.group(1).strip()
    
    # Then look for scheduled time patterns
    for pattern in time_patterns:
        match = re.search(pattern, section_text, re.IGNORECASE)
        if match:
            time_str = match.group(1).strip()
            
            # Verify this time is not preceded by an unrelated keyword
            match_start = match.start()
            before_text = section_text[max(0, match_start - 100):match_start].lower()
            
            is_unrelated = False
            for keyword in unrelated_keywords:
                if keyword in before_text:
                    # Check if it's truly an unrelated context (not just coincidence)
                    if re.search(rf"\b{keyword}\s*[\w\s]*(?:time|hour)", before_text):
                        is_unrelated = True
                        break
            
            if not is_unrelated:
                logger.info("[Time] Found scheduled time: %s", time_str)
                return time_str
    
    return ""


# Time patterns for extraction (without leading keywords that vary by section)
_TIME_ONLY_PATTERNS = [
    r"(\d{1,2}:\d{2}\s*(?:AM|PM))",  # HH:MM AM/PM with explicit spacing
    r"(\d{1,2}:\d{2}(?:\s*-\s*|\s+to\s+)\d{1,2}:\d{2}\s*(?:AM|PM))",  # Time range with AM/PM
    r"(\d{1,2}:\d{2}(?:\s*-\s*|\s+to\s+)\d{1,2}:\d{2})",  # Time range without AM/PM
    r"(\d{1,2}:\d{2})",  # HH:MM only
]


def parse_shipment_data(text: str) -> ShipmentData:
    data = ShipmentData()
    missing: list[str] = []

    data.load_number = _search_first(FIELD_PATTERNS["load_number"], text)
    data.pickup_date = _search_first(FIELD_PATTERNS["pickup_date"], text)
    data.delivery_date = _search_first(FIELD_PATTERNS["delivery_date"], text)
    raw_weight = _search_first(FIELD_PATTERNS["weight"], text)
    data.weight = format_weight(raw_weight)
    data.equipment_type = _search_first(FIELD_PATTERNS["equipment_type"], text)
    data.reference_numbers = _search_first(FIELD_PATTERNS["reference_numbers"], text)
    data.customer = _search_first(FIELD_PATTERNS["customer"], text)
    data.pickup_address = _search_first(ADDRESS_LABELS["pickup_address"], text)
    data.delivery_address = _search_first(ADDRESS_LABELS["delivery_address"], text)
    
    # Context-aware time extraction: only extract times from their respective sections
    # Pickup time from Pickup section only
    pickup_section = _extract_section(text, "PICK", "DELIVERY")
    data.pickup_time = _extract_time_from_section(pickup_section, _TIME_ONLY_PATTERNS)
    
    # Delivery time from Delivery section only
    delivery_section = _extract_section(text, "DELIVERY", "")
    data.delivery_time = _extract_time_from_section(delivery_section, _TIME_ONLY_PATTERNS)

    for field_name in (
        "load_number",
        "pickup_date",
        "delivery_date",
        "pickup_address",
        "delivery_address",
        "weight",
    ):
        if not getattr(data, field_name):
            missing.append(field_name)

    data.missing_fields = missing
    # Keep the first ~600 characters of raw text as extra shipment context for Gemini.
    data.extra_notes = text[:600].replace("\n", " ").strip()
    return data


# ======================================================================================
# MILES CALCULATION — FREE OpenStreetMap stack (Nominatim geocoding + OSRM routing)
# ======================================================================================
#
# Both services are free and require NO API key, but both have strict usage
# policies that WILL silently reject/block requests if not respected:
#   - Nominatim: https://operations.osmfoundation.org/policies/nominatim/
#     -> max 1 request per second, and a real, identifying User-Agent header
#        is mandatory (requests without one, or with a generic library
#        User-Agent, get rejected).
#   - OSRM public demo server: no hard published limit, but also expects a
#     reasonable, non-bursty request rate.
#
# The rate limiter below enforces a minimum 1.1s gap between *every*
# Nominatim call across the whole bot (not per-user), which is what the
# previous version was missing -- if two users sent a PDF within the same
# second, the second geocode call could get silently blocked.

NOMINATIM_GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
OSRM_ROUTING_URL = "https://router.project-osrm.org/route/v1/driving"

# Nominatim requires a descriptive User-Agent identifying the application
# (and ideally a contact point) -- generic/default User-Agents get blocked.
USER_AGENT = "LoadConfirmationBot/1.0 (+https://t.me/load_bot; contact: admin)"

# In-memory cache for geocoding results (address -> (lon, lat)), so repeat
# addresses (same shipper/consignee used again) never need a new request.
_geocoding_cache: dict[str, tuple[float, float]] = {}

# Global rate limiter for Nominatim: guarantees >=1.1s between calls no
# matter how many users are being processed concurrently.
_NOMINATIM_MIN_INTERVAL = 1.1
_nominatim_lock = asyncio.Lock()
_nominatim_last_call: float = 0.0


class DistanceCalculationError(Exception):
    pass


def _clean_address(address: str) -> str:
    """Clean address by removing extra whitespace and normalizing."""
    if not address:
        return ""
    cleaned = " ".join(address.split())
    cleaned = cleaned.rstrip(".,;:")
    return cleaned


def _simplify_address_for_attempt(address: str, attempt: int) -> str:
    """Build the query string to send for a given attempt number.

    Attempt 1: send the address as-is (cleaned) -- Nominatim is often able
        to resolve a full "Business Name, Street, City, State ZIP" string on
        its own.
    Attempt 2: if that failed, drop a likely leading business/facility-name
        segment (a comma-separated segment that doesn't start with a house
        number, e.g. "Isoflex Packaging Pampano Beach FL") and keep the rest
        (the real street/city/state/zip).
    Attempt 3: fall back further to just the city/state/zip portion (last
        two comma-separated segments), which is enough for a usable,
        if slightly less precise, mileage estimate.
    """
    cleaned = _clean_address(address)
    if attempt <= 1:
        return cleaned

    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if attempt == 2:
        if len(parts) > 1 and not re.match(r"^\d", parts[0]):
            parts = parts[1:]
        return ", ".join(parts)

    # attempt >= 3: keep only the last 2 segments (city, state+zip)
    if len(parts) > 2:
        parts = parts[-2:]
    return ", ".join(parts)


async def _rate_limited_get(
    session: aiohttp.ClientSession, url: str, **kwargs: Any
):
    """GET request that enforces the global Nominatim rate limit
    (>=1.1s between requests) before firing."""
    global _nominatim_last_call
    async with _nominatim_lock:
        now = asyncio.get_event_loop().time()
        wait = _NOMINATIM_MIN_INTERVAL - (now - _nominatim_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _nominatim_last_call = asyncio.get_event_loop().time()
    return session.get(url, **kwargs)


async def _geocode_address_nominatim(
    session: aiohttp.ClientSession, address: str, attempt: int = 1
) -> tuple[float, float]:
    """Geocode an address using the free Nominatim service, respecting its
    usage policy (rate limit + identifying User-Agent)."""
    if not address or len(address.strip()) < 3:
        raise DistanceCalculationError(f"Address too short to geocode: '{address}'")

    query = _simplify_address_for_attempt(address, attempt)
    if not query:
        raise DistanceCalculationError(f"Address became empty after cleaning: '{address}'")

    cache_key = query.lower()
    if cache_key in _geocoding_cache:
        logger.info("[Nominatim] Cache hit for query: %s", query)
        return _geocoding_cache[cache_key]

    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": USER_AGENT}
    try:
        async with await _rate_limited_get(
            session, NOMINATIM_GEOCODE_URL, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                raise DistanceCalculationError(
                    f"Nominatim geocoding failed with status {resp.status} for query '{query}'."
                )
            payload = await resp.json()
            if not payload:
                raise DistanceCalculationError(f"No geocoding result for query: '{query}'.")

            result = payload[0]
            lon = float(result["lon"])
            lat = float(result["lat"])

            _geocoding_cache[cache_key] = (lon, lat)
            logger.info("[Nominatim] Geocoded '%s' -> (%.4f, %.4f)", query, lon, lat)
            return lon, lat
    except asyncio.TimeoutError:
        raise DistanceCalculationError(f"Nominatim request timeout for query: '{query}'")
    except (KeyError, ValueError) as exc:
        raise DistanceCalculationError(f"Invalid Nominatim response for query '{query}': {exc}")


async def _get_driving_distance_meters_osrm(
    session: aiohttp.ClientSession, start: tuple[float, float], end: tuple[float, float]
) -> float:
    """Get driving distance using the free OSRM public routing server,
    picking the shortest of any alternative routes it returns."""
    coords = f"{start[0]},{start[1]};{end[0]},{end[1]}"
    url = f"{OSRM_ROUTING_URL}/{coords}"
    params = {"overview": "false", "alternatives": "true"}
    headers = {"User-Agent": USER_AGENT}

    try:
        async with session.get(
            url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status != 200:
                raise DistanceCalculationError(f"OSRM request failed with status {resp.status}.")
            payload = await resp.json()

            if payload.get("code") != "Ok":
                raise DistanceCalculationError(f"OSRM error: {payload.get('code', 'unknown')}")

            routes = payload.get("routes") or []
            if not routes:
                raise DistanceCalculationError("No route found between the two addresses.")

            shortest_route = min(routes, key=lambda r: r.get("distance", float("inf")))
            distance_meters = shortest_route.get("distance", 0)
            logger.info(
                "[OSRM] Selected shortest route: %.2f meters out of %d candidate route(s)",
                distance_meters, len(routes),
            )
            return float(distance_meters)
    except asyncio.TimeoutError:
        raise DistanceCalculationError("OSRM request timeout")


async def calculate_miles(pickup_address: str, delivery_address: str) -> Optional[int]:
    """Calculate driving distance in miles using free Nominatim (geocoding)
    + OSRM (routing), with up to 3 attempts. From the 2nd attempt onward the
    address is simplified (dropping a likely business-name prefix, then
    falling back to city/state/zip) in case that's what's preventing a match.
    """
    if not pickup_address or not delivery_address:
        logger.warning("[Miles] Missing addresses for calculation")
        return None

    start_time = datetime.now(timezone.utc)
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("[Miles] Attempt %d/%d to calculate distance", attempt, max_attempts)
            async with aiohttp.ClientSession() as session:
                try:
                    pickup_lon, pickup_lat = await _geocode_address_nominatim(
                        session, pickup_address, attempt=attempt
                    )
                    logger.info("[Miles] Pickup coordinates: (%.4f, %.4f)", pickup_lon, pickup_lat)
                except DistanceCalculationError as exc:
                    if attempt < max_attempts:
                        logger.warning("[Miles] Geocoding pickup failed (attempt %d): %s, retrying...", attempt, exc)
                        await asyncio.sleep(0.5)
                        continue
                    raise

                try:
                    delivery_lon, delivery_lat = await _geocode_address_nominatim(
                        session, delivery_address, attempt=attempt
                    )
                    logger.info("[Miles] Delivery coordinates: (%.4f, %.4f)", delivery_lon, delivery_lat)
                except DistanceCalculationError as exc:
                    if attempt < max_attempts:
                        logger.warning("[Miles] Geocoding delivery failed (attempt %d): %s, retrying...", attempt, exc)
                        await asyncio.sleep(0.5)
                        continue
                    raise

                try:
                    distance_meters = await _get_driving_distance_meters_osrm(
                        session, (pickup_lon, pickup_lat), (delivery_lon, delivery_lat)
                    )
                except DistanceCalculationError as exc:
                    if attempt < max_attempts:
                        logger.warning("[Miles] Routing failed (attempt %d): %s, retrying...", attempt, exc)
                        await asyncio.sleep(0.5)
                        continue
                    raise

                miles = int(round(distance_meters / 1609.344))
                elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                logger.info(
                    "[Miles] Successfully calculated distance: %d miles (%.2f seconds, %.0f meters)",
                    miles, elapsed, distance_meters,
                )
                return miles

        except DistanceCalculationError as exc:
            logger.warning("[Miles] Attempt %d failed: %s", attempt, exc)
            if attempt < max_attempts:
                await asyncio.sleep(0.5)

    logger.error("[Miles] Failed to calculate distance after %d attempts", max_attempts)
    return None


def format_miles_line(miles: int) -> str:
    """DH is always '-' per specification."""
    return f"Miles :{miles} + - dh"


# ======================================================================================
# GEMINI — LOAD CONFIRMATION GENERATION
# ======================================================================================

GEMINI_CLIENT: Optional[genai.Client] = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Model name is configurable via GEMINI_MODEL env var, defaults to gemini-3.5-flash.
# Ensure it's a currently supported Flash model with native PDF understanding.
GEMINI_MODEL_NAME = GEMINI_MODEL


class GeminiExtractionError(Exception):
    """Raised when Gemini's native PDF understanding fails to produce usable
    JSON. Triggers the regex-based fallback extractor (see PDF TEXT + REGEX
    EXTRACTION section above)."""


PDF_EXTRACTION_PROMPT = (
    "Read this Load Confirmation PDF carefully.\n"
    "Extract every important shipment detail, including pickup and delivery TIMES.\n"
    "\n"
    "Return ONLY valid JSON. No markdown. No code fences. No explanation. No comments.\n"
    "Return this exact structure:\n"
    "{\n"
    '  "load_number": "",\n'
    '  "pickup_date": "",\n'
    '  "pickup_time": "",\n'
    '  "delivery_date": "",\n'
    '  "delivery_time": "",\n'
    '  "pickup_address": "",\n'
    '  "delivery_address": "",\n'
    '  "weight": "",\n'
    '  "equipment": "",\n'
    '  "reference_numbers": [],\n'
    '  "customer": "",\n'
    '  "broker": "",\n'
    '  "notes": ""\n'
    "}\n"
    "\n"
    "Time extraction CRITICAL RULES:\n"
    "- pickup_time MUST ONLY come from the Pickup section. NEVER from Delivery section.\n"
    "- delivery_time MUST ONLY come from the Delivery section. NEVER from Pickup section.\n"
    "- NEVER use: ETA, Created Time, Tendered Time, Appointment Created, Dispatch Time, or other unrelated times.\n"
    "- PREFER: Appointment Time (if available).\n"
    "- OTHERWISE: Use the scheduled Pickup/Delivery time.\n"
    "- Support all common formats: 06:00, 15:00, 6:00 AM, 3:00 PM, 06:00-15:00, 6:00 AM - 3:00 PM, etc.\n"
    "- PRESERVE: AM/PM exactly as shown in the PDF.\n"
    "- PRESERVE: Time ranges exactly (e.g. 08:00 AM - 05:00 PM).\n"
    "- If only one time exists, extract only that one.\n"
    "- If no time is found, leave empty string \"\".\n"
    "\n"
    "JSON Rules:\n"
    "- Return ONLY JSON.\n"
    "- Never wrap JSON in markdown or code fences.\n"
    "- Never add explanations before or after JSON.\n"
    "- Never guess values. If a field is missing, use empty string \"\" or empty array [].\n"
    "- The JSON must be valid and parseable by Python json.loads().\n"
    "- Do not include any text outside the JSON object."
)


def _strip_json_fences(raw: str) -> str:
    """Remove ``` / ```json code fences and markdown."""
    cleaned = raw.strip()
    # Remove markdown code fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()
    # Extract first JSON object if there's extra text
    brace_start = cleaned.find('{')
    if brace_start > 0:
        cleaned = cleaned[brace_start:]
    brace_end = cleaned.rfind('}')
    if brace_end >= 0:
        cleaned = cleaned[:brace_end + 1]
    return cleaned.strip()


def _try_parse_json(raw: str) -> Optional[dict]:
    """Try to parse JSON, returning None if it fails."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _repair_json_via_gemini(gemini_client: genai.Client, broken_json: str) -> Optional[dict]:
    """Ask Gemini to repair invalid JSON. Returns parsed dict or None if repair fails.
    
    Note: JSON repair is a best-effort attempt. If it fails for any reason,
    we silently return None and let the caller decide the next action.
    """
    repair_prompt = (
        "You returned invalid JSON. Fix it and return ONLY valid JSON.\n"
        "No markdown.\nNo explanation.\nNo comments.\nOnly one valid JSON object.\n\n"
        f"Broken JSON:\n{broken_json}"
    )
    try:
        response = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=repair_prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            ),
            timeout=30,
        )
        if response and getattr(response, "text", None):
            repaired_text = _strip_json_fences(response.text)
            logger.info("[Gemini] Attempted JSON repair")
            return _try_parse_json(repaired_text)
    except asyncio.TimeoutError:
        logger.warning("[Gemini] JSON repair timed out after 30 seconds")
    except Exception as exc:  # noqa: BLE001
        # Catch all exceptions including network errors from google-genai
        logger.warning("[Gemini] JSON repair failed (will skip repair): %s", type(exc).__name__)
    return None


def _shipment_from_gemini_json(payload: dict) -> ShipmentData:
    """Map the JSON object returned by Gemini's PDF understanding call onto
    our ShipmentData model. Weight is reformatted into 'NNk lbs' here; miles
    are filled in separately once OpenRouteService has been queried."""
    data = ShipmentData()
    data.load_number = str(payload.get("load_number") or "").strip()
    data.pickup_date = str(payload.get("pickup_date") or "").strip()
    data.pickup_time = str(payload.get("pickup_time") or "").strip()
    data.delivery_date = str(payload.get("delivery_date") or "").strip()
    data.delivery_time = str(payload.get("delivery_time") or "").strip()
    data.pickup_address = str(payload.get("pickup_address") or "").strip()
    data.delivery_address = str(payload.get("delivery_address") or "").strip()

    raw_weight = payload.get("weight") or ""
    data.weight = format_weight(str(raw_weight)) if raw_weight else ""

    data.equipment_type = str(payload.get("equipment") or "").strip()

    ref_numbers = payload.get("reference_numbers") or []
    if isinstance(ref_numbers, list):
        data.reference_numbers = ", ".join(str(x).strip() for x in ref_numbers if str(x).strip())
    else:
        data.reference_numbers = str(ref_numbers).strip()

    data.customer = str(payload.get("customer") or "").strip()
    data.broker = str(payload.get("broker") or "").strip()
    data.extra_notes = str(payload.get("notes") or "").strip()

    data.missing_fields = [
        name
        for name in (
            "load_number",
            "pickup_date",
            "delivery_date",
            "pickup_address",
            "delivery_address",
            "weight",
        )
        if not getattr(data, name)
    ]
    return data


def _is_quota_exhausted(exc: Exception) -> bool:
    """True if this is a Gemini 429 RESOURCE_EXHAUSTED quota error.

    These happen when the API key's daily/per-minute request quota (e.g. the
    free tier's 20 requests/day limit for a model) has been used up.
    Retrying with exponential backoff is pointless here: the quota won't
    reset in the ~60s the backoff schedule covers, so we should fail fast
    and let the caller fall back to regex extraction immediately instead of
    burning a full minute of retries on every single PDF.
    """
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", "") or ""
    message = str(exc)
    return (
        code == 429
        or "RESOURCE_EXHAUSTED" in status
        or "RESOURCE_EXHAUSTED" in message
        or "exceeded your current quota" in message
    )


async def extract_shipment_via_gemini_pdf(pdf_bytes: bytes) -> ShipmentData:
    """PRIMARY shipment-data extraction method with exponential backoff retry.

    Sends the original PDF bytes directly to Gemini using its native
    document/PDF input capability (no OCR, no regex) via the official
    `google-genai` SDK, and asks it to return a strict JSON object describing
    the shipment. Implements exponential backoff (2, 4, 8, 16, 32 seconds)
    and JSON repair attempts. Regular expressions are used ONLY as a fallback 
    if this ultimately fails.
    """
    if GEMINI_CLIENT is None:
        raise GeminiExtractionError("Gemini client is not configured (missing GEMINI_API_KEY).")

    pdf_part = genai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")

    # Exponential backoff: 2, 4, 8, 16, 32 seconds (5 attempts)
    RETRY_DELAYS = [2, 4, 8, 16, 32]
    last_error: Optional[Exception] = None

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            logger.info("[Gemini] Extraction attempt %d/6", attempt + 1)
            response = await asyncio.wait_for(
                GEMINI_CLIENT.aio.models.generate_content(
                    model=GEMINI_MODEL_NAME,
                    contents=[pdf_part, PDF_EXTRACTION_PROMPT],
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0,
                    ),
                ),
                timeout=60,
            )
            if not response or not getattr(response, "text", None):
                raise GeminiExtractionError("Empty response from Gemini while extracting PDF data.")

            cleaned = _strip_json_fences(response.text)
            payload = _try_parse_json(cleaned)
            
            if payload is None:
                # JSON parsing failed, attempt repair
                logger.warning("[Gemini] JSON parsing failed on attempt %d, attempting repair", attempt + 1)
                payload = await _repair_json_via_gemini(GEMINI_CLIENT, cleaned)
                
            if payload is None:
                raise GeminiExtractionError("Could not parse or repair JSON response")
            if not isinstance(payload, dict):
                raise GeminiExtractionError("Gemini JSON response was not a JSON object.")

            logger.info("[Gemini] Extraction succeeded on attempt %d", attempt + 1)
            return _shipment_from_gemini_json(payload)

        except asyncio.TimeoutError as exc:  # noqa: BLE001
            last_error = exc
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "[Gemini] Extraction timed out on attempt %d, retrying in %d seconds",
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("[Gemini] Extraction timed out after all 6 attempts")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            exc_type = type(exc).__name__
            if _is_quota_exhausted(exc):
                logger.error(
                    "[Gemini] Extraction stopped early on attempt %d: quota exhausted (%s). "
                    "Not retrying — this won't clear within the backoff window. "
                    "Check plan/billing at https://ai.dev/rate-limit",
                    attempt + 1,
                    str(exc)[:150],
                )
                break
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "[Gemini] Extraction failed on attempt %d (%s), retrying in %d seconds",
                    attempt + 1,
                    exc_type,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "[Gemini] Extraction failed after all 6 attempts (%s): %s",
                    exc_type,
                    str(exc)[:100],
                )

    raise GeminiExtractionError(f"Gemini PDF extraction exhausted all retries: {last_error}") from last_error


def build_gemini_prompt(template: str, data: ShipmentData) -> str:
    return (
        "This is the user's Load Confirmation template.\n\n"
        f"{template}\n\n"
        "Below is extracted information from the PDF.\n\n"
        f"{data.as_prompt_block()}\n\n"
        "Replace ONLY the corresponding values in the template above with the "
        "extracted information (Load Number, Pickup Date, Delivery Date, Pickup "
        "Address, Delivery Address, Weight, Miles, and any other matching fields "
        "such as Equipment Type, Reference Numbers or Customer if the template "
        "contains placeholders for them).\n"
        "DO NOT modify, reorder, rewrite, or remove any other part of the "
        "template. Keep all labels, punctuation, line breaks and formatting "
        "exactly identical to the original template.\n"
        "Return plain text only, with no explanations, no markdown, and no code "
        "fences."
    )


class GeminiError(Exception):
    pass


async def generate_load_confirmation(template: str, data: ShipmentData) -> str:
    """Generate Load Confirmation with exponential backoff retry (2, 4, 8, 16, 32 seconds)."""
    if GEMINI_CLIENT is None:
        raise GeminiError("Gemini client is not configured (missing GEMINI_API_KEY).")

    prompt = build_gemini_prompt(template, data)
    RETRY_DELAYS = [2, 4, 8, 16, 32]
    last_error: Optional[Exception] = None

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            logger.info("[Gemini] Generation attempt %d/6", attempt + 1)
            response = await asyncio.wait_for(
                GEMINI_CLIENT.aio.models.generate_content(
                    model=GEMINI_MODEL_NAME,
                    contents=prompt,
                ),
                timeout=45,
            )
            if not response or not getattr(response, "text", None):
                raise GeminiError("Empty response from Gemini.")
            logger.info("[Gemini] Generation succeeded on attempt %d", attempt + 1)
            return response.text.strip()
        except asyncio.TimeoutError as exc:  # noqa: BLE001
            last_error = exc
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "[Gemini] Generation timed out on attempt %d, retrying in %d seconds",
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("[Gemini] Generation timed out after all 6 attempts")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            exc_type = type(exc).__name__
            if _is_quota_exhausted(exc):
                logger.error(
                    "[Gemini] Generation stopped early on attempt %d: quota exhausted (%s). "
                    "Not retrying — this won't clear within the backoff window. "
                    "Check plan/billing at https://ai.dev/rate-limit",
                    attempt + 1,
                    str(exc)[:150],
                )
                break
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "[Gemini] Generation failed on attempt %d (%s), retrying in %d seconds",
                    attempt + 1,
                    exc_type,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "[Gemini] Generation failed after all 6 attempts (%s): %s",
                    exc_type,
                    str(exc)[:100],
                )

    raise GeminiError(f"Gemini generation exhausted all retries: {last_error}") from last_error


# ======================================================================================
# GLOBAL STATE
# ======================================================================================

db = Database(DATABASE_URL)
cache = TemplateCache()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class BotStates(StatesGroup):
    waiting_for_template = State()
    waiting_for_pdf = State()
    admin_waiting_broadcast = State()
    admin_waiting_search = State()
    admin_waiting_delete_template = State()
    admin_waiting_view_template = State()
    admin_waiting_add_sub = State()
    admin_waiting_remove_sub = State()
    choosing_language = State()


# ======================================================================================
# HELPERS
# ======================================================================================


async def get_effective_template(telegram_id: int) -> Optional[str]:
    cached = await cache.get(telegram_id)
    if cached is not None:
        return cached
    template = await db.get_template(telegram_id)
    if template is not None:
        await cache.set(telegram_id, template)
    return template


async def save_effective_template(telegram_id: int, template: str) -> None:
    await db.save_template(telegram_id, template)
    await cache.set(telegram_id, template)


async def remove_effective_template(telegram_id: int) -> None:
    await db.delete_template(telegram_id)
    await cache.delete(telegram_id)


def is_admin(telegram_id: int) -> bool:
    return ADMIN_ID != 0 and telegram_id == ADMIN_ID


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="👥 Total Users", callback_data="admin:total_users"),
            InlineKeyboardButton(text="📄 Templates Count", callback_data="admin:templates_count"),
        ],
        [
            InlineKeyboardButton(text="📅 Today's Users", callback_data="admin:today_users"),
            InlineKeyboardButton(text="📊 DB Stats", callback_data="admin:db_stats"),
        ],
        [
            InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast"),
            InlineKeyboardButton(text="🔎 Search User", callback_data="admin:search"),
        ],
        [
            InlineKeyboardButton(text="👁️ View Template", callback_data="admin:view_template"),
            InlineKeyboardButton(text="🗑️ Delete Template", callback_data="admin:delete_template"),
        ],
        [
            InlineKeyboardButton(text="🧹 Clear Cache", callback_data="admin:clear_cache"),
        ],
        [
            InlineKeyboardButton(text="⬇️ Export Users (CSV)", callback_data="admin:export_users"),
            InlineKeyboardButton(text="⬇️ Export Templates (JSON)", callback_data="admin:export_templates"),
        ],
        [
            InlineKeyboardButton(text="💾 Backup Database (SQL)", callback_data="admin:backup_db"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def safe_send(chat_id: int, text: str, **kwargs: Any) -> bool:
    """Send a message, swallowing errors that shouldn't crash the bot
    (blocked bot, deactivated user, etc.)."""
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramForbiddenError:
        logger.info("User %s has blocked the bot; skipping.", chat_id)
        return False
    except TelegramBadRequest as exc:
        logger.warning("Bad request sending message to %s: %s", chat_id, exc)
        return False
    except TelegramNetworkError as exc:
        logger.warning("Network error sending message to %s, retrying once: %s", chat_id, exc)
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except Exception as exc2:  # noqa: BLE001
            logger.error("Failed to send message to %s after retry: %s", chat_id, exc2)
            return False


# ======================================================================================
# USER COMMAND HANDLERS
# ======================================================================================


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Start command - shows different menu based on user role."""
    await state.clear()
    telegram_id = message.from_user.id
    language = normalize_language(message.from_user.language_code)

    await db.upsert_user(
        telegram_id=telegram_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        language=language,
    )
    
    # Save language preference
    await db.set_user_language(telegram_id, language)

    await message.answer(t(language, "start_welcome"))
    
    # Show role-based commands menu
    is_admin = telegram_id == ADMIN_ID
    commands_text = "📋 <b>Komandalar / Commands / Команды:</b>\n\n"
    commands_text += "/help - " + ("Help (English)" if language == "en" else "Помощь (Русский)" if language == "ru" else "Yordam (Uzbek)") + "\n"
    commands_text += "/lang - " + ("Change language" if language == "en" else "Изменить язык" if language == "ru" else "Tilni o'zgartirish") + "\n"
    commands_text += "/template - " + ("View/Set template" if language == "en" else "Просмотр/Установка шаблона" if language == "ru" else "Shablonni ko'rish/o'rnatish") + "\n"
    commands_text += "/delete - " + ("Delete template" if language == "en" else "Удалить шаблон" if language == "ru" else "Shablonni o'chirish") + "\n"
    
    if is_admin:
        commands_text += "\n🛠️ <b>Admin Commands:</b>\n"
        commands_text += "/add_sub - Add subscription\n"
        commands_text += "/remove_sub - Remove subscription\n"
        commands_text += "/list_subs - List subscriptions\n"
        commands_text += "/broadcast - Broadcast message\n"
        commands_text += "/clearcache - Clear cache\n"
    
    await message.answer(commands_text, parse_mode="HTML")

    template = await get_effective_template(telegram_id)
    if template:
        await message.answer(t(language, "ask_pdf"))
        await state.set_state(BotStates.waiting_for_pdf)
    else:
        await message.answer(t(language, "ask_template"))
        await state.set_state(BotStates.waiting_for_template)


@router.message(Command("lang"))
async def cmd_lang(message: Message, state: FSMContext) -> None:
    """Language selection command."""
    telegram_id = message.from_user.id
    current_lang = await db.get_user_language_pref(telegram_id)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
            ],
            [
                InlineKeyboardButton(text="🇺🇿 Ўзбек", callback_data="lang_uz"),
            ],
        ]
    )
    
    await message.answer(t(current_lang, "choose_language"), reply_markup=keyboard)


@router.callback_query(F.data.startswith("lang_"))
async def handle_lang_select(query: CallbackQuery) -> None:
    """Handle language selection."""
    lang_code = query.data.split("_")[1]
    telegram_id = query.from_user.id
    
    await db.set_user_language(telegram_id, lang_code)
    await query.answer()
    # Map language code to display name
    lang_names = {"en": "English", "ru": "Русский", "uz": "Ўзбек"}
    await query.message.edit_text(t(lang_code, "language_set", lang=lang_names.get(lang_code, lang_code)))


@router.message(Command("add_sub"))
async def cmd_add_sub(message: Message, state: FSMContext) -> None:
    """Admin: add mandatory subscription."""
    if message.from_user.id != ADMIN_ID:
        language = await db.get_user_language(message.from_user.id)
        await message.answer(t(language, "admin_denied"))
        return
    
    language = await db.get_user_language(ADMIN_ID)
    await message.answer(t(language, "add_sub_prompt"))
    await state.set_state(BotStates.admin_waiting_add_sub)


@router.message(BotStates.admin_waiting_add_sub, F.text)
async def handle_add_sub(message: Message, state: FSMContext) -> None:
    """Process subscription addition."""
    language = await db.get_user_language(ADMIN_ID)
    
    try:
        chat_id = int(message.text.strip())
        await db.add_subscription(chat_id, "channel", required=True)
        await message.answer(t(language, "sub_added", chat_id=chat_id))
    except ValueError:
        await message.answer("❌ Invalid chat_id. Please send a number.")
        return
    
    await state.clear()


@router.message(Command("remove_sub"))
async def cmd_remove_sub(message: Message, state: FSMContext) -> None:
    """Admin: remove subscription."""
    if message.from_user.id != ADMIN_ID:
        language = await db.get_user_language(message.from_user.id)
        await message.answer(t(language, "admin_denied"))
        return
    
    language = await db.get_user_language(ADMIN_ID)
    await message.answer(t(language, "remove_sub_prompt"))
    await state.set_state(BotStates.admin_waiting_remove_sub)


@router.message(BotStates.admin_waiting_remove_sub, F.text)
async def handle_remove_sub(message: Message, state: FSMContext) -> None:
    """Process subscription removal."""
    language = await db.get_user_language(ADMIN_ID)
    
    try:
        chat_id = int(message.text.strip())
        await db.remove_subscription(chat_id)
        await message.answer(t(language, "sub_removed", chat_id=chat_id))
    except ValueError:
        await message.answer("❌ Invalid chat_id. Please send a number.")
        return
    
    await state.clear()


@router.message(Command("list_subs"))
async def cmd_list_subs(message: Message) -> None:
    """Admin: list all subscriptions."""
    if message.from_user.id != ADMIN_ID:
        language = await db.get_user_language(message.from_user.id)
        await message.answer(t(language, "admin_denied"))
        return
    
    language = await db.get_user_language(ADMIN_ID)
    subs = await db.get_subscriptions()
    
    if not subs:
        await message.answer("📋 No subscriptions configured.")
        return
    
    subs_text = "\n".join([f"• {s['chat_id']} ({s['chat_type']})" for s in subs])
    await message.answer(t(language, "list_subs", subs=subs_text))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    language = await db.get_user_language(message.from_user.id)
    is_admin = message.from_user.id == ADMIN_ID
    
    help_text = t(language, "help_text")
    if is_admin:
        help_text += "\n\n" + t(language, "admin_menu")
    
    await message.answer(help_text)


@router.message(Command("template"))
async def cmd_template(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    language = await db.get_user_language(telegram_id)
    template = await get_effective_template(telegram_id)
    if template:
        await message.answer(t(language, "template_current", template=template))
        await state.set_state(BotStates.waiting_for_template)
    else:
        await message.answer(t(language, "template_none"))
        await state.set_state(BotStates.waiting_for_template)


@router.message(Command("delete"))
async def cmd_delete(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    language = await db.get_user_language(telegram_id)
    template = await get_effective_template(telegram_id)
    if not template:
        await message.answer(t(language, "template_none_to_delete"))
        return
    await remove_effective_template(telegram_id)
    await message.answer(t(language, "template_deleted"))
    await state.set_state(BotStates.waiting_for_template)


@router.message(Command("clearcache"))
async def cmd_clear_cache(message: Message) -> None:
    if not is_admin(message.from_user.id):
        language = await db.get_user_language(message.from_user.id)
        await message.answer(t(language, "admin_denied"))
        return
    await cache.clear()
    await message.answer(t("en", "cache_cleared"))
    logger.info("Admin %s cleared the cache.", message.from_user.id)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        language = await db.get_user_language(message.from_user.id)
        await message.answer(t(language, "admin_denied"))
        return
    await state.clear()
    await message.answer(t("en", "admin_panel_title"), reply_markup=admin_panel_keyboard())


# ======================================================================================
# TEMPLATE INPUT HANDLER
# ======================================================================================


@router.message(BotStates.waiting_for_template, F.text)
async def handle_template_text(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    language = await db.get_user_language(telegram_id)
    template_text = message.text.strip()

    if not template_text:
        await message.answer(t(language, "ask_template"))
        return

    await save_effective_template(telegram_id, template_text)
    await message.answer(t(language, "template_saved"))
    await state.set_state(BotStates.waiting_for_pdf)


# ======================================================================================
# PDF INPUT HANDLER
# ======================================================================================


@router.message(F.document)
async def handle_document(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    language = await db.get_user_language(telegram_id)
    document: Document = message.document

    file_name = document.file_name or ""
    is_pdf = (document.mime_type == "application/pdf") or file_name.lower().endswith(".pdf")
    if not is_pdf:
        await message.answer(t(language, "not_a_pdf"))
        return

    template = await get_effective_template(telegram_id)
    if not template:
        await message.answer(t(language, "need_template_first"))
        await state.set_state(BotStates.waiting_for_template)
        return

    status_message = await message.answer(t(language, "processing_pdf"))

    pdf_bytes: Optional[bytes] = None
    start_time = datetime.now(timezone.utc)
    try:
        file_info = await bot.get_file(document.file_id)
        file_buffer = await bot.download_file(file_info.file_path)
        pdf_bytes = file_buffer.read()
        logger.info("[PDF] Downloaded PDF for user %s (size: %d bytes)", telegram_id, len(pdf_bytes))

        # --- extract shipment data (never store the PDF on disk) ---
        # PRIMARY: send the PDF directly to Gemini and let it read/understand
        # the document natively, returning structured JSON.
        # FALLBACK: only if Gemini's PDF understanding fails do we fall back
        # to local text extraction (PyMuPDF -> pdfplumber) plus regex parsing.
        await status_message.edit_text("📄 Reading PDF...")
        try:
            shipment = await extract_shipment_via_gemini_pdf(pdf_bytes)
            logger.info("[PDF] Extracted shipment data for user %s via Gemini PDF understanding.", telegram_id)
        except GeminiExtractionError as exc:
            logger.warning(
                "[PDF] Gemini PDF understanding failed for user %s, falling back to regex extraction: %s",
                telegram_id,
                exc,
            )
            try:
                await status_message.edit_text("📦 Extracting shipment...")
                text = extract_pdf_text(pdf_bytes)
                logger.info("[PDF] Extracted text from PDF (%d chars)", len(text))
            except Exception as exc2:  # noqa: BLE001
                logger.error("[PDF] Regex fallback extraction also failed for user %s: %s", telegram_id, exc2)
                await status_message.edit_text(t(language, "pdf_parse_error"))
                return
            shipment = parse_shipment_data(text)
            logger.info("[PDF] Parsed shipment data via regex fallback")

        # --- validate and request missing addresses if needed ---
        await status_message.edit_text("🤖 Understanding PDF...")
        if shipment.pickup_address and not is_valid_address(shipment.pickup_address):
            logger.warning("[PDF] Invalid pickup address extracted: %s", shipment.pickup_address)
            shipment.pickup_address = ""
        if shipment.delivery_address and not is_valid_address(shipment.delivery_address):
            logger.warning("[PDF] Invalid delivery address extracted: %s", shipment.delivery_address)
            shipment.delivery_address = ""

        # --- miles calculation ---
        await status_message.edit_text("🚛 Calculating miles...")
        miles_text = ""
        if shipment.pickup_address and shipment.delivery_address:
            miles = await calculate_miles(shipment.pickup_address, shipment.delivery_address)
            if miles is not None:
                miles_text = format_miles_line(miles)
                logger.info("[Miles] Calculated %d miles for user %s", miles, telegram_id)
            else:
                logger.error("[Miles] Calculation failed for user %s", telegram_id)
                miles_text = "Miles :N/A + - dh"
        else:
            if not shipment.pickup_address:
                shipment.missing_fields.append("pickup_address")
            if not shipment.delivery_address:
                shipment.missing_fields.append("delivery_address")

        shipment.miles_text = miles_text or "Miles :N/A + - dh"

        # --- Gemini generation ---
        await status_message.edit_text("📝 Generating Load Confirmation...")
        try:
            result_text = await generate_load_confirmation(template, shipment)
        except GeminiError as exc:
            logger.error("[Gemini] Generation failed for user %s: %s", telegram_id, exc)
            await status_message.edit_text(t(language, "gemini_error"))
            return

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        await status_message.edit_text(t(language, "result_ready"))
        await message.answer(result_text)

        if shipment.missing_fields:
            await message.answer(t(language, "extracted_missing"))

        logger.info(
            "[PDF] Generated Load Confirmation for user %s (load: %s, time: %.1fs)",
            telegram_id,
            shipment.load_number,
            elapsed,
        )

    except Exception as exc:  # noqa: BLE001
        logger.error("[PDF] Unhandled error processing PDF for user %s: %s\n%s", telegram_id, exc, traceback.format_exc())
        try:
            await status_message.edit_text(t(language, "generic_error"))
        except Exception:  # noqa: BLE001
            await message.answer(t(language, "generic_error"))
    finally:
        # Never store uploaded PDFs -- drop the reference immediately.
        pdf_bytes = None
        logger.info("[PDF] PDF bytes cleared for user %s", telegram_id)


@router.message(BotStates.waiting_for_pdf, F.text)
async def handle_waiting_for_pdf_text(message: Message) -> None:
    language = await db.get_user_language(message.from_user.id)
    await message.answer(t(language, "ask_pdf"))


# ======================================================================================
# ADMIN CALLBACK HANDLERS
# ======================================================================================


@router.callback_query(F.data.startswith("admin:"))
async def handle_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(t("en", "admin_denied"), show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "total_users":
        count = await db.count_users()
        await callback.message.answer(f"👥 Total users: <b>{count}</b>")

    elif action == "templates_count":
        count = await db.count_templates()
        await callback.message.answer(f"📄 Templates saved: <b>{count}</b>")

    elif action == "today_users":
        count = await db.count_users_today()
        await callback.message.answer(f"📅 New users today: <b>{count}</b>")

    elif action == "db_stats":
        users = await db.count_users()
        templates = await db.count_templates()
        cache_size = await cache.size()
        await callback.message.answer(
            "📊 <b>Database Statistics</b>\n\n"
            f"Users: {users}\n"
            f"Templates: {templates}\n"
            f"Cached templates (RAM): {cache_size}"
        )

    elif action == "clear_cache":
        await cache.clear()
        await callback.message.answer(t("en", "cache_cleared"))
        logger.info("Admin %s cleared cache via panel.", callback.from_user.id)

    elif action == "broadcast":
        await state.set_state(BotStates.admin_waiting_broadcast)
        await callback.message.answer(t("en", "broadcast_prompt"))

    elif action == "search":
        await state.set_state(BotStates.admin_waiting_search)
        await callback.message.answer(t("en", "search_prompt"))

    elif action == "view_template":
        await state.set_state(BotStates.admin_waiting_view_template)
        await callback.message.answer(t("en", "view_template_prompt"))

    elif action == "delete_template":
        await state.set_state(BotStates.admin_waiting_delete_template)
        await callback.message.answer(t("en", "delete_template_prompt"))

    elif action == "export_users":
        await export_users_csv(callback)

    elif action == "export_templates":
        await export_templates_json(callback)

    elif action == "backup_db":
        await backup_database_sql(callback)

    else:
        logger.warning("Unknown admin action: %s", action)


@router.message(BotStates.admin_waiting_broadcast, F.text)
async def admin_broadcast(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    text_to_send = message.text
    user_ids = await db.all_user_ids()
    sent, failed = 0, 0
    for uid in user_ids:
        ok = await safe_send(uid, text_to_send)
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # gentle rate limiting
    await message.answer(t("en", "broadcast_done", sent=sent, failed=failed))
    logger.info("Admin %s broadcast message to %s users (%s failed).", message.from_user.id, sent, failed)


@router.message(BotStates.admin_waiting_search, F.text)
async def admin_search(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    query = message.text.strip()
    record = await db.find_user(query)
    if not record:
        await message.answer(t("en", "search_not_found", query=query))
        return
    await message.answer(
        "👤 <b>User found</b>\n\n"
        f"Telegram ID: <code>{record['telegram_id']}</code>\n"
        f"Username: @{record['username'] if record['username'] else 'N/A'}\n"
        f"First name: {record['first_name'] or 'N/A'}\n"
        f"Language: {record['language']}\n"
        f"Joined: {record['joined_at']}\n"
        f"Last active: {record['last_active']}"
    )


@router.message(BotStates.admin_waiting_view_template, F.text)
async def admin_view_template(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    query = message.text.strip()
    if not query.isdigit():
        await message.answer("Please send a numeric Telegram ID.")
        return
    template = await get_effective_template(int(query))
    if not template:
        await message.answer(f"No template found for {query}.")
        return
    await message.answer(f"📋 Template for <code>{query}</code>:\n\n<code>{template}</code>")


@router.message(BotStates.admin_waiting_delete_template, F.text)
async def admin_delete_template(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    query = message.text.strip()
    if not query.isdigit():
        await message.answer("Please send a numeric Telegram ID.")
        return
    telegram_id = int(query)
    deleted = await db.delete_template(telegram_id)
    await cache.delete(telegram_id)
    if deleted:
        await message.answer(t("en", "admin_action_done"))
    else:
        await message.answer(f"No template found for {query}.")


# ======================================================================================
# ADMIN EXPORT / BACKUP FUNCTIONS
# ======================================================================================


async def export_users_csv(callback: CallbackQuery) -> None:
    records = await db.export_users()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "telegram_id", "username", "first_name", "language", "joined_at", "last_active"])
    for r in records:
        writer.writerow(
            [r["id"], r["telegram_id"], r["username"], r["first_name"], r["language"], r["joined_at"], r["last_active"]]
        )
    file_bytes = buffer.getvalue().encode("utf-8")
    document = BufferedInputFile(file_bytes, filename="users_export.csv")
    await callback.message.answer_document(document, caption=f"👥 Users export ({len(records)} rows)")
    logger.info("Admin %s exported users CSV.", callback.from_user.id)


async def export_templates_json(callback: CallbackQuery) -> None:
    records = await db.export_templates()
    data = [
        {
            "telegram_id": r["telegram_id"],
            "template": r["template"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in records
    ]
    file_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    document = BufferedInputFile(file_bytes, filename="templates_export.json")
    await callback.message.answer_document(document, caption=f"📄 Templates export ({len(records)} rows)")
    logger.info("Admin %s exported templates JSON.", callback.from_user.id)


def _sql_escape(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


async def backup_database_sql(callback: CallbackQuery) -> None:
    """Generate a portable SQL backup (schema + data) for the users and
    templates tables without depending on an external pg_dump binary."""
    users = await db.export_users()
    templates = await db.export_templates()

    lines: list[str] = [
        "-- Load Confirmation Bot database backup",
        f"-- Generated at {datetime.now(timezone.utc).isoformat()}",
        "",
        "CREATE TABLE IF NOT EXISTS users (",
        "    id SERIAL PRIMARY KEY,",
        "    telegram_id BIGINT UNIQUE NOT NULL,",
        "    username TEXT,",
        "    first_name TEXT,",
        "    language TEXT DEFAULT 'en',",
        "    joined_at TIMESTAMP DEFAULT NOW(),",
        "    last_active TIMESTAMP DEFAULT NOW()",
        ");",
        "",
        "CREATE TABLE IF NOT EXISTS templates (",
        "    id SERIAL PRIMARY KEY,",
        "    telegram_id BIGINT UNIQUE NOT NULL,",
        "    template TEXT NOT NULL,",
        "    updated_at TIMESTAMP DEFAULT NOW()",
        ");",
        "",
    ]

    for r in users:
        lines.append(
            "INSERT INTO users (id, telegram_id, username, first_name, language, joined_at, last_active) "
            f"VALUES ({r['id']}, {r['telegram_id']}, {_sql_escape(r['username'])}, "
            f"{_sql_escape(r['first_name'])}, {_sql_escape(r['language'])}, "
            f"{_sql_escape(r['joined_at'])}, {_sql_escape(r['last_active'])}) "
            "ON CONFLICT (telegram_id) DO NOTHING;"
        )

    lines.append("")
    for r in templates:
        lines.append(
            "INSERT INTO templates (id, telegram_id, template, updated_at) "
            f"VALUES ({r['id']}, {r['telegram_id']}, {_sql_escape(r['template'])}, "
            f"{_sql_escape(r['updated_at'])}) ON CONFLICT (telegram_id) DO NOTHING;"
        )

    file_bytes = "\n".join(lines).encode("utf-8")
    document = BufferedInputFile(file_bytes, filename="database_backup.sql")
    await callback.message.answer_document(document, caption="💾 Full database backup (SQL)")
    logger.info("Admin %s generated a database backup.", callback.from_user.id)


# ======================================================================================
# GLOBAL ERROR HANDLER
# ======================================================================================


@router.errors()
async def global_error_handler(event) -> bool:  # noqa: ANN001
    exception = event.exception
    logger.error("Unhandled exception in update handler: %s\n%s", exception, traceback.format_exc())
    try:
        update = event.update
        chat_id = None
        if update.message:
            chat_id = update.message.chat.id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id
        if chat_id:
            language = await db.get_user_language(chat_id)
            await safe_send(chat_id, t(language, "generic_error"))
    except Exception:  # noqa: BLE001
        logger.error("Error while handling error: %s", traceback.format_exc())
    return True


# ======================================================================================
# STARTUP / SHUTDOWN
# ======================================================================================


def validate_environment() -> None:
    missing = [name for name, value in REQUIRED_ENV.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing) + ". "
            "Please set them in your .env file (see README.md)."
        )
    if ADMIN_ID == 0:
        logger.warning("ADMIN_ID is not set (or 0) -- the admin panel will be disabled.")


async def on_startup() -> None:
    logger.info("Starting Load Confirmation Bot...")
    await db.connect()
    logger.info("Bot startup complete.")


async def on_shutdown() -> None:
    logger.info("Shutting down Load Confirmation Bot...")
    await db.close()
    await bot.session.close()
    logger.info("Bot shutdown complete.")


async def main() -> None:
    validate_environment()
    await on_startup()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # add_signal_handler is not available on some platforms (e.g. Windows)
            pass

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        polling_task = asyncio.create_task(dp.start_polling(bot))
        await stop_event.wait()
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
    finally:
        await on_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
