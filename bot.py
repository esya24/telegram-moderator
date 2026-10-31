import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ChatPermissions

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Добавьте токен в Secrets / переменные окружения.")

DB_PATH = os.getenv("DB_PATH", "moderation.sqlite3")
WARNING_LIMIT = 3
MUTE_MINUTES = 15
RESET_AFTER_DAYS = 7

# Список можно расширять. Фильтр проверяет отдельные слова и распространённые обходы.
BAD_WORDS = {
    "бля", "блять", "блядь", "блядина", "бляди",
    "сука", "суки", "сучка",
    "хуй", "хуи", "хуя", "хуе", "хуев", "хуёв", "хер",
    "пизда", "пизде", "пизду", "пиздеть", "пиздец",
    "ебать", "ебал", "ебала", "ебет", "ебёт", "ебаный", "ебанутый",
    "нахуй", "охуел", "охуеть", "заебал", "заебись",
    "мудак", "мудила", "гандон", "долбоеб", "долбоёб",
    "уебок", "уёбок", "шлюха", "выеб"
}

TRANSLATE = str.maketrans({
    "ё": "е", "0": "о", "1": "и", "3": "з",
    "4": "ч", "6": "б", "8": "в", "@": "а", "$": "с"
})

def normalize(text: str) -> str:
    text = text.lower().translate(TRANSLATE)
    # Заменяем разделители пробелами, чтобы слова не склеивались.
    text = re.sub(r"[^а-я]+", " ", text)
    # Убираем повторение букв: "бляяяя" -> "бля".
    words = []
    for word in text.split():
        word = re.sub(r"(.)\1{2,}", r"\1\1", word)
        words.append(word)
    return " ".join(words)

def contains_swear(text: str) -> bool:
    words = normalize(text).split()
    for token in words:
        for bad in BAD_WORDS:
            if token == bad or (len(bad) >= 4 and bad in token):
                return True
    return False

db = sqlite3.connect(DB_PATH)
db.execute("""
CREATE TABLE IF NOT EXISTS violations (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    last_violation TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
)
""")
db.commit()

bot = Bot(token=TOKEN)
dp = Dispatcher()

def get_violation_count(chat_id: int, user_id: int, now: datetime) -> int:
    row = db.execute(
        "SELECT count, last_violation FROM violations WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()

    if row:
        last = datetime.fromisoformat(row[1])
        if now - last <= timedelta(days=RESET_AFTER_DAYS):
            return row[0]
    return 0

def save_violation(chat_id: int, user_id: int, count: int, now: datetime) -> None:
    db.execute("""
        INSERT INTO violations(chat_id, user_id, count, last_violation)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET count=excluded.count, last_violation=excluded.last_violation
    """, (chat_id, user_id, count, now.isoformat()))
    db.commit()

async def is_admin(chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")

@dp.message(F.text)
async def moderate(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return

    try:
        if await is_admin(message.chat.id, message.from_user.id):
            return
    except Exception:
        # Если Telegram не дал проверить статус, не применяем наказание вслепую.
        return

    if not contains_swear(message.text):
        return

    try:
        await message.delete()
    except Exception:
        # Без права удаления бот всё равно сообщит о нарушении.
        pass

    now = datetime.now(timezone.utc)
    chat_id = message.chat.id
    user_id = message.from_user.id
    count = get_violation_count(chat_id, user_id, now) + 1
    save_violation(chat_id, user_id, count, now)
    name = message.from_user.full_name

    if count <= WARNING_LIMIT:
        remaining = WARNING_LIMIT - count
        if remaining:
            text = (
                f"⚠️ {name}, предупреждение {count}/{WARNING_LIMIT} за мат. "
                f"До санкций осталось предупреждений: {remaining}."
            )
        else:
            text = (
                f"⚠️ {name}, это третье предупреждение из {WARNING_LIMIT}. "
                "Следующее нарушение приведёт к муту на 15 минут."
            )
        await message.answer(text)
        return

    until = now + timedelta(minutes=MUTE_MINUTES)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await message.answer(
            f"🔇 {name} получил мут на {MUTE_MINUTES} минут. "
            f"Всего нарушений: {count}. Следующее нарушение — ещё 15 минут мута."
        )
    except Exception:
        await message.answer(
            f"⚠️ {name}, зафиксировано нарушение №{count}, но выдать мут не удалось. "
            "Проверьте права бота."
        )

@dp.message(Command("warns"))
async def warns(message: Message):
    if not message.from_user:
        return
    now = datetime.now(timezone.utc)
    count = get_violation_count(message.chat.id, message.from_user.id, now)
    await message.answer(
        f"Твои нарушения за последние {RESET_AFTER_DAYS} дней: {count}.\n"
        f"Первые {WARNING_LIMIT} — предупреждения; далее каждый мат — мут на {MUTE_MINUTES} минут."
    )

@dp.message(Command("rules"))
async def rules(message: Message):
    await message.answer(
        "📜 Правила чата:\n"
        f"1. За первые {WARNING_LIMIT} нарушения за 7 дней выдаются предупреждения.\n"
        f"2. Начиная с {WARNING_LIMIT + 1}-го нарушения — мут на {MUTE_MINUTES} минут.\n"
        f"3. Каждое последующее нарушение — ещё {MUTE_MINUTES} минут мута.\n"
        f"4. Счётчик обнуляется после {RESET_AFTER_DAYS} дней без нарушений."
    )

@dp.message(Command("mute"))
async def manual_mute(message: Message):
    if not message.from_user or not await is_admin(message.chat.id, message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Ответь на сообщение пользователя командой /mute или /mute 15.")
        return
    args = (message.text or "").split()
    try:
        minutes = int(args[1]) if len(args) > 1 else 15
        if minutes < 1 or minutes > 10080:
            raise ValueError
    except ValueError:
        await message.answer("Укажи время от 1 до 10080 минут.")
        return
    target = message.reply_to_message.from_user
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await bot.restrict_chat_member(
        message.chat.id, target.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until
    )
    await message.answer(f"🔇 {target.full_name} замьючен на {minutes} минут.")

@dp.message(Command("unmute"))
async def manual_unmute(message: Message):
    if not message.from_user or not await is_admin(message.chat.id, message.from_user.id):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Ответь на сообщение пользователя командой /unmute.")
        return
    target = message.reply_to_message.from_user
    await bot.restrict_chat_member(
        message.chat.id, target.id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
    )
    await message.answer(f"🔊 {target.full_name} размьючен.")

async def main():
    print("Модератор запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
