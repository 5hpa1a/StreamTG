# main.py
import asyncio
import csv
import io
import json
import time
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (
    Message, CallbackQuery, MessageEntity,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    BufferedInputFile
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

import config
import database as db
from checkers import StreamChecker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Поддержка прокси, если PROXY_URL указан в config.py
proxy_url = getattr(config, "PROXY_URL", None)
session = AiohttpSession(proxy=proxy_url) if proxy_url else None

bot = Bot(token=config.BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router()
dp.include_router(router)
checker = StreamChecker()

# Время последней проверки для точного подсчета длительности стримов
last_check_timestamp: float | None = None

# Надписи для платформ в постах
PLATFORM_LABELS = {
    "twitch": "TWITCH",
    "kick": "KICK",
    "youtube": "YOUTUBE"
}

# Названия для внутреннего меню админ-панели
PLATFORM_NAMES = {
    "twitch": "Twitch",
    "kick": "Kick",
    "youtube": "YouTube"
}

# Доступные форматы оформления ссылок в постах
LINK_STYLE_NAMES = {
    "hyperlinks": "Гиперссылки (TWITCH | KICK)",
    "urls": "Прямые ссылки в посте",
    "buttons": "Инлайн-кнопки под постом"
}


# --- Машина состояний (FSM) для анкет ---
class AdminStates(StatesGroup):
    waiting_nickname = State()
    waiting_platform = State()
    waiting_channel = State()
    waiting_photo = State()
    waiting_text = State()

    adding_platform_select = State()
    adding_platform_channel = State()
    editing_photo = State()
    editing_text = State()


# --- Вспомогательные функции ---
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def utf16_len(text: str) -> int:
    """Вычисляет длину строки в UTF-16 кодовых единицах (стандарт смещений Telegram API)."""
    return len(text.encode("utf-16-le")) // 2


def serialize_entities(entities: list[MessageEntity] | None) -> str:
    """Сохраняет стандартное форматирование текста, отфильтровывая кастомные эмодзи."""
    if not entities:
        return "[]"
    clean_entities = [
        e.model_dump(exclude_none=True)
        for e in entities
        if e.type != "custom_emoji"
    ]
    return json.dumps(clean_entities, ensure_ascii=False)


def deserialize_entities(entities_json: str) -> list[MessageEntity]:
    try:
        raw_list = json.loads(entities_json)
        return [MessageEntity(**item) for item in raw_list if item.get("type") != "custom_emoji"]
    except Exception:
        return []


async def get_current_link_style() -> str:
    style = await db.get_setting("post_link_style")
    return style if style in LINK_STYLE_NAMES else "hyperlinks"


def build_stream_post_kb(live_streams: list) -> InlineKeyboardMarkup:
    """Формирует инлайн-кнопки TWITCH / KICK / YOUTUBE со ссылками на активные стримы."""
    buttons = []
    for s in live_streams:
        label = PLATFORM_LABELS.get(s["platform"], s["platform"].upper())
        buttons.append([InlineKeyboardButton(text=label, url=s["channel_url"])])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def compose_stream_post(player, live_streams: list) -> tuple[str, list[MessageEntity], InlineKeyboardMarkup | None]:
    """
    Собирает итоговый текст поста, сущности разметки (entities) и клавиатуру
    в зависимости от выбранного режима оформления ссылок.
    """
    base_caption = player["caption"] or ""
    final_entities = deserialize_entities(player["entities_json"])
    link_style = await get_current_link_style()

    if not live_streams:
        return base_caption, final_entities, None

    # Режим 1: Инлайн-кнопки под постом
    if link_style == "buttons":
        kb = build_stream_post_kb(live_streams)
        return base_caption, final_entities, kb

    # Режим 2: Классические гиперссылки формата TWITCH | KICK | YOUTUBE
    if link_style == "hyperlinks":
        prefix = "\n\n" if base_caption else ""
        current_text = base_caption + prefix

        for idx, s in enumerate(live_streams):
            if idx > 0:
                current_text += " | "

            label = PLATFORM_LABELS.get(s["platform"], s["platform"].upper())
            offset = utf16_len(current_text)
            length = utf16_len(label)
            current_text += label

            final_entities.append(
                MessageEntity(type="text_link", offset=offset, length=length, url=s["channel_url"])
            )
            final_entities.append(
                MessageEntity(type="bold", offset=offset, length=length)
            )

        return current_text, final_entities, None

    # Режим 3: Прямые URL-ссылки, встроенные в текст поста
    prefix = "\n\n" if base_caption else ""
    current_text = base_caption + prefix

    for idx, s in enumerate(live_streams):
        if idx > 0:
            current_text += "\n"
        label = PLATFORM_LABELS.get(s["platform"], s["platform"].upper())
        label_offset = utf16_len(current_text)
        label_length = utf16_len(label)
        final_entities.append(
            MessageEntity(type="bold", offset=label_offset, length=label_length)
        )
        current_text += f"{label}: {s['channel_url']}"

    return current_text, final_entities, None


def format_duration(seconds: int) -> str:
    """Переводит секунды в читаемый вид: X ч. Y мин."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours} ч. {minutes} мин."


async def build_stats_report_text(month_key: str) -> str:
    """Формирует текст отчета в формате: Никнейм-Платформа-Суммарное время."""
    rows = await db.get_monthly_stats(month_key)
    year, month = month_key.split(" - ")
    header = f"📊 <b>Статистика стримов за {month}.{year}:</b>\n\n"

    if not rows:
        return header + "<i>Данных за этот период нет.</i>"

    lines = []
    for r in rows:
        plat = PLATFORM_LABELS.get(r["platform"], r["platform"].upper())
        duration_str = format_duration(r["total_seconds"])
        lines.append(f"{r['nickname']}-{plat}-{duration_str}")

    return header + "\n".join(lines)


async def build_stats_csv_bytes(month_key: str) -> bytes:
    """Генерирует CSV-файл в кодировке UTF-8-SIG (для прямого открытия в Excel)."""
    rows = await db.get_monthly_stats(month_key)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow([
        "Период",
        "Никнейм",
        "Платформа",
        "Суммарное время",
        "Всего минут",
        "Всего часов"
    ])

    for r in rows:
        total_sec = r["total_seconds"]
        plat = PLATFORM_LABELS.get(r["platform"], r["platform"].upper())
        duration_str = format_duration(total_sec)
        total_minutes = total_sec // 60
        total_hours = f"{total_sec / 3600:.2f}".replace(".", ",")

        writer.writerow([
            month_key,
            r["nickname"],
            plat,
            duration_str,
            total_minutes,
            total_hours
        ])

    return output.getvalue().encode("utf-8-sig")


async def main_menu_kb() -> InlineKeyboardMarkup:
    current_style = await get_current_link_style()
    style_label = LINK_STYLE_NAMES.get(current_style, current_style)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить игрока / канал", callback_data="menu:add_player")],
        [InlineKeyboardButton(text="📋 Список игроков", callback_data="menu:list_players")],
        [InlineKeyboardButton(text=f"🔗 Ссылки: {style_label}", callback_data="menu:link_style")],
        [InlineKeyboardButton(text="📊 Статистика за месяц", callback_data="menu:stats")],
        [InlineKeyboardButton(text="🔄 Проверить стримы сейчас", callback_data="menu:force_check")]
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:cancel")]
    ])


def platform_select_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="TWITCH", callback_data=f"{prefix}:twitch"),
            InlineKeyboardButton(text="KICK", callback_data=f"{prefix}:kick"),
            InlineKeyboardButton(text="YOUTUBE", callback_data=f"{prefix}:youtube")
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:cancel")]
    ])


# --- Главное меню ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    reply_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🎛 Панель управления")]],
        resize_keyboard=True
    )
    await message.answer("Добро пожаловать! Используйте кнопки ниже:", reply_markup=reply_kb)
    await message.answer(
        "🎛 <b>Главное меню управления оповещениями:</b>",
        parse_mode="HTML",
        reply_markup=await main_menu_kb()
    )


@router.message(F.text == "🎛 Панель управления")
async def open_panel_btn(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "🎛 <b>Главное меню управления оповещениями:</b>",
        parse_mode="HTML",
        reply_markup=await main_menu_kb()
    )


@router.callback_query(F.data == "menu:main")
@router.callback_query(F.data == "menu:cancel")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(
        "🎛 <b>Главное меню управления оповещениями:</b>",
        parse_mode="HTML",
        reply_markup=await main_menu_kb()
    )


# --- Настройка формата ссылок в постах ---
@router.callback_query(F.data == "menu:link_style")
async def cb_link_style_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    current = await get_current_link_style()

    buttons = []
    for key, title in LINK_STYLE_NAMES.items():
        mark = "✅ " if key == current else ""
        buttons.append([InlineKeyboardButton(text=f"{mark}{title}", callback_data=f"setstyle:{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")])

    text = (
        "🔗 <b>Выберите формат отображения ссылок в постах:</b>\n\n"
        "• <b>Гиперссылки (TWITCH | KICK)</b> — в конец текста поста добавляется строка "
        "вида <b>TWITCH | KICK | YOUTUBE</b> (или просто <b>TWITCH</b>, если стрим один) со встроенными ссылками.\n"
        "• <b>Прямые ссылки в посте</b> — в конец текста добавляются открытые URL-ссылки.\n"
        "• <b>Инлайн-кнопки под постом</b> — ссылки прикрепляются кнопками под постом."
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("setstyle:"))
async def cb_set_link_style(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    new_style = call.data.split(":")[1]
    if new_style not in LINK_STYLE_NAMES:
        return

    await db.set_setting("post_link_style", new_style)
    await call.answer(f"Выбран режим: {LINK_STYLE_NAMES[new_style]}")

    # Сразу обновляем уже опубликованные активные посты в канале под новый стиль
    players = await db.get_all_players()
    for player in players:
        if player["active_message_id"]:
            streams = await db.get_player_streams(player["id"])
            live_streams = [s for s in streams if s["is_live"]]
            if live_streams:
                caption_text, entities, kb = await compose_stream_post(player, live_streams)
                try:
                    await bot.edit_message_caption(
                        chat_id=config.CHANNEL_ID,
                        message_id=player["active_message_id"],
                        caption=caption_text,
                        caption_entities=entities,
                        parse_mode=None,
                        reply_markup=kb
                    )
                except Exception as e:
                    logging.warning(f"Не удалось обновить активный пост для {player['nickname']}: {e}")

    await cb_link_style_menu(call)


# --- Раздел статистики в админ-меню ---
@router.callback_query(F.data == "menu:stats")
async def cb_menu_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    current_month = datetime.now().strftime("%Y-%m")
    report_text = await build_stats_report_text(current_month)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать таблицу (CSV / Excel)", callback_data=f"stats:csv:{current_month}")],
        [InlineKeyboardButton(text="📢 Отправить эту сводку в канал", callback_data=f"stats:send:{current_month}")],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")]
    ])
    await call.message.edit_text(report_text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("stats:csv:"))
async def cb_export_stats_csv(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    month_key = call.data.split(":")[2]
    csv_bytes = await build_stats_csv_bytes(month_key)
    file = BufferedInputFile(csv_bytes, filename=f"stream_stats_{month_key}.csv")

    await call.message.answer_document(
        document=file,
        caption=f"📊 Таблица статистики стримов за <b>{month_key}</b>",
        parse_mode="HTML"
    )
    await call.answer("Файл отправлен!")


@router.callback_query(F.data.startswith("stats:send:"))
async def cb_send_stats_to_channel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    month_key = call.data.split(":")[2]
    report_text = await build_stats_report_text(month_key)
    try:
        await bot.send_message(chat_id=config.CHANNEL_ID, text=report_text, parse_mode="HTML")
        await call.answer("✅ Статистика опубликована в канале!", show_alert=True)
    except Exception as e:
        await call.answer(f"Ошибка отправки: {e}", show_alert=True)


# --- Добавление нового игрока ---
@router.callback_query(F.data == "menu:add_player")
async def cb_add_player_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_nickname)
    await call.message.edit_text(
        "👤 <b>Шаг 1 из 5:</b> Введите никнейм игрока:",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )


@router.message(AdminStates.waiting_nickname, F.text)
async def step_nickname(message: Message, state: FSMContext):
    nickname = message.text.strip()
    existing = await db.get_player_by_nickname(nickname)

    if existing:
        await state.update_data(player_id=existing["id"])
        await state.set_state(AdminStates.adding_platform_select)
        await message.answer(
            f"ℹ️ Игрок <b>{existing['nickname']}</b> уже существует в базе!\n"
            f"Выберите платформу, которую хотите добавить к его профилю:",
            parse_mode="HTML",
            reply_markup=platform_select_kb("addplat")
        )
        return

    await state.update_data(nickname=nickname)
    await state.set_state(AdminStates.waiting_platform)
    await message.answer(
        f"🎮 <b>Шаг 2 из 5:</b> Выберите платформу для игрока <b>{nickname}</b>:",
        parse_mode="HTML",
        reply_markup=platform_select_kb("newplat")
    )


@router.callback_query(AdminStates.waiting_platform, F.data.startswith("newplat:"))
async def step_platform(call: CallbackQuery, state: FSMContext):
    platform = call.data.split(":")[1]
    await state.update_data(platform=platform)
    await state.set_state(AdminStates.waiting_channel)
    await call.message.edit_text(
        f"🔗 <b>Шаг 3 из 5:</b> Отправьте ссылку на канал или название канала ({PLATFORM_NAMES[platform]}):",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )


@router.message(AdminStates.waiting_channel, F.text)
async def step_channel(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        slug, url = checker.parse_channel_input(data["platform"], message.text)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка разбора ссылки: {e}\nПопробуйте еще раз:", reply_markup=cancel_kb())
        return

    await state.update_data(slug=slug, url=url)
    await state.set_state(AdminStates.waiting_photo)
    await message.answer(
        "🖼 <b>Шаг 4 из 5:</b> Отправьте изображение (как фото, не файлом), которое будет прикрепляться к посту:",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )


@router.message(AdminStates.waiting_photo, F.photo)
async def step_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    await state.set_state(AdminStates.waiting_text)
    await message.answer(
        "📝 <b>Шаг 5 из 5:</b> Отправьте текст оповещения (поддерживаются стандартные эмодзи и форматирование текста):",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )


@router.message(AdminStates.waiting_text, F.text | F.caption)
async def step_text(message: Message, state: FSMContext):
    caption = message.text or message.caption or ""
    entities = message.entities or message.caption_entities
    entities_json = serialize_entities(entities)

    data = await state.get_data()
    player_id = await db.create_player(
        nickname=data["nickname"],
        photo_id=data["photo_id"],
        caption=caption,
        entities_json=entities_json
    )
    await db.add_or_update_stream(player_id, data["platform"], data["slug"], data["url"])
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё платформу этому игроку", callback_data=f"player:addplat:{player_id}")],
        [InlineKeyboardButton(text="👤 Открыть профиль игрока", callback_data=f"player:view:{player_id}")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:main")]
    ])
    await message.answer(
        f"✅ Игрок <b>{data['nickname']}</b> и канал <b>{data['url']}</b> успешно сохранены!",
        parse_mode="HTML",
        reply_markup=kb
    )


# --- Список игроков и управление профилем ---
@router.callback_query(F.data == "menu:list_players")
async def cb_list_players(call: CallbackQuery, state: FSMContext):
    if state:
        await state.clear()
    players = await db.get_all_players()
    if not players:
        await call.message.edit_text(
            "📭 Список игроков пока пуст.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить игрока", callback_data="menu:add_player")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")]
            ])
        )
        return

    buttons = []
    for p in players:
        status_icon = "🟢 В эфире" if p["active_message_id"] else "⚫️ Офлайн"
        buttons.append([
            InlineKeyboardButton(
                text=f"{p['nickname']} ({status_icon})",
                callback_data=f"player:view:{p['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")])

    await call.message.edit_text(
        "📋 <b>Выберите игрока для настройки:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


async def render_player_card(player_id: int) -> tuple[str, InlineKeyboardMarkup]:
    player = await db.get_player(player_id)
    streams = await db.get_player_streams(player_id)

    lines = [f"👤 <b>Профиль игрока: {player['nickname']}</b>\n"]
    if streams:
        lines.append("<b>Привязанные платформы:</b>")
        for s in streams:
            st = "🟢 LIVE" if s["is_live"] else "⚫️ Офлайн"
            lines.append(f"• {PLATFORM_NAMES[s['platform']]}: {s['channel_url']} ({st})")
    else:
        lines.append("⚠️ <i>Нет привязанных платформ</i>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить / обновить платформу", callback_data=f"player:addplat:{player_id}")],
        [InlineKeyboardButton(text="🗑 Отвязать платформу", callback_data=f"player:delplat_menu:{player_id}")],
        [
            InlineKeyboardButton(text="🖼 Изменить фото", callback_data=f"player:editphoto:{player_id}"),
            InlineKeyboardButton(text="📝 Изменить текст", callback_data=f"player:edittext:{player_id}")
        ],
        [InlineKeyboardButton(text="👁 Предпросмотр поста", callback_data=f"player:preview:{player_id}")],
        [InlineKeyboardButton(text="❌ Удалить игрока полностью", callback_data=f"player:delete:{player_id}")],
        [InlineKeyboardButton(text="⬅️ К списку игроков", callback_data="menu:list_players")]
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("player:view:"))
async def cb_view_player(call: CallbackQuery, state: FSMContext):
    await state.clear()
    player_id = int(call.data.split(":")[2])
    text, kb = await render_player_card(player_id)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# --- Добавление платформы к существующему игроку ---
@router.callback_query(F.data.startswith("player:addplat:"))
async def cb_add_platform_existing(call: CallbackQuery, state: FSMContext):
    player_id = int(call.data.split(":")[2])
    await state.update_data(player_id=player_id)
    await state.set_state(AdminStates.adding_platform_select)
    await call.message.edit_text(
        "🎮 Выберите платформу для привязки:",
        reply_markup=platform_select_kb("addplat")
    )


@router.callback_query(AdminStates.adding_platform_select, F.data.startswith("addplat:"))
async def step_addplat_select(call: CallbackQuery, state: FSMContext):
    platform = call.data.split(":")[1]
    await state.update_data(platform=platform)
    await state.set_state(AdminStates.adding_platform_channel)
    await call.message.edit_text(
        f"🔗 Отправьте ссылку или название канала для {PLATFORM_NAMES[platform]}:",
        reply_markup=cancel_kb()
    )


@router.message(AdminStates.adding_platform_channel, F.text)
async def step_addplat_channel(message: Message, state: FSMContext):
    data = await state.get_data()
    player_id = data["player_id"]
    platform = data["platform"]

    try:
        slug, url = checker.parse_channel_input(platform, message.text)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}", reply_markup=cancel_kb())
        return

    await db.add_or_update_stream(player_id, platform, slug, url)
    await state.clear()

    text, kb = await render_player_card(player_id)
    await message.answer(f"✅ Платформа {PLATFORM_NAMES[platform]} привязана!\n\n{text}", parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# --- Удаление отдельной платформы ---
@router.callback_query(F.data.startswith("player:delplat_menu:"))
async def cb_delplat_menu(call: CallbackQuery):
    player_id = int(call.data.split(":")[2])
    streams = await db.get_player_streams(player_id)
    if not streams:
        await call.answer("У игрока нет привязанных платформ!", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text=f"🗑 {PLATFORM_NAMES[s['platform']]} ({s['channel_slug']})", callback_data=f"player:delplat:{s['id']}:{player_id}")]
        for s in streams
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"player:view:{player_id}")])
    await call.message.edit_text("Выберите платформу для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("player:delplat:"))
async def cb_delplat_confirm(call: CallbackQuery):
    _, _, stream_id, player_id = call.data.split(":")
    await db.delete_stream(int(stream_id))
    await call.answer("Платформа удалена")
    text, kb = await render_player_card(int(player_id))
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# --- Редактирование фото и текста ---
@router.callback_query(F.data.startswith("player:editphoto:"))
async def cb_edit_photo(call: CallbackQuery, state: FSMContext):
    player_id = int(call.data.split(":")[2])
    await state.update_data(player_id=player_id)
    await state.set_state(AdminStates.editing_photo)
    await call.message.edit_text("🖼 Отправьте новое фото для поста:", reply_markup=cancel_kb())


@router.message(AdminStates.editing_photo, F.photo)
async def step_edit_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    player_id = data["player_id"]
    await db.update_player_photo(player_id, message.photo[-1].file_id)
    await state.clear()
    text, kb = await render_player_card(player_id)
    await message.answer(f"✅ Фото обновлено!\n\n{text}", parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("player:edittext:"))
async def cb_edit_text(call: CallbackQuery, state: FSMContext):
    player_id = int(call.data.split(":")[2])
    await state.update_data(player_id=player_id)
    await state.set_state(AdminStates.editing_text)
    await call.message.edit_text("📝 Отправьте новый текст оповещения (поддерживаются стандартные эмодзи и разметка):", reply_markup=cancel_kb())


@router.message(AdminStates.editing_text, F.text | F.caption)
async def step_edit_text(message: Message, state: FSMContext):
    data = await state.get_data()
    player_id = data["player_id"]
    caption = message.text or message.caption or ""
    entities = message.entities or message.caption_entities
    await db.update_player_text(player_id, caption, serialize_entities(entities))
    await state.clear()
    text, kb = await render_player_card(player_id)
    await message.answer(f"✅ Текст обновлен!\n\n{text}", parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# --- Предпросмотр и удаление игрока ---
@router.callback_query(F.data.startswith("player:preview:"))
async def cb_preview_post(call: CallbackQuery):
    player_id = int(call.data.split(":")[2])
    player = await db.get_player(player_id)
    streams = await db.get_player_streams(player_id)

    caption_text, entities, kb = await compose_stream_post(player, streams)

    await call.message.answer_photo(
        photo=player["photo_id"],
        caption=caption_text,
        caption_entities=entities,
        parse_mode=None,
        reply_markup=kb
    )
    await call.answer("Предпросмотр отправлен ниже")


@router.callback_query(F.data.startswith("player:delete:"))
async def cb_delete_player(call: CallbackQuery):
    player_id = int(call.data.split(":")[2])
    player = await db.get_player(player_id)
    if player and player["active_message_id"]:
        try:
            await bot.delete_message(config.CHANNEL_ID, player["active_message_id"])
        except Exception:
            pass
    await db.delete_player(player_id)
    await call.answer("Игрок удален")
    await cb_list_players(call, None)


@router.callback_query(F.data == "menu:force_check")
async def cb_force_check(call: CallbackQuery):
    await call.answer("Проверка запущена в фоне...")
    await check_all_streams()
    await call.message.answer("✅ Принудительная проверка всех каналов завершена.")


# --- Автоматическая отправка ежемесячного отчета при смене месяца ---
async def check_and_send_monthly_report():
    current_month = datetime.now().strftime("%Y-%m")
    last_tracked_month = await db.get_setting("active_stats_month")

    if last_tracked_month is None:
        await db.set_setting("active_stats_month", current_month)
        return

    if last_tracked_month != current_month:
        report_text = await build_stats_report_text(last_tracked_month)
        try:
            await bot.send_message(
                chat_id=config.CHANNEL_ID,
                text=report_text,
                parse_mode="HTML"
            )
            await db.set_setting("active_stats_month", current_month)
            logging.info(f"Ежемесячный отчет за {last_tracked_month} успешно опубликован в канале.")
        except Exception as e:
            logging.error(f"Не удалось отправить ежемесячный отчет за {last_tracked_month}: {e}")


# --- Фоновая проверка стримов и логика постов ---
async def check_all_streams():
    global last_check_timestamp
    now = time.time()

    if last_check_timestamp is None:
        elapsed_seconds = config.CHECK_INTERVAL
    else:
        elapsed_seconds = int(min(max(0, now - last_check_timestamp), config.CHECK_INTERVAL * 3))
    last_check_timestamp = now

    await check_and_send_monthly_report()
    current_month = datetime.now().strftime("%Y-%m")

    players = await db.get_all_players()

    for player in players:
        player_id = player["id"]
        streams = await db.get_player_streams(player_id)
        if not streams:
            continue

        live_streams = []
        status_changed = False

        for s in streams:
            is_live = await checker.is_stream_live(s["platform"], s["channel_slug"], s["channel_url"])
            was_live = bool(s["is_live"])

            if is_live != was_live:
                status_changed = True
                await db.update_stream_status(s["id"], is_live)

            if is_live:
                live_streams.append(s)
                await db.add_stream_duration(s["id"], current_month, elapsed_seconds)

        active_msg_id = player["active_message_id"]
        offline_since = player["offline_since"]

        # СЦЕНАРИЙ 1: Хотя бы на одной платформе идёт стрим
        if len(live_streams) > 0:
            if offline_since is not None:
                await db.update_player_post_state(player_id, active_msg_id, None)
                offline_since = None

            caption_text, entities, kb = await compose_stream_post(player, live_streams)

            if active_msg_id is None:
                try:
                    sent = await bot.send_photo(
                        chat_id=config.CHANNEL_ID,
                        photo=player["photo_id"],
                        caption=caption_text,
                        caption_entities=entities,
                        parse_mode=None,
                        reply_markup=kb
                    )
                    await db.update_player_post_state(player_id, sent.message_id, None)
                    logging.info(f"[{player['nickname']}] Опубликован пост о старте стрима (msg_id={sent.message_id})")
                except Exception as e:
                    logging.error(f"Ошибка отправки поста для {player['nickname']}: {e}")
            elif status_changed:
                try:
                    await bot.edit_message_caption(
                        chat_id=config.CHANNEL_ID,
                        message_id=active_msg_id,
                        caption=caption_text,
                        caption_entities=entities,
                        parse_mode=None,
                        reply_markup=kb
                    )
                    logging.info(f"[{player['nickname']}] Обновлены активные платформы в посте (msg_id={active_msg_id})")
                except TelegramBadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        logging.error(f"Ошибка редактирования поста {player['nickname']}: {e}")

        # СЦЕНАРИЙ 2: Все платформы офлайн, но пост ещё висит в канале
        elif active_msg_id is not None:
            if offline_since is None:
                await db.update_player_post_state(player_id, active_msg_id, now)
                logging.info(f"[{player['nickname']}] Все стримы офлайн. Запущен 5-минутный таймер до удаления поста.")
            elif now - offline_since >= config.OFFLINE_GRACE_PERIOD:
                try:
                    await bot.delete_message(chat_id=config.CHANNEL_ID, message_id=active_msg_id)
                    logging.info(f"[{player['nickname']}] Пост удален после 5 минут офлайна (msg_id={active_msg_id}).")
                except Exception as e:
                    logging.warning(f"Не удалось удалить пост {active_msg_id} (возможно, уже удален): {e}")
                await db.update_player_post_state(player_id, None, None)


async def scheduler_loop():
    while True:
        try:
            await check_all_streams()
        except Exception as e:
            logging.error(f"Критическая ошибка в цикле проверки: {e}")
        await asyncio.sleep(config.CHECK_INTERVAL)


async def main():
    await db.init_db()
    asyncio.create_task(scheduler_loop())
    logging.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())