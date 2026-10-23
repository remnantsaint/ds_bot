import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx
from fastapi import FastAPI
from src.admin import install_admin


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"persona_file": "persona.txt", "reply": {}}
        (self.root / "persona.txt").write_text("原人设", encoding="utf-8")
        self.app = FastAPI()
        install_admin(self.app, self.root, "test-password-long-enough")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        return await self.client.post("/admin/api/login", json={"password": "test-password-long-enough"}, headers={"X-Admin-Request": "1"})

    async def test_auth_csrf_logout(self):
        self.assertEqual((await self.client.get("/admin/api/state")).status_code, 401)
        self.assertEqual((await self.client.post("/admin/api/login", json={"password": "x"})).status_code, 403)
        self.assertEqual((await self.login()).status_code, 200)
        headers = {"X-Admin-Request": "1", "Origin": "http://evil"}
        self.assertEqual((await self.client.post("/admin/api/logout", json={}, headers=headers)).status_code, 403)
        self.assertEqual((await self.client.post("/admin/api/logout", json={}, headers={"X-Admin-Request": "1"})).status_code, 200)
        self.assertEqual((await self.client.get("/admin/api/state")).status_code, 401)

    async def test_persona_validation_and_atomic_save(self):
        await self.login()
        with patch("src.admin.load_config", return_value=self.config):
            for text, expected in [("  ", 422), ("新人设", 200)]:
                result = await self.client.put("/admin/api/persona", json={"text": text}, headers={"X-Admin-Request": "1"})
                self.assertEqual(result.status_code, expected)
        self.assertEqual((self.root / "persona.txt").read_text(encoding="utf-8"), "新人设\n")

    async def test_login_rate_limit(self):
        for _ in range(5):
            await self.client.post("/admin/api/login", json={"password": "wrong"}, headers={"X-Admin-Request": "1"})
        self.assertEqual((await self.login()).status_code, 429)

    async def test_page_is_mobile_ready(self):
        response = await self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("width=device-width", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_reply_save_validates_and_updates_runtime(self):
        import yaml
        from src.plugins import chat
        path = self.root / "config.yaml"
        path.write_text("reply: {}\n", encoding="utf-8")
        await self.login()
        with patch.dict(chat.reply_config, {}, clear=False):
            headers = {"X-Admin-Request": "1"}
            bad = await self.client.put("/admin/api/reply", json={"max_pending_per_group": 0}, headers=headers)
            self.assertEqual(bad.status_code, 422)
            good = await self.client.put("/admin/api/reply", json={"max_pending_per_group": "7", "planner_enabled": False, "planner_interval_seconds": 45, "conversation_window_seconds": 120}, headers=headers)
            self.assertEqual(good.status_code, 200)
            self.assertEqual(chat.reply_config["max_pending_per_group"], 7)
            self.assertFalse(chat.reply_config["planner_enabled"])
            self.assertEqual(chat.reply_config["planner_interval_seconds"],45)
            self.assertEqual(chat.reply_config["conversation_window_seconds"],120)
            self.assertEqual(yaml.safe_load(path.read_text())["reply"]["max_pending_per_group"], 7)

    async def test_state_does_not_expose_keys(self):
        await self.login()
        with patch("src.admin.load_persona", return_value="人设"), \
             patch("src.admin.load_config", return_value={"deepseek_api_key": "secret"}), \
             patch("src.admin.qq_online", new_callable=AsyncMock, return_value=True):
            response = await self.client.get("/admin/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret", response.text)

    async def test_memory_retention_requires_confirmation_and_preserves_records(self):
        from src.memory import MemoryStore, MemoryService, MemoryConfig
        service = MemoryService(MemoryStore(self.root/'test.sqlite3'),MemoryConfig(enabled=True))
        (self.root/'config.yaml').write_text('memory: {}\n', encoding='utf-8')
        config={'memory':service.config.model_dump()}
        try:
            await self.login()
            with patch('src.memory.get_memory',return_value=service), patch('src.admin.load_config',return_value=config):
                headers={'X-Admin-Request':'1'}
                response=await self.client.put('/admin/api/memory',json={'settings':{'max_records':100}},headers=headers)
                self.assertEqual(response.status_code,409)
                self.assertTrue(response.json()['confirm_required'])
                self.assertEqual(service.config.max_records,10000)
                response=await self.client.put('/admin/api/memory',json={'settings':{'max_records':100},'confirm_retention':True},headers=headers)
                self.assertEqual(response.status_code,200)
                self.assertEqual(service.config.max_records,100)
                response=await self.client.put('/admin/api/memory',json={'settings':{'timezone':'invalid/zone'}},headers=headers)
                self.assertEqual(response.status_code,422)
        finally:
            service.store.db.close()

    async def test_fact_review_auth_scope_and_sources(self):
        from src.memory import MemoryStore,MemoryService,MemoryConfig
        store=MemoryStore(self.root/'facts.sqlite3');service=MemoryService(store,MemoryConfig(enabled=True))
        record={'role':'user','user_id':202,'message_id':1,'content':'聚餐周六'}
        rid=store.add(1,100,record,service.config)
        store.save_facts(1,100,'2026-01-01',[{'subject':'202','subject_user_id':'202','statement_type':'self_report','relation':'聚餐','content':'聚餐周六','source_ids':[rid]}],[rid],service.config)
        try:
            headers={'X-Admin-Request':'1'};scope={'bot':'1','group':'100'}
            with patch('src.memory.get_memory',return_value=service):
                response=await self.client.post('/admin/api/memory/facts',json=scope,headers=headers)
                self.assertEqual(response.status_code,401)
                await self.login()
                response=await self.client.post('/admin/api/memory/facts',json=scope,headers=headers)
                fact=response.json()['facts'][0]
                self.assertEqual(fact['source_records'][0]['user_id'],202)
                body={**scope,'fact_id':fact['fact_id'],'action':'edit','content':'聚餐周日','status':'confirmed'}
                rejected=await self.client.put('/admin/api/memory/facts',json={**body,'group':'999'},headers=headers)
                self.assertEqual(rejected.status_code,404)
                saved=await self.client.put('/admin/api/memory/facts',json=body,headers=headers)
                self.assertEqual(saved.status_code,200)
                self.assertEqual(store.list_facts(1,100)[0]['content'],'聚餐周日')
        finally:store.db.close()

    async def test_maintenance_requires_auth_and_can_resume(self):
        from src import message_recovery as recovery
        try:
            headers={'X-Admin-Request':'1'}
            denied=await self.client.post('/admin/api/maintenance',json={'enabled':True},headers=headers)
            self.assertEqual(denied.status_code,401)
            await self.login()
            paused=await self.client.post('/admin/api/maintenance',json={'enabled':True},headers=headers)
            self.assertEqual(paused.status_code,200)
            self.assertTrue(recovery.maintenance)
            resumed=await self.client.post('/admin/api/maintenance',json={'enabled':False},headers=headers)
            self.assertEqual(resumed.status_code,200)
            self.assertFalse(recovery.maintenance)
        finally:recovery.maintenance=False
