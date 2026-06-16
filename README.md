# 交付批次验收 JSON API

本地交付批次验收管理系统，支持创建批次、导入 CSV/JSON 清单、规则校验、驳回返修、审批归档、报告导出全流程。

## 技术栈

- **后端框架**: FastAPI 0.115
- **数据库**: SQLite（本地文件，无需额外服务）
- **ORM**: SQLAlchemy 2.0
- **数据校验**: Pydantic 2.9
- **运行服务器**: Uvicorn

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 3. 访问文档
# Swagger UI:  http://localhost:8000/docs
# ReDoc:       http://localhost:8000/redoc
# 健康检查:    http://localhost:8000/health
```

服务启动后会自动创建数据库 `delivery_acceptance.db` 并初始化种子数据。

## 预置用户（本地简化版认证）

系统通过 HTTP Header `X-User-Id` 识别用户，启动时预置 6 个用户：

| ID | Username | 角色 | 说明 |
|----|----------|------|------|
| 1 | admin | admin | 系统管理员，所有权限 |
| 2 | lead_wang | lead | 王组长，可通过/归档 |
| 3 | reviewer_li | reviewer | 李评审，可驳回 |
| 4 | reviewer_zhang | reviewer | 张评审，可驳回 |
| 5 | submitter_chen | submitter | 陈交付，可创建批次/导入清单 |
| 6 | submitter_zhao | submitter | 赵交付，可创建批次/导入清单 |

## 状态流转图

```
  [草稿] draft
      │
      ▼  submitter 提交
  [待验收] pending_review ──────┐
      │                         │
      ▼ reviewer 驳回           │ lead 通过
  [部分驳回] partially_rejected │
      │                         │
      ▼ submitter 开始返修      │
  [返修中] repairing            │
      │                         │
      ▼ 导入修订版清单          │
  (回到草稿→再提交) ────────────┘
      │
      ▼
  [已通过] approved
      │
      ▼ lead 归档
  [已归档] archived
```

**非法流转**（如直接从草稿→已通过）会被明确拒绝，不产生半截数据。

---

## 完整可复现 CURL 链路测试

### 0. 环境变量准备

```bash
export API=http://localhost:8000
# Windows PowerShell:  $API = "http://localhost:8000"
```

---

### 主流程：创建批次 → 导入清单 → 校验 → 驳回 → 返修 → 再校验 → 通过 → 归档 → 导出报告

#### 步骤 1：查看系统预置用户
```bash
curl -X GET "$API/api/users/" -H "X-User-Id: 1"
```

#### 步骤 2：创建交付批次（submitter_chen，用户ID=5）
```bash
curl -X POST "$API/api/batches/" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 5" \
  -d '{
    "batch_code": "BATCH-2026-Q2-001",
    "name": "2026年Q2服务器配件交付批次",
    "description": "包含主板、内存、硬盘、电源等服务器核心配件",
    "submitter_id": 5
  }'
```
**预期输出**: 返回 status 为 "draft" 的批次信息，记下返回的 `id`。
```bash
# 假设返回 id=1，保存到变量
export BATCH_ID=1
# PowerShell: $BATCH_ID = 1
```

#### 步骤 3：导入第一版清单（有错误的样例，验证失败路径）
```bash
curl -X POST "$API/api/batches/$BATCH_ID/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_with_errors.csv;type=text/csv" \
  -F "import_format=auto"
```
**预期输出**: `success=false`，指出各条目的错误（缺字段、负数、格式不对等），但**不覆盖旧清单**（此时也没有旧清单）。

#### 步骤 4：导入正确的 v1 清单
```bash
curl -X POST "$API/api/batches/$BATCH_ID/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_good.csv;type=text/csv" \
  -F "import_format=auto"
```
**预期输出**: `success=true`, `version_number=1`, `item_count=5`

#### 步骤 5：查看最新清单内容
```bash
curl -X GET "$API/api/batches/$BATCH_ID/manifests/latest" -H "X-User-Id: 1"
```

#### 步骤 6：执行规则校验（逐项跑 9 条预置规则）
```bash
curl -X POST "$API/api/batches/$BATCH_ID/validate" -H "X-User-Id: 5"
```
**预期输出**: 包含 `validation_summary`，其中 `validation_passed=true`（样例数据无错误）

#### 步骤 7：查看校验结果（只看失败项，应为空）
```bash
curl -X GET "$API/api/batches/$BATCH_ID/validation-results?only_failed=true" -H "X-User-Id: 1"
```

#### 步骤 8：submitter 提交待验收（状态 draft → pending_review）
```bash
curl -X POST "$API/api/batches/$BATCH_ID/transition" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 5" \
  -d '{
    "target_status": "pending_review",
    "comment": "清单已完成初检，请评审验收"
  }'
