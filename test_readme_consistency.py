import requests
import json
import sys
import random
import string

API = "http://127.0.0.1:8000"
H_ADMIN = {"X-User-Id": "1"}
H_SUBMITTER = {"X-User-Id": "5"}
H_REVIEWER = {"X-User-Id": "3"}

PASS = 0
FAIL = 0

def random_suffix():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

SUFFIX = random_suffix()
print(f"使用随机后缀: {SUFFIX}")

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}")
        if detail:
            print(f"       {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        if detail:
            print(f"       {detail}")

print("=" * 80)
print("文档一致性检查：核对接口行为与 README.md 描述完全一致")
print("=" * 80)

print("\n=== 步骤 1：创建全新批次，走完整返修流程 ===")
r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
    "batch_code": f"BATCH-DOC-{SUFFIX}",
    "name": "文档一致性检查批次",
    "description": "用于验证 README 描述与实际接口行为一致",
    "submitter_id": 5
})
batch = r.json()
print(f"创建批次返回: {json.dumps(batch, indent=2, ensure_ascii=False)}")
BATCH_ID = batch["id"]
print(f"创建批次 ID={BATCH_ID}, status={batch['status']}")
check("批次初始状态为 draft", batch["status"] == "draft")

print("\n=== 步骤 2：导入 v1 清单 ===")
with open("samples/manifest_sample_good.csv", "rb") as f:
    r = requests.post(f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUBMITTER,
        files={"file": ("v1.csv", f, "text/csv")})
v1 = r.json()
print(f"接口返回: success={v1['success']}, version_number={v1['version_number']}, item_count={v1['item_count']}")
check("v1 导入成功", v1["success"] == True)
check("v1 版本号为 1", v1["version_number"] == 1)
check("v1 条目数为 5", v1["item_count"] == 5)

print("\n=== 步骤 3：提交待验收 ===")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/transition",
    headers=H_SUBMITTER,
    json={"target_status": "pending_review", "comment": "请评审"})
check("状态流转到 pending_review", r.json()["status"] == "pending_review")

print("\n=== 步骤 4：驳回（创建 2 条驳回记录） ===")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/reject",
    headers=H_REVIEWER,
    json={
        "comment": "有问题需返修",
        "rejections": [
            {"item_key": "ITEM-001", "rejection_reason": "主板需补充规格说明"},
            {"item_key": "ITEM-002", "rejection_reason": "内存需补充 ECC 说明"}
        ]
    })
rej = r.json()
check("状态变为 partially_rejected", rej["batch_status"] == "partially_rejected")
check("驳回记录数量为 2", rej["rejection_count"] == 2)

print("\n=== 步骤 5：开始返修 ===")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/start-repair",
    headers=H_SUBMITTER,
    data={"comment": "开始返修"})
print(f"开始返修返回: {json.dumps(r.json(), indent=2, ensure_ascii=False)}")
check("状态变为 repairing", r.json()["batch_status"] == "repairing")

print("\n=== 步骤 6：首次导入 v2 修订版（README 步骤 12 预期） ===")
print("README 预期: version_number=2, item_count=7")
with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
    r = requests.post(f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUBMITTER,
        files={"file": ("v2.csv", f, "text/csv")})
v2 = r.json()
print(f"接口返回: success={v2['success']}, version_number={v2['version_number']}, item_count={v2['item_count']}, message={v2.get('message', '')}")
check("首次导入 v2 成功", v2["success"] == True)
check("首次导入 v2 版本号为 2（与 README 步骤 12 一致）", v2["version_number"] == 2,
      f"预期 version_number=2，实际 {v2['version_number']}")
check("首次导入 v2 条目数为 7（与 README 步骤 12 一致）", v2["item_count"] == 7,
      f"预期 item_count=7，实际 {v2['item_count']}")
check("首次导入 v2 没有复用消息", "复用" not in v2.get("message", ""))

print("\n=== 步骤 7：版本历史检查（首次导入 v2 后） ===")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/version-history", headers=H_ADMIN)
versions = r.json()
print(f"版本历史数量: {len(versions)}")
for v in versions:
    print(f"  v{v['version_number']}: id={v['id']}, {v['item_count']} items")
check("版本历史数量为 2", len(versions) == 2)

print("\n=== 步骤 8：审批日志检查（首次导入 v2 后） ===")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H_ADMIN)
logs = r.json()
import_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]
print(f"审批日志总数: {len(logs)}, IMPORT 日志数: {len(import_logs)}")
check("IMPORT 审批日志数量为 2", len(import_logs) == 2)

