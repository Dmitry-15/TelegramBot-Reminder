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

Формат:
{
  "title": string | null,
  "description": string | null,
  "date": "YYYY-MM-DD" | null,
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

