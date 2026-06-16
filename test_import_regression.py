"""
清单导入回归测试 - 聚焦文档与接口对齐
覆盖：1.无 token 导入返回 400 + 错误结构无 KeyError
     2.README Python 验收脚本完整链路跑通
     3.响应结构防御性校验（成功/失败响应键互不混用）
"""
import requests
import json
import uuid
import sys

API = "http://127.0.0.1:8000"
H_SUBMITTER = {"X-User-Id": "5"}
H_ADMIN = {"X-User-Id": "1"}
H_REVIEWER = {"X-User-Id": "3"}
H_LEAD = {"X-User-Id": "2"}

passed = 0
failed = 0
results = []

V1 = "samples/manifest_sample_good.csv"
V2 = "samples/manifest_sample_repaired_v2.csv"


def case(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        s = "PASS"
    else:
        failed += 1
        s = "FAIL"
    results.append((name, s, detail))
    print(f"  [{s}] {name}")
    if not condition and detail:
        print(f"         {detail}")


def make_batch(prefix="REG"):
    code = f"{prefix}-{uuid.uuid4().hex[:6].upper()}"
    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": code, "name": f"回归测试-{prefix}", "submitter_id": 5
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def precheck(bid, filename, filepath, user=H_SUBMITTER):
    with open(filepath, "rb") as f:
        return requests.post(
            f"{API}/api/batches/{bid}/manifests/precheck",
            headers=user,
            files={"file": (filename, f, "text/csv")},
        )


def do_import(bid, filename, filepath, token, user=H_SUBMITTER):
    with open(filepath, "rb") as f:
        return requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=user,
            files={"file": (filename, f, "text/csv")},
            data={"precheck_token": token},
        )


IMPORT_SUCCESS_KEYS = {"success", "manifest_version_id", "version_number",
                       "item_count", "errors", "message"}
IMPORT_ERROR_KEYS = {"success", "error"}
PRECHECK_KEYS = {"success", "precheck_token", "batch_id", "action_type",
                 "has_conflict", "import_format", "item_count", "content_hash",
                 "planned_version_number", "reused_version_id",
                 "reused_version_number", "conflicts", "batch_status",
                 "can_import", "expires_at", "reasons", "message", "parse_errors"}


def reg1_no_token_400():
    print("\n" + "=" * 70)
    print("回归 1：无 token 导入 → 400 + 错误结构不引发 KeyError")
    print("=" * 70)
    bid = make_batch("R1")

    with open(V1, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("good.csv", f, "text/csv")},
        )
    case("无 token → status=400", r.status_code == 400, f"actual={r.status_code}")

    d = r.json()
    case("错误响应有 success=False", d.get("success") is False)
    case("错误响应有 error 对象", isinstance(d.get("error"), dict))
    case("error.message 含'缺少 precheck_token'",
         "缺少 precheck_token" in d.get("error", {}).get("message", ""))

    missing_from_error = IMPORT_SUCCESS_KEYS - set(d.keys())
    case("错误响应不含成功专用键 (version_number 等)",
         "version_number" not in d and "item_count" not in d,
         f"错误响应中出现了不应有的键: {missing_from_error & set(d.keys())}")

    try:
        _ = d["version_number"]
        case("直接访问 d['version_number'] → KeyError", False,
             "KeyError 未触发，说明错误响应中不应有此键")
    except KeyError:
        case("直接访问 d['version_number'] → KeyError（预期行为）", True)

    try:
        vn = d.get("version_number")
        case("防御性访问 d.get('version_number') → None", vn is None,
             f"actual={vn}")
    except Exception as e:
        case("防御性访问 d.get('version_number') 不抛异常", False, str(e))


