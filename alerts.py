"""告警台账：重要用户停电通知、催办、回执与恢复发布闸门。

职责边界：
- 影响对象来自 impact 模块（线路父子关系），本模块不重复推导拓扑；
- 页面交互在 app 路由与 static 脚本中维护，本模块只提供领域操作。

规则：
- 同一事故下同一对象只保留一条台账（UNIQUE(outage_id, facility_id)），
  且全局同一对象只有一条“当前通知”：新通知送达时，其他事故遗留的
  未闭环通知置 superseded，避免多起停电叠加时互相矛盾的通知并存；
- 事故版本变动（revision 增加）后，已送达未回执的内容转 pending_resend（待重发）；
- 一级用户备用电源不足、联系人失联或尚未回执时，事故不能发布恢复完成；
- 催办与回执都记录时间与操作人（alert_events 明细 + alerts 末态字段）。
"""
from __future__ import annotations

import sqlite3

from common import ApiError, now
from impact import affected_facilities

# 台账状态：待送达 / 已送达 / 待重发 / 已回执 / 已被更新通知取代
ALERT_STATES = {"pending", "sent", "pending_resend", "acked", "superseded"}


class AlertLedger:
    def __init__(self, conn: sqlite3.Connection, audit):
        self.conn = conn
        self.audit = audit  # Store.audit(actor, action, entity_type, entity_id, details)

    # ---- schema 与取数 -------------------------------------------------

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          outage_id INTEGER NOT NULL REFERENCES outages(id),
          facility_id INTEGER NOT NULL REFERENCES facilities(id),
          state TEXT NOT NULL CHECK(state IN ('pending','sent','pending_resend','acked','superseded')),
          outage_revision INTEGER NOT NULL,
          backup_sufficient INTEGER NOT NULL,
          contact_reachable INTEGER NOT NULL,
          sent_by TEXT, sent_at TEXT,
          reminded_by TEXT, reminded_at TEXT,
          acked_by TEXT, acked_at TEXT,
          created_by TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(outage_id, facility_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_facility ON alerts(facility_id);
        CREATE TABLE IF NOT EXISTS alert_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          alert_id INTEGER NOT NULL REFERENCES alerts(id),
          event TEXT NOT NULL CHECK(event IN
            ('created','sent','resent','reminded','acked','contact_changed','backup_changed','superseded','stale')),
          actor TEXT NOT NULL, at TEXT NOT NULL, note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_alert_events_alert ON alert_events(alert_id,id);
        """)
        self.conn.commit()

    def _get(self, alert_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            """SELECT a.*, f.name AS facility_name, f.priority AS facility_priority,
                      f.backup_power_mw, f.connected, o.incident_code
               FROM alerts a JOIN facilities f ON f.id=a.facility_id
               JOIN outages o ON o.id=a.outage_id WHERE a.id=?""",
            (alert_id,)).fetchone()
        if not row:
            raise ApiError(404, "告警台账记录不存在")
        return row

    def alert_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "outage_id": row["outage_id"], "incident_code": row["incident_code"],
                "facility_id": row["facility_id"], "facility_name": row["facility_name"],
                "priority": row["facility_priority"], "state": row["state"],
                "outage_revision": row["outage_revision"],
                "backup_sufficient": bool(row["backup_sufficient"]),
                "contact_reachable": bool(row["contact_reachable"]),
                "sent_by": row["sent_by"], "sent_at": row["sent_at"],
                "reminded_by": row["reminded_by"], "reminded_at": row["reminded_at"],
                "acked_by": row["acked_by"], "acked_at": row["acked_at"],
                "created_by": row["created_by"], "created_at": row["created_at"]}

    # ---- 建账（同一对象只留一条） --------------------------------------

    def ensure_for_outage(self, outage: sqlite3.Row, actor: str) -> list[int]:
        """按事故根资产的线路父子关系生成/补齐台账；已存在的不重复建档。"""
        if outage["asset_id"] is None:
            return []
        facilities = affected_facilities(self.conn, int(outage["asset_id"]))
        created: list[int] = []
        for facility in facilities:
            existing = self.conn.execute(
                "SELECT id FROM alerts WHERE outage_id=? AND facility_id=?",
                (outage["id"], facility["id"])).fetchone()
            if existing:
                continue
            cur = self.conn.execute(
                """INSERT INTO alerts(outage_id,facility_id,state,outage_revision,
                                      backup_sufficient,contact_reachable,created_by,created_at)
                   VALUES(?,?,'pending',?,?,1,?,?)""",
                (outage["id"], facility["id"], int(outage["revision"]),
                 int(float(facility["backup_power_mw"]) > 0), actor, now()))
            alert_id = int(cur.lastrowid)
            self._event(alert_id, "created", actor,
                        f"按线路父子关系建档（{facility['name']}）")
            created.append(alert_id)
        return created

    def mark_revision(self, outage_id: int, new_revision: int, reason: str) -> int:
        """事故版本变动：已送达但未回执的内容转待重发（ack/superseded/pending 不动）。"""
        rows = self.conn.execute(
            "SELECT id FROM alerts WHERE outage_id=? AND state='sent'", (outage_id,)).fetchall()
        for row in rows:
            self.conn.execute(
                "UPDATE alerts SET state='pending_resend',outage_revision=? WHERE id=? AND state='sent'",
                (new_revision, row["id"]))
            self._event(row["id"], "stale", "system", f"{reason}，未回执内容待重发（rev {new_revision}）")
        return len(rows)

    # ---- 送达 / 重发 ---------------------------------------------------

    def _deliver(self, alert_id: int, actor: str, event: str, note: str) -> sqlite3.Row:
        """公共送达逻辑：更新本记录为已送达，并把同一对象的其他当前通知置为 superseded。"""
        row = self._get(alert_id)
        revision = int(self.conn.execute("SELECT revision FROM outages WHERE id=?", (row["outage_id"],)).fetchone()[0])
        self.conn.execute(
            """UPDATE alerts SET state='sent',outage_revision=?,sent_by=?,sent_at=? WHERE id=?""",
            (revision, actor, now(), alert_id))
        # 同一对象只留一条当前通知：其他事故下仍在流转的通知全部取代
        self.conn.execute(
            """UPDATE alerts SET state='superseded'
               WHERE facility_id=? AND id<>? AND state IN ('sent','pending_resend')""",
            (row["facility_id"], alert_id))
        for old in self.conn.execute(
                "SELECT id FROM alerts WHERE facility_id=? AND id<>? AND state='superseded'",
                (row["facility_id"], alert_id)).fetchall():
            # 仅为本次刚被取代的旧记录补事件（已 superseded 的不再重复记）
            latest = self.conn.execute(
                "SELECT event FROM alert_events WHERE alert_id=? ORDER BY id DESC LIMIT 1",
                (old["id"],)).fetchone()
            if latest is None or latest["event"] != "superseded":
                self._event(old["id"], "superseded", actor,
                            f"同一对象收到更新通知（事故 {row['incident_code']}，台账#{alert_id}）")
        self._event(alert_id, event, actor, note)
        return self._get(alert_id)

    def send(self, actor: str | None, role: str | None, alert_id: int, note: str = "") -> dict:
        self._role(actor, role, {"dispatcher"})
        row = self._get(alert_id)
        if row["state"] != "pending":
            raise ApiError(409, "只有待送达的通知可以发送")
        with self.conn:
            row = self._deliver(alert_id, actor, "sent", note or "通知已送达")
            self.audit(actor, "alert.send", "alert", alert_id, {"outage_id": row["outage_id"], "facility": row["facility_name"]})
        return self.alert_dict(row)

    def resend(self, actor: str | None, role: str | None, alert_id: int, note: str = "") -> dict:
        self._role(actor, role, {"dispatcher"})
        row = self._get(alert_id)
        if row["state"] not in {"pending_resend", "superseded"}:
            raise ApiError(409, "只有待重发或已被取代的通知可以重发")
        with self.conn:
            row = self._deliver(alert_id, actor, "resent", note or "按最新事故版本重新送达")
            self.audit(actor, "alert.resend", "alert", alert_id, {"outage_id": row["outage_id"], "facility": row["facility_name"]})
        return self.alert_dict(row)

    # ---- 催办 / 回执 ---------------------------------------------------

    def remind(self, actor: str | None, role: str | None, alert_id: int, note: str = "") -> dict:
        self._role(actor, role, {"dispatcher"})
        row = self._get(alert_id)
        if row["state"] != "sent":
            raise ApiError(409, "只有已送达未回执的通知需要催办")
        with self.conn:
            self.conn.execute("UPDATE alerts SET reminded_by=?,reminded_at=? WHERE id=?",
                              (actor, now(), alert_id))
            self._event(alert_id, "reminded", actor, note or "电话/短信催办")
            self.audit(actor, "alert.remind", "alert", alert_id, {"facility": row["facility_name"], "note": note})
        return self.alert_dict(self._get(alert_id))

    def ack(self, actor: str | None, role: str | None, alert_id: int, note: str = "") -> dict:
        self._role(actor, role, {"dispatcher", "operator"})
        row = self._get(alert_id)
        if row["state"] != "sent":
            raise ApiError(409, "只有已送达的通知可以回执（若已被取代请先重发）")
        with self.conn:
            self.conn.execute(
                "UPDATE alerts SET state='acked',acked_by=?,acked_at=?,contact_reachable=1 WHERE id=?",
                (actor, now(), alert_id))  # 收到回执即证明联系人可达
            self._event(alert_id, "acked", actor, note or "用户确认收到")
            self.audit(actor, "alert.ack", "alert", alert_id, {"facility": row["facility_name"], "note": note})
        return self.alert_dict(self._get(alert_id))

    # ---- 现场上报：联系人 / 备用电源 -----------------------------------

    def set_contact(self, actor: str | None, role: str | None, alert_id: int, reachable: bool, note: str = "") -> dict:
        self._role(actor, role, {"operator", "dispatcher"})
        row = self._get(alert_id)
        if row["state"] == "acked" and not reachable:
            raise ApiError(409, "已回执的通知说明联系人可达，不能改标记为失联")
        with self.conn:
            self.conn.execute("UPDATE alerts SET contact_reachable=? WHERE id=?", (int(reachable), alert_id))
            self._event(alert_id, "contact_changed", actor,
                        ("联系人已取得联系" if reachable else "联系人失联") + (f"：{note}" if note else ""))
            self.audit(actor, "alert.contact", "alert", alert_id, {"reachable": reachable})
        return self.alert_dict(self._get(alert_id))

    def set_backup(self, actor: str | None, role: str | None, alert_id: int, sufficient: bool, note: str = "") -> dict:
        self._role(actor, role, {"operator", "dispatcher"})
        self._get(alert_id)
        with self.conn:
            self.conn.execute("UPDATE alerts SET backup_sufficient=? WHERE id=?", (int(sufficient), alert_id))
            self._event(alert_id, "backup_changed", actor,
                        ("备用电源满足要求" if sufficient else "一级用户备用电源不足") + (f"：{note}" if note else ""))
            self.audit(actor, "alert.backup", "alert", alert_id, {"sufficient": sufficient})
        return self.alert_dict(self._get(alert_id))

    # ---- 恢复发布闸门 --------------------------------------------------

    def restore_blockers(self, outage_id: int) -> list[dict]:
        """返回阻止该事故发布“恢复完成”的一级用户告警明细。

        阻断条件（任一）：备用电源不足 / 联系人失联 / 尚未回执。
        按当前影响范围实时计算：范围内一级用户若尚无台账也视为未通知、未回执。
        """
        outage = self.conn.execute("SELECT * FROM outages WHERE id=?", (outage_id,)).fetchone()
        if not outage:
            raise ApiError(404, "事故不存在")
        if outage["asset_id"] is None:
            return []
        facilities = affected_facilities(self.conn, int(outage["asset_id"]))
        blockers: list[dict] = []
        for facility in facilities:
            if int(facility["priority"]) != 1:
                continue
            row = self.conn.execute("SELECT * FROM alerts WHERE outage_id=? AND facility_id=?",
                                    (outage_id, facility["id"])).fetchone()
            reasons: list[str] = []
            if row is None:
                # 范围内一级用户却没有台账：既没有送达也没有回执
                if float(facility["backup_power_mw"]) <= 0:
                    reasons.append("备用电源不足")
                reasons.append("尚未回执")
            else:
                if not row["backup_sufficient"]:
                    reasons.append("备用电源不足")
                if not row["contact_reachable"]:
                    reasons.append("联系人失联")
                if row["state"] != "acked":
                    reasons.append("尚未回执")
            if reasons:
                blockers.append({"facility_id": facility["id"], "facility": facility["name"],
                                 "priority": 1, "reasons": reasons,
                                 "alert_id": None if row is None else row["id"],
                                 "alert_state": None if row is None else row["state"]})
        return blockers

    # ---- 查询（页面按事故看待送达） ------------------------------------

    def list_for_outage(self, outage_id: int) -> dict:
        outage = self.conn.execute("SELECT * FROM outages WHERE id=?", (outage_id,)).fetchone()
        if not outage:
            raise ApiError(404, "事故不存在")
        rows = self.conn.execute(
            """SELECT a.*, f.name AS facility_name, f.priority AS facility_priority,
                      f.backup_power_mw, f.connected, o.incident_code
               FROM alerts a JOIN facilities f ON f.id=a.facility_id
               JOIN outages o ON o.id=a.outage_id
               WHERE a.outage_id=? ORDER BY f.priority,a.id""", (outage_id,)).fetchall()
        alerts = [self.alert_dict(r) for r in rows]
        events = [dict(r) for r in self.conn.execute(
            """SELECT e.id,e.alert_id,e.event,e.actor,e.at,e.note FROM alert_events e
               JOIN alerts a ON a.id=e.alert_id WHERE a.outage_id=? ORDER BY e.id""",
            (outage_id,)).fetchall()]
        return {"outage_id": outage_id, "incident_code": outage["incident_code"],
                "revision": outage["revision"], "alerts": alerts, "events": events}

    def all_alerts(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT a.*, f.name AS facility_name, f.priority AS facility_priority,
                      f.backup_power_mw, f.connected, o.incident_code
               FROM alerts a JOIN facilities f ON f.id=a.facility_id
               JOIN outages o ON o.id=a.outage_id ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
        return [self.alert_dict(r) for r in rows]

    # ---- 内部 ----------------------------------------------------------

    def _event(self, alert_id: int, event: str, actor: str, note: str) -> None:
        self.conn.execute("INSERT INTO alert_events(alert_id,event,actor,at,note) VALUES(?,?,?,?,?)",
                          (alert_id, event, actor, now(), note))

    @staticmethod
    def _role(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor:
            raise ApiError(401, "缺少身份")
        if role not in allowed:
            raise ApiError(403, "角色无权执行此操作")
        return actor
