"""protocol_compat — 协议端归一化层（纯 stdlib，可离线单测，无 astrbot 依赖）。

存在理由（2026-09-10 迁移 NapCat → LLBot v8 时立）：

1. **入站 poke 形状在不同协议端/不同 AstrBot 版本间不一致**，插件不应把某一种
   写法当作真理。历史教训：`main.py::on_other` 曾要求
   `notice_type == "notify" and sub_type == "poke"`，同时又挂
   `EventMessageType.OTHER_MESSAGE` 过滤器 —— 而 AstrBot v4.27.4 的
   `_convert_handle_notice_event()` 会把**带 group_id 的通知归为 GROUP_MESSAGE**
   （`OTHER_MESSAGE` 不可达），于是该处理器**永不触发**，`poke.enabled=1` 也静默无效。
   → 结论：**判定集中在插件侧**，不依赖宿主的事件类型映射；过滤器用 ALL。

2. **出站 poke 通道**：LLBot v8 的出站消息段转换表
   （`src/onebot11/transform/message/outgoing.ts`）**没有 `poke` 分支且没有 `default`**
   → `{"type":"poke",...}` 消息段被**静默丢弃**（无日志、无报错）。
   正确通道是 action `group_poke`（LLBot `src/onebot11/action/llbot/group/GroupPoke.ts`，
   NapCat 亦有同名 action）。本模块把「走哪条通道」变成可测数据。

边界（刻意不做的事）：
  - **不决定「发不发」**：是否回戳由 `services/poke.py` 的冷却/配额/未知成员/严肃语境
    等策略决定；`poke.enabled=0` 仍然全静默（验收清单第 8 条）。
  - 不做网络/IO，不 import astrbot：纯函数，可在开发机离线单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

# 一级通知类型取值（poke 可能被放在 notice_type 或 sub_type 上）
_POKE_NOTICE_TYPES = ("poke", "notify")
_POKE_SUB_TYPES = ("poke", "poke_recall")

# 出站通道标识（顺序即回退顺序，见 poke_channels）
CHANNEL_ACTION = "action"
CHANNEL_SEGMENT = "segment"


def _raw_get(raw: Any, key: str) -> Any:
    """dict / Mapping / aiocqhttp.Event 通用的取值（缺失或类型不符 -> None）。"""
    if raw is None:
        return None
    getter = getattr(raw, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            return None
    return None


def _uid(value: Any) -> str:
    """QQ 号归一化为非空字符串；None / 0 / "0" / 空串 / 布尔 一律视为无效。

    OneBot v11 的 QQ 号是数字或数字字符串；`0` 在多个实现里是「未设置」的默认值
    （例：LLBot `OB11PokeEvent.target_id = 0`），必须当无效处理，否则会去戳 0 号。
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int) and value == 0:
        return ""
    text = str(value).strip()
    if not text or text == "0":
        return ""
    return text


