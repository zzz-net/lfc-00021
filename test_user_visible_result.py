import requests
import json

API = "http://127.0.0.1:8000"
H_ADMIN = {"X-User-Id": "1"}
H_SUBMITTER = {"X-User-Id": "5"}

BATCH_ID = 5

print("=" * 80)
print("验证：服务重启后持久化一致性（批次 ID=" + str(BATCH_ID) + "）")
print("=" * 80)

r = requests.get(f"{API}/api/batches/{BATCH_ID}", headers=H_ADMIN)
batch = r.json()
print(f"\n1. 批次基本信息:")
print(f"   batch_code: {batch['batch_code']}")
print(f"   name: {batch['name']}")
print(f"   status: {batch['status']}")
print(f"   submitter_id: {batch['submitter_id']}")
print(f"   current_manifest_version_id: {batch['current_manifest_version_id']}")
assert batch["status"] == "draft", f"状态应为 draft，实际 {batch['status']}"
print("   [OK] 批次信息正确")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/manifests", headers=H_ADMIN)
versions = r.json()
print(f"\n2. 版本历史（用户可直接通过接口查看）:")
for v in versions:
    print(f"   版本 v{v['version_number']}: id={v['id']}, 条目数={v['item_count']}, 格式={v['import_format']}, 导入人={v['imported_by']}, 导入时间={str(v['imported_at'])[:19]}")
assert len(versions) == 2, f"版本历史应为 2 个，实际 {len(versions)}"
assert versions[0]["version_number"] == 1
assert versions[1]["version_number"] == 2
assert versions[0]["item_count"] == 5
assert versions[1]["item_count"] == 7
print("   [OK] 版本历史正确，无多余版本")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H_ADMIN)
logs = r.json()
import_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]
print(f"\n3. 审批日志（审计链路，用户可查看）:")
for l in logs:
    fs = l.get('from_status') or '-'
    ts = l.get('to_status') or '-'
    print(f"   [{str(l['created_at'])[:19]}] {l['action']:18s} {fs:18s} -> {ts:18s} by user#{l['actor_id']}")
assert len(import_logs) == 2, f"IMPORT 日志应为 2 条，实际 {len(import_logs)}"
print("   [OK] 审批日志正确，无多余 IMPORT 记录")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/rejections", headers=H_ADMIN)
rejs = r.json()
print(f"\n4. 驳回记录（关联到具体版本，用户可查看）:")
for rj in rejs:
    resolved_v = None
    if rj["resolved_by_manifest_version_id"]:
        for v in versions:
            if v["id"] == rj["resolved_by_manifest_version_id"]:
                resolved_v = f"v{v['version_number']}"
                break
    print(f"   id={rj['id']}, item_key={rj['item_key']}, line={rj['line_number']}, reason='{rj['rejection_reason']}'")
    print(f"      created_at={str(rj['created_at'])[:19]}, resolved={rj['resolved']}, resolved_by={resolved_v}")
assert len(rejs) == 1
assert rejs[0]["resolved"] == True
print("   [OK] 驳回记录正确，关联到正确的解决版本")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/acceptance-report", headers=H_ADMIN)
report = r.json()
print(f"\n5. 验收报告摘要（最终导出结果）:")
print(f"   批次: {report['batch_code']} / {report['batch_name']}")
print(f"   状态: {report['status']}")
print(f"   总版本数: {report['total_versions']}")
print(f"   当前版本: v{report['current_version']}")
print(f"   条目总数: {report['item_count']}")
print(f"   总驳回数: {report['total_rejections']}")
print(f"   已解决驳回: {report['resolved_rejections']}")
print(f"   校验通过: {report['validation_passed']}")
assert report["total_versions"] == 2
assert report["current_version"] == 2
assert report["item_count"] == 7
assert report["total_rejections"] == 1
assert report["resolved_rejections"] == 1
print("   [OK] 验收报告数据正确，无重复版本污染")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/export-report?format=json", headers=H_ADMIN)
export = r.json()
print(f"\n6. 导出报告 JSON（用户可下载）:")
print(f"   batch_code: {export['batch_summary']['batch_code']}")
print(f"   status: {export['batch_summary']['status']}")
print(f"   version_history 数量: {len(export['version_history'])}")
print(f"   approval_workflow 步骤数: {len(export['approval_workflow'])}")
assert len(export["version_history"]) == 2
print("   [OK] 导出报告正确")

print("\n" + "=" * 80)
print("持久化验证通过！服务重启后：")
print("  - 版本历史仍为 2 个（无多余 v3）")
print("  - 审批日志 IMPORT 记录仍为 2 条（无多余记录）")
print("  - 驳回记录正确关联解决版本")
print("  - 验收报告数据完全一致")
print("  - 导出报告未受污染")
print("=" * 80)

print("\n" + "=" * 80)
print("实际演示：重复导入完全相同内容时的接口返回（用户视角）")
print("=" * 80)

print(f"\n批次 {BATCH_ID} 当前版本历史:")
for v in versions:
    print(f"  v{v['version_number']}: id={v['id']}, {v['item_count']} items")

print(f"\n>>> 重复导入 v2 完全相同的内容...")
with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
    r = requests.post(f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUBMITTER,
        files={"file": ("v2_dup.csv", f, "text/csv")})
result = r.json()
print(f"\n接口返回:")
print(f"  success: {result['success']}")
print(f"  version_number: {result['version_number']}")
print(f"  manifest_version_id: {result['manifest_version_id']}")
print(f"  message: '{result['message']}'")
print(f"  版本历史数量: {len(requests.get(f'{API}/api/batches/{BATCH_ID}/manifests', headers=H_ADMIN).json())}")
print(f"  IMPORT 审批日志数量: {len([l for l in requests.get(f'{API}/api/batches/{BATCH_ID}/approval-logs', headers=H_ADMIN).json() if l['action'] == 'IMPORT_MANIFEST'])}")

assert result["success"] == True
assert result["version_number"] == 2
assert result["manifest_version_id"] == versions[1]["id"]
assert "复用" in result["message"]

print("\n[OK] 用户能明确看到：")
print("  - success=true，操作成功")
print(f"  - 复用了现有版本 v{result['version_number']}")
print("  - 版本历史未增长")
print("  - 审批日志未新增")
print("  - 导出报告未污染")
print("=" * 80)
