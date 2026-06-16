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

环境变量（可选）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PRECHECK_TOKEN_TTL_SECONDS` | `1800` | 预检查令牌有效期（秒），测试时可设为极短值 |

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

## 清单导入预检查流程（必读）

**所有清单导入必须先做预检查，再携带预检查令牌执行正式导入。** 直接调用导入接口而不提供 `precheck_token` 会返回 `400` 错误。

### 为什么要预检查？

预检查在真正写入数据之前，提前告知你：

| 预检查结果 `action_type` | 含义 | `can_import` |
|--------------------------|------|--------------|
| `NEW_VERSION` | 清单内容与历史版本不同，将创建新版本 | `true` |
| `REUSE_VERSION` | 清单内容与某个历史版本完全一致，将复用旧版本（不产生新版本号） | `true` |
| `CONFLICT` | 存在阻塞性冲突（如批次状态不允许导入，或解析失败），无法导入 | `false` |

此外，预检查还会检查是否存在**非阻塞性冲突**（`severity: "warning"`），例如"存在 N 条未解决的驳回记录"——这类冲突不阻止导入，但提醒你确认。

### 完整操作步骤

```
步骤 1  POST /api/batches/{batch_id}/manifests/precheck
        → 上传文件，获取 precheck_token 和预检查结论

步骤 2  查看预检查结论：
        - action_type = NEW_VERSION 或 REUSE_VERSION → can_import=true，可继续
        - action_type = CONFLICT → can_import=false，先解决冲突再重新预检查
        - 有 warning 级冲突 → 注意提醒内容，确认后仍可导入

步骤 3  POST /api/batches/{batch_id}/manifests/import
        → 携带同一文件 + precheck_token 执行正式导入
```

### 权限要求

- **预检查**：仅批次提交人（submitter）或管理员（admin）可执行
- **正式导入**：仅预检查令牌的生成者本人可使用（管理员对他人批次做的预检查，提交人也无法使用该 token）

### 令牌安全机制

| 校验项 | 说明 |
|--------|------|
| 必须提供 | 不提供 `precheck_token` 直接 400 |
| 有效性 | token 不存在 → 400 |
| 归属校验 | token 只能由生成者本人使用 → 403 |
| 批次匹配 | token 只能用于对应批次 → 400 |
| 有效期 | 默认 30 分钟，过期 → 400 |
| 一次性 | 使用后即失效，不可重复使用 → 400 |
| 内容一致性 | 导入时的文件内容必须与预检查时一致（SHA-256 哈希校验）→ 400 |
| 冲突结论 | action_type=CONFLICT 的令牌不允许导入 → 400 |

### 查询预检查记录

| 接口 | 说明 |
|------|------|
| `GET /api/batches/{batch_id}/manifests/prechecks/latest` | 查看最近一次预检查结果 |
| `GET /api/batches/{batch_id}/manifests/prechecks` | 列出所有预检查记录 |
| `GET /api/batches/{batch_id}/approval-logs` | 审批日志中 `action=PRECHECK_IMPORT` 可追溯预检查操作 |

---

## 完整可复现 CURL 链路测试

### 0. 环境变量准备

```bash
export API=http://localhost:8000
# Windows PowerShell:  $API = "http://localhost:8000"
```

---

### 主流程：创建批次 → 预检查 → 导入清单 → 校验 → 驳回 → 返修 → 再导入 → 通过 → 归档

#### 步骤 1：查看系统预置用户
```bash
curl -s -X GET "$API/api/users/" -H "X-User-Id: 1"
```

#### 步骤 2：创建交付批次（submitter_chen，用户ID=5）
```bash
curl -s -X POST "$API/api/batches/" \
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
export BATCH_ID=1
# PowerShell: $BATCH_ID = 1
```

#### 步骤 3：预检查有错误的清单（验证失败路径）
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/precheck" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_with_errors.csv;type=text/csv" \
  -F "import_format=auto"
```
**预期输出**: `success=true`, `action_type=CONFLICT`, `can_import=false`，`parse_errors` 中列出各条目的错误（缺字段、负数等）。

> 注意：即使预检查返回 `can_import=false`，你**不能**用这个 token 执行导入。必须先修复文件，重新做预检查。

#### 步骤 4：预检查正确的 v1 清单
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/precheck" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_good.csv;type=text/csv" \
  -F "import_format=auto"
