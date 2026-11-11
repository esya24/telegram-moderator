import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import test_bot as legacy
from test_bot import message,callback
import bot as app

class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        legacy.FlowTests.setUp(self)
        app.db.execute('DELETE FROM chat_pick_requests');app.db.commit()

    async def click(self,data,uid=20,chat_type='private'):
        handler=next(h.callback for h in app.dp.callback_query.handlers if h.callback.__module__=='chat_sync')
        c=callback(data,uid=uid,chat_type=chat_type);await handler(c);return c

    async def test_start_keeps_command_and_posts_visible_panel(self):
        app.db.execute('INSERT INTO panels VALUES(20,40)');app.db.commit()
        m=message(uid=20,text='/start')
        with patch.object(app,'sync_chats',AsyncMock()) as sync:
            await app.start(m)
        m.delete.assert_not_awaited();self.fake.delete_message.assert_not_awaited()
        self.fake.send_message.assert_awaited_once();self.fake.edit_message_text.assert_not_awaited()
        sync.assert_awaited_once_with(20)

    async def test_private_input_is_never_deleted(self):
        m=message(text='привет')
        await app.private_input(m)
        m.delete.assert_not_awaited()

    async def test_recover_known_ids_and_linked_discussion(self):
        app.db.execute('DELETE FROM chats')
        app.db.execute('INSERT INTO settings VALUES(-100,3,15,1)');app.db.commit()
        async def get_chat(cid):
            return NS(id=cid,type='channel' if cid==-100 else 'supergroup',title=f'Chat {cid}',linked_chat_id=-200 if cid==-100 else None)
        self.fake.get_chat.side_effect=get_chat
        with patch.object(app,'allowed',AsyncMock(return_value=True)),patch.object(app,'is_admin',AsyncMock(return_value=True)):
            await app.sync_chats(20)
        self.assertEqual(set(app.db.execute('SELECT chat_id FROM chats').fetchall()),{(-100,),(-200,)})
        self.assertEqual(app.db.execute('SELECT title FROM chats WHERE chat_id=-100').fetchone()[0],'Chat -100')

    async def test_choose_requests_only_chats_with_bot(self):
        await self.click('s:choose')
        markup=self.fake.send_message.call_args.kwargs['reply_markup']
        self.assertTrue(markup.keyboard[0][0].request_chat.bot_is_member)
        self.assertFalse(markup.keyboard[0][0].request_chat.chat_is_channel)
        self.assertTrue(markup.keyboard[1][0].request_chat.chat_is_channel)
        self.assertEqual(app.db.execute('SELECT COUNT(*) FROM chat_pick_requests').fetchone()[0],1)

    async def test_forged_and_unauthorized_selection_cannot_connect(self):
        m=message(uid=20);m.chat_shared=NS(request_id=123,chat_id=-500)
        await app.shared_chat(m)
        self.fake.get_chat.assert_not_awaited()
        app.db.execute('INSERT INTO chat_pick_requests VALUES(20,123)');app.db.commit()
        with patch.object(app,'allowed',AsyncMock(return_value=False)):
            await app.shared_chat(m)
        self.assertIsNone(app.db.execute('SELECT 1 FROM chats WHERE chat_id=-500').fetchone())

    async def test_valid_shared_chat_rechecks_admin_rights(self):
        app.db.execute('INSERT INTO chat_pick_requests VALUES(20,123)');app.db.commit()
        m=message(uid=20);m.chat_shared=NS(request_id=123,chat_id=-500)
        self.fake.get_chat.return_value=NS(id=-500,type='supergroup',title='Existing group',linked_chat_id=None)
        with patch.object(app,'allowed',AsyncMock(return_value=True)) as allowed,patch.object(app,'is_admin',AsyncMock(return_value=True)) as admin:
            await app.shared_chat(m)
        allowed.assert_awaited_once_with(-500,20);admin.assert_awaited_once_with(-500,self.fake.id)
        self.assertIsNotNone(app.db.execute('SELECT 1 FROM chats WHERE chat_id=-500').fetchone())
        self.assertIsNone(app.db.execute('SELECT 1 FROM chat_pick_requests WHERE user_id=20').fetchone())

    async def test_picker_private_only(self):
        await self.click('s:choose',chat_type='supergroup')
        self.fake.send_message.assert_not_awaited()

    async def test_unavailable_chat_does_not_break_sync(self):
        self.fake.get_chat.side_effect=TimeoutError()
        with patch.object(app,'allowed',AsyncMock(return_value=True)):
            self.assertEqual(await app.sync_chats(20),0)

    async def test_middleware_remembers_even_service_updates(self):
        middleware=next(m for m in app.dp.update.outer_middleware if m.__class__.__name__=='Discover')
        event=NS(message=NS(chat=NS(id=-777,type='supergroup',title='Already there'),forward_origin=None))
        handler=AsyncMock(return_value='done')
        self.assertEqual(await middleware(handler,event,{}),'done')
        self.assertEqual(app.db.execute('SELECT title FROM chats WHERE chat_id=-777').fetchone()[0],'Already there')
