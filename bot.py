import logging
import os
import sqlite3
import asyncio
import time
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from datetime import datetime, timedelta
import re
from typing import Dict, List, Tuple, Optional
from flask import Flask
from threading import Thread

# Настройка логирования для Railway
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler()  # Только консоль на Railway
    ]
)
logger = logging.getLogger(__name__)

# Токен бота - будет установлен через Railway Variables
BOT_TOKEN = os.environ.get('BOT_TOKEN_REMINDER')

# Дни недели для повторения
DAYS_OF_WEEK = {
    0: "Понедельник",
    1: "Вторник", 
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Саббота",
    6: "Воскресенье"
}

# Инициализация базы данных
def init_db():
    # Проверяем, существует ли файл БД
    db_path = 'reminders.db'
    logger.info(f"Инициализация базы данных: {db_path}")
    
    conn = sqlite3.connect(db_path, check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        user_name TEXT,
        text TEXT NOT NULL,
        reminder_time DATETIME NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_active BOOLEAN DEFAULT 1,
        sent BOOLEAN DEFAULT 0,
        postponed_count INTEGER DEFAULT 0,
        repeat_type TEXT DEFAULT 'once',
        repeat_days TEXT DEFAULT '',
        repeat_interval INTEGER DEFAULT 1,
        next_reminder_time DATETIME,
        original_reminder_id INTEGER DEFAULT NULL
    )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

init_db()

