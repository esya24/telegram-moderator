import asyncio, os, re, sqlite3, logging, uuid
from html import escape
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
SHORT_FORMS={'лох','чмо','гад','хер','сука','бля','blya','fuck','shit','bitch'}
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


# Latin transliterations and spaced/obfuscated variants commonly used in Russian chats.
# Checked against the ORIGINAL text before Cyrillic normalization, because Latin
# letters such as s/o/a are otherwise discarded or mapped as homoglyphs.
_LATIN_BAD_WORDS = (
    'blyat', 'bljat', 'bl9t', 'bl4t', 'blya', 'sosi', 'otsosi', 'otsos', 'otsasy', 'suka', 'suk',
    'xuy', 'hui', 'xyi', 'huy', 'xui', 'pizda', 'pizd', 'pizdec',
    'ebat', 'yebat', 'e6at', 'eblan', 'nahuy', 'nahui', 'na xuy',
    'poxuy', 'pohuy', 'pohui', 'fuck', 'fck', 'shit', 'bitch',
)

def has_latin_profanity(text: str) -> bool:
    # Remove punctuation/spacing between Latin letters, preserving words as a
    # compact stream so forms like "s o s i" and "b.l.y.a.t" are caught.
    latin = text.casefold().replace('ё', 'е')
    compact = re.sub(r'[^a-z0-9]+', '', latin)
    for word in _LATIN_BAD_WORDS:
        key = re.sub(r'[^a-z0-9]+', '', word)
        if key and key in compact:
            return True
    # Common leetspeak substitutions in Latin spellings.
    compact = compact.translate(str.maketrans({'0':'o','1':'i','3':'e','4':'a','5':'s','6':'b','7':'t','@':'a','$':'s'}))
    return any(re.sub(r'[^a-z0-9]+','',w) in compact for w in _LATIN_BAD_WORDS)

def is_offensive(text: str) -> bool:
    if has_latin_profanity(text):return True
    normalized=normalize(text)
    if not normalized:return False
    words=normalized.split()
    # Explicitly catch sexual imperative/derivative forms missed by root-prefix matching.
    if re.search(r'(?<![а-я])(?:отсос\w*|отсоси\w*|отсасыва\w*|соси\w*|сосал\w*|сосала\w*|сосать\w*)(?![а-я])', normalized):
        return True
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

# Private administration and appeals persist across process restarts.
db.executescript('''
CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY,title TEXT);
CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER PRIMARY KEY,warnings INTEGER,minutes INTEGER,enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS private_users(user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS drafts(user_id INTEGER PRIMARY KEY,kind TEXT,chat_id INTEGER);
CREATE TABLE IF NOT EXISTS appeals(id TEXT PRIMARY KEY,chat_id INTEGER,user_id INTEGER,name TEXT,reason TEXT,status TEXT DEFAULT 'pending');
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_appeal ON appeals(chat_id,user_id) WHERE status IN ('pending','processing');
INSERT OR IGNORE INTO chats SELECT DISTINCT chat_id,CAST(chat_id AS TEXT) FROM chat_users WHERE chat_id<0;
''')
db.commit()
bot=Bot(TOKEN); dp=Dispatcher()

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=d) for t,d in row] for row in rows])
def remember_chat(chat):
    if chat.type in ('group','supergroup','channel'):
        db.execute('INSERT INTO chats VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title',(chat.id,chat.title or str(chat.id)));db.commit()
def config(cid):
    row=db.execute('SELECT warnings,minutes,enabled FROM settings WHERE chat_id=?',(cid,)).fetchone()
    return row or (WARNING_LIMIT,MUTE_MINUTES,1)
def set_config(cid,warnings,minutes,enabled):
    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?,?)',(cid,warnings,minutes,enabled));db.commit()
def mention(user):
    return '@'+user.username if user.username else f'<a href="tg://user?id={user.id}">{escape(user.full_name or "Пользователь")}</a>'
async def is_admin(cid,uid):
    return (await bot.get_chat_member(cid,uid)).status in ('administrator','creator')
async def allowed(cid,uid):
    try:return await is_admin(cid,uid)
    except Exception:return False
async def writable_target(cid,uid):
    member=await bot.get_chat_member(cid,uid)
    if member.status in ('creator','administrator','left','kicked') or member.user.is_bot:
        raise ValueError('Этот участник недоступен для ограничения.')