def _as_int_or_str(value: str) -> Union[int, str]:
    """action 参数：能转 int 就转（LLBot schema 接受 Number|String），否则原样。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


@dataclass(frozen=True)
class PokeNotice:
    """归一化后的戳一戳通知。

    poker  = 发起戳的人（OneBot `user_id`）
    target = 被戳的人（OneBot `target_id`）——调用方据此判定是否戳的是自己
    group_id 为空串表示私聊戳
    """

    poker: str
    target: str
    group_id: str
    sub_type: str
    notice_type: str

    @property
    def is_recall(self) -> bool:
        """戳一戳撤回（`sub_type=poke_recall`）——必须忽略，不得当成一次真戳。"""
        return self.sub_type == "poke_recall"

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)


def normalize_poke(raw: Any) -> Optional[PokeNotice]:
    """把任意实现的 poke 通知 dict 归一化为 PokeNotice；不是 poke 则返回 None。

    接受的形状（保守优先：认不出就不动作，宁可不错戳）：
      a. `{notice_type: "notify", sub_type: "poke", target_id, user_id[, group_id]}`
         —— OneBot v11 标准 / NapCat / **LLBot v8 实测同形**
         （LLBot `event/notice/OB11PokeEvent.ts`: notice_type='notify', sub_type='poke', target_id）
      b. `{notice_type: "poke", ...}` —— 部分实现把 poke 提为一级通知类型
      c. `{sub_type: "poke", ...}`（缺 notice_type）—— AstrBot 自身的 poke 组件判定
         就只看 `sub_type` + `target_id`，不看 `notice_type`，故此处同样放宽

    明确拒绝：
      - 非 poke 通知（group_recall / group_ban / group_admin / notify/title …）：
        它们的 sub_type 取值为 normal/anonymous/ban/lift_ban/set/unset 等，不与 poke 冲突
      - 带 `notice_type` 但取值不在 {notify, poke} 内的（防误判）
      - 缺 `target_id` 或 `user_id` 无效（无法确定戳谁/谁戳）→ 返回 None
    """
    sub_type = _uid(_raw_get(raw, "sub_type"))
    notice_type = _uid(_raw_get(raw, "notice_type"))

    # 一级类型校验：notice_type 存在时必须是 notify/poke
    if notice_type and notice_type not in _POKE_NOTICE_TYPES:
        return None
    # 必须是 poke 子类型，或 notice_type 直接为 poke
    if sub_type not in _POKE_SUB_TYPES and notice_type != "poke":
        return None

    poker = _uid(_raw_get(raw, "user_id"))
    target = _uid(_raw_get(raw, "target_id"))
    if not poker or not target:
        return None

    return PokeNotice(
        poker=poker,
        target=target,
        group_id=_uid(_raw_get(raw, "group_id")),
        sub_type=sub_type or "poke",
        notice_type=notice_type or "notify",
    )


def poke_action_call(notice: PokeNotice) -> Optional[tuple[str, dict]]:
    """回戳应调用的 (action, payload)。

    群戳 -> `group_poke{group_id, user_id}`；私戳 -> `friend_poke{user_id}`
    （两者均存在于 LLBot 与 NapCat）。

    注意：**是否回戳的策略由调用方决定**（本插件当前只回群戳）。
    """
    if not notice.poker:
        return None
    if notice.is_group:
        return (
            "group_poke",
            {
                "group_id": _as_int_or_str(notice.group_id),
                "user_id": _as_int_or_str(notice.poker),
            },
        )
    return ("friend_poke", {"user_id": _as_int_or_str(notice.poker)})


@dataclass(frozen=True)
class ProtocolCapabilities:
    """协议端出站能力（供通道回退决策，不决定是否发言）。"""

    name: str
    poke_action: bool
    poke_message_segment: bool
    reply_segment: bool
    image_segment: bool
    note: str = ""


# --- 已知协议端（2026-09-10 源码核实）---------------------------------------
#
# LLBot v8：outgoing.ts 的 switch 支持 text/at/reply/face/mface/image/video/
#   record/json/dice/rps/contact/shake/music/forward/node/file —— **独缺 poke**，
#   且无 default 分支 → poke 段静默丢弃。action 侧有 group_poke/friend_poke/send_poke。
# NapCat：poke 段的 ob11ToRawConverter 是 `async () => undefined` 桩（同为无效路径），
#   action 侧同样提供 group_poke。→ 两者都应以 action 为准。
LLBOT = ProtocolCapabilities(
    name="llbot",
    poke_action=True,
    poke_message_segment=False,
    reply_segment=True,
    image_segment=True,
    note="v8 headless：出站无 poke 段（switch 无分支且无 default），用 group_poke action",
)
NAPCAT = ProtocolCapabilities(
    name="napcat",
    poke_action=True,
    poke_message_segment=False,
    reply_segment=True,
    image_segment=True,
    note="poke 段转换为空实现，历史上同样无效；用 group_poke action",
)
UNKNOWN = ProtocolCapabilities(
    name="unknown",
    poke_action=True,
    poke_message_segment=True,
    reply_segment=True,
    image_segment=True,
    note="未知协议端：action 优先，失败回退消息段",
)

_KNOWN = {"llbot": LLBOT, "napcat": NAPCAT}


def capabilities_for(protocol_name: str = "") -> ProtocolCapabilities:
    """按协议端名取能力表；未知则返回 UNKNOWN（action 优先 + 段回退）。

    说明：AstrBot 适配器名恒为 `aiocqhttp`，**无法从中区分 NapCat/LLBot**，
    故默认走 UNKNOWN（两者行为恰好一致：action 优先、段回退）。
    传入 "llbot"/"napcat" 可得到显式记录的能力（供文档/测试/未来分支使用）。
    """
    key = (protocol_name or "").strip().lower()
    return _KNOWN.get(key, UNKNOWN)


def poke_channels(caps: ProtocolCapabilities) -> tuple[str, ...]:
    """回戳的通道回退顺序（从优到劣）。"""
    order: list[str] = []
    if caps.poke_action:
        order.append(CHANNEL_ACTION)
    if caps.poke_message_segment:
        order.append(CHANNEL_SEGMENT)
    return tuple(order)
