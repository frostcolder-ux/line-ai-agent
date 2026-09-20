"""白名單的測試：python test_access.py

不需要資料庫——沒有 DATABASE_URL 時 access 會走本機 JSON，測試把那個檔案
指到暫存目錄，跑完就丟。
"""
import os
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)  # 確保走 JSON 模式

import access


class FakeSource:
    """模擬 LINE SDK 的 event.source。群組訊息有 group_id，私訊只有 user_id。"""

    def __init__(self, user_id="U_stranger", group_id=None):
        self.user_id = user_id
        if group_id:
            self.group_id = group_id


BOSS = "U_boss"


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        access.JSON_PATH = os.path.join(self.tmp.name, "line_access.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_stranger_is_outside(self):
        """陌生人私訊：外人。這是白名單存在的理由。"""
        self.assertEqual(access.role_of(FakeSource(), BOSS), access.OUTSIDE)

    def test_boss_private_is_boss(self):
        self.assertEqual(access.role_of(FakeSource(BOSS), BOSS), access.BOSS)

    def test_unapproved_group_is_outside(self):
        """外人把小凡拉進自己的群組，在核准前一律當外人。"""
        src = FakeSource("U_someone", "C_new")
        self.assertEqual(access.role_of(src, BOSS), access.OUTSIDE)
        self.assertFalse(access.is_approved("C_new"))

    def test_approve_by_code(self):
        row = access.remember_pending("C_new", "大溪工作群")
        self.assertEqual(len(row["code"]), 4)
        # 同一個群組再進來一次不會換代號，老闆手上那組不會失效
        self.assertEqual(access.remember_pending("C_new")["code"], row["code"])

        access.decide(row["code"], "approved")
        self.assertTrue(access.is_approved("C_new"))
        self.assertEqual(access.role_of(FakeSource("U_someone", "C_new"), BOSS), access.GROUP)
        # 群組裡的老闆仍然是老闆（交給總管、核准指令都要能用）
        self.assertEqual(access.role_of(FakeSource(BOSS, "C_new"), BOSS), access.BOSS)

    def test_reject_keeps_group_out(self):
        row = access.remember_pending("C_bad", "陌生人的群")
        access.decide(row["code"], "rejected")
        self.assertFalse(access.is_approved("C_bad"))

    def test_boss_commands(self):
        self.assertEqual(access.parse_boss_command("核准 A7K2"), ("approve", "A7K2"))
        self.assertEqual(access.parse_boss_command("拒絕A7K2"), ("reject", "A7K2"))
        self.assertEqual(access.parse_boss_command("群組清單"), ("list", ""))
        # 後面沒有代號的就是一般對話，不能被當成指令吃掉
        self.assertIsNone(access.parse_boss_command("核准這件事再跟我說"))
        self.assertIsNone(access.parse_boss_command("這週誰還沒交週報"))

    def test_boss_command_tolerates_extra_words(self):
        """老闆不會每次都照最精簡的格式打（2026-09-20 實際踩到「拒絕代號 F3VH」沒反應）。"""
        self.assertEqual(access.parse_boss_command("拒絕代號 F3VH"), ("reject", "F3VH"))
        self.assertEqual(access.parse_boss_command("核准群組 A7K2"), ("approve", "A7K2"))
        self.assertEqual(access.parse_boss_command("核准：A7K2"), ("approve", "A7K2"))
        self.assertEqual(access.parse_boss_command("核准 A7K2。"), ("approve", "A7K2"))
        # 代號讀不出來時回清單，不要沉默
        self.assertEqual(access.parse_boss_command("拒絕代號"), ("list", ""))
        self.assertEqual(access.parse_boss_command("核准 F3VH2XYZW"), ("list", ""))
        # 正常對話不能被當成指令
        self.assertIsNone(access.parse_boss_command("核准這件事再跟我說"))

    def test_group_decision_words(self):
        """老闆人在那個群組裡時，說「核准」兩個字就算數，不必找代號。"""
        self.assertEqual(access.parse_group_decision("核准"), "approve")
        self.assertEqual(access.parse_group_decision(" 同意！"), "approve")
        self.assertEqual(access.parse_group_decision("拒絕"), "reject")
        self.assertIsNone(access.parse_group_decision("核准這件事"))
        self.assertIsNone(access.parse_group_decision("這週誰還沒交週報"))

    def test_seed_from_config(self):
        """白名單上線時，設定檔裡現有的群組要自動變成已核准。"""
        config = {
            "partner_groups": [{"group_id": "C_partner", "name": "契作群"}],
            "scheduled_tasks": [{"group_id": "C_sched", "name": "排程群"}, {"group_id": ""}],
        }
        self.assertEqual(access.seed_from_config(config), 2)
        self.assertTrue(access.is_approved("C_partner"))
        self.assertTrue(access.is_approved("C_sched"))
        # 重跑不會重複新增
        self.assertEqual(access.seed_from_config(config), 0)

    def test_seed_does_not_revive_rejected(self):
        row = access.remember_pending("C_partner", "曾經被拒絕的群")
        access.decide(row["code"], "rejected")
        access.seed_from_config({"partner_groups": [{"group_id": "C_partner"}]})
        self.assertFalse(access.is_approved("C_partner"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
