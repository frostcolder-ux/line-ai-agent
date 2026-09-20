"""香草遊戲通知名單的測試：python test_herb_notify.py（不需要資料庫，走 JSON 模式）"""
import os
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)

import herb_notify


class HerbNotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        herb_notify.JSON_PATH = os.path.join(self.tmp.name, "herb_notify.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_wants_reads_the_sentence(self):
        self.assertEqual(herb_notify.wants("香草通知"), "on")
        self.assertEqual(herb_notify.wants("我要香草通知！"), "on")
        # 「取消香草通知」裡面也有「香草通知」，要先看取消
        self.assertEqual(herb_notify.wants("取消香草通知"), "off")
        self.assertIsNone(herb_notify.wants("這週誰還沒交週報"))

    def test_subscribe_then_unsubscribe(self):
        self.assertTrue(herb_notify.subscribe("U1"))
        self.assertFalse(herb_notify.subscribe("U1"), "重複傳不算新加入")
        herb_notify.subscribe("U2")
        self.assertEqual(herb_notify.count(), 2)

        self.assertTrue(herb_notify.unsubscribe("U1"))
        self.assertEqual(herb_notify.recipients(), ["U2"])
        self.assertFalse(herb_notify.unsubscribe("U1"), "已經不在名單上")

    def test_chunks_fit_line_limit(self):
        ids = [f"U{i}" for i in range(400)]
        batches = herb_notify.chunks(ids)
        self.assertTrue(all(len(b) <= 500 for b in batches))
        self.assertEqual(sum(len(b) for b in batches), 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