```
**预期输出**: `success=true`, `action_type=NEW_VERSION`, `can_import=true`, `planned_version_number=1`

关键字段解读：
- `action_type: "NEW_VERSION"` → 清单内容与历史版本不同，将创建新版本
- `reasons` 列表会明确提示"将创建新版本"
- `precheck_token` → 记下此值，下一步必须携带

```bash
# 记下返回的 precheck_token，例如：
export TOKEN_V1="返回的 precheck_token 值"
```

#### 步骤 5：携带 precheck_token 正式导入 v1 清单
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_good.csv;type=text/csv" \
  -F "import_format=auto" \
  -F "precheck_token=$TOKEN_V1"
```
**预期输出**: `success=true`, `version_number=1`, `item_count=5`

> 重要：`file` 必须与预检查时完全一致（内容哈希校验），`precheck_token` 必须是本次预检查返回的令牌。

#### 步骤 6：查看最新清单内容
```bash
curl -s -X GET "$API/api/batches/$BATCH_ID/manifests/latest" -H "X-User-Id: 1"
```

#### 步骤 7：执行规则校验（逐项跑 9 条预置规则）
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/validate" -H "X-User-Id: 5"
```
**预期输出**: 包含 `validation_summary`，其中 `validation_passed=true`（样例数据无错误）

#### 步骤 8：查看校验结果（只看失败项，应为空）
```bash
curl -s -X GET "$API/api/batches/$BATCH_ID/validation-results?only_failed=true" -H "X-User-Id: 1"
```

#### 步骤 9：submitter 提交待验收（状态 draft → pending_review）
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/transition" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 5" \
  -d '{
    "target_status": "pending_review",
    "comment": "清单已完成初检，请评审验收"
  }'
```

#### 步骤 10：reviewer 驳回部分条目（状态 pending_review → partially_rejected）
这里 reviewer_li（用户ID=3）认为 ITEM-002 和 ITEM-005 有问题：
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/reject" \
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

#### 步骤 11：查看驳回记录
```bash
curl -s -X GET "$API/api/batches/$BATCH_ID/rejections" -H "X-User-Id: 1"
```

#### 步骤 12：submitter 开始返修（状态 partially_rejected → repairing）
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/start-repair" \
  -H "X-User-Id: 5" \
  --data-urlencode "comment=收到驳回意见，开始修订清单"
```

#### 步骤 13：预检查修订版 v2 清单（会看到未解决驳回的 WARNING 提醒）
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/precheck" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_repaired_v2.csv;type=text/csv" \
  -F "import_format=auto"
```
**预期输出**:
- `action_type: "NEW_VERSION"` → 内容有变化，将创建新版本
- `has_conflict: true` → 有冲突（但非阻塞）
- `can_import: true` → 可以导入
- `conflicts` 中包含 `severity: "warning"`, `conflict_type: "UNRESOLVED_REJECTIONS"` → 提醒有 2 条未解决驳回
- `reasons` 中提示"存在 2 条未解决的驳回记录"

```bash
export TOKEN_V2="返回的 precheck_token 值"
```

#### 步骤 14：携带 precheck_token 正式导入 v2 清单（自动解决驳回）
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_repaired_v2.csv;type=text/csv" \
  -F "import_format=auto" \
  -F "precheck_token=$TOKEN_V2"
```
**预期输出**: `success=true`, `version_number=2`, `item_count=7`，导入后旧驳回自动标记为已解决

#### 步骤 15：验证驳回记录已被自动标记为 resolved
```bash
curl -s -X GET "$API/api/batches/$BATCH_ID/rejections" -H "X-User-Id: 1"
```
预期 2 条驳回的 `resolved=true`，且关联到 v2

#### 步骤 16：对 v2 重新跑校验
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/validate" -H "X-User-Id: 5"
```

#### 步骤 17：submitter 再次提交待验收
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/transition" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 5" \
  -d '{
    "target_status": "pending_review",
    "comment": "已修复 2 项驳回问题，新增 ITEM-006/ITEM-007，请重新验收"
  }'
```

#### 步骤 18：lead 验证通过（状态 pending_review → approved）
**先看失败路径 - 用 reviewer_li (ID=3) 尝试通过（权限不足）：**
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/approve" \
  -H "X-User-Id: 3" \
  --data-urlencode "comment=验收通过"
```
**预期输出**: `403 Forbidden` - reviewer 无权通过，只能 lead 才行

