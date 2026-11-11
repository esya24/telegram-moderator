from ui_theme import text as ui_text
import asyncio, os, re, sqlite3, logging, uuid
from html import escape
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.filters import Command
from aiogram.types import Message, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('Set BOT_TOKEN environment variable')
DB_PATH = os.getenv('DB_PATH', 'moderation.sqlite3')
WARNING_LIMIT = int(os.getenv('WARNING_LIMIT', '3'))
MUTE_MINUTES = int(os.getenv('MUTE_MINUTES', '15'))
RESET_AFTER_DAYS = int(os.getenv('RESET_AFTER_DAYS', '7'))
from language_filter import analyze, normalize, is_offensive, is_directed_insult, has_latin_profanity
db = sqlite3.connect(DB_PATH)
db.execute('PRAGMA journal_mode=WAL')
db.execute('CREATE TABLE IF NOT EXISTS violations(chat_id INTEGER,user_id INTEGER,count INTEGER DEFAULT 0,last_violation TEXT,\nPRIMARY KEY(chat_id,user_id))')
db.execute('CREATE TABLE IF NOT EXISTS chat_users(chat_id INTEGER,user_id INTEGER,username TEXT,full_name TEXT,\nPRIMARY KEY(chat_id,user_id))')
db.commit()

def remember_user(chat_id, user):
    db.execute('INSERT OR IGNORE INTO member_profiles VALUES(?,?,?)', (chat_id, user.id, datetime.now(timezone.utc).isoformat()))
    db.execute('INSERT INTO chat_users VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET\n    username=excluded.username,full_name=excluded.full_name', (chat_id, user.id, (user.username or '').lower(), user.full_name or ''))
    db.commit()

def find_user(chat_id, username):
    return db.execute('SELECT user_id FROM chat_users WHERE chat_id=? AND username=?', (chat_id, username.lower().lstrip('@'))).fetchone()

def count_violations(chat_id, user_id, now):
    row = db.execute('SELECT count,last_violation FROM violations WHERE chat_id=? AND user_id=?', (chat_id, user_id)).fetchone()
    if not row:
        return 0
    try:
        last = datetime.fromisoformat(row[1])
    except (ValueError, TypeError):
        return 0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return row[0] if now - last <= timedelta(days=RESET_AFTER_DAYS) else 0

def save_count(chat_id, user_id, count, now):
    db.execute('INSERT INTO violations VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET\n    count=excluded.count,last_violation=excluded.last_violation', (chat_id, user_id, count, now.isoformat()))
    db.commit()
