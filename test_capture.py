"""交辦擷取的單元測試。只用標準庫，不打任何 API、不連資料庫。

    python test_capture.py
"""
import os
import tempfile
import time
import unittest

import monitor
import work_capture


class TestFingerprint(unittest.TestCase):

    def test_同一則訊息永遠算出同一個指紋(self):
        a = work_capture.fingerprint("張姐", "2026-09-09 09:12", "合約給我")
        b = work_capture.fingerprint("張姐", "2026-09-09 09:12", "合約給我")
        self.assertEqual(a, b)

    def test_不同的人算出不同指紋(self):
        a = work_capture.fingerprint("張姐", "2026-09-09 09:12", "合約給我")
        b = work_capture.fingerprint("李總", "2026-09-09 09:12", "合約給我")
        self.assertNotEqual(a, b)


class TestSave(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = work_capture.JSON_PATH
        work_capture.JSON_PATH = os.path.join(self.tmp, "cap.json")

    def tearDown(self):
        work_capture.JSON_PATH = self._orig

    ROW = {"title": "給報價單", "raw": "報價單什麼時候給我", "asker": "張姐",
           "said_at": "2026-09-09 09:12", "kind": "quote",
           "group_id": "C1", "group_name": "蒲草專案"}

    def test_同一批寫兩次只會有一筆(self):
        self.assertEqual(work_capture.save_many([self.ROW]), 1)
        self.assertEqual(work_capture.save_many([self.ROW]), 0)
        self.assertEqual(len(work_capture.list_recent(days=30)), 1)

    def test_沒有標題的不建檔(self):
        self.assertEqual(work_capture.save_many([{"raw": "隨便講講"}]), 0)

    def test_統計算得出誰交辦最多(self):
        work_capture.save_many([
            self.ROW,
            {**self.ROW, "raw": "另一句", "asker": "張姐"},
            {**self.ROW, "raw": "第三句", "asker": "李總"},
        ])
        st = work_capture.stats()
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["by_asker"][0], ("張姐", 2))


class TestBuffer(unittest.TestCase):
    """沒達門檻的訊息要囤著，不能丟。

    交辦常常只有孤零零一句（「麻煩你下週給我報價單」），
    在安靜的群組裡永遠湊不到 5 則——以前那句話會被永久丟棄。
    """

    def setUp(self):
        monitor._buffer.clear()

    def test_放回去之後訊息還在而且順序不變(self):
        monitor.buffer_message("C1", "U1", "第一句")
        monitor.buffer_message("C1", "U2", "第二句")
        snapshot = monitor._drain_buffer()
        self.assertEqual(monitor._buffer["C1"], [], "drain 之後該是空的")

        monitor._requeue("C1", snapshot["C1"])
        texts = [m["text"] for m in monitor._buffer["C1"]]
        self.assertEqual(texts, ["第一句", "第二句"])

    def test_放回去之後新訊息接在後面(self):
        monitor.buffer_message("C1", "U1", "舊的")
        old = monitor._drain_buffer()["C1"]
        monitor.buffer_message("C1", "U2", "新的")
        monitor._requeue("C1", old)
        texts = [m["text"] for m in monitor._buffer["C1"]]
        self.assertEqual(texts, ["舊的", "新的"], "時間順序不能亂")

    def test_囤太多會砍掉最舊的(self):
        msgs = [{"user_id": "U1", "text": f"第{i}句", "ts": time.time()}
                for i in range(monitor._MAX_BUFFER + 50)]
        monitor._requeue("C1", msgs)
        self.assertEqual(len(monitor._buffer["C1"]), monitor._MAX_BUFFER)
        self.assertEqual(monitor._buffer["C1"][-1]["text"],
                         f"第{monitor._MAX_BUFFER + 49}句", "要留最新的")

    def test_貼圖之類的照樣會進暫存(self):
        # 過濾是在 app.py 那層做的，這裡只管存
        monitor.buffer_message("C1", "U1", "任何文字")
        self.assertEqual(len(monitor._buffer["C1"]), 1)


class TestAnalyzePrompt(unittest.TestCase):

    def test_送進模型的對話帶時間與名字(self):
        """模型要靠這兩樣才推得出「明天」是哪天、誰交辦的。"""
        captured = {}

        class FakeClient:
            class messages:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    class R:
                        content = [type("B", (), {"text": '{"alert": false}'})()]
                    return R()

        monitor._analyze(FakeClient, "m", "C1", [
            {"user_id": "Uabc123", "text": "合約給我", "ts": 1757380320.0,
             "who": "張姐"}])
        sent = captured["messages"][0]["content"]
        self.assertIn("張姐", sent)
        self.assertIn("09:12", sent)
        self.assertGreater(captured["max_tokens"], 200,
                           "要同時吐預警與交辦，200 會被截斷")


if __name__ == "__main__":
    unittest.main(verbosity=2)
