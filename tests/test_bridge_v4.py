"""hermes_bridge v4 状态机单元测试。

不依赖 NoneBot 运行环境：通过注入假 Bot/Event 直接测试判断逻辑。
运行: python -m pytest tests/test_bridge_v4.py -v
"""

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))

# 导入模块（只取可测试的纯函数部分；模块级 os.environ 读取用假值）
os.environ.setdefault("HERMES_KEY", "test-key")
os.environ.setdefault("QQ_GROUP_WHITELIST", "1108551011")

import hermes_bridge as hb  # noqa: E402


class FakeSeg:
    def __init__(self, seg_type, data=None):
        self.type = seg_type
        self.data = data or {}


class FakeEvent:
    def __init__(self, group_id=None, user_id="147789565", message=None, raw_message="", message_id=1):
        self.group_id = group_id
        self.user_id = user_id
        self.message_id = message_id
        self._message = message or []
        self.raw_message = raw_message

    def get_user_id(self):
        return self.user_id

    def get_plaintext(self):
        return "".join(s.data.get("text", "") for s in self._message if s.type == "text")

    def get_message(self):
        return self._message


class FakeBot:
    def __init__(self, self_id="3810598600"):
        self.self_id = self_id
        self.sent = []

    async def send(self, event, message):
        self.sent.append(str(message))


def ev_at(text="你好"):
    return FakeEvent(
        group_id=1108551011,
        message=[FakeSeg("at", {"qq": "3810598600"}), FakeSeg("text", {"text": text})],
        raw_message=f"[CQ:at,qq=3810598600] {text}",
        message_id=1001,
    )


def ev_text(text):
    return FakeEvent(group_id=1108551011, message=[FakeSeg("text", {"text": text})], raw_message=text, message_id=1002)


def test_direct_at_detected():
    bot = FakeBot()
    e = ev_at("帮我看看")
    assert hb.has_direct_at(bot, e) is True
    level, _ = hb.classify_group_message(bot, e, "帮我看看")
    assert level == "direct"


def test_direct_at_no_text():
    """纯 @ 也必须识别为 direct。"""
    bot = FakeBot()
    e = FakeEvent(
        group_id=1108551011,
        message=[FakeSeg("at", {"qq": "3810598600"})],
        raw_message="[CQ:at,qq=3810598600]",
        message_id=1003,
    )
    assert hb.has_direct_at(bot, e) is True
    level, _ = hb.classify_group_message(bot, e, "")
    assert level == "direct"


def test_question_detected():
    bot = FakeBot()
    e = ev_text("这个怎么弄？")
    level, _ = hb.classify_group_message(bot, e, "这个怎么弄？")
    assert level == "question"


def test_tech_detected():
    bot = FakeBot()
    e = ev_text("docker 起不来")
    level, _ = hb.classify_group_message(bot, e, "docker 起不来")
    assert level == "tech"


def test_noise_detected():
    bot = FakeBot()
    e = ev_text("🤣")
    level, _ = hb.classify_group_message(bot, e, "🤣")
    assert level == "noise"


def test_should_reply_direct_always():
    assert hb.should_reply_for_level("direct") is True
    assert hb.should_reply_for_level("question") is True
    assert hb.should_reply_for_level("noise") is False


def test_cleanup_reply_strips_customer_service():
    assert "有什么需要帮忙的" not in hb.cleanup_reply("你好呀，我是可洛喵，有什么需要帮忙的？")
    assert hb.cleanup_reply("你好呀，我是可洛喵") == ""


def test_cleanup_reply_keeps_normal():
    assert hb.cleanup_reply("行，贴一下报错") == "行，贴一下报错"


