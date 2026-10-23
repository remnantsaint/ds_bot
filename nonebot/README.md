# NoneBot 服务

首次部署见[根目录 README](../README.md)。默认由 Docker Compose 运行，本目录负责聊天业务，不负责 QQ 登录。

## 容器运行

- `Dockerfile` 安装 Python 3.11 和 `requirements.txt`，用 `python bot.py` 直接启动，便于接收容器停止信号。
- `.dockerignore` 只允许依赖声明、启动入口和源码进入构建上下文；实际 `.env`、配置、人设和缓存不会进入镜像。
- Compose 将宿主机 `nonebot/` 只读挂载到 `/app`，因此保留现有相对人设路径，不需公开个人配置。
- Compose 的 `env_file` 从 `nonebot/.env` 注入变量，并覆盖 `HOST=0.0.0.0`、`PORT=8080`。宿主机不发布这个端口。
- `/healthz` 只返回 `{"status":"ok"}`，不暴露配置；它检查 HTTP 就绪，不检查 QQ 是否在线，避免与 NapCat 的启动依赖形成循环。

在仓库根目录查看和操作：

```bash
docker compose logs -f --tail=100 nonebot
docker compose restart nonebot
```

代码、人设和 YAML 在进程中缓存，修改后重启。修改 `.env` 后用 `docker compose up -d --force-recreate nonebot` 重新创建容器以刷新注入的变量；修改依赖后用 `docker compose up -d --build nonebot`。

## 宿主机开发入口

仍可在 Python 3.11 / Conda / venv 环境中安装和启动：

```bash
python -m pip install -r nonebot/requirements.txt
```

在仓库根目录执行 `./run.sh` 或 `./run.sh --reload`；也可进入本目录执行 `nb run` 或 `python bot.py`。Windows PowerShell 可直接在本目录运行 `nb run`，不需要 Bash 脚本。`tzdata` 已显式声明，供 Windows 和精简镜像使用 IANA 时区。

注意：默认 Compose 中 NapCat 的 URL 为 `ws://nonebot:8080/onebot/v11/ws`，目标是容器服务。宿主机开发实例不会自动接收它的消息。使用本机实例时，需要另行让配套 NapCat 连接到该实例；不要同时运行多个处理同一批群消息的机器人实例。

`run.sh` 仍只做切换目录和前台启动，不创建 Docker 容器，也不负责后台运行或开机自启。

## 环境变量与人设

`nonebot/.env` 保存 `DEEPSEEK_API_KEY`、`ONEBOT_ACCESS_TOKEN`、驱动和环境选择；开发 `.env.dev` 和生产 `.env.prod` 保存对应环境配置。系统环境变量优先于文件中的同名值，所以容器可使用内部监听地址而不改宿主机配置。

容器日志级别以 `.env` 注入的 `LOG_LEVEL` 为准；如果它已设置，`.env.dev` 中的同名项不会覆盖系统环境变量。修改容器环境变量后需重新创建容器。

公开示例使用 `persona_file: "persona.txt"`。将 `persona.example.txt` 复制为 `persona.txt`，按需编辑；既有部署可以继续使用原来的相对文件名。相对路径以 `config.yaml` 为基准，UTF-8 和 BOM 均支持。宿主机绝对路径在容器内不一定可用。

人设文件不存在、为空或编码错误时会明确报错。读取文件仅方便管理，每次模型请求仍会携带完整人设，不会因此自动减少输入 Token。

## 回复策略与上下文

```yaml
reply:
  keywords: ["小鲸"]
  probability: 0.1
  reply_on_at: true
  cooldown_seconds: 300
  max_history: 20
```

- @ 或适配器认定发给机器人的消息按同群接收顺序排队，不受自动回复冷却限制。
- 未 @ 时可由关键词或概率触发，受冷却限制；忙碌时只记录背景。
- 冷却从成功发送回答后计算；失败不消耗冷却时间。
- `max_history` 计入群友消息及机器人成功回复，每群分别保存；同群成员共享本群背景。
- 当前提问、提问者和原消息 ID 明确传入，历史回答记录自己的回复对象；晚于当前提问的消息不混入该轮背景。
- 当前仍为内存中的短期历史。重启、重建容器或重载后清空，尚无数据库、每日摘要或七天检索。

`deepseek.thinking` 与 `search.enabled` 分别控制思考和自动搜索。搜索结果只用于本次工具循环，思考字段不会发到 QQ 或保存成长期记忆。搜索次数、模型参数及人设都由 `config.yaml` 管理。

## 开发位置和测试

| 路径 | 作用 |
| --- | --- |
| `bot.py` | 初始化、注册 OneBot、加载插件及健康检查 |
| `src/plugins/chat.py` | 消息触发、排队、按群上下文和发送回答 |
| `src/deepseek_client.py` | 模型请求、回复对象及搜索工具循环 |
| `src/search.py` | 搜索工具和 HTTP 接口 |
| `src/utils.py` | YAML 与人设加载、缓存 |
| `src/vision.py` | 当前为空，尚未实现识图 |

依赖范围见 `pyproject.toml`，已验证的直接依赖版本见 `requirements.txt`，调整时同步维护。

在仓库根目录运行容器内离线测试：

```bash
docker compose run --rm --no-deps nonebot python -m unittest discover -s tests -v
```

或在本目录的开发环境执行 `python -m unittest discover -s tests -v`。测试覆盖上下文隔离、同群排队、失败释放、搜索边界、人设加载和启动健康接口，不请求真实 API 或发送 QQ 消息。
