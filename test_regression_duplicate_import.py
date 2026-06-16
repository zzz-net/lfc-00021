import requests
import json
import uuid

API = "http://127.0.0.1:8000"
H_SUBMITTER = {"X-User-Id": "5"}
H_REVIEWER = {"X-User-Id": "3"}
H_LEAD = {"X-User-Id": "2"}
H_ADMIN = {"X-User-Id": "1"}

passed = 0
failed = 0
test_results = []


def test_case(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        status = "PASS"
    else:
        failed += 1
        status = "FAIL"
    test_results.append((name, status, detail))
    print(f"  [{status}] {name}")
    if not condition and detail:
        print(f"         {detail}")


def test_scenario_1_first_repair_success():
    """场景 1：首次返修导入成功 - 正常路径"""
    print("\n" + "=" * 70)
    print("场景 1：首次返修导入成功 - 正常路径")
    print("=" * 70)

    batch_code = f"BATCH-REG1-{uuid.uuid4().hex[:6].upper()}"

    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": batch_code,
        "name": "回归测试-场景1",
        "submitter_id": 5
    })
    assert r.status_code == 201
    batch_id = r.json()["id"]
    print(f"\n  批次 id={batch_id}, code={batch_code}")

    with open("samples/manifest_sample_good.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v1.csv", f, "text/csv")})
    res = r.json()
    test_case("导入 v1 成功", res["success"] and res["version_number"] == 1,
        f"version={res.get('version_number')}")

    requests.post(f"{API}/api/batches/{batch_id}/transition", headers=H_SUBMITTER,
        json={"target_status": "pending_review"})

    requests.post(f"{API}/api/batches/{batch_id}/reject", headers=H_REVIEWER, json={
        "comment": "问题",
        "rejections": [{"item_key": "ITEM-001", "rejection_reason": "返修"}]
    })

    requests.post(f"{API}/api/batches/{batch_id}/start-repair", headers=H_SUBMITTER)

    with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v2.csv", f, "text/csv")})
    res = r.json()
    test_case("导入 v2 成功", res["success"] and res["version_number"] == 2,
        f"version={res.get('version_number')}")

    r = requests.get(f"{API}/api/batches/{batch_id}/manifests", headers=H_ADMIN)
    versions = r.json()
    test_case("版本历史应有 2 个", len(versions) == 2, f"实际 {len(versions)} 个")

    r = requests.get(f"{API}/api/batches/{batch_id}/approval-logs", headers=H_ADMIN)
    logs = r.json()
    import_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]
    test_case("IMPORT 审批日志应有 2 条", len(import_logs) == 2, f"实际 {len(import_logs)} 条")

    r = requests.get(f"{API}/api/batches/{batch_id}/rejections", headers=H_ADMIN)
    rejs = r.json()
    test_case("驳回记录应全部 resolved", all(r["resolved"] for r in rejs),
        f"resolved={sum(1 for r in rejs if r['resolved'])}/{len(rejs)}")

    return batch_id


