# ds_bot

基于 **NoneBot2 + NapCat + DeepSeek API** 的 QQ 群聊天机器人，支持人设、按群隔离的短期上下文，以及模型自动决定的联网搜索。

**默认部署为两个 Docker 容器**：NoneBot 处理聊天业务，NapCat 登录 QQ。Compose 通过服务名连接它们，宿主机无需安装 Python 或 Conda。当前在 Linux 上验证，Windows 可使用 Docker Desktop 的 Linux 容器模式；本项目不需要 Docker Desktop 的 host networking 功能。

```text
QQ 群 ↔ NapCat 容器 ↔ ws://nonebot:8080/onebot/v11/ws ↔ NoneBot 容器 → DeepSeek
            ↑
   127.0.0.1:6099（WebUI）
```

## 当前功能与边界

- @ 机器人：同群请求按收到的顺序排队回复，引用各自原消息；没有人为随机等待。
- 未 @：按关键词或概率触发，受冷却限制；忙碌时只记录为本群背景。
- 不同群分别记录上下文，同群成员共享本群背景，并标记发言人、消息和回复对象。
- 默认保留每群最近 20 条记录，群友消息和机器人成功回复都计数；重启 NoneBot 后清空。
- 模型自行决定是否搜索，默认每次回复最多搜索一次；程序不追加固定来源尾注。
- 尚未实现识图、知识库、持久化记忆、每日摘要或七天历史检索。

## 目录

```text
ds_bot/
├── compose.yaml                 # 两个服务、内部网络、健康检查及本地 WebUI 端口
├── run.sh                       # 可选的宿主机 NoneBot 开发入口
├── nonebot/
│   ├── Dockerfile               # Python 运行环境和依赖
│   ├── .dockerignore            # 构建只包含业务代码，不打包密钥和人设
│   ├── bot.py                   # 容器 / nb-cli 共用启动入口，提供 /healthz
│   ├── requirements.txt         # 已验证的直接依赖版本
│   ├── pyproject.toml           # 插件、适配器及依赖范围
│   ├── .env.example             # API Key、OneBot Token 等环境变量示例
│   ├── .env.dev / .env.prod      # 开发 / 生产日志级别默认值
│   ├── config.yaml              # 模型、回复和搜索的公共默认配置
│   ├── persona.txt              # 默认人设，按需直接编辑
│   ├── src/                     # 业务代码
│   └── tests/                   # 离线测试
└── napcat/
    ├── start.sh                 # 准备网络配置后调用上游启动器
    ├── configure.py             # 迁移本项目的连接地址，保留 Token 和其他配置
    ├── webui.example.json       # 容器内 WebUI 配置示例
    ├── tests/                   # 配置迁移测试
    └── data/                    # QQ / 实际配置 / 插件，忽略上传
```

更多细节见 [NoneBot 说明](nonebot/README.md)和 [NapCat 说明](napcat/README.md)。

## 首次部署

### 1. 准备 Docker

Linux 安装 Docker Engine 和 Docker Compose v2；Windows 安装 Docker Desktop 并使用 Linux 容器模式。先克隆或下载仓库并进入根目录，确认：

```bash
docker compose version
```

Linux 用户如果没有 Docker 访问权限，可以在下面的 Docker 命令前加 `sudo`；Windows 的 Docker Desktop 不用加。

### 2. 创建本地配置

Linux / WSL 的 Bash：

```bash
cp -n nonebot/.env.example nonebot/.env
mkdir -p napcat/data/qq napcat/data/config napcat/data/plugins
cp -n napcat/webui.example.json napcat/data/config/webui.json
```

Windows PowerShell：

```powershell
if (!(Test-Path nonebot/.env)) { Copy-Item nonebot/.env.example nonebot/.env }
New-Item -ItemType Directory -Force -Path napcat/data/qq,napcat/data/config,napcat/data/plugins | Out-Null
if (!(Test-Path napcat/data/config/webui.json)) { Copy-Item napcat/webui.example.json napcat/data/config/webui.json }
```

