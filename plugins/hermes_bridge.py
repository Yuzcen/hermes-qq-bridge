import asyncio
import json
import os
import random
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Event, Message

HERMES_URL = os.environ.get("HERMES_URL", "http://127.0.0.1:8650")
HERMES_KEY = os.environ["HERMES_KEY"]
HERMES_GROUP_URL = os.environ.get("HERMES_GROUP_URL", HERMES_URL)
HERMES_GROUP_KEY = os.environ.get("HERMES_GROUP_KEY", HERMES_KEY)
QQ_GROUP_WHITELIST = {
    int(x.strip()) for x in os.environ.get("QQ_GROUP_WHITELIST", "").split(",") if x.strip()
}
FILE_DIR = Path(os.environ.get("QQ_BRIDGE_FILE_DIR", "/tmp/hermes-qq-bridge-files"))
FILE_DIR.mkdir(parents=True, exist_ok=True)

# 群聊合并：短暂等待后把连续消息合成一轮，避免逐条机械回复
GROUP_DEBOUNCE_SECONDS = 4.0
_pending: dict[str, list[str]] = {}
_pending_tasks: dict[str, asyncio.Task] = {}
_group_busy: dict[str, bool] = {}


def onebot_api_root(bot: Bot) -> str:
    raw = os.environ.get("ONEBOT_V11_API_ROOTS", "")
    try:
        roots = json.loads(raw) if raw else {}
        if isinstance(roots, dict):
            root = roots.get(str(getattr(bot, "self_id", ""))) or roots.get("default")
            if root:
                return str(root).rstrip("/")
    except (json.JSONDecodeError, TypeError):
        pass
    return os.environ.get("ONEBOT_API_ROOT", "http://127.0.0.1:18801").rstrip("/")


def normalize_file_location(bot: Bot, location: object) -> tuple[str | None, Path | None]:
    if location is None:
        return None, None
    value = str(location).strip()
    if not value:
        return None, None
    if value.startswith("file://"):
        local = Path(urlparse(value).path)
        return None, local if local.is_file() else None
    local = Path(value)
    if local.is_file():
        return None, local
    if value.startswith(("http://", "https://")):
        return value, None
    return urljoin(onebot_api_root(bot) + "/", value.lstrip("/")), None


matcher = on_message(priority=10, block=False)


def session_id(event: Event) -> str:
    group_id = getattr(event, "group_id", None)
    return f"qq-group-v3-{group_id}" if group_id else f"qq-dm-{event.get_user_id()}"


def has_direct_at(bot: Bot, event: Event) -> bool:
    self_id = str(getattr(bot, "self_id", ""))
    # OneBot v11 不同 NapCat 版本可能把 @ 段的 data 暴露成属性、字典，
    # 或只保留 CQ/raw 表示；三种形式都识别，避免被静默过滤。
    for seg in event.get_message():
        seg_type = getattr(seg, "type", "")
        data = getattr(seg, "data", {}) or {}
        if not isinstance(data, dict):
            try:
                data = dict(data)
            except Exception:
                data = {}
        if seg_type == "at" and str(data.get("qq", "")) == self_id:
            return True
        if seg_type == "at" and self_id in str(seg):
            return True
    raw = str(getattr(event, "raw_message", "") or "")
    return (
        f"[CQ:at,qq={self_id}]" in raw
        or f"qq={self_id}" in raw
        or f"@{self_id}" in raw
    )


def group_needs_reply(bot: Bot, event: Event, text: str) -> bool:
    if has_direct_at(bot, event):
        return True
    # 不再每句话都接：只响应明确的问题、求助或技术话题。
    markers = ("？", "?", "吗", "能不能", "怎么", "如何", "帮我", "请问", "代码", "编程", "服务器", "运维", "bot", "机器人")
    return any(x in text.lower() for x in markers)


async def download_file(bot: Bot, event: Event, segment) -> Path | None:
    data = dict(segment.data)
    name = Path(str(data.get("name") or data.get("file") or "qq-file")).name or "qq-file"
    location = data.get("url")
    if not location and data.get("file_id"):
        result = await bot.call_api("get_file", file_id=data["file_id"])
        location = result.get("url") or result.get("file")
    location = location or data.get("file")
    if not location:
        return None
    remote_url, local_path = normalize_file_location(bot, location)
    if not remote_url and not local_path:
        return None
    target = FILE_DIR / f"{session_id(event)}-{name}"
    if local_path:
        target.write_bytes(local_path.read_bytes())
    else:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(remote_url)
            response.raise_for_status()
            target.write_bytes(response.content)
    return target


