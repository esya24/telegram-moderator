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
    raise RuntimeError("Set BOT_TOKEN in hosting environment variables.")

DB_PATH = os.getenv("DB_PATH", "moderation.sqlite3")
WARNING_LIMIT = 3
MUTE_MINUTES = 15
RESET_AFTER_DAYS = 7

# Mixed Cyrillic/Latin lookalikes -> Cyrillic. Covers common obfuscation.
LOOKALIKE_MAP = str.maketrans({
    "a":"а", "A":"а", "c":"с", "C":"с", "e":"е", "E":"е",
    "o":"о", "O":"о", "p":"р", "P":"р", "x":"х", "X":"х",
    "y":"у", "Y":"у", "k":"к", "K":"к", "m":"м", "M":"м",
    "t":"т", "T":"т", "b":"в", "B":"в", "h":"н", "H":"н",
    "i":"і", "I":"і", "l":"л", "L":"л"
})
DIGIT_MAP = str.maketrans({
    "0":"о", "1":"и", "3":"з", "4":"ч", "5":"с",
    "6":"б", "7":"т", "8":"в", "9":"я", "@":"а", "$":"с"
})

# Broad Russian profanity roots. Review/edit this list for your community.
PROFANITY_ROOTS = {
    "бля", "бляд", "блять", "блят", "блядин",
    "сука", "сук", "суч", "сучар",
    "хуй", "хуя", "хуе", "хуи", "хуёв", "хуев", "хер",
    "пизд", "пизд", "пиздец", "пизда",
    "еб", "ёб", "йоб", "заеб", "заёб", "наеб", "наёб",
    "уеб", "уёб", "выеб", "выёб", "въеб", "въёб",
    "муд", "мудак", "мудил",
    "гандон", "гондон", "манд", "залуп", "шлюх",
    "дроч", "дрис", "перд", "срал", "срать", "говн",
    "жоп", "сволоч", "сукин", "пидор", "пидар",
    "даун", "чмо", "fuck", "shit", "bitch", "asshole",
    # Insults / personal attacks
    "лох", "лошар", "лузер", "неудачник", "дебил", "идиот", "туп",
    "тупица", "дурак", "кретин", "придур", "ублюд", "урод", "мраз",
    "гнид", "ссык", "ссик", "петух", "питух", "пидух", "чушпан",
    "чурк", "хач", "чушка", "свин", "скот", "животн", "осел", "ослов",
    "козел", "козлина", "псих", "шиз", "мусор", "помой", "ничтож",
    "бесполез", "слабак", "сосун", "терпил", "подстил", "вырод",
    "глуп", "безмозг", "мозгоклюй", "балбес", "болван", "фрик",
    "лохмат", "позор", "засранец", "говнюк", "гад", "мерзав",
    "ничтожество", "профурсет", "шалава", "проститут", "твар",
    # Additional explicit vulgarity and insults (root-based)
    "сос", "соси", "сосал", "сосала", "сосать",
    "письк", "писюн", "писеч", "вагин", "хрен", "хренов",
    "член", "залуп", "манда", "мандав", "мандах",
    "сучка", "сукаб", "сучон", "сучье",
    "пидр", "педик", "гомик", "гомосек",
    "еблан", "ебан", "ебуч", "ебучк", "ебнут",
    "хуесос", "хуйня", "хуйн", "хует", "хуйло",
    "пиздюк", "пиздюл", "пиздобол", "пиздос",
    "долбо", "долбоящ", "долбень",
    "мудозвон", "мудила", "мудозвон",
    "чмош", "чмыр", "чмошник",
    "петушар", "петушок", "петушин",
    "говноед", "говно", "говня",
    "сран", "сру", "срун", "насрал",
    "обосран", "обоссы", "засс",
    "сволоч", "выродок", "душнил", "душный",
    "мразот", "мразина", "гниль", "гнило",
    "клоун", "циркач", "позорище",
    "тупорыл", "тупоголов", "бездар", "ничтожн"
}

