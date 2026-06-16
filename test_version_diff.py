import requests
import json
import sys
import random
import string
import time
import os

API = "http://127.0.0.1:8000"
H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER_1 = {"X-User-Id": "3"}
H_REVIEWER_2 = {"X-User-Id": "4"}
H_SUBMITTER_1 = {"X-User-Id": "5"}
H_SUBMITTER_2 = {"X-User-Id": "6"}

passed = 0
failed = 0

SUFFIX = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
BATCH_CODE = f"BATCH-DIFF-TEST-{SUFFIX}"

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


def test(name, response, expect_status=None, expect_success=None, check_fn=None):
    global passed, failed
    ok = True
    msgs = []
    if expect_status and response.status_code != expect_status:
        ok = False
        msgs.append(f"status_code: got {response.status_code}, expect {expect_status}")
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
    if check_fn and data is not None:
        try:
            check_result = check_fn(data)
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
        "name": "版本差异测试批次",
        "description": "用于测试版本差异对比功能的批次",
        "submitter_id": 5
    })
    data = test("创建批次", r, 201, check_fn=lambda d: d.get("batch_code") == BATCH_CODE)
    batch_id = data.get("id")
    assert batch_id, "创建批次失败"

    print(f"\n批次已创建, ID={batch_id}")

    r, _ = precheck_and_import(batch_id, "review_v1.json", V1_FILE, H_SUBMITTER_1, "json")
    test("导入 v1 清单", r, 200, expect_success=True)

    r, _ = precheck_and_import(batch_id, "review_v2.json", V2_FILE, H_SUBMITTER_1, "json")
    test("导入 v2 清单", r, 200, expect_success=True)

    return batch_id


def test_version_diff_main_success(batch_id):
    print("\n" + "=" * 70)
    print("测试 1: 主成功链路 - lead 查看 v1 与 v2 差异")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2",
        headers=H_LEAD
    )
    data = test("lead 调用版本对比接口", r, 200, check_fn=lambda d: all(k in d for k in ["metadata", "summary"]))

    assert data, "响应数据为空"

    metadata = data.get("metadata", {})
    summary = data.get("summary", {})

    test("元数据校验 - 批次信息", r, check_fn=lambda d:
        d["metadata"]["batch_id"] == batch_id and
        d["metadata"]["old_version"] == 1 and
        d["metadata"]["new_version"] == 2)

    test("元数据校验 - 导入信息", r, check_fn=lambda d:
        d["metadata"]["old_import"]["imported_by_username"] == "submitter_chen" and
        d["metadata"]["new_import"]["imported_by_username"] == "submitter_chen")

    test("汇总统计 - 新增条目", r, check_fn=lambda d:
        d["summary"]["added_count"] == 1 and
        d["summary"]["removed_count"] == 0 and
        d["summary"]["modified_count"] == 1 and
        d["summary"]["unchanged_count"] == 1)

    test("汇总统计 - 字段变更数", r, check_fn=lambda d:
        d["summary"]["field_change_count"] >= 1)

    added = data.get("added_items", [])
    test("新增条目 - ITEM-R3", r, check_fn=lambda d:
        len(d["added_items"]) == 1 and
        d["added_items"][0]["item_key"] == "ITEM-R3" and
        d["added_items"][0]["action"] == "added")

    modified = data.get("modified_items", [])
    test("修改条目 - ITEM-R1", r, check_fn=lambda d:
        len(d["modified_items"]) == 1 and
        d["modified_items"][0]["item_key"] == "ITEM-R1" and
        d["modified_items"][0]["action"] == "modified")

    test("修改条目 - 字段变更明细", r, check_fn=lambda d:
        any(c["field_name"] == "item_name" for c in d["modified_items"][0]["field_changes"]) and
        any(c["old_value"] == "Router Board" for c in d["modified_items"][0]["field_changes"]) and
        any(c["new_value"] == "Router Board RevB" for c in d["modified_items"][0]["field_changes"]))

    unchanged = data.get("unchanged_items", [])
    test("未变条目 - ITEM-R2", r, check_fn=lambda d:
        len(d["unchanged_items"]) == 1 and
        d["unchanged_items"][0]["item_key"] == "ITEM-R2" and
        d["unchanged_items"][0]["action"] == "unchanged")

    print("\n  [INFO] 版本差异明细:")
    print(f"    新增: {[i['item_key'] for i in added]}")
    print(f"    删除: {[i['item_key'] for i in data.get('removed_items', [])]}")
    print(f"    修改: {[i['item_key'] for i in modified]}")
    print(f"    未变: {[i['item_key'] for i in unchanged]}")

    return data


