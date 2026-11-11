import os, json
import unittest
from unittest.mock import patch, AsyncMock
import test_bot as legacy
from test_bot import message, callback
import bot as app
import ui_theme as theme

class ThemeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        legacy.FlowTests.setUp(self)
        for table in ('ui_overrides','ui_options','ui_drafts','ui_audit'):app.db.execute('DELETE FROM '+table)
        app.db.commit()
        self.env=patch.dict(os.environ,{'BOT_OWNER_ID':'20'});self.env.start();self.addCleanup(self.env.stop)

    def key(self,value):
        return next(k for k,v in theme.CATALOG.items() if v['default']==value)

    async def click(self,data,uid=20,chat_type='private'):
        handler=next(h.callback for h in app.dp.callback_query.handlers if h.callback.__module__=='ui_owner')
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
