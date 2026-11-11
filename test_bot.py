# test_bot.py
"""Offline regression checks: no Telegram requests or production database."""

import os
os.environ['BOT_TOKEN']='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
os.environ['DB_PATH']=':memory:'
os.environ['AI_CHAT_COMPLETIONS_URL']=''
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import bot as app
from language_filter import analyze
import moderation as store
from moderation import validate, evaluate
import json
import interface as theme
from aiogram.types import MessageEntity, InlineKeyboardMarkup
from aiogram.methods import SendMessage, EditMessageText
from aiogram.exceptions import TelegramBadRequest
import interface as emoji



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
        for table in ('ui_overrides','ui_options','ui_drafts','ui_audit','chat_pick_requests'):
            app.db.execute('DELETE FROM '+table)
        app.db.commit()
        __import__('interface').configure(app.db)
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




# test_language_filter.py

BAD = [
    'blyаt','blуat','пuздa','блять','бля','сука','sosi','s0s1','s o s i','cоcи','с0си','6лять','6л9ть',
    'b.l.y.a.t','blyat','bl9t','nahuy','na hui','п1зд3ц','pizdec','пизdец',
    'xуй','xyй','х у й','п-и-з-д-е-ц','п***и***з***д***е***ц','ты дeбил',
    'ты loh','ты клоун','ti idiot','иди нахуй','я ебал','е6ать','ебаааать',
    'ты тупой','отсоси','чм0','f.u.c.k','5uk4','суkа','xуeсос','ты идиот',
    'пошёл нахер','ты пeтyx','идu нaхyй','п\u200bи\u200dздец','ＦＵＣＫ',
    'бляяяяять','сссууукккааа','пиздёж','заебись','заебал','пидорас',
    'тебе говорят, ты полный идиот','ты дурак, это слово «блять»',
    'слово «идиот» — это ты','слово «sosi», иди нахуй',
]
CLEAN = [
    'Доброе утро','сосиски','сосед','сосна','насос','особенности',
    'команда','мандарины','употребление','учеба','Херсон',
    'страхуй машину','подстрахуй меня','член команды',
    'вагина — анатомический термин','во дворе петух','мусор нужно вынести',
    'это тупой нож','я идиот','я такой дурак','ты не идиот',
    'sosiski','subscribe','class assignment','Scunthorpe',
    'соси леденец','сосать сок через трубочку',
    'это слово «блять» означает ругательство',
    'Что означает слово "sosi"?',
    'Он написал «иди нахуй», это оскорбление',
    'слово «идиот» — оскорбление','роман Идиот Достоевского',
    'ты прочитал роман «Идиот»?',
    'бляха на ремне','рубль','сук дерева','психолог','психиатр',
    'Я прошу не писать слово «сука» в чате',
]

class LanguageTests(unittest.TestCase):
    def test_obfuscations_and_abuse(self):
        for text in BAD:
            with self.subTest(text=text):self.assertTrue(analyze(text).offensive)
    def test_normal_words_and_context(self):
        for text in CLEAN:
            with self.subTest(text=text):self.assertFalse(analyze(text).offensive)
    def test_reply_does_not_make_neutral_sentence_an_insult(self):
        self.assertFalse(analyze('во дворе петух',addressed=True).offensive)
        self.assertTrue(analyze('клоун',addressed=True).offensive)
        self.assertFalse(analyze('клоун',addressed=False).offensive)
    def test_categories(self):
        self.assertEqual(analyze('ты идиот').category,'insult')
        self.assertEqual(analyze('блять, автобус уехал').category,'profanity')
        self.assertEqual(analyze('Что означает слово «блять»?').category,'discussion')
    def test_distant_target_does_not_poison_normal_noun(self):
        self.assertFalse(analyze('ты знаешь историю про ферму где по утрам очень громко кричит петух').offensive)
    def test_no_message_wide_word_concatenation(self):
        self.assertFalse(analyze('class is interesting').offensive)
        self.assertFalse(analyze('люди обсуждают Херсон и учебу').offensive)




