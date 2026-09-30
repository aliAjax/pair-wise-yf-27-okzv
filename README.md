# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张、
审查依据包（basis package）与主张流转作业，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 代码结构（规则 / 持久化 / 页面入口分开维护）

- `rules.py`：纯规则层。主张阶段机、依据包条目可选性（公开来源事件 + 内部证据）、封存前置条件、
  失效判定、作业项重试筛选，不依赖数据库与 HTTP。
- `store.py`：持久化层。SQLite 表结构、事务、并发写入（`BEGIN IMMEDIATE` + revision 乐观锁）、
  依据包失效联动、批量流转作业的落盘与续做。
- `app.py`：HTTP 入口层。协议解析、路由、白名单静态页面服务，不含业务规则。
- `web/index.html` + `web/app.js`：公开入口。
- `web/review.html` + `web/review.js`：审查员工作台（建包、补材料、封存、看失效原因与受影响项、复审、流转与重试）。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>，审查工作台在 <http://127.0.0.1:8103/review>。
数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 审查依据包规则

- 审查员只能选取**公开来源事件**和**内部证据**组成依据包；草稿阶段可继续补材料。
- 封存采用 `expected_revision` 乐观锁：两个人同时封存或补材料时，先写入者生效，
  后到者得到 `409 package_revision_conflict`（或 `package_sealed`）。
- 封存时记录**藏品版本**（`sealed_object_version`）与**证据摘要**（每条证据的 SHA-256）。
- 藏品、来源事件或证据变化后，该藏品所有已封存依据包**立即失效**，原因分别记为
  `object_version_changed` / `evidence_changed` / `evidence_summary_changed`；
  引用失效包、尚未执行完的主张流转项被置为 `blocked`，依据包详情列出受影响项。
- 失效包不能复活、不能再改；审查员点"复审"会按原条目另起一个草稿包，补材料后重新封存。
- 主张流转以批量作业执行：已完成项不重做，失败/挡住/待办项保留，重试只续做未完成部分；
  作业与每项状态全部持久化，服务重启后进度照旧可查。
- 主张阶段仍受阶段机约束：`submitted → under_review → negotiating → resolved_return/rejected`，
  不能跳跃、终态不可重开；且没有有效封存依据包时流转被挡住（`basis_invalid`）。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照（会使依据包失效）。
- `POST /api/sources`、`GET /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256（新证据会使依据包失效）。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：不依赖依据包的单次流转（仍受阶段机约束）。
- `GET/POST /api/objects/{id}/packages`：列出 / 创建依据包。
- `POST /api/packages/{id}/items`：补材料（带 `expected_revision`）。
- `POST /api/packages/{id}/seal`：封存（带 `expected_revision`，记录藏品版本与证据摘要）。
- `GET /api/packages/{id}`：依据包详情，含失效原因与未执行的受影响流转项。
- `POST /api/packages/{id}/review`：失效包复审，另起草稿包。
- `POST /api/packages/{id}/jobs`：依据封存包创建并执行主张流转作业。
- `GET /api/jobs/{id}`、`POST /api/jobs/{id}/retry`：查询与仅续做未完成项的重试。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张；每次对象变化都会保存 JSON 快照和审计记录。