async def lift_mute(cid,uid):
    await writable_target(cid,uid)
    # All True lifts individual restrictions; normal group permissions still apply.
    permissions=ChatPermissions(**{key:True for key in ChatPermissions.model_fields})
    await bot.restrict_chat_member(cid,uid,permissions=permissions,use_independent_chat_permissions=True)
    db.execute('DELETE FROM violations WHERE chat_id=? AND user_id=?',(cid,uid));db.commit()
async def mute_user(cid,uid,minutes):
    await writable_target(cid,uid)
    if (await bot.get_chat(cid)).type!='supergroup':raise ValueError('Мут доступен в супергруппе обсуждений канала.')
    await bot.restrict_chat_member(cid,uid,permissions=ChatPermissions(can_send_messages=False),until_date=datetime.now(timezone.utc)+timedelta(minutes=minutes))
async def appeal_keyboard(cid):
    from aiogram.utils.deep_linking import create_start_link
    url=await create_start_link(bot,f'appeal_{cid}')
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⚖️ Обжаловать в ЛС',url=url)]])
async def panel(m,uid):
    rows=[]
    for cid,title in db.execute('SELECT chat_id,title FROM chats ORDER BY title').fetchall():
        if await allowed(cid,uid):rows.append([(title[:45],f'p:chat:{cid}')])
    await m.answer('⚙️ Выбери чат для управления.' if rows else 'Нет доступных чатов. Добавь бота администратором и отправь /panel в группе или привяжи чат здесь: /connect -1001234567890',reply_markup=kb(rows) if rows else None)
async def show_chat(m,cid):
    w,minutes,enabled=config(cid)
    title=db.execute('SELECT title FROM chats WHERE chat_id=?',(cid,)).fetchone()
    total=db.execute("SELECT COUNT(*) FROM appeals WHERE chat_id=? AND status='pending'",(cid,)).fetchone()[0]
    await m.answer(f'⚙️ {title[0] if title else cid}\nФильтр: {"включён" if enabled else "выключен"}\nПредупреждений до мута: {w}\nМут: {minutes} мин.\nЗаявок: {total}',reply_markup=kb([
        [('🔄 Вкл/выкл фильтр',f'p:toggle:{cid}')],
        [('⚠️ Лимит предупреждений',f'p:warnings:{cid}'),('⏱ Длительность мута',f'p:minutes:{cid}')],
        [('👤 Управление участником',f'p:user:{cid}'),('📋 Нарушения',f'p:list:{cid}')],
        [('📨 Обжалования',f'p:inbox:{cid}')],[('⬅️ Список чатов','p:home:0')]]))
async def show_user(m,cid,uid):
    member=await bot.get_chat_member(cid,uid)
    count=count_violations(cid,uid,datetime.now(timezone.utc))
    await m.answer(f'{member.user.full_name}\nID: {uid}\nПредупреждений: {count}\nСтатус: {member.status}',reply_markup=kb([
        [('🔇 Мут',f'p:mute:{cid}:{uid}'),('🔊 Снять мут',f'p:unmute:{cid}:{uid}')],
        [('🧹 Сбросить предупреждения',f'p:reset:{cid}:{uid}')],[('⬅️ Назад',f'p:chat:{cid}')]]))
async def show_appeal(m,row):
    aid,cid,uid,name,reason,status=row
    title=db.execute('SELECT title FROM chats WHERE chat_id=?',(cid,)).fetchone()
    await m.answer(f'📨 Обжалование в {title[0] if title else cid}\n{name} (ID {uid})\n\n{reason}',reply_markup=kb([[('✅ Принять',f'a:approve:{aid}'),('❌ Отклонить',f'a:reject:{aid}')]]))
async def start_appeal(m,cid):
    member=await bot.get_chat_member(cid,m.from_user.id)
    if member.status!='restricted' or member.can_send_messages:
        await m.answer('В этом чате у тебя нет активного мута.');return
    if db.execute("SELECT 1 FROM appeals WHERE chat_id=? AND user_id=? AND status IN ('pending','processing')",(cid,m.from_user.id)).fetchone():
        await m.answer('Твоя заявка уже ожидает решения.');return
    db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)',(m.from_user.id,'appeal',cid));db.commit()
    await m.answer('Напиши одним сообщением причину обжалования (до 2000 символов). Её увидят только администраторы в ЛС бота. Отмена: /cancel.')

@dp.my_chat_member()
async def membership(event):remember_chat(event.chat)

