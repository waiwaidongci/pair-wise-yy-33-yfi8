import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


class AlarmLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "支线", "line", 100, "B", self.sub["id"])
        self.hosp = self.s.register_facility("dispatcher", "dispatcher", "市医院", "hospital", self.line["id"], 1, 50, 40)
        self.mall = self.s.register_facility("dispatcher", "dispatcher", "商场", "mall", self.sub["id"], 3, 10)

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def outage(self, code="OUT-A"):
        return self.s.create_outage("dispatcher", "dispatcher", code, "母线故障", ["A"])

    def note_of(self, board, name):
        return [f["notification"] for f in board["facilities"] if f["facility"]["name"] == name][0]

    def active_plan(self, outage):
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "送电", "asset": "SUB", "required_mw": 80, "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        return self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def test_impact_follows_parent_child_and_single_current(self):
        outage = self.outage()
        board = self.s.issue_notifications("dispatcher", "dispatcher", outage["id"])
        names = [f["facility"]["name"] for f in board["facilities"]]
        self.assertIn("市医院", names)  # 挂在子线路 LINE（区域B）上，沿父子关系推算命中
        self.assertIn("商场", names)
        self.s.issue_notifications("dispatcher", "dispatcher", outage["id"])
        notes = [n for n in self.s.state()["notifications"] if n["outage_id"] == outage["id"]]
        self.assertEqual(2, len(notes))  # 重复生成不产生第二条当前通知

    def test_revision_change_marks_unacked_for_resend(self):
        outage = self.outage()
        board = self.s.issue_notifications("dispatcher", "dispatcher", outage["id"])
        self.s.ack_notification("field", "field", self.note_of(board, "商场")["id"])
        self.s.revise_outage("dispatcher", "dispatcher", outage["id"], "母线故障扩大", ["A"])
        board = self.s.delivery_board(outage["id"])
        self.assertEqual("resend", self.note_of(board, "市医院")["state"])       # 未回执 → 待重发
        self.assertEqual("acknowledged", self.note_of(board, "商场")["state"])  # 已回执不打扰
        with self.assertRaises(ApiError):  # 待重发的旧内容不能回执
            self.s.ack_notification("field", "field", self.note_of(board, "市医院")["id"])
        resent = self.s.resend_notification("dispatcher", "dispatcher", self.note_of(board, "市医院")["id"])
        self.assertEqual("sent", resent["notification"]["state"])
        self.assertEqual(2, resent["notification"]["outage_revision"])

    def test_urge_and_ack_record_operator_and_time(self):
        outage = self.outage()
        board = self.s.issue_notifications("dispatcher", "dispatcher", outage["id"])
        note_id = self.note_of(board, "市医院")["id"]
        view = self.s.urge_notification("dispatcher", "dispatcher", note_id, "请尽快回执")
        urge = [e for e in view["events"] if e["event_type"] == "urge"][0]
        self.assertEqual("dispatcher", urge["actor"]); self.assertTrue(urge["at"])
        view = self.s.ack_notification("field", "field", note_id)
        self.assertEqual("field", view["notification"]["acked_by"])
        self.assertTrue(view["notification"]["acked_at"])
        with self.assertRaises(ApiError):  # 已回执不能重复回执或催办
            self.s.ack_notification("field", "field", note_id)
        with self.assertRaises(ApiError):
            self.s.urge_notification("dispatcher", "dispatcher", note_id)

    def test_restore_blocked_until_tier1_clear(self):
        outage = self.outage()
        plan = self.active_plan(outage)
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        with self.assertRaises(ApiError) as ctx:  # 市医院：联系人失联 + 尚未回执
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertIn("联系人失联", str(ctx.exception))
        self.assertIn("尚未回执", str(ctx.exception))
        self.s.set_contact("dispatcher", "dispatcher", self.hosp["id"], "配电室", "95598", True)
        board = self.s.issue_notifications("dispatcher", "dispatcher", outage["id"])
        self.s.ack_notification("field", "field", self.note_of(board, "市医院")["id"])
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restored", status["status"]["state"])

    def test_restore_blocked_by_weak_backup(self):
        icu = self.s.register_facility("dispatcher", "dispatcher", "ICU", "hospital", self.sub["id"], 1, 10, 30)
        outage = self.outage("OUT-B")
        plan = self.active_plan(outage)
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        for fac in (self.hosp, icu):
            self.s.set_contact("dispatcher", "dispatcher", fac["id"], "配电室", "95598", True)
        board = self.s.issue_notifications("dispatcher", "dispatcher", outage["id"])
        for item in board["facilities"]:
            self.s.ack_notification("field", "field", item["notification"]["id"])
        with self.assertRaises(ApiError) as ctx:  # ICU 备用 10MW < 必需 30MW
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertIn("备用电源不足", str(ctx.exception))


if __name__ == "__main__": unittest.main()
