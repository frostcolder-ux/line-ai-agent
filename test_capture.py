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


class TestQueue(unittest.TestCase):
    """訊息要存進資料庫，不能留在記憶體。

    這支跑在 Render 免費方案上，行程會休眠也會自己重啟。
    2026-09-14 實測：webhook 進來的訊息在 5 分鐘內就因為行程被換掉而消失，
    「囤在記憶體等湊滿 5 則」的設計永遠等不到那一刻。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (work_capture.JSON_PATH, work_capture.PENDING_JSON)
        work_capture.JSON_PATH = os.path.join(self.tmp, "cap.json")
        work_capture.PENDING_JSON = os.path.join(self.tmp, "pending.json")

    def tearDown(self):
        work_capture.JSON_PATH, work_capture.PENDING_JSON = self._orig

    def q(self, text, gid="C1", who="張姐"):
        return work_capture.queue_message(gid, "蒲草專案", "U1", who, text,
                                          "2026-09-14 15:00:00")

    def test_進來的訊息會留在佇列裡(self):
        self.q("麻煩你下週三前給我報價單")
        self.assertEqual(work_capture.pending_count(), 1)

    def test_只有一則也照樣處理不會被丟掉(self):
        """關鍵：以前少於 5 則會被直接丟棄，最典型的交辦剛好就是一則。"""
        self.q("麻煩你下週三前給我報價單")
        seen = {}

        def fake_analyze(gid, msgs):
            seen["n"] = len(msgs)
            return {"alert": False, "requests": [
                {"title": "給報價單", "raw": msgs[0]["text"], "asker": "張姐",
                 "said_at": "2026-09-14 15:00", "kind": "quote"}]}

        out = work_capture.process_pending(fake_analyze)
        self.assertEqual(seen["n"], 1)
        self.assertEqual(out["captured"], 1)
        self.assertEqual(work_capture.pending_count(), 0, "處理完要標記")

    def test_分析失敗時訊息留在佇列下次再試(self):
        self.q("某句話")

        def boom(gid, msgs):
            raise RuntimeError("API 掛了")

        out = work_capture.process_pending(boom)
        self.assertEqual(out["captured"], 0)
        self.assertEqual(work_capture.pending_count(), 1,
                         "失敗不能標成已處理，否則訊息就永遠消失了")

    def test_多個群組分開分析(self):
        self.q("A 群的話", gid="C1")
        self.q("B 群的話", gid="C2")
        calls = []

        def fake(gid, msgs):
            calls.append(gid)
            return {"requests": []}

        out = work_capture.process_pending(fake)
        self.assertEqual(sorted(calls), ["C1", "C2"])
        self.assertEqual(out["groups"], 2)

    def test_佇列寫入失敗不會拋例外(self):
        """webhook 路徑上的呼叫，壞了也只能吞掉——不能害 LINE 收不到 200。"""
        work_capture.PENDING_JSON = os.path.join(self.tmp, "no", "such", "x.json")
        self.assertFalse(self.q("寫不進去的話"))


class TestParseJson(unittest.TestCase):
    """模型愛把 JSON 包在 markdown 圍欄裡，提示詞寫了「不要 markdown」也沒用。

    2026-09-14 實測：每一次都包。以前是直接 json.loads()，所以每一次分析
    都 JSONDecodeError 然後靜靜回 None——外面看起來像「沒有交辦」，
    其實是從來沒解析成功過。
    """

    GOOD = '{"alert": false, "requests": [{"title": "給報價單"}]}'

    def test_乾淨的_json(self):
        self.assertEqual(monitor.parse_json(self.GOOD)["requests"][0]["title"],
                         "給報價單")

    def test_包在圍欄裡的照樣解得出來(self):
        for wrapped in (F + "json
" + self.GOOD + "
" + F,
                        F + "
" + self.GOOD + "
" + F,
                        F + "json
" + self.GOOD):      # 結尾圍欄被截掉
            out = monitor.parse_json(wrapped)
            self.assertIsNotNone(out, wrapped[:20])
            self.assertEqual(out["requests"][0]["title"], "給報價單")

    def test_前後有廢話也撈得出來(self):
        out = monitor.parse_json("好的，分析結果如下：
" + self.GOOD + "
以上。")
        self.assertIsNotNone(out)

    def test_真的不是_json_就回_None(self):
        self.assertIsNone(monitor.parse_json("我沒辦法分析這段對話"))
        self.assertIsNone(monitor.parse_json(""))


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
