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

def precheck(bid, filename, filepath):
    """执行预检查，返回 (status_code, response_dict)"""
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/precheck",
            headers=H_SUBMITTER,
            files={"file": (filename, f, "text/csv")},
        )
    return r.status_code, r.json()

def precheck_and_import(bid, filename, filepath):
    """按 README 链路：先 precheck 拿 token，再正式 import。返回 (import_status, import_dict)"""
    pc_status, pc_body = precheck(bid, filename, filepath)
    check(f"{filename} 预检查 status=200", pc_status == 200,
          f"status={pc_status}, msg={pc_body.get('error', {}).get('message', '')}")
    if pc_status != 200:
        return pc_status, pc_body
    token = pc_body.get("precheck_token")
    can_import = pc_body.get("can_import")
    check(f"{filename} 预检查 can_import=true", can_import is True,
          f"action_type={pc_body.get('action_type')}, can_import={can_import}")
    if not token or not can_import:
        return 400, {"success": False, "error": {"message": f"precheck 未通过或无 token: {str(pc_body)[:120]}"}}
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": (filename, f, "text/csv")},
            data={"precheck_token": token},
        )
    return r.status_code, r.json()

def safe_print_import(d):
    """防御性打印导入响应，缺字段时不抛 KeyError"""
    return (f"success={d.get('success')}, "
            f"version_number={d.get('version_number')}, "
            f"item_count={d.get('item_count')}, "
            f"message={d.get('message', d.get('error', {}).get('message', ''))}")

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

print("\n=== 步骤 2：预检查 + 导入 v1 清单（按 README 链路） ===")
v1_status, v1 = precheck_and_import(BATCH_ID, "v1.csv", "samples/manifest_sample_good.csv")
print(f"接口返回: {safe_print_import(v1)}")
check("v1 导入 status=200", v1_status == 200, f"status={v1_status}")
check("v1 导入成功", v1.get("success") is True, f"success={v1.get('success')}")
check("v1 版本号为 1", v1.get("version_number") == 1, f"actual={v1.get('version_number')}")
check("v1 条目数为 5", v1.get("item_count") == 5, f"actual={v1.get('item_count')}")

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

print("\n=== 步骤 6：预检查 + 首次导入 v2 修订版（README 步骤 12 预期） ===")
print("README 预期: version_number=2, item_count=7")
v2_status, v2 = precheck_and_import(BATCH_ID, "v2.csv", "samples/manifest_sample_repaired_v2.csv")
print(f"接口返回: {safe_print_import(v2)}")
check("首次导入 v2 status=200", v2_status == 200, f"status={v2_status}")
check("首次导入 v2 成功", v2.get("success") is True, f"success={v2.get('success')}")
check("首次导入 v2 版本号为 2（与 README 步骤 12 一致）", v2.get("version_number") == 2,
      f"预期 version_number=2，实际 {v2.get('version_number')}")
check("首次导入 v2 条目数为 7（与 README 步骤 12 一致）", v2.get("item_count") == 7,
      f"预期 item_count=7，实际 {v2.get('item_count')}")
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

print("\n=== 步骤 9：预检查 + 重复导入完全相同的 v2 内容（README 专项测试 B 预期） ===")
print("README 预期: success=true, version_number=2, message='内容无变更，复用现有版本 v2。'")
print("README 预期: 不会创建 v3，版本历史保持 2 个，审批日志无新增")
dup_status, dup = precheck_and_import(BATCH_ID, "v2_dup.csv", "samples/manifest_sample_repaired_v2.csv")
print(f"接口返回: {safe_print_import(dup)}, manifest_version_id={dup.get('manifest_version_id')}")
check("重复导入 status=200", dup_status == 200, f"status={dup_status}")
check("重复导入 success=true（与 README 一致）", dup.get("success") is True,
      f"预期 success=true，实际 {dup.get('success')}")
check("重复导入 version_number=2（与 README 一致）", dup.get("version_number") == 2,
      f"预期 version_number=2，实际 {dup.get('version_number')}")
