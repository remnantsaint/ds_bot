"""主动检查 OneBot API，区分连接存在与 QQ 实际在线。"""

import asyncio


async def connection_state(bots) -> str:
    async def check(bot):
        try:
            status = await asyncio.wait_for(bot.get_status(), timeout=5)
            if not isinstance(status, dict):
                return "invalid_status"
            if status.get("online") is not True:
                return "logged_out"
            return "online" if status.get("good") is True else "unhealthy"
        except asyncio.TimeoutError:
            return "timeout"
        except Exception:
            return "api_error"

    results = await asyncio.gather(*(check(bot) for bot in bots))
    if not results:
        return "disconnected"
    return next((state for state in results if state != "online"), "online")


async def qq_online(bots) -> bool:
    return await connection_state(bots) == "online"


async def watch_connections(get_bots, logger):
    previous = None
    # 给 NapCat 首次连接留出宽限时间。
    await asyncio.sleep(30)
    while True:
        state = await connection_state(get_bots().values())
        if state != previous:
            if state == "online":
                logger.info("[connection] QQ 在线检查通过，连接已恢复")
            else:
                reasons = {
                    "disconnected": "WebSocket 未连接，等待 NapCat 自动重连",
                    "logged_out": "QQ 未登录，请在 NapCat 检查登录或重新扫码",
                    "timeout": "QQ 状态接口超时，请检查 NapCat 是否卡住",
                    "api_error": "QQ 状态接口调用失败，请检查 NapCat 日志",
                    "invalid_status": "QQ 状态接口返回格式异常",
                    "unhealthy": "QQ 已登录但服务状态异常，请检查 NapCat",
                }
                logger.warning(f"[connection] {reasons[state]}")
            previous = state
        await asyncio.sleep(30)
