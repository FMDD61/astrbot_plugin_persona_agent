"""SessionManager — per-group conversation session with multi-user name support.

Each group gets one session that accumulates messages across calls.
Messages use the OpenAI Chat API format with optional ``name`` field to
differentiate participants under the same ``user`` role.

Session lifecycle:
  1. On startup: empty. Builds naturally from incoming messages.
  2. Each inbound message: ``append("user", text, name=alias)``.
  3. Each bot reply:     ``append("assistant", reply_text)``.
  4. KG injection is appended separately by the caller before the LLM call.

Trim policy: keep system message at position 0 + last N messages.
Thread-safe.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


#: 可变块在 deque 里占位的标记（见 ``Session.append_system_update`` 的说明）。
#: 用标记占位而非直接存内容，是为了让它**不受 maxlen 驱逐** —— 代价是
#: ``len(messages)`` 会把标记算进去（本项目所有 ``sess.messages`` 访问都经
#: SessionManager，一致地跳过标记即可）。
_BLOCK_MARK = "__sys_block__"


@dataclass
class Session:
    group_id: str
    day: str = ""
    """Session day key (rotation window), empty until first use."""
    messages: deque[dict] = field(default_factory=lambda: deque(maxlen=600))
    #: 🔴 B-022（2026-09-17 生产验证）：**关系图谱头部块的冻结副本**。
    #: 为什么不每轮现算：图谱会随新成员入列增长（实测一天 8~11 次），
    #: 而它位于**会话历史之前** → 每变一次，其后 4–9 万 token 前缀缓存全废
    #: （实测 13 次变更事件吃掉全部全价 token 的 32.6%）。
    #: 冻结后：头部逐字节不变，变更改由 `_relations_delta_block` 以一条
    #: system 消息**追加到会话尾部**（S10 的设计意图，此前只做了一半）。
    #: 与 `system_prompt` 同样**不放进 deque**（deque 有 maxlen 会被挤掉）。
    relations_block: str = ""
    #: 会话开卷时的 system prompt（**不放进 deque**）。
    #: 🔴 为什么不放 deque：deque 有 maxlen，容量压力下它会被**静默挤掉**
    #: （测试抓到：max_messages=10 灌 50 条后 prompt 消失）→ 下次构建又会
    #: 重写 [0] → **破缓存**。它是会话状态，必须与容量无关。
    system_prompt: str = ""
    #: 可变块内容：``{block_id: content}``。同样刻意**不放 deque**
    #: （deque 有 maxlen，内容会被驱逐；标记不会）。
    sys_blocks: dict[int, str] = field(default_factory=dict)
    _next_block_id: int = 0

    def append(
        self,
        role: str,
        content: str,
        name: Optional[str] = None,
        *,
        message_id: str = "",
        sender_uin: str = "",
        reasoning: str = "",
    ) -> None:
        # 🔴 S15（用户 2026-09-15）：**发言人写进 content，不再用 name 字段**。
        #
        # 为什么改（有据可依，非风格偏好）：
        #   1. OpenAI 官方社区的明确结论：**`name` 字段无法用于多用户归属** ——
        #      最后那条待补全的 assistant 位置无法标注名字，且多用户场景语义模糊
        #      （见 https://community.openai.com/t/clarification-on-missing-name-field-in-responses-api-and-handling-multi-persona-multi-user-dialogues/1365804）
        #   2. 通行做法是**在内容里加发送者前缀**（`[Joe] ...`）
        #   3. 实测我们因此出现"指向错人"：把"租房白嫖"错挂到一个前 10 条里
        #      **根本没出现过**的人名上
        #
        # 实现：写入时**就把前缀固化进 content**（不改历史 = 不破坏已缓存前缀）。
        # `name` 字段**保留**（内部/工具用），但发送路径会剥掉它（见 `_public`）。
        text = str(content)
        # ⚠️ **空内容不得加前缀** —— 否则 "" 会变成 "脏："，
        # 而 `_has_content()` 判据是"content 非空白"，于是空条目不再被识别、
        # B-002 的自愈失效 → 脏条目进入引用编号基 → `[r:-N]` 整体偏移。
        # （这条由 `test_empty_entries_do_not_shift_numbering` 抓到）
        if name and role == "user" and text.strip():
            msg: dict = {"role": role, "content": f"{name}：{text}", "name": name}
        else:
            msg = {"role": role, "content": text}
        # B-001: 内部元数据。仅用于 ``[r:-N]`` 引用目标解析（编号基与 LLM 所见
        # 一致），**绝不进 LLM 请求** —— 所有对外出口都经 ``_public`` 剥离。
        if message_id:
            msg["_mid"] = str(message_id)
        if sender_uin and role == "user":
            msg["_uin"] = str(sender_uin)
        # S16：**思维链留存**（用户要求"保留完整思维链"供人工查看）。
        # 用内部键 `_reasoning` → 被 `_public` 自动剥离 → **存进 session、
        # 但不进 LLM 上下文**。理由：Context Rot（信噪比退化）+ 推理链不忠实
        # （长而详尽的推理链未必如实反映决策）。
        if reasoning and str(reasoning).strip():
            msg["_reasoning"] = str(reasoning)
        # 🔴 S14：deque 满时，**先驱逐最老的真消息**，别把可变块标记挤掉。
        # 契约（docstring 早已写明）：保留 system + 最近 N 条 ——
        # 纯靠 deque(maxlen) 会连标记一起 FIFO 掉，且在"下一个 append"时才
        # 表现为标记先消失（测试抓到：max_messages=12 灌 30 条后块没了）。
        ml = self.messages.maxlen
        if ml is not None and len(self.messages) >= ml:
            for i, m in enumerate(self.messages):
                if _BLOCK_MARK not in m:      # 找最老的真消息
                    del self.messages[i]
                    break
        self.messages.append(msg)

    def has_system_prompt(self) -> bool:
        """会话是否已经"开卷"（已记录首轮 system prompt）。"""
        return bool(self.system_prompt)

    def ensure_system_prompt(self, content: str) -> bool:
        """确保会话第一条是 system prompt。返回**是否新写入**。

        ## 为什么 system prompt 要进会话（S14，用户设计）

        旧实现每轮把 system prompt 作为 ``llm_generate(system_prompt=…)``
        独立参数传入 —— prompt 一改，**整个请求的第一个 token 就变了**，
        其后全部（实测 8 万 token 的会话）前缀缓存失效。
        实测代价：一天 8–11 次变更，单次 61k–99k 全价 token。

        新实现把它**写进会话第一条**，此后**从日志加载**：
          - 不变 → 前缀永远稳定
          - 变更 → 走 ``append_system_update()`` **追加**，不动第一条

        ``content`` 为空时不写（避免往历史里塞空 system 条目）。
        """
        c = str(content or "").strip()
        if not c:
            return False
        if self.has_system_prompt():
            return False
        self.system_prompt = c          # 存在字段里，不受 deque maxlen 驱逐
        return True

    def append_system_update(self, content: str) -> bool:
        """prompt 变更 → **追加**一个更新块。返回是否真的追加。

        追加是**必然选择**：prompt 变了不能改第一条（改了 = 破坏全量前缀
        缓存），只能往后加。用户拍板：**追加块累积保留、不清理**
        （清理 = 改历史 = 破缓存）。

        内容与"上一次生效的设定"相同时不追加 —— 否则每轮都会产生新块、
        每轮都破缓存。

        块内容存在 ``sys_blocks``，deque 里只放**标记** —— 这样容量压力下
        块不会随消息一起被驱逐（它是设定的一部分）。
        """
        c = str(content or "").strip()
        if not c or not self.has_system_prompt():
            return False
        last = self.last_system_content()
        if not last or self._norm(last) == self._norm(c):
            return False
        bid = self._next_block_id
        self._next_block_id += 1
        self.sys_blocks[bid] = c
        self.messages.append({"role": "system", _BLOCK_MARK: bid})
        return True

    @staticmethod
    def _norm(t: str) -> str:
        """比较用的归一化（只比正文，忽略声明前缀差异）。"""
        s = str(t or "")
        # 去掉"［设定更新］…以此为准："这类声明头，只比正文
        for sep in ("：", ":"):
            if "为准" in s and sep in s:
                s = s.split(sep, 1)[1]
                break
        return " ".join(s.split())

    def last_system_content(self) -> str:
        """当前**生效**的设定全文 = 首条 system，或最后一个更新块。"""
        last = self.system_prompt
        for m in self.messages:
            if _BLOCK_MARK in m:
                c = self.sys_blocks.get(m[_BLOCK_MARK])
                if c:
                    last = c
        return last

    def _materialize(self, m: dict) -> dict:
        """把 deque 里的标记还原成真正的 system 消息。"""
        bid = m.get(_BLOCK_MARK)
        return {"role": "system", "content": str(self.sys_blocks.get(bid) or "")}

    def raw_messages(self) -> list[dict]:
        """**原始条目**（含内部元数据与思维链）—— 仅供导出/排查，**不得发 LLM**。

        与 ``get_messages()`` 的区别：不做 ``_public`` 剥离。导出工具要用它，
        因为人工排查**需要看到 LLM 看不到的东西**（思维链、_mid/_uin）。
        """
        out: list[dict] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})
        for m in self.messages:
            if _BLOCK_MARK in m:
                mm = self._materialize(m)
                if mm["content"]:
                    out.append(mm)
                continue
            if _has_content(m) or m.get("_reasoning"):
                out.append(dict(m))
        return out

    def get_messages(self) -> list[dict]:
        """公开条目（剥离内部键 + 丢弃空 content）。

        B-002: 历史脏数据里出现过 ``content=""`` 的条目，原样送进 LLM 会让
        网关返回 HTTP 400 ``user message must have content``，插件的 try/except
        把异常吞掉 → 表现为「收到消息但永远不回复」的永久静默。
        这里在唯一收口点过滤：正常会话无空条目 → **零行为变化**。
        """
        out: list[dict] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})
        for m in self.messages:
            if _BLOCK_MARK in m:                 # 可变块：还原成真正的 system
                mm = self._materialize(m)
                if mm["content"]:
                    out.append(mm)
                continue
            if _has_content(m):
                out.append(_wire(m))
        return out

    def quote_entries(self) -> list[tuple[str, str, str]]:
        """引用编号基的原始三元组（与 ``get_messages()`` 逐条对齐）。

        ``get_messages()`` 会丢弃空 content 条目，这里必须用**同一过滤**，
        否则编号基又会与 LLM 所见错位 —— 那正是 B-001 的次要成因。
        """
        return [
            (str(m.get("_mid") or ""), str(m.get("_uin") or ""), str(m.get("name") or ""))
            for m in self.messages
            if _has_content(m) and _BLOCK_MARK not in m
        ]

    def recent(self, n: int = 20) -> list[dict]:
        # 与 get_messages 同过滤，保证调用方看到的内容一致（B-002）
        items = self.get_messages()
        return items[-n:] if len(items) > n else items

    def size(self) -> int:
        """可见条目数（**不含**可变块标记 —— 它们不是消息）。"""
        return sum(1 for m in self.messages if _BLOCK_MARK not in m)

    def clear(self) -> None:
        """清空会话（轮转/重置）。

        ⚠️ 必须同时清 ``system_prompt`` 与 ``sys_blocks`` —— 否则轮转后
        `has_system_prompt()` 仍为真 → **新会话不会写入最新的 prompt**，
        而是继续用上一轮的旧设定（测试抓到）。
        """
        self.messages.clear()
        self.system_prompt = ""
        self.sys_blocks.clear()
        self._next_block_id = 0
        # B-022: 轮转 = 新会话 → 冻结块也重置（下一天按当时的图谱重新冻结）
        self.relations_block = ""


# 内部元数据键前缀：绝不进 LLM 请求/对外 API
_INTERNAL_PREFIX = "_"


def _public(msg: dict) -> dict:
    """剥掉**内部元数据**（``_mid``/``_uin``/``_reasoning``）。

    ⚠️ **保留 `name`** —— 它是存储的一部分：
      - 旧格式条目（R5 之前）内容**没有前缀**，name 是其唯一发言人标识
      - 落盘时丢掉它就永久丢了（S15 就是这么丢的 1313 条的 name）
    发 LLM 时才剥 name（见 ``_wire``），因为那时发言人已在前缀里、重复标识
    反而是"指向错人"的温床。
    """
    return {k: v for k, v in msg.items() if not k.startswith(_INTERNAL_PREFIX)}


def _wire(msg: dict) -> dict:
    """**发 LLM 用**：在 `_public` 基础上再剥 `name`（发言人已在前缀里）。"""
    out = _public(msg)
    out.pop("name", None)
    return out


def _has_content(msg) -> bool:
    """B-002 判据：content 非空白。非 dict / 非 str 一律视为无内容。"""
    if not isinstance(msg, dict):
        return False
    c = msg.get("content")
    return isinstance(c, str) and bool(c.strip())



class SessionManager:
    def __init__(
        self,
        data_dir: Optional[str] = None,
        max_messages: Optional[int] = 300,
        rotation_hour: int = 2,
        tz_offset_hours: int = 8,
    ) -> None:
        self._dir = Path(data_dir) if data_dir else None
        # None => unbounded (daily rotation bounds the session anyway)
        self._max_messages = max_messages
        self._rotation_hour = int(rotation_hour)
        self._tz_offset_hours = int(tz_offset_hours)
        self._persist_step = 50
        self._persist_interval = 300.0
        self._sessions: dict[str, Session] = {}
        self._last_save: dict[str, float] = {}
        self._saved_at_count: dict[str, int] = {}
        # B-002 观测：恢复期丢弃的空 content 条目（>0 说明数据曾损坏）
        self._empty_dropped: dict[str, int] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, group_id: str) -> Session:
        if group_id not in self._sessions:
            sess = Session(group_id=group_id)
            sess.messages = deque(maxlen=self._max_messages)
            sess.day = self.day_key()
            self._sessions[group_id] = sess
        return self._sessions[group_id]

    def day_key(self, ts: float = 0.0) -> str:
        """Day-boundary key: dates roll over at `rotation_hour` local time."""
        dt = datetime.datetime.fromtimestamp(ts or time.time(), datetime.timezone.utc)
        dt = dt + datetime.timedelta(hours=self._tz_offset_hours)
        if dt.hour < self._rotation_hour:
            dt = dt - datetime.timedelta(days=1)
        return dt.strftime("%Y-%m-%d")

    def append(
        self,
        group_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        *,
        message_id: str = "",
        sender_uin: str = "",
        reasoning: str = "",
    ) -> None:
        with self._lock:
            sess = self._get_or_create(group_id)
        sess.append(
            role, content, name=name, message_id=message_id,
            sender_uin=sender_uin, reasoning=reasoning,
        )
        self._maybe_save(group_id)

    def ensure_system_prompt(self, group_id: str, content: str) -> bool:
        """确保该群的会话第一条是 system prompt。返回是否新写入。"""
        with self._lock:
            sess = self._get_or_create(group_id)
        with self._lock:
            return sess.ensure_system_prompt(content)

    def freeze_relations_block(self, group_id: str, content: str) -> str:
        """关系图谱头部块：**首次调用冻结，之后恒返回冻结值**（B-022）。

        背景（2026-09-17 生产验证）：`_assemble_base` 每轮现算活块，而该块位于
        会话历史**之前** → 图谱随新成员入列增长时，其后 4–9 万 token 前缀缓存
        全部作废。实测 13 次变更事件吃掉**全部全价 token 的 32.6%**
        （`common_prefix_chars` 恒为 793(gate)/1703(rp)，断点恰在"群友识别"块）。

        S10 的设计意图是"旧块留在前缀里不动，更新以新块追加到上下文尾部" ——
        此处补上前半句：块一旦进前缀就不再变；变更由
        `main._relations_delta_block` 以一条 system 消息追加到会话尾部。

        - 空串不冻结（未启用/无图谱时不占位，返回空串）
        - 跨重启存活（随 session 文件落盘）
        - 轮转即重置（新一天的会话前缀本来就要重建）
        """
        with self._lock:
            sess = self._get_or_create(group_id)
            if not sess.relations_block and content:
                sess.relations_block = str(content)
            return sess.relations_block

    def append_system_update(self, group_id: str, content: str) -> bool:
        """prompt 变更 → 追加更新块。返回是否真的追加。

        ⚠️ 调用方应传**已带声明头**的文本（"以此为准"），本方法不负责包装。
        """
        with self._lock:
            sess = self._get_or_create(group_id)
        with self._lock:
            return sess.append_system_update(content)

    def system_prompt_of(self, group_id: str) -> str:
        """当前生效的设定全文（供调用方判断是否需要追加）。"""
        with self._lock:
            sess = self._get_or_create(group_id)
        with self._lock:
            return sess.last_system_content()

    def sync_system_prompt(self, group_id: str, latest: str) -> str:
        """把"当前最新 prompt"同步进会话。返回动作：``init``/``update``/``noop``。

        - **首次**（会话还没开卷）→ 写入第一条（``init``）
        - **已开卷但 prompt 变了** → **追加**更新块（``update``）
          —— 不能改第一条（改了 = 破坏全量前缀缓存）
        - 未变 → ``noop``

        追加块的声明头由**本方法**包装（调用方只给正文），保证格式统一：
            ［设定更新］以下为最新设定，与此冲突之处以此为准：
            <新版全文>

        用户拍板：追加块**累积保留、不清理**。
        """
        c = str(latest or "").strip()
        if not c:
            return "noop"
        with self._lock:
            sess = self._get_or_create(group_id)
        with self._lock:
            if not sess.has_system_prompt():
                sess.ensure_system_prompt(c)
                return "init"
            if sess._norm(sess.last_system_content()) == sess._norm(c):
                return "noop"
            wrapped = ("［设定更新］以下为最新设定，与此冲突之处以此为准：\n" + c)
            return "update" if sess.append_system_update(wrapped) else "noop"

    def raw_messages(self, group_id: str) -> list[dict]:
        """原始条目（含思维链与内部元数据）—— 供导出/排查。**不得发 LLM**。"""
        with self._lock:
            sess = self._get_or_create(group_id)
        with self._lock:
            return sess.raw_messages()

    def get_contexts(self, group_id: str, *, drop_leading_system: bool = False) -> list[dict]:
        """会话历史（含首条 system prompt）。

        ``drop_leading_system=True``：**去掉开头的 system prompt**，只留历史。
        给 Gate 用 —— 它有自己的裁判 system，不该继承 RP 的人格
        （S4 的教训：把 RP 人格当 Gate 的 system 会让模型"参与聊天"而不是判断；
        实测解析失败率 0%→45%）。两者**共用同一份历史**、**各用各的 system**，
        这也正是用户要的"不再共用缓存"。
        """
        with self._lock:
            sess = self._get_or_create(group_id)
        msgs = sess.get_messages()
        if drop_leading_system:
            while msgs and msgs[0].get("role") == "system":
                msgs.pop(0)
        return msgs

    def quote_snapshot(self, group_id: str):
        """B-001: 冻结当前编号基，供 ``[r:-N]`` 在生成结束后求值。

        必须在**构建上下文之后、发起生成之前**调用。返回 ``QuoteIndex``
        （不可变），生成窗口内新到的消息不会影响它。
        """
        from .context_buffer import QuoteIndex

        with self._lock:
            sess = self._get_or_create(group_id)
        return QuoteIndex.from_entries(sess.quote_entries())

    def recent(self, group_id: str, n: int = 20) -> list[dict]:
        with self._lock:
            sess = self._get_or_create(group_id)
        return sess.recent(n)

    def size(self, group_id: str) -> int:
        with self._lock:
            sess = self._get_or_create(group_id)
        return sess.size()

    def dropped_empty(self, group_id: str) -> int:
        """B-002 观测：本会话被丢弃的空 content 条目数（自愈计数）。"""
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None:
                return 0
            return sum(1 for m in sess.messages if not _has_content(m))

    def empty_dropped_on_load(self) -> dict[str, int]:
        """B-002 观测：启动恢复期丢弃的空 content 条目数（按群）。"""
        with self._lock:
            return dict(self._empty_dropped)

    def clear(self, group_id: str) -> None:
        with self._lock:
            if group_id in self._sessions:
                self._sessions[group_id].clear()
            self._saved_at_count[group_id] = 0
        if self._dir is not None:
            try:
                for f in self._dir.glob(f"session_{self._safe_name(group_id)}_*.json"):
                    f.unlink(missing_ok=True)
                self._session_path(group_id).unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _safe_name(group_id: str) -> str:
        return group_id.replace("/", "_").replace("\\", "_")

    # ---- daily rotation (v3: rotate at rotation_hour local, keep unlimited) ----

    def rotate_if_day_changed(self, group_id: str) -> Optional[list[dict]]:
        """If the session belongs to a previous day window, archive and clear it.

        Returns the archived messages (for diary generation) or None when no
        rotation happened. Persists the old day to session_<group>_<day>.json.
        """
        key = self.day_key()
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None:
                sess = Session(group_id=group_id)
                sess.messages = deque(maxlen=self._max_messages)
                sess.day = key
                self._sessions[group_id] = sess
                return None
            if sess.day == key:
                return None
            old_msgs = list(sess.messages)
            if not old_msgs:
                sess.day = key
                return None
            old_day = sess.day
            # 🔴 B-025（2026-09-17 生产验证）：归档 payload 此前**只写 5 键**，
            # 漏了 system_prompt / sys_blocks / next_block_id（_save 写 8 键）
            # → 归档日 session 的导出/离线复核里**人格整条消失**（实测同一导出
            # 工具：归档文件命中人格标记 0 次、在写文件 1 次），可变块也还原不出。
            # 必须在 clear() **之前**取走 —— clear 会把这些字段清空。
            old_sys_prompt = sess.system_prompt
            old_blocks = {str(k): v for k, v in sess.sys_blocks.items()}
            old_nbid = sess._next_block_id
            old_rel_block = sess.relations_block
            # 轮转 = 新会话：system prompt 与可变块都要重置，
            # 否则新一天会继续用旧设定、且不写入最新的 prompt。
            sess.clear()
            sess.day = key
            self._saved_at_count[group_id] = 0
            self._last_save[group_id] = 0.0
        if self._dir is not None and old_day:
            try:
                payload = {
                    "version": 2,
                    "group_id": group_id,
                    "day": old_day,
                    "saved_at": time.time(),
                    "messages": old_msgs,
                    # B-025: 会话状态随归档一起留存（与 _save 的键保持一致）
                    "system_prompt": old_sys_prompt,
                    "sys_blocks": old_blocks,
                    "next_block_id": old_nbid,
                    "relations_block": old_rel_block,
                }
                path = self._session_path(group_id, old_day)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        return old_msgs

    def save_all(self) -> None:
        """Best-effort flush of all live sessions (terminate / pre-rotation)."""
        with self._lock:
            gids = list(self._sessions.keys())
        for gid in gids:
            self._save(gid)

    # ---- persistence (v0.2 4.2 lightweight: every ~50 msgs or ~5 min) ----

    def _session_path(self, group_id: str, day: Optional[str] = None) -> Path:
        safe = group_id.replace("/", "_").replace("\\", "_")
        if day:
            return self._dir / f"session_{safe}_{day}.json" if self._dir else Path(f"session_{safe}_{day}.json")
        return self._dir / f"session_{safe}.json" if self._dir else Path(f"session_{safe}.json")

    def _save(self, group_id: str) -> None:
        if self._dir is None:
            return
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None or not sess.messages:
                return
            payload = {
                "version": 2,
                "group_id": group_id,
                "day": sess.day or "",
                "saved_at": time.time(),
                "messages": list(sess.messages),
                # S14: system prompt + 可变块内容（都不在 deque 里）
                "system_prompt": sess.system_prompt,
                "sys_blocks": {str(k): v for k, v in sess.sys_blocks.items()},
                "next_block_id": sess._next_block_id,
                # B-022: 冻结的关系图谱块（重启后前缀仍需逐字节不变）
                "relations_block": sess.relations_block,
            }
            path = self._session_path(group_id, sess.day or None)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _maybe_save(self, group_id: str) -> None:
        now = time.time()
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None or len(sess.messages) == 0:
                return
            size = len(sess.messages)
            prev_count = self._saved_at_count.get(group_id, 0)
            prev_ts = self._last_save.get(group_id, 0.0)
            if size == prev_count:
                if (now - prev_ts) < self._persist_interval:
                    return
            else:
                if (size - prev_count) < self._persist_step and (now - prev_ts) < self._persist_interval:
                    return
            self._saved_at_count[group_id] = size
            self._last_save[group_id] = now
        self._save(group_id)

    def load_all(self) -> dict[str, int]:
        """Restore the newest per-group day session at startup.

        Picks, per group, the day file with the largest day key (the current
        session window); legacy v1 files without a day field are treated as
        current-day. Corrupt files are skipped; never raises.
        Returns {group_id: restored_message_count}.
        """
        restored: dict[str, int] = {}
        if self._dir is None:
            return restored
        skipped = 0
        best: dict[str, tuple[str, dict]] = {}
        today = self.day_key()
        for path in sorted(self._dir.glob("session_*.json")):
            if path.name.endswith(".tmp"):
                continue
            try:
                payload = json.loads(path.read_text("utf-8"))
                gid = str(payload.get("group_id", ""))
                msgs = payload.get("messages") or []
                if not gid or not isinstance(msgs, list):
                    skipped += 1
                    continue
                day = str(payload.get("day") or today)
                prev = best.get(gid)
                if prev is None or day > prev[0]:
                    best[gid] = (day, payload)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                skipped += 1
        for gid, (day, payload) in best.items():
            msgs = payload.get("messages") or []
            # B-002: 恢复时同样过滤空 content（脏数据可能来自旧版本插件）。
            # B-001: 保留 _mid/_uin 元数据（deque 整体迁移，不重建条目）。
            # 🔴 S14：**必须保留 system 条目**。
            # 旧代码写的是 `role in ("user","assistant")` → **system 被静默丢弃**
            # → system prompt 持久化失效（每次恢复都会重写 [0] → 破缓存），
            # 且 S10 的［群友识别更新］块也会丢。
            # 可变块标记（只有 __sys_block__ 键）也要保留，其内容在 sys_blocks。
            def _keep(m) -> bool:
                if not isinstance(m, dict):
                    return False
                if _BLOCK_MARK in m:
                    return True                     # 可变块标记
                if m.get("role") == "system":
                    return _has_content(m)          # system prompt
                return _has_content(m) and m.get("role") in ("user", "assistant")

            def _clean(m: dict) -> dict:
                """恢复时的条目清洗：剥内部键，但**必须保住存储性键**。

                🔴 必须保留 `name`！S15 漏了它 → `_public` 已剥 name，恢复时
                不再补回 → **save_all() 落盘时 name 永久丢失**。
                而旧格式条目（R5 之前写入的）**内容里没有前缀**，`name` 是它们
                唯一的发言人标识 → 丢 name 等于让那些条目"没有主"。

                🔴 B-031（2026-09-17 随 B-025 的往返测试发现）：可变块标记
                `__sys_block__` 也是"下划线开头的内部键" → 被 `_public` 一并剥掉，
                于是内容留在 `sys_blocks` 里却**无人引用** → **每次重启静默丢掉
                所有［设定更新］块**。标记本身是存储结构，必须原样保留。
                """
                base = _public(m) | {
                    k: m[k] for k in ("_mid", "_uin", "_reasoning", "name")
                    if m.get(k)
                }
                if _BLOCK_MARK in m:
                    base[_BLOCK_MARK] = m[_BLOCK_MARK]
                return base

            cleaned = [_clean(m) for m in msgs if _keep(m)]
            dropped = sum(
                1 for m in msgs
                if isinstance(m, dict)
                and m.get("role") in ("user", "assistant")
                and not _has_content(m)
            )
            # 恢复 system prompt + 可变块内容
            sys_prompt = str(payload.get("system_prompt") or "")
            blocks = payload.get("sys_blocks") or {}
            nbid = int(payload.get("next_block_id") or 0)
            # B-022: 冻结的关系图谱块（缺键 = 旧文件 → 留空，下轮重新冻结）
            rel_frozen = str(payload.get("relations_block") or "")
            with self._lock:
                if dropped:
                    self._empty_dropped[gid] = self._empty_dropped.get(gid, 0) + dropped
                if gid not in self._sessions:
                    sess = Session(group_id=gid)
                    sess.messages = deque(maxlen=self._max_messages)
                    self._sessions[gid] = sess
                sess = self._sessions[gid]
                if self._max_messages is not None:
                    cleaned = cleaned[-self._max_messages:]
                sess.messages = deque(cleaned, maxlen=self._max_messages)
                sess.system_prompt = sys_prompt
                # 只恢复**仍在 deque 里**的块（被淘汰的标记不再引用它们）
                live = {m[_BLOCK_MARK] for m in sess.messages if _BLOCK_MARK in m}
                sess.sys_blocks = {int(k): str(v) for k, v in blocks.items()
                                   if int(k) in live}
                sess._next_block_id = max([nbid] + [k + 1 for k in live] or [0])
                sess.relations_block = rel_frozen
                sess.day = day
                self._last_save[gid] = time.time()
                self._saved_at_count[gid] = len(sess.messages)
            restored[gid] = len(sess.messages)
        if skipped:
            import logging
            logging.getLogger("persona_session").warning(
                f"[persona_agent] session restore skipped {skipped} file(s)"
            )
        return restored

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {gid: s.size() for gid, s in self._sessions.items()}
