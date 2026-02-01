import asyncio
import os
import re
from datetime import datetime, date, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv
from sqlalchemy import select

from database import AsyncSessionLocal
from models import Task
from llm import parse_task

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ================= STATES =================

STATE_CREATING = "CREATING"
STATE_EDITING = "EDITING"
STATE_WAITING_DATE = "WAITING_DATE"
STATE_WAITING_TIME = "WAITING_TIME"

# Храним состояние пользователя: {"state": "STATE", "data": {...}}
user_states: dict[int, dict] = {}

# ================= УЛУЧШЕННЫЕ HELPERS =================

ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def normalize_date(value: str | None) -> str | None:
    """Приводит дату к строке ISO формата"""
    if not value:
        return None

    value = value.lower().strip()
    today = date.today()

    # Обработка русских слов
    if value in ["сегодня", "today"]:
        return today.isoformat()

    if value in ["завтра", "tomorrow"]:
        return (today + timedelta(days=1)).isoformat()

    if value == "послезавтра":
        return (today + timedelta(days=2)).isoformat()

    # Дни недели
    days_map = {
        "понедельник": 0, "monday": 0,
        "вторник": 1, "tuesday": 1,
        "среду": 2, "среда": 2, "wednesday": 2,
        "четверг": 3, "thursday": 3,
        "пятницу": 4, "пятница": 4, "friday": 4,
        "субботу": 5, "суббота": 5, "saturday": 5,
        "воскресенье": 6, "sunday": 6
    }

    if value in days_map:
        target_weekday = days_map[value]
        current_weekday = today.weekday()
        days_ahead = target_weekday - current_weekday
        if days_ahead <= 0:
            days_ahead += 7
        return (today + timedelta(days=days_ahead)).isoformat()

    # Если это дата в формате YYYY-MM-DD
    if ISO_DATE_RE.fullmatch(value):
        try:
            parsed = date.fromisoformat(value)
            if parsed.year < today.year:
                parsed = parsed.replace(year=today.year)
            return parsed.isoformat()
        except ValueError:
            return None

    # Попробуем другие форматы дат
    formats = [
        ("%d.%m.%Y", r"\d{2}\.\d{2}\.\d{4}"),
        ("%d-%m-%Y", r"\d{2}-\d{2}-\d{4}"),
        ("%d/%m/%Y", r"\d{2}/\d{2}/\d{4}"),
        ("%d.%m.%y", r"\d{2}\.\d{2}\.\d{2}"),
    ]

    for fmt, pattern in formats:
        if re.match(pattern, value):
            try:
                parsed = datetime.strptime(value, fmt).date()
                if parsed.year < today.year:
                    parsed = parsed.replace(year=today.year)
                return parsed.isoformat()
            except ValueError:
                continue

    return None


def normalize_time(value: str | None) -> Optional[tuple[int, int]]:
    """Приводит время к (часы, минуты)"""
    if not value:
        return None

    value = value.strip()

    # Пробуем разные форматы
    formats = ["%H:%M", "%H.%M", "%I:%M %p", "%I.%M %p"]

    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.hour, dt.minute
        except ValueError:
            continue

    # Ищем время с помощью regex
    time_match = re.search(r"(\d{1,2})[.:](\d{2})", value)
    if time_match:
        hours = int(time_match.group(1))
        minutes = int(time_match.group(2))

        # Проверяем AM/PM
        if "pm" in value.lower() and hours < 12:
            hours += 12
        elif "am" in value.lower() and hours == 12:
            hours = 0

        return hours, minutes

    # Если просто число (часы)
    if value.isdigit() and 0 <= int(value) <= 23:
        return int(value), 0

    return None


def parse_relative_time(text: str) -> datetime | None:
    """Парсит относительное время"""
    text = text.lower()

    patterns = [
        (r"через\s+(\d+)\s*минут[уы]?", "minutes"),
        (r"через\s+(\d+)\s*час[аов]?", "hours"),
        (r"через\s+(\d+)\s*день|дн[яей]", "days"),
    ]

    for pattern, unit in patterns:
        match = re.search(pattern, text)
        if match:
            amount = int(match.group(1))
            now = datetime.now()

            if unit == "minutes":
                return now + timedelta(minutes=amount)
            elif unit == "hours":
                return now + timedelta(hours=amount)
            elif unit == "days":
                return now + timedelta(days=amount)

    # Особые случаи
    special_cases = {
        "через минуту": timedelta(minutes=1),
        "через час": timedelta(hours=1),
        "через день": timedelta(days=1),
    }

    for phrase, delta in special_cases.items():
        if phrase in text:
            return datetime.now() + delta

    return None