@dp.message(Command('start','panel','admin'))
async def start(m:Message):
    if m.chat.type!='private':
        remember_chat(m.chat)
        from aiogram.utils.deep_linking import create_start_link
        await m.answer('Панель управления находится в ЛС бота.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть панель в ЛС',url=await create_start_link(bot,'panel'))]]));return
    db.execute('INSERT OR IGNORE INTO private_users VALUES(?)',(m.from_user.id,));db.commit()
    parts=(m.text or '').split(maxsplit=1)
    if len(parts)>1 and parts[1].startswith('appeal_'):
        try:await start_appeal(m,int(parts[1][7:]))
        except Exception:await m.answer('Не удалось проверить мут. Проверь, что бот остался администратором чата.')
        return
    await panel(m,m.from_user.id)

@dp.message(Command('connect'),F.chat.type=='private')
async def connect(m:Message):
    try:
        cid=int(m.text.split()[1])
        if cid>=0 or not await allowed(cid,m.from_user.id):raise ValueError()
        chat=await bot.get_chat(cid)
        if not await is_admin(cid,bot.id):raise ValueError()
        remember_chat(chat);await show_chat(m,cid)
    except Exception:await m.answer('Укажи /connect -1001234567890. Ты и бот должны быть администраторами этого чата.')

@dp.message(Command('cancel'),F.chat.type=='private')
async def cancel(m:Message):
    db.execute('DELETE FROM drafts WHERE user_id=?',(m.from_user.id,));db.commit();await m.answer('Ввод отменён. Панель: /panel')

@dp.callback_query(F.data.startswith('p:'))
async def panel_click(c:CallbackQuery):
    if c.message is None or c.message.chat.type!='private' or c.message.chat.id!=c.from_user.id:
        await c.answer('Панель доступна только в ЛС.',show_alert=True);return
    try:
        parts=c.data.split(':');action=parts[1];cid=int(parts[2])
        if action=='home':await c.answer();await panel(c.message,c.from_user.id);return
        if not await allowed(cid,c.from_user.id):await c.answer('Нет прав администратора.',show_alert=True);return
        await c.answer()
        if action=='chat':await show_chat(c.message,cid)
        elif action=='toggle':
            w,t,e=config(cid);set_config(cid,w,t,1-e);await show_chat(c.message,cid)
        elif action in ('warnings','minutes','user'):
            db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)',(c.from_user.id,action,cid));db.commit()
            await c.message.answer({'warnings':'Введи лимит предупреждений от 1 до 100.','minutes':'Введи длительность мута от 1 до 10080 минут.','user':'Введи ID участника или @username (бот должен ранее видеть его сообщения).'}[action]+' Отмена: /cancel')
        elif action=='list':
            rows=db.execute('SELECT user_id FROM violations WHERE chat_id=? ORDER BY last_violation DESC',(cid,)).fetchall()
            rows=[(uid,count_violations(cid,uid,datetime.now(timezone.utc))) for (uid,) in rows]
            rows=[(uid,n) for uid,n in rows if n>0][:30]
            await c.message.answer('Последние участники с нарушениями (до 30):' if rows else 'Активных предупреждений нет.',reply_markup=kb([[(f'ID {uid}: {n}',f'p:view:{cid}:{uid}')] for uid,n in rows]) if rows else None)
        elif action=='inbox':
            rows=db.execute("SELECT * FROM appeals WHERE chat_id=? AND status='pending' ORDER BY rowid LIMIT 10",(cid,)).fetchall()
            if not rows:await c.message.answer('Необработанных заявок нет.')
            for row in rows:await show_appeal(c.message,row)
            if len(rows)==10:await c.message.answer('Показаны первые 10. После обработки открой список снова.')
        elif action in ('view','mute','unmute','reset'):
            uid=int(parts[3])
            if action=='mute':await mute_user(cid,uid,config(cid)[1])
            elif action=='unmute':await lift_mute(cid,uid)
            elif action=='reset':
                db.execute('DELETE FROM violations WHERE chat_id=? AND user_id=?',(cid,uid));db.commit()
            await show_user(c.message,cid,uid)
    except Exception:logging.exception('panel action failed');await c.message.answer('Не удалось выполнить действие. Проверь права бота и ID участника.')

