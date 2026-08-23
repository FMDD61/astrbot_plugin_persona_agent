# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.context_buffer import ContextBuffer


class TestQuoteTarget(unittest.TestCase):
    def test_n_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            cb = ContextBuffer(td, persist_jsonl=False)
            cb.add(ts=1, group_id='g', sender_id='a', sender_name='a', text='第一条', message_id='m1')
            cb.add(ts=2, group_id='g', sender_id='b', sender_name='b', text='第二条', message_id='m2')
            self.assertEqual(cb.quote_target(1), 'm2')
            self.assertEqual(cb.quote_target(2), 'm1')
            self.assertIsNone(cb.quote_target(3))
            self.assertIsNone(cb.quote_target(0))

    def test_bot_message_no_id(self):
        with tempfile.TemporaryDirectory() as td:
            cb = ContextBuffer(td, persist_jsonl=False)
            cb.add(ts=1, group_id='g', sender_id='a', sender_name='a', text='x', message_id='m1')
            cb.add(ts=2, group_id='g', sender_id='bot', sender_name='<bot>', text='y', message_id='', message_type='bot')
            self.assertIsNone(cb.quote_target(1))  # bot msg has no id
            self.assertEqual(cb.quote_target(2), 'm1')


if __name__ == "__main__":
    unittest.main()