# test_moderator2.py
os.environ['AI_CHAT_COMPLETIONS_URL']=''


class V2Tests(unittest.IsolatedAsyncioTestCase):
    setUp=FlowTests.setUp
    def group_message(self):
        m=message(text='s0s1',chat_type='supergroup',cid=-100)
        m.from_user.is_bot=False;m.sender_chat=None;m.reply_to_message=None
        return m

    async def test_observation_has_no_sanctions(self):
        m=self.group_message()
        with patch.object(app,'is_admin',AsyncMock(return_value=False)):
            await app.moderation(m)
        m.delete.assert_not_awaited();self.fake.restrict_chat_member.assert_not_awaited()
        self.assertEqual(app.db.execute('SELECT mode,action,review_status FROM decisions').fetchone(),('observation','none','pending'))
        self.assertEqual(app.count_violations(-100,10,app.datetime.now(app.timezone.utc)),0)

    async def review(self,data,allowed=True):
        handler=next(h.callback for h in app.dp.callback_query.handlers if h.callback.__name__=='click')
        with patch.object(app,'allowed',AsyncMock(return_value=allowed)):
            await handler(callback(data))

    async def test_observation_review_is_label_only_and_once(self):
        await self.test_observation_has_no_sanctions()
        did=app.db.execute('SELECT id FROM decisions').fetchone()[0]
        await asyncio.gather(self.review(f'q:accept:-100:{did}'),self.review(f'q:reject:-100:{did}'))
        self.assertEqual(store.get(app.db,did)['review_status'],'accepted')
        self.fake.restrict_chat_member.assert_not_awaited();self.fake.delete_message.assert_not_awaited()

    async def test_confirmation_and_permission(self):
        app.db.execute("INSERT INTO ai_settings VALUES(-100,'confirmation')");app.db.commit()
        m=self.group_message()
        with patch.object(app,'is_admin',AsyncMock(return_value=False)):await app.moderation(m)
        did=app.db.execute('SELECT id FROM decisions').fetchone()[0]
        await self.review(f'q:apply:-100:{did}',allowed=False)
        self.assertEqual(store.get(app.db,did)['review_status'],'pending')
        await self.review(f'q:apply:-100:{did}')
        await self.review(f'q:apply:-100:{did}')
        self.fake.delete_message.assert_awaited_once()
        self.assertEqual(app.count_violations(-100,10,app.datetime.now(app.timezone.utc)),1)

    async def test_ai_failure_preserves_local_filter(self):
        with patch.dict(os.environ,{'AI_CHAT_COMPLETIONS_URL':'https://invalid.local','AI_API_KEY':'test','AI_MODEL':'test'}),patch('aiohttp.ClientSession',side_effect=RuntimeError()):
            result=await evaluate('s0s1',False,[],{})
        self.assertEqual(result['action'],'warn');self.assertEqual(result['model'],'local-fallback-2.0')

    def test_validation_downgrades_ambiguous_and_rejects_fake_quote(self):
        raw=dict(category='personal_insult',confidence=.82,reason='Причина',evidence='идиот',action='mute')
        self.assertEqual(validate(raw,'ты идиот','test')['action'],'review')
        with self.assertRaises(ValueError):validate(raw,'привет','test')
        raw['confidence']=float('nan')
        with self.assertRaises(ValueError):validate(raw,'ты идиот','test')

    def test_retention_preserves_metadata(self):
        result=dict(category='spam',confidence=.8,reason='spam',evidence='old',action='review',model='test')
        did=store.record(app.db,-100,10,71,'old',[],result,'observation')
        app.db.execute("UPDATE decisions SET created_at='2000-01-01' WHERE id=?",(did,));app.db.commit()
        store.purge(app.db,30);row=store.get(app.db,did)
        self.assertEqual(row['source_text'],'');self.assertEqual(row['category'],'spam')

    def test_additive_migration_keeps_old_warning_counts(self):
        app.save_count(-100,10,2,app.datetime.now(app.timezone.utc))
        store.migrate(app.db);store.migrate(app.db)
        self.assertEqual(app.count_violations(-100,10,app.datetime.now(app.timezone.utc)),2)

    async def test_manual_confirmation_is_single_use(self):
        await self.review('q:askwarn:-100:10')
        token=app.db.execute('SELECT id FROM manual_requests').fetchone()[0]
        await self.review(f'q:manual:-100:{token}')
        await self.review(f'q:manual:-100:{token}')
        self.assertEqual(app.count_violations(-100,10,app.datetime.now(app.timezone.utc)),1)

    async def test_uncertain_delivery_never_retries(self):
        app.db.execute("INSERT INTO ai_settings VALUES(-100,'confirmation')");app.db.commit()
        m=self.group_message()
        with patch.object(app,'is_admin',AsyncMock(return_value=False)):await app.moderation(m)
        did=app.db.execute('SELECT id FROM decisions').fetchone()[0]
        with patch.object(app,'writable_target',AsyncMock(side_effect=TimeoutError())):
            await self.review(f'q:apply:-100:{did}')
        self.assertEqual(store.get(app.db,did)['review_status'],'uncertain')
        await self.review(f'q:apply:-100:{did}')
        self.assertEqual(app.count_violations(-100,10,app.datetime.now(app.timezone.utc)),0)