def test_version_diff_permission_denied(batch_id):
    print("\n" + "=" * 70)
    print("测试 2: 越权访问失败测试")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers=H_REVIEWER_1
    )
    test("reviewer 越权查看版本差异 - 拒绝", r, 403, expect_success=False,
         check_fn=lambda d: "Permission denied" in d["error"]["message"])

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers=H_REVIEWER_2
    )
    test("reviewer_2 越权查看版本差异 - 拒绝", r, 403, expect_success=False)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers=H_SUBMITTER_2
    )
    test("其他 submitter 越权查看版本差异 - 拒绝", r, 403, expect_success=False,
         check_fn=lambda d: "Permission denied" in d["error"]["message"])

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export",
        headers=H_REVIEWER_1
    )
    test("reviewer 越权导出版本差异 - 拒绝", r, 403, expect_success=False)


def test_version_diff_authorized_users(batch_id):
    print("\n" + "=" * 70)
    print("测试 3: 授权用户访问测试")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers=H_ADMIN
    )
    test("admin 查看版本差异 - 允许", r, 200)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers=H_LEAD
    )
    test("lead 查看版本差异 - 允许", r, 200)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers=H_SUBMITTER_1
    )
    test("提交人本人查看版本差异 - 允许", r, 200)


def test_version_diff_same_version(batch_id):
    print("\n" + "=" * 70)
    print("测试 4: 相同版本对比边界测试")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=1",
        headers=H_LEAD
    )
    data = test("v1 对比 v1 - 无差异", r, 200, check_fn=lambda d:
        d["summary"]["added_count"] == 0 and
        d["summary"]["removed_count"] == 0 and
        d["summary"]["modified_count"] == 0 and
        d["summary"]["unchanged_count"] == 2)

    if data:
        print(f"  [INFO] 相同版本对比结果: 未变={data['summary']['unchanged_count']} 项")


def test_version_diff_default_params(batch_id):
    print("\n" + "=" * 70)
    print("测试 5: 默认参数自动选取最近两个版本")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers=H_LEAD
    )
    test("不指定版本号 - 自动对比最近两个版本", r, 200, check_fn=lambda d:
        d["metadata"]["old_version"] == 1 and
        d["metadata"]["new_version"] == 2)


def test_version_diff_reversed_versions(batch_id):
    print("\n" + "=" * 70)
    print("测试 6: 版本号倒序自动修正")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=2&new_version=1",
        headers=H_LEAD
    )
    test("倒序版本号 - 自动修正为 v1->v2", r, 200, check_fn=lambda d:
        d["metadata"]["old_version"] == 1 and
        d["metadata"]["new_version"] == 2)


def test_version_diff_nonexistent_versions(batch_id):
    print("\n" + "=" * 70)
    print("测试 7: 不存在的版本号错误处理")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=999",
        headers=H_LEAD
    )
    test("新版本不存在 - 404", r, 404, expect_success=False,
         check_fn=lambda d: "不存在" in d["error"]["message"])

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=999&new_version=2",
        headers=H_LEAD
    )
    test("旧版本不存在 - 404", r, 404, expect_success=False)


def test_version_diff_insufficient_versions():
    print("\n" + "=" * 70)
    print("测试 8: 版本不足时的错误处理")
    print("=" * 70)

    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER_1, json={
        "batch_code": f"BATCH-SINGLE-{SUFFIX}",
        "name": "单版本测试批次",
        "description": "只有一个版本的测试批次",
        "submitter_id": 5
    })
    data = test("创建单版本测试批次", r, 201)
    single_batch_id = data.get("id")

    r, _ = precheck_and_import(single_batch_id, "review_v1.json", V1_FILE, H_SUBMITTER_1, "json")
    test("导入单个版本", r, 200, expect_success=True)

    r = requests.get(
        f"{API}/api/batches/{single_batch_id}/version-diff",
        headers=H_LEAD
    )
    test("单版本无法对比 - 400 错误", r, 400, expect_success=False,
         check_fn=lambda d: "至少需要 2 个版本" in d["error"]["message"])