def normalize_title(title: str | None, description: str | None) -> str:
    """Создает заголовок"""
    if title and title.lower() not in ["напоминание", "reminder", "задача", "task"]:
        return title[:100]

    if description:
        words = description.split()[:5]
        return " ".join(words).capitalize()

    return "Напоминание"


# ================= ОБНОВЛЕННЫЙ MESSAGE HANDLER =================

async def message_handler(message: Message):
    user_id = message.from_user.id
    state_data = user_states.get(user_id)

    # Если нет состояния, просто игнорируем или показываем помощь
    if not state_data:
        return

    state = state_data.get("state")
    text = message.text.strip()

    print(f"DEBUG: user_id={user_id}, state={state}, text={text}")

    # Обработка отмены в любом состоянии
    if text.lower() == "/cancel":
        user_states.pop(user_id, None)
        await message.answer("❌ Действие отменено")
        return

    # Обработка состояний ожидания
    if state == STATE_WAITING_DATE:
        # Пользователь отвечает на вопрос "Когда напомнить?"
        date_norm = normalize_date(text)

        if not date_norm:
            await message.answer(
                "❌ Не понял дату. Попробуй еще раз:\n"
                "• 'сегодня', 'завтра', 'послезавтра'\n"
                "• 'понедельник', 'вторник'...\n"
                "• '25.02.2026', '2026-02-25'"
            )
            return

        # Сохраняем дату
        state_data["date"] = date_norm
        state_data["state"] = STATE_WAITING_TIME

        # Спрашиваем время
        await message.answer(
            f"📅 Отлично! Дата: {date_norm}\n\n"
            "⏰ Во сколько напомнить?\n"
            "• 15:00\n"
            "• 18.30\n"
            "• или просто '10' (это 10:00)"
        )
        return

    elif state == STATE_WAITING_TIME:
        # Пользователь отвечает на вопрос "Во сколько?"
        time_tuple = normalize_time(text)

        if not time_tuple:
            await message.answer(
                "❌ Не понял время. Попробуй еще раз:\n"
                "• '15:00', '18.30'\n"
                "• '3pm' (15:00)\n"
                "• или просто число от 0 до 23"
            )
            return

        hours, minutes = time_tuple

        # Сохраняем время
        state_data["time"] = f"{hours:02d}:{minutes:02d}"
        state_data["state"] = STATE_CREATING

        # Переходим к созданию задачи
        await create_or_edit_task(message, state_data)
        return

    # Обработка начального создания/редактирования
    elif state in [STATE_CREATING, STATE_EDITING]:
        await handle_task_creation(message, state_data)
        return


async def handle_task_creation(message: Message, state_data: dict):
    """Обрабатывает начальный ввод задачи"""
    user_id = message.from_user.id
    text = message.text.strip()

    # Парсим задачу через LLM
    parsed = await asyncio.to_thread(parse_task, text)
    print(f"DEBUG: LLM parsed = {parsed}")

    # Сохраняем parsed в state_data
    state_data["parsed"] = parsed

    # Проверяем относительное время
    relative_deadline = parse_relative_time(text)

    # Проверяем, какие данные есть
    has_date = parsed.get("date") is not None
    has_time = parsed.get("time") is not None

    # Если есть относительное время - сразу создаем задачу
    if relative_deadline:
        await create_task_from_data(message, state_data, relative_deadline)
        return

    # Если нет даты - спрашиваем дату
    if not has_date:
        state_data["state"] = STATE_WAITING_DATE
        await message.answer(
            "📅 Когда напомнить?\n"
            "• 'сегодня', 'завтра', 'послезавтра'\n"
            "• 'понедельник', 'вторник'...\n"
            "• '25.02.2026', '2026-02-25'"
        )
        return

    # Если есть дата, но нет времени - спрашиваем время
    if has_date and not has_time:
        # Сохраняем дату из LLM
        date_norm = normalize_date(parsed.get("date"))
        if date_norm:
            state_data["date"] = date_norm
            state_data["state"] = STATE_WAITING_TIME
            await message.answer(
                f"📅 Дата: {date_norm}\n\n"
                "⏰ Во сколько напомнить?\n"
                "• 15:00\n"
                "• 18.30\n"
                "• или просто '10' (это 10:00)"
            )
            return

    # Если все данные есть - создаем задачу
    await create_task_from_data(message, state_data)


