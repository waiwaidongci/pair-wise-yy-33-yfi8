"""告警台账：受影响对象的通知生命周期、催办与回执留痕、恢复完成阻断检查。

规则：
- 同一事故下同一受影响对象只保留一条当前通知（数据库部分唯一索引兜底）。
- 事故版本变动时，未回执的当前通知转"待重发"；已回执的不打扰。
- 催办与回执均记录操作人与时间（notification_events）。
- 一级用户备用电源不足、联系人失联或尚未回执时，阻断事故发布恢复完成。
"""
import json
from datetime import datetime, timezone

from errors import ApiError


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def notification_content(outage) -> str:
    regions = "、".join(json.loads(outage["affected_regions_json"]))
    return (f"事故{outage['incident_code']}（第{outage['revision']}版）：{outage['title']}；"
            f"影响区域：{regions}；当前状态：{outage['state']}")


class AlarmLedger:
    def __init__(self, conn, impact):
        self.conn, self.impact = conn, impact

    def _outage(self, outage_id: int):
        row = self.conn.execute("SELECT * FROM outages WHERE id=?", (outage_id,)).fetchone()
        if not row:
            raise ApiError(404, "事故不存在")
        return row

    def _notification(self, notification_id: int):
        row = self.conn.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
        if not row:
            raise ApiError(404, "通知不存在")
        return row

    def _event(self, notification_id: int, event_type: str, actor: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO notification_events(notification_id,event_type,actor,at,note) VALUES(?,?,?,?,?)",
            (notification_id, event_type, actor, now(), note))

    def issue_for_outage(self, outage_id: int, actor: str) -> dict:
        """按当前影响范围生成通知：新对象首达，未回执对象按最新事故版本重发，退出范围的对象作废。"""
        outage = self._outage(outage_id)
        affected = self.impact.affected_facilities(json.loads(outage["affected_regions_json"]))
        content = notification_content(outage)
        current = {row["facility_id"]: row for row in self.conn.execute(
            "SELECT * FROM notifications WHERE outage_id=? AND is_current=1", (outage_id,))}
        with self.conn:
            for item in affected:
                facility = item["facility"]
                existing = current.pop(facility["id"], None)
                if existing is not None and (
                        existing["state"] == "acknowledged" or
                        (existing["state"] == "sent" and int(existing["outage_revision"]) == int(outage["revision"]))):
                    continue  # 已回执或内容未变，不重复打扰
                if existing is None:
                    cur = self.conn.execute(
                        """INSERT INTO notifications(outage_id,facility_id,outage_revision,content,state,is_current,
                                                     created_by,created_at,sent_by,sent_at)
                           VALUES(?,?,?,?, 'sent',1,?,?,?,?)""",
                        (outage_id, facility["id"], outage["revision"], content, actor, now(), actor, now()))
                    self._event(cur.lastrowid, "send", actor, "首次送达")
                else:
                    self.conn.execute(
                        "UPDATE notifications SET content=?,outage_revision=?,state='sent',sent_by=?,sent_at=? WHERE id=?",
                        (content, outage["revision"], actor, now(), existing["id"]))
                    self._event(existing["id"], "send", actor, "按最新事故版本重发")
            for stale in current.values():
                self.conn.execute("UPDATE notifications SET is_current=0 WHERE id=?", (stale["id"],))
                self._event(stale["id"], "withdraw", actor, "对象已不在影响范围")
        return self.delivery_board(outage_id)

    def sync_outage_revision(self, outage_id: int, actor: str) -> int:
        """事故版本变动：未回执的当前通知转待重发，返回转换条数。"""
        outage = self._outage(outage_id)
        rows = self.conn.execute(
            """SELECT * FROM notifications WHERE outage_id=? AND is_current=1
               AND state!='acknowledged' AND outage_revision!=?""",
            (outage_id, outage["revision"])).fetchall()
        with self.conn:
            for row in rows:
                self.conn.execute("UPDATE notifications SET state='resend' WHERE id=? AND state=?", (row["id"], row["state"]))
                self._event(row["id"], "resend_flag", actor,
                            f"事故版本 {row['outage_revision']} → {outage['revision']}，待重发")
        return len(rows)

    def urge(self, notification_id: int, actor: str, note: str = "") -> dict:
        """催办：留操作人与时间。"""
        row = self._notification(notification_id)
        if not row["is_current"]:
            raise ApiError(409, "历史通知不能催办")
        if row["state"] == "acknowledged":
            raise ApiError(409, "已回执，无需催办")
        with self.conn:
            self._event(notification_id, "urge", actor, note)
        return self.notification_view(notification_id)

    def acknowledge(self, notification_id: int, actor: str, note: str = "") -> dict:
        """回执：只对已送达的最新内容有效，留操作人与时间。"""
        row = self._notification(notification_id)
        if not row["is_current"]:
            raise ApiError(409, "历史通知不能回执")
        if row["state"] == "resend":
            raise ApiError(409, "通知已转待重发，请先重发最新版本")
        if row["state"] == "acknowledged":
            raise ApiError(409, "通知已回执")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE notifications SET state='acknowledged',acked_by=?,acked_at=? WHERE id=? AND state='sent'",
                (actor, now(), notification_id))
            if cur.rowcount != 1:
                raise ApiError(409, "并发回执冲突")
            self._event(notification_id, "ack", actor, note)
        return self.notification_view(notification_id)

    def resend(self, notification_id: int, actor: str) -> dict:
        """待重发通知按当前事故版本重新送达。"""
        row = self._notification(notification_id)
        if not row["is_current"]:
            raise ApiError(409, "历史通知不能重发")
        if row["state"] != "resend":
            raise ApiError(409, "仅待重发通知需要重发")
        outage = self._outage(row["outage_id"])
        with self.conn:
            self.conn.execute(
                "UPDATE notifications SET state='sent',content=?,outage_revision=?,sent_by=?,sent_at=? WHERE id=?",
                (notification_content(outage), outage["revision"], actor, now(), notification_id))
            self._event(notification_id, "send", actor, "待重发后重新送达")
        return self.notification_view(notification_id)

    def notification_view(self, notification_id: int) -> dict:
        row = self._notification(notification_id)
        events = [dict(r) for r in self.conn.execute(
            "SELECT * FROM notification_events WHERE notification_id=? ORDER BY id DESC", (notification_id,))]
        return {"notification": dict(row), "events": events}

    def delivery_board(self, outage_id: int) -> dict:
        """按事故看待送达：每个受影响对象的备用电源、联系人、当前通知与留痕。"""
        outage = self._outage(outage_id)
        affected = self.impact.affected_facilities(json.loads(outage["affected_regions_json"]))
        notifications = {row["facility_id"]: dict(row) for row in self.conn.execute(
            "SELECT * FROM notifications WHERE outage_id=? AND is_current=1", (outage_id,))}
        board = {"outage": {"id": outage["id"], "incident_code": outage["incident_code"], "title": outage["title"],
                            "state": outage["state"], "revision": outage["revision"],
                            "affected_regions": json.loads(outage["affected_regions_json"])},
                 "facilities": [], "blockers": self.restore_blockers(outage_id)}
        for item in affected:
            facility = item["facility"]
            contacts = [dict(r) for r in self.conn.execute(
                "SELECT * FROM facility_contacts WHERE facility_id=? ORDER BY id", (facility["id"],))]
            note = notifications.get(facility["id"])
            events = [] if note is None else [dict(r) for r in self.conn.execute(
                "SELECT * FROM notification_events WHERE notification_id=? ORDER BY id DESC LIMIT 10", (note["id"],))]
            board["facilities"].append({
                "facility": facility, "asset": item["asset"], "contacts": contacts,
                "backup_ok": float(facility["backup_power_mw"]) >= float(facility["essential_load_mw"]),
                "contact_ok": any(c["reachable"] for c in contacts),
                "notification": note, "events": events})
        return board

    def restore_blockers(self, outage_id: int) -> list[dict]:
        """一级用户阻断项：备用电源不足、联系人失联、尚未回执。"""
        outage = self._outage(outage_id)
        affected = self.impact.affected_facilities(json.loads(outage["affected_regions_json"]))
        notifications = {row["facility_id"]: row for row in self.conn.execute(
            "SELECT * FROM notifications WHERE outage_id=? AND is_current=1", (outage_id,))}
        blockers = []
        for item in affected:
            facility = item["facility"]
            if int(facility["priority"]) != 1:
                continue
            reasons = []
            if float(facility["backup_power_mw"]) < float(facility["essential_load_mw"]):
                reasons.append("备用电源不足")
            reachable = self.conn.execute(
                "SELECT id FROM facility_contacts WHERE facility_id=? AND reachable=1 LIMIT 1",
                (facility["id"],)).fetchone()
            if not reachable:
                reasons.append("联系人失联")
            note = notifications.get(facility["id"])
            if note is None or note["state"] != "acknowledged":
                reasons.append("尚未回执")
            if reasons:
                blockers.append({"facility_id": facility["id"], "facility_name": facility["name"], "reasons": reasons})
        return blockers