db.executescript("\nCREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY,title TEXT);\nCREATE TABLE IF NOT EXISTS settings(chat_id INTEGER PRIMARY KEY,warnings INTEGER,minutes INTEGER,enabled INTEGER DEFAULT 1);\nCREATE TABLE IF NOT EXISTS panels(user_id INTEGER PRIMARY KEY,message_id INTEGER);\nCREATE TABLE IF NOT EXISTS private_users(user_id INTEGER PRIMARY KEY);\nCREATE TABLE IF NOT EXISTS drafts(user_id INTEGER PRIMARY KEY,kind TEXT,chat_id INTEGER);\nCREATE TABLE IF NOT EXISTS appeals(id TEXT PRIMARY KEY,chat_id INTEGER,user_id INTEGER,name TEXT,reason TEXT,status TEXT DEFAULT 'pending');\nCREATE UNIQUE INDEX IF NOT EXISTS one_pending_appeal ON appeals(chat_id,user_id) WHERE status IN ('pending','processing');\nINSERT OR IGNORE INTO chats SELECT DISTINCT chat_id,CAST(chat_id AS TEXT) FROM chat_users WHERE chat_id<0;\n")
db.commit()
import ui_theme
ui_theme.configure(db)
bot = Bot(TOKEN,default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher(events_isolation=SimpleEventIsolation())

def kb(rows):

    def button(item):
        text, data, *style = item
        chosen=ui_theme.style(text,style[0] if style else None)
        return InlineKeyboardButton(text=str(text),callback_data=data,**({'style':chosen} if chosen else {}))
    return InlineKeyboardMarkup(inline_keyboard=[[button(item) for item in row] for row in ui_theme.layout(rows)])

def back(cid):
    return kb([[(ui_text('c1ff8780621d'), f'p:chat:{cid}')], [(ui_text('c20945ceeebc'), 'p:home:0')]])

def clear_draft(uid):
    db.execute('DELETE FROM ui_drafts WHERE user_id=?',(uid,))
    db.execute('DELETE FROM drafts WHERE user_id=?', (uid,))
    db.commit()
_panel_locks = {}

async def render(m, text, reply_markup=None, **kwargs):
    uid = m.chat.id
    if reply_markup is None:
        reply_markup = kb([[(ui_text('c20945ceeebc'), 'p:home:0')]])
    async with _panel_locks.setdefault(uid, asyncio.Lock()):
        row = db.execute('SELECT message_id FROM panels WHERE user_id=?', (uid,)).fetchone()
        if row:
            try:
                return await bot.edit_message_text(chat_id=uid, message_id=row[0], text=text, parse_mode='HTML', reply_markup=reply_markup, **kwargs)
            except TelegramBadRequest as exc:
                reason = str(exc).lower()
                if 'message is not modified' in reason:
                    return
                if not any((x in reason for x in ('message to edit not found', "message can't be edited", 'message_id_invalid'))):
                    raise
        sent = await bot.send_message(uid, text, parse_mode='HTML', reply_markup=reply_markup, **kwargs)
        db.execute('INSERT OR REPLACE INTO panels VALUES(?,?)', (uid, sent.message_id))
        db.commit()

async def clean_input(m):
    try:
        await m.delete()
    except Exception:
        logging.debug('Could not remove private input')

def remember_chat(chat):
    if chat.type in ('group', 'supergroup', 'channel'):
        db.execute('INSERT INTO chats VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title', (chat.id, chat.title or str(chat.id)))
        db.commit()

def config(cid):
    row = db.execute('SELECT warnings,minutes,enabled FROM settings WHERE chat_id=?', (cid,)).fetchone()
    return row or (WARNING_LIMIT, MUTE_MINUTES, 1)

def set_config(cid, warnings, minutes, enabled):
    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?,?)', (cid, warnings, minutes, enabled))
    db.commit()

def mention(user):
    return '@' + user.username if user.username else f"""<a href="tg://user?id={user.id}">{escape(user.full_name or ui_text('1f690ad82730'))}</a>"""

async def is_admin(cid, uid):
    return (await bot.get_chat_member(cid, uid)).status in ('administrator', 'creator')

async def allowed(cid, uid):
    try:
        return await is_admin(cid, uid)
    except Exception:
        return False

async def writable_target(cid, uid):
    member = await bot.get_chat_member(cid, uid)
    if member.status in ('creator', 'administrator', 'left', 'kicked') or member.user.is_bot:
        raise ValueError(ui_text('0c3eb76aa6af'))

async def lift_mute(cid, uid):
    await writable_target(cid, uid)
    permissions = ChatPermissions(**{key: True for key in ChatPermissions.model_fields})
    await bot.restrict_chat_member(cid, uid, permissions=permissions, use_independent_chat_permissions=True)
    db.execute('DELETE FROM violations WHERE chat_id=? AND user_id=?', (cid, uid))
    db.commit()

async def mute_user(cid, uid, minutes):
    await writable_target(cid, uid)
    if (await bot.get_chat(cid)).type != 'supergroup':
        raise ValueError(ui_text('c4d4ae148682'))
    await bot.restrict_chat_member(cid, uid, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes))

async def appeal_keyboard(cid):
    from aiogram.utils.deep_linking import create_start_link
    url = await create_start_link(bot, f'appeal_{cid}')
    return InlineKeyboardMarkup(inline_keyboard=[[ui_theme.button(text=ui_text('28729c6f31dd'), url=url)]])