# A small allowlist helps avoid accidental matches in common innocent words.
ALLOW_WORDS = {
    "бляха", "блядь", # remove if you prefer strict filtering
    "перед", "пердеть"  # edit as desired; these can be false-positive-prone
}

def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = text.translate(LOOKALIKE_MAP).translate(DIGIT_MAP)
    # Keep only letters; punctuation/spacing is removed to catch separators.
    text = re.sub(r"[^а-я]", "", text)
    # Collapse long repetitions: "суууука" -> "сука", "бляяяя" -> "бля"
    text = re.sub(r"(.)\1{1,}", r"\1", text)
    return text

def contains_swear(text: str) -> bool:
    norm = normalize(text)
    if not norm:
        return False

    # Avoid broad accidental matches inside ordinary words/names.
    # Check normalized tokens first; for longer profanity roots, allow inflected forms.
    # Very short roots (2-3 letters) are intentionally excluded from substring matching.
    tokens = re.findall(r"[а-я]+", text.lower().replace("ё", "е").translate(LOOKALIKE_MAP).translate(DIGIT_MAP))
    normalized_tokens = [normalize(token) for token in tokens]
    for root in PROFANITY_ROOTS:
        root_norm = normalize(root)
        if len(root_norm) <= 3:
            if root_norm in normalized_tokens:
                return True
        elif any(token == root_norm or token.startswith(root_norm) for token in normalized_tokens):
            return True

    # Catch punctuation/spacing inserted inside a word, but only roots of 4+ letters.
    compact = re.sub(r"[^а-я]", "", text.lower().replace("ё", "е").translate(LOOKALIKE_MAP).translate(DIGIT_MAP))
    return any(len(normalize(root)) >= 4 and normalize(root) in compact for root in PROFANITY_ROOTS)

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
db.execute("""
CREATE TABLE IF NOT EXISTS chat_users (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT, full_name TEXT,
 PRIMARY KEY (chat_id, user_id)
)
""")
db.commit()

