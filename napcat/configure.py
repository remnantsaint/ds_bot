"""将本项目的 NapCat 配置迁移到 Compose 网络，保留账号、Token 和其他连接。"""

import copy
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
from urllib.parse import urlsplit


DEFAULT_URL = "ws://nonebot:8080/onebot/v11/ws"
CLIENT_NAME = "ds-bot"


def _read(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return value


def _managed(client):
    if client.get("name") == CLIENT_NAME:
        return True
    try:
        url = urlsplit(client.get("url", ""))
        return (url.scheme in {"ws", "wss"}
                and url.hostname in {"127.0.0.1", "localhost", "nonebot"}
                and url.port == 8080 and url.path.rstrip("/") == "/onebot/v11/ws")
    except (TypeError, ValueError):
        return False


def _client_config(data, url, fallback_token=""):
    data = copy.deepcopy(data)
    network = data.setdefault("network", {})
    if not isinstance(network, dict):
        raise ValueError("OneBot network 必须是 JSON 对象")
    clients = network.get("websocketClients", [])
    if not isinstance(clients, list) or any(not isinstance(c, dict) for c in clients):
        raise ValueError("OneBot websocketClients 必须是对象列表")
    managed = [c for c in clients if _managed(c)]
    client = {
        "enable": True, "name": CLIENT_NAME, "url": url,
        "reportSelfMessage": False, "messagePostFormat": "array", "debug": False,
        "heartInterval": 30000, "reconnectInterval": 30000, "token": fallback_token,
    }
    if managed:
        client.update(managed[0])
    client.update(enable=True, name=CLIENT_NAME, url=url)
    # 去掉同一个本项目端点的重复连接，其他客户端及协议配置保留。
    network["websocketClients"] = [c for c in clients if not _managed(c)] + [client]
    return data, client.get("token", "")


def _write(path, data):
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        original = path.read_bytes()
        if json.loads(original.decode("utf-8-sig")) == data:
            return False
        mode = stat.S_IMODE(path.stat().st_mode)
        backup = path.with_name(path.name + ".pre-container")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(original)
    else:
        mode = 0o600
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(encoded)
    try:
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def configure(directory, url=DEFAULT_URL):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("ONEBOT_URL 必须是有效的 WebSocket URL")

    webui_path = directory / "webui.json"
    webui = _read(webui_path, {"prefix": "", "loginRate": 3})
    token = webui.get("token")
    if token and not isinstance(token, str):
        raise ValueError("WebUI 管理密码必须是字符串")
    if isinstance(token, str) and token.startswith("replace_with_"):
        raise ValueError("请先替换 napcat/data/config/webui.json 中的管理密码占位值")
    if not token:
        webui["token"] = secrets.token_urlsafe(32)
    webui.update(host="0.0.0.0", port=6099)

    template = Path("/app/templates/nonebot.json")
    default = _read(template, {"network": {}})
    base_path = directory / "onebot11.json"
    base, token = _client_config(_read(base_path, default), url)
    updates = [(webui_path, webui), (base_path, base)]
    for path in sorted(directory.glob("onebot11_*.json")):
        account, _ = _client_config(_read(path, {}), url, token)
        updates.append((path, account))

    # 完成所有解析和校验后才写文件，避免错误 JSON 导致部分配置被覆盖。
    return [path.name for path, data in updates if _write(path, data)]


if __name__ == "__main__":
    try:
        changed = configure(
            sys.argv[1] if len(sys.argv) > 1 else "/app/napcat/config",
            os.environ.get("ONEBOT_URL", DEFAULT_URL),
        )
    except (OSError, ValueError) as error:
        print(f"[ds-bot] NapCat 配置准备失败：{error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[ds-bot] Compose 网络配置就绪，更新 {len(changed)} 个文件；原配置保存在 .pre-container")