@dp.callback_query(F.data.startswith('a:'))
async def decide(c:CallbackQuery):
    if c.message is None or c.message.chat.type!='private' or c.message.chat.id!=c.from_user.id:
        await c.answer('Решения доступны только в ЛС.',show_alert=True);return
    try:
        _,decision,aid=c.data.split(':')
        row=db.execute('SELECT * FROM appeals WHERE id=?',(aid,)).fetchone()
        if decision not in ('approve','reject') or not row or row[5]!='pending':
            await c.answer('Заявка уже обработана или недействительна.',show_alert=True);return
        _,cid,uid,_,_,_=row
        if not await allowed(cid,c.from_user.id):await c.answer('Нет прав администратора.',show_alert=True);return
        # Reserve synchronously before the first network operation: one decision only.
        claimed=db.execute("UPDATE appeals SET status='processing' WHERE id=? AND status='pending'",(aid,)).rowcount;db.commit()
        if not claimed:await c.answer("Заявка уже обрабатывается.",show_alert=True);return
        await c.answer('Обрабатываю…')
        try:
            if decision=='approve':await lift_mute(cid,uid)
        except Exception:
            db.execute("UPDATE appeals SET status='pending' WHERE id=?",(aid,));db.commit()
            await c.message.answer('Не удалось снять мут. Заявка сохранена, можно повторить.');return
        db.execute('UPDATE appeals SET status=? WHERE id=?',(decision,aid));db.commit()
        result='✅ Обжалование принято. Мут снят, предупреждения сброшены.' if decision=='approve' else '❌ Обжалование отклонено.'
        await c.message.edit_reply_markup(reply_markup=None)
        await c.message.answer(result)
        try:await bot.send_message(uid,result)
        except Exception:logging.info('User cannot receive appeal result')
    except Exception:logging.exception('appeal decision failed')

# Legacy group buttons only display a private-chat link; never publish appeal text.
@dp.callback_query(F.data.startswith('appeal_start:'))
async def legacy_appeal(c:CallbackQuery):
    from aiogram.utils.deep_linking import create_start_link
    try:
        cid=int(c.data.split(':')[1]);url=await create_start_link(bot,f'appeal_{cid}')
        await c.answer(url=url)
    except Exception:await c.answer('Открой ЛС бота через новую кнопку обжалования.',show_alert=True)
@dp.callback_query(F.data.startswith('appeal_decide:'))
async def legacy_decision(c:CallbackQuery):
    await c.answer('Старая заявка недействительна. Подай обжалование через ЛС бота.',show_alert=True)

@dp.message(Command('warns','rules'),F.chat.type.in_({'group','supergroup'}))
async def rules(m:Message):
    remember_chat(m.chat);w,t,_=config(m.chat.id)
    n=count_violations(m.chat.id,m.from_user.id,datetime.now(timezone.utc)) if m.from_user else 0
    await m.answer(f'Первые {w} нарушения — предупреждения, затем мут на {t} мин. Сброс после {RESET_AFTER_DAYS} дней без нарушений. Твой счётчик: {n}. Обжалование — через ЛС бота.')

@dp.message(Command('mute','unmute'),F.chat.type.in_({'group','supergroup'}))
async def group_action(m:Message):
    if not m.from_user or not await allowed(m.chat.id,m.from_user.id):return
    remember_chat(m.chat)
    parts=(m.text or '').split();cmd=parts[0].split('@')[0]
    uid=m.reply_to_message.from_user.id if m.reply_to_message and m.reply_to_message.from_user else None
    try:
        if cmd=='/unmute' and len(parts)>1:
            row=find_user(m.chat.id,parts[1]);uid=row[0] if row else int(parts[1])
        if uid is None:await m.answer('Ответь на сообщение участника этой командой. Управление в ЛС: /panel');return
        if cmd=='/unmute':await lift_mute(m.chat.id,uid);await m.answer('Мут снят, предупреждения сброшены.')
        else:
            minutes=int(parts[1]) if len(parts)>1 else config(m.chat.id)[1]
            if not 1<=minutes<=10080:raise ValueError()
            await mute_user(m.chat.id,uid,minutes)
            await m.answer(f'🔇 Участник ID {uid} получил мут на {minutes} мин.',reply_markup=await appeal_keyboard(m.chat.id))
    except Exception:await m.answer('Не удалось выполнить действие. Проверь участника, число минут (1–10080) и права бота.')