async def panel(m, uid, page=0):
    clear_draft(uid)
    rows = []
    for cid, title in db.execute('SELECT chat_id,title FROM chats ORDER BY title').fetchall():
        if await allowed(cid, uid):
            rows.append([(title[:40], f'p:chat:{cid}')])
    text = ui_text('6efdb3b3b163')
    text += ui_text('9ec4593f2aa5') if rows else ui_text('cdfd00e80265')
    page = max(0, min(page, max(0, (len(rows) - 1) // 6)))
    total = len(rows)
    rows = rows[page * 6:page * 6 + 6]
    nav = []
    if page:
        nav.append((ui_text('c2b3f927088e'), f'p:home:0:{page - 1}'))
    if (page + 1) * 6 < total:
        nav.append((ui_text('53039c3d2d29'), f'p:home:0:{page + 1}'))
    if nav:
        rows.append(nav)
    rows += [[(ui_text('fec2b2edfb22'), 'p:connect:0', 'success')], [(ui_text('7e8aac83b310'), 'p:help:0'), (ui_text('808970003ae5'), 'p:home:0', 'primary')]]
    if ui_theme.is_owner(uid):rows.append([('🎨 Оформление','u:home')])
    await render(m, text, reply_markup=kb(rows))

async def show_chat(m, cid, note=''):
    w, minutes, enabled = config(cid)
    title = db.execute('SELECT title FROM chats WHERE chat_id=?', (cid,)).fetchone()
    total = db.execute("SELECT COUNT(*) FROM appeals WHERE chat_id=? AND status='pending'", (cid,)).fetchone()[0]
    await render(m, ui_text('7460d3ea71e0', v0=f'{escape(str(title[0] if title else cid))}', v1=f"{__import__('moderation_store').mode(db, cid)}", v2=f"{(ui_text('d607794aa65c') if enabled else ui_text('59e61d989724'))}", v3=f'{w}', v4=f'{minutes}', v5=f'{total}') + (f'\n\n{escape(note)}' if note else ''), reply_markup=kb([[(ui_text('cb6f91a5c8bf') if enabled else ui_text('64c59b4c5438'), f'p:toggle:{cid}', 'danger' if enabled else 'success')], [(ui_text('348ce92dffca'), f'p:warnings:{cid}'), (ui_text('653d48f72af6'), f'p:minutes:{cid}')], [(ui_text('9bc0685f61f5'), f'p:user:{cid}'), (ui_text('29adc9309c43'), f'p:list:{cid}')], [(ui_text('79b5ce112c02'), f'q:settings:{cid}'), (ui_text('d2b8c03ef0d8'), f'q:queue:{cid}:0')], [(ui_text('53d92a6a5b01'), f'q:history:{cid}:0')], [(ui_text('cd59f9bfc7a8', v0=f'{total}'), f'p:inbox:{cid}', 'primary')], [(ui_text('808970003ae5'), f'p:chat:{cid}'), (ui_text('23e67a4e5347'), 'p:home:0')]]))

async def show_user(m, cid, uid, note=''):
    member = await bot.get_chat_member(cid, uid)
    count = count_violations(cid, uid, datetime.now(timezone.utc))
    status = {'restricted': ui_text('3dbcfa050a2b'), 'member': ui_text('ccd79e755b49'), 'administrator': ui_text('b263ebf9e181'), 'creator': ui_text('2cccf1cf398d'), 'left': ui_text('030a684d5681'), 'kicked': ui_text('964466896474')}.get(member.status, member.status)
    await render(m, ui_text('b6dfca22b845', v0=f'{escape(member.user.full_name)}', v1=f'{uid}', v2=f'{count}', v3=f'{escape(str(status))}') + (f'\n\n{escape(note)}' if note else ''), reply_markup=kb([[(ui_text('1886d473bfc1'), f'p:confirmmute:{cid}:{uid}', 'danger'), (ui_text('1e36aa6a752b'), f'p:unmute:{cid}:{uid}', 'success')], [(ui_text('9edd61654e61'), f'p:reset:{cid}:{uid}')], [(ui_text('c1ff8780621d'), f'p:chat:{cid}')]]))

async def show_inbox(m, cid, page=0, note=''):
    rows = db.execute("SELECT id,name FROM appeals WHERE chat_id=? AND status='pending' ORDER BY rowid", (cid,)).fetchall()
    page = max(0, min(page, max(0, (len(rows) - 1) // 6)))
    buttons = [[(name[:35], f'p:appeal:{cid}:{aid}')] for aid, name in rows[page * 6:page * 6 + 6]]
    nav = []
    if page:
        nav.append((ui_text('c2b3f927088e'), f'p:inbox:{cid}:{page - 1}'))
    if (page + 1) * 6 < len(rows):
        nav.append((ui_text('53039c3d2d29'), f'p:inbox:{cid}:{page + 1}'))
    if nav:
        buttons.append(nav)
    buttons += [[(ui_text('808970003ae5'), f'p:inbox:{cid}:{page}', 'primary')], [(ui_text('c1ff8780621d'), f'p:chat:{cid}')]]
    await render(m, ui_text('15d4c76ee0dc') + (ui_text('08daed33c25d', v0=f'{len(rows)}', v1=f'{page + 1}') if rows else ui_text('a32d41f74d27')) + (f'\n\n{escape(note)}' if note else ''), reply_markup=kb(buttons))

async def show_appeal(m, row):
    aid, cid, uid, name, reason, status = row
    title = db.execute('SELECT title FROM chats WHERE chat_id=?', (cid,)).fetchone()
    buttons = []
    if status == 'pending':
        buttons = [[(ui_text('3800e260501a'), f'a:approve:{aid}', 'success'), (ui_text('1c7e1b622835'), f'a:reject:{aid}', 'danger')]]
    buttons += [[(ui_text('c85e45cf0eff'), f'p:inbox:{cid}')]]
    await render(m, ui_text('84f177f5098a', v0=f'{escape(str(title[0] if title else cid))}', v1=f'{escape(name)}', v2=f'{uid}', v3=f'{escape(reason)}'), reply_markup=kb(buttons))

async def start_appeal(m, cid):
    member = await bot.get_chat_member(cid, m.from_user.id)
    if member.status != 'restricted' or member.can_send_messages:
        await render(m, ui_text('e8ba749a717e'))
        return
    if db.execute("SELECT 1 FROM appeals WHERE chat_id=? AND user_id=? AND status IN ('pending','processing')", (cid, m.from_user.id)).fetchone():
        await render(m, ui_text('32f03e7696a7'))
        return
    db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)', (m.from_user.id, 'appeal', cid))
    db.commit()
    await render(m, ui_text('d15ab719bf8e'), reply_markup=kb([[(ui_text('1c17b29ec9bd'), 'p:home:0')]]))

@dp.my_chat_member()
async def membership(event):
    remember_chat(event.chat)

@dp.message(Command('start', 'panel', 'admin'))
async def start(m: Message):
    if m.chat.type != 'private':
        remember_chat(m.chat)
        from aiogram.utils.deep_linking import create_start_link
        await render(m, ui_text('942bcaae65d6'), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[ui_theme.button(text=ui_text('b02518e08c3d'), url=await create_start_link(bot, 'panel'))]]))
        return
    db.execute('INSERT OR IGNORE INTO private_users VALUES(?)', (m.from_user.id,))
    db.commit()
    await clean_input(m)
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith('appeal_'):
        try:
            await start_appeal(m, int(parts[1][7:]))
        except Exception:
            await render(m, ui_text('d5042e53e2b8'))
        return
    await panel(m, m.from_user.id)

@dp.message(Command('connect'), F.chat.type == 'private')
async def connect(m: Message):
    try:
        cid = int(m.text.split()[1])
        if cid >= 0 or not await allowed(cid, m.from_user.id):
            raise ValueError()
        chat = await bot.get_chat(cid)
        if not await is_admin(cid, bot.id):
            raise ValueError()
        remember_chat(chat)
        clear_draft(m.from_user.id)
        await clean_input(m)
        await show_chat(m, cid)
    except Exception:
        await render(m, ui_text('9adae3b5a152'))

@dp.message(Command('cancel'), F.chat.type == 'private')
async def cancel(m: Message):
    clear_draft(m.from_user.id)
    await clean_input(m)
    await panel(m, m.from_user.id)

@dp.callback_query(F.data.startswith('p:'))
async def panel_click(c: CallbackQuery):
    if c.message is None or c.message.chat.type != 'private' or c.message.chat.id != c.from_user.id:
        await c.answer(ui_text('3bc5bf2f8837'), show_alert=True)
        return
    try:
        parts = c.data.split(':')
        action = parts[1]
        cid = int(parts[2])
        if action in ('home', 'help', 'connect'):
            await c.answer()
            clear_draft(c.from_user.id)
            if action == 'home':
                await panel(c.message, c.from_user.id, int(parts[3]) if len(parts) > 3 else 0)
            elif action == 'help':
                await render(c.message, ui_text('08507f3dffbc'), reply_markup=kb([[(ui_text('b3ff4a18f846'), 'p:home:0')]]))
            else:
                db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)', (c.from_user.id, 'connect', 0))
                db.commit()
                await render(c.message, ui_text('711486dc5d9a'), reply_markup=kb([[(ui_text('1c17b29ec9bd'), 'p:home:0')]]))
            return
        if not await allowed(cid, c.from_user.id):
            await c.answer(ui_text('4b11439aefa6'), show_alert=True)
            return
        await c.answer()
        clear_draft(c.from_user.id)
        if action == 'chat':
            await show_chat(c.message, cid)
        elif action == 'toggle':
            w, t, e = config(cid)
            set_config(cid, w, t, 1 - e)
            await show_chat(c.message, cid)
        elif action in ('warnings', 'minutes', 'user'):
            db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)', (c.from_user.id, action, cid))
            db.commit()
            await render(c.message, {'warnings': ui_text('bd6d444a9285'), 'minutes': ui_text('6cf83ed60c27'), 'user': ui_text('f3e684a99571')}[action], reply_markup=back(cid))
        elif action == 'list':
            rows = db.execute('SELECT user_id FROM violations WHERE chat_id=? ORDER BY last_violation DESC', (cid,)).fetchall()
            rows = [(uid, count_violations(cid, uid, datetime.now(timezone.utc))) for uid, in rows]
            rows = [(uid, n) for uid, n in rows if n > 0][:30]
            page = int(parts[3]) if len(parts) > 3 else 0
            page = max(0, min(page, max(0, (len(rows) - 1) // 6)))
            buttons = [[(ui_text('e88d922a436a', v0=f'{uid}', v1=f'{n}'), f'p:view:{cid}:{uid}')] for uid, n in rows[page * 6:page * 6 + 6]]
            nav = []
            if page:
                nav.append((ui_text('c2b3f927088e'), f'p:list:{cid}:{page - 1}'))
            if (page + 1) * 6 < len(rows):
                nav.append((ui_text('53039c3d2d29'), f'p:list:{cid}:{page + 1}'))
            if nav:
                buttons.append(nav)
            await render(c.message, ui_text('0889085cbdb8') + (ui_text('77211aeb983d') if rows else ui_text('07a82705517b')), reply_markup=kb(buttons + [[(ui_text('c1ff8780621d'), f'p:chat:{cid}')]]))
        elif action == 'inbox':
            await show_inbox(c.message, cid, int(parts[3]) if len(parts) > 3 else 0)
        elif action == 'appeal':
            row = db.execute('SELECT * FROM appeals WHERE id=? AND chat_id=?', (parts[3], cid)).fetchone()
            if row:
                await show_appeal(c.message, row)
            else:
                await show_inbox(c.message, cid, note=ui_text('310774d47c56'))
        elif action == 'confirmmute':
            uid = int(parts[3])
            await render(c.message, ui_text('2b9c2552b96a', v0=f'{uid}', v1=f'{config(cid)[1]}'), reply_markup=kb([[(ui_text('d3b8348beddf'), f'p:mute:{cid}:{uid}', 'danger')], [(ui_text('1c17b29ec9bd'), f'p:view:{cid}:{uid}')]]))
        elif action in ('view', 'mute', 'unmute', 'reset'):
            uid = int(parts[3])
            if action == 'mute':
                await mute_user(cid, uid, config(cid)[1])
            elif action == 'unmute':
                await lift_mute(cid, uid)
            elif action == 'reset':
                db.execute('DELETE FROM violations WHERE chat_id=? AND user_id=?', (cid, uid))
                db.commit()
            await show_user(c.message, cid, uid, {'mute': ui_text('44a9385dcf96'), 'unmute': ui_text('d0b3378cfcaf'), 'reset': ui_text('407755fe0f23')}.get(action, ''))
    except Exception:
        logging.exception('panel action failed')
        await render(c.message, ui_text('73d87f95e939'), reply_markup=kb([[(ui_text('c20945ceeebc'), 'p:home:0')]]))

@dp.callback_query(F.data.startswith('a:'))
async def decide(c: CallbackQuery):
    if c.message is None or c.message.chat.type != 'private' or c.message.chat.id != c.from_user.id:
        await c.answer(ui_text('90140e43e069'), show_alert=True)
        return
    try:
        _, decision, aid = c.data.split(':')
        row = db.execute('SELECT * FROM appeals WHERE id=?', (aid,)).fetchone()
        if decision not in ('approve', 'reject') or not row or row[5] != 'pending':
            await c.answer(ui_text('6e85116e3848'), show_alert=True)
            return
        _, cid, uid, _, _, _ = row
        if not await allowed(cid, c.from_user.id):
            await c.answer(ui_text('4b11439aefa6'), show_alert=True)
            return
        claimed = db.execute("UPDATE appeals SET status='processing' WHERE id=? AND status='pending'", (aid,)).rowcount
        db.commit()
        if not claimed:
            await c.answer(ui_text('a52b3cfa2a8a'), show_alert=True)
            return
        clear_draft(c.from_user.id)
        await c.answer(ui_text('1894ed4325f8'))
        try:
            if decision == 'approve':
                await lift_mute(cid, uid)
        except Exception:
            db.execute("UPDATE appeals SET status='pending' WHERE id=?", (aid,))
            db.commit()
            await render(c.message, ui_text('f0cfe54b3437'), reply_markup=kb([[(ui_text('5bac65cb07e1'), f'p:appeal:{cid}:{aid}')], [(ui_text('180473624e22'), f'p:inbox:{cid}')]]))
            return
        db.execute('UPDATE appeals SET status=? WHERE id=?', (decision, aid))
        db.commit()
        import moderation_store
        moderation_store.audit(db, cid, uid, c.from_user.id, 'appeal_' + decision, details=aid)
        result = ui_text('2f8bdd486f10') if decision == 'approve' else ui_text('74f75b558dcd')
        await show_inbox(c.message, cid, note=result)
        try:
            await bot.send_message(uid, result)
        except Exception:
            logging.info('User cannot receive appeal result')
    except Exception:
        logging.exception('appeal decision failed')

@dp.callback_query(F.data.startswith('appeal_start:'))
async def legacy_appeal(c: CallbackQuery):
    from aiogram.utils.deep_linking import create_start_link
    try:
        cid = int(c.data.split(':')[1])
        url = await create_start_link(bot, f'appeal_{cid}')
        await c.answer(url=url)
    except Exception:
        await c.answer(ui_text('b5e3c85f7820'), show_alert=True)

@dp.callback_query(F.data.startswith('appeal_decide:'))
async def legacy_decision(c: CallbackQuery):
    await c.answer(ui_text('61151ccc74fc'), show_alert=True)

@dp.message(Command('warns', 'rules'), F.chat.type.in_({'group', 'supergroup'}))
async def rules(m: Message):
    remember_chat(m.chat)
    w, t, _ = config(m.chat.id)
    n = count_violations(m.chat.id, m.from_user.id, datetime.now(timezone.utc)) if m.from_user else 0
    await m.answer(ui_text('2f2c31d1c67b', v0=f'{w}', v1=f'{t}', v2=f'{RESET_AFTER_DAYS}', v3=f'{n}'))

@dp.message(Command('mute', 'unmute'), F.chat.type.in_({'group', 'supergroup'}))
async def group_action(m: Message):
    if not m.from_user or not await allowed(m.chat.id, m.from_user.id):
        return
    remember_chat(m.chat)
    parts = (m.text or '').split()
    cmd = parts[0].split('@')[0]
    uid = m.reply_to_message.from_user.id if m.reply_to_message and m.reply_to_message.from_user else None
    try:
        if cmd == '/unmute' and len(parts) > 1:
            row = find_user(m.chat.id, parts[1])
            uid = row[0] if row else int(parts[1])
        if uid is None:
            await m.answer(ui_text('4266f1fc07d3'))
            return
        if cmd == '/unmute':
            await lift_mute(m.chat.id, uid)
            await m.answer(ui_text('d0b3378cfcaf'))
        else:
            minutes = int(parts[1]) if len(parts) > 1 else config(m.chat.id)[1]
            if not 1 <= minutes <= 10080:
                raise ValueError()
            await mute_user(m.chat.id, uid, minutes)
            await m.answer(ui_text('75fe50ac9385', v0=f'{uid}', v1=f'{minutes}'), reply_markup=await appeal_keyboard(m.chat.id))
    except Exception:
        await m.answer(ui_text('68e5811e6d09'))

@dp.message(Command('id'), F.chat.type=='private')
async def own_id(m:Message):
    await clean_input(m)
    await render(m,f'Твой Telegram ID: <code>{m.from_user.id}</code>')

@dp.message(Command('design'), F.chat.type=='private')
async def design(m:Message):
    if not ui_theme.is_owner(m.from_user.id):
        await render(m,'Настройки оформления доступны только создателю бота.');return
    await clean_input(m)
    await owner_design(m)

@dp.message(F.chat.type == 'private')
async def private_input(m: Message):
    if m.from_user and await owner_design_input(m):return
    if not m.from_user:
        return
    await clean_input(m)
    row = db.execute('SELECT kind,chat_id FROM drafts WHERE user_id=?', (m.from_user.id,)).fetchone()
    if not row:
        await panel(m, m.from_user.id)
        return
    kind, cid = row
    text = (m.text or '').strip()
    if not text or text.startswith('/'):
        await render(m, ui_text('247ced0bfdb7'))
        return
    if kind not in ('appeal', 'connect') and (not await allowed(cid, m.from_user.id)):
        db.execute('DELETE FROM drafts WHERE user_id=?', (m.from_user.id,))
        db.commit()
        await render(m, ui_text('4b11439aefa6'))
        return
    try:
        if kind == 'connect':
            target = int(text)
            if target >= 0 or not await allowed(target, m.from_user.id) or (not await is_admin(target, bot.id)):
                raise ValueError()
            remember_chat(await bot.get_chat(target))
            clear_draft(m.from_user.id)
            await show_chat(m, target)
            return
        if kind == 'appeal':
            if len(text) > 2000:
                await render(m, ui_text('b2de4e1f01d3'))
                return
            member = await bot.get_chat_member(cid, m.from_user.id)
            if member.status != 'restricted' or member.can_send_messages:
                await render(m, ui_text('473201abe877'))
                return
            aid = uuid.uuid4().hex[:16]
            try:
                db.execute('INSERT INTO appeals(id,chat_id,user_id,name,reason) VALUES(?,?,?,?,?)', (aid, cid, m.from_user.id, m.from_user.full_name, text))
                db.commit()
            except sqlite3.IntegrityError:
                await render(m, ui_text('b7599b32dd9d'))
                return
            db.execute('DELETE FROM drafts WHERE user_id=?', (m.from_user.id,))
            db.commit()
            delivered = 0
            appeal = db.execute('SELECT * FROM appeals WHERE id=?', (aid,)).fetchone()
            try:
                admins = await bot.get_chat_administrators(cid)
            except Exception:
                logging.exception('Cannot enumerate appeal recipients')
                admins = []
            for admin in admins:
                if admin.user.is_bot:
                    continue
                try:
                    await bot.send_message(admin.user.id, ui_text('1cc8c75acc70'), reply_markup=kb([[(ui_text('86e1ebfed67d'), f'p:appeal:{cid}:{aid}', 'primary')]]))
                    delivered += 1
                except Exception:
                    logging.info('Admin %s unavailable in private chat', admin.user.id)
            await render(m, ui_text('222ec2fbdc5b') if delivered else ui_text('e190f23ee535'))
            return
        if kind == 'user':
            found = find_user(cid, text) if text.startswith('@') else None
            uid = found[0] if found else int(text)
            await show_user(m, cid, uid)
        else:
            value = int(text)
            w, t, e = config(cid)
            if kind == 'warnings':
                if not 1 <= value <= 100:
                    raise ValueError()
                w = value
            elif kind == 'minutes':
                if not 1 <= value <= 10080:
                    raise ValueError()
                t = value
            set_config(cid, w, t, e)
            await show_chat(m, cid)
        db.execute('DELETE FROM drafts WHERE user_id=?', (m.from_user.id,))
        db.commit()
    except ValueError:
        await render(m, ui_text('5e652fc85ecb'), reply_markup=back(cid) if cid else kb([[(ui_text('1c17b29ec9bd'), 'p:home:0')]]))
    except Exception:
        logging.exception('private input failed')
        await render(m, ui_text('88c664e96810'), reply_markup=back(cid) if cid else kb([[(ui_text('1c17b29ec9bd'), 'p:home:0')]]))

@dp.message(F.chat.type.in_({'group', 'supergroup'}), F.text | F.caption)
async def moderation(m: Message):
    pass
import sys
from moderator2 import install
install(sys.modules[__name__])
import ui_owner
ui_owner.install(sys.modules[__name__])

async def main():
    db.execute("UPDATE appeals SET status='pending' WHERE status='processing'")
    db.commit()
    db.execute("UPDATE decisions SET review_status='uncertain' WHERE review_status='processing'")
    db.commit()
    cleanup_task = asyncio.create_task(cleanup_moderation())
    logging.info('Moderator 2.0 with private panel started')
    async with bot:
        await dp.start_polling(bot)
if __name__ == '__main__':
    asyncio.run(main())
