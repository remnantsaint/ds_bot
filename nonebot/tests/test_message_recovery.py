import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import nonebot
try:nonebot.get_driver()
except ValueError:nonebot.init(driver='~fastapi',log_level='WARNING',deepseek_api_key='test-only',_env_file=None)
from src import message_recovery as recovery


def event_data(mid=1,stamp=None,group=100):
    return {'time':int(stamp or time.time()),'self_id':1,'group_id':group,'user_id':202,
            'message_id':mid,'post_type':'message','message_type':'group','sub_type':'normal','font':0,
            'message':[{'type':'at','data':{'qq':'1'}},{'type':'text','data':{'text':'你好'}}],
            'original_message':[{'type':'text','data':{'text':'你好'}}],
            'raw_message':'你好','sender':{'user_id':202,'nickname':'测试'},'to_me':True}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'inbox.sqlite3'
        self.store=recovery.Inbox(self.path);self.bot=SimpleNamespace(self_id='1')
        self.patcher=patch.object(recovery,'get_inbox',return_value=self.store);self.patcher.start()
        recovery.recovering.clear()
    async def asyncTearDown(self):
        self.patcher.stop();recovery.recovering.clear()
        self.store.db.close();self.temp.cleanup()

    def test_message_id_is_deduplicated_per_group(self):
        key=self.store.capture(1,event_data())
        self.assertTrue(self.store.claim(key));self.store.finish(key)
        self.store.capture(1,event_data());self.assertFalse(self.store.claim(key))
        self.store.capture(1,event_data(group=999))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM inbox').fetchone()[0],2)

    def test_restart_requeues_processing_but_not_uncertain_send(self):
        first=self.store.capture(1,event_data(1));self.store.claim(first)
        second=self.store.capture(1,event_data(2));self.store.claim(second);self.store.mark(second,'sending')
        self.store.db.close();self.store=recovery.Inbox(self.path)
        self.assertEqual(self.store.state(first),'queued')
        self.assertEqual(self.store.state(second),'uncertain')

    def test_nested_credential_is_redacted(self):
        data=event_data();secret='sk-'+'a'*24
        data['message'][1]['data']['text']=secret
        self.store.capture(1,data)
        payload=self.store.db.execute('SELECT payload FROM inbox').fetchone()[0]
        self.assertNotIn(secret,payload)

    def test_history_validation_preserves_at_and_rejects_other_group(self):
        event=recovery.history_event(1,100,event_data())
        self.assertTrue(event.is_tome())
        with self.assertRaises(ValueError):recovery.history_event(1,999,event_data())

    async def test_reconnect_queues_then_replays_once(self):
        from src.plugins import chat
        event=recovery.history_event(1,100,event_data())
        with patch.object(chat,'process_group_message',new_callable=AsyncMock) as process:
            recovery.recovering.add('1')
            await chat.dispatch_saved(self.bot,event)
            process.assert_not_awaited()
            recovery.recovering.discard('1')
            await chat.dispatch_saved(self.bot,event,replay=True)
            await chat.dispatch_saved(self.bot,event)
            process.assert_awaited_once()
            self.assertEqual(self.store.state(('1','100','1')),'done')

    async def test_cancelled_processing_is_resumable(self):
        from src.plugins import chat
        event=recovery.history_event(1,100,event_data())
        with patch.object(chat,'process_group_message',new_callable=AsyncMock,side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):await chat.dispatch_saved(self.bot,event)
        self.assertEqual(self.store.state(('1','100','1')),'queued')

    async def test_cancelled_send_is_not_repeated(self):
        from src.plugins import chat
        event=recovery.history_event(1,100,event_data())
        async def sending(*args):
            recovery.mark_sending(self.bot,event)
            raise asyncio.CancelledError()
        with patch.object(chat,'process_group_message',side_effect=sending):
            with self.assertRaises(asyncio.CancelledError):await chat.dispatch_saved(self.bot,event)
        self.assertEqual(self.store.state(('1','100','1')),'uncertain')
        with patch.object(chat,'process_group_message',new_callable=AsyncMock) as process:
            await chat.dispatch_saved(self.bot,event,replay=True)
            process.assert_not_awaited()

    async def test_history_backfill_and_realtime_duplicate_processed_once(self):
        self.store.capture(1,event_data(1,time.time()-20),'done')
        self.bot.call_api=AsyncMock(return_value={'messages':[event_data(1,time.time()-20),event_data(2,time.time()-10)]})
        from src.plugins import chat
        with patch.object(chat,'process_group_message',new_callable=AsyncMock) as process:
            await recovery.recover(self.bot)
            await recovery.recover(self.bot)
            await chat.dispatch_saved(self.bot,recovery.history_event(1,100,event_data(2,time.time()-10)))
            process.assert_awaited_once()
            self.assertEqual(self.store.status()['done'],2)

    async def test_reconnect_captures_watermark_before_new_live_messages(self):
        old=time.time()-20
        self.store.capture(1,event_data(1,old),'done')
        with patch.object(recovery,'recover',new_callable=AsyncMock) as recover:
            await recovery.connected(self.bot)
            self.store.capture(1,event_data(2,time.time()))
            await asyncio.sleep(0)
            snapshot=recover.await_args.args[1]
            self.assertLess(snapshot[0]['stamp'],time.time()-10)
        recovery._tasks.clear()

    def test_seed_remembers_idle_groups_without_replaying_old_messages(self):
        import sqlite3
        path=Path(self.temp.name)/'memory.sqlite3'
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE messages(bot TEXT,grp TEXT,message_id TEXT,stamp REAL,role TEXT)')
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?)',('1','100','1',time.time()-3600,'user'))
        db.commit();db.close()
        self.store.seed(path)
        self.assertEqual(len(self.store.groups(1)),1)
        self.assertFalse(self.store.pending(1))