# test_ui_owner.py


class ThemeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FlowTests.setUp(self)
        for table in ('ui_overrides','ui_options','ui_drafts','ui_audit'):app.db.execute('DELETE FROM '+table)
        app.db.commit()
        self.env=patch.dict(os.environ,{'BOT_OWNER_ID':'20'});self.env.start();self.addCleanup(self.env.stop)

    def key(self,value):
        return next(k for k,v in theme.CATALOG.items() if v['default']==value)

    async def click(self,data,uid=20,chat_type='private'):
        handler=next(h.callback for h in app.dp.callback_query.handlers if h.callback.__qualname__.startswith('install_owner.'))
        c=callback(data,uid,chat_type);await handler(c);return c

    def test_all_catalog_defaults_are_valid(self):
        for key,item in theme.CATALOG.items():
            with self.subTest(key=key):theme.validate(key,item['default'])

    def test_owner_is_configured_explicitly(self):
        with patch.dict(os.environ,{'BOT_OWNER_ID':''}):self.assertFalse(theme.is_owner(20))
        with patch.dict(os.environ,{'BOT_OWNER_ID':'invalid'}):self.assertFalse(theme.is_owner(20))
        with patch.dict(os.environ,{'BOT_OWNER_ID':'-20'}):self.assertFalse(theme.is_owner(-20))
        self.assertTrue(theme.is_owner(20));self.assertFalse(theme.is_owner(10))

    async def test_only_owner_sees_entry(self):
        await app.panel(message(uid=20),20)
        buttons=self.fake.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertTrue(any(b.callback_data=='u:home' for row in buttons for b in row))
        await app.panel(message(uid=10),10)
        buttons=self.fake.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertFalse(any(b.callback_data=='u:home' for row in buttons for b in row))

    async def test_forged_callback_cannot_mutate_theme(self):
        c=await self.click('u:style:global:danger',uid=10)
        c.answer.assert_awaited_once();self.assertEqual(theme.option('style'),'original')
        await self.click('u:style:global:danger',chat_type='supergroup')
        self.assertEqual(theme.option('style'),'original')
        with self.assertRaises(PermissionError):theme.save(app.db,10,self.key('Главное меню'),'test')

    async def test_edit_preview_save_and_persistence(self):
        key=self.key('Главное меню')
        await self.click(f'u:edit:{key}')
        await app.private_input(message(uid=20,text='🏠 На главную'))
        self.assertEqual(theme.get(key),'Главное меню') # preview is not published
        await self.click(f'u:save:{key}')
        self.assertEqual(theme.get(key),'🏠 На главную')
        theme.configure(app.db)
        self.assertEqual(str(theme.text(key)),'🏠 На главную')
        self.assertIsNone(app.db.execute('SELECT * FROM ui_drafts').fetchone())
        await self.click(f'u:save:{key}')
        self.assertEqual(app.db.execute("SELECT COUNT(*) FROM ui_audit WHERE action='text'").fetchone()[0],1)

    def test_placeholders_and_html_are_validated(self):
        key=next(k for k,v in theme.CATALOG.items() if '{v0}' in v['default'] and v['kind']=='text')
        with self.assertRaises(ValueError):theme.validate(key,'Потерял данные')
        with self.assertRaises(ValueError):theme.validate(key,'{v0.__class__}')
        with self.assertRaises(ValueError):theme.validate(key,'<script>{v0}</script>')
        with self.assertRaises(ValueError):theme.validate(key,'<b>{v0}</i>')
        plain=self.key('Главное меню')
        with self.assertRaises(ValueError):theme.validate(plain,'<b>Кнопка</b>')
        with self.assertRaises(ValueError):theme.validate(plain,'x'*65)

    def test_style_and_layout_preserve_actions(self):
        key=self.key('Главное меню')
        theme.set_option(app.db,20,'style','primary')
        theme.set_option(app.db,20,'style:'+key,'danger')
        theme.set_option(app.db,20,'columns','1')
        markup=app.kb([[(theme.text(key),'p:home:0'),('Другая','p:chat:-100')]])
        self.assertEqual(len(markup.inline_keyboard),2)
        self.assertEqual(markup.inline_keyboard[0][0].style,'danger')
        self.assertEqual(markup.inline_keyboard[1][0].style,'primary')
        self.assertEqual(markup.inline_keyboard[0][0].callback_data,'p:home:0')
        link=theme.button(theme.text(key),url='https://example.com')
        self.assertEqual(link.style,'danger');self.assertEqual(link.url,'https://example.com')

    async def test_stale_owner_draft_cannot_be_saved_by_new_owner(self):
        key=self.key('Главное меню')
        await self.click(f'u:edit:{key}')
        with patch.dict(os.environ,{'BOT_OWNER_ID':'10'}):
            await app.private_input(message(uid=20,text='Взлом'))
            await self.click(f'u:save:{key}',uid=10)
        self.assertEqual(theme.get(key),'Главное меню')

    async def test_search_and_reset(self):
        await self.click('u:search')
        await app.private_input(message(uid=20,text='Главное меню'))
        self.assertIn('Каталог оформления',self.fake.edit_message_text.call_args.kwargs['text'])
        key=self.key('Главное меню')
        theme.save(app.db,20,key,'Домой')
        await self.click('u:resetask')
        self.assertEqual(theme.get(key),'Домой')
        await self.click('u:reset')
        self.assertEqual(theme.get(key),'Главное меню')

    async def test_cancel_discards_pending_edit(self):
        key=self.key('Главное меню')
        await self.click(f'u:edit:{key}')
        await app.private_input(message(uid=20,text='Домой'))
        await self.click(f'u:cancel:{key}')
        await self.click(f'u:save:{key}')
        self.assertEqual(theme.get(key),'Главное меню')

    def test_catalog_never_exposes_permission_constants_or_callbacks(self):
        values={v['default'] for v in theme.CATALOG.values()}
        self.assertFalse(values & {'creator','administrator','pending','automatic','warn','home','AI_CHAT_COMPLETIONS_URL'})
        self.assertFalse(any(v.startswith(('p:','q:','a:','u:')) for v in values))


