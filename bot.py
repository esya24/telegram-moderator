import asyncio, os, re, sqlite3, logging, uuid
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv('BOT_TOKEN')
if not TOKEN: raise RuntimeError('Set BOT_TOKEN environment variable')
DB_PATH=os.getenv('DB_PATH','moderation.sqlite3')
WARNING_LIMIT=int(os.getenv('WARNING_LIMIT','3')); MUTE_MINUTES=int(os.getenv('MUTE_MINUTES','15')); RESET_AFTER_DAYS=int(os.getenv('RESET_AFTER_DAYS','7'))

# Robust text normalization and configurable moderation vocabulary.
HOMOGLYPHS = str.maketrans({'a':'а','c':'с','e':'е','o':'о','p':'р','x':'х','y':'у','k':'к','m':'м','t':'т','b':'в','h':'н','i':'и','j':'й','l':'л','v':'в','w':'ш','z':'з','0':'о','1':'и','3':'з','4':'ч','5':'с','6':'б','7':'т','8':'в','9':'я','@':'а','$':'с'})
PROFANITY_ROOTS = set("""бля бляд блять блят сук суч хуй хуе хуя хуи хуёв хуев хую хер хрен пизд пиздец пизда еб ёб йоб заеб заёб наеб наёб уеб уёб выеб выёб въеб въёб вахуй вахуи вахуе вахую похуй похую похуи похуист похуйня ахуе охуе гандон гондон манд залуп шлюх дроч дрис перд срал срать говн жоп сволоч сукин пидор пидар даун чмо дебил идиот туп тупиц дурак кретин придур ублюд урод мраз гнид ссык петух питух пидух чушпан чурк хач чушка свин скот животн осел козел козлин псих шиз мусор помой ничтож бесполез слабак сосун терпил подстил вырод глуп безмозг балбес болван фрик позор засранец говнюк гад мерзав профурсет шалав проститут твар сос соси сосал сосала сосать письк писюн писеч вагин член манда мандав мандах сучка сучон сучье пидр педик гомик гомосек еблан ебан ебуч ебучк ебнут хуесос хуйн хует хуйло пиздюк пиздюл пиздобол пиздос долбо долбоящ долбень мудозвон мудила чмош чмыр петушар петушок петушин говноед говня сран сру срун насрал обосран обоссы засс мразот мразина гниль клоун циркач позорище тупорыл тупоголов бездар ничтожн лох лошар лузер неудачник тупица мразота гнида слабак соска шалава мерзкий козлина чмошник вахуйня хуйня нахуй нахуя нахер херня пидорас пидораст пидора пидорасина""".split())
INSULT_ROOTS = set("""лох лошар лузер неудачник туп тупиц дурак кретин придур ублюд урод мраз гнид ссык петух питух пидух чушпан чурк хач чушка свин скот осел козел козлин псих шиз мусор помой ничтож бесполез слабак сосун терпил подстил вырод глуп безмозг балбес болван фрик позор засранец говнюк гад мерзав профурсет шалав проститут твар чмо дебил идиот пидор пидар даун гандон мудозвон мудила чмош чмыр петушар петушок говноед мразот мразина гниль клоун циркач позорище тупорыл тупоголов бездар ничтожн мразота гнида козлина чмошник душнил душный""".split())
SHORT_FORMS={'лох','чмо','гад','хер','сука','fuck','shit','bitch'}
ALLOW_WORDS={'бляха','перед','пердеть','пердёж','пердёжка'}

def normalize(text: str) -> str:
    text=text.casefold().replace('ё','е').translate(HOMOGLYPHS)
    text=re.sub(r'[\u200b-\u200f\u2060\ufeff]','',text)
    text=re.sub(r'([а-я])\1{2,}',r'\1\1',text)
    return re.sub(r'[^а-я]+',' ',text).strip()

def _root_pattern(root: str) -> re.Pattern:
    chars=[re.escape(ch) for ch in root]
    return re.compile(r'(?<![а-я])'+r'[а-я]{0,1}'.join(chars)+r'(?![а-я])')

_ROOT_PATTERNS=tuple((root,_root_pattern(root)) for root in PROFANITY_ROOTS if len(root)>=4)
_INSULT_PATTERNS=tuple((root,_root_pattern(root)) for root in INSULT_ROOTS if len(root)>=4)

