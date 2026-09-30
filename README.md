# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 模块划分（规则 / 持久化 / 页面入口分开维护）

- `rules.py`：领域规则。阶段流转状态机、依据包有效性判定、证据摘要、乐观并发写入、`BusinessError`。不依赖 SQLite 或 HTTP。
- `store.py`：持久化层。`ProvenanceStore` 封装全部 SQLite 读写，包括依据包、封存、失效、任务与重试。
- `app.py`：HTTP 入口。`Handler` 只负责路由与参数解析，规则与持久化分别来自 `rules.py`、`store.py`。
- `web/index.html`：页面。建包、封存、查看失效原因、复审、任务进度与重试。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

### 审查依据包

- `POST /api/claims/{id}/packages`：审查员选取公开来源事件与内部证据组成依据包（草稿）。
- `GET /api/claims/{id}/packages`：列出该主张的依据包及有效性、受影响项。
- `GET /api/packages/{id}`：查看依据包详情（封存版本、证据摘要、失效原因）。
- `POST /api/packages/{id}/seal`：封存，记下藏品版本与证据摘要。
- `POST /api/packages/{id}/revalidate`：复审，按当前藏品/事件/证据重建草稿包。
- `GET /api/claims/{id}/jobs`、`GET /api/jobs/{id}`：查看封存/流转任务进度。
- `POST /api/jobs/{id}/retry`：重试，只续做未完成部分。

## 关键行为

- **封存并发**：两个人同时封存时，乐观锁（`WHERE version=? AND status='draft'`）保证先写入者生效，后到者返回 `conflict`。
- **立即失效**：藏品更新、来源事件增删、证据增删或主张流转导致藏品版本变化时，已封存依据包立即失效，并逐项列出受影响对象（藏品版本、事件、证据）。
- **流转挡住**：依据包失效后，未执行的主张流转被挡住（`basis_package_invalid`），响应列出受影响项；复审重建并封存后可继续流转。
- **失败保留与重试**：封存或流转失败后保留未完成项（任务落库），重试只续做未完成步骤；服务重启后任务进度照旧可查、可重试。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
