import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_fetch import response, call
from src.deepseek_client import DeepSeekClient
from src.memory import MemoryStore, MemoryService, MemoryConfig, search_for_model


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=MemoryService(MemoryStore(Path(self.temp.name)/'memory.sqlite3'),MemoryConfig(enabled=True))
        store=self.service.store
        for bot,group,text in [(1,100,'聚餐改为周日15点'),(2,100,'其他机器人秘密聚餐'),(1,200,'其他群秘密聚餐')]:
            store.add(bot,group,{'role':'user','message_id':1,'group_id':group,'content':text},self.service.config)
        ident=store.add(1,100,{'role':'user','message_id':2,'group_id':100,'content':'问聚餐'},self.service.config)
        self.context={'bot_user_id':1,'group_id':100,'conversation':[],
                      'current_request':{'group_id':100,'memory_record_id':ident,'content':'聚餐几点'}}
        self.client=DeepSeekClient()
        if self.client.search_client: await self.client.search_client.close()
        if self.client.fetch_client: await self.client.fetch_client.close()
        self.client.search_client=self.client.fetch_client=None

    async def asyncTearDown(self):
        await self.client.close()
        self.service.store.db.close()
        self.temp.cleanup()

    async def test_tool_loop_and_reasoning_preserved(self):
        replies=[response(calls=[call('memory_search',{'query':'聚餐'})],reasoning='test reasoning'),response('周日15点')]
        with patch('src.deepseek_client.get_memory',return_value=self.service), \
             patch.object(self.client,'_complete',new_callable=AsyncMock,side_effect=replies) as complete:
            result=await self.client.chat([{'role':'system','content':'人设'}],context=self.context)
        self.assertEqual(result,'周日15点')
        transcript=complete.await_args.args[0]
        tools=[json.loads(item['content']) for item in transcript if item['role']=='tool']
        self.assertEqual(len(tools[0]['records']),1)
        self.assertNotIn('秘密',json.dumps(tools,ensure_ascii=False))
        self.assertTrue(any(m.get('reasoning_content')=='test reasoning' for m in transcript))

    async def test_limit_blocks_third_search(self):
        calls=[call('memory_search',{'query':'聚餐'},str(i)) for i in range(3)]
        with patch('src.deepseek_client.get_memory',return_value=self.service), \
             patch.object(self.client,'_complete',new_callable=AsyncMock,side_effect=[response(calls=calls),response('回答')]) as complete, \
             patch.object(self.service.store,'search',wraps=self.service.store.search) as search:
            await self.client.chat([{'role':'system','content':'人设'}],context=self.context)
        self.assertEqual(search.call_count,2)
        self.assertEqual(complete.await_args.kwargs['tool_choice'],'none')

    async def test_cross_group_parameter_and_bad_query_are_rejected(self):
        for args in ({'query':'聚餐','group_id':200},{'query':''},{'query':123},{'query':'x'*129}):
            with self.assertRaises(ValueError):
                search_for_model(self.service,self.context,json.dumps(args),4000)

    async def test_result_budget_and_future_records_excluded(self):
        self.service.store.add(1,100,{'role':'user','message_id':3,'content':'聚餐未来消息'},self.service.config)
        result,used=search_for_model(self.service,self.context,'{"query":"聚餐"}',4000)
        self.assertNotIn('未来',json.dumps(result,ensure_ascii=False))
        result,used=search_for_model(self.service,self.context,'{"query":"聚餐"}',0)
        self.assertFalse(result['records'])
        self.assertEqual(used,0)

    async def test_challenge_without_evidence_forces_lookup(self):
        self.context['memory']={'verification_requested':True,'evidence':[]}
        with patch('src.deepseek_client.get_memory',return_value=self.service), \
             patch.object(self.client,'_complete',new_callable=AsyncMock,side_effect=[response(calls=[call('memory_search',{'query':'聚餐'})]),response('核查回答')]) as complete:
            await self.client.chat([{'role':'system','content':'人设'}],context=self.context)
        choice=complete.await_args_list[0].kwargs['tool_choice']
        self.assertEqual(choice['function']['name'],'memory_search')

    async def test_disabled_tool_is_not_advertised(self):
        self.service.config.max_tool_searches=0
        with patch('src.deepseek_client.get_memory',return_value=self.service), \
             patch.object(self.client,'_complete',new_callable=AsyncMock,return_value=response('直接回答')) as complete:
            await self.client.chat([{'role':'system','content':'人设'}],context=self.context)
        self.assertNotIn('tools',complete.await_args.kwargs)

    async def test_storage_failure_returns_tool_error_without_crashing(self):
        with patch('src.deepseek_client.get_memory',return_value=self.service), \
             patch.object(self.service.store,'search',side_effect=OSError('disk failed')), \
             patch.object(self.client,'_complete',new_callable=AsyncMock,side_effect=[response(calls=[call('memory_search',{'query':'聚餐'})]),response('暂时无法确认')]) as complete:
            self.assertEqual(await self.client.chat([{'role':'system','content':'人设'}],context=self.context),'暂时无法确认')
        result=next(json.loads(m['content']) for m in complete.await_args.args[0] if m['role']=='tool')
        self.assertEqual(result['error'],'memory_unavailable')
