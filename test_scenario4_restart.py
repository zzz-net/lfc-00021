import requests

API = "http://127.0.0.1:8000"
H_ADMIN = {"X-User-Id": "1"}

BATCH_ID = 8

print("=" * 70)
print("场景 4 验证：服务重启后数据持久化一致性")
print("=" * 70)

print(f"\n批次 ID = {BATCH_ID}")
print()

r = requests.get(f"{API}/api/batches/{BATCH_ID}", headers=H_ADMIN)
batch = r.json()
print(f"1. 批次信息:")
print(f"   code: {batch['batch_code']}")
print(f"   status: {batch['status']}")
print(f"   current_manifest_version_id: {batch['current_manifest_version_id']}")
assert batch["status"] == "draft", f"状态应为 draft，实际 {batch['status']}"

r = requests.get(f"{API}/api/batches/{BATCH_ID}/manifests", headers=H_ADMIN)
versions = r.json()
print(f"\n2. 版本历史: {len(versions)} 个")
for v in versions:
    print(f"   v{v['version_number']}: id={v['id']}, items={v['item_count']}, format={v['import_format']}")
assert len(versions) == 2, f"版本历史应为 2 个，实际 {len(versions)}"
assert versions[0]["version_number"] == 1
assert versions[1]["version_number"] == 2
assert versions[0]["item_count"] == 5
assert versions[1]["item_count"] == 7

r = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H_ADMIN)
logs = r.json()
import_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]
print(f"\n3. 审批日志: {len(logs)} 条, IMPORT 日志 {len(import_logs)} 条")
for l in import_logs:
    vn = l.get('extra_data', {}).get('version_number') if l.get('extra_data') else None
    print(f"   [{str(l['created_at'])[:19]}] IMPORT v{vn} by user#{l['actor_id']}")
assert len(import_logs) == 2, f"IMPORT 日志应为 2 条，实际 {len(import_logs)}"

r = requests.get(f"{API}/api/batches/{BATCH_ID}/acceptance-report", headers=H_ADMIN)
report = r.json()
print(f"\n4. 验收报告:")
print(f"   total_versions: {report['total_versions']}")
print(f"   current_version: {report['current_version']}")
print(f"   item_count: {report['item_count']}")
print(f"   total_rejections: {report['total_rejections']}")
print(f"   resolved_rejections: {report['resolved_rejections']}")
print(f"   validation_passed: {report['validation_passed']}")
assert report["total_versions"] == 2
assert report["current_version"] == 2
assert report["item_count"] == 7
assert report["total_rejections"] == 1
assert report["resolved_rejections"] == 1

r = requests.get(f"{API}/api/batches/{BATCH_ID}/rejections", headers=H_ADMIN)
rejs = r.json()
print(f"\n5. 驳回记录: {len(rejs)} 条")
for rj in rejs:
    print(f"   id={rj['id']}, item_key={rj['item_key']}, resolved={rj['resolved']}, resolved_by_v={rj['resolved_by_manifest_version_id']}")
assert len(rejs) == 1
assert rejs[0]["resolved"] == True

r = requests.get(f"{API}/api/batches/{BATCH_ID}/export-report?format=json", headers=H_ADMIN)
export = r.json()
print(f"\n6. 导出报告:")
print(f"   batch_code: {export['batch_summary']['batch_code']}")
print(f"   version_history count: {len(export['version_history'])}")
assert len(export["version_history"]) == 2

print("\n" + "=" * 70)
print("场景 4 验证通过！服务重启后所有数据完全一致。")
print("=" * 70)
