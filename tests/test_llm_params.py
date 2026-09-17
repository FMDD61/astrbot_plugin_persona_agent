# -*- coding: utf-8 -*-
"""llm_params reasoning_value mapping tests (A7④).

2026-09-10 实测修正：网关拒绝 "none"/"off" 等值（HTTP 400）——关闭显式思考
档位的正确方式是返回 None（不发送该参数）。旧实现 off→"none" 会让每次调用
400 并被吞成空回复。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.llm_params import (  # noqa: E402
    reasoning_value, resolve_provider_id, extract_reasoning,
)


class TestReasoningValue(unittest.TestCase):
    def test_off_maps_none_sentinel(self):
        """off → None（不发送该键），而非字符串 "none"（网关 400）。"""
        self.assertIsNone(reasoning_value("off"))

    def test_passthrough_valid(self):
        for v in ("low", "medium", "high", "max"):
            self.assertEqual(reasoning_value(v), v)

    def test_case_insensitive(self):
        self.assertIsNone(reasoning_value("OFF"))
        self.assertEqual(reasoning_value("Low"), "low")
        self.assertEqual(reasoning_value("HIGH"), "high")

    def test_gateway_rejected_values_map_to_none(self):
        """网关明确 400 的取值（none/minimal/disabled/false）→ None。"""
        for v in ("none", "minimal", "disabled", "false", False, 0):
            self.assertIsNone(reasoning_value(v), v)

    def test_unknown_falls_back_none(self):
        self.assertIsNone(reasoning_value("deep"))
        self.assertIsNone(reasoning_value(""))
        self.assertIsNone(reasoning_value(None))
        self.assertIsNone(reasoning_value(123))


class TestResolveProviderId(unittest.TestCase):
    """S0：provider 三级回退 + **配置值存在性校验**。

    为什么需要校验：配置里写错一个 provider id，``llm_generate`` 会抛
    ``ProviderNotFoundError``，插件把它吞成空回复 → 表面"没坏"、实际全哑。
    S0 的整批缺陷都是这个形状，所以这里宁可多查一次。
    """

    def test_configured_wins(self):
        self.assertEqual(resolve_provider_id("cfg", "known", "sess", True), "cfg")

    def test_unknown_existence_still_uses_configured(self):
        """拿不到 provider_manager 时按"未知，放行"，不能因校验本身失败而停摆。"""
        self.assertEqual(resolve_provider_id("cfg", "known", "sess", None), "cfg")

    def test_bad_configured_falls_back_to_known(self):
        self.assertEqual(resolve_provider_id("typo", "known", "sess", False), "known")

    def test_bad_configured_falls_back_to_session(self):
        self.assertEqual(resolve_provider_id("typo", None, "sess", False), "sess")

    def test_bad_configured_no_fallback_returns_none(self):
        self.assertIsNone(resolve_provider_id("typo", None, None, False))

    def test_empty_configured_uses_known_then_session(self):
        self.assertEqual(resolve_provider_id("", "known", "sess", None), "known")
        self.assertEqual(resolve_provider_id("", None, "sess", None), "sess")
        self.assertIsNone(resolve_provider_id("", None, None, None))

    def test_whitespace_configured_treated_as_empty(self):
        self.assertEqual(resolve_provider_id("   ", None, "sess", None), "sess")

    def test_never_returns_empty_string(self):
        """调用方用 ``if not provider_id`` 判定 → 绝不能返回空串。"""
        for args in (("", None, None, None), ("  ", "", "", False),
                     ("typo", "", "", False)):
            self.assertFalse(resolve_provider_id(*args))


class TestExtractReasoning(unittest.TestCase):
    """🔴 B-029（2026-09-17 实测）：网关把思维链放在 **reasoning** 字段。

    AstrBot 的 openai_chat_completion 只认标准字段 reasoning_content（且**从不**
    给它赋值 —— 只有 anthropic_source 会），而 commandcode 网关（OpenAI 兼容）
    回的是非标准字段 reasoning（str）+ reasoning_details（list）。

    实测（2026-09-17，同一模型同一参数）：
      message 字段 = [..., 'reasoning', 'reasoning_details', ...]
      reasoning_content → None；reasoning → '我们需要回答用户中文…'（非空）
      usage.reasoning_tokens = 27

    后果：S16 的"思维链留存"从未生效（401/401 次 reasoning_chars=0，6 个 session
    文件的 _reasoning 全为 0），导出恒显示"思维链：无"，而同一批调用被计费
    reasoning_tokens 合计 60,482 —— **看起来像"模型没思考"**。
    """

    @staticmethod
    def _resp(reasoning_content=None, raw=None):
        import types
        return types.SimpleNamespace(reasoning_content=reasoning_content,
                                     raw_completion=raw)

    @staticmethod
    def _raw(message_attrs: dict):
        import types
        msg = types.SimpleNamespace(**message_attrs)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])

    def test_standard_field_takes_precedence(self):
        r = self._resp("标准字段", self._raw({"reasoning": "网关字段"}))
        self.assertEqual(extract_reasoning(r), "标准字段")

    def test_gateway_reasoning_field(self):
        r = self._resp(None, self._raw({"content": "2", "reasoning": "先算加法"}))
        self.assertEqual(extract_reasoning(r), "先算加法")

    def test_reasoning_details_list_fallback(self):
        r = self._resp(None, self._raw({"reasoning_details": [
            {"type": "reasoning.text", "text": "第一段"},
            {"type": "reasoning.text", "text": "第二段"},
        ]}))
        self.assertEqual(extract_reasoning(r), "第一段\n第二段")

    def test_dict_shaped_message(self):
        """兼容 raw 是 dict（不同 SDK/版本）的情况。"""
        r = self._resp(None, {"choices": [{"message": {"reasoning": "字典形状"}}]})
        self.assertEqual(extract_reasoning(r), "字典形状")

    def test_missing_everything_returns_empty(self):
        self.assertEqual(extract_reasoning(self._resp(None, self._raw({"content": "2"}))), "")
        self.assertEqual(extract_reasoning(self._resp()), "")
        self.assertEqual(extract_reasoning(None), "")

    def test_never_raises_on_weird_objects(self):
        class Boom:
            @property
            def reasoning_content(self):
                raise RuntimeError("boom")

            @property
            def raw_completion(self):
                raise RuntimeError("boom")

        self.assertEqual(extract_reasoning(Boom()), "")
        self.assertEqual(extract_reasoning(object()), "")



if __name__ == "__main__":
    unittest.main()