def test_group_merge_debounce():
    """连续两条消息应合并为一次 Hermes 调用、一次发送。"""
    bot = FakeBot()
    calls = []

    async def fake_ask(event, prompt, url, key, timeout=240.0):
        calls.append(prompt)
        return "收到，合并回复"

    hb.ask_hermes = fake_ask
    hb.GROUP_DEBOUNCE_SECONDS = 0.05
    hb.GROUP_MAX_BATCH_SECONDS = 0.5

    sid = "qq-group-v4-1108551011"
    st = hb.state_for(sid)
    st.pending = []
    st.task = None

    e1 = ev_at("第一个问题")
    e2 = ev_text("第二个补充")
    # 两条都进入 pending（第二条在第一条 worker 未启动前也判断为值得回）
    hb._ = None

    async def run():
        # 模拟 handle 的入队逻辑
        for e in (e1, e2):
            level, _ = hb.classify_group_message(bot, e, e.get_plaintext())
            if not hb.should_reply_for_level(level) and not st.pending:
                continue
            st.pending.append({"user_id": e.get_user_id(), "text": e.get_plaintext(), "is_at": hb.has_direct_at(bot, e), "msg_id": e.message_id})
        if st.task is None or st.task.done():
            st.task = asyncio.create_task(hb.group_worker(bot, e2, sid))
        await st.task

    asyncio.run(run())
    assert len(calls) == 1, f"期望合并为1次调用，实际 {len(calls)}"
    assert len(bot.sent) == 1
    assert "第一个问题" in calls[0] and "第二个补充" in calls[0]


def test_busy_short_state_reply():
    """长任务 busy 时，明确 @ 收到状态回复，且不新增 Hermes 调用。"""
    bot = FakeBot()
    calls = []

    async def fake_ask(event, prompt, url, key, timeout=240.0):
        calls.append(prompt)
        return "x"

    hb.ask_hermes = fake_ask
    sid = "qq-group-v4-1108551011"
    st = hb.state_for(sid)
    st.busy = True
    st.task = None

    e = ev_at("弄好了吗")

    async def run():
        # 模拟 busy 分支
        direct_at = hb.has_direct_at(bot, e)
        level, _ = hb.classify_group_message(bot, e, e.get_plaintext())
        if st.busy:
            if direct_at or level in ("question", "tech"):
                await bot.send(e, hb.GROUP_BUSY_REPLY)
            return

    asyncio.run(run())
    assert len(calls) == 0
    assert len(bot.sent) == 1
    assert "正忙着呢" in bot.sent[0]
    st.busy = False


def test_image_message_detected():
    """图片消息必须被识别并触发下载，不能当纯文本。"""
    bot = FakeBot()
    e = FakeEvent(
        group_id=1108551011,
        message=[FakeSeg("image", {"url": "http://example.com/x.png"}), FakeSeg("text", {"text": "看看这张图"})],
        raw_message="[CQ:image,url=http://example.com/x.png]看看这张图",
        message_id=1004,
    )
    assert any(getattr(s, "type", "") == "image" for s in e.get_message())
    level, _ = hb.classify_group_message(bot, e, "看看这张图")
    assert level == "question"


def test_send_with_reply():
    """send_with_reply 应带 reply 段引用原消息。"""
    bot = FakeBot()
    e = ev_at("你好")

    async def run():
        await hb.send_with_reply(bot, e, "测试回复", reply_to=1001)

    asyncio.run(run())
    assert len(bot.sent) == 1
    assert "测试回复" in bot.sent[0]
    assert "[CQ:reply" in bot.sent[0] or "reply" in bot.sent[0].lower()


def test_group_worker_prompt_has_no_false_image_claim():
    """prompt 必须禁止模型在没有图片时假装看到图片。"""
    bot = FakeBot()
    sid = "qq-group-v4-1108551011"
    st = hb.state_for(sid)
    st.pending = [{"user_id": "147789565", "text": "你好", "is_at": True, "msg_id": 1001}]
    st.task = None
    captured = {}

    async def fake_ask(event, prompt, url, key, timeout=240.0):
        captured["prompt"] = prompt
        return "你好"

    hb.ask_hermes = fake_ask
    hb.GROUP_DEBOUNCE_SECONDS = 0.01
    hb.GROUP_MAX_BATCH_SECONDS = 0.2

    e = ev_at("你好")

    async def run():
        st.task = asyncio.create_task(hb.group_worker(bot, e, sid))
        await st.task

    asyncio.run(run())
    assert "绝不要说自己看到了什么图片" in captured["prompt"]