```

#### 步骤 9：reviewer 驳回部分条目（状态 pending_review → partially_rejected）
这里 reviewer_li（用户ID=3）认为 ITEM-002 和 ITEM-005 有问题：
```bash
curl -X POST "$API/api/batches/$BATCH_ID/reject" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 3" \
  -d '{
    "comment": "发现 2 项问题，请返修后重新提交",
    "rejections": [
      {
        "item_key": "ITEM-002",
        "rejection_reason": "内存条描述中未明确标注 ECC 校验支持，需补充规格说明"
      },
      {
        "item_key": "ITEM-001",
        "rejection_reason": "主板未提供 BIOS 兼容性测试报告，需补充固件版本说明"
      }
    ]
  }'
```
**预期输出**: 记录 2 条驳回，状态变为 `partially_rejected`

#### 步骤 10：查看驳回记录
```bash
curl -X GET "$API/api/batches/$BATCH_ID/rejections" -H "X-User-Id: 1"
```

#### 步骤 11：submitter 开始返修（状态 partially_rejected → repairing）
```bash
curl -X POST "$API/api/batches/$BATCH_ID/start-repair" \
  -H "X-User-Id: 5" \
  --data-urlencode "comment=收到驳回意见，开始修订清单"
```

#### 步骤 12：导入修订版 v2 清单（自动关联并标记旧驳回为已解决）
```bash
curl -X POST "$API/api/batches/$BATCH_ID/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_repaired_v2.csv;type=text/csv" \
  -F "import_format=auto"
```
**预期输出**: `version_number=2`, `item_count=7`

#### 步骤 13：验证驳回记录已被自动标记为 resolved
```bash
curl -X GET "$API/api/batches/$BATCH_ID/rejections" -H "X-User-Id: 1"
```
预期 2 条驳回的 `resolved=true`，且关联到 v2

#### 步骤 14：对 v2 重新跑校验
```bash
curl -X POST "$API/api/batches/$BATCH_ID/validate" -H "X-User-Id: 5"
```

#### 步骤 15：submitter 再次提交待验收
```bash
curl -X POST "$API/api/batches/$BATCH_ID/transition" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 5" \
  -d '{
    "target_status": "pending_review",
    "comment": "已修复 2 项驳回问题，新增 ITEM-006/ITEM-007，请重新验收"
  }'
```

#### 步骤 16：lead 验证通过（状态 pending_review → approved）
**先看失败路径 - 用 reviewer_li (ID=3) 尝试通过（权限不足）：**
```bash
curl -X POST "$API/api/batches/$BATCH_ID/approve" \
  -H "X-User-Id: 3" \
  --data-urlencode "comment=验收通过"
```
**预期输出**: `403 Forbidden` - reviewer 无权通过，只能 lead 才行

**再用 lead_wang (ID=2) 执行：**
```bash
curl -X POST "$API/api/batches/$BATCH_ID/approve" \
  -H "X-User-Id: 2" \
  --data-urlencode "comment=v2 验收通过，所有规格符合要求，可以交付"
```
**预期输出**: 状态变为 `approved`，返回审批人、时间等信息

#### 步骤 17：lead 归档（状态 approved → archived）
**先看失败路径 - 用 submitter (ID=5) 尝试归档：**
```bash
curl -X POST "$API/api/batches/$BATCH_ID/archive" \
  -H "X-User-Id: 5" \
  --data-urlencode "comment=归档"
```
**预期输出**: `403 Forbidden`

**再用 lead_wang (ID=2) 归档：**
```bash
curl -X POST "$API/api/batches/$BATCH_ID/archive" \
  -H "X-User-Id: 2" \
  --data-urlencode "comment=批次完成交付，正式归档"
```

#### 步骤 18：查询版本历史（验证持久化：重启服务后数据仍在）
```bash
curl -X GET "$API/api/batches/$BATCH_ID/version-history" -H "X-User-Id: 1"
```
预期能看到 v1 和 v2 两个版本的完整信息

#### 步骤 19：查询审批日志（完整审计链）
```bash
curl -X GET "$API/api/batches/$BATCH_ID/approval-logs" -H "X-User-Id: 1"
```
预期看到 CREATE → IMPORT_MANIFEST → VALIDATE → STATUS_TRANSITION → REJECT → START_REPAIR → IMPORT_MANIFEST → VALIDATE → STATUS_TRANSITION → APPROVE → ARCHIVE 的完整链路

#### 步骤 20：导出验收报告
```bash
# 查看结构化报告
curl -X GET "$API/api/batches/$BATCH_ID/acceptance-report" -H "X-User-Id: 1"