async def create_task_from_data(message: Message, state_data: dict, relative_deadline: datetime = None):
    """Создает или редактирует задачу из данных в state_data"""
    user_id = message.from_user.id
    parsed = state_data.get("parsed", {})

    async with AsyncSessionLocal() as session:
        try:
            # Определяем deadline
            if relative_deadline:
                deadline = relative_deadline
            else:
                # Берем дату из state_data или из parsed
                date_str = state_data.get("date") or parsed.get("date")
                time_str = state_data.get("time") or parsed.get("time")

                if not date_str:
                    await message.answer("❌ Ошибка: не указана дата")
                    return

                date_norm = normalize_date(date_str)
                if not date_norm:
                    await message.answer(f"❌ Не удалось распознать дату: {date_str}")
                    return

                # Время по умолчанию 09:00
                if not time_str:
                    time_str = "09:00"

                # Нормализуем время
                time_tuple = normalize_time(time_str)
                if not time_tuple:
                    time_tuple = (9, 0)  # Дефолтное время

                hours, minutes = time_tuple
                deadline = datetime.combine(
                    date.fromisoformat(date_norm),
                    datetime.min.time().replace(hour=hours, minute=minutes)
                )

            # Создаем заголовок
            title = normalize_title(parsed.get("title"), parsed.get("description") or message.text)

            if state_data["state"] == STATE_CREATING:
                # Создаем новую задачу
                new_task = Task(
                    telegram_user_id=user_id,
                    title=title,
                    description=parsed.get("description") or message.text,
                    deadline_at=deadline,
                    status="ACTIVE",
                )
                session.add(new_task)
                await session.commit()

                user_states.pop(user_id, None)

                await message.answer(
                    f"✅ Задача создана! (ID: {new_task.id})\n\n"
                    f"📌 {new_task.title}\n"
                    f"📅 {new_task.deadline_at.strftime('%d.%m.%Y %H:%M')}\n"
                    f"📝 {new_task.description[:100]}{'...' if len(new_task.description) > 100 else ''}"
                )

            elif state_data["state"] == STATE_EDITING:
                # Редактируем существующую задачу
                task_id = state_data.get("task_id")
                if not task_id:
                    await message.answer("❌ Ошибка: не указан ID задачи")
                    return

                task = await session.get(Task, task_id)
                if not task or task.telegram_user_id != user_id:
                    await message.answer("❌ Задача не найдена")
                    return

                task.title = title
                task.description = parsed.get("description") or task.description
                task.deadline_at = deadline
                task.updated_at = datetime.now()

                await session.commit()
                user_states.pop(user_id, None)

                await message.answer(
                    f"✏️ Задача обновлена!\n"
                    f"📅 Новый дедлайн: {task.deadline_at.strftime('%d.%m.%Y %H:%M')}"
                )

        except Exception as e:
            print(f"❌ Ошибка при создании задачи: {e}")
            await message.answer(f"❌ Произошла ошибка: {str(e)}")
            await session.rollback()


async def create_or_edit_task(message: Message, state_data: dict):
    """Создает или редактирует задачу после получения всех данных"""
    await create_task_from_data(message, state_data)


# ================= COMMANDS (остаются без изменений) =================

async def start_handler(message: Message):
    await message.answer(
        "Привет! 👋\n\n"
        "/new — новая задача\n"
        "/tasks — список задач\n"
        "/task <id> — детали\n"
        "/edit <id> — изменить\n"
        "/delete <id> — удалить\n"
        "/cancel — отмена"
    )