def is_offensive(text: str) -> bool:
    normalized=normalize(text)
    if not normalized:return False
    words=normalized.split()
    if all(word in ALLOW_WORDS for word in words):return False
    if any(word in SHORT_FORMS for word in words):return True
    for word in words:
        if any(word.startswith(root) for root in PROFANITY_ROOTS if len(root)>=4):return True
        if any(root in word for root in ('хуй','хуе','хуя','хуи','хер','пизд','еба','ебу','ебн')):return True
    if any(pattern.search(normalized) for _,pattern in _ROOT_PATTERNS):return True
    compact=''.join(words)
    return any(len(root)>=5 and root in compact for root in PROFANITY_ROOTS)

def is_directed_insult(text: str) -> bool:
    normalized=normalize(text)
    if not normalized:return False
    if any(word in SHORT_FORMS for word in normalized.split()):return True
    return any(pattern.search(normalized) for _,pattern in _INSULT_PATTERNS)

# SQLite connection is used by this single asyncio event loop.
db=sqlite3.connect(DB_PATH)
db.execute('PRAGMA journal_mode=WAL')
db.execute('''CREATE TABLE IF NOT EXISTS violations(chat_id INTEGER,user_id INTEGER,count INTEGER DEFAULT 0,last_violation TEXT,
PRIMARY KEY(chat_id,user_id))''')
db.execute('''CREATE TABLE IF NOT EXISTS chat_users(chat_id INTEGER,user_id INTEGER,username TEXT,full_name TEXT,
PRIMARY KEY(chat_id,user_id))'''); db.commit()

def remember_user(chat_id,user):
    db.execute('''INSERT INTO chat_users VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
    username=excluded.username,full_name=excluded.full_name''',(chat_id,user.id,(user.username or '').lower(),user.full_name or '')); db.commit()
def find_user(chat_id,username): return db.execute('SELECT user_id FROM chat_users WHERE chat_id=? AND username=?',(chat_id,username.lower().lstrip('@'))).fetchone()
def count_violations(chat_id,user_id,now):
    row=db.execute('SELECT count,last_violation FROM violations WHERE chat_id=? AND user_id=?',(chat_id,user_id)).fetchone()
    if not row:return 0
    try: last=datetime.fromisoformat(row[1])
    except (ValueError,TypeError): return 0
    if last.tzinfo is None:last=last.replace(tzinfo=timezone.utc)
    return row[0] if now-last<=timedelta(days=RESET_AFTER_DAYS) else 0
def save_count(chat_id,user_id,count,now):
    db.execute('''INSERT INTO violations VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
    count=excluded.count,last_violation=excluded.last_violation''',(chat_id,user_id,count,now.isoformat()));db.commit()

# Appeals: one active draft per user per chat; moderator decisions are callback-protected.
pending_appeals = {}  # (chat_id, user_id) -> original mute message id
active_appeals = {}   # appeal_id -> (chat_id, user_id)

bot=Bot(TOKEN); dp=Dispatcher()
async def is_admin(chat_id,user_id):
    m=await bot.get_chat_member(chat_id,user_id); return m.status in ('administrator','creator')
def mention(user):
    if user.username:return '@'+user.username
    name=(user.full_name or 'Пользователь').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    return f'<a href="tg://user?id={user.id}">{name}</a>'

@dp.message(Command('warns'))
async def warns(m:Message):
    if m.from_user:
        c=count_violations(m.chat.id,m.from_user.id,datetime.now(timezone.utc))
        await m.answer(f'Нарушений за последние {RESET_AFTER_DAYS} дней: {c}. Первые {WARNING_LIMIT} — предупреждения; далее мут {MUTE_MINUTES} минут.')
@dp.message(Command('rules'))
async def rules(m:Message):
    await m.answer(f'📜 Первые {WARNING_LIMIT} нарушения за {RESET_AFTER_DAYS} дней — предупреждения; с {WARNING_LIMIT+1}-го — мут {MUTE_MINUTES} минут. Счётчик сбрасывается после {RESET_AFTER_DAYS} дней без нарушений.')