# Создание основного меню
def create_main_menu():
    keyboard = [
        [KeyboardButton("Создать напоминание"), KeyboardButton("Мои напоминания")],
        [KeyboardButton("Ближайшие"), KeyboardButton("🔄"), KeyboardButton("Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, input_field_placeholder="Выберите действие...")

# Создание клавиатуры списка напоминаний
def create_reminders_list_keyboard(reminders: List[Dict], page: int = 0, page_size: int = 8):
    keyboard = []
    
    # Рассчитываем, какие напоминания показывать на текущей странице
    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_reminders = reminders[start_idx:end_idx]
    
    for reminder in page_reminders:
        time_str = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S').strftime('%d.%m %H:%M')
        text_preview = reminder['text'][:15] + "..." if len(reminder['text']) > 15 else reminder['text']
        
        # Добавляем эмодзи для статуса
        if reminder['sent']:
            status = "✅"
        elif reminder['is_active']:
            current_time = datetime.now()
            reminder_time = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S')
            if reminder_time < current_time:
                status = "⚠️"
            else:
                status = "⏳"
        else:
            status = "❌"
        
        # Добавляем эмодзи для повторения
        if reminder['repeat_type'] != 'once':
            repeat_emoji = "🔄"
        else:
            repeat_emoji = ""
        
        button_text = f"{status} {time_str} {text_preview} {repeat_emoji}"
        callback_data = f"view_{reminder['id']}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    # Добавляем кнопки навигации
    nav_buttons = []
    total_pages = (len(reminders) + page_size - 1) // page_size
    
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"list_page_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="list_page_current"))
    
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"list_page_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    # Кнопка возврата
    keyboard.append([InlineKeyboardButton("🔙", callback_data="back_to_start")])
    
    return InlineKeyboardMarkup(keyboard)

# Создание клавиатуры для управления напоминанием
def create_reminder_control_keyboard(reminder_id: int):
    keyboard = [
        [
            InlineKeyboardButton("📝 Изменить текст", callback_data=f"edit_text_{reminder_id}"),
            InlineKeyboardButton("⏰ Изменить время", callback_data=f"edit_time_{reminder_id}")
        ],
        [
            InlineKeyboardButton("🔄 Изменить повторение", callback_data=f"edit_repeat_{reminder_id}"),
            InlineKeyboardButton("❌ Удалить", callback_data=f"delete_confirm_{reminder_id}")
        ],
        [
            InlineKeyboardButton("✅ Выполнить сейчас", callback_data=f"done_now_{reminder_id}"),
            InlineKeyboardButton("⏰ Отложить", callback_data=f"snooze_menu_{reminder_id}")
        ],
        [
            InlineKeyboardButton("К списку", callback_data="back_to_list_0"),
            InlineKeyboardButton("🔙", callback_data="back_to_start")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# Создание клавиатуры для подтверждения удаления
def create_delete_confirm_keyboard(reminder_id: int):
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete_yes_{reminder_id}"),
            InlineKeyboardButton("❌ Нет, отмена", callback_data=f"view_{reminder_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# Создание клавиатуры для выбора типа повторения
def create_repeat_keyboard(reminder_id: int = None):
    callback_prefix = f"edit_repeat_type_{reminder_id}_" if reminder_id else "repeat_"
    
    keyboard = [
        [
            InlineKeyboardButton("📌 Один раз", callback_data=f"{callback_prefix}once"),
            InlineKeyboardButton("📅 Ежедневно", callback_data=f"{callback_prefix}daily")
        ],
        [
            InlineKeyboardButton("🗓️ Еженедельно", callback_data=f"{callback_prefix}weekly"),
            InlineKeyboardButton("📆 Выбрать дни", callback_data=f"{callback_prefix}custom")
        ]
    ]
    
    if reminder_id:
        keyboard.append([
            InlineKeyboardButton("🔙", callback_data=f"view_{reminder_id}")
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("⏭️ Пропустить", callback_data="repeat_skip")
        ])
    
    return InlineKeyboardMarkup(keyboard)

# Создание клавиатуры для ежедневного интервала
def create_daily_interval_keyboard(reminder_id: int = None):
    callback_prefix = f"edit_interval_{reminder_id}_" if reminder_id else "interval_"
    
    keyboard = []
    row = []
    
    intervals = [1, 2, 3, 7, 14, 30]
    
    for interval in intervals:
        if interval == 1:
            text = "Каждый день"
        elif interval == 7:
            text = "Раз в неделю"
        elif interval == 14:
            text = "Раз в 2 недели"
        elif interval == 30:
            text = "Раз в месяц"
        else:
            text = f"Каждые {interval} дня"
        
        row.append(InlineKeyboardButton(text, callback_data=f"{callback_prefix}{interval}"))
        
        if len(row) == 2:
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)
    
    back_callback = f"edit_repeat_{reminder_id}" if reminder_id else "interval_back"
    keyboard.append([
        InlineKeyboardButton("🔙", callback_data=back_callback)
    ])
    
    return InlineKeyboardMarkup(keyboard)

# Создание клавиатуры для выбора дней недели
def create_days_keyboard(selected_days: List[int] = None, reminder_id: int = None):
    if selected_days is None:
        selected_days = []
    
    keyboard = []
    row = []
    
    for day_num, day_name in DAYS_OF_WEEK.items():
        if day_num in selected_days:
            emoji = "✅"
        else:
            emoji = "◻️"
        
        callback_data = f"edit_day_{reminder_id}_{day_num}" if reminder_id else f"day_{day_num}"
        row.append(InlineKeyboardButton(f"{emoji} {day_name[:3]}", callback_data=callback_data))
        
        if len(row) == 2:
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)
    
    done_callback = f"edit_days_done_{reminder_id}" if reminder_id else "days_done"
    cancel_callback = f"edit_repeat_{reminder_id}" if reminder_id else "days_cancel"
    
    keyboard.append([
        InlineKeyboardButton("✅ Готово", callback_data=done_callback),
        InlineKeyboardButton("❌ Отмена", callback_data=cancel_callback)
    ])
    
    return InlineKeyboardMarkup(keyboard)

# Создание клавиатуры для напоминания (для уведомлений)
def create_reminder_keyboard(reminder_id: int):
    keyboard = [
        [
            InlineKeyboardButton("✅ Выполнено", callback_data=f"done_{reminder_id}"),
            InlineKeyboardButton("⏰ Отложить", callback_data=f"snooze_menu_{reminder_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# Создание клавиатуры для выбора времени откладывания
def create_snooze_options_keyboard(reminder_id: int):
    keyboard = [
        [
            InlineKeyboardButton("5 мин", callback_data=f"snooze_5_{reminder_id}"),
            InlineKeyboardButton("15 мин", callback_data=f"snooze_15_{reminder_id}"),
            InlineKeyboardButton("30 мин", callback_data=f"snooze_30_{reminder_id}")
        ],
        [
            InlineKeyboardButton("1 час", callback_data=f"snooze_60_{reminder_id}"),
            InlineKeyboardButton("2 часа", callback_data=f"snooze_120_{reminder_id}"),
            InlineKeyboardButton("Завтра", callback_data=f"snooze_tomorrow_{reminder_id}")
        ],
        [
            InlineKeyboardButton("🔙", callback_data=f"view_{reminder_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    
    welcome_text = f"""
💭 Привет, {user.first_name}!

Я твой персональный помощник для напоминаний!

🌟 Что я умею:
• Создавать разовые и повторяющиеся напоминания
• Показывать все напоминания
• Редактировать и удалять напоминания
• Показывать 3 ближайших напоминания
• Отправлять уведомления

💫 Начни с кнопки «Создать напоминание» или «Мои напоминания»!
    """
    
    keyboard = create_main_menu()
    await update.message.reply_text(welcome_text, reply_markup=keyboard)

# Показать список напоминаний с кнопками
async def show_reminders_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    user_id = update.message.from_user.id if update.message else update.callback_query.from_user.id
    
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Получаем все активные напоминания пользователя
    cursor.execute('''
        SELECT * FROM reminders 
        WHERE user_id = ? 
        AND is_active = 1
        ORDER BY reminder_time
    ''', (user_id,))
    
    columns = [column[0] for column in cursor.description]
    reminders = []
    for row in cursor.fetchall():
        reminder_dict = dict(zip(columns, row))
        reminders.append(reminder_dict)
    
    conn.close()
    
    if not reminders:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "💭 У вас пока нет активных напоминаний.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Создать напоминание", callback_data="create_new")],
                    [InlineKeyboardButton("🔙", callback_data="back_to_start")]
                ])
            )
        else:
            await update.message.reply_text(
                "💭 У вас пока нет активных напоминаний.",
                reply_markup=create_main_menu()
            )
        return
    
    # Создаем клавиатуру со списком
    keyboard = create_reminders_list_keyboard(reminders, page)
    
    current_time = datetime.now()
    upcoming_count = 0
    overdue_count = 0
    
    for reminder in reminders:
        reminder_time = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S')
        if reminder_time >= current_time and not reminder['sent']:
            upcoming_count += 1
        elif reminder_time < current_time and not reminder['sent']:
            overdue_count += 1
    
    status_text = ""
    if overdue_count > 0:
        status_text += f"⚠️ Просрочено: {overdue_count}\n"
    if upcoming_count > 0:
        status_text += f"⏳ Ожидает: {upcoming_count}\n"
    
    response = f"""
💭 *Список всех напоминаний*

{status_text}
Всего: {len(reminders)} напоминаний

✨Выберите напоминание для изменения:
    """
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
    else:
        await update.message.reply_text(response, parse_mode='Markdown', reply_markup=keyboard)

# Показать детали напоминания
async def show_reminder_details(update: Update, context: ContextTypes.DEFAULT_TYPE, reminder_id: int):
    query = update.callback_query
    await query.answer()
    
    reminder = get_reminder_info(reminder_id)
    
    if not reminder:
        await query.edit_message_text(
            "❌ Напоминание не найдено или было удалено.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("К списку", callback_data="back_to_list_0")],
                [InlineKeyboardButton("🔙", callback_data="back_to_start")]
            ])
        )
        return
    
    # Проверяем, принадлежит ли напоминание пользователю
    if query.from_user.id != reminder['user_id']:
        await query.edit_message_text(
            "❌ У вас нет доступа к этому напоминанию.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("К списку", callback_data="back_to_list_0")],
                [InlineKeyboardButton("🔙", callback_data="back_to_start")]
            ])
        )
        return
    
    reminder_time = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S')
    time_str = reminder_time.strftime('%d.%m.%Y %H:%M')
    created_str = datetime.strptime(reminder['created_at'], '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y')
    
    current_time = datetime.now()
    time_diff = reminder_time - current_time
    
    # Статус напоминания
    if reminder['sent']:
        status = "✅ *Выполнено*"
    elif not reminder['is_active']:
        status = "❌ *Неактивно*"
    elif reminder_time < current_time:
        status = "⚠️ *Просрочено*"
    else:
        status = "⏳ *Ожидает*"
        
        days = time_diff.days
        hours = time_diff.seconds // 3600
        minutes = (time_diff.seconds % 3600) // 60
        
        time_left_parts = []
        if days > 0:
            time_left_parts.append(f"{days} д.")
        if hours > 0:
            time_left_parts.append(f"{hours} ч.")
        if minutes > 0:
            time_left_parts.append(f"{minutes} мин.")
        
        time_left = " ".join(time_left_parts) if time_left_parts else "менее минуты"
        status += f"\n⏱️ *Через:* {time_left}"
    
    # Информация о повторении
    repeat_info = ""
    if reminder['repeat_type'] != 'once':
        repeat_info = "\n\n🔄 *Повторение:* "
        if reminder['repeat_type'] == 'daily':
            if reminder['repeat_interval'] == 1:
                repeat_info += "Каждый день"
            else:
                repeat_info += f"Каждые {reminder['repeat_interval']} дня"
        elif reminder['repeat_type'] == 'weekly':
            day_name = DAYS_OF_WEEK[reminder_time.weekday()]
            repeat_info += f"Каждый {day_name}"
        elif reminder['repeat_type'] == 'custom':
            days_list = [DAYS_OF_WEEK[int(d)] for d in reminder['repeat_days'].split(',') if d]
            days_str = ', '.join([d for d in days_list])
            repeat_info += f"По {days_str}"
    
    if reminder['postponed_count'] > 0:
        postponed = f"\n⏰ *Откладывалось:* {reminder['postponed_count']} раз"
    else:
        postponed = ""
    
    response = f"""
💭 *Детали напоминания*

{status}

📝 *Текст:* {reminder['text']}
⏰ *Время:* {time_str}

🌟*Выберите действие:*
    """
    
    keyboard = create_reminder_control_keyboard(reminder_id)
    await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)

