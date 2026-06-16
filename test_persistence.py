import requests
import json

API = "http://127.0.0.1:8000"
H = {"X-User-Id": "1"}
BATCH_ID = 1

print("=" * 60)
print("服务重启后 - 数据持久化验证")
print("=" * 60)

r = requests.get(f"{API}/api/batches/{BATCH_ID}", headers=H)
batch = r.json()
print(f"\n1. 批次状态: {batch['status']}  (应为 'archived')")
assert batch["status"] == "archived", f"FAIL: 状态应为 archived, 实际 {batch['status']}"
print("   [OK]")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/version-history", headers=H)
versions = r.json()
print(f"\n2. 版本历史数量: {len(versions)}  (应为 2)")
assert len(versions) == 2, f"FAIL: 应为 2 个版本"
print(f"   v1: id={versions[0]['id']}, items={versions[0]['item_count']}, format={versions[0]['import_format']}")
print(f"   v2: id={versions[1]['id']}, items={versions[1]['item_count']}, format={versions[1]['import_format']}")
assert versions[0]["item_count"] == 5
assert versions[1]["item_count"] == 7
print("   [OK]")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/rejections", headers=H)
rejs = r.json()
print(f"\n3. 驳回记录: {len(rejs)} 条")
print(f"   已解决: {sum(1 for r in rejs if r['resolved'])}, 未解决: {sum(1 for r in rejs if not r['resolved'])}")
assert len(rejs) == 2 and all(r["resolved"] for r in rejs), "FAIL: 驳回记录应为 2 条且均已解决"
print("   [OK]")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H)
logs = r.json()
print(f"\n4. 审批日志: {len(logs)} 条")
actions = [l["action"] for l in logs]
expected = ["CREATE", "IMPORT_MANIFEST", "STATUS_TRANSITION", "REJECT", "START_REPAIR",
            "IMPORT_MANIFEST", "STATUS_TRANSITION", "APPROVE", "ARCHIVE"]
print(f"   动作链: {' → '.join(actions)}")
assert len(logs) == 9, f"FAIL: 应为 9 条日志"
for i, exp in enumerate(expected):
    assert actions[i] == exp, f"FAIL: 第 {i} 条应为 {exp}, 实际 {actions[i]}"
print("   [OK]")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/acceptance-report", headers=H)
report = r.json()
print(f"\n5. 验收报告摘要:")
print(f"   总版本: {report['total_versions']}, 当前: v{report['current_version']}")
print(f"   条目数: {report['item_count']}")
print(f"   驳回: {report['total_rejections']}, 已解决: {report['resolved_rejections']}")
print(f"   校验通过: {report['validation_passed']}")
assert report["total_versions"] == 2
assert report["current_version"] == 2
assert report["item_count"] == 7
assert report["total_rejections"] == 2
assert report["resolved_rejections"] == 2
assert report["validation_passed"] == True
print("   [OK]")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/manifests/{versions[1]['id']}", headers=H)
v2_items = r.json()["items"]
print(f"\n6. v2 清单内容 ({len(v2_items)} 条):")
for item in v2_items:
    print(f"   行{item['line_number']:>3} | {item['item_key']:12} | {item['item_data'].get('item_name','')}")

print("\n" + "=" * 60)
print("所有持久化验证通过！服务重启后数据完全一致。")
print("=" * 60)