async def ask_hermes(event: Event, prompt: str, url: str, key: str) -> str:
    headers = {"Authorization": f"Bearer {key}", "X-Hermes-Session-Id": session_id(event)}
    body = {"model": "hermes-agent", "stream": False, "messages": [{"role": "user", "content": prompt}]}
    async with httpx.AsyncClient(timeout=1800) as client:
        response = await client.post(f"{url.rstrip('/')}/v1/chat/completions", headers=headers, json=body)
        response.raise_for_status()
        data = response.json()
    return str(data["choices"][0]["message"].get("content") or "").strip()


async def group_worker(bot: Bot, event: Event, sid: str):
    await asyncio.sleep(GROUP_DEBOUNCE_SECONDS)
    while True:
        messages = _pending.pop(sid, [])
        if not messages:
            break
        _group_busy[sid] = True
        try:
            prompt = (
                "[群聊合并回复]\n"
                "你在 QQ 群里像一个真实群友一样说话。只在确实有帮助或被直接问到时回应；不要自我介绍，"
                "不要说‘作为AI’，不要逐条复述。下面是短时间内收到的连续消息，请综合后只给一个自然、精炼的最终回复；"
                "如果只是闲聊或没有必要回答，输出空字符串。普通回复控制在50字以内，只有明确要求才展开。\n\n"
                "[连续消息]\n" + "\n".join(messages)
            )
            answer = await ask_hermes(event, prompt, HERMES_GROUP_URL, HERMES_GROUP_KEY)
            if answer:
                await bot.send(event, Message(answer[:500]))
        except Exception:
            pass
        finally:
            _group_busy[sid] = False
    _pending_tasks.pop(sid, None)


@matcher.handle()
async def handle(bot: Bot, event: Event):
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        if QQ_GROUP_WHITELIST and group_id not in QQ_GROUP_WHITELIST:
            return
        text = event.get_plaintext().strip()
        has_file = any(getattr(s, "type", "") == "file" for s in event.get_message())
        direct_at = has_direct_at(bot, event)
        # 纯 @ 也是明确召唤，不能在触发判断前被空文本过滤掉。
        if not text and not has_file and not direct_at:
            return
        sid = session_id(event)
        dbg = f"[DBG group] gid={group_id} text={text!r} at={direct_at} need={group_needs_reply(bot, event, text)} busy={_group_busy.get(sid)} pending={sid in _pending}"
        print(dbg, flush=True)
        # 长任务进行中：被直接问到就回一句"正在干活"，不排队干等，也不打断后台任务
        if _group_busy.get(sid):
            if direct_at or group_needs_reply(bot, event, text):
                await bot.send(event, Message("正忙着呢，活儿干完跟你说～"))
            return
        if not direct_at and not group_needs_reply(bot, event, text) and sid not in _pending:
            print("[DBG group] SKIP (no trigger)", flush=True)
            return
        _pending.setdefault(sid, []).append(text or "[收到一条文件消息]")
        if sid not in _pending_tasks:
            _pending_tasks[sid] = asyncio.create_task(group_worker(bot, event, sid))
            print(f"[DBG group] worker started for {sid}", flush=True)
        return

    allowed_users = {int(x) for x in os.environ.get("QQ_ALLOWED_USERS", "").split(",") if x.strip()}
    if allowed_users and int(event.get_user_id()) not in allowed_users:
        return
    parts = []
    text = event.get_plaintext().strip()
    if text:
        parts.append(text)
    for segment in event.get_message():
        if getattr(segment, "type", "") != "file":
            continue
        try:
            path = await download_file(bot, event, segment)
            if path:
                parts.append(f"[QQ收到文件：{path}。请使用文件工具读取它。]")
        except Exception:
            parts.append("[QQ文件下载失败]")
    if not parts:
        return
    try:
        answer = await ask_hermes(event, "\n".join(parts), HERMES_URL, HERMES_KEY)
    except Exception:
        return
    if answer:
        await bot.send(event, Message(answer[:500]))