@dp.message(F.chat.type=='private')
async def private_input(m:Message):
    if not m.from_user:return
    row=db.execute('SELECT kind,chat_id FROM drafts WHERE user_id=?',(m.from_user.id,)).fetchone()
    if not row:await m.answer('Панель управления: /panel. Для обжалования используй кнопку под сообщением о муте.');return
    kind,cid=row;text=(m.text or '').strip()
    if not text or text.startswith('/'):await m.answer('Отправь текст или /cancel.');return
    if kind!='appeal' and not await allowed(cid,m.from_user.id):
        db.execute('DELETE FROM drafts WHERE user_id=?',(m.from_user.id,));db.commit();await m.answer('Нет прав администратора.');return
    try:
        if kind=='appeal':
            if len(text)>2000:await m.answer('Сократи причину до 2000 символов.');return
            member=await bot.get_chat_member(cid,m.from_user.id)
            if member.status!='restricted' or member.can_send_messages:await m.answer('Мут уже не активен. Отмена: /cancel');return
            aid=uuid.uuid4().hex[:16]
            try:
                db.execute('INSERT INTO appeals(id,chat_id,user_id,name,reason) VALUES(?,?,?,?,?)',(aid,cid,m.from_user.id,m.from_user.full_name,text));db.commit()
            except sqlite3.IntegrityError:await m.answer('Заявка уже ожидает решения.');return
            db.execute('DELETE FROM drafts WHERE user_id=?',(m.from_user.id,));db.commit()
            delivered=0
            appeal=db.execute('SELECT * FROM appeals WHERE id=?',(aid,)).fetchone()
            try:admins=await bot.get_chat_administrators(cid)
            except Exception:
                logging.exception("Cannot enumerate appeal recipients");admins=[]
            for admin in admins:
                if admin.user.is_bot:continue
                try:
                    await show_appeal(_PrivateRecipient(admin.user.id),appeal);delivered+=1
                except Exception:logging.info('Admin %s unavailable in private chat',admin.user.id)
            await m.answer('Заявка отправлена администраторам в ЛС.' if delivered else 'Заявка сохранена в панели. Администраторы увидят её, когда откроют раздел «Обжалования» в ЛС бота.')
            return
        if kind=='user':
            found=find_user(cid,text) if text.startswith('@') else None
            uid=found[0] if found else int(text);await show_user(m,cid,uid)
        else:
            value=int(text);w,t,e=config(cid)
            if kind=='warnings':
                if not 1<=value<=100:raise ValueError()
                w=value
            elif kind=='minutes':
                if not 1<=value<=10080:raise ValueError()
                t=value
            set_config(cid,w,t,e);await show_chat(m,cid)
        db.execute('DELETE FROM drafts WHERE user_id=?',(m.from_user.id,));db.commit()
    except ValueError:await m.answer('Некорректное значение. Попробуй ещё раз или /cancel.')
    except Exception:logging.exception('private input failed');await m.answer('Не удалось выполнить действие. Попробуй ещё раз или /cancel.')
class _PrivateRecipient:
    def __init__(self,uid):self.uid=uid
    async def answer(self,text,**kwargs):return await bot.send_message(self.uid,text,**kwargs)

@dp.message(F.chat.type.in_({'group','supergroup'}),F.text | F.caption)
async def moderation(m:Message):
    remember_chat(m.chat);u=m.from_user
    if not u or u.is_bot or m.sender_chat:return
    remember_user(m.chat.id,u)
    w,minutes,enabled=config(m.chat.id)
    if not enabled:return
    try:
        if await is_admin(m.chat.id,u.id):return
    except Exception:logging.exception('admin check failed');return
    if not is_offensive(m.text or m.caption or ''):return
    try:await m.delete()
    except Exception:logging.exception('delete failed')
    now=datetime.now(timezone.utc);cid,uid=m.chat.id,u.id
    count=count_violations(cid,uid,now)+1;save_count(cid,uid,count,now)
    if count<=w:
        await m.answer(f'⚠️ {mention(u)}, предупреждение {count}/{w}. Следующее после лимита — мут на {minutes} минут.',parse_mode='HTML');return
    try:
        await mute_user(cid,uid,minutes)
        await m.answer(f'🔇 {mention(u)} получил мут на {minutes} минут. Нарушение №{count}.',parse_mode='HTML',reply_markup=await appeal_keyboard(cid))
    except Exception:logging.exception('automatic mute failed');await m.answer('Не удалось выдать мут. Проверь права бота.')

async def main():
    # Interrupted decisions remain reviewable after an unexpected restart.
    db.execute("UPDATE appeals SET status='pending' WHERE status='processing'");db.commit()
    logging.info('Moderator with private panel started')
    async with bot:await dp.start_polling(bot)
if __name__=='__main__':asyncio.run(main())