def remember_user(chat_id: int, user):
    if user:
        db.execute("""INSERT INTO chat_users(chat_id,user_id,username,full_name) VALUES(?,?,?,?)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
        (chat_id,user.id,(user.username or "").lower(),user.full_name or ""))
        db.commit()

def find_user_by_username(chat_id: int, username: str):
    return db.execute("SELECT user_id FROM chat_users WHERE chat_id=? AND username=?",
        (chat_id, username.lower().lstrip("@"))).fetchone()

bot = Bot(token=TOKEN)
dp = Dispatcher()

def current_count(chat_id: int, user_id: int, now: datetime) -> int:
    row = db.execute(
        "SELECT count, last_violation FROM violations WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()
    if not row:
        return 0
    last = datetime.fromisoformat(row[1])
    return row[0] if now - last <= timedelta(days=RESET_AFTER_DAYS) else 0

def save_count(chat_id: int, user_id: int, count: int, now: datetime):
    db.execute("""
        INSERT INTO violations(chat_id,user_id,count,last_violation)
        VALUES(?,?,?,?)
        ON CONFLICT(chat_id,user_id)
        DO UPDATE SET count=excluded.count,last_violation=excluded.last_violation
    """, (chat_id, user_id, count, now.isoformat()))
    db.commit()

async def admin_check(chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")

def mention(user) -> str:
    # Prefer @username; fall back to a clickable name if no username exists.
    if user.username:
        return "@" + user.username
    safe_name = (user.full_name or "Пользователь")
    safe_name = (safe_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'


@dp.message(Command("warns"))
async def warns(message: Message):
    if not message.from_user:
        return
    now = datetime.now(timezone.utc)
    count = current_count(message.chat.id, message.from_user.id, now)
    await message.answer(
        f"Нарушений за последние {RESET_AFTER_DAYS} дней: {count}. "
        f"Первые {WARNING_LIMIT} — предупреждения; далее мут {MUTE_MINUTES} минут."
    )

@dp.message(Command("rules"))
async def rules(message: Message):
    await message.answer(
        f"📜 Правила: первые {WARNING_LIMIT} нарушения за {RESET_AFTER_DAYS} дней — предупреждения; "
        f"с {WARNING_LIMIT + 1}-го — мут на {MUTE_MINUTES} минут за каждое нарушение. "
        "Счётчик сбрасывается после {RESET_AFTER_DAYS} дней без нарушений."
    )

@dp.message(Command("mute"))
async def mute_command(message: Message):
    if not message.from_user or not await admin_check(message.chat.id, message.from_user.id):
        return
    reply = message.reply_to_message
    if not reply or not reply.from_user:
        await message.answer("Ответь на сообщение пользователя: /mute или /mute 45")
        return
    args = (message.text or "").split()
    try:
        minutes = int(args[1]) if len(args) > 1 else MUTE_MINUTES
        if not 1 <= minutes <= 10080:
            raise ValueError
    except ValueError:
        await message.answer("Укажи число минут от 1 до 10080.")
        return
    target = reply.from_user
    await bot.restrict_chat_member(
        message.chat.id, target.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes)
    )
    await message.answer(
        f"🔇 {mention(target)} принудительно замьючен на {minutes} минут.",
        parse_mode="HTML"
    )

@dp.message(Command("unmute"))
async def unmute_command(message: Message):
    if not message.from_user or not await admin_check(message.chat.id, message.from_user.id):
        return
    args = (message.text or "").split(maxsplit=1)
    target = None
    if len(args)>1 and args[1].strip().startswith("@"):
        username=args[1].strip().split()[0].lstrip("@")
        row=find_user_by_username(message.chat.id, username)
        if row:
            try:
                target=(await bot.get_chat_member(message.chat.id,row[0])).user
            except Exception:
                target=None
        if not target:
            await message.answer(f"Не нашёл @{username}. Пользователь должен написать хотя бы раз в группе. "
                               "Или ответь на его сообщение командой /unmute.")
            return
    elif message.reply_to_message and message.reply_to_message.from_user:
        target=message.reply_to_message.from_user
    else:
        await message.answer("Используй /unmute @username или ответь на сообщение пользователя командой /unmute.")
        return
    try:
        await bot.restrict_chat_member(message.chat.id,target.id,permissions=ChatPermissions(
            can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,
            can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,
            can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True))
    except Exception:
        await message.answer("Не удалось снять мут. Проверь права бота на ограничение участников.")
        return
    db.execute("DELETE FROM violations WHERE chat_id=? AND user_id=?",(message.chat.id,target.id)); db.commit()
    await message.answer(f"🔊 {mention(target)} размьючен. Предупреждения сброшены.",parse_mode="HTML")

@dp.message(F.text)
async def moderation(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    remember_user(message.chat.id, message.from_user)
    try:
        if await admin_check(message.chat.id, message.from_user.id):
            return
    except Exception:
        return

    if not contains_swear(message.text):
        return

    try:
        await message.delete()
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    chat_id, user_id = message.chat.id, message.from_user.id
    count = current_count(chat_id, user_id, now) + 1
    save_count(chat_id, user_id, count, now)
    tag = mention(message.from_user)

    if count <= WARNING_LIMIT:
        left = WARNING_LIMIT - count
        msg = (f"⚠️ {tag}, предупреждение {count}/{WARNING_LIMIT} за мат."
               + (f" До мута осталось предупреждений: {left}." if left else
                  " Следующее нарушение — мут на 15 минут."))
        await message.answer(msg, parse_mode="HTML")
        return

    try:
        until = now + timedelta(minutes=MUTE_MINUTES)
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await message.answer(
            f"🔇 {tag} получил мут на {MUTE_MINUTES} минут.\n"
            f"Причина: мат. Нарушение №{count}.",
            parse_mode="HTML"
        )
    except Exception:
        await message.answer(
            f"⚠️ {tag}: нарушение №{count}, но мут не удалось выдать. Проверьте права бота.",
            parse_mode="HTML"
        )


async def main():
    print("Moderator running")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