def test_version_diff_export(batch_id):
    print("\n" + "=" * 70)
    print("测试 9: JSON 导出功能")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2",
        headers=H_LEAD
    )
    data = test("导出版本差异 JSON", r, 200, check_fn=lambda d:
        "export_id" in d and "export_timestamp" in d and "diff_data" in d)

    assert data, "导出响应为空"

    export_id_1 = data.get("export_id")
    test("导出 ID 存在", r, check_fn=lambda d: len(d["export_id"]) == 16)

    test("导出内容包含完整差异数据", r, check_fn=lambda d:
        all(k in d["diff_data"] for k in ["metadata", "summary", "added_items", "modified_items"]))

    r2 = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2",
        headers=H_LEAD
    )
    data2 = r2.json()
    export_id_2 = data2.get("export_id")

    test("同一批次同一版本对比 export_id 幂等", r2, check_fn=lambda d:
        d["export_id"] == export_id_1)

    print(f"  [INFO] export_id = {export_id_1} (两次调用一致)")

    return export_id_1


def test_version_diff_audit_logs(batch_id):
    print("\n" + "=" * 70)
    print("测试 10: 审批日志记录检查")
    print("=" * 70)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/approval-logs",
        headers=H_LEAD
    )
    data = test("获取审批日志", r, 200)

    assert isinstance(data, list), "日志应为列表"

    view_logs = [l for l in data if l.get("action") == "VIEW_VERSION_DIFF"]
    export_logs = [l for l in data if l.get("action") == "EXPORT_VERSION_DIFF"]

    test("存在查看版本差异日志", r, check_fn=lambda d:
        any(l["action"] == "VIEW_VERSION_DIFF" for l in d))

    test("存在导出版本差异日志", r, check_fn=lambda d:
        any(l["action"] == "EXPORT_VERSION_DIFF" for l in d))

    if view_logs:
        test("查看日志记录版本信息", r, check_fn=lambda d:
            "old_version" in view_logs[0].get("extra_data", {}) and
            "new_version" in view_logs[0].get("extra_data", {}))

    if export_logs:
        test("导出日志记录 export_id", r, check_fn=lambda d:
            "export_id" in export_logs[0].get("extra_data", {}))

    print(f"  [INFO] VIEW_VERSION_DIFF 日志数: {len(view_logs)}")
    print(f"  [INFO] EXPORT_VERSION_DIFF 日志数: {len(export_logs)}")


def test_version_diff_with_validation(batch_id):
    print("\n" + "=" * 70)
    print("测试 11: 校验结果变化关联")
    print("=" * 70)

    r = requests.post(
        f"{API}/api/batches/{batch_id}/validate",
        headers=H_SUBMITTER_1
    )
    test("对当前版本执行校验", r, 200, expect_success=True)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2",
        headers=H_LEAD
    )
    data = test("获取含校验信息的版本差异", r, 200)

    if data:
        summary = data.get("summary", {})
        print(f"  [INFO] v1 校验错误: {summary.get('validation_errors_old')}, "
              f"v2 校验错误: {summary.get('validation_errors_new')}")
        print(f"  [INFO] v1 校验警告: {summary.get('validation_warnings_old')}, "
              f"v2 校验警告: {summary.get('validation_warnings_new')}")

        test("校验统计字段存在", r, check_fn=lambda d:
            "validation_errors_old" in d["summary"] and
            "validation_errors_new" in d["summary"] and
            "validation_warnings_old" in d["summary"] and
            "validation_warnings_new" in d["summary"])

        test("校验变化列表存在", r, check_fn=lambda d:
            "validation_changes" in d)