普通配置 `nonebot/config.yaml`、默认人设 `nonebot/persona.txt` 和 `.env.dev` / `.env.prod` 已随仓库提供，无需复制。只有填写密钥的 `.env` 和 WebUI 管理密码配置保留模板形式。上面的命令不覆盖已有文件。已有部署请同时阅读下方“从原来部署迁移”。

### 3. 填写配置

用编辑器填写，不能保留 `replace_with_...` 占位值：

| 配置 | 位置 | 用途 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | `nonebot/.env` | 自己申请的 DeepSeek API Key |
| `ONEBOT_ACCESS_TOKEN` | `nonebot/.env` | 自己设置的随机连接密码，稍后在 NapCat 里填相同值 |
| `token` | `napcat/data/config/webui.json` | 自己设置的 NapCat WebUI 管理密码，与 OneBot Token 用途不同 |

直接编辑 `nonebot/config.yaml` 设置模型、回复和搜索策略，编辑 `nonebot/persona.txt` 设置人设。程序只读取 `config.yaml`，默认 `persona_file: persona.txt`；这两个文件都是公开默认文件，会参与 Git 版本管理。API Key 和连接密码仍放在 Git 忽略的 `.env` 中。

`persona_file` 相对于 `config.yaml` 所在目录解析；宿主机绝对路径不一定存在于容器中。

Compose 会把 NoneBot 的监听地址覆盖为容器内的 `0.0.0.0:8080`，但不将 8080 发布到宿主机；`.env` 的 `127.0.0.1` 仍可留给宿主机开发使用。NapCat WebUI 在容器内监听 `0.0.0.0:6099`，只映射到宿主机的 `127.0.0.1:6099`。

### 4. 构建并启动两个容器

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100 nonebot napcat
```

首次构建需要下载 Python 镜像、依赖和 NapCat 镜像。NoneBot 通过健康检查后，NapCat 才启动。`logs -f` 只查看日志，按 `Ctrl+C` 退出查看不会停止容器。

镜像默认从官方 PyPI 安装依赖，并使用构建缓存、下载超时和重试。网络不畅时可自行选择可信的 HTTPS 软件源，例如先执行 `docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple nonebot`，再执行 `docker compose up -d`。该参数只影响构建时的 Python 依赖下载，不改变 DeepSeek API 地址。

健康检查通过表示 NoneBot 的 HTTP 服务已就绪，尚不代表 QQ 已登录或群聊已连通。

### 5. 打开 WebUI 并登录 QQ

在运行 Docker 的本机打开 `http://127.0.0.1:6099/webui`。如果部署在远程 Linux 服务器，先在自己电脑建立 SSH 隧道：

```bash
ssh -N -L 6099:127.0.0.1:6099 用户名@服务器地址
```

保持隧道运行，浏览器访问同一地址。使用自己设置的 WebUI 管理密码登录，再用机器人 QQ 扫码并在手机上确认。

在“网络配置”中找到脚本维护的 **ds-bot / WebSocket Client**：

| 项目 | 值 |
| --- | --- |
| 启用 | 开启 |
| URL | `ws://nonebot:8080/onebot/v11/ws` |
| 消息格式 | `Array` |
| Token | 与 `nonebot/.env` 的 `ONEBOT_ACCESS_TOKEN` 相同 |
| 上报自身消息 | 关闭 |

首次配置需要手动填写连接 Token；脚本不会把 API Key 传给 NapCat。保存后，NoneBot 出现 `Bot ... connected`，再用另一个 QQ 账号在群里真正 @ 机器人发文字测试。

`nonebot` 是容器之间的服务名，宿主机浏览器不能用它代替 WebUI 的 `127.0.0.1`。无需开放公网 8080 或 6099。

## 从原来部署迁移

旧版使用 `.example` 复制普通配置；现在仓库直接提供 `config.yaml`、`persona.txt`、`.env.dev` 和 `.env.prod`。更新前先备份自己修改过的配置和人设，拉取后按需保留自己的设置。程序只读取 `nonebot/config.yaml`，不再使用本地配置优先级；默认人设文件为 `persona.txt`。密钥仍放在不提交的 `.env` 中。

