# NoneBot 服务

首次部署见[根目录 README](../README.md)。默认由 Docker Compose 运行，本目录负责聊天业务，不负责 QQ 登录。

## 容器运行

WebSocket 断开由 NapCat 的重连机制恢复（默认 30 秒）；QQ 被踢下线或会话失效仍需人工扫码，不自动重启 NapCat，以免丢失登录状态。

- `Dockerfile` 安装 Python 3.11 和 `requirements.txt`，用 `python bot.py` 直接启动，便于接收容器停止信号。
- `.dockerignore` 只允许依赖声明、启动入口和源码进入构建上下文；实际 `.env`、配置、人设和缓存不会进入镜像。
- Compose 将宿主机 `nonebot/` 可写挂载到 `/app`，以保存 SQLite 记忆与收件箱数据，直接读取其中的 `config.yaml` 和默认 `persona.txt`；密钥保存在不提交的 `.env` 中。
- Compose 的 `env_file` 从 `nonebot/.env` 注入变量，并覆盖 `HOST=0.0.0.0`、`PORT=8080`。NoneBot 的 8080 端口仅供容器内部通信，不映射到宿主机。
- `/healthz` 只返回 `{"status":"ok"}`，不暴露配置；它检查 HTTP 就绪，不检查 QQ 是否在线，避免与 NapCat 的启动依赖形成循环。

在仓库根目录查看和操作：

```bash
docker compose logs -f --tail=100 nonebot
docker compose restart nonebot
```

代码与直接编辑的 YAML 修改后需重启；人设文件每次生成回答前重新读取，保存后下一轮生效，正在生成的回答不受影响。文件为空、丢失或编码错误时该轮失败，不使用空人设或旧人设，修复文件后自动恢复。修改 `.env` 后用 `docker compose up -d --force-recreate nonebot` 重新创建容器以刷新注入的变量；修改依赖后用 `docker compose up -d --build nonebot`。

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

仓库直接提供 `config.yaml`、默认 `persona.txt` 和仅含日志级别的 `.env.dev` / `.env.prod`，不需要复制这些文件。密钥仍从 `.env.example` 复制到 Git 忽略的 `.env` 后填写，不要写进公共配置。

程序只读取 `config.yaml`，默认使用 `persona_file: "persona.txt"`。模型、回复和搜索策略直接在 `config.yaml` 中修改，人设直接在 `persona.txt` 中修改；两个文件都作为默认配置提交到 Git。

相对人设路径以 `config.yaml` 所在目录为基准，UTF-8 和 BOM 均支持。宿主机绝对路径在容器内不一定可用。

人设文件不存在、为空或编码错误时会明确报错。读取文件仅方便管理，每次模型请求仍会携带完整人设，不会因此自动减少输入 Token。

人设即时更新由 `src/utils.py` 的 `load_persona()` 和 `src/plugins/chat.py` 配合完成，每轮调用回答模型前读取一次，同轮工具调用保持相同人设。
`tests/test_config.py` 覆盖不清配置缓存时的文件更新、空文件、文件丢失和恢复；`tests/test_chat.py` 验证每轮使用新人设，读取失败不调用模型。
配置中的 `persona_file` 路径和旧式内联 `persona` 仍随配置缓存，修改它们需要重启。文件内容即时生效不会清空历史。

## 回复策略与上下文

同一群的同一用户连续 @ 默认间隔至少 3 秒，间隔内的重复请求忽略（`reply.user_cooldown_seconds`，0 关闭）。
历史背景连同当前请求的序列化字符预算为 `reply.max_context_chars`（默认 16000）；超出时丢弃最旧历史。当前请求自身（含身份和提及信息）超过预算则拒绝生成。
队列满时仍保留普通消息背景；被拒绝的请求不启动用户冷却。
此预算不包含人设和本轮工具结果，也不等同于 Token 预算。
新增参数、历史条数、概率、冷却及延迟范围在启动时校验，错误配置拒绝加载。

请求保护：每群最多保留 5 个处理中或等待中的请求，全局最多同时执行 3 个模型回答。

```yaml
reply:
  keywords: ["小鲸"]
  probability: 0
  reply_on_at: true
  cooldown_seconds: 300
  max_history: 20
  delay_min_seconds: 1
  delay_max_seconds: 4
```

- @ 或适配器认定发给机器人的消息按同群接收顺序排队，不受自动回复冷却限制。
- 生成完成后，发送前随机等待 `delay_min_seconds` 到 `delay_max_seconds` 秒（默认 1～4 秒）；两项均为 `0` 可关闭。等待仍在同群锁内，不合并后续提问；总响应时间还包含模型和工具耗时。
- 未 @ 时由关键词触发，受冷却限制；概率随机回复默认关闭（`probability: 0`），忙碌时只记录背景。发送前的随机延迟是独立设置，不受此开关影响。
- 正文按原顺序保留 `@昵称（QQ:号码）`，并在 `mentioned_users` 中提供被提及的成员信息；机器人自身的 @ 只用于触发，`@全体成员` 单独标记。
- @ 对象的名称优先用本群已有发言中的名片/昵称，缺失时在同群队列内查询成员信息；查询失败保留 QQ 号，不跨群取名，也不会因查名片让后来的提问插队。
- 冷却从成功发送回答后计算；失败不消耗冷却时间。
- `max_history` 计入群友消息及机器人成功回复，每群分别保存；同群成员共享本群背景。
- 当前提问、提问者和原消息 ID 明确传入，历史回答记录自己的回复对象；晚于当前提问的消息不混入该轮背景。
- 短期缓存重启后清空；启用持久记忆时，原文和摘要保存在SQLite中，可在重启后继续参与回答，参见根目录 MEMORY.md。

