"""容器与 nb-cli 共用的启动入口。"""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter


nonebot.init()
nonebot.get_driver().register_adapter(Adapter)
nonebot.load_from_toml("pyproject.toml")
app = nonebot.get_app()


@app.get("/healthz", include_in_schema=False)
async def healthz():
    # 这里只检查 HTTP 服务就绪；QQ 连接由 NapCat 建立，不能作为启动依赖。
    return {"status": "ok"}


if __name__ == "__main__":
    nonebot.run()
