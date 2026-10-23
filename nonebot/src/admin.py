"""可部署在电脑或服务器的轻量管理后台，不公开模型密钥。"""
import asyncio
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

import nonebot
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import yaml

from src.connection_health import qq_online
from src.utils import ReplyLimits, load_config, load_persona


class Login(BaseModel):
    password: str = Field(max_length=256)


class Persona(BaseModel):
    text: str = Field(min_length=1, max_length=64000)


class MemoryUpdate(BaseModel):
    settings: dict
    confirm_retention: bool = False


class FactScope(BaseModel):
    bot: str = Field(pattern=r'^[0-9]{1,20}$')
    group: str = Field(pattern=r'^[0-9]{1,20}$')
    offset: int = Field(default=0,ge=0,le=100000)


class FactReview(FactScope):
    fact_id: int = Field(gt=0)
    action: str = Field(pattern=r'^(edit|delete)$')
    content: str = Field(default='',max_length=500)
    status: str = Field(default='uncertain',pattern=r'^(confirmed|tentative|cancelled|uncertain)$')


def install_admin(app, root=None, password=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    password = password if password is not None else os.getenv("ADMIN_PASSWORD", "")
    sessions = {}
    attempts = []
    lock = asyncio.Lock()

    def guard(request):
        if len(password) < 16 or password.startswith("replace_with_"):
            raise HTTPException(503, "请先设置 ADMIN_PASSWORD 并重新创建容器")
        if request.method != "GET":
            origin = request.headers.get("origin")
            if (request.headers.get("x-admin-request") != "1"
                    or (origin and urlsplit(origin).netloc != request.headers.get("host"))):
                raise HTTPException(403, "请求来源无效")

    def auth(request):
        guard(request)
        now = time.monotonic()
        for token in list(sessions):
            if sessions[token] <= now:
                sessions.pop(token, None)
        if sessions.get(request.cookies.get("ds_admin", ""), 0) <= now:
            raise HTTPException(401, "请登录")

    @app.middleware("http")
    async def admin_headers(request, call_next):
        if request.url.path.startswith("/admin"):
            if request.method != "GET":
                total = 0
                chunks = []
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > 300000:
                        return JSONResponse({"detail": "请求内容过大"}, status_code=413)
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        return await call_next(request)

    def save(path, text):
        # 同目录原子替换，避免请求读取半份文件。
        temporary = path.with_name(path.name + ".admin-tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def page():
        return HTMLResponse((Path(__file__).parent / "admin.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                     "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @app.post("/admin/api/login", include_in_schema=False)
    async def login(data: Login, request: Request):
        guard(request)
        now = time.monotonic()
        attempts[:] = [timestamp for timestamp in attempts if now - timestamp < 60]
        if len(attempts) >= 5:
            raise HTTPException(429, "尝试过多，请一分钟后重试")
        if not secrets.compare_digest(data.password.encode(), password.encode()):
            attempts.append(now)
            raise HTTPException(401, "密码错误")
        token = secrets.token_urlsafe(32)
        if len(sessions) >= 64:
            sessions.pop(next(iter(sessions)))
        sessions[token] = now + 3600 * 8
        response = JSONResponse({"ok": True})
        response.set_cookie("ds_admin", token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=28800, path="/admin")
        return response

    @app.post("/admin/api/logout", include_in_schema=False)
    async def logout(request: Request):
        auth(request)
        sessions.pop(request.cookies.get("ds_admin"), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("ds_admin", path="/admin")
        return response

    @app.get("/admin/api/state", include_in_schema=False)
    async def state(request: Request):
        auth(request)
        from src.plugins import chat
        try:
            persona_text = load_persona(load_config())
            persona_error = ""
        except (ValueError, OSError):
            persona_text, persona_error = "", "人设读取失败，请重新填写并保存"
        response = JSONResponse({"online": await qq_online(nonebot.get_bots().values()),
            "pending": sum(chat.pending_requests.values()),
            "persona": persona_text, "persona_error": persona_error, "reply": dict(chat.reply_config)})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.put("/admin/api/persona", include_in_schema=False)
    async def persona(data: Persona, request: Request):
        auth(request)
        if not data.text.strip():
            raise HTTPException(422, "人设不能为空")
        config = load_config()
        name = config.get("persona_file")
        if not name:
            raise HTTPException(409, "请先配置 persona_file")
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = root / path
        async with lock:
            save(path, data.text.strip() + "\n")
        return {"ok": True, "message": "人设已保存，下一轮回答生效"}

    @app.put("/admin/api/reply", include_in_schema=False)
    async def reply(data: dict, request: Request):
        auth(request)
        # 只开放可即时生效的数字参数；不修改全局并发对象或历史容器大小。
        allowed = {"cooldown_seconds", "user_cooldown_seconds", "delay_min_seconds",
                   "delay_max_seconds", "max_pending_per_group", "request_timeout_seconds"}
        if not data or set(data) - allowed:
            raise HTTPException(422, "不支持的参数")
        from src.plugins import chat
        try:
            validated = ReplyLimits.model_validate({**chat.reply_config, **data}).model_dump()
            data = {key: validated[key] for key in data}
        except ValueError:
            raise HTTPException(422, "参数范围或延迟顺序错误") from None
        async with lock:
            path = root / "config.yaml"
            config = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            config["reply"].update(data)
            save(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
            chat.reply_config.update(data)
        return {"ok": True, "message": "回复参数已保存并生效"}

    @app.get("/admin/api/memory", include_in_schema=False)
    async def memory_state(request: Request):
        auth(request)
        from src.memory import get_memory, MemoryConfig
        from src.token_budget import mode
        service = get_memory()
        settings = service.config if service else MemoryConfig.model_validate(load_config().get("memory", {}))
        return {"settings": settings.model_dump(), "status": service.store.status() if service else
                {"records": 0, "unprocessed": 0, "summaries": 0, "facts": 0, "summary_api_calls": 0, "summary_total_tokens": 0},
                "last_error": service.last_error if service else "", "tokenizer_mode": mode()}

    @app.put("/admin/api/memory", include_in_schema=False)
    async def memory_save(data: MemoryUpdate, request: Request):
        auth(request)
        from src.memory import get_memory, MemoryConfig
        if set(data.settings) - set(MemoryConfig.model_fields):
            raise HTTPException(422, "不支持的记忆参数")
        service = get_memory()
        old = service.config if service else MemoryConfig.model_validate(load_config().get("memory", {}))
        if service and service.lock.locked():
            raise HTTPException(409, "记忆正在整理，请完成后再修改参数")
        try:
            updated = MemoryConfig.model_validate({**old.model_dump(), **data.settings})
        except (ValueError, KeyError):
            raise HTTPException(422, "记忆参数范围或时区无效") from None
        if (updated.max_records < old.max_records or updated.facts_per_group < old.facts_per_group) and not data.confirm_retention:
            count = 0
            if service:
                groups = service.store.db.execute("SELECT DISTINCT bot,grp FROM messages").fetchall()
                for group in groups:
                    count += service.store.db.execute('''SELECT count(*) FROM messages WHERE bot=? AND grp=? AND summarized=1
                        AND id NOT IN (SELECT id FROM messages WHERE bot=? AND grp=? ORDER BY id DESC LIMIT ?)''',
                        (*group, *group, updated.max_records)).fetchone()[0]
            return JSONResponse({"detail": f"降低原文保留上限：目前约 {count} 条已整理原文将可被后续清理。未整理原文不会删除；降低事实条目上限也会在后续整理时清理旧条目。", "confirm_required": True}, status_code=409)
        async with lock:
            path = root / "config.yaml"
            config = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            config["memory"] = updated.model_dump()
            save(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
            load_config()["memory"] = updated.model_dump()
            if service:
                service.config = updated
            else:
                get_memory()
        return {"ok": True, "message": "记忆参数已保存；进行中的整理使用本轮设置，保留策略后续生效"}

    @app.post("/admin/api/memory/search", include_in_schema=False)
    async def memory_search(data: dict, request: Request):
        auth(request)
        from src.memory import get_memory
        service = get_memory()
        if not service:
            return {"results": []}
        bot, group, query = (str(data.get(key, "")) for key in ("bot", "group", "query"))
        if not bot.isdecimal() or not group.isdecimal() or not 1 <= len(query.strip()) <= 200:
            raise HTTPException(422, "请填写机器人QQ、群号及1到200字符关键词")
        return {"results": service.store.search(bot, group, query)}


    @app.post('/admin/api/memory/facts', include_in_schema=False)
    async def memory_facts(data: FactScope, request: Request):
        auth(request)
        import json
        from src.memory import get_memory
        from src.memory_privacy import clean_record
        service=get_memory()
        if not service:
            return {'facts':[], 'total':0}
        facts=service.store.list_facts(data.bot,data.group)
        page=facts[data.offset:data.offset+50]
        for fact in page:
            ids=fact['source_ids']
            rows=service.store.db.execute('SELECT id,payload FROM messages WHERE bot=? AND grp=? AND id IN ('+
                ','.join('?' for _ in ids)+')',(data.bot,data.group,*ids)).fetchall()
            fact['source_records']=[{'record_id':row['id'],**clean_record(json.loads(row['payload']))} for row in rows]
        return {'facts':page,'total':len(facts)}

    @app.put('/admin/api/memory/facts', include_in_schema=False)
    async def memory_fact_review(data: FactReview, request: Request):
        auth(request)
        from src.memory import get_memory
        service=get_memory()
        if not service:
            raise HTTPException(409,'记忆未启用')
        try:
            service.store.review_fact(data.bot,data.group,data.fact_id,data.action,data.content,data.status)
        except KeyError:
            raise HTTPException(404,'事实不存在或不属于此群') from None
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
        return {'ok':True,'message':'事实已停用' if data.action=='delete' else '事实已人工修订；来源原文保留'}