# Показать повторяющиеся напоминания
async def show_repeating_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Ищем оригинальные повторяющиеся напоминания
    cursor.execute('''
        SELECT * FROM reminders 
        WHERE user_id = ? 
        AND is_active = 1 
        AND repeat_type != 'once'
        AND original_reminder_id IS NULL
        ORDER BY created_at DESC
    ''', (user_id,))
    
    repeating_reminders = []
    columns = [column[0] for column in cursor.description]
    for row in cursor.fetchall():
        reminder_dict = dict(zip(columns, row))
        repeating_reminders.append(reminder_dict)
    
    conn.close()
    
    if not repeating_reminders:
        await update.message.reply_text(
            "🔄 У вас нет повторяющихся напоминаний.",
            reply_markup=create_main_menu()
        )
        return
    
    response = "🔄 *Повторяющиеся напоминания:*\n\n"
    
    for i, reminder in enumerate(repeating_reminders, 1):
        time_str = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S').strftime('%H:%M')
        
        response += f"{i}. *{reminder['text']}*\n"
        response += f"   🕐 Время: {time_str}\n"
        
        if reminder['repeat_type'] == 'daily':
            if reminder['repeat_interval'] == 1:
                response += f"   🔄 Повтор: Каждый день\n"
            else:
                response += f"   🔄 Повтор: Каждые {reminder['repeat_interval']} дня\n"
        
        elif reminder['repeat_type'] == 'weekly':
            days_list = [DAYS_OF_WEEK[int(d)] for d in reminder['repeat_days'].split(',') if d]
            days_str = ', '.join([d[:3] for d in days_list])
            response += f"   🔄 Повтор: По {days_str}\n"
        
        elif reminder['repeat_type'] == 'custom':
            days_list = [DAYS_OF_WEEK[int(d)] for d in reminder['repeat_days'].split(',') if d]
            days_str = ', '.join([d[:3] for d in days_list])
            response += f"   🔄 Повтор: По {days_str}\n"
        
        response += f"   🆔 ID: {reminder['id']}\n\n"
    
    response += f"📊 *Всего повторяющихся:* {len(repeating_reminders)}"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Весь список", callback_data="back_to_list_0")],
        [InlineKeyboardButton("🔙", callback_data="back_to_start")]
    ])
    
    await update.message.reply_text(response, parse_mode='Markdown', reply_markup=keyboard)

# Показать 3 БЛИЖАЙШИХ напоминания
async def show_three_upcoming_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM reminders 
        WHERE user_id = ? 
        AND is_active = 1 
        AND sent = 0
        ORDER BY reminder_time
    ''', (user_id,))
    
    columns = [column[0] for column in cursor.description]
    all_reminders = []
    for row in cursor.fetchall():
        reminder_dict = dict(zip(columns, row))
        all_reminders.append(reminder_dict)
    
    conn.close()
    
    if not all_reminders:
        await update.message.reply_text("💭 У вас пока нет активных напоминаний.")
        return
    
    current_time = datetime.now()
    upcoming = []
    
    for reminder in all_reminders:
        reminder_time = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S')
        if reminder_time >= current_time:
            upcoming.append(reminder)
    
    if not upcoming:
        await update.message.reply_text("⏰ Нет предстоящих напоминаний.")
        return
    
    upcoming.sort(key=lambda x: datetime.strptime(x['reminder_time'], '%Y-%m-%d %H:%M:%S'))
    nearest = upcoming[:3]
    
    response = "✨ *Три ближайших напоминания:*\n\n"
    
    for i, reminder in enumerate(nearest, 1):
        reminder_time = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S')
        time_str = reminder_time.strftime('%d.%m.%Y %H:%M')
        time_diff = reminder_time - current_time
        
        days = time_diff.days
        hours = time_diff.seconds // 3600
        minutes = (time_diff.seconds % 3600) // 60
        
        time_left_parts = []
        if days > 0:
            time_left_parts.append(f"{days} д.")
        if hours > 0:
            time_left_parts.append(f"{hours} ч.")
        if minutes > 0:
            time_left_parts.append(f"{minutes} мин.")
        
        time_left = " ".join(time_left_parts) if time_left_parts else "менее минуты"
        
        if days == 0 and hours < 1:
            urgency = "🔴"
        elif days == 0 and hours < 3:
            urgency = "🟠"
        else:
            urgency = "🟢"
        
        if reminder['postponed_count'] > 0:
            postponed = f" (отложено {reminder['postponed_count']} раз)"
        else:
            postponed = ""
        
        response += f"{urgency} *{i}. {reminder['text']}*{postponed}\n"
        response += f"   🕐 {time_str}\n"
        response += f"   ⏱️ Через: {time_left}\n\n"
    
    if len(upcoming) > 3:
        response += f"💭 И ещё {len(upcoming) - 3} напоминаний..."
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Весь список", callback_data="back_to_list_0")],
        [InlineKeyboardButton("🔙", callback_data="back_to_start")]
    ])
    
    await update.message.reply_text(response, parse_mode='Markdown', reply_markup=keyboard)

# Парсинг даты и времени
def parse_datetime(text: str) -> datetime:
    current_time = datetime.now()
    text = text.lower().strip()
    
    try:
        if text.startswith('сегодня'):
            time_str = text.replace('сегодня', '').strip()
            if ':' in time_str:
                time_obj = datetime.strptime(time_str, '%H:%M').time()
                result = datetime.combine(current_time.date(), time_obj)
                if result < current_time:
                    result += timedelta(days=1)
                return result
        
        elif text.startswith('завтра'):
            time_str = text.replace('завтра', '').strip()
            time_obj = datetime.strptime(time_str, '%H:%M').time()
            result = datetime.combine(current_time.date() + timedelta(days=1), time_obj)
            return result
        
        elif re.match(r'\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}', text):
            return datetime.strptime(text, '%d.%m.%Y %H:%M')
        
        elif re.match(r'^\d{1,2}:\d{2}$', text):
            time_obj = datetime.strptime(text, '%H:%M').time()
            result = datetime.combine(current_time.date(), time_obj)
            if result < current_time:
                result += timedelta(days=1)
            return result
        
        elif 'через' in text:
            if 'час' in text or 'часа' in text or 'часов' in text:
                matches = re.findall(r'\d+', text)
                if matches:
                    hours = int(matches[0])
                    return current_time + timedelta(hours=hours)
            elif 'минут' in text:
                matches = re.findall(r'\d+', text)
                if matches:
                    minutes = int(matches[0])
                    return current_time + timedelta(minutes=minutes)
            elif 'день' in text or 'дня' in text or 'дней' in text:
                matches = re.findall(r'\d+', text)
                if matches:
                    days = int(matches[0])
                    return current_time + timedelta(days=days)
        
        # Попробуем распознать относительное время
        time_patterns = {
            'через час': timedelta(hours=1),
            'через 30 минут': timedelta(minutes=30),
            'через 15 минут': timedelta(minutes=15),
            'через 5 минут': timedelta(minutes=5),
        }
        
        for pattern, delta in time_patterns.items():
            if pattern in text:
                return current_time + delta
        
    except Exception as e:
        logger.error(f"Ошибка парсинга времени '{text}': {e}")
    
    raise ValueError(f"Не удалось распознать время: '{text}'. Используйте форматы: 'сегодня 20:30', 'завтра 10:00', '25.12.2024 15:45', '15:30', 'через 2 часа', 'через 30 минут'")

# Сохранение напоминания
def save_reminder_to_db(user_id: int, user_name: str, text: str, reminder_time: datetime, 
                        repeat_type: str = 'once', repeat_days: str = '', 
                        repeat_interval: int = 1, original_reminder_id: int = None) -> int:
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    time_str = reminder_time.strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute('''
    INSERT INTO reminders (user_id, user_name, text, reminder_time, created_at,
                          repeat_type, repeat_days, repeat_interval, original_reminder_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, user_name, text, time_str, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
          repeat_type, repeat_days, repeat_interval, original_reminder_id))
    
    reminder_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    logger.info(f"Создано напоминание {reminder_id} для пользователя {user_id}, тип: {repeat_type}")
    return reminder_id

