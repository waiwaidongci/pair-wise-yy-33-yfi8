# 电网事故应急与恢复调度系统

标准库 Python 3.11+ + SQLite。系统管理停运事故、重要用户、备用容量、恢复步骤及安全依赖；接受现场离线报告并区分已合并、版本冲突和受保护记录，异常遥测单独隔离。告警台账按线路父子关系推算受影响对象，同一对象只保留一条当前通知，事故版本变动时未回执通知转待重发，催办与回执均留操作人与时间。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8215`。身份使用 `X-Actor` 和 `X-Role`，角色为 `dispatcher`、`operator`、`field`。可用 `--port`、`--db` 覆盖。

## 模块

- `app.py`：HTTP 接口、调度流程、权限与审计。
- `impact.py`：影响范围推算，沿资产父子关系展开下游线路，找出受影响重要用户。
- `alarms.py`：告警台账，通知生成/重发/回执/催办与恢复完成阻断检查。
- `static/index.html`：页面交互，按事故看待送达。

## 主要接口

- `POST /api/assets`、`POST /api/facilities`：登记线路资产和医院等重要用户（含必需负荷 `essential_load_mw`）。
- `POST /api/facilities/{id}/contacts`：登记或更新联系人及可达状态。
- `POST /api/outages`：创建或幂等接收同一事故。
- `POST /api/outages/{id}/revise`：变更事故标题或影响区域，事故版本 +1，未回执通知自动转待重发。
- `POST /api/outages/{id}/notifications`：按影响范围生成通知；同一对象只留一条当前通知，未回执的按最新版本重发，退出影响范围的作废。
- `POST /api/notifications/{id}/urge`、`/ack`、`/resend`：催办、回执、重发，均记录操作人与时间；待重发的旧内容不能回执。
- `GET /api/outages/{id}/delivery`：按事故查看送达台账（备用电源、联系人、当前通知、留痕、阻断项）。
- `POST /api/telemetry`：记录并隔离错误遥测。
- `POST /api/plans`、`/submit`、`/approve`、`/activate`：创建、提交、审批并启用安全恢复计划。
- `POST /api/plans/{id}/change`：在不修改已确认步骤的前提下创建新计划版本。
- `POST /api/field-reports`：合并现场离线报告，重复客户端编号不会重复写入。
- `POST /api/plans/{id}/confirm`：调度员确认步骤，依赖未满足时拒绝。
- `POST /api/status`：发布当前恢复状态；一级用户备用电源不足、联系人失联或尚未回执时，拒绝发布恢复完成。
- `GET /api/plans/{id}`、`GET /api/state`、`GET /api/health`：详情、状态和健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：容量和依赖是静态安全模型，不包含潮流计算、SCADA/EMS 协议、实时遥测质量码或生产级多实例锁；离线合并通过客户端编号和计划版本完成；通知台账以事故版本号标识内容新旧，不接入真实短信/电话网关。