def test_version_diff_rejection_data(batch_id):
    print("\n" + "=" * 70)
    print("测试 12: 未解决驳回关联")
    print("=" * 70)

    r = requests.post(
        f"{API}/api/batches/{batch_id}/transition",
        headers=H_SUBMITTER_1,
        json={"target_status": "pending_review", "comment": "提交评审"}
    )
    test("提交批次到待评审", r, 200)

    r = requests.post(
        f"{API}/api/batches/{batch_id}/reject",
        headers=H_REVIEWER_1,
        json={
            "rejections": [
                {"item_key": "ITEM-R1", "rejection_reason": "名称需要进一步确认"},
                {"item_key": "ITEM-R3", "rejection_reason": "新增项目需要审批"}
            ],
            "comment": "发现两个问题需要修正"
        }
    )
    test("reviewer 驳回两项问题", r, 200, expect_success=True)

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2",
        headers=H_LEAD
    )
    data = test("获取含驳回信息的版本差异", r, 200)

    if data:
        rejections = data.get("unresolved_rejections", [])
        summary = data.get("summary", {})

        test("未解决驳回数量统计", r, check_fn=lambda d:
            d["summary"]["unresolved_rejections_new"] == 2)

        test("未解决驳回明细存在", r, check_fn=lambda d:
            len(d["unresolved_rejections"]) == 2)

        test("驳回信息包含 rejector", r, check_fn=lambda d:
            all(r.get("rejector_username") for r in d["unresolved_rejections"]))

        print(f"  [INFO] 未解决驳回: {summary.get('unresolved_rejections_new')} 项")
        for rej in rejections:
            print(f"    - {rej.get('item_key')}: {rej.get('rejection_reason')} "
                  f"(by {rej.get('rejector_username')})")


def test_validation_state_transitions():
    global passed, failed
    print("\n" + "=" * 70)
    print("测试 13: 通过/违规切换边界（修复校验误报）")
    print("=" * 70)

    tmp_dir = "reviewer_generated"
    os.makedirs(tmp_dir, exist_ok=True)

    v1_bad = [
        {"item_id": "ITEM-R1", "item_name": "Router Board", "quantity": -1, "unit_price": 1200},
        {"item_id": "ITEM-R2", "item_name": "Power Module", "quantity": 4, "unit_price": 300}
    ]
    v2_fixed = [
        {"item_id": "ITEM-R1", "item_name": "Router Board", "quantity": 2, "unit_price": 1200},
        {"item_id": "ITEM-R2", "item_name": "Power Module", "quantity": 4, "unit_price": 300},
        {"item_id": "ITEM-R3", "item_name": "Fan Tray", "quantity": 8, "unit_price": 150}
    ]
    v2_bad = [
        {"item_id": "ITEM-R1", "item_name": "Router Board", "quantity": -1, "unit_price": 1200},
        {"item_id": "ITEM-R2", "item_name": "Power Module", "quantity": -3, "unit_price": 300}
    ]

    v1_bad_path = os.path.join(tmp_dir, f"val_v1_bad_{SUFFIX}.json")
    v2_fixed_path = os.path.join(tmp_dir, f"val_v2_fixed_{SUFFIX}.json")
    v2_bad_path = os.path.join(tmp_dir, f"val_v2_bad_{SUFFIX}.json")

    with open(v1_bad_path, "w", encoding="utf-8") as f:
        json.dump(v1_bad, f, ensure_ascii=False, indent=2)
    with open(v2_fixed_path, "w", encoding="utf-8") as f:
        json.dump(v2_fixed, f, ensure_ascii=False, indent=2)
    with open(v2_bad_path, "w", encoding="utf-8") as f:
        json.dump(v2_bad, f, ensure_ascii=False, indent=2)

    def _create_and_import(batch_name, paths):
        code = f"{BATCH_CODE}-{batch_name.replace(' ', '')[-8:]}"
        r = requests.post(
            f"{API}/api/batches",
            headers=H_SUBMITTER_1,
            json={"batch_code": code, "name": batch_name, "description": batch_name, "submitter_id": 5}
        )
        if r.status_code != 201:
            return None
        bid = r.json()["id"]
        for p in paths:
            rr, _ = precheck_and_import(bid, os.path.basename(p), p, H_SUBMITTER_1)
            if rr.status_code != 200:
                return None
            requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER_1)
        return bid

    bid_resolve = _create_and_import(
        "val-resolve-test", [v1_bad_path, v2_fixed_path]
    )
    if bid_resolve is None:
        failed += 1
        print("  [FAIL] 场景A: 创建 resolve 批次(违规→修复+新增)")
    else:
        passed += 1
        print("  [PASS] 场景A: 创建 resolve 批次(违规→修复+新增)")

    if bid_resolve:
        r = requests.get(
            f"{API}/api/batches/{bid_resolve}/version-diff?old_version=1&new_version=2",
            headers=H_LEAD
        )
        data = test("场景A: 获取版本差异", r, 200)
        if data:
            changes = data.get("validation_changes", [])
            print(f"  [INFO] validation_changes 共 {len(changes)} 条")
            for c in changes:
                print(f"    - {c['item_key']}/{c.get('field_name', '')}/{c.get('rule_code', '')}: {c['change_type']}")

            resolved_changes = [c for c in changes if c["change_type"] == "resolved"]
            new_violations = [c for c in changes if c["change_type"] == "new_violation"]
            modified_changes = [c for c in changes if c["change_type"] == "modified"]

            test("场景A: ITEM-R1 违规修复 change_type=resolved", r, check_fn=lambda d:
                 any(c["item_key"] == "ITEM-R1" and c["change_type"] == "resolved"
                     for c in d["validation_changes"]))

            test("场景A: ITEM-R3(新增且校验通过) 不应出现在 new_violation", r, check_fn=lambda d:
                 not any(c["item_key"] == "ITEM-R3" and c["change_type"] == "new_violation"
                         for c in d["validation_changes"]))

            test("场景A: ITEM-R3(新增且校验通过) 不应出现在 validation_changes", r, check_fn=lambda d:
                 not any(c["item_key"] == "ITEM-R3" for c in d["validation_changes"]))

            test("场景A: 不应有误报的 new_violation", r, check_fn=lambda d:
                 len([c for c in d["validation_changes"]
                      if c["change_type"] == "new_violation" and c.get("new_passed")]) == 0)

    bid_new_vio = _create_and_import(
        "val-newviolation-test", [v1_bad_path, v2_bad_path]
    )
    if bid_new_vio is None:
        failed += 1
        print("  [FAIL] 场景B: 创建 new_violation 批次(违规→新增违规)")
    else:
        passed += 1
        print("  [PASS] 场景B: 创建 new_violation 批次(违规→新增违规)")

    if bid_new_vio:
        r = requests.get(
            f"{API}/api/batches/{bid_new_vio}/version-diff?old_version=1&new_version=2",
            headers=H_LEAD
        )
        data = test("场景B: 获取版本差异", r, 200)
        if data:
            changes = data.get("validation_changes", [])
            print(f"  [INFO] validation_changes 共 {len(changes)} 条")
            for c in changes:
                print(f"    - {c['item_key']}/{c.get('field_name', '')}/{c.get('rule_code', '')}: {c['change_type']}")

            test("场景B: ITEM-R2 新增违规 change_type=new_violation", r, check_fn=lambda d:
                 any(c["item_key"] == "ITEM-R2" and c["change_type"] == "new_violation"
                     for c in d["validation_changes"]))

            test("场景B: ITEM-R1 仍违规且字段相同不应计入", r, check_fn=lambda d:
                 not any(c["item_key"] == "ITEM-R1" and c["change_type"] == "modified"
                         for c in d["validation_changes"]))

    os.remove(v1_bad_path)
    os.remove(v2_fixed_path)
    os.remove(v2_bad_path)