# Обновление напоминания
def update_reminder(reminder_id: int, **kwargs):
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    if 'reminder_time' in kwargs and isinstance(kwargs['reminder_time'], datetime):
        kwargs['reminder_time'] = kwargs['reminder_time'].strftime('%Y-%m-%d %H:%M:%S')
    
    set_clause = ', '.join([f"{key} = ?" for key in kwargs.keys()])
    values = list(kwargs.values())
    values.append(reminder_id)
    
    cursor.execute(f'''
        UPDATE reminders 
        SET {set_clause}
        WHERE id = ?
    ''', values)
    
    conn.commit()
    conn.close()
    
    logger.info(f"Обновлено напоминание {reminder_id}")

# Удаление напоминания
def delete_reminder(reminder_id: int):
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Сначала получаем информацию о напоминании
    cursor.execute('SELECT repeat_type, original_reminder_id FROM reminders WHERE id = ?', (reminder_id,))
    result = cursor.fetchone()
    
    if result:
        repeat_type, original_id = result
        
        # Если это повторяющееся напоминание и оригинальное, удаляем все связанные
        if repeat_type != 'once' and original_id is None:
            cursor.execute('DELETE FROM reminders WHERE original_reminder_id = ?', (reminder_id,))
        
        # Удаляем само напоминание
        cursor.execute('DELETE FROM reminders WHERE id = ?', (reminder_id,))
    
    conn.commit()
    conn.close()
    
    logger.info(f"Удалено напоминание {reminder_id}")
    return True

# Обновление времени напоминания (откладывание)
def postpone_reminder(reminder_id: int, minutes: int):
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('SELECT reminder_time FROM reminders WHERE id = ?', (reminder_id,))
    result = cursor.fetchone()
    
    if result:
        old_time = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
        new_time = old_time + timedelta(minutes=minutes)
        new_time_str = new_time.strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute('''
            UPDATE reminders 
            SET reminder_time = ?, sent = 0, postponed_count = postponed_count + 1 
            WHERE id = ?
        ''', (new_time_str, reminder_id))
        
        conn.commit()
        conn.close()
        return new_time
    
    conn.close()
    return None

# Отложить на завтра
def postpone_to_tomorrow(reminder_id: int):
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('SELECT reminder_time FROM reminders WHERE id = ?', (reminder_id,))
    result = cursor.fetchone()
    
    if result:
        old_time = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
        new_time = old_time + timedelta(days=1)
        new_time_str = new_time.strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute('''
            UPDATE reminders 
            SET reminder_time = ?, sent = 0, postponed_count = postponed_count + 1 
            WHERE id = ?
        ''', (new_time_str, reminder_id))
        
        conn.commit()
        conn.close()
        return new_time
    
    conn.close()
    return None

