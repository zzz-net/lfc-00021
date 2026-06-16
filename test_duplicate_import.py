import requests
import json
import uuid

API = "http://127.0.0.1:8000"
H_SUBMITTER = {"X-User-Id": "5"}
H_REVIEWER = {"X-User-Id": "3"}
H_LEAD = {"X-User-Id": "2"}
H_ADMIN = {"X-User-Id": "1"}

def test_duplicate_import_bug():
    print("=" * 70)
    print("BUG 复现测试：返修版本重复导入问题")
    print("=" * 70)

    batch_id = None
    batch_code = f"BATCH-DUP-TEST-{uuid.uuid4().hex[:8].upper()}"
    try:
        r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
            "batch_code": batch_code,
            "name": "重复导入测试批次",
            "description": "测试重复导入相同内容不应生成新版本",
            "submitter_id": 5
        })
        if r.status_code != 201:
            print(f"  创建批次失败: {r.status_code} {r.text}")
            return False, None
        batch_id = r.json()["id"]
        print(f"  [OK] 创建批次 id={batch_id}, code={batch_code}")

        with open("samples/manifest_sample_good.csv", "rb") as f:
            r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
                headers=H_SUBMITTER,
                files={"file": ("manifest_sample_good.csv", f, "text/csv")})
        assert r.status_code == 200 and r.json()["success"] == True
        v1_id = r.json()["manifest_version_id"]
        v1_num = r.json()["version_number"]
        print(f"  [OK] 导入 v1 成功, version={v1_num}, id={v1_id}")

        r = requests.post(f"{API}/api/batches/{batch_id}/transition", headers=H_SUBMITTER, json={
            "target_status": "pending_review"
        })
        assert r.status_code == 200
        print(f"  [OK] 提交待验收")

        r = requests.post(f"{API}/api/batches/{batch_id}/reject", headers=H_REVIEWER, json={
            "comment": "发现问题",
            "rejections": [
                {"item_key": "ITEM-001", "rejection_reason": "需要返修"}
            ]
        })
        assert r.status_code == 200
        print(f"  [OK] reviewer 驳回 1 项")

        r = requests.post(f"{API}/api/batches/{batch_id}/start-repair", headers=H_SUBMITTER)
        assert r.status_code == 200
        print(f"  [OK] 进入返修状态")

        with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
            r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
                headers=H_SUBMITTER,
                files={"file": ("manifest_sample_repaired_v2.csv", f, "text/csv")})
        assert r.status_code == 200 and r.json()["success"] == True
        v2_id = r.json()["manifest_version_id"]
        v2_num = r.json()["version_number"]
        print(f"  [OK] 第一次导入返修清单 v2 成功, version={v2_num}, id={v2_id}")
        assert v2_num == 2, f"预期 v2 版本号应为 2，实际 {v2_num}"

        with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
            r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
                headers=H_SUBMITTER,
                files={"file": ("manifest_sample_repaired_v2.csv", f, "text/csv")})
        result = r.json()
        print(f"  第二次导入相同内容的返回:")
        print(f"    success={result.get('success')}")
        print(f"    version_number={result.get('version_number')}")
        print(f"    manifest_version_id={result.get('manifest_version_id')}")
        print(f"    message={result.get('message')}")

        r = requests.get(f"{API}/api/batches/{batch_id}/manifests", headers=H_ADMIN)
        versions = r.json()
        print(f"  当前版本历史: {len(versions)} 个")
        for v in versions:
            print(f"    v{v['version_number']}: id={v['id']}, items={v['item_count']}")

        r = requests.get(f"{API}/api/batches/{batch_id}/approval-logs", headers=H_ADMIN)
        logs = r.json()
        import_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]
        print(f"  IMPORT 审批日志: {len(import_logs)} 条")
        for l in import_logs:
            vn = l.get('extra_data', {}).get('version_number') if l.get('extra_data') else None
            print(f"    [{str(l['created_at'])[:19]}] v{vn}  by user#{l['actor_id']}")

        print()
        if len(versions) == 3:
            print("  [BUG 复现成功] 重复导入相同内容生成了 v3！")
            print("     版本历史被污染，审批日志被污染")
            return False, batch_id
        elif len(versions) == 2 and result.get("version_number") == 2 and result.get("manifest_version_id") == v2_id:
            print("  [修复验证通过] 重复导入复用了 v2，没有生成 v3！")
            return True, batch_id
        else:
            print(f"  [异常] 版本历史长度: {len(versions)}, 返回版本号: {result.get('version_number')}")
            return False, batch_id

    except Exception as e:
        print(f"  [ERROR] {e}")
        import traceback
        traceback.print_exc()
        return False, batch_id


if __name__ == "__main__":
    success, batch_id = test_duplicate_import_bug()
    print()
    if success:
        print("=" * 70)
        print("测试通过：重复导入已修复。")
    else:
        print("=" * 70)
        print("BUG 存在：重复导入会生成新版本！")
