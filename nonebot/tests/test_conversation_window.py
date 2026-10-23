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
from nonebot.adapters.onebot.v11 import MessageSegment
from src.plugins import chat
from src.reply_planner import ReplyPlan

class ConversationWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_chat import event,GroupQueueTests
        GroupQueueTests.reset();self.event=event
        self.policy=patch.dict(chat.reply_config,{'conversation_window_seconds':120,'planner_enabled':True,'planner_filter_enabled':True,'planner_interval_seconds':30,'cooldown_seconds':300,'user_cooldown_seconds':0,'delay_min_seconds':0,'delay_max_seconds':0,'keywords':[]})
        self.policy.start();self.bot=SimpleNamespace(send=AsyncMock(return_value={'message_id':900}))
    async def asyncTearDown(self):
        self.policy.stop();chat.conversation_activity.clear()

    async def test_reply_opens_window_and_short_followup_bypasses_gates(self):
        with patch.object(chat,'decide',new_callable=AsyncMock,return_value=(ReplyPlan(should_reply=False,needs_memory=False),{},{})) as planner,patch.object(chat.deepseek_client,'chat',new_callable=AsyncMock,return_value='我在') as answer:
            await chat.handle_group_message(self.bot,self.event(1,'你好',at=True))
            await chat.handle_group_message(self.bot,self.event(2,'呜呜呜',at=False))
            self.assertEqual(answer.await_count,2)
            self.assertTrue(planner.await_args.args[-1])
            self.assertEqual(self.bot.send.await_count,2)

    def test_window_is_scoped_to_bot_group_and_user(self):
        chat.conversation_activity[('1',1001,2)]=time.monotonic()
        self.assertTrue(chat.is_followup(self.event(1,'你',at=False)))
        self.assertFalse(chat.is_followup(self.event(2,'你',at=False,user_id=3)))
        self.assertFalse(chat.is_followup(self.event(3,'你',at=False,group_id=999)))
        other=self.event(4,'你',at=False);other.self_id=9
        self.assertFalse(chat.is_followup(other))

    def test_expiry_and_disabled_window(self):
        chat.conversation_activity[('1',1001,2)]=time.monotonic()-121
        self.assertFalse(chat.is_followup(self.event(1,'你',at=False)))
        chat.conversation_activity[('1',1001,2)]=time.monotonic()
        with patch.dict(chat.reply_config,{'conversation_window_seconds':0}):
            self.assertFalse(chat.is_followup(self.event(2,'你',at=False)))

    def test_explicitly_addressing_another_person_is_not_followup(self):
        chat.conversation_activity[('1',1001,2)]=time.monotonic()
        msg=self.event(1,'来聊聊',at=False,mentions=[MessageSegment.at(303)])
        self.assertFalse(chat.is_followup(msg))

    def test_window_restores_from_confirmed_persistent_reply(self):
        from src.memory import MemoryConfig,MemoryService,MemoryStore
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'memory.sqlite3');cfg=MemoryConfig(enabled=True)
            service=MemoryService(store,cfg)
            try:
                store.add(1,1001,{'role':'assistant','message_id':10,'user_id':1,'reply_to_user_id':2,'content':'我在'},cfg,time.time()-30)
                with patch.object(chat,'get_memory',return_value=service):
                    self.assertTrue(chat.is_followup(self.event(11,'你',at=False)))
                    self.assertFalse(chat.is_followup(self.event(12,'你',at=False,user_id=3)))
            finally:store.db.close()

    async def test_busy_followup_is_queued_instead_of_discarded(self):
        chat.conversation_activity[('1',1001,2)]=time.monotonic()
        lock=chat.group_locks[1001];await lock.acquire()
        with patch.object(chat,'decide',new_callable=AsyncMock,return_value=(ReplyPlan(should_reply=True,needs_memory=False),{},{})),patch.object(chat.deepseek_client,'chat',new_callable=AsyncMock,return_value='收到') as answer:
            task=asyncio.create_task(chat.handle_group_message(self.bot,self.event(1,'给你一拳',at=False)))
            try:
                await asyncio.sleep(.01)
                self.assertFalse(task.done())
            finally:lock.release()
            await task
            answer.assert_awaited_once()
