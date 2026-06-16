#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
版本差异快照与导出功能 - 完整回归测试套件
覆盖: 快照自动创建、快照查询(latest/by-versions/by-id/list)、
      CSV/JSON 导出、权限控制、版本隔离、幂等性、审计日志
"""

import requests
import json
import sys
import random
import string
import time
import os

API = "http://127.0.0.1:8001"
H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER_1 = {"X-User-Id": "3"}
H_REVIEWER_2 = {"X-User-Id": "4"}
H_SUBMITTER_1 = {"X-User-Id": "5"}
H_SUBMITTER_2 = {"X-User-Id": "6"}

passed = 0
failed = 0

SUFFIX = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
BATCH_CODE = f"BATCH-SNAPSHOT-{SUFFIX}"

V1_FILE = "reviewer_generated/review_v1.json"
V2_FILE = "reviewer_generated/review_v2.json"


def _safe_get(data, key, default=None):
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def precheck_and_import(bid, filename, filepath, user=H_SUBMITTER_1, import_format="json"):
    mime = "application/json" if import_format == "json" else "text/csv"
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/precheck",
            headers=user,
            files={"file": (filename, f, mime)},
            data={"import_format": import_format},
        )
    if r.status_code != 200:
        return r, None
    body = r.json()
    if not body.get("can_import"):
        return r, None
    token = body.get("precheck_token")
    if not token:
        return r, None
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=user,
            files={"file": (filename, f, mime)},
            data={"import_format": import_format, "precheck_token": token},
        )
    return r, token


def test(name, response, expect_status=None, expect_success=None, check_fn=None, parse_json=True):
    global passed, failed
    ok = True
    msgs = []
    data = None
    if expect_status and response.status_code != expect_status:
        ok = False
        msgs.append(f"status_code: got {response.status_code}, expect {expect_status}")
    if parse_json:
        try:
            data = response.json()
        except Exception:
            data = None
            ok = False
            msgs.append("invalid JSON response")
    if data is not None and expect_success is not None:
        succ = data.get("success", True) if isinstance(data, dict) else True
        if succ != expect_success:
            ok = False
            msgs.append(f"success: got {succ}, expect {expect_success}")
    if check_fn:
        try:
            check_arg = data if parse_json else response
            check_result = check_fn(check_arg)
            if check_result is not True:
                ok = False
                msgs.append(f"check failed: {check_result}")
        except Exception as e:
            ok = False
            msgs.append(f"check_fn error: {type(e).__name__}: {e}")
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  --  {'; '.join(msgs)}")
        if data is not None:
            print(f"         response: {json.dumps(data, ensure_ascii=False)[:800]}")
    return data


def setup_batch():
    print("=" * 70)
    print("设置测试环境: 创建批次 + 导入 v1 和 v2")
    print("=" * 70)

    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER_1, json={
        "batch_code": BATCH_CODE,
        "name": "快照测试批次",
        "description": "用于测试快照、CSV导出、权限控制的批次",
        "submitter_id": 5
    })
    data = test("创建批次", r, 201, check_fn=lambda d: d.get("batch_code") == BATCH_CODE)
    batch_id = data.get("id")
    assert batch_id, "创建批次失败"

    print(f"\n批次已创建, ID={batch_id}")

    r, _ = precheck_and_import(batch_id, "review_v1.json", V1_FILE, H_SUBMITTER_1, "json")
    test("导入 v1 清单", r, 200, expect_success=True)

    r, _ = precheck_and_import(batch_id, "review_v2.json", V2_FILE, H_SUBMITTER_1, "json")
    data = test("导入 v2 清单(应自动创建快照)", r, 200, expect_success=True)

    return batch_id


def test_snapshot_auto_created(batch_id):
    print("\n" + "=" * 70)
    print("测试 1: 导入新版本后自动创建快照")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots",
        headers=H_LEAD
    )
    data = test("列出快照 - 有数据", r, 200, check_fn=lambda d: d["total"] >= 1)

    if data and data["total"] >= 1:
        snap = data["snapshots"][0]
        test("快照版本号正确", r, check_fn=lambda d:
             d["snapshots"][0]["old_version_number"] == 1 and
             d["snapshots"][0]["new_version_number"] == 2)
        test("快照状态为 valid", r, check_fn=lambda d:
             d["snapshots"][0]["status"] == "valid")
        test("快照包含 content_hash", r, check_fn=lambda d:
             len(d["snapshots"][0].get("content_hash", "")) == 64)
        test("快照包含 has_added/has_modified 标记", r, check_fn=lambda d:
             "has_added" in d["snapshots"][0] and
             "has_modified" in d["snapshots"][0])

        print(f"  [INFO] 快照 ID={snap['id']}, key={snap['snapshot_key']}")
        print(f"  [INFO] has_added={snap['has_added']}, has_modified={snap['has_modified']}")


def test_snapshot_query_variants(batch_id):
    print("\n" + "=" * 70)
    print("测试 2: 多种快照查询方式(latest/by-versions/by-id)")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots/latest",
        headers=H_LEAD
    )
    latest = test("查询 latest 快照", r, 200, check_fn=lambda d:
                  "added_items" in d and "modified_items" in d)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots/by-versions?old_version=1&new_version=2",
        headers=H_LEAD
    )
    by_ver = test("按版本号 v1->v2 查询快照", r, 200, check_fn=lambda d:
                  d["old_version_number"] == 1 and d["new_version_number"] == 2)

    if latest:
        sid = latest["id"]
        r = requests.get(
            f"{API}/api/batches/{batch_id}/snapshots/{sid}",
            headers=H_LEAD
        )
        by_id = test("按快照 ID 查询", r, 200, check_fn=lambda d: d["id"] == sid)

        if by_ver and by_id:
            test("三种查询方式 content_hash 一致", r, check_fn=lambda d:
                 latest["content_hash"] == by_ver["content_hash"] == by_id["content_hash"])
            test("三种查询方式 summary 一致", r, check_fn=lambda d:
                 latest["summary"]["added_count"] == by_ver["summary"]["added_count"] == by_id["summary"]["added_count"])


def test_snapshot_vs_live_diff_consistency(batch_id):
    print("\n" + "=" * 70)
    print("测试 3: 快照数据与实时计算的 version-diff 一致性")
    print("=" * 70)

    r_diff = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2",
        headers=H_LEAD
    )
    diff_data = test("获取实时 version-diff", r_diff, 200)

    r_snap = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots/by-versions?old_version=1&new_version=2",
        headers=H_LEAD
    )
    snap_data = test("获取对应快照", r_snap, 200)

    if diff_data and snap_data:
        def _check(name, cond, detail=""):
            global passed, failed
            if cond:
                passed += 1
                print(f"  [PASS] {name}")
            else:
                failed += 1
                print(f"  [FAIL] {name}  --  {detail}")

        _check("summary.added_count 一致",
               diff_data["summary"]["added_count"] == snap_data["summary"]["added_count"])
        _check("summary.removed_count 一致",
               diff_data["summary"]["removed_count"] == snap_data["summary"]["removed_count"])
        _check("summary.modified_count 一致",
               diff_data["summary"]["modified_count"] == snap_data["summary"]["modified_count"])
        _check("added_items item_key 一致",
               sorted([i["item_key"] for i in diff_data["added_items"]]) ==
               sorted([i["item_key"] for i in snap_data["added_items"]]))
        _check("modified_items item_key 一致",
               sorted([i["item_key"] for i in diff_data["modified_items"]]) ==
               sorted([i["item_key"] for i in snap_data["modified_items"]]))
        _check("unresolved_rejections 数量一致",
               len(diff_data["unresolved_rejections"]) == len(snap_data["unresolved_rejections"]))
        _check("validation_changes 数量一致",
               len(diff_data["validation_changes"]) == len(snap_data["validation_changes"]))


def test_snapshot_permission_denied(batch_id):
    print("\n" + "=" * 70)
    print("测试 4: 快照查询权限控制 - 越权拒绝")
    print("=" * 70)

    endpoints = [
        ("列出快照", f"{API}/api/batches/{batch_id}/snapshots"),
        ("查询 latest", f"{API}/api/batches/{batch_id}/snapshots/latest"),
        ("按版本查询", f"{API}/api/batches/{batch_id}/snapshots/by-versions?old_version=1&new_version=2"),
    ]

    for name, url in endpoints:
        for header_name, headers in [("reviewer_1", H_REVIEWER_1), ("other_submitter", H_SUBMITTER_2)]:
            r = requests.get(url, headers=headers)
            test(f"{header_name} 越权 {name} - 403", r, 403, expect_success=False,
                 check_fn=lambda d: "Permission denied" in d.get("error", {}).get("message", ""))


def test_snapshot_permission_allowed(batch_id):
    print("\n" + "=" * 70)
    print("测试 5: 快照查询权限控制 - 合法用户允许")
    print("=" * 70)

    url = f"{API}/api/batches/{batch_id}/snapshots"
    for name, headers in [("admin", H_ADMIN), ("lead", H_LEAD), ("submitter_本人", H_SUBMITTER_1)]:
        r = requests.get(url, headers=headers)
        test(f"{name} 查询快照 - 200", r, 200)


def test_csv_export(batch_id):
    print("\n" + "=" * 70)
    print("测试 6: CSV 导出功能")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2&format=csv",
        headers=H_LEAD
    )
    test("CSV 导出 - 200", r, 200, parse_json=False)

    content = r.text
    test("CSV 包含表头行", r, parse_json=False, check_fn=lambda resp:
         "change_category" in content and "item_key" in content and "field_name" in content)
    test("CSV 包含 item_added 行", r, parse_json=False, check_fn=lambda resp: "item_added" in content)
    test("CSV 包含 item_modified 行", r, parse_json=False, check_fn=lambda resp: "item_modified" in content)

    lines = content.strip().split("\n")
    test(f"CSV 行数合理 (header + 数据 >= 3): 实际 {len(lines)} 行", r, parse_json=False,
         check_fn=lambda resp: len(lines) >= 3)

    print(f"  [INFO] CSV 共 {len(lines)} 行, 前 3 行:")
    for line in lines[:3]:
        print(f"    {line[:100]}")


def test_json_export(batch_id):
    print("\n" + "=" * 70)
    print("测试 7: JSON 导出功能")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2&format=json",
        headers=H_LEAD
    )
    data = test("JSON 导出 - 200", r, 200, check_fn=lambda d:
                "export_id" in d and "export_timestamp" in d and "diff_data" in d)

    if data:
        test("JSON 导出 ID 长度 16", r, check_fn=lambda d: len(d["export_id"]) == 16)
        test("JSON 导出包含 diff_data.summary", r, check_fn=lambda d: "summary" in d["diff_data"])


def test_export_format_validation(batch_id):
    print("\n" + "=" * 70)
    print("测试 8: 导出格式参数验证")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?format=xml",
        headers=H_LEAD
    )
    test("不支持的导出格式 xml - 400", r, 400, expect_success=False)


def test_export_permission_denied(batch_id):
    print("\n" + "=" * 70)
    print("测试 9: 导出权限控制 - 越权拒绝")
    print("=" * 70)

    for fmt in ["json", "csv"]:
        r = requests.get(
            f"{API}/api/batches/{batch_id}/version-diff/export?format={fmt}",
            headers=H_REVIEWER_1
        )
        test(f"reviewer 越权导出 {fmt} - 403", r, 403, expect_success=False)


def test_version_isolation():
    print("\n" + "=" * 70)
    print("测试 10: 不同批次版本隔离 - 互不混淆")
    print("=" * 70)

    r1 = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER_1, json={
        "batch_code": f"{BATCH_CODE}-ISO-A",
        "name": "隔离测试批次A",
        "submitter_id": 5
    })
    data1 = test("创建隔离批次A", r1, 201)
    bid_a = data1.get("id")

    r2 = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER_1, json={
        "batch_code": f"{BATCH_CODE}-ISO-B",
        "name": "隔离测试批次B",
        "submitter_id": 5
    })
    data2 = test("创建隔离批次B", r2, 201)
    bid_b = data2.get("id")

    if bid_a and bid_b:
        r, _ = precheck_and_import(bid_a, "review_v1.json", V1_FILE, H_SUBMITTER_1, "json")
        test("批次A导入v1", r, 200, expect_success=True)
        r, _ = precheck_and_import(bid_a, "review_v2.json", V2_FILE, H_SUBMITTER_1, "json")
        test("批次A导入v2(产生A的快照)", r, 200, expect_success=True)

        r, _ = precheck_and_import(bid_b, "review_v1.json", V1_FILE, H_SUBMITTER_1, "json")
        test("批次B导入v1", r, 200, expect_success=True)

        r = requests.get(f"{API}/api/batches/{bid_a}/snapshots", headers=H_LEAD)
        test("批次A快照数量正确", r, 200, check_fn=lambda d: d["total"] == 1)

        r = requests.get(f"{API}/api/batches/{bid_b}/snapshots", headers=H_LEAD)
        test("批次B快照数量为0(仅1个版本)", r, 200, check_fn=lambda d: d["total"] == 0)

        r = requests.get(
            f"{API}/api/batches/{bid_a}/snapshots/by-versions?old_version=1&new_version=2",
            headers=H_LEAD
        )
        test("批次A v1->v2 快照存在", r, 200)

        r = requests.get(
            f"{API}/api/batches/{bid_b}/snapshots/by-versions?old_version=1&new_version=2",
            headers=H_LEAD
        )
        test("批次B v1->v2 快照不存在 - 404", r, 404, expect_success=False)


def test_audit_logs_snapshot(batch_id):
    print("\n" + "=" * 70)
    print("测试 11: 快照/导出相关审计日志")
    print("=" * 70)

    requests.get(f"{API}/api/batches/{batch_id}/snapshots", headers=H_LEAD)
    requests.get(f"{API}/api/batches/{batch_id}/snapshots/latest", headers=H_LEAD)
    requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?format=csv",
        headers=H_LEAD
    )

    r = requests.get(
        f"{API}/api/batches/{batch_id}/approval-logs",
        headers=H_LEAD
    )
    data = test("获取审批日志", r, 200)

    if isinstance(data, list):
        actions = [l.get("action") for l in data]
        test("日志包含 CREATE_DIFF_SNAPSHOT", r, check_fn=lambda d:
             "CREATE_DIFF_SNAPSHOT" in actions)
        test("日志包含 QUERY_DIFF_SNAPSHOT", r, check_fn=lambda d:
             "QUERY_DIFF_SNAPSHOT" in actions)
        test("日志包含 EXPORT_DIFF_SNAPSHOT_CSV", r, check_fn=lambda d:
             "EXPORT_DIFF_SNAPSHOT_CSV" in actions)
        test("日志包含 EXPORT_VERSION_DIFF", r, check_fn=lambda d:
             "EXPORT_VERSION_DIFF" in actions or "VIEW_VERSION_DIFF" in actions)

        create_logs = [l for l in data if l.get("action") == "CREATE_DIFF_SNAPSHOT"]
        if create_logs:
            test("CREATE_DIFF_SNAPSHOT 日志含 snapshot_id extra", r, check_fn=lambda d:
                 "snapshot_id" in (create_logs[0].get("extra_data") or {}))


def test_snapshot_query_nonexistent(batch_id):
    print("\n" + "=" * 70)
    print("测试 12: 不存在的快照查询边界")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots/by-versions?old_version=1&new_version=999",
        headers=H_LEAD
    )
    test("不存在的版本对查询 - 404", r, 404, expect_success=False)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots/99999",
        headers=H_LEAD
    )
    test("不存在的快照ID查询 - 404", r, 404, expect_success=False)


def test_export_idempotency(batch_id):
    print("\n" + "=" * 70)
    print("测试 13: 导出幂等性 - 同批次同版本同格式 ID 一致")
    print("=" * 70)

    r1 = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2&format=json",
        headers=H_LEAD
    )
    r2 = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2&format=json",
        headers=H_LEAD
    )
    d1 = r1.json() if r1.status_code == 200 else None
    d2 = r2.json() if r2.status_code == 200 else None

    if d1 and d2:
        test("两次 JSON 导出 export_id 一致", r1, check_fn=lambda d:
             d1["export_id"] == d2["export_id"])
        test("两次 JSON 导出 content 一致", r1, check_fn=lambda d:
             json.dumps(d1["diff_data"]["summary"], sort_keys=True) ==
             json.dumps(d2["diff_data"]["summary"], sort_keys=True))


def test_list_snapshots_pagination(batch_id):
    print("\n" + "=" * 70)
    print("测试 14: 快照列表分页参数")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots?limit=1&offset=0",
        headers=H_LEAD
    )
    test("limit=1 只返回1条", r, 200, check_fn=lambda d: len(d["snapshots"]) == 1)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots?status=valid",
        headers=H_LEAD
    )
    test("status=valid 过滤正常", r, 200, check_fn=lambda d:
         all(s["status"] == "valid" for s in d["snapshots"]))

    r = requests.get(
        f"{API}/api/batches/{batch_id}/snapshots?status=invalid_status",
        headers=H_LEAD
    )
    test("无效 status 参数 - 400", r, 400, expect_success=False)


def test_version_diff_uses_snapshot(batch_id):
    print("\n" + "=" * 70)
    print("测试 15: version-diff 接口优先使用快照")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2",
        headers=H_LEAD
    )
    test("version-diff 返回正常", r, 200)

    r_logs = requests.get(
        f"{API}/api/batches/{batch_id}/approval-logs",
        headers=H_LEAD
    )
    if r_logs.status_code == 200:
        logs = r_logs.json()
        view_logs = [l for l in logs if l.get("action") == "VIEW_VERSION_DIFF"]
        if view_logs:
            extra = view_logs[-1].get("extra_data") or {}
            test("VIEW_VERSION_DIFF 日志标记 from_snapshot=true", r_logs, check_fn=lambda d:
                 extra.get("from_snapshot") is True)
            test("VIEW_VERSION_DIFF 日志含 snapshot_id", r_logs, check_fn=lambda d:
                 "snapshot_id" in extra)
            print(f"  [INFO] from_snapshot={extra.get('from_snapshot')}, snapshot_id={extra.get('snapshot_id')}")


def main():
    print("\n" + "=" * 70)
    print("版本差异快照与导出 - 完整回归测试套件")
    print(f"测试批次: {BATCH_CODE}")
    print(f"测试端点: {API}")
    print("=" * 70)

    try:
        r = requests.get(f"{API}/health")
        if r.status_code != 200:
            print(f"错误: 服务未启动, 请先运行: python -m uvicorn main:app --host 127.0.0.1 --port 8001")
            sys.exit(1)
    except requests.ConnectionError:
        print("错误: 无法连接到服务, 请先启动服务 (port 8001)")
        sys.exit(1)

    batch_id = setup_batch()

    test_snapshot_auto_created(batch_id)
    test_snapshot_query_variants(batch_id)
    test_snapshot_vs_live_diff_consistency(batch_id)
    test_snapshot_permission_denied(batch_id)
    test_snapshot_permission_allowed(batch_id)
    test_csv_export(batch_id)
    test_json_export(batch_id)
    test_export_format_validation(batch_id)
    test_export_permission_denied(batch_id)
    test_version_isolation()
    test_audit_logs_snapshot(batch_id)
    test_snapshot_query_nonexistent(batch_id)
    test_export_idempotency(batch_id)
    test_list_snapshots_pagination(batch_id)
    test_version_diff_uses_snapshot(batch_id)

    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    print(f"通过: {passed}")
    print(f"失败: {failed}")
    print(f"总计: {passed + failed}")
    print(f"成功率: {(passed / (passed + failed) * 100):.1f}%" if (passed + failed) > 0 else "无测试")

    if failed > 0:
        print("\n[FAIL] 存在测试失败!")
        sys.exit(1)
    else:
        print("\n[PASS] 所有测试通过!")
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[ERROR] 测试执行异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