# Пометить как выполненное
def mark_as_done(reminder_id: int):
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE reminders 
        SET sent = 1, is_active = 0 
        WHERE id = ?
    ''', (reminder_id,))
    
    conn.commit()
    conn.close()
    
    logger.info(f"Напоминание {reminder_id} помечено как выполненное")

# Получить информацию о напоминании
def get_reminder_info(reminder_id: int):
    conn = sqlite3.connect('reminders.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM reminders WHERE id = ?', (reminder_id,))
    reminder = cursor.fetchone()
    
    conn.close()
    
    if reminder:
        return dict(reminder)
    return None

# Создание напоминания
async def create_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reminder_step'] = 'waiting_text'
    
    text = """
💭 *Создание напоминания*

Введите текст напоминания:
    """
    
    await update.message.reply_text(text, parse_mode='Markdown')

# Обработка текста напоминания
async def handle_reminder_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('reminder_step') == 'waiting_text':
        text = update.message.text.strip()
        
        if len(text) > 500:
            await update.message.reply_text("❌ Текст слишком длинный. Максимум 500 символов.")
            return
        
        context.user_data['reminder_text'] = text
        context.user_data['reminder_step'] = 'waiting_date'
        
        response = f"""
💭 Текст: *{text}*

Теперь введите дату и время напоминания:

🌟 *Форматы даты:*
• Сегодня 20:30
• Завтра 10:00
• 25.12.2024 15:45
• 15:30 (если время уже прошло, будет на завтра)
• через 2 часа
• через 30 минут
• через 1 день
        """
        
        await update.message.reply_text(response, parse_mode='Markdown')

# Обработка даты и времени
async def handle_reminder_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('reminder_step') == 'waiting_date':
        try:
            time_text = update.message.text.strip()
            reminder_time = parse_datetime(time_text)
            
            current_time = datetime.now()
            if reminder_time <= current_time:
                await update.message.reply_text("❌ Время должно быть в будущем! Пожалуйста, укажите будущее время.")
                return
            
            context.user_data['reminder_time'] = reminder_time
            context.user_data['reminder_step'] = 'waiting_repeat'
            
            time_str = reminder_time.strftime('%d.%m.%Y %H:%M')
            
            response = f"""
💭 Текст: *{context.user_data['reminder_text']}*
🌟 Время: *{time_str}*

Теперь выберите тип повторения:

📌 *Один раз* - напоминание придет один раз
📅 *Ежедневно* - каждый день в это время
🗓️ *Еженедельно* - каждую неделю в этот день
📆 *Выбрать дни* - выбрать конкретные дни недели

Выберите тип повторения:
            """
            
            keyboard = create_repeat_keyboard()
            await update.message.reply_text(response, parse_mode='Markdown', reply_markup=keyboard)
            
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}\n\nПопробуйте еще раз:")
        except Exception as e:
            logger.error(f"Ошибка создания напоминания: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")

# Команда помощи
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
💭 *Помощь по боту*

*Основные кнопки:*
• Создать напоминание - добавить новое напоминание
• Мои напоминания - список всех напоминаний с кнопками
• Ближайшие - 3 САМЫХ БЛИЖАЙШИХ напоминания
• 🔄 - все повторяющиеся напоминания

*Управление напоминаниями:*
📝 *Изменить текст* - изменить текст напоминания
⏰ *Изменить время* - изменить дату и время
🔄 *Изменить повторение* - изменить настройки повторения
❌ *Удалить* - удалить напоминание
✅ *Выполнить сейчас* - отметить как выполненное
⏰ *Отложить* - отложить на время

*Форматы времени:*
• Сегодня 20:30
• Завтра 10:00
• 25.12.2024 15:45
• 15:30 (автоматически на завтра если время прошло)
• через 2 часа
• через 30 минут
• через 1 день

*Важно:*
🌟 Бот работает 24/7
🌟 Уведомления приходят автоматически
🌟 Все напоминания хранятся в базе данных
    """
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

# Обработка callback-кнопок
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    user_id = query.from_user.id
    
    # Обработка возврата в начало
    if callback_data == 'back_to_start':
        welcome_text = f"""
💭 Возвращаемся в главное меню...

Используйте кнопки ниже для навигации:
        """
        
        keyboard = create_main_menu()
        
        # Нельзя редактировать сообщение с reply_markup (обычной клавиатурой) в inline-сообщении
        # Поэтому просто отправляем новое сообщение
        await context.bot.send_message(
            chat_id=user_id,
            text=welcome_text,
            reply_markup=keyboard
        )
        return
    
    # Обработка создания нового напоминания
    elif callback_data == 'create_new':
        context.user_data['reminder_step'] = 'waiting_text'
        
        text = """
💭 *Создание напоминания*

Введите текст напоминания:
        """
        
        await query.edit_message_text(text, parse_mode='Markdown')
        return
    
    # Обработка возврата к списку
    elif callback_data.startswith('back_to_list_'):
        page = int(callback_data.split('_')[-1])
        await show_reminders_list(update, context, page)
        return
    
    # Обработка навигации по страницам списка
    elif callback_data.startswith('list_page_'):
        if callback_data == 'list_page_current':
            await query.answer(f"Текущая страница", show_alert=False)
            return
        
        page = int(callback_data.split('_')[-1])
        await show_reminders_list(update, context, page)
        return
    
    # Обработка просмотра напоминания
    elif callback_data.startswith('view_'):
        reminder_id = int(callback_data.split('_')[1])
        await show_reminder_details(update, context, reminder_id)
        return
    
    # Обработка удаления напоминания (подтверждение)
    elif callback_data.startswith('delete_confirm_'):
        reminder_id = int(callback_data.split('_')[2])
        
        response = """
💭 *Подтверждение удаления*

Вы уверены, что хотите удалить это напоминание?

❌ Это действие нельзя отменить!
        """
        
        keyboard = create_delete_confirm_keyboard(reminder_id)
        await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка подтверждения удаления
    elif callback_data.startswith('delete_yes_'):
        reminder_id = int(callback_data.split('_')[2])
        reminder = get_reminder_info(reminder_id)
        
        if reminder and reminder['user_id'] == user_id:
            delete_reminder(reminder_id)
            
            response = f"""
💭 *Напоминание удалено!*

📝 {reminder['text']}
⏰ {datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')}
            """
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 К списку", callback_data="back_to_list_0")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")]
            ])
            
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка "Выполнить сейчас"
    elif callback_data.startswith('done_now_'):
        reminder_id = int(callback_data.split('_')[2])
        reminder = get_reminder_info(reminder_id)
        
        if reminder and reminder['user_id'] == user_id:
            mark_as_done(reminder_id)
            
            response = f"""
💭 *Напоминание выполнено!*

📝 {reminder['text']}
⏰ {datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')}
            """
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 К списку", callback_data="back_to_list_0")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")]
            ])
            
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка изменения текста
    elif callback_data.startswith('edit_text_'):
        reminder_id = int(callback_data.split('_')[2])
        context.user_data['edit_reminder_id'] = reminder_id
        context.user_data['edit_step'] = 'waiting_new_text'
        
        response = """
💭 *Изменение текста напоминания*

Введите новый текст напоминания:
        """
        
        await query.edit_message_text(response, parse_mode='Markdown')
        return
    
    # Обработка изменения времени
    elif callback_data.startswith('edit_time_'):
        reminder_id = int(callback_data.split('_')[2])
        context.user_data['edit_reminder_id'] = reminder_id
        context.user_data['edit_step'] = 'waiting_new_time'
        
        response = """
💭 *Изменение времени напоминания*

Введите новое время напоминания:

💫 *Форматы даты:*
• Сегодня 20:30
• Завтра 10:00
• 25.12.2024 15:45
• 15:30
• через 2 часа
• через 30 минут
        """
        
        await query.edit_message_text(response, parse_mode='Markdown')
        return
    
    # Обработка изменения повторения
    elif callback_data.startswith('edit_repeat_'):
        reminder_id = int(callback_data.split('_')[2])
        context.user_data['edit_reminder_id'] = reminder_id
        
        response = """
🔄 *Изменение повторения напоминания*

Выберите новый тип повторения:
        """
        
        keyboard = create_repeat_keyboard(reminder_id)
        await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка выбора типа повторения при редактировании
    elif callback_data.startswith('edit_repeat_type_'):
        parts = callback_data.split('_')
        reminder_id = int(parts[3])
        repeat_type = parts[4]
        
        context.user_data['edit_reminder_id'] = reminder_id
        context.user_data['edit_repeat_type'] = repeat_type
        
        if repeat_type == 'once':
            # Просто обновляем напоминание
            update_reminder(reminder_id, repeat_type='once', repeat_days='', repeat_interval=1)
            
            response = f"""
💭 *Повторение изменено!*

Теперь это разовое напоминание.
            """
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("К деталям", callback_data=f"view_{reminder_id}")],
                [InlineKeyboardButton("🔙", callback_data="back_to_start")]
            ])
            
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        
        elif repeat_type == 'daily':
            # Показываем выбор интервала
            response = """
💭 *Ежедневное повторение*

Выберите интервал повторения:
            """
            
            keyboard = create_daily_interval_keyboard(reminder_id)
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        
        elif repeat_type == 'weekly':
            # Устанавливаем повторение на тот же день недели
            reminder = get_reminder_info(reminder_id)
            if reminder:
                reminder_time = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S')
                weekday = reminder_time.weekday()
                update_reminder(reminder_id, repeat_type='weekly', repeat_days=str(weekday), repeat_interval=1)
                
                response = f"""
💭 *Повторение изменено!*

Теперь это еженедельное напоминание.
Повторяется каждый {DAYS_OF_WEEK[weekday]}.
                """
                
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("К деталям", callback_data=f"view_{reminder_id}")],
                    [InlineKeyboardButton("🔙", callback_data="back_to_start")]
                ])
                
                await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        
        elif repeat_type == 'custom':
            # Показываем выбор дней
            context.user_data['edit_selected_days'] = []
            
            response = """
💭 *Выбор дней недели*

Выберите дни недели для напоминания:
Нажмите на день, чтобы выбрать/отменить.
Когда закончите, нажмите "✅ Готово"
            """
            
            keyboard = create_days_keyboard([], reminder_id)
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка выбора интервала при редактировании
    elif callback_data.startswith('edit_interval_'):
        parts = callback_data.split('_')
        reminder_id = int(parts[2])
        interval = int(parts[3])
        
        update_reminder(reminder_id, repeat_type='daily', repeat_interval=interval)
        
        if interval == 1:
            interval_text = "каждый день"
        elif interval == 7:
            interval_text = "раз в неделю"
        elif interval == 14:
            interval_text = "раз в 2 недели"
        elif interval == 30:
            interval_text = "раз в месяц"
        else:
            interval_text = f"каждые {interval} дня"
        
        response = f"""
💭 *Повторение изменено!*

Теперь это ежедневное напоминание.
Повторяется {interval_text}.
        """
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("К деталям", callback_data=f"view_{reminder_id}")],
            [InlineKeyboardButton("🔙", callback_data="back_to_start")]
        ])
        
        await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка выбора дней при редактировании
    elif callback_data.startswith('edit_day_'):
        parts = callback_data.split('_')
        reminder_id = int(parts[2])
        day_num = int(parts[3])
        
        selected_days = context.user_data.get('edit_selected_days', [])
        
        if day_num in selected_days:
            selected_days.remove(day_num)
        else:
            selected_days.append(day_num)
        
        context.user_data['edit_selected_days'] = selected_days
        
        # Обновляем клавиатуру
        keyboard = create_days_keyboard(selected_days, reminder_id)
        await query.edit_message_text(query.message.text, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка завершения выбора дней при редактировании
    elif callback_data.startswith('edit_days_done_'):
        reminder_id = int(callback_data.split('_')[3])
        selected_days = context.user_data.get('edit_selected_days', [])
        
        if not selected_days:
            await query.answer("❌ Нужно выбрать хотя бы один день!", show_alert=True)
            return
        
        # Сортируем дни
        selected_days.sort()
        repeat_days = ','.join(map(str, selected_days))
        
        update_reminder(reminder_id, repeat_type='custom', repeat_days=repeat_days, repeat_interval=1)
        
        days_list = [DAYS_OF_WEEK[d] for d in selected_days]
        days_str = ', '.join([d for d in days_list])
        
        response = f"""
💭 *Повторение изменено!*

Теперь напоминание повторяется по выбранным дням:
{days_str}
        """
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 К деталям", callback_data=f"view_{reminder_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")]
        ])
        
        await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка выбора типа повторения (создание нового)
    elif callback_data.startswith('repeat_'):
        if context.user_data.get('reminder_step') == 'waiting_repeat':
            repeat_type = callback_data.split('_')[1]
            
            if repeat_type == 'skip':
                # Пропускаем выбор повторения
                await complete_reminder_creation(query, context, user_id)
            
            elif repeat_type == 'once':
                context.user_data['repeat_type'] = 'once'
                await complete_reminder_creation(query, context, user_id)
            
            elif repeat_type == 'daily':
                context.user_data['repeat_type'] = 'daily'
                context.user_data['reminder_step'] = 'waiting_interval'
                
                response = """
💭 *Ежедневное напоминание*

Выберите интервал повторения:
                """
                
                keyboard = create_daily_interval_keyboard()
                await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
            
            elif repeat_type == 'weekly':
                context.user_data['repeat_type'] = 'weekly'
                context.user_data['repeat_days'] = str(context.user_data['reminder_time'].weekday())
                await complete_reminder_creation(query, context, user_id)
            
            elif repeat_type == 'custom':
                context.user_data['repeat_type'] = 'custom'
                context.user_data['selected_days'] = []
                context.user_data['reminder_step'] = 'waiting_days'
                
                response = """
💭 *Выбор дней недели*

Выберите дни недели для напоминания:
Нажмите на день, чтобы выбрать/отменить.
Когда закончите, нажмите "✅ Готово"
                """
                
                keyboard = create_days_keyboard([])
                await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка выбора интервала (создание нового)
    elif callback_data.startswith('interval_'):
        if callback_data == 'interval_back':
            # Возвращаемся к выбору типа повторения
            context.user_data['reminder_step'] = 'waiting_repeat'
            
            time_str = context.user_data['reminder_time'].strftime('%d.%m.%Y %H:%M')
            
            response = f"""
📝 Текст: *{context.user_data['reminder_text']}*
⏰ Время: *{time_str}*

Теперь выберите тип повторения:
            """
            
            keyboard = create_repeat_keyboard()
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        
        else:
            interval = int(callback_data.split('_')[1])
            context.user_data['repeat_interval'] = interval
            await complete_reminder_creation(query, context, user_id)
        return
    
    # Обработка выбора дней (создание нового)
    elif callback_data.startswith('day_'):
        if context.user_data.get('reminder_step') == 'waiting_days':
            day_num = int(callback_data.split('_')[1])
            selected_days = context.user_data.get('selected_days', [])
            
            if day_num in selected_days:
                selected_days.remove(day_num)
            else:
                selected_days.append(day_num)
            
            context.user_data['selected_days'] = selected_days
            
            # Обновляем клавиатуру
            keyboard = create_days_keyboard(selected_days)
            await query.edit_message_text(query.message.text, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка завершения выбора дней (создание нового)
    elif callback_data in ['days_done', 'days_cancel']:
        if callback_data == 'days_done':
            selected_days = context.user_data.get('selected_days', [])
            if not selected_days:
                await query.answer("❌ Нужно выбрать хотя бы один день!", show_alert=True)
                return
            
            # Сортируем дни
            selected_days.sort()
            context.user_data['repeat_days'] = ','.join(map(str, selected_days))
            await complete_reminder_creation(query, context, user_id)
        
        else:  # days_cancel
            # Возвращаемся к выбору типа повторения
            context.user_data['reminder_step'] = 'waiting_repeat'
            context.user_data.pop('selected_days', None)
            
            time_str = context.user_data['reminder_time'].strftime('%d.%m.%Y %H:%M')
            
            response = f"""
📝 Текст: *{context.user_data['reminder_text']}*
⏰ Время: *{time_str}*

Теперь выберите тип повторения:
            """
            
            keyboard = create_repeat_keyboard()
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка кнопки "Выполнено" в уведомлении
    elif callback_data.startswith('done_'):
        reminder_id = int(callback_data.split('_')[1])
        reminder = get_reminder_info(reminder_id)
        
        if reminder and reminder['user_id'] == user_id:
            mark_as_done(reminder_id)
            
            reminder_time = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S')
            time_str = reminder_time.strftime('%d.%m.%Y %H:%M')
            
            response = f"""
💭 *выполнено!*

📝 {reminder['text']}
⏰ {time_str}

🌟 Напоминание выполнено и архивировано.
            """
            
            await query.edit_message_text(response, parse_mode='Markdown')
            
            # Отправляем подтверждение
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ Напоминание «{reminder['text']}» отмечено как выполненное!",
                reply_markup=create_main_menu()
            )
        return
    
    # Обработка кнопки "Отложить" (меню)
    elif callback_data.startswith('snooze_menu_'):
        reminder_id = int(callback_data.split('_')[2])
        reminder = get_reminder_info(reminder_id)
        
        if reminder and reminder['user_id'] == user_id:
            reminder_time = datetime.strptime(reminder['reminder_time'], '%Y-%m-%d %H:%M:%S')
            time_str = reminder_time.strftime('%d.%m.%Y %H:%M')
            
            response = f"""
⏰ *ОТЛОЖИТЬ НАПОМИНАНИЕ*

📝 {reminder['text']}
💫 Текущее время: {time_str}

Выберите, на сколько отложить:
            """
            
            keyboard = create_snooze_options_keyboard(reminder_id)
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)
        return
    
    # Обработка выбора времени откладывания
    elif callback_data.startswith('snooze_'):
        parts = callback_data.split('_')
        if len(parts) == 3:
            time_str = parts[1]
            reminder_id = int(parts[2])
            
            reminder = get_reminder_info(reminder_id)
            
            if reminder and reminder['user_id'] == user_id:
                if time_str == 'tomorrow':
                    new_time = postpone_to_tomorrow(reminder_id)
                    time_delta = "завтра"
                else:
                    minutes = int(time_str)
                    new_time = postpone_reminder(reminder_id, minutes)
                    
                    if minutes >= 60:
                        hours = minutes // 60
                        time_delta = f"{hours} час{'а' if 2 <= hours % 10 <= 4 and (hours % 100 < 10 or hours % 100 > 20) else '' if hours % 10 == 1 else 'ов'}"
                    else:
                        time_delta = f"{minutes} минут{'у' if minutes % 10 == 1 and minutes % 100 != 11 else 'ы' if 2 <= minutes % 10 <= 4 and (minutes % 100 < 10 or minutes % 100 > 20) else ''}"
                
                if new_time:
                    new_time_str = new_time.strftime('%d.%m.%Y %H:%M')
                    
                    response = f"""
💭 *напоминание отложено*

📝 {reminder['text']}
⏰ Новое время: {new_time_str}
⏱️ Отложено на: {time_delta}

Бот напомнит в новое время! 🌟
                    """
                    
                    await query.edit_message_text(response, parse_mode='Markdown')
                    
                    # Отправляем подтверждение
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"⏰ Напоминание «{reminder['text']}» отложено на {time_delta}!\nНовое время: {new_time_str}",
                        reply_markup=create_main_menu()
                    )
        return

# Завершение создания напоминания
async def complete_reminder_creation(query, context, user_id):
    user = query.from_user
    text = context.user_data['reminder_text']
    reminder_time = context.user_data['reminder_time']
    
    repeat_type = context.user_data.get('repeat_type', 'once')
    repeat_days = context.user_data.get('repeat_days', '')
    repeat_interval = context.user_data.get('repeat_interval', 1)
    
    reminder_id = save_reminder_to_db(
        user.id, user.first_name, text, reminder_time,
        repeat_type, repeat_days, repeat_interval
    )
    
    time_str = reminder_time.strftime('%d.%m.%Y %H:%M')
    time_diff = reminder_time - datetime.now()
    
    days = time_diff.days
    hours = time_diff.seconds // 3600
    minutes = (time_diff.seconds % 3600) // 60
    
    time_left_parts = []
    if days > 0:
        time_left_parts.append(f"{days} д.")
    if hours > 0:
        time_left_parts.append(f"{hours} ч.")
    if minutes > 0:
        time_left_parts.append(f"{minutes} мин.")
    
    time_left = " ".join(time_left_parts) if time_left_parts else "менее минуты"
    
    # Добавляем информацию о повторении
    repeat_info = ""
    if repeat_type == 'daily':
        if repeat_interval == 1:
            repeat_info = "\n🔄 *Повторение:* Каждый день"
        else:
            repeat_info = f"\n🔄 *Повторение:* Каждые {repeat_interval} дня"
    
    elif repeat_type == 'weekly':
        day_name = DAYS_OF_WEEK[reminder_time.weekday()]
        repeat_info = f"\n🔄 *Повторение:* Каждый {day_name}"
    
    elif repeat_type == 'custom':
        days_list = [DAYS_OF_WEEK[int(d)] for d in repeat_days.split(',') if d]
        days_str = ', '.join([d for d in days_list])
        repeat_info = f"\n🔄 *Повторение:* По {days_str}"
    
    response = f"""
💭 *напоминание создано успешно!*

📝 *Текст:* {text}
⏰ *Время:* {time_str}
⏱️ *Через:* {time_left}{repeat_info} 
    """
    
    # Очищаем временные данные
    for key in ['reminder_step', 'reminder_text', 'reminder_time', 
                'repeat_type', 'repeat_days', 'repeat_interval', 'selected_days']:
        context.user_data.pop(key, None)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("К списку", callback_data="back_to_list_0")],
        [InlineKeyboardButton("🔙", callback_data="back_to_start")]
    ])
    
    await query.edit_message_text(response, parse_mode='Markdown', reply_markup=keyboard)

# Обработка редактирования текста
async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('edit_step') == 'waiting_new_text':
        new_text = update.message.text.strip()
        reminder_id = context.user_data.get('edit_reminder_id')
        
        if len(new_text) > 500:
            await update.message.reply_text("❌ Текст слишком длинный. Максимум 500 символов.")
            return
        
        update_reminder(reminder_id, text=new_text)
        
        # Очищаем временные данные
        context.user_data.pop('edit_step', None)
        context.user_data.pop('edit_reminder_id', None)
        
        await update.message.reply_text(
            f"💭 Текст напоминания изменен на: {new_text}",
            reply_markup=create_main_menu()
        )

# Обработка редактирования времени
async def handle_edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('edit_step') == 'waiting_new_time':
        try:
            time_text = update.message.text.strip()
            new_time = parse_datetime(time_text)
            
            current_time = datetime.now()
            if new_time <= current_time:
                await update.message.reply_text("❌ Время должно быть в будущем! Пожалуйста, укажите будущее время.")
                return
            
            reminder_id = context.user_data.get('edit_reminder_id')
            update_reminder(reminder_id, reminder_time=new_time)
            
            time_str = new_time.strftime('%d.%m.%Y %H:%M')
            
            # Очищаем временные данные
            context.user_data.pop('edit_step', None)
            context.user_data.pop('edit_reminder_id', None)
            
            await update.message.reply_text(
                f"💭 Время напоминания изменено на: {time_str}",
                reply_markup=create_main_menu()
            )
            
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}\n\nПопробуйте еще раз:")
        except Exception as e:
            logger.error(f"Ошибка изменения времени: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")

# Функция проверки и отправки напоминаний
async def async_reminder_checker(bot_token: str):
    """Асинхронная проверка напоминаний"""
    from telegram import Bot
    
    bot = Bot(token=bot_token)
    
    while True:
        try:
            conn = sqlite3.connect('reminders.db', check_same_thread=False)
            cursor = conn.cursor()
            
            current_time = datetime.now()
            time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')
            
            cursor.execute('''
                SELECT id, user_id, text, reminder_time, user_name, postponed_count, repeat_type
                FROM reminders 
                WHERE reminder_time <= ? 
                AND is_active = 1 
                AND sent = 0
            ''', (time_str,))
            
            reminders = cursor.fetchall()
            
            sent_count = 0
            
            for reminder_id, user_id, text, reminder_time_str, user_name, postponed_count, repeat_type in reminders:
                try:
                    reminder_time = datetime.strptime(reminder_time_str, '%Y-%m-%d %H:%M:%S')
                    time_formatted = reminder_time.strftime('%d.%m.%Y %H:%M')
                    
                    if postponed_count > 0:
                        postponed = f"\n⏰ Откладывалось: {postponed_count} раз"
                    else:
                        postponed = ""
                    
                    repeat_info = ""
                    if repeat_type != 'once':
                        repeat_info = "\n🔄 *Повторяющееся напоминание*"
                    
                    message = f"""
💭 *напоминание*{repeat_info}

📝 {text}
⏰ {time_formatted}{postponed}

Выберите действие:
                    """
                    
                    keyboard = create_reminder_keyboard(reminder_id)
                    
                    await bot.send_message(
                        chat_id=user_id, 
                        text=message, 
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                    
                    cursor.execute(
                        'UPDATE reminders SET sent = 1 WHERE id = ?',
                        (reminder_id,)
                    )
                    
                    sent_count += 1
                    logger.info(f"Отправлено напоминание {reminder_id} пользователю {user_id}")
                    
                    await asyncio.sleep(0.1)  # Короткая задержка
                    
                except Exception as e:
                    logger.error(f"Ошибка отправки напоминания {reminder_id}: {e}")
                    
                    if "Forbidden" in str(e) or "blocked" in str(e).lower():
                        cursor.execute(
                            'UPDATE reminders SET is_active = 0 WHERE id = ?',
                            (reminder_id,)
                        )
            
            conn.commit()
            
            # Очищаем старые выполненные напоминания
            month_ago = (current_time - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('DELETE FROM reminders WHERE sent = 1 AND is_active = 0 AND reminder_time < ?', (month_ago,))
            deleted_count = cursor.rowcount
            
            if deleted_count > 0:
                logger.info(f"Удалено {deleted_count} старых напоминаний")
                conn.commit()
            
            conn.close()
            
            if sent_count > 0:
                logger.info(f"Отправлено {sent_count} напоминаний")
            
            # Интервал проверки (10 секунд)
            await asyncio.sleep(10)
            
        except Exception as e:
            logger.error(f"Ошибка в reminder_checker_loop: {e}")
            # При ошибке ждем дольше
            await asyncio.sleep(60)

# Обработка текстовых сообщений
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    
    # Обработка редактирования
    if context.user_data.get('edit_step') == 'waiting_new_text':
        await handle_edit_text(update, context)
        return
    
    if context.user_data.get('edit_step') == 'waiting_new_time':
        await handle_edit_time(update, context)
        return
    
    # Основные команды
    if user_text == "Создать напоминание":
        await create_reminder(update, context)
    elif user_text == "Мои напоминания":
        await show_reminders_list(update, context)
    elif user_text == "Ближайшие":
        await show_three_upcoming_reminders(update, context)
    elif user_text == "🔄":
        await show_repeating_reminders(update, context)
    elif user_text == "Помощь":
        await help_command(update, context)
    
    # Обработка шагов создания напоминания
    elif context.user_data.get('reminder_step') == 'waiting_text':
        await handle_reminder_text(update, context)
    elif context.user_data.get('reminder_step') == 'waiting_date':
        await handle_reminder_datetime(update, context)
    else:
        await update.message.reply_text(
            "🤔 Я не понял ваше сообщение. Используйте кнопки меню или команды.",
            reply_markup=create_main_menu()
        )

# Основная функция запуска бота
async def main_async():
    """Асинхронный запуск бота"""
    try:
        # Проверяем токен
        if not BOT_TOKEN or BOT_TOKEN == '8543266583:AAFMsPSWjMW1ZqMwE_B2VqvJsyWUi35T1vM':
            logger.error("❌ Не установлен токен бота!")
            logger.error("Установите переменную окружения BOT_TOKEN_REMINDER в Railway")
            return
        
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Добавляем обработчики команд
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("list", show_reminders_list))
        application.add_handler(CommandHandler("reminders", show_reminders_list))
        application.add_handler(CommandHandler("upcoming", show_three_upcoming_reminders))
        application.add_handler(CommandHandler("repeating", show_repeating_reminders))
        
        # Добавляем обработчик callback-кнопок
        application.add_handler(CallbackQueryHandler(handle_callback_query))
        
        # Обработчик текстовых сообщений
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
        
        # Запускаем фоновую проверку напоминаний как асинхронную задачу
        asyncio.create_task(async_reminder_checker(BOT_TOKEN))
        
        logger.info("=" * 50)
        logger.info("🤖 Бот-напоминалка запущен!")
        logger.info(f"✅ Токен: {BOT_TOKEN[:10]}...")
        logger.info("✅ Система с интерактивным списком активна")
        logger.info("📋 Управление напоминаниями через кнопки")
        logger.info("🔔 Уведомления будут приходить автоматически")
        logger.info("⏰ Проверка каждые 10 секунд")
        logger.info("=" * 50)
        
        await application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
        logger.error(f"❌ Критическая ошибка: {e}")

def main():
    """Точка входа для Railway"""
    asyncio.run(main_async())

if __name__ == '__main__':
    main()

app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Telegram Reminder Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    # Запускаем Flask в отдельном потоке
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем бота
    asyncio.run(main_async())