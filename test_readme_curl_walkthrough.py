import requests
import json
import subprocess
import os
import random
import string

API = "http://127.0.0.1:8000"
SUFFIX = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

print("=" * 80)
print("按 README.md 实际走一遍 curl 链路 - 用户可见结果验证")
print(f"批次代码后缀: {SUFFIX}")
print("=" * 80)

def run_curl(desc, method, url, headers=None, files=None, data=None, json_data=None, expected_code=200):
    print(f"\n>>> {desc}")
    print(f"    {method} {url}")
    if method == "GET":
        r = requests.get(url, headers=headers)
    elif method == "POST" and files:
        r = requests.post(url, headers=headers, files=files, data=data)
    elif method == "POST" and json_data:
        r = requests.post(url, headers=headers, json=json_data)
    elif method == "POST":
        r = requests.post(url, headers=headers, data=data)
    
    print(f"    状态码: {r.status_code}")
    try:
        resp = r.json()
        print(f"    返回: {json.dumps(resp, indent=2, ensure_ascii=False)[:500]}")
    except:
        print(f"    返回文本: {r.text[:200]}")
    
    assert r.status_code == expected_code, f"预期状态码 {expected_code}，实际 {r.status_code}"
    return r.json() if r.status_code == expected_code else None

print("\n" + "=" * 80)
print("=== 主流程：创建批次 → 导入 → 驳回 → 返修 → 导入 v2 ===")
print("=" * 80)

# 步骤 2: 创建批次
H_SUBMITTER = {"X-User-Id": "5"}
BATCH_CODE = f"BATCH-CURL-DEMO-{SUFFIX}"
batch = run_curl(
    "步骤 2: 创建交付批次",
    "POST",
    f"{API}/api/batches/",
    headers=H_SUBMITTER,
    json_data={
        "batch_code": BATCH_CODE,
        "name": "README curl 演示批次",
        "description": "按 README 步骤实际验证",
        "submitter_id": 5
    },
    expected_code=201
)
BATCH_ID = batch["id"]
print(f"    [OK] 批次创建成功，ID={BATCH_ID}")

# 步骤 4: 导入 v1 清单
print("\n" + "-" * 60)
print("README 步骤 4 预期: success=true, version_number=1, item_count=5")
print("-" * 60)
with open("samples/manifest_sample_good.csv", "rb") as f:
    v1 = run_curl(
        "步骤 4: 导入正确的 v1 清单",
        "POST",
        f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUBMITTER,
        files={"file": ("manifest_sample_good.csv", f, "text/csv")}
    )
assert v1["success"] == True, "v1 导入应该成功"
assert v1["version_number"] == 1, f"预期 version_number=1，实际 {v1['version_number']}"
assert v1["item_count"] == 5, f"预期 item_count=5，实际 {v1['item_count']}"
print("    [OK] 与 README 步骤 4 预期完全一致！")

# 步骤 8: 提交待验收
run_curl(
    "步骤 8: 提交待验收 (draft -> pending_review)",
    "POST",
    f"{API}/api/batches/{BATCH_ID}/transition",
    headers=H_SUBMITTER,
    json_data={"target_status": "pending_review", "comment": "请评审验收"}
)

# 步骤 9: 驳回
H_REVIEWER = {"X-User-Id": "3"}
print("\n" + "-" * 60)
print("README 步骤 9 预期: 记录 2 条驳回，状态变为 partially_rejected")
print("-" * 60)
rej = run_curl(
    "步骤 9: reviewer 驳回 2 项问题",
    "POST",
    f"{API}/api/batches/{BATCH_ID}/reject",
    headers=H_REVIEWER,
    json_data={
        "comment": "发现问题请返修",
        "rejections": [
            {"item_key": "ITEM-001", "rejection_reason": "主板需补充 BIOS 版本说明"},
            {"item_key": "ITEM-002", "rejection_reason": "内存需补充 ECC 校验说明"}
        ]
    }
)
assert rej["batch_status"] == "partially_rejected", f"预期 partially_rejected，实际 {rej['batch_status']}"
assert rej["rejection_count"] == 2, f"预期 2 条驳回，实际 {rej['rejection_count']}"
print("    [OK] 与 README 步骤 9 预期完全一致！")

# 步骤 11: 开始返修
run_curl(
    "步骤 11: 开始返修",
    "POST",
    f"{API}/api/batches/{BATCH_ID}/start-repair",
    headers=H_SUBMITTER,
    data={"comment": "收到驳回，开始返修"}
)

# 步骤 12: 导入 v2 修订版
print("\n" + "-" * 60)
print("README 步骤 12 预期: version_number=2, item_count=7")
print("-" * 60)
with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
    v2 = run_curl(
        "步骤 12: 导入修订版 v2 清单",
        "POST",
        f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUBMITTER,
        files={"file": ("manifest_sample_repaired_v2.csv", f, "text/csv")}
    )
assert v2["success"] == True, "v2 导入应该成功"
assert v2["version_number"] == 2, f"预期 version_number=2，实际 {v2['version_number']}"
assert v2["item_count"] == 7, f"预期 item_count=7，实际 {v2['item_count']}"
print("    [OK] 与 README 步骤 12 预期完全一致！")

