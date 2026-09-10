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
from services.llm_params import reasoning_value


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


if __name__ == "__main__":
    unittest.main()