**再用 lead_wang (ID=2) 执行：**
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/approve" \
  -H "X-User-Id: 2" \
  --data-urlencode "comment=v2 验收通过，所有规格符合要求，可以交付"
```
**预期输出**: 状态变为 `approved`，返回审批人、时间等信息

#### 步骤 19：lead 归档（状态 approved → archived）
**先看失败路径 - 用 submitter (ID=5) 尝试归档：**
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/archive" \
  -H "X-User-Id: 5" \
  --data-urlencode "comment=归档"
```
**预期输出**: `403 Forbidden`

**再用 lead_wang (ID=2) 归档：**
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/archive" \
  -H "X-User-Id: 2" \
  --data-urlencode "comment=批次完成交付，正式归档"
```

#### 步骤 20：查询版本历史（验证持久化：重启服务后数据仍在）
```bash
curl -s -X GET "$API/api/batches/$BATCH_ID/version-history" -H "X-User-Id: 1"
```
预期能看到 v1 和 v2 两个版本的完整信息

#### 步骤 21：查询审批日志（完整审计链）
```bash
curl -s -X GET "$API/api/batches/$BATCH_ID/approval-logs" -H "X-User-Id: 1"
```
预期看到 CREATE → PRECHECK_IMPORT → IMPORT_MANIFEST → VALIDATE → STATUS_TRANSITION → REJECT → START_REPAIR → PRECHECK_IMPORT → IMPORT_MANIFEST → VALIDATE → STATUS_TRANSITION → APPROVE → ARCHIVE 的完整链路

#### 步骤 22：导出验收报告
```bash
curl -s -X GET "$API/api/batches/$BATCH_ID/acceptance-report" -H "X-User-Id: 1"

curl -s -X GET "$API/api/batches/$BATCH_ID/export-report?format=json" \
  -H "X-User-Id: 1" \
  -o "acceptance_report_BATCH-2026-Q2-001.json"
```

---

### 失败路径专项测试

#### A. 直接导入不提供 precheck_token（必返回 400）
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_good.csv;type=text/csv" \
  -F "import_format=auto"
```
**预期**: `400 Bad Request`，错误信息明确指出"缺少 precheck_token，请先执行导入预检查"

#### B. 重复导入完全相同内容（预检查识别 REUSE_VERSION）
在步骤 14 导入 v2 之后，再次预检查+导入完全相同的文件：
```bash
# 先做预检查
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/precheck" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_repaired_v2.csv;type=text/csv" \
  -F "import_format=auto"
```
**预期输出**: `action_type=REUSE_VERSION`, `reused_version_number=2`, `can_import=true`
```bash
# 记下 token，再执行导入
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/import" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_repaired_v2.csv;type=text/csv" \
  -F "import_format=auto" \
  -F "precheck_token=<上一步返回的token>"
```
**预期输出**: `success=true`, `version_number=2`, `message="内容无变更，复用现有版本 v2。"`
不会创建 v3，版本历史保持 2 个，审批日志新增 1 条 IMPORT 记录但 `extra_data.reused=true`（可据此区分复用与真实导入），导出报告数据不变。

#### C. 批次状态不允许导入（预检查识别 CONFLICT）
在步骤 9 提交待验收后，尝试预检查：
```bash
curl -s -X POST "$API/api/batches/$BATCH_ID/manifests/precheck" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_good.csv;type=text/csv"
```
**预期输出**: `action_type=CONFLICT`, `can_import=false`，conflicts 中包含 `severity: "error"`, `conflict_type: "STATUS_CONFLICT"`
即使拿到 token，正式导入也会被拒绝（400）。

#### D. 已归档批次禁止更新
```bash
curl -s -X PATCH "$API/api/batches/$BATCH_ID" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 1" \
  -d '{"name": "尝试修改已归档批次"}'
```
**预期**: 400 Bad Request

#### E. 清单缺字段（预检查识别解析错误）
```bash
# 创建新批次
curl -s -X POST "$API/api/batches/" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 5" \
  -d '{"batch_code":"BATCH-TEST-MISSING","name":"缺字段测试","submitter_id":5}'
# 记返回 id，然后预检查缺字段文件
curl -s -X POST "$API/api/batches/2/manifests/precheck" \
  -H "X-User-Id: 5" \
  -F "file=@samples/manifest_sample_with_errors.csv;type=text/csv"
```
预期 `action_type=CONFLICT`, `can_import=false`, `parse_errors` 中每条都包含 `line_number`、`item_key`、`field_name`、`error_message`