def test_export_api_consistency():
    global passed, failed
    print("\n" + "=" * 70)
    print("测试 14: 接口与导出 JSON 一致性")
    print("=" * 70)

    r = requests.post(
        f"{API}/api/batches",
        headers=H_SUBMITTER_1,
        json={"batch_code": f"{BATCH_CODE}-CONSIST", "name": f"一致性测试批次-{SUFFIX}", "description": "接口 vs 导出一致性", "submitter_id": 5}
    )
    data = test("创建一致性测试批次", r, 201)
    if not data:
        return
    bid = data["id"]

    rr, _ = precheck_and_import(bid, os.path.basename(V1_FILE), V1_FILE, H_SUBMITTER_1)
    test("一致性测试导入 v1", rr, 200, expect_success=True)
    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER_1)

    rr, _ = precheck_and_import(bid, os.path.basename(V2_FILE), V2_FILE, H_SUBMITTER_1)
    test("一致性测试导入 v2", rr, 200, expect_success=True)
    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER_1)

    r_api = requests.get(
        f"{API}/api/batches/{bid}/version-diff?old_version=1&new_version=2",
        headers=H_LEAD
    )
    api_data = test("调用接口 version-diff", r_api, 200)

    r_export = requests.get(
        f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2",
        headers=H_LEAD
    )
    export_data = test("调用 export 接口", r_export, 200)

    if api_data and export_data:
        diff_from_api = api_data
        diff_from_export = export_data.get("diff_data")

        def _check(name, cond, detail=""):
            global passed, failed
            if cond:
                passed += 1
                print(f"  [PASS] {name}")
            else:
                failed += 1
                print(f"  [FAIL] {name}  --  {detail}")

        test("导出结果包含 export_id", r_export, check_fn=lambda d:
             len(d.get("export_id", "")) == 16)

        _check("接口与导出: summary.added_count 一致",
               diff_from_api["summary"]["added_count"] == diff_from_export["summary"]["added_count"],
               f"{diff_from_api['summary']['added_count']} vs {diff_from_export['summary']['added_count']}")

        _check("接口与导出: summary.removed_count 一致",
               diff_from_api["summary"]["removed_count"] == diff_from_export["summary"]["removed_count"],
               f"{diff_from_api['summary']['removed_count']} vs {diff_from_export['summary']['removed_count']}")

        _check("接口与导出: summary.modified_count 一致",
               diff_from_api["summary"]["modified_count"] == diff_from_export["summary"]["modified_count"],
               f"{diff_from_api['summary']['modified_count']} vs {diff_from_export['summary']['modified_count']}")

        _check("接口与导出: summary.validation_errors_new 一致",
               diff_from_api["summary"].get("validation_errors_new") == diff_from_export["summary"].get("validation_errors_new"),
               f"{diff_from_api['summary'].get('validation_errors_new')} vs {diff_from_export['summary'].get('validation_errors_new')}")

        api_vc = diff_from_api.get("validation_changes", [])
        export_vc = diff_from_export.get("validation_changes", [])
        _check(f"接口与导出: validation_changes 数量一致 ({len(api_vc)} vs {len(export_vc)})",
               len(api_vc) == len(export_vc))

        api_rej = diff_from_api.get("unresolved_rejections", [])
        export_rej = diff_from_export.get("unresolved_rejections", [])
        _check(f"接口与导出: unresolved_rejections 数量一致 ({len(api_rej)} vs {len(export_rej)})",
               len(api_rej) == len(export_rej))

        api_item_keys_added = sorted([i["item_key"] for i in diff_from_api.get("added_items", [])])
        exp_item_keys_added = sorted([i["item_key"] for i in diff_from_export.get("added_items", [])])
        _check("接口与导出: added_items item_key 一致",
               api_item_keys_added == exp_item_keys_added,
               f"{api_item_keys_added} vs {exp_item_keys_added}")

        r2 = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2",
            headers=H_LEAD
        )
        export2 = test("再次调用 export", r2, 200)
        if export2:
            _check("同批次同版本对比: export_id 幂等一致",
                   export_data.get("export_id") == export2.get("export_id"),
                   f"{export_data.get('export_id')} vs {export2.get('export_id')}")

        print(f"  [INFO] export_id = {export_data.get('export_id')}")