print("\n=== 步骤 9：重复导入完全相同的 v2 内容（README 专项测试 B 预期） ===")
print("README 预期: success=true, version_number=2, message='内容无变更，复用现有版本 v2。'")
print("README 预期: 不会创建 v3，版本历史保持 2 个，审批日志无新增")
with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
    r = requests.post(f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUBMITTER,
        files={"file": ("v2_dup.csv", f, "text/csv")})
dup = r.json()
print(f"接口返回: success={dup['success']}, version_number={dup['version_number']}, manifest_version_id={dup['manifest_version_id']}")
print(f"            message='{dup['message']}'")
check("重复导入 success=true（与 README 一致）", dup["success"] == True,
      f"预期 success=true，实际 {dup['success']}")
check("重复导入 version_number=2（与 README 一致）", dup["version_number"] == 2,
      f"预期 version_number=2，实际 {dup['version_number']}")
check("重复导入 message 包含'复用'（与 README 一致）", "复用" in dup["message"],
      f"预期 message 包含'复用'，实际 '{dup['message']}'")
check("重复导入 message 包含'v2'（与 README 一致）", "v2" in dup["message"],
      f"预期 message 包含'v2'，实际 '{dup['message']}'")
check("重复导入复用了 v2 的 id", dup["manifest_version_id"] == v2["manifest_version_id"])

print("\n=== 步骤 10：版本历史检查（重复导入后） ===")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/version-history", headers=H_ADMIN)
versions_after = r.json()
print(f"版本历史数量: {len(versions_after)}（应仍为 2）")
check("重复导入后版本历史保持 2 个（未长出 v3）", len(versions_after) == 2,
      f"预期 2 个，实际 {len(versions_after)} 个")

print("\n=== 步骤 11：审批日志检查（重复导入后） ===")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H_ADMIN)
logs_after = r.json()
import_logs_after = [l for l in logs_after if l["action"] == "IMPORT_MANIFEST"]
print(f"IMPORT 审批日志数量: {len(import_logs_after)}（应仍为 2）")
check("重复导入后 IMPORT 审批日志保持 2 条（未新增）", len(import_logs_after) == 2,
      f"预期 2 条，实际 {len(import_logs_after)} 条")

print("\n=== 步骤 12：验收报告检查（重复导入后） ===")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/acceptance-report", headers=H_ADMIN)
report = r.json()
print(f"验收报告: total_versions={report['total_versions']}, current_version={report['current_version']}, item_count={report['item_count']}")
check("验收报告 total_versions=2", report["total_versions"] == 2)
check("验收报告 current_version=2", report["current_version"] == 2)
check("验收报告 item_count=7", report["item_count"] == 7)

print("\n=== 步骤 13：导入失败（缺字段）无半截写入 ===")
r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
    "batch_code": f"BATCH-DOC-FAIL-{SUFFIX}",
    "name": "文档一致性检查-失败测试",
    "submitter_id": 5
})
batch_fail = r.json()
BATCH_FAIL_ID = batch_fail["id"]
print(f"创建失败测试批次 ID={BATCH_FAIL_ID}")

r = requests.get(f"{API}/api/batches/{BATCH_FAIL_ID}/version-history", headers=H_ADMIN)
versions_before_fail = len(r.json())

with open("samples/manifest_sample_with_errors.csv", "rb") as f:
    r = requests.post(f"{API}/api/batches/{BATCH_FAIL_ID}/manifests/import",
        headers=H_SUBMITTER,
        files={"file": ("bad.csv", f, "text/csv")})
fail_result = r.json()
print(f"导入失败结果: success={fail_result['success']}, errors={len(fail_result['errors'])}")
check("缺字段导入返回 success=false", fail_result["success"] == False)
check("缺字段导入返回错误数组", len(fail_result["errors"]) > 0)

r = requests.get(f"{API}/api/batches/{BATCH_FAIL_ID}/version-history", headers=H_ADMIN)
versions_after_fail = len(r.json())
check("失败后版本历史数量不变（无半截写入）", versions_after_fail == versions_before_fail,
      f"导入前 {versions_before_fail} 个，导入后 {versions_after_fail} 个")

r = requests.get(f"{API}/api/batches/{BATCH_FAIL_ID}/approval-logs", headers=H_ADMIN)
logs_fail = len([l for l in r.json() if l["action"] == "IMPORT_MANIFEST"])
check("失败后审批日志无新增 IMPORT 记录", logs_fail == 0,
      f"预期 0 条 IMPORT 日志，实际 {logs_fail} 条")

print("\n" + "=" * 80)
print(f"文档一致性检查总计: PASS={PASS}, FAIL={FAIL}")
print("=" * 80)

if FAIL > 0:
    print("\n[FAIL] 存在不一致！请检查 README 描述与实际接口行为。")
    sys.exit(1)
else:
    print("\n[OK] README 文档描述与实际接口行为完全一致！")
    sys.exit(0)