---

## Python requests 验收脚本

以下脚本可直接运行，完整覆盖「预检查 → 确认 → 导入」流程：

```python
"""清单导入预检查验收脚本 - 按 README 文档完整链路验证"""
import requests, sys

API = "http://127.0.0.1:8000"
H_SUBMITTER = {"X-User-Id": "5"}
H_ADMIN = {"X-User-Id": "1"}
H_REVIEWER = {"X-User-Id": "3"}
H_LEAD = {"X-User-Id": "2"}

OK = "[OK]"
FAIL = "[FAIL]"
errors = []

def check(step, condition, detail=""):
    if condition:
        print(f"  {OK} {step}")
    else:
        print(f"  {FAIL} {step}  --  {detail}")
        errors.append(step)

def precheck(bid, filename, filepath, user=H_SUBMITTER):
    with open(filepath, "rb") as f:
        return requests.post(
            f"{API}/api/batches/{bid}/manifests/precheck",
            headers=user,
            files={"file": (filename, f, "text/csv")},
        )

def do_import(bid, filename, filepath, token, user=H_SUBMITTER):
    with open(filepath, "rb") as f:
        return requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=user,
            files={"file": (filename, f, "text/csv")},
            data={"precheck_token": token},
        )

# 1. 创建批次
r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
    "batch_code": "VERIFY-001", "name": "验收测试批次", "submitter_id": 5
})
check("创建批次", r.status_code == 201, f"status={r.status_code} body={r.text[:200]}")
if r.status_code != 201:
    print("  中止：无法创建批次"); sys.exit(1)
bid = r.json()["id"]
print(f"  批次 id={bid}")

# 2. 预检查 v1
r = precheck(bid, "v1.csv", "samples/manifest_sample_good.csv")
d = r.json()
check("预检查 v1 status=200", r.status_code == 200, f"status={r.status_code}")
check("预检查 action_type=NEW_VERSION", d.get("action_type") == "NEW_VERSION",
      f"actual={d.get('action_type')}")
check("预检查 can_import=True", d.get("can_import") is True,
      f"actual={d.get('can_import')}")
token = d.get("precheck_token")
if not token:
    print("  中止：未获取到 precheck_token"); sys.exit(1)

# 3. 正式导入 v1
r = do_import(bid, "v1.csv", "samples/manifest_sample_good.csv", token)
d = r.json()
check("导入 v1 status=200", r.status_code == 200, f"status={r.status_code}")
check("导入 v1 success=True", d.get("success") is True, f"body={str(d)[:200]}")
check("导入 v1 version_number=1", d.get("version_number") == 1,
      f"actual={d.get('version_number')}")

# 4. 重复预检查同一文件 → REUSE_VERSION
r = precheck(bid, "dup.csv", "samples/manifest_sample_good.csv")
d = r.json()
check("重复预检查 REUSE_VERSION", d.get("action_type") == "REUSE_VERSION",
      f"actual={d.get('action_type')}")
check("重复预检查 reused_version_number=1", d.get("reused_version_number") == 1,
      f"actual={d.get('reused_version_number')}")
token_dup = d.get("precheck_token")

# 5. 重复导入 → 复用旧版本
r = do_import(bid, "dup.csv", "samples/manifest_sample_good.csv", token_dup)
d = r.json()
check("重复导入 success=True", d.get("success") is True)
check("重复导入 version=1（无新版本）", d.get("version_number") == 1,
      f"actual={d.get('version_number')}")

# 6. 无 token 直接导入 → 400
with open("samples/manifest_sample_good.csv", "rb") as f:
    r = requests.post(f"{API}/api/batches/{bid}/manifests/import",
        headers=H_SUBMITTER, files={"file": ("a.csv", f, "text/csv")})
check("无 token 导入 → 400", r.status_code == 400, f"status={r.status_code}")
err_msg = r.json().get("error", {}).get("message", "")
check("错误提示含'缺少 precheck_token'", "缺少 precheck_token" in err_msg,
      f"msg={err_msg[:80]}")

# 7. 校验 → 提交 → 驳回 → 返修
requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
    json={"target_status": "pending_review", "comment": "请验收"})
requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
    "comment": "问题", "rejections": [
        {"item_key": "ITEM-001", "rejection_reason": "BIOS报告缺失"},
        {"item_key": "ITEM-002", "rejection_reason": "ECC标注缺失"},
    ]
})
requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER)
print(f"  {OK} 驳回→返修流程完成")

# 8. 返修中预检查 v2 → 应看到未解决驳回 WARNING
r = precheck(bid, "v2.csv", "samples/manifest_sample_repaired_v2.csv")
d = r.json()
check("v2 预检查 NEW_VERSION", d.get("action_type") == "NEW_VERSION")
check("v2 预检查 has_conflict=True", d.get("has_conflict") is True)
check("v2 预检查 can_import=True", d.get("can_import") is True)
rej_conflicts = [c for c in d.get("conflicts", [])
                 if c.get("conflict_type") == "UNRESOLVED_REJECTIONS"]
check("v2 预检查含 UNRESOLVED_REJECTIONS warning", len(rej_conflicts) >= 1)
token_v2 = d.get("precheck_token")

# 9. 正式导入 v2
r = do_import(bid, "v2.csv", "samples/manifest_sample_repaired_v2.csv", token_v2)
d = r.json()
check("导入 v2 success=True", d.get("success") is True)
check("导入 v2 version=2", d.get("version_number") == 2,
      f"actual={d.get('version_number')}")

# 10. 查看最近预检查记录
r = requests.get(f"{API}/api/batches/{bid}/manifests/prechecks/latest", headers=H_ADMIN)
d = r.json()
check("最近预检查可查", r.status_code == 200)
check("最近预检查 consumed=True", d.get("consumed") is True)

# 11. 审批日志中包含 PRECHECK_IMPORT
r = requests.get(f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
pre_logs = [l for l in r.json() if l.get("action") == "PRECHECK_IMPORT"]
check("审批日志含 >=3 条 PRECHECK_IMPORT", len(pre_logs) >= 3,
      f"actual={len(pre_logs)}")

# 12. 完成归档
requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
    json={"target_status": "pending_review", "comment": "v2已修"})
requests.post(f"{API}/api/batches/{bid}/approve", headers=H_LEAD, data={"comment": "通过"})
requests.post(f"{API}/api/batches/{bid}/archive", headers=H_LEAD, data={"comment": "归档"})
print(f"  {OK} 通过→归档完成")

if errors:
    print(f"\n{FAIL} {len(errors)} 项未通过: {errors}")
    sys.exit(1)
else:
    print(f"\n全部验收通过！")
```

