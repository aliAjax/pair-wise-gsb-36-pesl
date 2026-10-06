# 跨区域水资源使用权分配与转让

仅使用 Python 标准库实现的水权账户、计量、转让审批、干旱情景和**河道污染禁调令**服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则、禁调令/恢复队列和完整审计日志。

## 分层

代码按关注点分四层，互不越界：

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 存储 | `storage.py` | 建表、迁移和纯 SQL 读写，不做业务判定 |
| 判定 | `freeze_policy.py` | 禁调覆盖、转出/取水拦截、额度/留存/季节上限等纯函数 |
| 接口 | `app.py` | HTTP 路由、身份角色、JSON 序列化、演示数据 |
| 页面 | `static/index.html` | 按河段展示冻结账户、待恢复顺位和失败原因 |

业务编排在 `service.py`（`Database` 门面，`app.py` 与测试通过它调用）。

## 运行

```bash
python3 app.py --init
python3 app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 创建三个示例账户：北区水库、河口灌区（河段「干流北段」）和西区水厂（支流西段，用于验证无禁调记录的账户不受影响）。数据库默认 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## 禁调令规则

污染预警发布后，受影响**河段**内的水权账户同时受到两类管控：

1. **管住转让和取水**：禁调期内不能发起/批准转出（`409`），也不接受新的取水确认；其他河段账户照旧运行。
2. **已批准转让暂停执行**：发布禁调令时把该河段已批准未执行的转出按原顺序（生效日、批准时间）挂起为 `suspended` 并编号；解除后恢复批处理按 `restore_seq` 原顺序补办。双方在可用量接口看到 `frozen_outgoing`/`frozen_incoming` 冻结额和 `restore_queue` 待恢复顺位。
3. **并发只有一方写入**：同一河段同时提交两份禁调令，只有一份成功（部分唯一索引 + `BEGIN IMMEDIATE`）；调度员当天改恢复条件用乐观版本号 `version`，输的一方保留自己的输入并在 `409` 响应的 `current` 字段看到当前状态。
4. **恢复批处理可重入**：只重试仍有未完成挂起转让的水权账户；已执行的转让和已恢复的账户直接跳过，额度绝不重复划转；同一账户按原顺序处理，首个失败会挡住该账户后续顺位并记录失败原因，下一次批处理重试该账户。
5. **看板按河段组织**：`GET /api/freeze/board` 按河段列出禁调令、冻结账户（冻结转出/转入额、重试次数、最近失败原因）、待恢复顺序和失败明细。

## API

请求头 `X-User` 和 `X-Role` 模拟身份；角色包括 `editor`、`reviewer`、`dispatcher`、`meter`、`viewer`。调度员可发布/解除禁调令并执行恢复批处理，也可审批/结算转让。

- `POST /api/accounts`：建立账户（额度、优先级、有效期、河段 `reach`）。
- `POST /api/rules/season` / `POST /api/rules/impact`：季节上限与上下游最小留存。
- `POST /api/transfers`：发起转让（待审批金额立即预占）。
- `POST /api/transfers/{id}/approve|reject`：审核；发起人不能自审。批准后额度只预占不划转，到生效日由结算执行；禁调期内批准会被拒绝并提示恢复后补办。
- `POST /api/transfers/settle`：结算批处理；到期的批准转让划转额度，禁调河段的已批准转让进入恢复队列。
- `POST /api/usage`：登记实际取水，同账户同事件编号只入账一次，禁调期内拒绝新确认。
- `GET /api/accounts/{id}/available`：可用额度、预占、冻结转出/转入额、禁调状态与恢复顺位。
- `POST /api/freeze`：发布禁调令（`reach/started_on/planned_end_on/condition_note/reason`），同时挂起存量已批准转让。
- `POST /api/freeze/{id}/conditions`：当天修改恢复条件（需带 `version`，版本冲突返回 `409` + `current`）。
- `POST /api/freeze/{id}/lift`：解除禁调令并自动执行恢复批处理。
- `POST /api/freeze/{id}/restore`：单独重跑恢复批处理（只处理未完成账户/转让，幂等）。
- `GET /api/freeze/board` / `GET /api/freeze/{id}`：按河段的看板与禁调令明细。
- `GET /api/drought/simulate?supply=1000&reduction=0.3`：干旱情景分配。
- `GET /api/audit`：完整操作审计。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖原有转让审批/计量/规则流程，以及禁调对转让与取水的拦截、已批准转让挂起与原顺序恢复、可用量冻结额和顺位、结算对禁调河段的捕获、恢复失败只重试未完成账户且不重复划转、并发禁调令单写入、恢复条件乐观版本冲突（返回当前状态）、HTTP 角色与页面。
