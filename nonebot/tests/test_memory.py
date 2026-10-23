import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from src.memory import MemoryConfig, MemoryStore, MemoryService


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_early_count_trigger_and_cooldown(self):
        self.config.early_records=10
        for i in range(10): self.add(i)
        now=datetime(2026,1,1,13,tzinfo=ZoneInfo('Asia/Shanghai'))
        await self.service.run(now)
        self.assertEqual(self.store.status()['unprocessed'],0)
        count=self.service.summarize.await_count
        for i in range(10,20): self.add(i)
        await self.service.run(now)
        self.assertEqual(self.service.summarize.await_count,count)
        await self.service.run(datetime(2026,1,1,14,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.assertGreater(self.service.summarize.await_count,count)
        self.assertIn('previous_summary',self.service.summarize.await_args.args[0])

    async def test_early_character_threshold_before_daily_hour(self):
        self.config.early_chars=2000
        self.add(text='一条很长的聊天'*400)
        await self.service.run(datetime(2026,1,1,2,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.assertGreater(self.service.summarize.await_count,0)

    async def test_early_disabled_does_not_process_current_day(self):
        self.config.early_enabled=False
        for i in range(100): self.add(i)
        await self.service.run(datetime(2026,1,1,13,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.service.summarize.assert_not_awaited()
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'memory.sqlite3'
        self.store = MemoryStore(self.path)
        self.config = MemoryConfig(enabled=True, summary_chars=200, batch_chars=2000)
        self.service = MemoryService(self.store, self.config)
        self.service.summarize = AsyncMock(return_value=('摘要保留人物和最终约定', {'total_tokens': 30}))
        self.day = datetime(2026,1,1,12,tzinfo=ZoneInfo('Asia/Shanghai'))

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def add(self, ident=1, text='林202：周日15点聚餐，取消周六安排', bot=1, group=100, day=None):
        return self.store.add(bot,group,{'role':'user','message_id':ident,'group_id':group,
            'user_id':202,'content':text},self.config,(day or self.day).timestamp())

    async def test_facts_validate_scope_and_merge_sources(self):
        first=self.add(1)
        second=self.add(2)
        other=self.add(3,group=999)
        fact={'subject':'202','relation':'聚餐安排','content':'周日15点吃饭','status':'confirmed','source_ids':[first]}
        self.store.save_facts(1,100,'2026-01-01',[fact],[first],self.config)
        self.store.save_facts(1,100,'2026-01-01',[{**fact,'source_ids':[second]}],[second],self.config)
        self.store.save_facts(1,100,'2026-01-01',[{**fact,'content':'错误来源','source_ids':[other]}],[other],self.config)
        facts=self.store.facts_for_query(1,100,'聚餐',None)
        self.assertEqual(len(facts),1)
        self.assertEqual(facts[0]['source_ids'],[first,second])
        self.assertTrue(facts[0]['source_available'])
        self.assertFalse(self.store.facts_for_query(1,999,'聚餐',None))
        self.assertFalse(self.store.facts_for_query(1,100,'聚餐',second))
        self.store.db.execute('DELETE FROM messages WHERE id=?',(first,))
        self.assertFalse(self.store.facts_for_query(1,100,'聚餐',None)[0]['source_available'])

    async def test_stored_attribution_tracks_actual_speaker(self):
        record={'role':'user','user_id':202,'message_id':8,'content':'小李对花生过敏','mentioned_user_ids':['303']}
        ident=self.store.add(1,100,record,self.config,self.day.timestamp())
        fact={'subject':'小李','subject_user_id':'303','statement_type':'self_report','relation':'过敏','content':'花生过敏','status':'confirmed','source_ids':[ident]}
        self.store.save_facts(1,100,'2026-01-01',[fact],[ident],self.config)
        result=self.store.facts_for_query(1,100,'过敏',None)[0]
        self.assertEqual(result['speaker_user_ids'],['202'])
        self.assertEqual(result['subject_user_id'],'303')
        self.assertEqual(result['statement_type'],'hearsay')
        self.assertEqual(result['status'],'uncertain')

    async def test_legacy_fact_is_unknown_when_read(self):
        import json
        ident=self.add()
        legacy={'subject':'202','relation':'聚餐','content':'周日聚餐','status':'confirmed','source_ids':[ident]}
        self.store.db.execute('INSERT INTO facts(bot,grp,day,payload,digest,latest_source) VALUES (?,?,?,?,?,?)',
            ('1','100','2026-01-01',json.dumps(legacy,ensure_ascii=False),'legacy',ident))
        result=self.store.facts_for_query(1,100,'聚餐',None)[0]
        self.assertEqual(result['statement_type'],'unknown')
        self.assertEqual(result['status'],'uncertain')

    async def test_assistant_only_fact_is_uncertain(self):
        ident=self.store.add(1,100,{'role':'assistant','message_id':9,'content':'聚餐周日'},self.config,self.day.timestamp())
        self.store.save_facts(1,100,'2026-01-01',[{'subject':'202','relation':'聚餐','content':'周日','status':'confirmed','source_ids':[ident]}],[ident],self.config)
        self.assertEqual(self.store.facts_for_query(1,100,'聚餐',None)[0]['status'],'uncertain')

    async def test_fts_finds_old_synonym_beyond_recent_200(self):
        old=self.add(0,text='周日聚餐在海棠餐厅')
        for i in range(1,220): self.add(i,text='今天晴天适合散步')
        hits=self.store.search(1,100,'吃饭',limit=1)
        self.assertIn(old,[row['record_id'] for row in hits])

    def save_person_fact(self, ident, content, group=100, event_time=''):
        rid=self.add(ident,text=content,group=group)
        fact={'subject':'202','subject_user_id':'202','statement_type':'self_report','relation':'聚餐时间','content':content,'status':'confirmed','source_ids':[rid],'event_time':event_time}
        self.store.save_facts(1,group,'2026-01-01',[fact],[rid],self.config)
        return rid

    async def test_conflicts_are_marked_without_selecting_truth(self):
        self.save_person_fact(1,'聚餐周六')
        second=self.save_person_fact(2,'聚餐周日')
        facts=self.store.list_facts(1,100)
        self.assertTrue(all(fact['needs_review'] for fact in facts))
        self.assertTrue(all(fact['status']=='uncertain' for fact in facts))
        self.assertTrue(all(fact['conflict_ids'] for fact in facts))
        past=self.store.list_facts(1,100,second)
        self.assertEqual(len(past),1)
        self.assertNotIn('needs_review',past[0])

    async def test_different_events_do_not_conflict(self):
        self.save_person_fact(1,'聚餐周六',event_time='2026-01-01')
        self.save_person_fact(2,'聚餐周日',event_time='2026-02-01')
        self.assertTrue(all(not fact.get('needs_review') for fact in self.store.list_facts(1,100)))

    async def test_manual_review_survives_reextraction_and_preserves_raw(self):
        rid=self.save_person_fact(1,'聚餐周六')
        fact=self.store.list_facts(1,100)[0]
        self.store.review_fact(1,100,fact['fact_id'],'edit','聚餐周日','confirmed')
        self.save_person_fact(1,'聚餐周六')
        result=self.store.list_facts(1,100)[0]
        self.assertEqual(result['content'],'聚餐周日')
        self.assertEqual(result['original_content'],'聚餐周六')
        self.assertTrue(result['manually_reviewed'])
        self.assertEqual(self.store.recent(1,100,rid+1,1)[0]['content'],'聚餐周六')
        with self.assertRaises(KeyError):self.store.review_fact(1,999,fact['fact_id'],'delete')
        self.store.review_fact(1,100,fact['fact_id'],'delete')
        self.assertFalse(self.store.facts_for_query(1,100,'聚餐',None))

    async def test_review_does_not_attach_to_reused_fact_id(self):
        self.save_person_fact(1,'聚餐周六')
        ident=self.store.list_facts(1,100)[0]['fact_id']
        self.store.review_fact(1,100,ident,'delete')
        self.store.db.execute('DELETE FROM facts WHERE id=?',(ident,))
        self.save_person_fact(2,'聚餐周日')
        result=self.store.list_facts(1,100)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['content'],'聚餐周日')

    async def test_credentials_redacted_at_storage_and_retrieval(self):
        secret='sk-'+('a'*24)
        rid=self.add(text='api_key='+secret)
        self.assertNotIn(secret,self.store.db.execute('SELECT payload FROM messages WHERE id=?',(rid,)).fetchone()[0])
        self.assertIn('[已隐藏凭据]',self.store.recent(1,100,rid+1,1)[0]['content'])

    async def test_restart_and_duplicate_are_safe(self):
        original = self.add()
        self.assertEqual(self.add(), original)
        self.assertEqual(self.store.status()['records'],1)
        self.store.db.close()
        self.store=MemoryStore(self.path)
        self.assertEqual(self.store.status()['records'],1)

    async def test_daily_checkpoint_and_no_duplicate_calls(self):
        self.add()
        now=datetime(2026,1,2,4,tzinfo=ZoneInfo('Asia/Shanghai'))
        result=await self.service.run(now)
        self.assertEqual(result['calls'],1)
        self.assertEqual(self.store.status()['unprocessed'],0)
        await self.service.run(now)
        self.service.summarize.assert_awaited_once()

    async def test_no_early_summary_or_current_day_summary(self):
        self.add()
        await self.service.run(datetime(2026,1,1,23,tzinfo=ZoneInfo('Asia/Shanghai')))
        await self.service.run(datetime(2026,1,2,2,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.service.summarize.assert_not_awaited()

    async def test_failure_keeps_raw_and_retries(self):
        self.add()
        now=datetime(2026,1,2,4,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.service.summarize.side_effect=RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            await self.service.run(now)
        self.assertEqual(self.store.status()['unprocessed'],1)
        self.service.summarize.side_effect=None
        await self.service.run(now)
        self.assertEqual(self.store.status()['unprocessed'],0)

    async def test_ten_day_merge_keeps_daily_sources(self):
        self.add()
        now=datetime(2026,1,11,4,tzinfo=ZoneInfo('Asia/Shanghai'))
        await self.service.run(now)
        self.assertEqual(self.store.status()['summaries'],2)
        await self.service.run(now)
        self.assertEqual(self.service.summarize.await_count,2)

    async def test_search_never_crosses_bot_or_group_and_returns_neighbors(self):
        self.add(1,'原来周六聚餐')
        self.add(2,'改成周日下午三点聚餐')
        self.add(3,'聚餐秘密',bot=2)
        self.add(4,'聚餐其他群',group=200)
        results=self.store.search(1,100,'聚餐')
        self.assertEqual(len(results),2)
        self.assertTrue(all(r['group_id']==100 for r in results))

    async def test_large_message_split_and_call_budget(self):
        self.add(text='事实内容'*2000)
        self.config.max_calls_per_run=1
        now=datetime(2026,1,2,4,tzinfo=ZoneInfo('Asia/Shanghai'))
        await self.service.run(now)
        self.assertEqual(self.store.status()['unprocessed'],1)
        self.assertEqual(self.service.summarize.await_count,1)
        for _ in range(10):
            await self.service.run(now)
        self.assertEqual(self.store.status()['unprocessed'],0)

    async def test_retention_never_drops_unsummarized_raw(self):
        self.config.max_records=100
        for i in range(105): self.add(i)
        self.assertEqual(self.store.status()['records'],105)
        await self.service.run(datetime(2026,1,2,4,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.assertEqual(self.store.status()['records'],100)

    async def test_challenge_gets_original_evidence(self):
        self.add()
        context=self.store.context(1,100,'聚餐记错了',100,self.config)
        self.assertTrue(context['verification_requested'])
        self.assertTrue(context['evidence'])

    async def test_database_lease_blocks_second_process_and_releases_on_failure(self):
        import json,time
        with self.store.db:
            self.store.db.execute("INSERT INTO metadata VALUES ('lease',?)",(json.dumps({'owner':'other','expires':time.time()+600}),))
        self.assertTrue((await self.service.run())['busy'])
        with self.store.db: self.store.db.execute('DELETE FROM metadata')
        self.add()
        self.service.summarize.side_effect=RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            await self.service.run(datetime(2026,1,2,4,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM metadata WHERE key='lease'").fetchone())

    async def test_late_message_invalidates_old_period(self):
        self.add()
        now=datetime(2026,1,11,4,tzinfo=ZoneInfo('Asia/Shanghai'))
        await self.service.run(now)
        self.add(2,'后来取消聚餐')
        self.config.max_calls_per_run=1
        await self.service.run(now)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM summaries WHERE kind='period'").fetchone())
        await self.service.run(now)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM summaries WHERE kind='period'").fetchone())

    def test_bad_config_is_rejected(self):
        for values in ({'max_records':1},{'timezone':'bad/timezone'},
                       {'summary_chars':8000,'batch_chars':2000}):
            with self.assertRaises(ValueError): MemoryConfig(**values)