保存为 `verify_precheck_flow.py` 后运行：
```bash
python verify_precheck_flow.py
```

---

## API 快速索引

| 方法 | 路径 | 说明 | 需要角色 |
|------|------|------|----------|
| POST | `/api/users/` | 创建用户 | 公开 |
| GET | `/api/users/` | 用户列表 | 所有 |
| POST | `/api/batches/` | 创建批次 | submitter/admin |
| GET | `/api/batches/` | 批次列表 | 所有 |
| GET | `/api/batches/{id}` | 批次详情 | 所有 |
| **POST** | **`/api/batches/{id}/manifests/precheck`** | **导入预检查（必须先调用）** | **submitter/admin** |
| **GET** | **`/api/batches/{id}/manifests/prechecks/latest`** | **最近一次预检查结果** | **所有** |
| **GET** | **`/api/batches/{id}/manifests/prechecks`** | **预检查历史列表** | **所有** |
| POST | `/api/batches/{id}/manifests/import` | 导入清单（需携带 precheck_token） | submitter/admin |
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
curl -s -X GET "$API/api/batches/1/version-history" -H "X-User-Id: 1"
curl -s -X GET "$API/api/batches/1/approval-logs" -H "X-User-Id: 1"
curl -s -X GET "$API/api/batches/1/acceptance-report" -H "X-User-Id: 1"
curl -s -X GET "$API/api/batches/1/manifests/prechecks/latest" -H "X-User-Id: 1"
```

数据应与重启前完全一致，所有版本、驳回记录、审批日志、校验结果、预检查记录均持久化在 SQLite 文件 `delivery_acceptance.db` 中。
