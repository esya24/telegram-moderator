"""Offline regression checks: no Telegram requests or production database."""
import os
os.environ['BOT_TOKEN']='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
os.environ['DB_PATH']=':memory:'
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import bot as app


def message(uid=10,text='',chat_type='private',cid=None):
    return NS(from_user=NS(id=uid,full_name='User',username=None),
              chat=NS(id=cid if cid is not None else uid,type=chat_type,title='Test'),
              text=text,answer=AsyncMock(),delete=AsyncMock(),message_id=70,edit_reply_markup=AsyncMock())
def callback(data,uid=20,chat_type='private'):
    return NS(data=data,from_user=NS(id=uid),message=message(uid,chat_type=chat_type),answer=AsyncMock())

class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for table in ('decisions','audit_log','restrictions','member_profiles','manual_requests','ai_settings','chats','settings','drafts','appeals','violations','chat_users','private_users','panels'):
            app.db.execute(f'DELETE FROM {table}')
        app.db.commit()
        app.remember_chat(NS(id=-100,title='Group',type='supergroup'))
        self.fake=NS(get_chat_member=AsyncMock(return_value=NS(status='restricted',can_send_messages=False,user=NS(id=10,is_bot=False,full_name='User'))),
                     get_chat=AsyncMock(return_value=NS(type='supergroup')),
                     delete_message=AsyncMock(),get_chat_administrators=AsyncMock(return_value=[NS(user=NS(id=20,is_bot=False))]),
                     send_message=AsyncMock(return_value=NS(message_id=70)),edit_message_text=AsyncMock(),restrict_chat_member=AsyncMock(),id=123456)
        self.p=patch.object(app,'bot',self.fake);self.p.start();self.addCleanup(self.p.stop)

    async def test_appeal_only_delivered_privately(self):
        m=message(text='Это ошибка')
        await app.start_appeal(m,-100)
        await app.private_input(m)
        notice=[call for call in self.fake.send_message.call_args_list if call.args[0]==20]
        self.assertEqual(len(notice),1)
        self.assertIn('Открыть заявку',notice[0].kwargs['reply_markup'].inline_keyboard[0][0].text)
        self.assertEqual(app.db.execute('SELECT status FROM appeals').fetchone()[0],'pending')
        self.assertIsNone(app.db.execute('SELECT * FROM drafts').fetchone())
        await app.start_appeal(m,-100)
        self.assertIsNone(app.db.execute('SELECT * FROM drafts').fetchone())

    async def test_unreachable_admin_keeps_inbox(self):
        async def send(uid,*args,**kwargs):
            if uid==20:raise RuntimeError('blocked')
            return NS(message_id=70)
        self.fake.send_message.side_effect=send
        m=message(text='Причина');await app.start_appeal(m,-100);await app.private_input(m)
        self.assertEqual(app.db.execute('SELECT status FROM appeals').fetchone()[0],'pending')
        self.assertIn('сохранена',self.fake.edit_message_text.call_args.kwargs['text'])

    async def test_cancel_removes_private_draft(self):
        m=message();await app.start_appeal(m,-100);await app.cancel(m)
        self.assertIsNone(app.db.execute('SELECT * FROM drafts').fetchone())

    def add_appeal(self):
        app.db.execute("INSERT INTO appeals VALUES('abc',-100,10,'User','Reason','pending')");app.db.commit()

    async def test_decision_requires_current_admin_and_private_chat(self):
        self.add_appeal()
        with patch.object(app,'allowed',AsyncMock(return_value=False)):
            await app.decide(callback('a:approve:abc'))
        self.fake.restrict_chat_member.assert_not_awaited()
        with patch.object(app,'allowed',AsyncMock(return_value=True)):
            await app.decide(callback('a:approve:abc',chat_type='supergroup'))
        self.fake.restrict_chat_member.assert_not_awaited()

    async def test_approve_once_and_notify_user_privately(self):
        self.add_appeal();app.save_count(-100,10,5,app.datetime.now(app.timezone.utc))
        with patch.object(app,'allowed',AsyncMock(return_value=True)):
            await asyncio.gather(app.decide(callback('a:approve:abc')),app.decide(callback('a:reject:abc')))
        self.assertEqual(app.db.execute('SELECT status FROM appeals').fetchone()[0],'approve')
        self.fake.restrict_chat_member.assert_awaited_once()
        self.assertEqual(app.count_violations(-100,10,app.datetime.now(app.timezone.utc)),0)
        self.assertEqual(self.fake.send_message.call_args.args[0],10)

    async def test_failed_unmute_can_retry(self):
        self.add_appeal();self.fake.restrict_chat_member.side_effect=RuntimeError('no rights')
        with patch.object(app,'allowed',AsyncMock(return_value=True)):await app.decide(callback('a:approve:abc'))
        self.assertEqual(app.db.execute('SELECT status FROM appeals').fetchone()[0],'pending')

    async def test_settings_are_scoped_and_permissions_rechecked(self):
        c=callback('p:minutes:-100')
        with patch.object(app,'allowed',AsyncMock(return_value=True)):
            await app.panel_click(c);await app.private_input(message(uid=20,text='42'))
        self.assertEqual(app.config(-100)[1],42)
        self.assertEqual(app.config(-200)[1],app.MUTE_MINUTES)
        with patch.object(app,'allowed',AsyncMock(return_value=False)):
            await app.panel_click(callback('p:toggle:-100'))
        self.assertEqual(app.config(-100)[2],1)

    async def test_bad_setting_retains_draft(self):
        app.db.execute("INSERT INTO drafts VALUES(20,'minutes',-100)");app.db.commit()
        with patch.object(app,'allowed',AsyncMock(return_value=True)):
            await app.private_input(message(uid=20,text='-1'))
        self.assertEqual(app.config(-100)[1],app.MUTE_MINUTES)
        self.assertIsNotNone(app.db.execute('SELECT * FROM drafts').fetchone())

    async def test_disabled_filter_does_not_delete_messages(self):
        app.set_config(-100,3,15,0)
        m=message(text='блять',chat_type='supergroup',cid=-100);m.from_user.is_bot=False;m.sender_chat=None;m.delete=AsyncMock()
        await app.moderation(m);m.delete.assert_not_awaited()

    async def test_contextual_filter_is_used_by_group_handler(self):
        app.db.execute("INSERT INTO ai_settings VALUES(-100,'automatic')");app.db.commit()
        for text,should_delete,reply in [
            ('s0s1',True,False),
            ('Что означает слово «sosi»?',False,False),
            ('во дворе петух',False,True),
            ('клоун',True,True),
        ]:
            with self.subTest(text=text):
                app.db.execute('DELETE FROM violations');app.db.execute('DELETE FROM decisions');app.db.commit()
                m=message(text=text,chat_type='supergroup',cid=-100)
                m.from_user.is_bot=False;m.sender_chat=None;m.delete=AsyncMock()
                m.reply_to_message=message(uid=99) if reply else None
                with patch.object(app,'is_admin',AsyncMock(return_value=False)):
                    await app.moderation(m)
                self.assertEqual(m.delete.await_count,int(should_delete))
                if should_delete:
                    self.assertIn('оскорбление',m.answer.call_args.args[0])

    async def test_navigation_and_input_reuse_one_message(self):
        with patch.object(app,'allowed',AsyncMock(return_value=True)):
            await app.panel(message(uid=20),20)
            await app.panel_click(callback('p:chat:-100'))
            await app.panel_click(callback('p:minutes:-100'))
            await app.private_input(message(uid=20,text='42'))
            await app.panel_click(callback('p:home:0'))
        self.fake.send_message.assert_awaited_once()
        self.assertEqual(self.fake.edit_message_text.await_count,4)
        for call in self.fake.edit_message_text.call_args_list:
            self.assertEqual(call.kwargs['message_id'],70)
            self.assertEqual(call.kwargs['chat_id'],20)

    async def test_duplicate_render_and_deleted_panel_recovery(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageText
        await app.panel(message(uid=20),20)
        method=EditMessageText(chat_id=20,message_id=70,text='Panel')
        self.fake.edit_message_text.side_effect=TelegramBadRequest(method=method,message='message is not modified')
        await app.panel(message(uid=20),20)
        self.fake.send_message.assert_awaited_once()
        self.fake.edit_message_text.side_effect=TelegramBadRequest(method=method,message='message to edit not found')
        self.fake.send_message.return_value=NS(message_id=71)
        await app.panel(message(uid=20),20)
        self.assertEqual(self.fake.send_message.await_count,2)
        self.assertEqual(app.db.execute('SELECT message_id FROM panels WHERE user_id=20').fetchone()[0],71)

    async def test_network_error_does_not_create_duplicate_panel(self):
        await app.panel(message(uid=20),20)
        self.fake.edit_message_text.side_effect=RuntimeError('network')
        with self.assertRaises(RuntimeError):await app.panel(message(uid=20),20)
        self.fake.send_message.assert_awaited_once()

    async def test_leaving_prompt_cancels_draft(self):
        with patch.object(app,'allowed',AsyncMock(return_value=True)):
            await app.panel_click(callback('p:minutes:-100'))
            await app.panel_click(callback('p:chat:-100'))
            await app.private_input(message(uid=20,text='99'))
        self.assertEqual(app.config(-100)[1],app.MUTE_MINUTES)
        self.assertIsNone(app.db.execute('SELECT * FROM drafts').fetchone())

    async def test_inbox_pagination_and_escaped_content(self):
        for n in range(8):
            app.db.execute('INSERT INTO appeals VALUES(?,?,?,?,?,?)',(str(n),-100,n,'<User>','<script>','pending'))
        app.db.commit()
        await app.show_inbox(message(uid=20),-100)
        buttons=self.fake.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(len([row for row in buttons if row[0].callback_data.startswith('p:appeal:')]),6)
        await app.show_inbox(message(uid=20),-100,1)
        buttons=self.fake.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(len([row for row in buttons if row[0].callback_data.startswith('p:appeal:')]),2)
        await app.show_appeal(message(uid=20),app.db.execute('SELECT * FROM appeals LIMIT 1').fetchone())
        self.assertIn('&lt;script&gt;',self.fake.edit_message_text.call_args.kwargs['text'])

    async def test_mute_requires_confirmation_screen(self):
        with patch.object(app,'allowed',AsyncMock(return_value=True)):
            await app.panel_click(callback('p:confirmmute:-100:10'))
            self.fake.restrict_chat_member.assert_not_awaited()
            await app.panel_click(callback('p:mute:-100:10'))
        self.fake.restrict_chat_member.assert_awaited_once()

    def test_filter_kept(self):
        self.assertTrue(app.is_offensive('блять'))
        self.assertFalse(app.is_offensive('Доброе утро'))

if __name__=='__main__':unittest.main()
