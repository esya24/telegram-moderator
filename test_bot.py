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
              text=text,answer=AsyncMock(),edit_reply_markup=AsyncMock())
def callback(data,uid=20,chat_type='private'):
    return NS(data=data,from_user=NS(id=uid),message=message(uid,chat_type=chat_type),answer=AsyncMock())

class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for table in ('chats','settings','drafts','appeals','violations','chat_users','private_users'):
            app.db.execute(f'DELETE FROM {table}')
        app.db.commit()
        app.remember_chat(NS(id=-100,title='Group',type='supergroup'))
        self.fake=NS(get_chat_member=AsyncMock(return_value=NS(status='restricted',can_send_messages=False,user=NS(id=10,is_bot=False,full_name='User'))),
                     get_chat=AsyncMock(return_value=NS(type='supergroup')),
                     get_chat_administrators=AsyncMock(return_value=[NS(user=NS(id=20,is_bot=False))]),
                     send_message=AsyncMock(),restrict_chat_member=AsyncMock(),id=123456)
        self.p=patch.object(app,'bot',self.fake);self.p.start();self.addCleanup(self.p.stop)

    async def test_appeal_only_delivered_privately(self):
        m=message(text='Это ошибка')
        await app.start_appeal(m,-100)
        await app.private_input(m)
        self.assertEqual(self.fake.send_message.call_args.args[0],20)
        self.assertIn('Это ошибка',self.fake.send_message.call_args.args[1])
        self.assertEqual(app.db.execute('SELECT status FROM appeals').fetchone()[0],'pending')
        self.assertIsNone(app.db.execute('SELECT * FROM drafts').fetchone())
        await app.start_appeal(m,-100)
        self.assertIsNone(app.db.execute('SELECT * FROM drafts').fetchone())

    async def test_unreachable_admin_keeps_inbox(self):
        self.fake.send_message.side_effect=RuntimeError('blocked')
        m=message(text='Причина');await app.start_appeal(m,-100);await app.private_input(m)
        self.assertEqual(app.db.execute('SELECT status FROM appeals').fetchone()[0],'pending')
        self.assertIn('сохранена',m.answer.call_args.args[0])

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
        for text,should_delete,reply in [
            ('s0s1',True,False),
            ('Что означает слово «sosi»?',False,False),
            ('во дворе петух',False,True),
            ('клоун',True,True),
        ]:
            with self.subTest(text=text):
                app.db.execute('DELETE FROM violations');app.db.commit()
                m=message(text=text,chat_type='supergroup',cid=-100)
                m.from_user.is_bot=False;m.sender_chat=None;m.delete=AsyncMock()
                m.reply_to_message=message(uid=99) if reply else None
                with patch.object(app,'is_admin',AsyncMock(return_value=False)):
                    await app.moderation(m)
                self.assertEqual(m.delete.await_count,int(should_delete))
                if should_delete:
                    self.assertIn('оскорбление',m.answer.call_args.args[0])

    def test_filter_kept(self):
        self.assertTrue(app.is_offensive('блять'))
        self.assertFalse(app.is_offensive('Доброе утро'))

if __name__=='__main__':unittest.main()
