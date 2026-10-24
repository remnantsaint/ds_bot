import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from pydantic import ValidationError
from src.reply_planner import ReplyPlan,basic_candidate,compact,packet,decide,select_memory
from src.token_budget import count_tokens
from src.memory import MemoryStore,MemoryConfig,search_for_model,MemoryService

class PlannerTests(unittest.IsolatedAsyncioTestCase):
    def test_basic_filter(self):
        for text in ['😀😀','哈哈哈哈','好的','!!!']:
            self.assertFalse(basic_candidate(text))
        self.assertTrue(basic_candidate('你觉得这个方案怎么样？'))

    def test_compaction_preserves_identity_and_marks_omission(self):
        row=compact({'user_id':202,'mentioned_user_ids':['303'],'content':'否定'+('中段'*500)+'改为周日'},100)
        self.assertEqual(row['user_id'],202)
        self.assertEqual(row['mentioned_user_ids'],['303'])
        self.assertTrue(row['truncated'])
        self.assertTrue(row['content'].startswith('否定'))
        self.assertTrue(row['content'].endswith('改为周日'))

    def test_packet_budget_and_no_persona_or_memory(self):
        data=packet({'user_id':202,'content':'查聚餐'},[{'content':'历史'*300}]*10,{'planner_input_tokens':500},True)
        self.assertLessEqual(count_tokens(json.dumps(data,ensure_ascii=False)),500)
        self.assertNotIn('memory',data)
        self.assertNotIn('persona',data)

    def test_numeric_qq_normalizes_but_boolean_is_rejected(self):
        self.assertEqual(ReplyPlan(should_reply=True,needs_memory=False,subject_user_id=202).subject_user_id,'202')
        with self.assertRaises(ValidationError):ReplyPlan(should_reply=True,needs_memory=False,subject_user_id=True)

    def test_invalid_ranges_and_unbounded_queries_rejected(self):
        for extra in [{'date_from':'2026-10-24','date_to':'2026-10-01'},{'date_from':'20261024'},{'query':'x'*129}, {'bot':'other'}]:
            with self.assertRaises(ValidationError):ReplyPlan(should_reply=True,needs_memory=False,**extra)
        with self.assertRaises(ValidationError):ReplyPlan(should_reply=True,needs_memory=True,query='')

    def client(self,result):
        response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps(result)))],usage=None)
        create=AsyncMock(return_value=response)
        return SimpleNamespace(config={'model':'test'},client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))

    async def test_force_reply_and_unknown_identity(self):
        client=self.client({'should_reply':False,'needs_memory':False,'subject_user_id':'999'})
        plan,_,_=await decide(client,{'user_id':202,'content':'你好'},[],{},True)
        self.assertTrue(plan.should_reply)
        self.assertIsNone(plan.subject_user_id)

    async def test_malformed_json_fails_closed(self):
        client=self.client({'should_reply':'yes','needs_memory':False})
        with self.assertRaises(ValidationError):await decide(client,{'content':'问题'},[],{})

    async def test_timeout(self):
        client=self.client({})
        async def slow(**kwargs):await asyncio.sleep(10)
        client.client.chat.completions.create.side_effect=slow
        with self.assertRaises(TimeoutError):await decide(client,{'content':'问题'},[],{'planner_timeout_seconds':.01})

    def test_related_daily_summary_fallback_is_bounded_and_excludes_future(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'summary.sqlite3');cfg=MemoryConfig(enabled=True)
            try:
                store.save_summary(1,100,'daily','2026-10-20','202：聚餐改为周日','10',{})
                store.save_summary(1,100,'daily','2026-10-21','202：聚餐未来更正','30',{})
                plan=ReplyPlan(should_reply=True,needs_memory=True,query='聚餐',subject_user_id='202')
                result=select_memory(store,1,100,20,plan,cfg)
                self.assertEqual(len(result['summaries']),1)
                self.assertIn('周日',result['summaries'][0]['content'])
                self.assertNotIn('未来',result['summaries'][0]['content'])
            finally:store.db.close()

    def test_selected_memory_scope_subject_time_and_future(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'test.sqlite3');cfg=MemoryConfig(enabled=True)
            def add(mid,user,group,day,text):
                return store.add(1,group,{'role':'user','user_id':user,'message_id':mid,'content':text},cfg,datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
            try:
                target=add(1,202,100,'2026-10-20','聚餐周日')
                add(2,303,100,'2026-10-20','聚餐周六')
                add(3,202,999,'2026-10-20','聚餐隔壁群')
                add(4,202,100,'2026-10-21','聚餐其他日期')
                current=add(5,202,100,'2026-10-22','问聚餐')
                add(6,202,100,'2026-10-23','聚餐未来信息')
                plan=ReplyPlan(should_reply=True,needs_memory=True,query='聚餐',subject_user_id='202',date_from='2026-10-20',date_to='2026-10-20')
                result=select_memory(store,1,100,current,plan,cfg)
                self.assertEqual([row['record_id'] for row in result['evidence']],[target])
                self.assertEqual(result['summaries'],[])
                store.fts=False
                fallback=select_memory(store,1,100,current,plan,cfg)
                self.assertEqual([row['record_id'] for row in fallback['evidence']],[target])
                self.assertLessEqual(count_tokens(json.dumps(result,ensure_ascii=False)),cfg.context_tokens)
                context={'bot_user_id':1,'group_id':100,'current_request':{'memory_record_id':current},'memory_plan':plan.model_dump()}
                tool,_=search_for_model(MemoryService(store,cfg),context,'{"query":"聚餐"}',4000)
                self.assertEqual([row['record_id'] for row in tool['records']],[target])
            finally:store.db.close()

class PlannerChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.plugins import chat
        from test_chat import GroupQueueTests,event
        self.chat=chat;self.event=event;GroupQueueTests.reset()
        self.policy=patch.dict(chat.reply_config,{'planner_enabled':True,'planner_filter_enabled':True,'planner_interval_seconds':30,'cooldown_seconds':0,'delay_min_seconds':0,'delay_max_seconds':0,'keywords':[]})
        self.policy.start();self.bot=SimpleNamespace(send=AsyncMock(return_value={'message_id':900}))
    async def asyncTearDown(self):self.policy.stop()

    async def test_noise_never_calls_api(self):
        with patch.object(self.chat,'decide',new_callable=AsyncMock) as planner:
            await self.chat.handle_group_message(self.bot,self.event(1,'😀😀',at=False))
            planner.assert_not_awaited()

    async def test_no_reply_stops_before_answer_and_interval_suppresses_next(self):
        plan=ReplyPlan(should_reply=False,needs_memory=False)
        with patch.object(self.chat,'decide',new_callable=AsyncMock,return_value=(plan,{},{})) as planner,patch.object(self.chat.deepseek_client,'chat',new_callable=AsyncMock) as answer:
            await self.chat.handle_group_message(self.bot,self.event(1,'这是一般闲聊消息',at=False))
            await self.chat.handle_group_message(self.bot,self.event(2,'这是另外一条消息',at=False))
            planner.assert_awaited_once();answer.assert_not_awaited()

    async def test_forced_failure_falls_back_but_ordinary_failure_stays_silent(self):
        with patch.object(self.chat,'decide',new_callable=AsyncMock,side_effect=ValueError('invalid')),patch.object(self.chat.deepseek_client,'chat',new_callable=AsyncMock,return_value='回复') as answer:
            await self.chat.handle_group_message(self.bot,self.event(1,'普通提问消息',at=False))
            answer.assert_not_awaited()
            await self.chat.handle_group_message(self.bot,self.event(2,'明确提问',at=True))
            answer.assert_awaited_once()

    async def test_no_memory_disables_memory_context_and_tool(self):
        plan=ReplyPlan(should_reply=True,needs_memory=False)
        with patch.object(self.chat,'decide',new_callable=AsyncMock,return_value=(plan,{},{})),patch.object(self.chat.deepseek_client,'chat',new_callable=AsyncMock,return_value='回复') as answer:
            await self.chat.handle_group_message(self.bot,self.event(1,'解释一下为什么下雨',at=True))
            context=answer.await_args.kwargs['context']
            self.assertNotIn('memory',context)
            self.assertFalse(context['memory_search_allowed'])

    async def test_compacted_history_passes_real_answer_context_validation(self):
        plan=ReplyPlan(should_reply=True,needs_memory=False)
        self.chat._record_group_message(self.event(1,'上一条背景消息',at=False))
        response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='你好，我在。',tool_calls=None))])
        with patch.object(self.chat,'decide',new_callable=AsyncMock,return_value=(plan,{},{})),patch.object(self.chat.deepseek_client,'_complete',new_callable=AsyncMock,return_value=response) as complete:
            await self.chat.handle_group_message(self.bot,self.event(2,'你好',at=True))
            complete.assert_awaited_once()
            sent=complete.await_args.args[0]
            payload=json.loads(next(row['content'] for row in sent if row['role']=='user'))
            self.assertTrue(payload['conversation'])
            self.assertTrue(all(row['group_id']==1001 for row in payload['conversation']))
            self.bot.send.assert_awaited_once()

    async def test_disabled_filter_sends_short_message_to_planner(self):
        plan=ReplyPlan(should_reply=False,needs_memory=False)
        with patch.dict(self.chat.reply_config,{'planner_filter_enabled':False}),patch.object(self.chat,'decide',new_callable=AsyncMock,return_value=(plan,{},{})) as planner:
            await self.chat.handle_group_message(self.bot,self.event(1,'可恶啊',at=False))
            planner.assert_awaited_once()

    async def test_planner_can_decline_same_user_message_after_answer(self):
        plans = [(ReplyPlan(should_reply=False, needs_memory=False), {}, {}) for _ in range(2)]
        with patch.dict(self.chat.reply_config, {'planner_filter_enabled':False,'planner_interval_seconds':0}), \
             patch.object(self.chat,'decide',new_callable=AsyncMock,side_effect=plans) as planner, \
             patch.object(self.chat.deepseek_client,'chat',new_callable=AsyncMock,return_value='回答') as answer:
            await self.chat.handle_group_message(self.bot,self.event(940,'先问一句',at=True))
            await self.chat.handle_group_message(self.bot,self.event(941,'继续聊一句',at=False))
        self.assertEqual(planner.await_count,2)
        self.assertTrue(planner.await_args_list[0].args[-1])
        self.assertFalse(planner.await_args_list[1].args[-1])
        answer.assert_awaited_once()