# 步骤 18: 查看版本历史
H_ADMIN = {"X-User-Id": "1"}
print("\n" + "-" * 60)
print("README 步骤 18 预期: 能看到 v1 和 v2 两个版本")
print("-" * 60)
versions = run_curl(
    "步骤 18: 查询版本历史",
    "GET",
    f"{API}/api/batches/{BATCH_ID}/version-history",
    headers=H_ADMIN
)
print(f"    版本数量: {len(versions)}")
for v in versions:
    print(f"    - v{v['version_number']}: id={v['id']}, {v['item_count']} items")
assert len(versions) == 2, f"预期 2 个版本，实际 {len(versions)}"
print("    [OK] 与 README 步骤 18 预期完全一致！")

# 步骤 19: 查看审批日志
print("\n" + "-" * 60)
print("README 步骤 19 预期: 完整审计链路，IMPORT 日志 2 条")
print("-" * 60)
logs = run_curl(
    "步骤 19: 查询审批日志",
    "GET",
    f"{API}/api/batches/{BATCH_ID}/approval-logs",
    headers=H_ADMIN
)
import_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]
print(f"    IMPORT 日志数量: {len(import_logs)}")
for l in logs:
    fs = l.get('from_status') or '-'
    ts = l.get('to_status') or '-'
    print(f"    [{str(l['created_at'])[:19]}] {l['action']:18s} {fs:18s} -> {ts:18s}")
assert len(import_logs) == 2, f"预期 2 条 IMPORT 日志，实际 {len(import_logs)}"
print("    [OK] 与 README 步骤 19 预期完全一致！")

print("\n" + "=" * 80)
print("=== 专项测试 B: 重复导入完全相同内容（幂等复用） ===")
print("=" * 80)

print("\n" + "-" * 60)
print("README 专项测试 B 预期:")
print("  success=true, version_number=2")
print("  message='内容无变更，复用现有版本 v2。'")
print("  不会创建 v3，版本历史保持 2 个")
print("  审批日志无新增 IMPORT 记录")
print("-" * 60)

with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
    dup = run_curl(
        "专项测试 B: 重复导入完全相同的 v2 内容",
        "POST",
        f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUBMITTER,
        files={"file": ("manifest_sample_repaired_v2.csv", f, "text/csv")}
    )

print(f"\n    用户实际看到的接口返回:")
print(f"      success: {dup['success']}")
print(f"      version_number: {dup['version_number']}")
print(f"      manifest_version_id: {dup['manifest_version_id']}")
print(f"      message: '{dup['message']}'")

assert dup["success"] == True, f"预期 success=true，实际 {dup['success']}"
assert dup["version_number"] == 2, f"预期 version_number=2，实际 {dup['version_number']}"
assert "复用" in dup["message"], f"预期 message 包含'复用'，实际 '{dup['message']}'"
assert "v2" in dup["message"], f"预期 message 包含'v2'，实际 '{dup['message']}'"
print("    [OK] 返回值与 README 专项测试 B 预期完全一致！")

# 验证版本历史没有增长
print("\n    验证版本历史:")
versions_after = requests.get(f"{API}/api/batches/{BATCH_ID}/version-history", headers=H_ADMIN).json()
print(f"      重复导入后版本数量: {len(versions_after)}")
assert len(versions_after) == 2, f"重复导入后版本历史仍应为 2，实际 {len(versions_after)}"
print("      [OK] 没有长出 v3！")

# 验证审批日志没有新增
print("\n    验证审批日志:")
logs_after = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H_ADMIN).json()
import_logs_after = [l for l in logs_after if l["action"] == "IMPORT_MANIFEST"]
print(f"      重复导入后 IMPORT 日志数量: {len(import_logs_after)}")
assert len(import_logs_after) == 2, f"重复导入后 IMPORT 日志仍应为 2，实际 {len(import_logs_after)}"
print("      [OK] 没有新增审批日志！")

# 验证验收报告未受污染
print("\n    验证验收报告:")
report = requests.get(f"{API}/api/batches/{BATCH_ID}/acceptance-report", headers=H_ADMIN).json()
print(f"      total_versions: {report['total_versions']}")
print(f"      current_version: {report['current_version']}")
print(f"      item_count: {report['item_count']}")
assert report["total_versions"] == 2
assert report["current_version"] == 2
assert report["item_count"] == 7
print("      [OK] 验收报告未受污染！")

# 验证导出报告未受污染
print("\n    验证导出报告:")
export = requests.get(f"{API}/api/batches/{BATCH_ID}/export-report?format=json", headers=H_ADMIN).json()
print(f"      version_history 数量: {len(export['version_history'])}")
print(f"      approval_workflow 步骤数: {len(export['approval_workflow'])}")
assert len(export["version_history"]) == 2
print("      [OK] 导出报告未受污染！")

print("\n" + "=" * 80)
print("[OK] 所有 README 步骤验证通过！")
print("=" * 80)
print("\n用户实际能看到的结果:")
print("  1. 首次返修导入 v2: version_number=2, item_count=7 [OK]")
print("  2. 重复导入相同内容: message='内容无变更，复用现有版本 v2。' [OK]")
print("  3. 版本历史: 始终 2 个（v1, v2），没有长出 v3 [OK]")
print("  4. 审批日志: 始终 2 条 IMPORT 记录，没有新增 [OK]")
print("  5. 验收报告: total_versions=2，数据正确 [OK]")
print("  6. 导出报告: 未受污染 [OK]")
print("\nREADME.md 文档描述与实际接口行为完全一致！")
print("=" * 80)