async def cancel_handler(message: Message):
    user_id = message.from_user.id
    if user_id in user_states:
        user_states.pop(user_id)
    await message.answer("❌ Действие отменено")


async def new_handler(message: Message):
    user_id = message.from_user.id
    user_states[user_id] = {"state": STATE_CREATING}

    await message.answer(
        "✍️ Опиши задачу одним сообщением\n\n"
        "Примеры:\n"
        "• <b>Напомни завтра купить молоко</b>\n"
        "• <b>Завтра в 18:00 совещание</b>\n"
        "• <b>Через 2 часа позвонить маме</b>\n"
        "• <b>В понедельник подготовить отчет</b>\n\n"
        "Если что-то не указано, я спрошу отдельно 😊",
        parse_mode="HTML"
    )


async def tasks_handler(message: Message):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task)
            .where(
                Task.telegram_user_id == message.from_user.id,
                Task.status == "ACTIVE"
            )
            .order_by(Task.id)
        )
        tasks = result.scalars().all()

    if not tasks:
        await message.answer("📭 У тебя нет активных задач")
        return

    text = "📋 Твои задачи:\n\n"
    for t in tasks:
        text += f"[{t.id}] {t.title} — {t.deadline_at:%Y-%m-%d %H:%M}\n"

    await message.answer(text)


async def task_handler(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используй: /task <id>")
        return

    async with AsyncSessionLocal() as session:
        task = await session.get(Task, int(parts[1]))

    if not task or task.telegram_user_id != message.from_user.id:
        await message.answer("Задача не найдена")
        return

    await message.answer(
        f"📌 {task.title}\n\n"
        f"{task.description or ''}\n"
        f"⏰ {task.deadline_at:%d.%m.%Y %H:%M}"
    )


async def delete_handler(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используй: /delete <id>")
        return

    async with AsyncSessionLocal() as session:
        task = await session.get(Task, int(parts[1]))
        if not task or task.telegram_user_id != message.from_user.id:
            await message.answer("Задача не найдена")
            return

        task.status = "DELETED"
        await session.commit()

    await message.answer("🗑 Задача удалена")


async def edit_handler(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используй: /edit <id>")
        return

    user_id = message.from_user.id
    user_states[user_id] = {
        "state": STATE_EDITING,
        "task_id": int(parts[1]),
    }

    await message.answer(
        "✏️ Напиши обновлённую задачу:\n\n"
        "Можно изменить:\n"
        "• Текст задачи\n"
        "• Дату и время\n\n"
        "Или используй /cancel для отмены"
    )


# ================= REMINDERS (исправленная версия) =================

async def reminder_loop(bot: Bot):
    """Отправляет напоминания за 1 минуту до дедлайна"""
    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Время через 1 минуту
                reminder_time = datetime.now() + timedelta(minutes=1)

                result = await session.execute(
                    select(Task).where(
                        Task.status == "ACTIVE",
                        Task.deadline_at <= reminder_time,
                        Task.deadline_at > datetime.now()
                    )
                )

                tasks = result.scalars().all()

                for task in tasks:
                    try:
                        await bot.send_message(
                            task.telegram_user_id,
                            f"⏰ <b>Напоминание!</b>\n\n"
                            f"📌 {task.title}\n"
                            f"📝 {task.description or 'Без описания'}\n"
                            f"⏰ Дедлайн: {task.deadline_at.strftime('%d.%m.%Y %H:%M')}",
                            parse_mode="HTML"
                        )
                        task.status = "DONE"
                    except Exception as e:
                        print(f"Ошибка при отправке напоминания: {e}")

                if tasks:
                    await session.commit()

        except Exception as e:
            print(f"Ошибка в reminder_loop: {e}")

        await asyncio.sleep(10)  # Проверяем каждые 10 секунд


# ================= MAIN =================

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(start_handler, Command("start"))
    dp.message.register(new_handler, Command("new"))
    dp.message.register(tasks_handler, Command("tasks"))
    dp.message.register(task_handler, Command("task"))
    dp.message.register(edit_handler, Command("edit"))
    dp.message.register(delete_handler, Command("delete"))
    dp.message.register(cancel_handler, Command("cancel"))
    dp.message.register(message_handler)

    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())