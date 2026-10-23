# NapCat 服务

NapCat 负责登录 QQ 并通过 OneBot 11 与 NoneBot 通信。完整安装从[根目录 README](../README.md)开始。

本项目使用上游 `mlikiowa/napcat-docker:latest`，不包含 QQ / NapCat 安装包，不修改上游程序。`start.sh` 先运行本项目的 JSON 配置迁移脚本，再调用上游启动器。

## 容器网络与配置迁移

默认 Compose 使用普通内部网络，NapCat 连接 `ws://nonebot:8080/onebot/v11/ws`，不使用 `network_mode: host`。这使 Linux Docker 与 Windows Docker Desktop 的 Linux 容器模式采用相同的服务地址。

`configure.py` 使用镜像中的 Python 3，负责：

- WebUI 在容器内监听 `0.0.0.0:6099`；宿主机通过 Compose 仅发布 `127.0.0.1:6099`。
- 将本项目原来的 localhost OneBot 连接迁移为内部服务名，命名为 `ds-bot`。
- 处理全局 `onebot11.json` 和已有 `onebot11_账号.json`，保留 Token 和其他协议、插件等配置。
- 合并本项目同一端点的重复连接，不改动指向其他服务的连接。
- 每个被修改的现有文件首次留下 `文件名.pre-container`，后续启动不覆盖这份原始备份。
- 已有错误 JSON 或未替换的 WebUI 密码占位值会明确报错，不静默覆盖原配置。

URL 由容器启动脚本维护；OneBot Token 仍需在 WebUI 中设置为与 `nonebot/.env` 一致。脚本不会读取或向 NapCat 传入 DeepSeek API Key。

没有 WebUI 配置时脚本会生成随机管理密码，并由上游启动日志提示；首次部署也可以从 `webui.example.json` 复制并预先填好自己的密码。容器重建不保证 QQ 自动保持登录，可能仍需扫码。

## 数据目录

| 宿主机目录 | 容器目录 | 内容 |
| --- | --- | --- |
| `napcat/data/qq` | `/app/.config/QQ` | QQ 登录状态、消息数据库和缓存 |
| `napcat/data/config` | `/app/napcat/config` | 实际 WebUI / OneBot 配置及迁移备份 |
| `napcat/data/plugins` | `/app/napcat/plugins` | NapCat 插件 |

这些目录挂载在服务器磁盘上，重建容器不自动删除，均不提交 Git。迁移脚本修改网络配置，不清理 QQ 数据；更新或迁移前仍应备份自己的数据。

## WebUI 与扫码

在运行 Docker 的电脑上直接访问 `http://127.0.0.1:6099/webui`。远程服务器需先在自己的电脑建立隧道：

```bash
ssh -N -L 6099:127.0.0.1:6099 用户名@服务器地址
```

本地端口被占用时可改为 `-L 16099:127.0.0.1:6099`，浏览器访问 `http://127.0.0.1:16099/webui`。使用 WebUI 管理密码登录，然后扫码并在手机上确认。

网络配置中编辑 **ds-bot / WebSocket Client**：

- 启用：开启。
- URL：`ws://nonebot:8080/onebot/v11/ws`。
- 消息格式：`Array`。
- Token：与 `nonebot/.env` 的 `ONEBOT_ACCESS_TOKEN` 一致。
- 上报自身消息：关闭；心跳和重连间隔可保留 `30000` 毫秒。

WebUI 管理密码和 OneBot Token 用途不同。连接成功以 NoneBot 的 `Bot ... connected` 为准；“反向服务已启动”只表示配置启用。

## 日常操作和排错

以下命令在仓库根目录执行；Linux 如需要可加 `sudo`：

```bash
docker compose ps
docker compose logs -f --tail=100 napcat
docker compose restart napcat
docker compose stop napcat
docker compose up -d
```

退出 `logs -f` 不停止容器。更新前备份数据，随后执行 `docker compose pull napcat`、`docker compose up -d napcat`；`latest` 会随上游更新，固定版本需按上游标签设置。

- **连不上 `nonebot:8080`**：检查 NoneBot 容器是否启动且健康；不要再把 URL 填成 NapCat 容器自己的 `127.0.0.1`。
- **WebSocket 403 / 被拒绝**：核对 OneBot Token；修改 NoneBot `.env` 后需重新创建容器刷新环境变量。
- **6099 无法访问**：检查容器日志、端口映射和 SSH 隧道；容器内 WebUI 要绑定 `0.0.0.0`，外部是否公开由 Compose 映射决定。
- **显示在线却不收消息**：看 NapCat 是否有“接收”日志；必要时重启 NapCat、重新扫码，再发送新消息验证。
- **收到消息但没有回答**：查看 NoneBot 的排队、冷却、DeepSeek 和 QQ 发送日志。
- **插件索引 fetch failed / 401**：应与 QQ 登录、OneBot 连接分开排查。

迁移测试只依赖 Python 标准库：在仓库根目录执行 `python -m unittest discover -s napcat/tests -v`，不会启动 QQ 或容器。

## 上游

- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [NapCat Docker 镜像及环境变量说明](https://github.com/NapNeko/NapCat-Docker)
- [NapCat 文档](https://napneko.github.io/)
- [NapCat 许可证](https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE)

NapCat 当前使用有非商业限制的自定义许可证；本项目自有代码未来选择的许可证不覆盖上游代码和 QQ 的要求。
