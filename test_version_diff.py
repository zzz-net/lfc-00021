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

    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    print(f"通过: {passed}")
    print(f"失败: {failed}")
    print(f"总计: {passed + failed}")
    print(f"成功率: {(passed / (passed + failed) * 100):.1f}%" if (passed + failed) > 0 else "无测试")

    if failed > 0:
        print("\n❌ 存在测试失败!")
        sys.exit(1)
    else:
        print("\n✅ 所有测试通过!")
        print(f"\n测试批次 ID: {batch_id}")
        print(f"导出示例 export_id: {export_id}")
        print("\n你可以通过以下命令手动验证:")
        print(f"  curl -H 'X-User-Id: 2' '{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2'")
        print(f"  curl -H 'X-User-Id: 2' '{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2'")
        sys.exit(0)


if __name__ == "__main__":
    main()
