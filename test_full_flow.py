import requests
import json
import sys
import random
import string

API = "http://127.0.0.1:8000"
H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER = {"X-User-Id": "3"}
H_SUBMITTER = {"X-User-Id": "5"}

passed = 0
failed = 0

SUFFIX = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
BATCH_CODE = f"BATCH-2026-Q2-{SUFFIX}"


def _safe_get(data, key, default=None):
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def precheck_and_import(bid, filename, filepath, user=H_SUBMITTER):
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/precheck",
            headers=user,
            files={"file": (filename, f, "text/csv")},
            data={"import_format": "auto"},
        )
    if r.status_code != 200:
        return r
    body = r.json()
    if not body.get("can_import"):
        return r
    token = body.get("precheck_token")
    if not token:
        return r
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=user,
            files={"file": (filename, f, "text/csv")},
            data={"import_format": "auto", "precheck_token": token},
        )
    return r


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
            print(f"         response: {json.dumps(data, ensure_ascii=False)[:500]}")
    return data


print("=" * 70)
print("STEP 1: 查看预置用户")
r = requests.get(f"{API}/api/users/", headers=H_ADMIN)
data = test("获取用户列表", r, 200, check_fn=lambda d: isinstance(d, list) and len(d) >= 6)

print("\n" + "=" * 70)
print(f"STEP 2: 创建交付批次 (submitter_chen, ID=5), code={BATCH_CODE}")
r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
    "batch_code": BATCH_CODE,
    "name": "2026年Q2服务器配件交付批次",
    "description": "包含主板、内存、硬盘、电源等服务器核心配件",
    "submitter_id": 5
})
data = test("创建批次", r, 201, check_fn=lambda d: d.get("status") == "draft" and d.get("batch_code") == BATCH_CODE)
BATCH_ID = data["id"] if data else None
print(f"  BATCH_ID = {BATCH_ID}")

print("\n" + "=" * 70)
print("STEP 3: 预检查有错误的 CSV (预检查识别 CONFLICT，导入被拒绝)")
with open("samples/manifest_sample_with_errors.csv", "rb") as f:
    r = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/precheck",
        headers=H_SUBMITTER,
        files={"file": ("manifest_sample_with_errors.csv", f, "text/csv")},
        data={"import_format": "auto"},
    )
data = test("预检查错误清单 - CONFLICT", r, 200, True,
    check_fn=lambda d: d.get("action_type") == "CONFLICT" and d.get("can_import") is False and len(d.get("parse_errors", [])) > 0)
if data and data.get("precheck_token"):
    bad_token = data["precheck_token"]
    with open("samples/manifest_sample_with_errors.csv", "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{BATCH_ID}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("manifest_sample_with_errors.csv", f, "text/csv")},
            data={"import_format": "auto", "precheck_token": bad_token},
        )
    test("CONFLICT token 导入被拒绝 - 400", r, 400)

print("\n" + "=" * 70)
print("STEP 4: 导入正确的 v1 清单 CSV (预检查 → 导入)")
r = precheck_and_import(BATCH_ID, "manifest_sample_good.csv", "samples/manifest_sample_good.csv")
data = test("导入正确清单 v1 - success=true", r, 200, True,
    check_fn=lambda d: d.get("version_number") == 1 and d.get("item_count") == 5)
V1_ID = _safe_get(data, "manifest_version_id")

print("\n" + "=" * 70)
print("STEP 5: 查看最新清单")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/manifests/latest", headers=H_ADMIN)
data = test("获取最新清单", r, 200, check_fn=lambda d: d.get("version_number") == 1 and len(d.get("items", [])) == 5)

print("\n" + "=" * 70)
print("STEP 6: 执行规则校验")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/validate", headers=H_SUBMITTER)
data = test("执行校验 - 应全部通过", r, 200, True,
    check_fn=lambda d: d.get("validation_summary", {}).get("validation_passed") and d.get("failed") == 0)

print("\n" + "=" * 70)
print("STEP 7: 查看失败的校验结果（应为空）")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/validation-results?only_failed=true", headers=H_ADMIN)
data = test("失败校验结果为空", r, 200, check_fn=lambda d: isinstance(d, list) and len(d) == 0)

print("\n" + "=" * 70)
print("STEP 8: submitter 提交待验收 (draft -> pending_review)")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/transition", headers=H_SUBMITTER, json={
    "target_status": "pending_review",
    "comment": "清单已完成初检，请评审验收"
})
data = test("状态流转到待验收", r, 200, check_fn=lambda d: d.get("status") == "pending_review")

print("\n" + "=" * 70)
print("STEP 9: reviewer 驳回 2 项 (pending_review -> partially_rejected)")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/reject", headers=H_REVIEWER, json={
    "comment": "发现 2 项问题，请返修后重新提交",
    "rejections": [
        {"item_key": "ITEM-002", "rejection_reason": "内存条描述中未明确标注 ECC 校验支持"},
        {"item_key": "ITEM-001", "rejection_reason": "主板未提供 BIOS 兼容性测试报告"}
    ]
})
data = test("驳回 2 项问题", r, 200, True,
    check_fn=lambda d: d.get("rejection_count") == 2)

print("\n" + "=" * 70)
print("STEP 10: 查看驳回记录")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/rejections", headers=H_ADMIN)
data = test("获取驳回记录 - 2条未解决", r, 200,
    check_fn=lambda d: isinstance(d, list) and len(d) == 2 and all(not rr.get("resolved") for rr in d))

