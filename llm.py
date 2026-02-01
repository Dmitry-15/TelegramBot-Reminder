import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL"),
    default_headers={
        "HTTP-Referer": "http://localhost",
        "X-Title": "telegram-reminder-bot",
    },
)

MODEL = os.getenv("LLM_MODEL")

SYSTEM_PROMPT = """
Ты помощник, который извлекает из текста задачу напоминания.
Верни СТРОГО JSON без комментариев.

Важные правила:
1. Если пользователь сказал "сегодня", "завтра", "послезавтра" или день недели ("понедельник", "вторник" и т.д.) - НЕ конвертируй в дату! Верни как есть.
2. Возвращай НЕОБРАБОТАННЫЕ слова из текста пользователя для полей date и time.

Примеры:
- "напомни завтра купить молоко" → {"date": "завтра", "time": null, "title": "купить молоко", "description": "напомни завтра купить молоко"}
- "сегодня в 18:00 встреча" → {"date": "сегодня", "time": "18:00", "title": "встреча", "description": "сегодня в 18:00 встреча"}
- "в пятницу совещание" → {"date": "пятница", "time": null, "title": "совещание", "description": "в пятницу совещание"}
- "2026-02-25 в 15:00 презентация" → {"date": "2026-02-25", "time": "15:00", "title": "презентация", "description": "2026-02-25 в 15:00 презентация"}

Формат:
{
  "title": string | null,
  "description": string | null,
  "date": string | null,  # "сегодня", "завтра", "2026-02-25", "пятница" и т.д.
  "time": "HH:MM" | null
}
"""

def parse_task(text: str) -> dict:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
    )

    content = response.choices[0].message.content
    return json.loads(content)