`deepseek.thinking`、`search.enabled` 和 `fetch.enabled` 分别控制思考、自动搜索和网页抓取。工具结果只用于本次工具循环，思考字段不会发到 QQ 或保存成长期记忆。工具次数、模型参数及人设由 `config.yaml` 管理。

## 网页抓取和工具异常处理

`web_fetch` 通过 DeepSeek 官方 `POST /v1/tools/fetch` 读取公开网页，复用现有 API Key；宿主机不直接访问目标 URL，不需要添加 Python 依赖。配置如下：

```yaml
fetch:
  enabled: true
  max_fetches: 2
  timeout_seconds: 30
  content_chars: 6000
```

搜索和抓取的次数独立限制，失败尝试也占用本轮次数，避免持续重试。搜索用完后若还有抓取次数，模型仍可读取已找到的网页。关闭抓取时设 `fetch.enabled: false`；旧配置没有 `fetch` 段时默认关闭。

只允许无账号密码的公开 HTTP/HTTPS URL；不支持登录、验证码或执行网页脚本。正文超过上限会标记 `truncated`，抓取失败会将明确的错误交给模型，不能据此编造缺失的角色记录。实际请求还受 DeepSeek 和目标站点的访问政策限制。

正常工具调用通过 `tool_calls` 执行。正文里出现 DSML / invoke 标记时最多重试一次，不执行文本中的指令，也不把异常正文写进上下文；再次失败则由聊天处理器给出失败提示。工具调用的 `reasoning_content` 仍在本轮原样保留。

查看日志里的 `[fetch] 网页抓取完成` / `[fetch] 网页抓取不可用` 和 `[tools] 检测到正文工具标记`，可以区分成功抓取、失败和格式异常恢复。

## 新闻与气温图片

`src/plugins/briefing.py` 是独立图片插件，匹配优先级为 5，早于聊天插件的 10。仅接受群内完整关键词；无需 @，也支持 @机器人。命中后阻止继续进入聊天模型，不消耗聊天冷却，不保存为聊天记忆。

```yaml
briefing:
  enabled: true
  news_commands: ["今日新闻"]
  weather_commands: ["今日天气", "今日气温"]
  weather_cache_seconds: 3600
  failure_cache_seconds: 60
  timeout_seconds: 15
```

`enabled: false` 可关闭。新闻从 `https://60s.viki.moe/v2/60s` 返回的日期和图片地址获取；天气提取 `https://www.nmc.cn/publish/temperature/hight/24hour.html` 中的当前最高气温 24 小时预报产品，不写死日期链接。北京时间当天无图则提示尚未更新；网络、图片下载或发送失败也会给出提示。不会自动退回昨天的图。

新闻缓存当天有效；天气按 `weather_cache_seconds` 刷新；失败按 `failure_cache_seconds` 短暂缓存以减少重复请求。所有缓存跨北京时间零点失效，各群和天气两个别名共用缓存，并发请求合并取图。缓存仅占内存，不写磁盘，重启即清空。

天气文字标注图片发布时次。晚间发布的未来 24 小时预报可能覆盖次日，**预报起止时段以图内标题为准**。图片通过 NoneBot 下载为字节后交给 OneBot 发送，NapCat 无需访问新闻或气象来源。图片大小限制为 8 MiB；来源域名和跳转均受限制。

日志 `[briefing] ... 获取当日图片成功` / `命中当日图片缓存` / `图片发送成功` 可区分取图、缓存和发送。来源限流、不可用或页面改版时可能失败；这些请求不经过 DeepSeek，不产生模型 Token 用量。

## 开发位置和测试

| 路径 | 作用 |
| --- | --- |
| `bot.py` | 初始化、注册 OneBot、加载插件及健康检查 |
| `src/plugins/chat.py` | 消息触发、排队、按群上下文和发送回答 |
| `src/deepseek_client.py` | 模型请求、回复对象及搜索工具循环 |
| `src/search.py` | 搜索工具和 HTTP 接口 |
| `src/fetch.py` | 网页抓取、URL 校验和正文长度限制 |
| `src/utils.py` | YAML 与人设加载、缓存 |
| `src/vision.py` | 当前为空，尚未实现识图 |

依赖范围见 `pyproject.toml`，已验证的直接依赖版本见 `requirements.txt`，调整时同步维护。

在仓库根目录运行容器内离线测试：

```bash
docker compose run --rm --no-deps -e MEMORY_ENABLED=false nonebot python -m unittest discover -s tests -v
```

或在本目录的开发环境执行 `python -m unittest discover -s tests -v`。测试覆盖上下文隔离、同群排队、失败释放、搜索和抓取边界、DSML 异常恢复、人设加载和启动健康接口，不请求真实 API 或发送 QQ 消息。

聊天请求不再设置个人间隔、每群数量、全局并发、消息字符数或应用层总超时限制；同群仍按顺序回复。保留模型/工具接口自身超时，以及独立的 Token 和记忆预算。已移除 QQ 定时在线监测与 `/readyz`；`/healthz` 仍用于容器健康检查。
