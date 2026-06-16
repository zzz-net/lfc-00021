import requests
import json

API = "http://127.0.0.1:8000"
H_LEAD = {"X-User-Id": "2"}

old_export_id = "aa51f344846db109"

print("=" * 70)
print("幂等性验证")
print("=" * 70)
print(f"批次 ID: 129")
print(f"对比版本: v1 -> v2")
print(f"重启前 export_id: {old_export_id}")

r = requests.get(
    f"{API}/api/batches/129/version-diff/export?old_version=1&new_version=2",
    headers=H_LEAD
)
data = r.json()
new_export_id = data.get("export_id")

print(f"重启后 export_id: {new_export_id}")
print()

if old_export_id == new_export_id:
    print("[OK] 幂等性验证通过！重启后同一批次导出 ID 保持一致")
else:
    print("[FAIL] 幂等性验证失败！ID 不一致")

print()
print("版本差异内容验证:")
summary = data["diff_data"]["summary"]
print(f"  新增: {summary['added_count']} 项")
print(f"  删除: {summary['removed_count']} 项")
print(f"  修改: {summary['modified_count']} 项")
print(f"  未变: {summary['unchanged_count']} 项")

if (summary['added_count'] == 1 and
    summary['removed_count'] == 0 and
    summary['modified_count'] == 1 and
    summary['unchanged_count'] == 1):
    print("[OK] 版本差异内容一致")
else:
    print("[FAIL] 版本差异内容不一致")

print()
print("=" * 70)
print("curl 命令验证:")
print(f'  curl -H "X-User-Id: 2" "{API}/api/batches/129/version-diff?old_version=1&new_version=2"')
print(f'  curl -H "X-User-Id: 2" "{API}/api/batches/129/version-diff/export?old_version=1&new_version=2"')
print("=" * 70)