# 下载 JSON 格式报告文件
curl -X GET "$API/api/batches/$BATCH_ID/export-report?format=json" \
  -H "X-User-Id: 1" \
  -o "acceptance_report_BATCH-2026-Q2-001.json"
```

---

### 失败路径专项测试

#### A. 非法状态流转（如从 archived 改回 approved）
```bash
curl -X POST "$API/api/batches/$BATCH_ID/transition" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 1" \
  -d '{
    "target_status": "approved",
    "comment": "试图改回已通过"
  }'
```
**预期**: 400 Bad Request，明确指出不允许的流转路径

#### B. 重复导入同一文件（不会产生半截数据，v3 正常创建）
```bash
curl -X POST "$API/api/batches/1/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_good.csv;type=text/csv"
```
如果批次状态允许导入，会创建 v3，否则明确拒绝

#### C. 已归档批次禁止更新
```bash
curl -X PATCH "$API/api/batches/$BATCH_ID" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 1" \
  -d '{"name": "尝试修改已归档批次"}'
```
**预期**: 400 Bad Request

#### D. 清单缺字段（指出条目和行号）
```bash
# 创建新批次
curl -X POST "$API/api/batches/" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 5" \
  -d '{"batch_code":"BATCH-TEST-MISSING","name":"缺字段测试","submitter_id":5}'
# 记返回 id，然后导入缺字段文件
curl -X POST "$API/api/batches/2/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_with_errors.csv;type=text/csv"
```
预期 `success=false`，`errors` 数组中每条都包含 `line_number`、`item_key`、`field_name`、`error_message`

---

## API 快速索引

| 方法 | 路径 | 说明 | 需要角色 |
|------|------|------|----------|
| POST | `/api/users/` | 创建用户 | 公开 |
| GET | `/api/users/` | 用户列表 | 所有 |
| POST | `/api/batches/` | 创建批次 | submitter/admin |
| GET | `/api/batches/` | 批次列表 | 所有 |
| GET | `/api/batches/{id}` | 批次详情 | 所有 |
| POST | `/api/batches/{id}/manifests/import` | 导入清单(CSV/JSON) | submitter/admin |
| GET | `/api/batches/{id}/manifests/` | 版本历史 | 所有 |
| GET | `/api/batches/{id}/manifests/latest` | 最新清单 | 所有 |
| POST | `/api/batches/{id}/validate` | 执行校验 | 所有 |
| GET | `/api/batches/{id}/validation-results` | 校验结果 | 所有 |
| POST | `/api/batches/{id}/transition` | 状态流转 | 按状态而定 |
| POST | `/api/batches/{id}/reject` | 驳回问题项 | reviewer+ |
| POST | `/api/batches/{id}/start-repair` | 开始返修 | submitter/admin |
| POST | `/api/batches/{id}/approve` | 通过验收 | lead/admin |
| POST | `/api/batches/{id}/archive` | 归档批次 | lead/admin |
| GET | `/api/batches/{id}/rejections` | 驳回记录 | 所有 |
| GET | `/api/batches/{id}/approval-logs` | 审批日志 | 所有 |
| GET | `/api/batches/{id}/acceptance-report` | 验收报告 | 所有 |
| GET | `/api/batches/{id}/export-report` | 导出报告文件 | 所有 |
| POST | `/api/validation-rules` | 新增校验规则 | admin |
| GET | `/api/validation-rules` | 规则列表 | 所有 |

## 预置校验规则（9 条）

| 规则代码 | 类型 | 目标字段 | 说明 |
|----------|------|----------|------|
| REQ_ITEM_ID | required | item_id | 必填 |
| REQ_ITEM_NAME | required | item_name | 必填 |
| REQ_QUANTITY | required | quantity | 必填 |
| REQ_UNIT_PRICE | required | unit_price | 必填 |
| TYPE_QUANTITY_INT | positive_integer | quantity | 正整数 |
| TYPE_PRICE_POSITIVE | positive_number | unit_price | 正数 |
| RANGE_QUANTITY | range | quantity | 1~10000 之间 (warning) |
| FORMAT_ITEM_ID | pattern | item_id | 以 ITEM- 开头 (warning) |
| CALC_TOTAL_AMOUNT | calculation | total_amount | = quantity * unit_price |

## 数据持久化验证

停止服务再重启后，执行以下命令验证数据不丢失：

```bash
# 重启服务后
curl -X GET "$API/api/batches/1/version-history" -H "X-User-Id: 1"
curl -X GET "$API/api/batches/1/approval-logs" -H "X-User-Id: 1"
curl -X GET "$API/api/batches/1/acceptance-report" -H "X-User-Id: 1"
```

数据应与重启前完全一致，所有版本、驳回记录、审批日志、校验结果均持久化在 SQLite 文件 `delivery_acceptance.db` 中。