@dp.message(Command('mute'))
async def mute(m:Message):
    if not m.from_user: return
    try:
        if not await is_admin(m.chat.id,m.from_user.id):return
    except Exception: await m.answer('Не удалось проверить права администратора.');return
    target=m.reply_to_message.from_user if m.reply_to_message and m.reply_to_message.from_user else None
    if not target: await m.answer('Ответь на сообщение пользователя: /mute или /mute 45');return
    args=(m.text or '').split()
    try: minutes=int(args[1]) if len(args)>1 else MUTE_MINUTES; assert 1<=minutes<=10080
    except (ValueError,AssertionError): await m.answer('Укажи число минут от 1 до 10080.');return
    try:
        until=datetime.now(timezone.utc)+timedelta(minutes=minutes)
        await bot.restrict_chat_member(m.chat.id,target.id,permissions=ChatPermissions(can_send_messages=False),until_date=until)
        appeal_kb=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='⚖️ Обжаловать мут',callback_data=f'appeal_start:{m.chat.id}')
        ]])
        await bot.send_message(m.chat.id,
            f'🔇 {mention(target)} получил мут на {minutes} минут. Если это ошибка, можно обжаловать:',
            parse_mode='HTML',reply_markup=appeal_kb)
        await m.answer(f'🔇 {mention(target)} замьючен на {minutes} минут.',parse_mode='HTML')
    except Exception as e: logging.exception('mute failed'); await m.answer('Не удалось выдать мут. Проверь права бота (ограничение участников).')
@dp.message(Command('unmute'))
async def unmute(m:Message):
    if not m.from_user:return
    try:
        if not await is_admin(m.chat.id,m.from_user.id):return
    except Exception: await m.answer('Не удалось проверить права администратора.');return
    target=None; args=(m.text or '').split(maxsplit=1)
    if len(args)>1 and args[1].strip().startswith('@'):
        row=find_user(m.chat.id,args[1].strip().split()[0])
        if row:
            try: target=(await bot.get_chat_member(m.chat.id,row[0])).user
            except Exception: pass
    elif m.reply_to_message and m.reply_to_message.from_user:target=m.reply_to_message.from_user
    if not target: await m.answer('Используй /unmute @username или ответь на сообщение пользователя командой /unmute.');return
    try:
        await bot.restrict_chat_member(m.chat.id,target.id,permissions=ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True))
        db.execute('DELETE FROM violations WHERE chat_id=? AND user_id=?',(m.chat.id,target.id));db.commit()
        await m.answer(f'🔊 {mention(target)} размьючен. Предупреждения сброшены.',parse_mode='HTML')
    except Exception: logging.exception('unmute failed'); await m.answer('Не удалось снять мут. Проверь права бота.')

@dp.callback_query(F.data.startswith('appeal_start:'))
async def appeal_start(c:CallbackQuery):
    try: cid=int(c.data.split(':',1)[1])
    except (ValueError,AttributeError): await c.answer('Некорректная кнопка.',show_alert=True); return
    if c.message is None or c.message.chat.id != cid:
        await c.answer('Кнопка больше недействительна.',show_alert=True); return
    try:
        member=await bot.get_chat_member(cid,c.from_user.id)
        # Only a currently restricted member can start an appeal.
        if getattr(member,'status',None) not in ('restricted',):
            await c.answer('Обжаловать мут могут только замьюченные участники.',show_alert=True); return
        if getattr(member,'can_send_messages',True):
            await c.answer('У тебя нет активного мута.',show_alert=True); return
    except Exception:
        await c.answer('Не удалось проверить статус мута. Попробуй позже.',show_alert=True); return
    key=(cid,c.from_user.id)
    pending_appeals[key]=c.message.message_id
    await c.answer()
    try:
        await bot.send_message(c.from_user.id, f'Отправь одним сообщением причину обжалования мута в чате {c.message.chat.title}.\nЧтобы отменить, напиши /cancel.')
        await c.message.answer(f'{mention(c.from_user)}, причина обжалования запрошена в личных сообщениях бота.', parse_mode='HTML')
    except Exception:
        pending_appeals.pop(key, None)
        await c.message.answer(f'{mention(c.from_user)}, сначала открой личный чат с ботом и нажми /start, затем повтори обжалование.', parse_mode='HTML')

@dp.message(Command('cancel'))
async def cancel_appeal(m:Message):
    if m.from_user and pending_appeals.pop((m.chat.id,m.from_user.id),None) is not None:
        await m.answer('Обжалование отменено.')

