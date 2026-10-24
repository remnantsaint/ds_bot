"""在独立进程内验证容器入口、环境变量优先级和健康接口，不启动监听端口。"""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_loads_adapter_plugin_and_health_endpoint(self):
        code = '''
import asyncio
import httpx
import nonebot
import bot
from src.deepseek_client import deepseek_client

assert str(nonebot.get_driver().config.host) == "0.0.0.0"
assert nonebot.get_driver().config.port == 8080
assert "OneBot V11" in nonebot.get_adapters()
assert any(p.name == "chat" for p in nonebot.get_loaded_plugins())

async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bot.app), base_url="http://test") as client:
        result = await client.get("/healthz")
        assert result.status_code == 200
        assert result.json() == {"status": "ok"}
        result = await client.get("/readyz")
        assert result.status_code == 503
        assert result.json() == {"qq_connected": False}
    await deepseek_client.close()
asyncio.run(check())
print("BOOTSTRAP_HEALTH_OK")
'''
        environment = dict(os.environ, HOST="0.0.0.0", PORT="8080", LOG_LEVEL="WARNING",
                           DEEPSEEK_API_KEY="test-only", ONEBOT_ACCESS_TOKEN="test-only")
        result = subprocess.run([sys.executable, "-c", code],
                                cwd=Path(__file__).resolve().parents[1],
                                env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BOOTSTRAP_HEALTH_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