check("重复导入 message 包含'复用'（与 README 一致）", "复用" in dup.get("message", ""),
      f"预期 message 包含'复用'，实际 '{dup.get('message', '')}'")
check("重复导入 message 包含'v2'（与 README 一致）", "v2" in dup.get("message", ""),
      f"预期 message 包含'v2'，实际 '{dup.get('message', '')}'")
check("重复导入复用了 v2 的 id", dup.get("manifest_version_id") == v2.get("manifest_version_id"),
      f"v2_id={v2.get('manifest_version_id')}, dup_id={dup.get('manifest_version_id')}")

print("\n=== 步骤 10：版本历史检查（重复导入后） ===")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/version-history", headers=H_ADMIN)
versions_after = r.json()
print(f"版本历史数量: {len(versions_after)}（应仍为 2）")
check("重复导入后版本历史保持 2 个（未长出 v3）", len(versions_after) == 2,
      f"预期 2 个，实际 {len(versions_after)} 个")

print("\n=== 步骤 11：审批日志检查（重复导入后） ===")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H_ADMIN)
logs_after = r.json()
import_logs_after = [l for l in logs_after if l.get("action") == "IMPORT_MANIFEST"]
reused_logs = [l for l in import_logs_after if (l.get("extra_data") or {}).get("reused") is True]
print(f"审批日志总数: {len(logs_after)}, IMPORT 日志数: {len(import_logs_after)}, 其中 reused=true: {len(reused_logs)}")
check("重复导入后 IMPORT 审批日志新增 1 条（真实接口行为）", len(import_logs_after) == 3,
      f"预期 3 条，实际 {len(import_logs_after)} 条")
check("新增的 IMPORT 日志标记 reused=true", len(reused_logs) == 1,
      f"预期 1 条 reused=true，实际 {len(reused_logs)} 条")

print("\n=== 步骤 12：验收报告检查（重复导入后） ===")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/acceptance-report", headers=H_ADMIN)
report = r.json()
print(f"验收报告: total_versions={report.get('total_versions')}, current_version={report.get('current_version')}, item_count={report.get('item_count')}")
check("验收报告 total_versions=2", report.get("total_versions") == 2, f"actual={report.get('total_versions')}")
check("验收报告 current_version=2", report.get("current_version") == 2, f"actual={report.get('current_version')}")
check("验收报告 item_count=7", report.get("item_count") == 7, f"actual={report.get('item_count')}")

print("\n=== 步骤 13：预检查失败（缺字段）无半截写入 ===")
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

pc_status, pc_body = precheck(BATCH_FAIL_ID, "bad.csv", "samples/manifest_sample_with_errors.csv")
print(f"预检查缺字段文件: status={pc_status}, action_type={pc_body.get('action_type')}, "
      f"can_import={pc_body.get('can_import')}, parse_errors count={len(pc_body.get('parse_errors', []))}")
check("缺字段预检查 status=200", pc_status == 200)
check("缺字段预检查 action_type=CONFLICT", pc_body.get("action_type") == "CONFLICT",
      f"actual={pc_body.get('action_type')}")
check("缺字段预检查 can_import=false", pc_body.get("can_import") is False,
      f"actual={pc_body.get('can_import')}")
check("缺字段预检查返回 parse_errors 数组", len(pc_body.get("parse_errors", [])) > 0,
      f"count={len(pc_body.get('parse_errors', []))}")

r = requests.get(f"{API}/api/batches/{BATCH_FAIL_ID}/version-history", headers=H_ADMIN)
versions_after_fail = len(r.json())
check("失败后版本历史数量不变（无半截写入）", versions_after_fail == versions_before_fail,
      f"导入前 {versions_before_fail} 个，导入后 {versions_after_fail} 个")

r = requests.get(f"{API}/api/batches/{BATCH_FAIL_ID}/approval-logs", headers=H_ADMIN)
logs_fail = len([l for l in r.json() if l.get("action") == "IMPORT_MANIFEST"])
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
