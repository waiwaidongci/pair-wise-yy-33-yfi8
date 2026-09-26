# 电网事故应急与恢复调度系统

标准库 Python 3.11+ + SQLite。系统管理停运事故、重要用户、备用容量、恢复步骤及安全依赖；接受现场离线报告并区分已合并、版本冲突和受保护记录，异常遥测单独隔离。

告警台账解决多起停电叠加时通知互相矛盾、换班后责任不清的问题。代码按三个关注点分处维护：

- `impact.py`：影响范围推算。仅按 `assets` 线路父子关系（父线停运波及全部子树）找受影响资产与重要用户，不读写通知状态。
- `alerts.py`：告警台账。建档去重、送达/重发/催办/回执、版本翻转、恢复发布闸门，全部规则集中在此。
- `app.py` 路由 + `static/index.html`、`static/app.js`：页面交互，只负责取数渲染和转发。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8215`。身份使用 `X-Actor` 和 `X-Role`，角色为 `dispatcher`、`operator`、`field`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/assets`、`POST /api/facilities`：登记线路资产和医院等重要用户。
- `POST /api/outages`：创建或幂等接收同一事故；带 `asset_id` 时按线路父子关系自动建告警台账。
- `GET /api/outages/{id}/scope`：查看推算出的影响范围（资产子树与重要用户）。
- `GET /api/outages/{id}/alerts`：按事故查看台账与操作留痕，页面据此“按事故看待送达”。
- `POST /api/outages/{id}/alerts/reconcile`：按最新线路关系补齐缺失台账（不重复、不覆盖）。
- `POST /api/alerts/{id}/send|resend|remind|ack`：发送、重发、催办、回执，均记录时间与操作人。
- `POST /api/alerts/{id}/contact`、`/backup`：现场上报联系人可达性、备用电源是否满足。
- `POST /api/telemetry`：记录并隔离错误遥测。
- `POST /api/plans`、`/submit`、`/approve`、`/activate`：创建、提交、审批并启用安全恢复计划。
- `POST /api/plans/{id}/change`：在不修改已确认步骤的前提下创建新计划版本。
- `POST /api/field-reports`：合并现场离线报告，重复客户端编号不会重复写入。
- `POST /api/plans/{id}/confirm`：调度员确认步骤，依赖未满足时拒绝。
- `POST /api/status`：发布当前恢复状态。
- `GET /api/plans/{id}`、`GET /api/state`、`GET /api/health`：详情、状态和健康检查。

## 告警台账规则

- 同一事故下同一对象只有一条台账（`UNIQUE(outage_id, facility_id)`）；全局同一对象只保留一条当前通知——叠加停电的新通知送达时，其他事故仍在流转的通知置 `superseded`（记录保留可追溯，可重发夺回当前状态），消除互相矛盾的通知。
- 台账状态：`pending`（待送达）→ `sent`（已送达）→ `acked`（已回执）；事故 `revision` 变动（建计划、启用、计划变更）时，已送达未回执的内容自动转 `pending_resend`，待按新版本重发。
- 一级用户存在“备用电源不足 / 联系人失联 / 尚未回执”任一情况时，`POST /api/status` 拒绝发布 `restored`（409，返回具体对象和原因）。
- 催办、回执及所有状态变更都写入 `alert_events`，带操作人与时间戳；台账另存最近一次送达/催办/回执的人和时间，换班后可查“谁确认过”。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_alerts.py` 覆盖父子关系推算、建档去重、叠加停电取代、版本翻转、催办/回执留痕和恢复发布闸门。

当前为原型：容量和依赖是静态安全模型，不包含潮流计算、SCADA/EMS 协议、实时遥测质量码或生产级多实例锁；离线合并通过客户端编号和计划版本完成。