原来“宿主机 `nb run` + host 网络 NapCat”的数据目录不变。先备份自己的配置和 `napcat/data/`，再停止原终端或 tmux 中的 NoneBot，然后执行：

```bash
docker compose up -d --build
```

启动脚本会在新的 NapCat 容器中完成以下调整：

- WebUI 改为容器内 `0.0.0.0:6099`，通过本地端口映射访问。
- 本项目原来指向 `127.0.0.1:8080` 的 OneBot 连接改为 `nonebot:8080`，并命名为 `ds-bot`。
- 保留已有 Token、其他协议连接和插件配置；同一个本项目端点的重复连接会合并。
- 首次改动每个 JSON 时，在原目录留下一份 `文件名.pre-container` 备份；反复启动不会覆盖原始备份。

QQ 登录数据不删除，但重建容器后可能需要重新扫码。NoneBot 原有内存上下文在切换后清空。迁移脚本针对本项目的默认端口与连接，自己额外定制的连接需自行核对。

## 日常使用和开发

在仓库根目录执行：

```bash
# 查看输出，退出查看不停止服务
docker compose logs -f --tail=100 nonebot

# 代码、YAML 或人设修改后重启 NoneBot
docker compose restart nonebot

# requirements.txt 改动后，重新构建并创建 NoneBot 容器
docker compose up -d --build nonebot

# 停止两个服务
docker compose stop

# 再次启动
docker compose up -d
```

`.env` 的环境变量由 Compose 创建容器时读取；修改 `.env` 后请使用 `docker compose up -d --force-recreate nonebot`，不能只依赖 `restart` 刷新环境变量。

两个容器都有 `restart: unless-stopped`。Docker 服务启动时会按该策略恢复未被手动停止的容器，主机 Docker 服务也需启用自启动。QQ 会话仍可能要求重新登录。

默认运行时只读挂载 `nonebot/`，因此在服务器或 VS Code 里修改代码后重启即可生效；依赖变更需要重建镜像。需要在前台查看并控制两个服务，可以用 `docker compose up`，按 `Ctrl+C` 停止。

`run.sh` 和 `nb run` 仍保留作宿主机开发入口，需要自己安装 Python 依赖。默认 NapCat 会连接容器内的 NoneBot；它不会因为你另开一个宿主机 `nb run` 就切换目标，详见 [NoneBot 说明](nonebot/README.md)。

## 搜索、记忆与测试

搜索使用 [DeepSeek 官方 Search API](https://api-docs.deepseek.com/guides/search/)，复用 API Key，按平台规则计费。思考开关和搜索开关独立；每次模型请求仍携带完整人设及本群短期上下文。

本次容器化没有加入长期记忆或每日摘要任务，也不会自动产生摘要模型调用。20 条限制控制当前短期上下文；七天原始记录、每日摘要和旧事检索仍是后续功能。

构建后可运行 NoneBot 离线测试：

```bash
docker compose run --rm --no-deps nonebot python -m unittest discover -s tests -v
```

配置迁移脚本只依赖 Python 标准库，在开发机可执行 `python -m unittest discover -s napcat/tests -v`。两套测试不请求真实 API、不发送 QQ 消息；QQ 端到端连通性需扫码后发消息验证。

## 上传 GitHub

代码、Dockerfile、Compose、入口脚本、README、测试、默认 `config.yaml` / `persona.txt`、仅含日志级别的 `.env.dev` / `.env.prod` 和密钥模板可以提交。真实 `.env`、NapCat 数据、日志和缓存不提交；运行配置和人设也不打入 NoneBot 镜像。新增私有文件需自行维护 `.gitignore`，提交前查看 `git diff --cached`，不要将密钥放进参与版本管理的普通配置中。

DEBUG 日志可能包含环境变量，分享前遮盖密钥。本仓库尚未选择自有代码的许可证。NapCat / QQ 从上游镜像获取，遵循上游自己的许可证及限制，参见 [第三方说明](napcat/README.md#上游)。
