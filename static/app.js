/* 页面交互层：只做取数渲染与按钮转发；影响范围推算与台账规则分别在 impact.py / alerts.py 维护。 */
const STATE_LABEL = {
  pending: "待送达", sent: "已送达", pending_resend: "待重发", acked: "已回执", superseded: "已被取代"
};
const EVENT_LABEL = {
  created: "建档", sent: "送达", resent: "重发", reminded: "催办", acked: "回执",
  contact_changed: "联系人状态变更", backup_changed: "备用电源变更", superseded: "被更新通知取代", stale: "版本变动转待重发"
};

function headers() {
  return {"Content-Type": "application/json", "X-Actor": actor.value, "X-Role": role.value};
}
function showError(msg) { error.textContent = msg || ""; }

async function api(path, body) {
  const res = await fetch(path, body ? {method: "POST", headers: headers(), body: JSON.stringify(body)}
                                     : {headers: {"X-Actor": actor.value, "X-Role": role.value}});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function loadState() {
  const out = await api("/api/state");
  state.hidden = false;
  state.textContent = JSON.stringify(out, null, 2);
  outageSel.innerHTML = "";
  for (const o of out.outages) {
    const opt = document.createElement("option");
    opt.value = o.id;
    opt.textContent = `${o.incident_code} rev${o.revision} ${o.title}`;
    outageSel.appendChild(opt);
  }
  if (outageSel.value) loadOutage();
}

function whoWhen(who, when) {
  if (!who) return '<span class="muted">—</span>';
  return `${who}<div class="muted">${when || ""}</div>`;
}

function actionButton(label, id, ev, payload, cls = "") {
  return `<button class="${cls}" onclick="doAction('${ev}',${id},${payload ? 1 : 0})">${label}</button>`;
}

async function loadOutage() {
  showError("");
  const id = outageSel.value;
  if (!id) return;
  try {
    const data = await api(`/api/outages/${id}/alerts`);
    outageTitle.textContent = `事故 ${data.incident_code}（版本 rev${data.revision}）— 按本事故看待送达`;
    const pending = data.alerts.filter(a => a.state === "pending").length;
    const resend = data.alerts.filter(a => a.state === "pending_resend").length;
    const unacked = data.alerts.filter(a => a.priority === 1 && a.state !== "acked").map(a => a.facility_name);
    summary.innerHTML = `共 ${data.alerts.length} 个受影响对象：待送达 ${pending}，待重发 ${resend}。` +
      (unacked.length ? ` <span class="bad">一级用户未回执，恢复完成不可发布：${unacked.join("、")}</span>`
                      : ' <span class="ok">一级用户均已回执。</span>');
    const tbody = alertTable.tBodies[0];
    tbody.innerHTML = "";
    for (const a of data.alerts) {
      const tr = document.createElement("tr");
      let buttons = "";
      if (a.state === "pending") buttons = actionButton("发送通知", a.id, "send", true);
      if (a.state === "sent") buttons = [
        actionButton("回执确认", a.id, "ack", true, "ghost"),
        actionButton("催办", a.id, "remind", true, "warn"),
        actionButton("联系人失联", a.id, "contact-lost", false, "ghost"),
        actionButton("备用不足", a.id, "backup-low", false, "ghost")
      ].join(" ");
      if (a.state === "pending_resend") buttons = actionButton("按新版本重发", a.id, "resend", true);
      if (a.state === "superseded") buttons = actionButton("重新送达", a.id, "resend", true, "ghost");
      if (a.state === "acked") buttons = '<span class="ok">闭环</span>';
      tr.innerHTML = `<td>${a.priority === 1 ? '<b>一级</b>' : a.priority + "级"}</td>` +
        `<td>${a.facility_name}</td>` +
        `<td><span class="tag s-${a.state}">${STATE_LABEL[a.state]}</span></td>` +
        `<td class="${a.backup_sufficient ? "ok" : "bad"}">${a.backup_sufficient ? "满足" : "不足"}</td>` +
        `<td class="${a.contact_reachable ? "ok" : "bad"}">${a.contact_reachable ? "可达" : "失联"}</td>` +
        `<td>${whoWhen(a.sent_by, a.sent_at)}</td>` +
        `<td>${whoWhen(a.reminded_by, a.reminded_at)}</td>` +
        `<td>${whoWhen(a.acked_by, a.acked_at)}</td>` +
        `<td>${buttons}</td>`;
      tbody.appendChild(tr);
    }
    alertTable.hidden = data.alerts.length === 0;
    events.innerHTML = data.events.length ? "" : "暂无";
    for (const e of data.events.slice().reverse()) {
      const div = document.createElement("div");
      div.innerHTML = `<span class="muted">${e.at}</span> <b>${EVENT_LABEL[e.event] || e.event}</b> ` +
        `台账#${e.alert_id} · ${e.actor}${e.note ? "：" + e.note : ""}`;
      events.appendChild(div);
    }
    window._alerts = data.alerts;
  } catch (err) { showError(err.message); }
}

async function doAction(ev, alertId, withNote) {
  showError("");
  const note = withNote ? prompt("备注（可留空，操作人/时间自动记录）", "") : null;
  if (note === null) return;
  let path, body = {note};
  if (ev === "send") path = "send";
  else if (ev === "resend") path = "resend";
  else if (ev === "remind") path = "remind";
  else if (ev === "ack") path = "ack";
  else if (ev === "contact-lost") { path = "contact"; body = {reachable: false, note: note || "电话无人接听"}; }
  else if (ev === "backup-low") { path = "backup"; body = {sufficient: false, note: note || "现场报告备用容量不足"}; }
  try {
    await api(`/api/alerts/${alertId}/${path}`, body);
    await loadOutage();
  } catch (err) { showError(err.message); }
}

async function reconcile() {
  showError("");
  try {
    await api(`/api/outages/${outageSel.value}/alerts/reconcile`, {});
    await loadOutage();
  } catch (err) { showError(err.message); }
}

loadState().catch(err => showError(err.message));
