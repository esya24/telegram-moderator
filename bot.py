import asyncio, os, re, sqlite3, logging, uuid
from html import escape
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.filters import Command
from aiogram.types import Message, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv('BOT_TOKEN')
if not TOKEN: raise RuntimeError('Set BOT_TOKEN environment variable')
DB_PATH=os.getenv('DB_PATH','moderation.sqlite3')
WARNING_LIMIT=int(os.getenv('WARNING_LIMIT','3')); MUTE_MINUTES=int(os.getenv('MUTE_MINUTES','15')); RESET_AFTER_DAYS=int(os.getenv('RESET_AFTER_DAYS','7'))

from language_filter import analyze, normalize, is_offensive, is_directed_insult, has_latin_profanity

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
CREATE TABLE IF NOT EXISTS panels(user_id INTEGER PRIMARY KEY,message_id INTEGER);
CREATE TABLE IF NOT EXISTS private_users(user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS drafts(user_id INTEGER PRIMARY KEY,kind TEXT,chat_id INTEGER);
CREATE TABLE IF NOT EXISTS appeals(id TEXT PRIMARY KEY,chat_id INTEGER,user_id INTEGER,name TEXT,reason TEXT,status TEXT DEFAULT 'pending');
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_appeal ON appeals(chat_id,user_id) WHERE status IN ('pending','processing');
INSERT OR IGNORE INTO chats SELECT DISTINCT chat_id,CAST(chat_id AS TEXT) FROM chat_users WHERE chat_id<0;
''')
db.commit()
bot=Bot(TOKEN); dp=Dispatcher(events_isolation=SimpleEventIsolation())

def kb(rows):
    def button(item):
        text,data,*style=item
        return InlineKeyboardButton(text=text,callback_data=data,**({'style':style[0]} if style else {}))
    return InlineKeyboardMarkup(inline_keyboard=[[button(item) for item in row] for row in rows])

def back(cid):return kb([[('‹ Назад',f'p:chat:{cid}')],[('Главное меню','p:home:0')]])

def clear_draft(uid):
    db.execute('DELETE FROM drafts WHERE user_id=?',(uid,));db.commit()

# One durable message per private chat. Serialize creation/editing to avoid duplicates.
_panel_locks={}
async def render(m,text,reply_markup=None,**kwargs):
    uid=m.chat.id
    if reply_markup is None:reply_markup=kb([[('Главное меню','p:home:0')]])
    async with _panel_locks.setdefault(uid,asyncio.Lock()):
        row=db.execute('SELECT message_id FROM panels WHERE user_id=?',(uid,)).fetchone()
        if row:
            try:
                return await bot.edit_message_text(chat_id=uid,message_id=row[0],text=text,
                    parse_mode='HTML',reply_markup=reply_markup,**kwargs)
            except TelegramBadRequest as exc:
                reason=str(exc).lower()
                if 'message is not modified' in reason:return
                if not any(x in reason for x in ('message to edit not found',"message can't be edited",'message_id_invalid')):raise
        sent=await bot.send_message(uid,text,parse_mode='HTML',reply_markup=reply_markup,**kwargs)
        db.execute('INSERT OR REPLACE INTO panels VALUES(?,?)',(uid,sent.message_id));db.commit()

async def clean_input(m):
    # Telegram permits deletion of incoming private messages; failure is harmless.
    try:await m.delete()
    except Exception:logging.debug('Could not remove private input')

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
async def panel(m,uid,page=0):
    clear_draft(uid)
    rows=[]
    for cid,title in db.execute('SELECT chat_id,title FROM chats ORDER BY title').fetchall():
        if await allowed(cid,uid):rows.append([(title[:40],f'p:chat:{cid}')])
    text='<b>◈ Модератор</b>\n\nЧаты, участники и обжалования — в одной панели.\n\n'
    text+='Выберите чат для управления.' if rows else 'Пока нет подключённых чатов. Добавьте бота и себя в администраторы группы обсуждений.'
    page=max(0,min(page,max(0,(len(rows)-1)//6)))
    total=len(rows);rows=rows[page*6:page*6+6]
    nav=[]
    if page:nav.append(('‹',f'p:home:0:{page-1}'))
    if (page+1)*6<total:nav.append(('›',f'p:home:0:{page+1}'))
    if nav:rows.append(nav)
    rows += [[('＋ Подключить чат','p:connect:0','success')],[('Как это работает','p:help:0'),('Обновить','p:home:0','primary')]]
    await render(m,text,reply_markup=kb(rows))

async def show_chat(m,cid,note=''):
    w,minutes,enabled=config(cid)
    title=db.execute('SELECT title FROM chats WHERE chat_id=?',(cid,)).fetchone()
    total=db.execute("SELECT COUNT(*) FROM appeals WHERE chat_id=? AND status='pending'",(cid,)).fetchone()[0]
    await render(m,f'<b>◈ {escape(str(title[0] if title else cid))}</b>\n\n'
        f'Фильтр  ·  {"включён" if enabled else "выключен"}\nПредупреждения  ·  {w}\nМут  ·  {minutes} мин.\nОбжалования  ·  {total}'
        +(f'\n\n{escape(note)}' if note else ''),reply_markup=kb([
        [(('Приостановить фильтр' if enabled else 'Включить фильтр'),f'p:toggle:{cid}',('danger' if enabled else 'success'))],
        [('Предупреждения',f'p:warnings:{cid}'),('Время мута',f'p:minutes:{cid}')],
        [('Участники',f'p:user:{cid}'),('Нарушения',f'p:list:{cid}')],
        [(f'Обжалования · {total}',f'p:inbox:{cid}','primary')],
        [('Обновить',f'p:chat:{cid}'),('‹ Все чаты','p:home:0')]]))

async def show_user(m,cid,uid,note=''):
    member=await bot.get_chat_member(cid,uid)
    count=count_violations(cid,uid,datetime.now(timezone.utc))
    status={'restricted':'ограничен','member':'участник','administrator':'администратор','creator':'владелец','left':'вышел','kicked':'заблокирован'}.get(member.status,member.status)
    await render(m,f'<b>Участник</b>\n\n{escape(member.user.full_name)}\nID  ·  <code>{uid}</code>\nПредупреждения  ·  {count}\nСтатус  ·  {escape(str(status))}'
        +(f'\n\n{escape(note)}' if note else ''),reply_markup=kb([
        [('Выдать мут',f'p:confirmmute:{cid}:{uid}','danger'),('Снять мут',f'p:unmute:{cid}:{uid}','success')],
        [('Сбросить предупреждения',f'p:reset:{cid}:{uid}')],[('‹ Назад',f'p:chat:{cid}')]]))

async def show_inbox(m,cid,page=0,note=''):
    rows=db.execute("SELECT id,name FROM appeals WHERE chat_id=? AND status='pending' ORDER BY rowid",(cid,)).fetchall()
    page=max(0,min(page,max(0,(len(rows)-1)//6)))
    buttons=[[(name[:35],f'p:appeal:{cid}:{aid}')] for aid,name in rows[page*6:page*6+6]]
    nav=[]
    if page:nav.append(('‹',f'p:inbox:{cid}:{page-1}'))
    if (page+1)*6<len(rows):nav.append(('›',f'p:inbox:{cid}:{page+1}'))
    if nav:buttons.append(nav)
    buttons += [[('Обновить',f'p:inbox:{cid}:{page}','primary')],[('‹ Назад',f'p:chat:{cid}')]]
    await render(m,'<b>Обжалования</b>\n\n'+(f'Ожидают решения: {len(rows)} · Страница {page+1}\nВыберите заявку.' if rows else 'Все заявки обработаны.')+(f'\n\n{escape(note)}' if note else ''),reply_markup=kb(buttons))

async def show_appeal(m,row):
    aid,cid,uid,name,reason,status=row
    title=db.execute('SELECT title FROM chats WHERE chat_id=?',(cid,)).fetchone()
    buttons=[]
    if status=='pending':buttons=[[('Принять',f'a:approve:{aid}','success'),('Отклонить',f'a:reject:{aid}','danger')]]
    buttons += [[('‹ К заявкам',f'p:inbox:{cid}')]]
    await render(m,f'<b>Обжалование</b>\n\n{escape(str(title[0] if title else cid))}\n{escape(name)} · <code>{uid}</code>\n\n{escape(reason)}',reply_markup=kb(buttons))

async def start_appeal(m,cid):
    member=await bot.get_chat_member(cid,m.from_user.id)
    if member.status!='restricted' or member.can_send_messages:
        await render(m,'В этом чате у тебя нет активного мута.');return
    if db.execute("SELECT 1 FROM appeals WHERE chat_id=? AND user_id=? AND status IN ('pending','processing')",(cid,m.from_user.id)).fetchone():
        await render(m,'Твоя заявка уже ожидает решения.');return
    db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)',(m.from_user.id,'appeal',cid));db.commit()
    await render(m,'<b>Обжаловать мут</b>\n\nНапишите причину одним сообщением, до 2000 символов. Её увидят только администраторы в ЛС бота.',reply_markup=kb([[('Отмена','p:home:0')]]))

@dp.my_chat_member()
async def membership(event):remember_chat(event.chat)

@dp.message(Command('start','panel','admin'))
async def start(m:Message):
    if m.chat.type!='private':
        remember_chat(m.chat)
        from aiogram.utils.deep_linking import create_start_link
        await render(m,'Панель управления находится в ЛС бота.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть панель в ЛС',url=await create_start_link(bot,'panel'))]]));return
    db.execute('INSERT OR IGNORE INTO private_users VALUES(?)',(m.from_user.id,));db.commit()
    await clean_input(m)
    parts=(m.text or '').split(maxsplit=1)
    if len(parts)>1 and parts[1].startswith('appeal_'):
        try:await start_appeal(m,int(parts[1][7:]))
        except Exception:await render(m,'Не удалось проверить мут. Проверь, что бот остался администратором чата.')
        return
    await panel(m,m.from_user.id)

@dp.message(Command('connect'),F.chat.type=='private')
async def connect(m:Message):
    try:
        cid=int(m.text.split()[1])
        if cid>=0 or not await allowed(cid,m.from_user.id):raise ValueError()
        chat=await bot.get_chat(cid)
        if not await is_admin(cid,bot.id):raise ValueError()
        remember_chat(chat);clear_draft(m.from_user.id);await clean_input(m);await show_chat(m,cid)
    except Exception:await render(m,'Укажи /connect -1001234567890. Ты и бот должны быть администраторами этого чата.')

@dp.message(Command('cancel'),F.chat.type=='private')
async def cancel(m:Message):
    clear_draft(m.from_user.id)
    await clean_input(m)
    await panel(m,m.from_user.id)

@dp.callback_query(F.data.startswith('p:'))
async def panel_click(c:CallbackQuery):
    if c.message is None or c.message.chat.type!='private' or c.message.chat.id!=c.from_user.id:
        await c.answer('Панель доступна только в ЛС.',show_alert=True);return
    try:
        parts=c.data.split(':');action=parts[1];cid=int(parts[2])
        if action in ('home','help','connect'):
            await c.answer();clear_draft(c.from_user.id)
            if action=='home':await panel(c.message,c.from_user.id,int(parts[3]) if len(parts)>3 else 0)
            elif action=='help':await render(c.message,'<b>Как это работает</b>\n\nВыберите чат, настройте фильтр и управляйте участниками. Все разделы открываются в этой карточке.\n\nБоту нужны права удалять сообщения и ограничивать участников в группе обсуждений. Обжалования доступны только в личных сообщениях.',reply_markup=kb([[('‹ Главное меню','p:home:0')]]))
            else:
                db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)',(c.from_user.id,'connect',0));db.commit()
                await render(c.message,'<b>Подключить чат</b>\n\nОтправьте ID группы обсуждений, например <code>-1001234567890</code>. Вы и бот должны быть её администраторами.',reply_markup=kb([[('Отмена','p:home:0')]]))
            return
        if not await allowed(cid,c.from_user.id):await c.answer('Нет прав администратора.',show_alert=True);return
        await c.answer()
        clear_draft(c.from_user.id)
        if action=='chat':await show_chat(c.message,cid)
        elif action=='toggle':
            w,t,e=config(cid);set_config(cid,w,t,1-e);await show_chat(c.message,cid)
        elif action in ('warnings','minutes','user'):
            db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)',(c.from_user.id,action,cid));db.commit()
            await render(c.message,{'warnings':'Введи лимит предупреждений от 1 до 100.','minutes':'Введи длительность мута от 1 до 10080 минут.','user':'Введи ID участника или @username (бот должен ранее видеть его сообщения).'}[action],reply_markup=back(cid))
        elif action=='list':
            rows=db.execute('SELECT user_id FROM violations WHERE chat_id=? ORDER BY last_violation DESC',(cid,)).fetchall()
            rows=[(uid,count_violations(cid,uid,datetime.now(timezone.utc))) for (uid,) in rows]
            rows=[(uid,n) for uid,n in rows if n>0][:30]
            page=int(parts[3]) if len(parts)>3 else 0
            page=max(0,min(page,max(0,(len(rows)-1)//6)))
            buttons=[[(f'ID {uid} · {n}',f'p:view:{cid}:{uid}')] for uid,n in rows[page*6:page*6+6]]
            nav=[]
            if page:nav.append(('‹',f'p:list:{cid}:{page-1}'))
            if (page+1)*6<len(rows):nav.append(('›',f'p:list:{cid}:{page+1}'))
            if nav:buttons.append(nav)
            await render(c.message,'<b>Нарушения</b>\n\n'+('Последние 30 участников. Выберите карточку.' if rows else 'Активных предупреждений нет.'),reply_markup=kb(buttons+[[('‹ Назад',f'p:chat:{cid}')]]))
        elif action=='inbox':await show_inbox(c.message,cid,int(parts[3]) if len(parts)>3 else 0)
        elif action=='appeal':
            row=db.execute('SELECT * FROM appeals WHERE id=? AND chat_id=?',(parts[3],cid)).fetchone()
            if row:await show_appeal(c.message,row)
            else:await show_inbox(c.message,cid,note='Заявка больше недоступна.')
        elif action=='confirmmute':
            uid=int(parts[3])
            await render(c.message,f'<b>Выдать мут?</b>\n\nУчастник <code>{uid}</code> не сможет писать {config(cid)[1]} мин.',reply_markup=kb([[('Подтвердить',f'p:mute:{cid}:{uid}','danger')],[('Отмена',f'p:view:{cid}:{uid}')]]))
        elif action in ('view','mute','unmute','reset'):
            uid=int(parts[3])
            if action=='mute':await mute_user(cid,uid,config(cid)[1])
            elif action=='unmute':await lift_mute(cid,uid)
            elif action=='reset':
                db.execute('DELETE FROM violations WHERE chat_id=? AND user_id=?',(cid,uid));db.commit()
            await show_user(c.message,cid,uid,{'mute':'Мут выдан.','unmute':'Мут снят, предупреждения сброшены.','reset':'Предупреждения сброшены.'}.get(action,''))
    except Exception:logging.exception('panel action failed');await render(c.message,'Не удалось выполнить действие. Проверьте права бота и ID участника.',reply_markup=kb([[('Главное меню','p:home:0')]]))

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
        clear_draft(c.from_user.id)
        await c.answer('Обрабатываю…')
        try:
            if decision=='approve':await lift_mute(cid,uid)
        except Exception:
            db.execute("UPDATE appeals SET status='pending' WHERE id=?",(aid,));db.commit()
            await render(c.message,'Не удалось снять мут. Заявка сохранена, можно повторить.',reply_markup=kb([[('Повторить',f'p:appeal:{cid}:{aid}')],[('К заявкам',f'p:inbox:{cid}')]]));return
        db.execute('UPDATE appeals SET status=? WHERE id=?',(decision,aid));db.commit()
        result='✅ Обжалование принято. Мут снят, предупреждения сброшены.' if decision=='approve' else '❌ Обжалование отклонено.'
        await show_inbox(c.message,cid,note=result)
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
    await clean_input(m)
    row=db.execute('SELECT kind,chat_id FROM drafts WHERE user_id=?',(m.from_user.id,)).fetchone()
    if not row:await panel(m,m.from_user.id);return
    kind,cid=row;text=(m.text or '').strip()
    if not text or text.startswith('/'):await render(m,'Отправь текст или /cancel.');return
    if kind not in ('appeal','connect') and not await allowed(cid,m.from_user.id):
        db.execute('DELETE FROM drafts WHERE user_id=?',(m.from_user.id,));db.commit();await render(m,'Нет прав администратора.');return
    try:
        if kind=='connect':
            target=int(text)
            if target>=0 or not await allowed(target,m.from_user.id) or not await is_admin(target,bot.id):raise ValueError()
            remember_chat(await bot.get_chat(target))
            clear_draft(m.from_user.id)
            await show_chat(m,target);return
        if kind=='appeal':
            if len(text)>2000:await render(m,'Сократи причину до 2000 символов.');return
            member=await bot.get_chat_member(cid,m.from_user.id)
            if member.status!='restricted' or member.can_send_messages:await render(m,'Мут уже не активен. Отмена: /cancel');return
            aid=uuid.uuid4().hex[:16]
            try:
                db.execute('INSERT INTO appeals(id,chat_id,user_id,name,reason) VALUES(?,?,?,?,?)',(aid,cid,m.from_user.id,m.from_user.full_name,text));db.commit()
            except sqlite3.IntegrityError:await render(m,'Заявка уже ожидает решения.');return
            db.execute('DELETE FROM drafts WHERE user_id=?',(m.from_user.id,));db.commit()
            delivered=0
            appeal=db.execute('SELECT * FROM appeals WHERE id=?',(aid,)).fetchone()
            try:admins=await bot.get_chat_administrators(cid)
            except Exception:
                logging.exception("Cannot enumerate appeal recipients");admins=[]
            for admin in admins:
                if admin.user.is_bot:continue
                try:
                    await bot.send_message(admin.user.id,'Новое обжалование',reply_markup=kb([[('Открыть заявку',f'p:appeal:{cid}:{aid}','primary')]]));delivered+=1
                except Exception:logging.info('Admin %s unavailable in private chat',admin.user.id)
            await render(m,'Заявка отправлена администраторам в ЛС.' if delivered else 'Заявка сохранена в панели. Администраторы увидят её, когда откроют раздел «Обжалования» в ЛС бота.')
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
    except ValueError:await render(m,'<b>Проверьте ввод</b>\n\nВведите корректное значение. Для подключения чата вы и бот должны быть администраторами.',reply_markup=back(cid) if cid else kb([[('Отмена','p:home:0')]]))
    except Exception:logging.exception('private input failed');await render(m,'Не удалось выполнить действие. Проверьте права бота и повторите ввод.',reply_markup=back(cid) if cid else kb([[('Отмена','p:home:0')]]))
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
    verdict=analyze(m.text or m.caption or '',addressed=bool(m.reply_to_message and m.reply_to_message.from_user and m.reply_to_message.from_user.id!=u.id))
    if not verdict.offensive:return
    try:await m.delete()
    except Exception:logging.exception('delete failed')
    now=datetime.now(timezone.utc);cid,uid=m.chat.id,u.id
    count=count_violations(cid,uid,now)+1;save_count(cid,uid,count,now)
    if count<=w:
        await m.answer(f'⚠️ {mention(u)}, предупреждение {count}/{w} ({verdict.reason}). Следующее после лимита — мут на {minutes} минут.',parse_mode='HTML');return
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
