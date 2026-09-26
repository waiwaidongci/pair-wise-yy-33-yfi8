import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store
from impact import affected_asset_ids, affected_facilities


class AlertLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "一号线", "line", 100, "A", self.sub["id"])
        self.branch = self.s.register_asset("dispatcher", "dispatcher", "BRANCH", "北支线", "line", 60, "A", self.line["id"])
        # 市医院（一级，有备用）挂在支线；水厂（二级，无备用）挂在支线
        self.hospital = self.s.register_facility("dispatcher", "dispatcher", "市医院", "hospital", self.branch["id"], 1, 50)
        self.water = self.s.register_facility("dispatcher", "dispatcher", "水厂", "water", self.branch["id"], 2, 0)

    def tearDown(self):
        self.s.store.close(); self.tmp.cleanup()

    def outage(self, code="OUT-1", asset_id=None):
        return self.s.create_outage("dispatcher", "dispatcher", code, "线路跳闸", ["A"], asset_id)

    def alerts_of(self, outage_id):
        return {a["facility_name"]: a for a in self.s.alerts_for_outage(outage_id)["alerts"]}

    # ---- 影响范围推算 --------------------------------------------------

    def test_scope_follows_parent_child_asset_tree(self):
        ids = affected_asset_ids(self.s.conn, self.sub["id"])
        self.assertEqual({self.sub["id"], self.line["id"], self.branch["id"]}, set(ids))
        self.assertEqual([self.branch["id"]], affected_asset_ids(self.s.conn, self.branch["id"]))
        names = {f["name"] for f in affected_facilities(self.s.conn, self.line["id"])}
        self.assertEqual({"市医院", "水厂"}, names)
        # 无下游的资产不会凭空产生对象
        self.assertEqual([], affected_facilities(self.s.conn, 9999))

    def test_outage_builds_one_ledger_row_per_facility_and_is_idempotent(self):
        out = self.outage(asset_id=self.sub["id"])
        self.assertEqual({"市医院", "水厂"}, set(self.alerts_of(out["id"])))
        self.assertEqual(2, len(self.alerts_of(out["id"])))
        # 重复事故幂等；补齐台账也不重复建档
        self.outage(asset_id=self.sub["id"])
        self.s.reconcile_alerts("dispatcher", "dispatcher", out["id"])
        self.assertEqual(2, len(self.alerts_of(out["id"])))

    def test_outage_without_asset_has_empty_scope(self):
        out = self.outage(code="OUT-NA")
        self.assertEqual([], self.s.alerts_for_outage(out["id"])["alerts"])
        self.assertEqual([], self.s.outage_scope(out["id"])["affected_assets"])

    # ---- 同一对象只留一条当前通知 --------------------------------------

    def test_overlapping_outages_keep_only_one_current_notification_per_facility(self):
        out1 = self.outage("OUT-A", self.sub["id"])
        out2 = self.outage("OUT-B", self.sub["id"])
        a1 = self.alerts_of(out1["id"])["市医院"]
        a2 = self.alerts_of(out2["id"])["市医院"]
        self.assertEqual("pending", a1["state"])
        self.s.ledger.send("dispatcher", "dispatcher", a1["id"], "第一次停电通知")
        self.assertEqual("sent", self.alerts_of(out1["id"])["市医院"]["state"])
        # 叠加停电的新通知送达后，旧通知被取代，但记录保留可追溯
        self.s.ledger.send("dispatcher", "dispatcher", a2["id"], "叠加停电通知")
        self.assertEqual("superseded", self.alerts_of(out1["id"])["市医院"]["state"])
        self.assertEqual("sent", self.alerts_of(out2["id"])["市医院"]["state"])
        events = [e for e in self.s.alerts_for_outage(out1["id"])["events"] if e["alert_id"] == a1["id"]]
        self.assertIn("superseded", [e["event"] for e in events])
        # 被取代的记录可重发，重发后再次成为当前通知
        redelivered = self.s.ledger.resend("dispatcher", "dispatcher", a1["id"])
        self.assertEqual("sent", redelivered["state"])
        self.assertEqual("superseded", self.alerts_of(out2["id"])["市医院"]["state"])

    # ---- 事故版本变动：未回执转待重发 ----------------------------------

    def test_outage_revision_flips_unacked_notice_to_pending_resend(self):
        out = self.outage(asset_id=self.sub["id"])
        a = self.alerts_of(out["id"])["市医院"]
        self.s.ledger.send("dispatcher", "dispatcher", a["id"])
        # 建立恢复计划 -> 事故 revision+1 -> 已送达未回执转待重发
        plan = self.s.create_plan("dispatcher", "dispatcher", out["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80}])
        self.assertEqual(2, self.s._row("outages", out["id"])["revision"])
        a = self.alerts_of(out["id"])["市医院"]
        self.assertEqual("pending_resend", a["state"])
        self.assertEqual(2, a["outage_revision"])
        stale_events = [e for e in self.s.alerts_for_outage(out["id"])["events"] if e["event"] == "stale"]
        self.assertEqual(1, len(stale_events))
        self.assertEqual("system", stale_events[0]["actor"])
        # 未送达的通知不受版本变动影响
        self.assertEqual("pending", self.alerts_of(out["id"])["水厂"]["state"])
        # 重发后回到已送达
        self.assertEqual("sent", self.s.ledger.resend("dispatcher", "dispatcher", a["id"])["state"])
        with self.assertRaises(ApiError):  # sent 不能再重发
            self.s.ledger.resend("dispatcher", "dispatcher", a["id"])

    def test_acked_notice_is_not_flipped_by_revision(self):
        out = self.outage(asset_id=self.sub["id"])
        a = self.alerts_of(out["id"])["市医院"]
        self.s.ledger.send("dispatcher", "dispatcher", a["id"])
        self.s.ledger.ack("operator-a", "operator", a["id"], "医院值班室确认")
        self.s.create_plan("dispatcher", "dispatcher", out["id"],
                          [{"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80}])
        self.assertEqual("acked", self.alerts_of(out["id"])["市医院"]["state"])

    # ---- 催办与回执留痕 ------------------------------------------------

    def test_remind_and_ack_record_actor_and_time(self):
        out = self.outage(asset_id=self.sub["id"])
        a = self.alerts_of(out["id"])["市医院"]
        with self.assertRaises(ApiError):  # 未送达不能催办
            self.s.ledger.remind("dispatcher", "dispatcher", a["id"])
        with self.assertRaises(ApiError):  # 未送达不能回执
            self.s.ledger.ack("operator-a", "operator", a["id"])
        self.s.ledger.send("dispatcher", "dispatcher", a["id"])
        reminded = self.s.ledger.remind("dispatcher", "dispatcher", a["id"], "换班后电话催办")
        self.assertEqual("dispatcher", reminded["reminded_by"])
        self.assertIsNotNone(reminded["reminded_at"])
        acked = self.s.ledger.ack("operator-b", "operator", a["id"], "夜班接收")
        self.assertEqual("operator-b", acked["acked_by"])
        self.assertIsNotNone(acked["acked_at"])
        self.assertTrue(acked["contact_reachable"])
        with self.assertRaises(ApiError):  # 已闭环不能重复催办
            self.s.ledger.remind("dispatcher", "dispatcher", a["id"])

    def test_contact_lost_blocks_and_ack_recovers(self):
        out = self.outage(asset_id=self.sub["id"])
        a = self.alerts_of(out["id"])["市医院"]
        self.s.ledger.send("dispatcher", "dispatcher", a["id"])
        self.s.ledger.set_contact("operator", "operator", a["id"], False, "总机无人接听")
        blockers = self.s.ledger.restore_blockers(out["id"])
        reasons = {b["facility"]: b["reasons"] for b in blockers}
        self.assertIn("联系人失联", reasons["市医院"])
        self.assertIn("尚未回执", reasons["市医院"])
        self.assertNotIn("市医院", [b["facility"] for b in blockers if b["reasons"] == ["备用电源不足"]])

    def test_roles_are_enforced_for_alert_actions(self):
        out = self.outage(asset_id=self.sub["id"])
        a = self.alerts_of(out["id"])["市医院"]
        with self.assertRaises(ApiError):  # field 不能发送通知
            self.s.ledger.send("field", "field", a["id"])
        with self.assertRaises(ApiError):  # 无身份
            self.s.ledger.send(None, None, a["id"])

    # ---- 恢复发布闸门 --------------------------------------------------

    def _activate_full_plan(self, out):
        plan = self.s.create_plan("dispatcher", "dispatcher", out["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        self.s.field_report("field", "field", plan["id"], 1, "r1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.s.field_report("field", "field", plan["id"], 2, "r2", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        return plan

    def test_restore_completion_blocked_until_priority1_cleared(self):
        out = self.outage(asset_id=self.sub["id"])
        for a in self.alerts_of(out["id"]).values():
            self.s.ledger.send("dispatcher", "dispatcher", a["id"])
        # 版本变动（计划创建/激活）后已送达通知转待重发：未回执阻断
        plan = self._activate_full_plan(out)
        blockers = {b["facility"]: b["reasons"] for b in self.s.ledger.restore_blockers(out["id"])}
        self.assertIn("市医院", blockers)
        self.assertIn("尚未回执", blockers["市医院"])
        # 水厂是二级用户，不参与闸门
        self.assertNotIn("水厂", blockers)
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", out["id"], plan["id"])
        # 重发并回执一级用户；其备用 50>0、联系人可达 -> 放行
        current = self.alerts_of(out["id"])["市医院"]
        self.s.ledger.resend("dispatcher", "dispatcher", current["id"])
        self.s.ledger.ack("operator-a", "operator", current["id"], "确认恢复")
        self.assertEqual([], self.s.ledger.restore_blockers(out["id"]))
        result = self.s.publish_status("dispatcher", "dispatcher", out["id"], plan["id"])
        self.assertEqual("restored", result["status"]["state"])

    def test_backup_shortage_blocks_restore_even_after_ack(self):
        # 无备用电源的一级用户
        self.s.register_facility("dispatcher", "dispatcher", "急救中心", "hospital", self.line["id"], 1, 0)
        out = self.outage("OUT-2", self.sub["id"])
        for a in self.s.alerts_for_outage(out["id"])["alerts"]:
            self.s.ledger.send("dispatcher", "dispatcher", a["id"])
        plan = self._activate_full_plan(out)
        a = next(a for a in self.s.alerts_for_outage(out["id"])["alerts"] if a["facility_name"] == "急救中心")
        self.s.ledger.resend("dispatcher", "dispatcher", a["id"])
        self.s.ledger.ack("operator-a", "operator", a["id"])
        h = next(a for a in self.s.alerts_for_outage(out["id"])["alerts"] if a["facility_name"] == "市医院")
        self.s.ledger.resend("dispatcher", "dispatcher", h["id"])
        self.s.ledger.ack("operator-a", "operator", h["id"])
        blockers = {b["facility"]: b["reasons"] for b in self.s.ledger.restore_blockers(out["id"])}
        self.assertEqual(["备用电源不足"], blockers.get("急救中心"))
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", out["id"], plan["id"])
        # 现场确认备用已补足 -> 放行
        self.s.ledger.set_backup("operator", "operator", a["id"], True, "柴油发电机已就位")
        result = self.s.publish_status("dispatcher", "dispatcher", out["id"], plan["id"])
        self.assertEqual("restored", result["status"]["state"])


if __name__ == "__main__":
    unittest.main()
