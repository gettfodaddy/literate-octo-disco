#!/usr/bin/env python3
"""Private Telegram helper for issuing CDNVideo access links."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    Defaults,
    MessageHandler,
    filters,
)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cdn-token-bot")

CHOOSE_SERVICE, EMAIL, PASSWORD = range(3)
MAX_AUTH_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60
REQUEST_COOLDOWN_SECONDS = 20
CONNECT_RETRIES = 2  # total attempts; only failures before establishing a connection retry
CONNECT_RETRY_DELAY_SECONDS = 2


@dataclass(frozen=True)
class Service:
    label: str
    token_url: str
    login_url: str
    credential_source: str


SERVICES: dict[str, Service] = {
    "beeline": Service(
        label="CDNVideo → Beeline CDN",
        token_url="https://api.cdnvideo.ru/app/oauth/v1/token/",
        login_url="https://cdn.beeline.ru/login",
        credential_source="CDNVideo",
    ),
    "cdnvideo_turboflare": Service(
        label="CDNVideo → Turboflare",
        token_url="https://api.cdnvideo.ru/app/oauth/v1/token/",
        login_url="https://dashboard.turboflare.ru/login",
        credential_source="CDNVideo",
    ),
    "turboflare_beeline": Service(
        label="Turboflare → Beeline CDN",
        token_url="https://dashboard.turboflare.ru/app/oauth/v1/token/",
        login_url="https://cdn.beeline.ru/login",
        credential_source="Turboflare",
    ),
    "turboflare_cdnvideo": Service(
        label="Turboflare → CDNVideo",
        token_url="https://dashboard.turboflare.ru/app/oauth/v1/token/",
        login_url="https://dashboard.cdnvideo.ru/login",
        credential_source="Turboflare",
    ),
    "turboflare": Service(
        label="Turboflare",
        token_url="https://dashboard.turboflare.ru/app/oauth/v1/token/",
        login_url="https://dashboard.turboflare.ru/login",
        credential_source="Turboflare",
    ),
}
SERVICE_CALLBACK_PATTERN = r"^service:(?:" + "|".join(re.escape(key) for key in SERVICES) + r")$"
ALLOWED_USER_IDS: frozenset[int] = frozenset()


def load_config() -> tuple[str, frozenset[int]]:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан.")

    raw_ids = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
    try:
        allowed_ids = frozenset(int(part.strip()) for part in raw_ids.split(",") if part.strip())
    except ValueError as exc:
        raise RuntimeError("ALLOWED_TELEGRAM_USER_IDS должен содержать числовые Telegram user ID через запятую.") from exc

    if not allowed_ids or any(user_id <= 0 for user_id in allowed_ids):
        raise RuntimeError(
            "Доступ закрыт по умолчанию: укажи хотя бы один положительный ID в ALLOWED_TELEGRAM_USER_IDS."
        )
    return bot_token, allowed_ids


def is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == ChatType.PRIVATE)


async def check_access(update: Update, *, notify: bool = True) -> bool:
    if not is_private(update):
        if notify and update.effective_message:
            await update.effective_message.reply_text(
                "🔒 Бот принимает запросы только в личном чате. Открой его профиль и нажми Start."
            )
        return False

    user = update.effective_user
    if user and user.id in ALLOWED_USER_IDS:
        return True

    if notify and update.effective_message:
        await update.effective_message.reply_text("🔒 Доступ к этому боту ограничен.")
    if user:
        log.warning("Rejected access for telegram_user_id=%s", user.id)
    return False


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            *[
                [InlineKeyboardButton(service.label, callback_data=f"service:{key}")]
                for key, service in SERVICES.items()
            ],
            [InlineKeyboardButton("ℹ️  О безопасности", callback_data="about:security")],
        ]
    )


def service_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("↩️  К выбору сервиса", callback_data="back:services")],
            [InlineKeyboardButton("✖️  Отмена", callback_data="cancel")],
        ]
    )


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    context.user_data.clear()

    message = update.effective_message
    if message is None:
        return ConversationHandler.END
    await message.reply_text(
        "<b>CDN ACCESS</b>  <code>PRIVATE CONSOLE</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Создай временную ссылку входа для своего CDN-аккаунта.\n\n"
        "<b>1</b>  Выбери сервис\n"
        "<b>2</b>  Подтверди email и пароль\n"
        "<b>3</b>  Открой ссылку и удали её из чата\n\n"
        "🔐 Только личный чат и заранее разрешённые Telegram ID.",
        reply_markup=home_keyboard(),
    )
    return CHOOSE_SERVICE


async def choose_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()

    key = (query.data or "").removeprefix("service:")
    service = SERVICES.get(key)
    if service is None:
        await query.edit_message_text("⚠️ Неизвестный сервис. Нажми /start и выбери его заново.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["service_key"] = key
    await query.edit_message_text(
        f"<b>{escape(service.label.upper())}</b>  <code>ШАГ 1 ИЗ 3</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Отправь email аккаунта <b>{escape(service.credential_source)}</b>.\n\n"
        f"<i>Email и пароль отправляются в {escape(service.credential_source)}. "
        "Ссылка входа не подтверждает доступность CDN-раздачи в целевом кабинете.</i>",
            reply_markup=service_keyboard(),
    )
    return EMAIL


async def about_security(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "<b>БЕЗОПАСНОСТЬ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• Бот доступен только ID из allowlist и только в личном чате.\n"
        "• Пароль не сохраняется в файлы и не выводится в логи.\n"
        "• После получения бот удаляет сообщение с паролем, если Telegram разрешает это.\n"
        "• Ссылка входа — секрет: не пересылай её и удали сообщение после использования.\n\n"
        "Учти: Telegram обрабатывает сообщения бота и пользователя. Для максимальной приватности вводи пароль напрямую на сайте сервиса.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️  Назад", callback_data="back:services")]]
        ),
    )
    return CHOOSE_SERVICE


async def back_to_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "<b>CDN ACCESS</b>\n━━━━━━━━━━━━━━━━━━\nВыбери сервис:",
        reply_markup=home_keyboard(),
    )
    return CHOOSE_SERVICE


async def got_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        return ConversationHandler.END
    message = update.effective_message
    email = (message.text or "").strip() if message else ""
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        if message:
            await message.reply_text("Проверь адрес и отправь email ещё раз, например <code>you@example.net</code>.")
        return EMAIL

    context.user_data["email"] = email
    if message:
        await message.reply_text(
            "<b>EMAIL ПРИНЯТ</b>  <code>ШАГ 2 ИЗ 3</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Теперь отправь пароль отдельным сообщением.\n\n"
            "⚠️ Бот сразу попробует удалить это сообщение после получения.",
            reply_markup=service_keyboard(),
        )
    return PASSWORD


def cooldown_remaining(context: ContextTypes.DEFAULT_TYPE, user_id: int, now: float) -> int:
    locks: dict[int, float] = context.application.bot_data.setdefault("lockouts", {})
    until = locks.get(user_id, 0.0)
    return max(0, int(until - now + 0.999))


def register_failed_login(context: ContextTypes.DEFAULT_TYPE, user_id: int, now: float) -> int:
    attempts: dict[int, list[float]] = context.application.bot_data.setdefault("auth_failures", {})
    history = [stamp for stamp in attempts.get(user_id, []) if now - stamp < FAILURE_WINDOW_SECONDS]
    history.append(now)
    attempts[user_id] = history
    if len(history) >= MAX_AUTH_FAILURES:
        locks: dict[int, float] = context.application.bot_data.setdefault("lockouts", {})
        locks[user_id] = now + LOCKOUT_SECONDS
        return LOCKOUT_SECONDS
    return 0


def clear_auth_state(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    context.application.bot_data.setdefault("auth_failures", {}).pop(user_id, None)
    context.application.bot_data.setdefault("lockouts", {}).pop(user_id, None)


async def request_token(client: httpx.AsyncClient, url: str, email: str, password: str) -> httpx.Response:
    for attempt in range(CONNECT_RETRIES):
        try:
            return await client.post(
                url,
                data={"username": email, "password": password},
                headers={"Accept": "application/json"},
            )
        except (httpx.ConnectTimeout, httpx.ConnectError):
            if attempt + 1 >= CONNECT_RETRIES:
                raise
            await asyncio.sleep(CONNECT_RETRY_DELAY_SECONDS)
    raise RuntimeError("Неожиданное завершение запроса.")


async def got_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        context.user_data.clear()
        return ConversationHandler.END
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        context.user_data.clear()
        return ConversationHandler.END

    password = message.text or ""
    email = context.user_data.pop("email", "")
    service_key = context.user_data.pop("service_key", "")
    service = SERVICES.get(service_key)

    # Delete the credential message before making any outbound request.
    try:
        await message.delete()
    except Exception as exc:  # Telegram may reject deletion because of permissions or age.
        log.info("Password message deletion unavailable (%s)", type(exc).__name__)

    if service is None or not email or not password or len(password) > 1024:
        await chat.send_message("⚠️ Не удалось продолжить. Нажми /start и начни заново.")
        context.user_data.clear()
        return ConversationHandler.END

    now = time.monotonic()
    lock_seconds = cooldown_remaining(context, user.id, now)
    if lock_seconds:
        await chat.send_message(
            f"⏳ Попытки временно приостановлены. Попробуй снова через <b>{lock_seconds} сек.</b>"
        )
        context.user_data.clear()
        return ConversationHandler.END

    last_requests: dict[int, float] = context.application.bot_data.setdefault("last_requests", {})
    wait_seconds = int(REQUEST_COOLDOWN_SECONDS - (now - last_requests.get(user.id, 0.0)))
    if wait_seconds > 0:
        await chat.send_message(f"⏳ Подожди {wait_seconds} сек. перед следующим запросом.")
        context.user_data.clear()
        return ConversationHandler.END
    last_requests[user.id] = now

    status = await chat.send_message(
        f"<b>{escape(service.label.upper())}</b>  <code>ШАГ 3 ИЗ 3</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ Запрашиваю временную ссылку…"
    )
    response: httpx.Response | None = None
    try:
        timeout = httpx.Timeout(connect=8.0, read=15.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await request_token(client, service.token_url, email, password)
    except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
        log.warning("Token endpoint connection failed for %s (%s)", service.label, type(exc).__name__)
        await status.edit_text(
            f"<b>{escape(service.label.upper())}</b>\n━━━━━━━━━━━━━━━━━━\n"
            "❌ Не удалось подключиться к сервису. Проверь сеть и повтори позже."
        )
        context.user_data.clear()
        return ConversationHandler.END
    except httpx.TimeoutException as exc:
        # Do not retry: the provider may have already processed the POST.
        log.warning("Token endpoint timed out for %s (%s)", service.label, type(exc).__name__)
        await status.edit_text(
            f"<b>{escape(service.label.upper())}</b>\n━━━━━━━━━━━━━━━━━━\n"
            "⌛ Сервис не успел ответить. Подожди немного и проверь, не появился ли вход."
        )
        context.user_data.clear()
        return ConversationHandler.END
    except httpx.RequestError as exc:
        log.warning("Token endpoint request failed for %s (%s)", service.label, type(exc).__name__)
        await status.edit_text(
            f"<b>{escape(service.label.upper())}</b>\n━━━━━━━━━━━━━━━━━━\n"
            "❌ Запрос не завершился. Повтори позже или проверь состояние сервиса."
        )
        context.user_data.clear()
        return ConversationHandler.END
    finally:
        # Drop local references as soon as the request finishes; Python cannot guarantee memory erasure.
        password = ""
        email = ""

    assert response is not None
    if response.status_code == 401:
        lock_seconds = register_failed_login(context, user.id, time.monotonic())
        if lock_seconds:
            message_text = (
                "❌ Достигнут лимит неверных попыток. Запросы приостановлены на 15 минут."
            )
        else:
            message_text = "❌ Email или пароль не подошли. Проверь данные и попробуй позже."
        await status.edit_text(
            f"<b>{escape(service.label.upper())}</b>\n━━━━━━━━━━━━━━━━━━\n{message_text}"
        )
        context.user_data.clear()
        return ConversationHandler.END

    if response.status_code == 429:
        await status.edit_text(
            f"<b>{escape(service.label.upper())}</b>\n━━━━━━━━━━━━━━━━━━\n"
            "⏳ Сервис ограничил частоту запросов. Подожди и повтори позже."
        )
        context.user_data.clear()
        return ConversationHandler.END

    if response.status_code != 200:
        log.warning("Token endpoint returned HTTP %s for %s", response.status_code, service.label)
        await status.edit_text(
            f"<b>{escape(service.label.upper())}</b>\n━━━━━━━━━━━━━━━━━━\n"
            "⚠️ Сервис временно не выдал токен. Подробности ответа не отправлены в чат."
        )
        context.user_data.clear()
        return ConversationHandler.END

    try:
        payload = response.json()
    except ValueError:
        log.warning("Token endpoint returned invalid JSON for %s", service.label)
        await status.edit_text("⚠️ Сервис вернул неожиданный ответ. Попробуй позже.")
        context.user_data.clear()
        return ConversationHandler.END

    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token or len(token) > 8192:
        log.warning("Token field missing or invalid in response from %s", service.label)
        await status.edit_text("⚠️ Сервис не вернул токен ожидаемого формата. Попробуй позже.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        raw_lifetime = payload.get("lifetime")
        lifetime_ms = float(raw_lifetime) if raw_lifetime is not None else 0.0
        lifetime_hours = lifetime_ms / 3_600_000 if lifetime_ms > 0 else None
    except (TypeError, ValueError):
        lifetime_hours = None

    clear_auth_state(context, user.id)
    safe_token = quote(token, safe="")
    login_url = f"{service.login_url}?cdnauthtoken={safe_token}&return_url=%2F"
    lifetime_text = f"Токен действует около <b>{lifetime_hours:.0f} ч.</b>" if lifetime_hours else "Срок действия определяет сервис."
    log.info("Access link issued for %s", service.label)

    await status.edit_text(
        f"<b>{escape(service.label.upper())}</b>  <code>ГОТОВО</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Ссылка входа создана. Открой её кнопкой ниже.\n"
        "Принятие токена целевым кабинетом и доступность CDN-раздачи не проверены.\n\n"
        f"⏱ {lifetime_text}\n"
        "🔐 Ссылка содержит секретный токен. Не пересылай её; после входа удали это сообщение.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("↗  Открыть аккаунт", url=login_url)],
                [InlineKeyboardButton("🧹  Удалить сообщение", callback_data="hide_token")],
            ]
        ),
        disable_web_page_preview=True,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def hide_token_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not await check_access(update, notify=False):
        await query.answer("Доступ ограничен.", show_alert=True)
        return
    await query.answer("Удаляю ссылку из чата…")
    try:
        if query.message:
            await query.message.delete()
    except Exception as exc:
        log.info("Token message deletion unavailable (%s)", type(exc).__name__)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await check_access(update):
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.clear()
    query = update.callback_query
    if query:
        await query.answer()
        try:
            await query.edit_message_text(
                "<b>ЗАПРОС ОТМЕНЁН</b>\n━━━━━━━━━━━━━━━━━━\nАктивный диалог очищен. Открой /start, чтобы начать заново."
            )
            return ConversationHandler.END
        except Exception as exc:
            log.info("Prompt edit unavailable during cancellation (%s)", type(exc).__name__)
    message = update.effective_message
    if message:
        await message.reply_text("Действие отменено. Секретные данные очищены из активного диалога.")
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    message = update.effective_message
    if message:
        await message.reply_text(
            "<b>Команды</b>\n/start — открыть меню\n/cancel — отменить текущий запрос\n\n"
            "Пароль вводи только в личном чате с этим ботом.",
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Avoid logging exception text/tracebacks because request objects can contain user data.
    error = context.error
    log.error("Unhandled update error (%s)", type(error).__name__ if error else "unknown")


def main() -> None:
    global ALLOWED_USER_IDS
    try:
        bot_token, allowed_ids = load_config()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    defaults = Defaults(parse_mode=ParseMode.HTML)
    application = Application.builder().token(bot_token).defaults(defaults).concurrent_updates(False).build()
    ALLOWED_USER_IDS = allowed_ids

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", show_home, filters=filters.ChatType.PRIVATE)],
        states={
            CHOOSE_SERVICE: [
                CallbackQueryHandler(
                    choose_service,
                    pattern=SERVICE_CALLBACK_PATTERN,
                ),
                CallbackQueryHandler(about_security, pattern=r"^about:security$"),
                CallbackQueryHandler(back_to_services, pattern=r"^back:services$"),
            ],
            EMAIL: [
                CallbackQueryHandler(back_to_services, pattern=r"^back:services$"),
                CallbackQueryHandler(cancel, pattern=r"^cancel$"),
                MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, got_email),
            ],
            PASSWORD: [
                CallbackQueryHandler(back_to_services, pattern=r"^back:services$"),
                CallbackQueryHandler(cancel, pattern=r"^cancel$"),
                MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, got_password),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=filters.ChatType.PRIVATE)],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
    )
    application.add_handler(conversation)
    application.add_handler(CallbackQueryHandler(hide_token_message, pattern=r"^hide_token$"))
    application.add_handler(CommandHandler("help", help_command, filters=filters.ChatType.PRIVATE))
    application.add_error_handler(on_error)

    log.info("Bot started; allowlist contains %d Telegram user(s)", len(allowed_ids))
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