# test_chat_sync.py


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FlowTests.setUp(self)
        app.db.execute('DELETE FROM chat_pick_requests');app.db.commit()

    async def click(self,data,uid=20,chat_type='private'):
        handler=next(h.callback for h in app.dp.callback_query.handlers if h.callback.__qualname__.startswith('install_chat_sync.'))
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


# test_custom_emoji.py


ID='5368324170671202286'
TAG=f'<tg-emoji emoji-id="{ID}">⭐</tg-emoji>'

class EmojiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FlowTests.setUp(self)
        for table in ('ui_overrides','ui_options','ui_drafts','ui_audit'):app.db.execute('DELETE FROM '+table)
        app.db.commit()
        self.p=patch.dict(os.environ,{'BOT_OWNER_ID':'20','CUSTOM_EMOJI_ENABLED':'1'});self.p.start();self.addCleanup(self.p.stop)

    def key(self,value):return next(k for k,v in theme.CATALOG.items() if v['default']==value)

    def test_utf16_offsets_after_astral_characters(self):
        m=NS(text='😀 Привет ⭐!',entities=[MessageEntity(type='custom_emoji',offset=10,length=1,custom_emoji_id=ID)])
        self.assertEqual(emoji.from_message(m),'😀 Привет '+TAG+'!')

    def test_multiple_emoji_and_escaped_source(self):
        m=NS(text='<b>⭐ ✨</b>',entities=[MessageEntity(type='custom_emoji',offset=3,length=1,custom_emoji_id=ID),MessageEntity(type='custom_emoji',offset=5,length=1,custom_emoji_id='123')])
        result=emoji.from_message(m)
        self.assertEqual(result,f'<b>{TAG} <tg-emoji emoji-id="123">✨</tg-emoji></b>')
        theme.validate(self.key('Пока нет оценок.'),result)

    async def test_owner_can_send_premium_emoji_and_save_text(self):
        handler=next(h.callback for h in app.dp.callback_query.handlers if h.callback.__qualname__.startswith('install_owner.'))
        key=self.key('Пока нет оценок.')
        await handler(callback(f'u:edit:{key}'))
        m=message(uid=20,text='⭐ Готово');m.entities=[MessageEntity(type='custom_emoji',offset=0,length=1,custom_emoji_id=ID)]
        await app.private_input(m)
        self.assertIn(TAG,self.fake.edit_message_text.call_args.kwargs['text'])
        self.assertEqual(theme.get(key),'Пока нет оценок.')
        await handler(callback(f'u:save:{key}'))
        self.assertEqual(theme.get(key),TAG+' Готово')

    def test_button_icon_uses_api_field_and_preserves_callback(self):
        key=self.key('Главное меню')
        theme.save(app.db,20,key,TAG+' Главная')
        b=app.kb([[(theme.text(key),'p:home:0')]]).inline_keyboard[0][0]
        self.assertEqual(b.text,'Главная');self.assertEqual(b.icon_custom_emoji_id,ID)
        self.assertEqual(b.callback_data,'p:home:0')
        self.assertEqual(theme.keyboard_button(theme.text(key)).icon_custom_emoji_id,ID)

    def test_multiple_or_misplaced_button_emoji_rejected(self):
        key=self.key('Главное меню')
        for value in (TAG+' '+TAG+' Меню','Меню '+TAG,TAG):
            with self.assertRaises(ValueError):theme.validate(key,value)

    def test_invalid_tags_ids_and_alternative_rejected(self):
        key=self.key('Пока нет оценок.')
        for value in ('<tg-emoji emoji-id="oops">⭐</tg-emoji>','<tg-emoji emoji-id="123">text</tg-emoji>','<tg-emoji emoji-id="123"><b>⭐</b></tg-emoji>','<TG-EMOJI emoji-id="123">⭐</TG-EMOJI>'):
            with self.assertRaises(ValueError):theme.validate(key,value)

    async def test_explicit_rejection_retries_plain_text_and_button(self):
        button=theme.button(theme.Label(TAG+' Меню','unknown'),callback_data='p:home:0')
        method=SendMessage(chat_id=20,text=TAG+' Привет',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))
        request=AsyncMock(side_effect=[TelegramBadRequest(method=method,message='CUSTOM_EMOJI_NOT_ALLOWED'),'ok'])
        self.assertEqual(await emoji.EmojiFallback()(request,self.fake,method),'ok')
        safe=request.call_args.args[1]
        self.assertEqual(safe.text,'⭐ Привет')
        self.assertIsNone(safe.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id)
        self.assertEqual(safe.reply_markup.inline_keyboard[0][0].text,'⭐ Меню')
        self.assertEqual(method.text,TAG+' Привет') # fallback does not mutate saved configuration

    async def test_disabled_feature_and_edit_fallback(self):
        method=EditMessageText(chat_id=20,message_id=70,text=TAG)
        request=AsyncMock(return_value='ok')
        with patch.dict(os.environ,{'CUSTOM_EMOJI_ENABLED':'0'}):
            await emoji.EmojiFallback()(request,self.fake,method)
        self.assertEqual(request.call_args.args[1].text,'⭐')

    async def test_unrelated_errors_and_timeouts_do_not_retry(self):
        method=SendMessage(chat_id=20,text=TAG)
        for exc in (TelegramBadRequest(method=method,message='chat not found'),TimeoutError()):
            request=AsyncMock(side_effect=exc)
            with self.assertRaises(type(exc)):await emoji.EmojiFallback()(request,self.fake,method)
            request.assert_awaited_once()

    async def test_non_owner_cannot_save_custom_emoji(self):
        with self.assertRaises(PermissionError):theme.save(app.db,10,self.key('Главное меню'),TAG+' Меню')

    def test_catalog_markup_survives_nested_labels_but_user_data_is_escaped(self):
        key=self.key('Пока нет оценок.')
        theme.save(app.db,20,key,TAG+' Готово')
        self.assertEqual(theme.escape(theme.text(key)),TAG+' Готово')
        self.assertEqual(theme.escape('<b>имя участника</b>'),'&lt;b&gt;имя участника&lt;/b&gt;')




class OptimizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FlowTests.setUp(self)
        self.env=patch.dict(os.environ,{'BOT_OWNER_ID':'20'})
        self.env.start();self.addCleanup(self.env.stop)

    def test_template_and_style_reads_do_not_query_sqlite(self):
        key=next(k for k,v in theme.CATALOG.items() if v['default']=='Главное меню')
        theme.save(app.db,20,key,'Домой')
        theme.set_option(app.db,20,'style','primary')
        statements=[]
        app.db.set_trace_callback(statements.append)
        try:
            for _ in range(20):
                label=theme.text(key)
                self.assertEqual(str(label),'Домой')
                self.assertEqual(theme.button(label,callback_data='p:home:0').style,'primary')
        finally:app.db.set_trace_callback(None)
        self.assertFalse(any(q.lstrip().upper().startswith('SELECT') for q in statements))
        theme.reset(app.db,20,key)
        self.assertEqual(str(theme.text(key)),'Главное меню')
        theme.reset(app.db,20)
        self.assertEqual(theme.option('style'),'original')

    def test_identical_chat_and_user_metadata_does_not_rewrite_rows(self):
        chat=NS(id=-100,title='Group',type='supergroup')
        user=NS(id=10,username='same',full_name='Same User')
        app.remember_chat(chat);app.remember_user(-100,user)
        before=app.db.total_changes
        app.remember_chat(chat);app.remember_user(-100,user)
        self.assertEqual(app.db.total_changes,before)
        user.full_name='Changed User'
        app.remember_user(-100,user)
        self.assertEqual(app.db.execute('SELECT full_name FROM chat_users WHERE user_id=10').fetchone()[0],'Changed User')

    def test_expiry_does_not_rewrite_already_clean_records(self):
        result=dict(category='spam',confidence=.8,reason='spam',evidence='old',action='review',model='test')
        did=store.record(app.db,-100,10,71,'old',[],result,'observation')
        app.db.execute("UPDATE decisions SET created_at='2000-01-01' WHERE id=?",(did,));app.db.commit()
        store.purge(app.db,30)
        before=app.db.total_changes
        store.purge(app.db,30)
        self.assertEqual(app.db.total_changes,before)
        self.assertEqual(store.get(app.db,did)['category'],'spam')

if __name__=='__main__':unittest.main()
