import asyncio
import os
import re
from datetime import datetime, date, timedelta

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

user_states: dict[int, dict] = {}

# ================= HELPERS =================

ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def normalize_date(value: str) -> str | None:
    if not value:
        return None

    value = value.lower().strip()
    today = date.today()

    if value == "today":
        return today.isoformat()

    if value == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    if ISO_DATE_RE.fullmatch(value):
        parsed = date.fromisoformat(value)
        if parsed.year < today.year:
            parsed = parsed.replace(year=today.year)
        return parsed.isoformat()

    return None


def parse_relative_time(text: str) -> datetime | None:
    text = text.lower()

    match = re.search(r"через\s+(\d+)\s*(минут|час)", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if "час" in unit:
            return datetime.now() + timedelta(hours=amount)
        return datetime.now() + timedelta(minutes=amount)

    if "через минуту" in text:
        return datetime.now() + timedelta(minutes=1)

    if "через час" in text:
        return datetime.now() + timedelta(hours=1)

    return None


def normalize_title(title: str | None, description: str | None) -> str:
    if title and title.lower() != "напоминание":
        return title
    if description:
        return description.capitalize()
    return "Напоминание"


# ================= COMMANDS =================

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
    user_states.pop(message.from_user.id, None)
    await message.answer("Действие отменено ❌")


async def new_handler(message: Message):
    user_states[message.from_user.id] = {"state": "CREATING"}
    await message.answer(
        "Опиши задачу одним сообщением 🙂\n\n"
        "Я понимаю такие форматы:\n\n"
        "🕒 Относительное время:\n"
        "• Напомни через 10 минут проверить почту\n"
        "• Напомни через час отправить письмо\n\n"
        "📅 Дата и время:\n"
        "• Напомни завтра купить хлеб\n"
        "• Напомни завтра в 18:00 написать отчёт\n"
        "• Напомни 2026-02-14 в 19:00 собеседование\n\n"
        "ℹ️ Если время не указано — используется 09:00"
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
        await message.answer("У тебя нет активных задач 🙂")
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
        f"⏰ {task.deadline_at:%Y-%m-%d %H:%M}"
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

    user_states[message.from_user.id] = {
        "state": "EDITING",
        "task_id": int(parts[1])
    }
    await message.answer("Напиши обновлённую задачу ✏️")


# ================= MESSAGE =================

async def message_handler(message: Message):
    state = user_states.get(message.from_user.id)
    if not state:
        return

    parsed = await asyncio.to_thread(parse_task, message.text)
    relative_deadline = parse_relative_time(message.text)

    async with AsyncSessionLocal() as session:
        task = None
        if state["state"] == "EDITING":
            task = await session.get(Task, state["task_id"])
            if not task:
                await message.answer("Задача не найдена")
                return

        # ---- deadline ----
        if relative_deadline:
            deadline = relative_deadline
        else:
            date_norm = normalize_date(parsed.get("date"))
            time_value = parsed.get("time")

            if state["state"] == "EDITING":
                old_date = task.deadline_at.date()
                old_time = task.deadline_at.time()
            else:
                old_date = None
                old_time = None

            if date_norm and time_value:
                deadline = datetime.fromisoformat(f"{date_norm}T{time_value}")
            elif date_norm and old_time:
                deadline = datetime.combine(
                    date.fromisoformat(date_norm),
                    old_time
                )
            elif time_value and old_date:
                deadline = datetime.combine(
                    old_date,
                    datetime.strptime(time_value, "%H:%M").time()
                )
            elif state["state"] == "EDITING":
                deadline = task.deadline_at
            else:
                # /new без времени → дефолт 09:00
                deadline = datetime.combine(
                    date.fromisoformat(date_norm),
                    datetime.strptime("09:00", "%H:%M").time()
                )

        title = normalize_title(parsed.get("title"), parsed.get("description"))

        if state["state"] == "CREATING":
            new_task = Task(
                telegram_user_id=message.from_user.id,
                title=title,
                description=parsed.get("description"),
                deadline_at=deadline,
                status="ACTIVE",
            )
            session.add(new_task)
            await session.commit()

            user_states.pop(message.from_user.id, None)

            await message.answer(
                "✅ Задача создана!\n\n"
                f"📌 {new_task.title}\n"
                f"⏰ {new_task.deadline_at:%Y-%m-%d %H:%M}"
            )

        elif state["state"] == "EDITING":
            task.title = title
            task.description = parsed.get("description") or task.description
            task.deadline_at = deadline
            await session.commit()

            user_states.pop(message.from_user.id, None)
            await message.answer("✏️ Задача изменена")


# ================= REMINDERS =================

async def reminder_loop(bot: Bot):
    while True:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Task).where(
                    Task.status == "ACTIVE",
                    Task.deadline_at <= datetime.now()
                )
            )
            for task in result.scalars():
                await bot.send_message(
                    task.telegram_user_id,
                    f"⏰ Напоминание!\n\n📌 {task.title}"
                )
                task.status = "DONE"
            await session.commit()
        await asyncio.sleep(10)


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

