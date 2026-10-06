# 跨区域水资源使用权分配与转让（含河道污染预警禁调令）

仅用 Python 标准库实现的水权账户、计量、转让审批、干旱情景服务，以及河道污染预警时的**禁调令**：冻结受影响河段账户、暂停已批准转让、恢复时按原顺序补办。SQLite 持久化账户、取水记录、规则、禁调令与完整审计日志。

## 分层结构

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 存储 | `waterrights/storage.py` | 表结构、轻量迁移、全部 SQL；提供 `BEGIN IMMEDIATE` 事务 |
| 判定 | `waterrights/domain.py` | 账户、转让、取水、禁调令的全部业务规则，不碰 HTTP |
| 接口 | `waterrights/api.py` | HTTP 路由、身份头解析、JSON 序列化、409 回传当前状态 |
| 页面 | `static/index.html` | 按河段展示冻结账户、冻结额、待恢复顺位、失败原因 |
| 入口 | `app.py` | CLI；`Database`/`DomainError`/`seed_demo` 保持向后兼容 |

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 会在同一河段 `north-canal` 创建北区水库、河口灌区两个账户，并额外在 `west-canal` 创建西岸水厂（无禁调令时照旧运行），同时写入季节上限与最小留存规则。数据库默认 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## 禁调令规则

- **双向管制**：禁调令按河段发布。生效期内该河段水权账户既**不能转出**，也**不接受新的取水确认**；无禁调记录的账户一切照旧。
- **暂停 + 顺位**：发布禁调令时所有"已批准未划转"且任一方在冻结河段的转让转为 `suspended`；禁调期内批准的转让也立即暂停。双方可用量接口先返回**冻结额**（转出/转入）与**待恢复顺位**（按批准时间排序）。
- **恢复批处理**：`POST /api/freezes/{id}/recover` 按原顺位逐笔复核（额度、有效期、第三方留存都重新判定）后划转。某一顺位失败则本次停止，失败账户标记 `failed` 与失败原因；**重试只处理未完成账户，已解冻/已划转结果不重复处理**（接口幂等）。
- **并发只有一方写入**：
  - 两人同时给同一河段发令：数据库唯一索引 + `BEGIN IMMEDIATE` 保证只成功一条，另一方得到 `409`，响应体 `current` 字段带回当前禁调令状态，原记录不变。
  - 调度员当天修改恢复条件（`PATCH /api/freezes/{id}`）必须携带 `version` 乐观锁；旧版本提交得到 `409` 并看到当前版本与恢复日期。

## API

请求头 `X-User`、`X-Role` 模拟身份：`editor`（配额管理员/调度员）、`reviewer`、`meter`、`viewer`。

- `POST /api/accounts`：建立账户（额度、优先级、有效期、`reach` 河段，不传河段时取地区）。
- `POST /api/rules/season` / `/api/rules/impact`：季节上限、上下游最小留存。
- `POST /api/transfers`、`POST /api/transfers/{id}/approve|reject`：发起与审批；待审批金额立即预占。
- `POST /api/transfers/{id}/execute`：批准转让的正式划转（暂停转让双方解冻后也可由此补办）。
- `POST /api/usage`：计量取水；同一账户同一事件号只入账一次。
- `GET /api/accounts/{id}/available`：可用额度，含 `freeze` 视图（是否禁调、冻结转出/转入额、待恢复顺位）。
- `POST /api/freezes`：发布禁调令（`reach/reason/started_on/planned_recovery_on`）。
- `PATCH /api/freezes/{id}`：修改恢复条件（`version` 必填，可改 `planned_recovery_on`/`reason`）。
- `POST /api/freezes/{id}/recover`：恢复批处理，返回各账户状态、失败原因、本次补办的转让 ID。
- `GET /api/freezes` / `/api/freezes/{id}`：禁调看板，按河段列出冻结账户、待恢复顺序、失败原因。
- `GET /api/drought/simulate`、`GET /api/audit`：干旱模拟与审计日志。

并发写操作都在同一个 `BEGIN IMMEDIATE` 事务内完成"判定 + 写入"，因此并发提交不会绕过额度检查或禁调管制。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖原有转让审批/计量/规则流程，以及禁调令的：转出与取水双管制、已批准转让暂停与顺位显示、无令账户照旧、并发发令只写一方、恢复条件乐观锁冲突、恢复按顺序补办与幂等、部分失败后只重试未完成账户。
