"""主动检查 OneBot API，区分连接存在与 QQ 实际在线。"""

import asyncio


async def qq_online(bots) -> bool:
    async def check(bot):
        try:
            status = await asyncio.wait_for(bot.get_status(), timeout=5)
            return isinstance(status, dict) and status.get("online") is True and status.get("good") is True
        except Exception:
            return False

    results = await asyncio.gather(*(check(bot) for bot in bots))
    return bool(results) and all(results)


async def watch_connections(get_bots, logger):
    previous = None
    while True:
        online = await qq_online(get_bots().values())
        if online != previous:
            if online:
                logger.info("[connection] QQ 在线检查通过，连接已恢复")
            else:
                logger.warning("[connection] QQ 离线或状态接口不可用；WebSocket 将自动重连，若持续离线请检查 NapCat 登录状态")
            previous = online
        await asyncio.sleep(30)