def main():
    print("\n" + "=" * 70)
    print("版本差异对比功能回归测试套件")
    print(f"测试批次: {BATCH_CODE}")
    print(f"测试端点: {API}")
    print("=" * 70)

    try:
        r = requests.get(f"{API}/health")
        if r.status_code != 200:
            print("错误: 服务未启动, 请先运行: python -m uvicorn main:app --host 127.0.0.1 --port 8000")
            sys.exit(1)
    except requests.ConnectionError:
        print("错误: 无法连接到服务, 请先启动服务")
        sys.exit(1)

    batch_id = setup_batch()

    test_version_diff_main_success(batch_id)
    test_version_diff_permission_denied(batch_id)
    test_version_diff_authorized_users(batch_id)
    test_version_diff_same_version(batch_id)
    test_version_diff_default_params(batch_id)
    test_version_diff_reversed_versions(batch_id)
    test_version_diff_nonexistent_versions(batch_id)
    test_version_diff_insufficient_versions()
    export_id = test_version_diff_export(batch_id)
    test_version_diff_audit_logs(batch_id)
    test_version_diff_with_validation(batch_id)
    test_version_diff_rejection_data(batch_id)
    test_validation_state_transitions()
    test_export_api_consistency()

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
        print(f"\n测试批次 ID: {batch_id}")
        print(f"导出示例 export_id: {export_id}")
        print("\n你可以通过以下命令手动验证:")
        print(f"  curl -H 'X-User-Id: 2' '{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2'")
        print(f"  curl -H 'X-User-Id: 2' '{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2'")
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