@dp.callback_query(F.data.startswith('appeal_decide:'))
async def appeal_decide(c:CallbackQuery):
    try:
        _, decision, appeal_id = c.data.split(':',2)
        cid, uid = active_appeals[appeal_id]
    except (ValueError,KeyError,AttributeError):
        await c.answer('Обжалование уже обработано или недействительно.',show_alert=True); return
    if c.message is None or c.message.chat.id != cid:
        await c.answer('Недействительная заявка.',show_alert=True); return
    try:
        if not await is_admin(cid,c.from_user.id):
            await c.answer('Решение доступно только администраторам.',show_alert=True); return
    except Exception:
        await c.answer('Не удалось проверить права.',show_alert=True); return
    active_appeals.pop(appeal_id,None)
    if decision=='approve':
        try:
            await bot.restrict_chat_member(cid,uid,permissions=ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True))
            db.execute('DELETE FROM violations WHERE chat_id=? AND user_id=?',(cid,uid)); db.commit()
            result='✅ Обжалование принято. Мут снят, счётчик предупреждений сброшен.'
        except Exception:
            logging.exception('appeal unmute failed'); result='⚠️ Не удалось снять мут. Проверьте права бота.'
    else:
        result='❌ Обжалование отклонено. Мут остаётся активным.'
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(result)
    await c.answer('Решение сохранено.')

@dp.message(F.chat.type == 'private', F.text)
async def private_appeal_reason(m:Message):
    if not m.from_user: return
    matches=[key for key in pending_appeals if key[1]==m.from_user.id]
    if not matches: return
    if (m.text or '').startswith('/cancel'):
        for key in matches: pending_appeals.pop(key,None)
        await m.answer('Обжалование отменено.')
        return
    if len(matches)>1:
        await m.answer('Несколько активных обжалований. Нажми кнопку в нужной группе и повтори.')
        return
    cid,uid=matches[0]; pending_appeals.pop(matches[0],None)
    appeal_id=uuid.uuid4().hex[:12]; active_appeals[appeal_id]=(cid,uid)
    keyboard=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Принять и снять мут',callback_data=f'appeal_decide:approve:{appeal_id}'),
        InlineKeyboardButton(text='❌ Отклонить',callback_data=f'appeal_decide:reject:{appeal_id}')
    ]])
    reason=(m.text or '').strip().replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')[:3000]
    await m.answer('Обжалование отправлено администраторам.')
    await bot.send_message(cid, f'📨 <b>Обжалование мута</b> от {mention(m.from_user)} (ID {uid})\n\n<b>Причина:</b>\n{reason}',parse_mode='HTML',reply_markup=keyboard)

@dp.message(F.text | F.caption)
async def moderation(m:Message):
    u=m.from_user
    if not u or u.is_bot:return
    remember_user(m.chat.id,u)
    try:
        if await is_admin(m.chat.id,u.id):return
    except Exception: logging.exception('admin check failed'); return # fail closed
    text=m.text or m.caption or ''
    if not is_offensive(text):return
    try: await m.delete()
    except Exception: logging.exception('delete failed')
    now=datetime.now(timezone.utc); cid,uid=m.chat.id,u.id
    count=count_violations(cid,uid,now)+1;save_count(cid,uid,count,now); tag=mention(u)
    if count<=WARNING_LIMIT:
        msg=f'⚠️ {tag}, предупреждение {count}/{WARNING_LIMIT} за оскорбление/мат.'
        msg+=f' До мута осталось предупреждений: {WARNING_LIMIT-count}.' if count<WARNING_LIMIT else f' Следующее нарушение — мут на {MUTE_MINUTES} минут.'
        await m.answer(msg,parse_mode='HTML');return
    try:
        await bot.restrict_chat_member(cid,uid,permissions=ChatPermissions(can_send_messages=False),until_date=now+timedelta(minutes=MUTE_MINUTES))
        await m.answer(f'🔇 {tag} получил мут на {MUTE_MINUTES} минут. Нарушение №{count}.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⚖️ Обжаловать мут',callback_data=f'appeal_start:{cid}')]]))
    except Exception: logging.exception('automatic mute failed'); await m.answer(f'⚠️ {tag}: нарушение №{count}, но мут не удалось выдать. Проверь права бота.',parse_mode='HTML')

async def main():
    logging.info('Moderator started')
    async with bot: await dp.start_polling(bot)
if __name__=='__main__': asyncio.run(main())
