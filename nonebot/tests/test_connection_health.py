import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.connection_health import qq_online, watch_connections


class ConnectionHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_and_logged_out_are_not_ready(self):
        self.assertFalse(await qq_online([]))
        self.assertFalse(await qq_online([SimpleNamespace(get_status=AsyncMock(return_value={"online": False, "good": True}))]))

    async def test_api_failure_is_not_ready(self):
        self.assertFalse(await qq_online([SimpleNamespace(get_status=AsyncMock(side_effect=RuntimeError))]))

    async def test_online_is_ready(self):
        self.assertTrue(await qq_online([SimpleNamespace(get_status=AsyncMock(return_value={"online": True, "good": True}))]))

    async def test_alerts_only_on_state_change(self):
        logger = Mock()
        with patch("src.connection_health.connection_state", new_callable=AsyncMock, side_effect=["disconnected", "disconnected", "online"]), \
             patch("src.connection_health.asyncio.sleep", new_callable=AsyncMock, side_effect=[None, None, None, asyncio.CancelledError]):
            with self.assertRaises(asyncio.CancelledError):
                await watch_connections(lambda: {}, logger)
        logger.warning.assert_called_once()
        logger.info.assert_called_once()
