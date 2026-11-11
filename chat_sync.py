from ui_theme import text as ui_text
"""Discover chats from updates, stored IDs and Telegram's native chat picker."""
import asyncio, secrets
from aiogram import BaseMiddleware, F
from aiogram.types import KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardMarkup, ReplyKeyboardRemove


def install(a):
    a.db.execute('CREATE TABLE IF NOT EXISTS chat_pick_requests(user_id INTEGER PRIMARY KEY,request_id INTEGER)');a.db.commit()

    def restore():
        # Recover IDs even if a previous version did not populate the chats table.
        for table in ('chat_users','violations','settings','appeals','decisions','member_profiles','audit_log','restrictions','ai_settings'):
            a.db.execute(f'INSERT OR IGNORE INTO chats SELECT DISTINCT chat_id,CAST(chat_id AS TEXT) FROM {table} WHERE chat_id<0')
        a.db.commit()
    restore()

    class Discover(BaseMiddleware):
        async def __call__(self,handler,event,data):
            for field in ('message','edited_message','channel_post','edited_channel_post','my_chat_member','chat_member','chat_join_request'):
                item=getattr(event,field,None)
                if item:
                    chat=getattr(item,'chat',None)
                    if chat:a.remember_chat(chat)
                    # Automatic forwards from a linked channel also expose its ID.
                    origin=getattr(item,'forward_origin',None)
                    if origin and getattr(origin,'chat',None):a.remember_chat(origin.chat)
            return await handler(event,data)
    a.dp.update.outer_middleware(Discover())

    async def add(cid,uid):
        if cid>=0 or not await a.allowed(cid,uid):raise ValueError('Ты должен быть администратором выбранного чата.')
        chat=await a.bot.get_chat(cid)
        if not await a.is_admin(cid,a.bot.id):raise ValueError('Бот должен быть администратором чата.')
        a.remember_chat(chat)
        linked=getattr(chat,'linked_chat_id',None)
        if isinstance(linked,int) and linked<0 and await a.allowed(linked,uid):
            try:
                if await a.is_admin(linked,a.bot.id):a.remember_chat(await a.bot.get_chat(linked))
            except Exception:a.logging.info('Linked discussion unavailable')
        return chat

    async def sync(uid):
        restore();ids=[r[0] for r in a.db.execute('SELECT chat_id FROM chats').fetchall()]
        slots=asyncio.Semaphore(4)
        found=0
        async def refresh(cid):
            nonlocal found
            async with slots:
                try:
                    await asyncio.wait_for(add(cid,uid),timeout=5);found+=1
                except Exception:pass
        try:await asyncio.wait_for(asyncio.gather(*(refresh(cid) for cid in ids)),timeout=15)
        except asyncio.TimeoutError:pass
        return found

    async def choose(m,uid):
        a.clear_draft(uid)
        rid=secrets.randbelow(2**30)
        a.db.execute('INSERT OR REPLACE INTO chat_pick_requests VALUES(?,?)',(uid,rid));a.db.commit()
        # Chat selection requires Telegram's reply keyboard; ordinary panel navigation stays inline.
        keyboard=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text=ui_text('46b733c94565'),request_chat=KeyboardButtonRequestChat(request_id=rid,chat_is_channel=False,bot_is_member=True,request_title=True))],
            [KeyboardButton(text=ui_text('b48c31408974'),request_chat=KeyboardButtonRequestChat(request_id=rid+1,chat_is_channel=True,bot_is_member=True,request_title=True))],
            [KeyboardButton(text='Отменить выбор чата')]],resize_keyboard=True,one_time_keyboard=True)
        await a.bot.send_message(uid,ui_text('ae386efa179c'),reply_markup=keyboard)

    async def shared(m):
        if not m.from_user:return
        row=a.db.execute('SELECT request_id FROM chat_pick_requests WHERE user_id=?',(m.from_user.id,)).fetchone()
        share=getattr(m,'chat_shared',None)
        if not row or not share or share.request_id not in (row[0],row[0]+1):
            await a.render(m,ui_text('b6db8045441d'));return
        try:
            chat=await add(share.chat_id,m.from_user.id)
        except Exception as exc:
            await a.render(m,'Не удалось подключить чат. '+a.escape(str(exc))[:300],reply_markup=a.kb([[(ui_text('05c2943f3f6b'),'s:choose')],[(ui_text('c20945ceeebc'),'p:home:0')]]));return
        a.db.execute('DELETE FROM chat_pick_requests WHERE user_id=?',(m.from_user.id,));a.db.commit()
        await a.bot.send_message(m.from_user.id,ui_text('93c332681647'),reply_markup=ReplyKeyboardRemove())
        await a.show_chat(m,chat.id)

    async def cancel_choice(m):
        a.db.execute('DELETE FROM chat_pick_requests WHERE user_id=?',(m.from_user.id,));a.db.commit()
        await a.bot.send_message(m.from_user.id,ui_text('f3d71269a3f3'),reply_markup=ReplyKeyboardRemove())
        await a.panel(m,m.from_user.id)

    @a.dp.callback_query(F.data.startswith('s:'))
    async def click(c):
        if not c.message or c.message.chat.type!='private' or c.message.chat.id!=c.from_user.id:
            await c.answer('Только в ЛС.',show_alert=True);return
        await c.answer()
        if c.data=='s:choose':await choose(c.message,c.from_user.id)
        elif c.data=='s:sync':
            await sync(c.from_user.id)
            await a.panel(c.message,c.from_user.id)
    a.sync_chats=sync;a.shared_chat=shared;a.cancel_chat_choice=cancel_choice