def reg2_readme_walkthrough():
    print("\n" + "=" * 70)
    print("回归 2：README 验收脚本完整链路跑通（防御性 .get() 无 KeyError）")
    print("=" * 70)
    bid = make_batch("R2")

    r = precheck(bid, "v1.csv", V1)
    d = r.json()
    case("预检查 v1 status=200", r.status_code == 200)
    case("预检查 action_type=NEW_VERSION", d.get("action_type") == "NEW_VERSION")
    case("预检查 can_import=True", d.get("can_import") is True)
    case("预检查有 precheck_token", d.get("precheck_token") is not None)
    case("预检查响应键集完整",
         PRECHECK_KEYS.issubset(set(d.keys())),
         f"missing={PRECHECK_KEYS - set(d.keys())}")
    token = d.get("precheck_token")

    r = do_import(bid, "v1.csv", V1, token)
    d = r.json()
    case("导入 v1 status=200", r.status_code == 200)
    case("导入 v1 success=True", d.get("success") is True)
    case("导入 v1 version_number=1", d.get("version_number") == 1)
    case("导入 v1 item_count=5", d.get("item_count") == 5)
    case("导入成功响应键集完整",
         IMPORT_SUCCESS_KEYS.issubset(set(d.keys())),
         f"missing={IMPORT_SUCCESS_KEYS - set(d.keys())}")

    r = precheck(bid, "dup.csv", V1)
    d = r.json()
    case("重复预检查 REUSE_VERSION", d.get("action_type") == "REUSE_VERSION")
    case("重复预检查 reused_version_number=1", d.get("reused_version_number") == 1)
    token_dup = d.get("precheck_token")

    r = do_import(bid, "dup.csv", V1, token_dup)
    d = r.json()
    case("重复导入 success=True", d.get("success") is True)
    case("重复导入 version_number=1（无新版本）", d.get("version_number") == 1)
    case("重复导入 message 含'复用'", "复用" in d.get("message", ""))

    with open(V1, "rb") as f:
        r = requests.post(f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER, files={"file": ("a.csv", f, "text/csv")})
    case("无 token 导入 → 400", r.status_code == 400)
    err_msg = r.json().get("error", {}).get("message", "")
    case("错误提示含'缺少 precheck_token'", "缺少 precheck_token" in err_msg)

    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
        json={"target_status": "pending_review", "comment": "请验收"})
    requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
        "comment": "问题", "rejections": [
            {"item_key": "ITEM-001", "rejection_reason": "BIOS报告缺失"},
            {"item_key": "ITEM-002", "rejection_reason": "ECC标注缺失"},
        ]
    })
    r = requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER)
    case("进入返修状态", r.json().get("batch_status") == "repairing")

    r = precheck(bid, "v2.csv", V2)
    d = r.json()
    case("v2 预检查 NEW_VERSION", d.get("action_type") == "NEW_VERSION")
    case("v2 预检查 has_conflict=True", d.get("has_conflict") is True)
    case("v2 预检查 can_import=True", d.get("can_import") is True)
    rej_c = [c for c in d.get("conflicts", [])
             if c.get("conflict_type") == "UNRESOLVED_REJECTIONS"]
    case("v2 预检查含 UNRESOLVED_REJECTIONS warning", len(rej_c) >= 1)
    token_v2 = d.get("precheck_token")

    r = do_import(bid, "v2.csv", V2, token_v2)
    d = r.json()
    case("导入 v2 success=True", d.get("success") is True)
    case("导入 v2 version_number=2", d.get("version_number") == 2)

    r = requests.get(f"{API}/api/batches/{bid}/manifests/prechecks/latest",
                     headers=H_ADMIN)
    case("最近预检查可查", r.status_code == 200)
    case("最近预检查 consumed=True", r.json().get("consumed") is True)

    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
        json={"target_status": "pending_review", "comment": "v2已修"})
    r = requests.post(f"{API}/api/batches/{bid}/approve", headers=H_LEAD,
        data={"comment": "通过"})
    d = r.json()
    case("approve 成功", d.get("success") is True)
    case("approve 有 batch_status=approved", d.get("batch_status") == "approved")
    case("approve 有 approved_at", "approved_at" in d)

    r = requests.post(f"{API}/api/batches/{bid}/archive", headers=H_LEAD,
        data={"comment": "归档"})
    d = r.json()
    case("archive 成功", d.get("success") is True)
    case("archive 有 batch_status=archived", d.get("batch_status") == "archived")

    print("\n  [链路审计] 完整步骤: 创建→预检查v1→导入v1→重复→无token报错→校验→驳回→返修→预检查v2→导入v2→通过→归档")


def reg3_response_structure_isolation():
    print("\n" + "=" * 70)
    print("回归 3：成功/失败响应键互不混用，防御性读取永不 KeyError")
    print("=" * 70)
    bid = make_batch("R3")

    r = precheck(bid, "v1.csv", V1)
    d_pre = r.json()
    case("预检查响应是 dict", isinstance(d_pre, dict))
    case("预检查有 action_type", "action_type" in d_pre)
    case("预检查无 version_number（那是导入响应的键）",
         "version_number" not in d_pre)
    case("预检查无 manifest_version_id（那是导入响应的键）",
         "manifest_version_id" not in d_pre)
    token = d_pre.get("precheck_token")

    r = do_import(bid, "v1.csv", V1, token)
    d_imp = r.json()
    case("导入成功响应是 dict", isinstance(d_imp, dict))
    case("导入成功有 version_number", "version_number" in d_imp)
    case("导入成功无 action_type（那是预检查响应的键）",
         "action_type" not in d_imp)
    case("导入成功无 can_import（那是预检查响应的键）",
         "can_import" not in d_imp)

    with open(V1, "rb") as f:
        r = requests.post(f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER, files={"file": ("a.csv", f, "text/csv")})
    d_err = r.json()
    case("导入错误响应是 dict", isinstance(d_err, dict))
    case("导入错误有 success=False", d_err.get("success") is False)
    case("导入错误有 error 对象", isinstance(d_err.get("error"), dict))
    case("导入错误无 version_number",
         "version_number" not in d_err)
    case("导入错误无 action_type",
         "action_type" not in d_err)
    case("导入错误无 can_import",
         "can_import" not in d_err)

    safe_vn = d_err.get("version_number")
    case("错误响应 .get('version_number') → None 不报错",
         safe_vn is None, f"actual={safe_vn}")

    safe_ai = d_err.get("action_type")
    case("错误响应 .get('action_type') → None 不报错",
         safe_ai is None, f"actual={safe_ai}")

    print("\n  [结论] 三种响应（预检查/导入成功/导入错误）键互不混用，.get() 永不 KeyError")


def run_all():
    print("=" * 70)
    print("清单导入回归测试 - 文档与接口对齐验证")
    print("=" * 70)
    try:
        r = requests.get(f"{API}/health", timeout=3)
        r.raise_for_status()
    except Exception as e:
        print(f"[FATAL] 无法连接 {API}: {e}")
        print("        请先启动服务: python -m uvicorn main:app --port 8000")
        sys.exit(2)

    reg1_no_token_400()
    reg2_readme_walkthrough()
    reg3_response_structure_isolation()

    print("\n" + "=" * 70)
    print("回归测试结果汇总")
    print("=" * 70)
    for name, s, detail in results:
        mark = "[PASS]" if s == "PASS" else "[FAIL]"
        print(f"  {mark} {name}")
    print(f"\n  总计: PASS={passed}, FAIL={failed}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all())
