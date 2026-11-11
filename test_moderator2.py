import os
os.environ['AI_CHAT_COMPLETIONS_URL']=''
import unittest, asyncio
from unittest.mock import patch, AsyncMock
import test_bot as legacy
from test_bot import message, callback
import bot as app
import moderation_store as store
from ai_analyzer import validate, evaluate

class V2Tests(unittest.IsolatedAsyncioTestCase):
    setUp=legacy.FlowTests.setUp
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