print("\n" + "=" * 70)
print("STEP 11: submitter 开始返修 (partially_rejected -> repairing)")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/start-repair", headers=H_SUBMITTER,
    data={"comment": "收到驳回意见，开始修订"})
data = test("进入返修状态", r, 200, True,
    check_fn=lambda d: d.get("batch_status") == "repairing")

print("\n" + "=" * 70)
print("STEP 12: 导入修订版 v2 清单 (预检查 → 导入，自动标记旧驳回为已解决)")
r = precheck_and_import(BATCH_ID, "manifest_sample_repaired_v2.csv", "samples/manifest_sample_repaired_v2.csv")
data = test("导入 v2 清单", r, 200, True,
    check_fn=lambda d: d.get("version_number") == 2 and d.get("item_count") == 7)

print("\n" + "=" * 70)
print("STEP 13: 验证驳回记录已被自动标记 resolved")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/rejections", headers=H_ADMIN)
data = test("驳回记录均已解决", r, 200,
    check_fn=lambda d: isinstance(d, list) and len(d) == 2 and all(rr.get("resolved") for rr in d))

print("\n" + "=" * 70)
print("STEP 14: 对 v2 重新跑校验")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/validate", headers=H_SUBMITTER)
data = test("v2 校验通过", r, 200, True,
    check_fn=lambda d: d.get("validation_summary", {}).get("validation_passed"))

print("\n" + "=" * 70)
print("STEP 15: submitter 再次提交待验收")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/transition", headers=H_SUBMITTER, json={
    "target_status": "pending_review",
    "comment": "已修复 2 项驳回问题，新增 ITEM-006/ITEM-007，请重新验收"
})
data = test("再次提交待验收", r, 200, check_fn=lambda d: d.get("status") == "pending_review")

print("\n" + "=" * 70)
print("STEP 16: reviewer (ID=3) 尝试通过 - 权限不足 (失败路径)")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/approve", headers=H_REVIEWER,
    data={"comment": "验收通过"})
data = test("reviewer 无权通过 - 403", r, 403)

print("\n" + "=" * 70)
print("STEP 17: lead (ID=2) 通过验收")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/approve", headers=H_LEAD,
    data={"comment": "v2 验收通过，所有规格符合要求，可以交付"})
data = test("lead 批准通过", r, 200, True,
    check_fn=lambda d: d.get("batch_status") == "approved" and "approved_at" in d)

print("\n" + "=" * 70)
print("STEP 18: submitter 尝试归档 - 权限不足 (失败路径)")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/archive", headers=H_SUBMITTER,
    data={"comment": "归档"})
data = test("submitter 无权归档 - 403", r, 403)

print("\n" + "=" * 70)
print("STEP 19: lead 归档批次")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/archive", headers=H_LEAD,
    data={"comment": "批次完成交付，正式归档"})
data = test("lead 归档", r, 200, True,
    check_fn=lambda d: d.get("batch_status") == "archived")

print("\n" + "=" * 70)
print("STEP 20: 非法状态流转 - 已归档改回已通过 (失败路径)")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/transition", headers=H_ADMIN, json={
    "target_status": "approved",
    "comment": "非法回滚"
})
data = test("非法状态流转被拒绝 - 400", r, 400)

print("\n" + "=" * 70)
print("STEP 21: 查询版本历史 (验证持久化 - 重启后仍在)")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/version-history", headers=H_ADMIN)
data = test("版本历史含 v1 和 v2", r, 200,
    check_fn=lambda d: isinstance(d, list) and len(d) == 2 and d[0].get("version_number") == 1 and d[1].get("version_number") == 2)

print("\n" + "=" * 70)
print("STEP 22: 查询审批日志 (完整审计链路)")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H_ADMIN)
data = test("审批日志 >= 9 条", r, 200, check_fn=lambda d: isinstance(d, list) and len(d) >= 9)
if data:
    print(f"  日志条数: {len(data)}")
    for log in data:
        fs = log.get('from_status') or '-'
        ts = log.get('to_status') or '-'
        print(f"    [{str(log['created_at'])[:19]}] {log['action']:18s} {fs:18s} -> {ts:18s}  by user#{log['actor_id']}")

print("\n" + "=" * 70)
print("STEP 23: 导出验收报告")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/acceptance-report", headers=H_ADMIN)
data = test("验收报告完整", r, 200,
    check_fn=lambda d: d.get("total_versions") == 2 and d.get("current_version") == 2
                       and d.get("total_rejections") == 2 and d.get("resolved_rejections") == 2
                       and d.get("validation_passed"))

r = requests.get(f"{API}/api/batches/{BATCH_ID}/export-report?format=json", headers=H_ADMIN)
test("下载 JSON 报告文件", r, 200,
    check_fn=lambda d: isinstance(d, dict) and "batch_summary" in d and "version_history" in d)
with open("test_report_output.json", "w", encoding="utf-8") as f:
    json.dump(r.json(), f, ensure_ascii=False, indent=2)
print("  报告已保存到 test_report_output.json")

print("\n" + "=" * 70)
print(f"测试结果: PASS={passed}  FAIL={failed}")
if failed == 0:
    print("ALL TESTS PASSED!")
else:
    print("SOME TESTS FAILED!")
    sys.exit(1)