def test_scenario_2_duplicate_import_idempotent():
    """场景 2：重复导入完全相同内容应幂等 - 不生成新版本"""
    print("\n" + "=" * 70)
    print("场景 2：重复导入完全相同内容 - 幂等不生成新版本")
    print("=" * 70)

    batch_code = f"BATCH-REG2-{uuid.uuid4().hex[:6].upper()}"

    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": batch_code,
        "name": "回归测试-场景2",
        "submitter_id": 5
    })
    batch_id = r.json()["id"]
    print(f"\n  批次 id={batch_id}, code={batch_code}")

    with open("samples/manifest_sample_good.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v1.csv", f, "text/csv")})
    v1_res = r.json()
    v1_id = v1_res["manifest_version_id"]

    requests.post(f"{API}/api/batches/{batch_id}/transition", headers=H_SUBMITTER,
        json={"target_status": "pending_review"})

    requests.post(f"{API}/api/batches/{batch_id}/reject", headers=H_REVIEWER, json={
        "comment": "问题",
        "rejections": [{"item_key": "ITEM-001", "rejection_reason": "返修"}]
    })

    requests.post(f"{API}/api/batches/{batch_id}/start-repair", headers=H_SUBMITTER)

    with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v2.csv", f, "text/csv")})
    first_res = r.json()
    v2_id = first_res["manifest_version_id"]
    v2_num = first_res["version_number"]
    test_case("首次导入 v2 成功", first_res["success"] and v2_num == 2,
        f"version={v2_num}, id={v2_id}")

    with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v2_dup.csv", f, "text/csv")})
    dup_res = r.json()
    test_case("重复导入应复用 v2",
        dup_res["success"] and dup_res["version_number"] == 2 and dup_res["manifest_version_id"] == v2_id,
        f"返回 version={dup_res.get('version_number')}, id={dup_res.get('manifest_version_id')}")
    test_case("重复导入应有明确提示信息", "复用" in dup_res.get("message", ""),
        f"message={dup_res.get('message')}")

    r = requests.get(f"{API}/api/batches/{batch_id}/manifests", headers=H_ADMIN)
    versions = r.json()
    test_case("版本历史仍应为 2 个（无 v3）", len(versions) == 2,
        f"实际 {len(versions)} 个: {['v' + str(v['version_number']) for v in versions]}")

    r = requests.get(f"{API}/api/batches/{batch_id}/approval-logs", headers=H_ADMIN)
    logs = r.json()
    import_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]
    test_case("审批日志仍应为 2 条 IMPORT（无第 3 条）", len(import_logs) == 2,
        f"实际 {len(import_logs)} 条")

    r = requests.get(f"{API}/api/batches/{batch_id}/rejections", headers=H_ADMIN)
    rejs = r.json()
    test_case("驳回记录仍全部 resolved", all(rr["resolved"] for rr in rejs),
        f"resolved={sum(1 for rr in rejs if rr['resolved'])}/{len(rejs)}")

    r = requests.post(f"{API}/api/batches/{batch_id}/validate", headers=H_SUBMITTER)
    r = requests.post(f"{API}/api/batches/{batch_id}/transition", headers=H_SUBMITTER,
        json={"target_status": "pending_review"})
    r = requests.post(f"{API}/api/batches/{batch_id}/approve", headers=H_LEAD)
    r = requests.post(f"{API}/api/batches/{batch_id}/archive", headers=H_LEAD)

    r = requests.get(f"{API}/api/batches/{batch_id}/acceptance-report", headers=H_ADMIN)
    report = r.json()
    test_case("验收报告 total_versions=2", report["total_versions"] == 2,
        f"实际 {report.get('total_versions')}")
    test_case("验收报告 current_version=2", report["current_version"] == 2,
        f"实际 {report.get('current_version')}")
    test_case("验收报告 item_count=7", report["item_count"] == 7,
        f"实际 {report.get('item_count')}")

    r = requests.get(f"{API}/api/batches/{batch_id}/export-report?format=json", headers=H_ADMIN)
    export = r.json()
    test_case("导出报告版本历史 2 个", len(export["version_history"]) == 2,
        f"实际 {len(export['version_history'])} 个")

    return batch_id


