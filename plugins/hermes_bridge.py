"""QQ 群聊状态机 bridge v4.

链路: QQ/NapCat → OneBot11 → NoneBot2 → Hermes API server (qq-group profile)

行为目标（替代 AstrBot 群聊体验）:
- 每个白名单群维护短期消息窗口，合并连续消息/同一话题，避免一条一回复
- 轻量可配置的接话判断：@/回复机器人/明确提问优先；闲聊/纯表情/重复默认静默
- 聊天 lane 与后台任务 lane 解耦：长任务继续执行，新消息独立处理，被问进度时短状态回复
- 只向 QQ 发送最终结果，不发中间工具调用/命令/思考过程
- 结构化脱敏日志；内部错误不发群
- Hermes API 失败时，明确 @ 至少给一次简短失败提示，不无声消失
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment

# ---------------------------------------------------------------------------
# 配置（全部可用环境变量覆盖，保持旧变量兼容）
# ---------------------------------------------------------------------------
HERMES_URL = os.environ.get("HERMES_URL", "http://127.0.0.1:8650")
HERMES_KEY = os.environ["HERMES_KEY"]
HERMES_GROUP_URL = os.environ.get("HERMES_GROUP_URL", HERMES_URL)
HERMES_GROUP_KEY = os.environ.get("HERMES_GROUP_KEY", HERMES_KEY)
QQ_GROUP_WHITELIST = {
    int(x.strip()) for x in os.environ.get("QQ_GROUP_WHITELIST", "").split(",") if x.strip()
}
QQ_ALLOWED_USERS = {
    int(x.strip()) for x in os.environ.get("QQ_ALLOWED_USERS", "").split(",") if x.strip()
}
FILE_DIR = Path(os.environ.get("QQ_BRIDGE_FILE_DIR", "/tmp/hermes-qq-bridge-files"))
FILE_DIR.mkdir(parents=True, exist_ok=True)

# 附件处理：单条消息最多落盘几个附件
MAX_ATTACHMENTS = int(os.environ.get("QQ_MAX_ATTACHMENTS", "4"))
# 按扩展名判定"其实是图片的文件"（QQ 可以把图片当文件发，段类型是 file）
IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".heic", ".heif", ".tif", ".tiff", ".avif", ".jfif",
}
# 需要下载正文的消息段类型
DOWNLOAD_SEG_TYPES = ("image", "flash", "file")
# 拿不到正文、只做存在性说明的消息段类型 -> 人类可读名称
NOTE_SEG_TYPES = {
    "mface": "表情包",
    "video": "视频",
    "record": "语音",
    "forward": "合并转发消息",
    "json": "分享卡片",
    "xml": "分享卡片",
}
ATTACHMENT_SEG_TYPES = set(DOWNLOAD_SEG_TYPES) | set(NOTE_SEG_TYPES)

# 群消息窗口
GROUP_WINDOW_SIZE = int(os.environ.get("QQ_GROUP_WINDOW_SIZE", "20"))          # 保留最近 N 条
GROUP_DEBOUNCE_SECONDS = float(os.environ.get("QQ_GROUP_DEBOUNCE_SECONDS", "4.0"))  # 合并窗口
GROUP_MAX_BATCH_SECONDS = float(os.environ.get("QQ_GROUP_MAX_BATCH_SECONDS", "12.0"))
# 接话概率：普通闲聊参与率；被 @ / 明确提问时不受此限制
GROUP_SMALLTALK_PROBABILITY = float(os.environ.get("QQ_GROUP_SMALLTALK_PROBABILITY", "0.12"))
# 回复时是否对原消息点一个表情回应（NapCat 功能：更像真人互动）
QQ_EMOJI_REACT_ON_REPLY = os.environ.get("QQ_EMOJI_REACT_ON_REPLY", "1") == "1"
QQ_EMOJI_ID = os.environ.get("QQ_EMOJI_ID", "76")  # 76=赞

# 长任务进行中，被问进度时返回的状态回复
GROUP_BUSY_REPLY = os.environ.get("QQ_GROUP_BUSY_REPLY", "正忙着呢，活儿干完跟你说～")

# ---------------------------------------------------------------------------
# 日志（脱敏：不记录消息正文，只记阶段/群号/消息ID/耗时/错误类型）
# ---------------------------------------------------------------------------
LOG_DIR = Path(os.environ.get("QQ_BRIDGE_LOG_DIR", "/home/johntime/hermes-qq-bridge/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("qq_bridge")
logger.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_DIR / "bridge.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)


def log_event(stage: str, group_id, message_id=None, ms=None, error=None, note=None):
    """结构化脱敏日志。group_id/message_id 是平台 ID，非隐私内容。"""
    parts = [f"stage={stage}", f"group={group_id}"]
    if message_id:
        parts.append(f"msg={message_id}")
    if ms is not None:
        parts.append(f"ms={int(ms)}")
    if error:
        parts.append(f"error={type(error).__name__}:{str(error)[:120]}")
    if note:
        parts.append(f"note={note}")
    logger.info(" ".join(parts))


# ---------------------------------------------------------------------------
# 群状态
# ---------------------------------------------------------------------------
class GroupState:
    __slots__ = ("window", "pending", "task", "busy", "busy_task_label", "last_reply_at")

    def __init__(self):
        self.window: deque = deque(maxlen=GROUP_WINDOW_SIZE)  # (ts, user_id, text, is_at, is_reply, msg_id)
        self.pending: list[dict] = []                          # 待合并消息
        self.task: asyncio.Task | None = None                  # 当前合并处理任务
        self.busy = False                                      # 是否正在调用 Hermes
        self.busy_task_label: str | None = None                # 长任务说明
        self.last_reply_at = 0.0


_STATES: dict[str, GroupState] = {}

matcher = on_message(priority=10, block=False)


def state_for(sid: str) -> GroupState:
    st = _STATES.get(sid)
    if st is None:
        st = GroupState()
        _STATES[sid] = st
    return st


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def session_id(event: Event) -> str:
    group_id = getattr(event, "group_id", None)
    return f"qq-group-v4-{group_id}" if group_id else f"qq-dm-{event.get_user_id()}"


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


def has_direct_at(bot: Bot, event: Event) -> bool:
    """兼容多种 NapCat 的 @ 表示。"""
    self_id = str(getattr(bot, "self_id", ""))
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


def is_reply_to_bot(bot: Bot, event: Event) -> bool:
    raw = str(getattr(event, "raw_message", "") or "")
    return f"[CQ:reply,id=" in raw


_QUESTION_MARKERS = ("？", "?", "吗", "能不能", "怎么", "如何", "帮我", "请问", "帮我看", "看看", "求", "教", "报错", "为什么", "啥", "什么", "谁")
_TECH_MARKERS = ("代码", "编程", "服务器", "运维", "docker", "脚本", "报错", "日志", "配置", "bug", "部署", "安装", "python", "linux", "命令", "api", "网络")


def classify_group_message(bot: Bot, event: Event, text: str) -> tuple[str, bool]:
    """返回 (level, should_debounce)。level: direct / question / tech / smalltalk / noise

    direct  -> 明确 @ 或回复机器人，必回
    question-> 明确提问/求助，高优先
    tech    -> 技术话题，中优先
    smalltalk-> 普通闲聊，低概率
    noise   -> 纯表情/单字/无意义，默认静默
    """
    if has_direct_at(bot, event):
        return "direct", True
    if is_reply_to_bot(bot, event):
        return "direct", True
    t = text.strip()
    if not t:
        return "noise", True
    # 纯表情/短复读
    if len(t) <= 2 and not any(c.isalnum() for c in t):
        return "noise", True
    if any(x in t for x in _QUESTION_MARKERS):
        return "question", True
    if any(x in t.lower() for x in _TECH_MARKERS):
        return "tech", True
    if len(t) <= 8:
        # 短句闲聊：低参与率，仍进入 debounce 窗口（可能被后续消息激活）
        return "smalltalk", True
    return "smalltalk", True


def should_reply_for_level(level: str) -> bool:
    if level == "direct":
        return True
    if level == "question":
        return True
    if level == "tech":
        return random.random() < 0.6
    if level == "smalltalk":
        return random.random() < GROUP_SMALLTALK_PROBABILITY
    return False


def sanitize_filename(name: object) -> str:
    """取基名并去掉路径分隔符/控制字符，防止写出 FILE_DIR 之外。"""
    base = Path(str(name or "")).name
    base = re.sub(r"[\\/\x00-\x1f]", "_", base).strip()
    return base[:120] or "qq-file"


def is_image_filename(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


def attachment_kind(seg_type: str, name: str) -> str:
    """image = 可用 vision_analyze 识图；file = 需用文件读取工具打开。"""
    if seg_type in ("image", "flash"):
        return "image"
    return "image" if is_image_filename(name) else "file"


async def resolve_segment_location(bot: Bot, event: Event, seg_type: str, data: dict) -> object | None:
    """依次尝试 url / path / OneBot 取文件接口 / file 字段，拿到可下载位置。"""
    location = data.get("url") or data.get("path")
    if location:
        return location

    file_id = data.get("file_id") or data.get("file")
    if not file_id:
        return None
    group_id = getattr(event, "group_id", None)
    # 群文件优先 get_group_file_url；其余走通用 get_file
    attempts: list[tuple[str, dict]] = []
    if seg_type == "file" and group_id is not None:
        attempts.append(("get_group_file_url", {"group_id": group_id, "file_id": file_id}))
    attempts.append(("get_file", {"file_id": file_id}))
    for api, kwargs in attempts:
        try:
            result = await bot.call_api(api, **kwargs) or {}
            found = result.get("url") or result.get("file")
            if found:
                return found
        except Exception as exc:
            log_event("file_api_error", group_id, error=exc, note=api)
    return data.get("file")


async def download_segment(bot: Bot, event: Event, segment) -> Path | None:
    """把单个附件段落盘，返回本地路径；拿不到内容返回 None（已记日志）。"""
    seg_type = getattr(segment, "type", "")
    data = getattr(segment, "data", {}) or {}
    if not isinstance(data, dict):
        try:
            data = dict(data)
        except Exception:
            data = {}
    group_id = getattr(event, "group_id", None)
    name = sanitize_filename(data.get("file_name") or data.get("name") or data.get("file"))
    location = await resolve_segment_location(bot, event, seg_type, data)
    if location is None:
        log_event("attachment_no_location", group_id, note=f"type={seg_type}")
        return None

    remote_url, local_path = normalize_file_location(bot, location)
    if not remote_url and not local_path:
        log_event("attachment_unreachable", group_id, note=f"type={seg_type}")
        return None

    msg_id = getattr(event, "message_id", None)
    target = FILE_DIR / f"{session_id(event)}-{msg_id}-{name}"
    if local_path:
        target.write_bytes(local_path.read_bytes())
    else:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(remote_url)
            response.raise_for_status()
            target.write_bytes(response.content)
    return target


# 兼容旧调用名
download_file = download_segment


async def collect_attachments(bot: Bot, event: Event, max_items: int = MAX_ATTACHMENTS) -> list[dict]:
    """收集消息中的全部附件，统一成 Hermes 能直接用的描述。

    返回 [{"kind": ..., "name": str, "path": Path | None, "note": str | None}]
      kind=image : 已落盘，可用 vision_analyze 识图
      kind=file  : 已落盘，可用文件读取工具打开
      kind=note  : 拿不到正文（表情包/视频/语音/卡片，或下载失败），只告知存在
    """
    attachments: list[dict] = []
    group_id = getattr(event, "group_id", None)
    for segment in event.get_message():
        if len(attachments) >= max_items:
            break
        seg_type = getattr(segment, "type", "")
        if seg_type not in ATTACHMENT_SEG_TYPES:
            continue

        if seg_type in NOTE_SEG_TYPES:
            data = getattr(segment, "data", {}) or {}
            summary = ""
            if isinstance(data, dict):
                summary = str(data.get("summary") or "").strip().strip("[]")
            label = NOTE_SEG_TYPES[seg_type]
            attachments.append({
                "kind": "note",
                "name": summary,
                "path": None,
                "note": f"{label}:{summary}" if summary else label,
            })
            continue

        data = getattr(segment, "data", {}) or {}
        raw_name = ""
        if isinstance(data, dict):
            raw_name = str(data.get("file_name") or data.get("name") or data.get("file") or "")
        name = sanitize_filename(raw_name)
        kind = attachment_kind(seg_type, name)
        try:
            path = await download_segment(bot, event, segment)
        except Exception as exc:
            log_event("attachment_download_error", group_id, error=exc, note=f"type={seg_type}")
            path = None
        if path:
            attachments.append({"kind": kind, "name": name, "path": path, "note": None})
        else:
            # 关键：下载失败要显式告知模型，否则它会假装看到内容或谎称没有附件
            attachments.append({
                "kind": "note",
                "name": name,
                "path": None,
                "note": ("图片" if kind == "image" else f"文件 {name}") + "（下载失败，拿不到内容）",
            })
    return attachments


def attachment_lines(attachments: list[dict]) -> list[str]:
    """把附件转成给 Hermes 的指引行。"""
    lines: list[str] = []
    for att in attachments:
        kind = att.get("kind")
        path = att.get("path")
        name = att.get("name") or ""
        if kind == "image" and path:
            lines.append(
                f"[图片文件: {path} —— 需要看图内容时用 vision_analyze 工具打开这个路径；"
                "没真正看过就不要编造图里有什么。]"
            )
        elif kind == "file" and path:
            lines.append(
                f"[附件文件: {path}（原名 {name}）—— 需要内容时用文件读取工具打开这个路径。]"
            )
        else:
            lines.append(
                f"[对方发了{att.get('note') or '附件'} —— 你拿不到它的正文，不要编造内容；"
                "确实需要时请对方用文字描述或重发。]"
            )
    return lines


def attachment_placeholder(attachments: list[dict]) -> str:
    """消息没有文字时的展示文本，用于群聊窗口与分类，不承载正文。"""
    if not attachments:
        return ""
    for att in attachments:
        if att.get("kind") == "image":
            return "[图片]"
    for att in attachments:
        if att.get("kind") == "file":
            return f"[文件 {att.get('name')}]" if att.get("name") else "[文件]"
    return f"[{attachments[0].get('note') or '附件'}]"


async def send_with_reply(bot: Bot, event: Event, text: str, reply_to: int | None = None):
    """发送群消息，优先带回复引用（更接近真人）。引用失败则发纯文本。"""
    if reply_to is not None:
        try:
            await bot.send(event, Message([MessageSegment.reply(reply_to), MessageSegment.text(text)]))
            return
        except Exception:
            pass
    await bot.send(event, Message(text))


async def ask_hermes(event: Event, prompt: str, url: str, key: str, timeout: float = 240.0) -> str:
    headers = {"Authorization": f"Bearer {key}", "X-Hermes-Session-Id": session_id(event)}
    body = {"model": "hermes-agent", "stream": False, "messages": [{"role": "user", "content": prompt}]}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{url.rstrip('/')}/v1/chat/completions", headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
        content = str(data["choices"][0]["message"].get("content") or "").strip()
        log_event("hermes_ok", getattr(event, "group_id", None), ms=(time.monotonic() - start) * 1000)
        return content
    except Exception as exc:
        log_event("hermes_error", getattr(event, "group_id", None), ms=(time.monotonic() - start) * 1000, error=exc)
        raise


def cleanup_reply(text: str) -> str:
    """最终回复后处理：去掉客服式开头/AI声明/多余换行。"""
    t = text.strip()
    for pat in (
        "你好呀，我是可洛喵，有什么需要帮忙的",
        "你好呀，我是可洛喵，有什么需要帮忙吗",
        "你好呀，我是可洛喵",
        "你好，我是可洛喵",
        "你好呀，我是",
        "你好，我是",
        "我是可洛喵",
        "我是AI",
        "我是 AI",
        "作为一个AI",
        "作为AI",
        "有什么需要帮忙的",
        "很高兴为你服务",
        "很乐意为你",
    ):
        if t.startswith(pat):
            t = t[len(pat):].lstrip("，。！!～~ ")
    t = t.strip(" \n")
    return t


# ---------------------------------------------------------------------------
# 群合并处理 worker
# ---------------------------------------------------------------------------
async def group_worker(bot: Bot, event: Event, sid: str):
    st = state_for(sid)
    await asyncio.sleep(GROUP_DEBOUNCE_SECONDS)
    batch_start = time.monotonic()
    while True:
        # 继续收集直到窗口结束或达到最大等待
        await asyncio.sleep(min(1.0, GROUP_MAX_BATCH_SECONDS - (time.monotonic() - batch_start)))
        items = st.pending
        st.pending = []
        if not items:
            break
        group_id = getattr(event, "group_id", None)
        st.busy = True
        try:
            # 组装上下文：最近窗口 + 本轮合并消息
            window_lines = []
            for ts, uid, txt, is_at, is_reply, mid in list(st.window)[-10:]:
                prefix = "→" if is_at else " "
                window_lines.append(f"{prefix} [{uid}] {txt}")
            batch_lines = []
            for it in items:
                base = f"{'→' if it['is_at'] else ' '} [{it['user_id']}] {it['text']}"
                for img in it.get("images", []):
                    base += f"\n   [图片文件: {img} —— 如果你需要了解图片内容，用 vision_analyze 工具查看；没看过就不要假装知道图片里有什么。]"
                batch_lines.append(base)
            prompt = (
                "[群聊语境]\n你是可洛喵，一个混在群里、会编程会运维的普通网友。说话要像真人：口语化、短句、"
                "带一点随意的语气，偶尔开个玩笑，不要用'哦'、'呢'、'~'之类的AI尾缀，不要列一二三点，"
                "不要自我介绍，不要解释自己是谁，不用客服腔。被 @ 或明确求助时认真、直接地回答。"
                "群里没有图片或你没有真正看过图片时，绝不要说自己看到了什么图片。\n"
                "[最近群聊]\n" + "\n".join(window_lines[-6:]) + "\n"
                "[本轮要处理的消息]\n" + "\n".join(batch_lines) + "\n"
                "[要求]\n"
                "1. 只有值得回应时才回复：@你、明确提问、技术求助必回；普通闲聊看情况，可以不回。\n"
                "2. 多条消息同属一个话题就合并成一个回复，不要逐条对应。\n"
                "3. 普通回复控制在60字内，语气自然随意；确实需要详细解释或动手时再展开。\n"
                "4. 直接给要说的话，不要 JSON、标题、列表式总结，不要复述工具过程。\n"
                "5. 如果不需要回应，只输出单个字符：¬\n"
                "[回复]"
            )
            answer = await ask_hermes(event, prompt, HERMES_GROUP_URL, HERMES_GROUP_KEY)
            answer = cleanup_reply(answer)
            if answer and answer != "¬":
                reply_to = items[0].get("msg_id") if items else None
                await send_with_reply(bot, event, answer[:800], reply_to=reply_to)
                # 表情回应：对直接 @ 的消息点个赞（失败不影响主回复）
                if QQ_EMOJI_REACT_ON_REPLY and reply_to is not None and any(it.get("is_at") for it in items):
                    try:
                        await bot.call_api("set_msg_emoji_like", message_id=str(reply_to), emoji_id=QQ_EMOJI_ID)
                    except Exception:
                        pass
                st.last_reply_at = time.time()
                log_event("reply_sent", group_id)
        except Exception as exc:
            # 内部错误不发群，但明确 @ 至少给一次简短提示
            log_event("worker_error", getattr(event, "group_id", None), error=exc)
            if any(it["is_at"] for it in items):
                try:
                    await bot.send(event, Message("刚没听清/接口抖了一下，你再说一遍？"))
                except Exception:
                    pass
        finally:
            st.busy = False
            st.task = None
        # 循环：合并期间可能又有新消息进入 pending
        if not st.pending:
            break
        batch_start = time.monotonic()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
@matcher.handle()
async def handle(bot: Bot, event: Event):
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        if QQ_GROUP_WHITELIST and group_id not in QQ_GROUP_WHITELIST:
            return
        text = event.get_plaintext().strip()
        has_file = any(getattr(s, "type", "") == "file" for s in event.get_message())
        has_image = any(getattr(s, "type", "") == "image" for s in event.get_message())
        direct_at = has_direct_at(bot, event)
        if not text and not has_file and not has_image and not direct_at:
            return
        sid = session_id(event)
        st = state_for(sid)
        level, _ = classify_group_message(bot, event, text or ("[图片]" if has_image else "[文件]"))
        msg_id = getattr(event, "message_id", None)
        st.window.append((time.time(), event.get_user_id(), text or ("[图片]" if has_image else "[文件]"), direct_at, is_reply_to_bot(bot, event), msg_id))

        item = {"user_id": event.get_user_id(), "text": text or ("[图片]" if has_image else "[文件]"), "is_at": direct_at, "msg_id": msg_id}
        if has_image:
            item["images"] = await collect_images(bot, event)

        # 长任务进行中：被问进度给短状态；普通消息先并入待处理
        if st.busy:
            if direct_at or level in ("question", "tech"):
                await bot.send(event, Message(GROUP_BUSY_REPLY))
                log_event("busy_reply", group_id)
            else:
                st.pending.append(item)
            return

        # 不值得回：并入窗口但不触发回复
        if not should_reply_for_level(level) and not st.pending:
            log_event("skip", group_id, message_id=msg_id, note=f"level={level}")
            return

        st.pending.append(item)
        if st.task is None or st.task.done():
            st.task = asyncio.create_task(group_worker(bot, event, sid))
            log_event("worker_start", group_id, message_id=msg_id, note=f"level={level}")
        return

    # 私聊
    if QQ_ALLOWED_USERS and int(event.get_user_id()) not in QQ_ALLOWED_USERS:
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
        except Exception as exc:
            log_event("file_download_error", group_id=None, error=exc)
    if not parts:
        return
    try:
        answer = await ask_hermes(event, "\n".join(parts), HERMES_URL, HERMES_KEY)
        answer = cleanup_reply(answer)
    except Exception as exc:
        log_event("dm_error", group_id=None, error=exc)
        return
    if answer:
        await bot.send(event, Message(answer[:800]))