def test_scenario_3_failed_import_no_partial_write():
    """场景 3：导入失败（缺字段）不应产生半截数据"""
    print("\n" + "=" * 70)
    print("场景 3：导入失败（缺字段）不应产生半截数据")
    print("=" * 70)

    batch_code = f"BATCH-REG3-{uuid.uuid4().hex[:6].upper()}"

    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": batch_code,
        "name": "回归测试-场景3",
        "submitter_id": 5
    })
    batch_id = r.json()["id"]
    print(f"\n  批次 id={batch_id}, code={batch_code}")

    with open("samples/manifest_sample_good.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v1.csv", f, "text/csv")})
    v1_res = r.json()
    v1_count_before = v1_res["version_number"]

    r = requests.get(f"{API}/api/batches/{batch_id}/manifests", headers=H_ADMIN)
    versions_before = len(r.json())

    requests.post(f"{API}/api/batches/{batch_id}/transition", headers=H_SUBMITTER,
        json={"target_status": "pending_review"})
    requests.post(f"{API}/api/batches/{batch_id}/reject", headers=H_REVIEWER, json={
        "comment": "问题",
        "rejections": [{"item_key": "ITEM-001", "rejection_reason": "返修"}]
    })
    requests.post(f"{API}/api/batches/{batch_id}/start-repair", headers=H_SUBMITTER)

    with open("samples/manifest_sample_with_errors.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("bad.csv", f, "text/csv")})
    bad_res = r.json()
    test_case("导入错误清单应返回 success=false", bad_res["success"] == False,
        f"success={bad_res.get('success')}")
    test_case("应返回字段错误详情", len(bad_res.get("errors", [])) > 0,
        f"errors count={len(bad_res.get('errors', []))}")

    r = requests.get(f"{API}/api/batches/{batch_id}/manifests", headers=H_ADMIN)
    versions_after = len(r.json())
    test_case("版本历史不应增长", versions_after == versions_before,
        f"之前 {versions_before} 个，之后 {versions_after} 个")

    r = requests.get(f"{API}/api/batches/{batch_id}/approval-logs", headers=H_ADMIN)
    logs_after = [l for l in r.json() if l["action"] == "IMPORT_MANIFEST"]
    test_case("审批日志 IMPORT 条数不应增长", len(logs_after) == versions_before,
        f"应为 {versions_before} 条，实际 {len(logs_after)} 条")

    r = requests.get(f"{API}/api/batches/{batch_id}", headers=H_ADMIN)
    batch = r.json()
    test_case("批次状态应仍为 repairing", batch["status"] == "repairing",
        f"实际 status={batch['status']}")

    return batch_id


def test_scenario_4_restart_persistence():
    """场景 4：服务重启后版本历史、审批日志、导出报告保持一致"""
    print("\n" + "=" * 70)
    print("场景 4：服务重启后数据持久化验证")
    print("=" * 70)

    batch_code = f"BATCH-REG4-{uuid.uuid4().hex[:6].upper()}"

    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": batch_code,
        "name": "回归测试-场景4",
        "submitter_id": 5
    })
    batch_id = r.json()["id"]
    print(f"\n  批次 id={batch_id}, code={batch_code}")

    with open("samples/manifest_sample_good.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v1.csv", f, "text/csv")})

    requests.post(f"{API}/api/batches/{batch_id}/transition", headers=H_SUBMITTER,
        json={"target_status": "pending_review"})
    requests.post(f"{API}/api/batches/{batch_id}/reject", headers=H_REVIEWER, json={
        "comment": "问题",
        "rejections": [{"item_key": "ITEM-001", "rejection_reason": "返修"}]
    })
    requests.post(f"{API}/api/batches/{batch_id}/start-repair", headers=H_SUBMITTER)

    with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v2.csv", f, "text/csv")})

    with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
        r = requests.post(f"{API}/api/batches/{batch_id}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v2_dup.csv", f, "text/csv")})

    r = requests.get(f"{API}/api/batches/{batch_id}/manifests", headers=H_ADMIN)
    versions_before = r.json()
    print(f"  重启前版本历史: {len(versions_before)} 个: {['v' + str(v['version_number']) for v in versions_before]}")

    r = requests.get(f"{API}/api/batches/{batch_id}/approval-logs", headers=H_ADMIN)
    logs_before = r.json()
    import_logs_before = [l for l in logs_before if l["action"] == "IMPORT_MANIFEST"]
    print(f"  重启前 IMPORT 日志: {len(import_logs_before)} 条")

    r = requests.get(f"{API}/api/batches/{batch_id}/acceptance-report", headers=H_ADMIN)
    report_before = r.json()

    r = requests.get(f"{API}/api/batches/{batch_id}/export-report?format=json", headers=H_ADMIN)
    export_before = r.json()

    test_case("重启前版本历史 2 个", len(versions_before) == 2)
    test_case("重启前 IMPORT 日志 2 条", len(import_logs_before) == 2)
    test_case("重启前报告 total_versions=2", report_before["total_versions"] == 2)

    print("\n  *** 请手动重启服务后重新运行此脚本验证场景 4 ***")
    print(f"  *** 或通过 test_persistence.py 验证持久化 ***")

    return batch_id


def run_all_tests():
    print("=" * 70)
    print("返修版本重复导入问题 - 完整回归测试套件")
    print("=" * 70)

    batch_ids = []
    batch_ids.append(test_scenario_1_first_repair_success())
    batch_ids.append(test_scenario_2_duplicate_import_idempotent())
    batch_ids.append(test_scenario_3_failed_import_no_partial_write())
    batch_ids.append(test_scenario_4_restart_persistence())

    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    for name, status, detail in test_results:
        mark = "[PASS]" if status == "PASS" else "[FAIL]"
        print(f"  {mark} {name}")

    print()
    print(f"  总计: PASS={passed}, FAIL={failed}")
    print(f"  涉及批次: {batch_ids}")
    print("=" * 70)

    if failed == 0:
        print("\nAll regression tests passed!")
        return 0
    else:
        print(f"\n{failed} tests failed!")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(run_all_tests())
