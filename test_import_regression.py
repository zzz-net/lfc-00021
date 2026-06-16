"""
清单导入回归测试套件
覆盖：1.直接导入报错 2.token过期 3.重复内容复用旧版本 4.未解决驳回冲突提示 5.按文档走完整流程成功
"""
import requests
import json
import uuid
import sys
import os
import sqlite3
from datetime import datetime, timedelta

API = "http://127.0.0.1:8000"
H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER = {"X-User-Id": "3"}
H_SUBMITTER = {"X-User-Id": "5"}
H_OTHER_SUBMITTER = {"X-User-Id": "6"}

passed = 0
failed = 0
results = []

V1 = "samples/manifest_sample_good.csv"
V2 = "samples/manifest_sample_repaired_v2.csv"
BAD = "samples/manifest_sample_with_errors.csv"


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
    return r.json()["id"], code


def precheck(bid, filename, filepath, user=H_SUBMITTER):
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/precheck",
            headers=user,
            files={"file": (filename, f, "text/csv")},
        )
    return r


def do_import(bid, filename, filepath, token, user=H_SUBMITTER):
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=user,
            files={"file": (filename, f, "text/csv")},
            data={"precheck_token": token},
        )
    return r


def expire_token(token):
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delivery_acceptance.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    past_time = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE import_prechecks SET expires_at = ? WHERE precheck_token = ?",
        (past_time, token),
    )
    conn.commit()
    conn.close()


# ========================================================================
# 回归 1：直接导入不提供 precheck_token → 400
# ========================================================================
def reg1_direct_import_error():
    print("\n" + "=" * 70)
    print("回归 1：直接导入不提供 precheck_token → 400")
    print("=" * 70)
    bid, code = make_batch("R1")
    print(f"  批次 id={bid}, code={code}")

    with open(V1, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("good.csv", f, "text/csv")},
        )
    case("无 token 导入 → 400", r.status_code == 400, f"actual={r.status_code}")
    err_msg = r.json().get("error", {}).get("message", "")
    case("错误提示含'缺少 precheck_token'", "缺少 precheck_token" in err_msg,
         f"msg={err_msg[:80]}")

    r = precheck(bid, "good.csv", V1)
    tok = r.json()["precheck_token"]
    with open(V2, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("other.csv", f, "text/csv")},
            data={"precheck_token": tok},
        )
    case("预检查内容与导入内容不一致 → 400", r.status_code == 400,
         f"actual={r.status_code}")
    err_msg = r.json().get("error", {}).get("message", "")
    case("错误提示含'哈希校验失败'", "哈希" in err_msg, f"msg={err_msg[:80]}")


# ========================================================================
# 回归 2：token 过期 → 400
# ========================================================================
def reg2_token_expiry():
    print("\n" + "=" * 70)
    print("回归 2：precheck_token 过期 → 400")
    print("=" * 70)
    bid, code = make_batch("R2")
    print(f"  批次 id={bid}, code={code}")

    r = precheck(bid, "v1.csv", V1)
    case("预检查 status=200", r.status_code == 200)
    tok = r.json()["precheck_token"]
    case("预检查 can_import=True", r.json()["can_import"] is True)

    expire_token(tok)
    case("已将 token expires_at 设为过去", True)

    r = do_import(bid, "v1.csv", V1, tok)
    case("过期 token 导入 → 400", r.status_code == 400, f"actual={r.status_code}")
    err_msg = r.json().get("error", {}).get("message", "")
    case("错误提示含'已过期'", "过期" in err_msg, f"msg={err_msg[:80]}")

    r = precheck(bid, "v1_new.csv", V1)
    fresh_tok = r.json()["precheck_token"]
    r = do_import(bid, "v1_new.csv", V1, fresh_tok)
    case("新 token 导入 → 200", r.status_code == 200)
    case("新 token 导入 success=True", r.json()["success"] is True)


# ========================================================================
# 回归 3：重复内容复用旧版本
# ========================================================================
def reg3_duplicate_reuse():
    print("\n" + "=" * 70)
    print("回归 3：重复内容复用旧版本 → REUSE_VERSION + 无新版本号")
    print("=" * 70)
    bid, code = make_batch("R3")
    print(f"  批次 id={bid}, code={code}")

    r = precheck(bid, "v1.csv", V1)
    tok1 = r.json()["precheck_token"]
    case("首次预检查 action_type=NEW_VERSION",
         r.json()["action_type"] == "NEW_VERSION")
    case("首次预检查 reasons 含'创建新版本'",
         any("创建新版本" in x for x in r.json()["reasons"]))

    r = do_import(bid, "v1.csv", V1, tok1)
    case("首次导入 success=True", r.json()["success"] is True)
    v1_id = r.json()["manifest_version_id"]

    r = precheck(bid, "dup.csv", V1)
    case("重复预检查 action_type=REUSE_VERSION",
         r.json()["action_type"] == "REUSE_VERSION",
         f"actual={r.json()['action_type']}")
    case("重复预检查 reused_version_number=1",
         r.json()["reused_version_number"] == 1)
    case("重复预检查 reasons 含'复用'",
         any("复用" in x for x in r.json()["reasons"]))

    tok_dup = r.json()["precheck_token"]
    r = do_import(bid, "dup.csv", V1, tok_dup)
    case("重复导入 success=True", r.json()["success"] is True)
    case("重复导入 version=1（无新版本）",
         r.json()["version_number"] == 1,
         f"actual={r.json()['version_number']}")
    case("重复导入 manifest_version_id == v1_id",
         r.json()["manifest_version_id"] == v1_id)
    case("重复导入 message 含'复用'",
         "复用" in r.json()["message"])

    r = requests.get(f"{API}/api/batches/{bid}/manifests", headers=H_ADMIN)
    case("版本历史只有 1 条",
         len(r.json()) == 1, f"actual={len(r.json())}")


# ========================================================================
# 回归 4：未解决驳回冲突提示
# ========================================================================
def reg4_unresolved_rejection_conflict():
    print("\n" + "=" * 70)
    print("回归 4：未解决驳回 → WARNING 冲突提示 + 仍可导入")
    print("=" * 70)
    bid, code = make_batch("R4")
    print(f"  批次 id={bid}, code={code}")

    r = precheck(bid, "v1.csv", V1)
    tok = r.json()["precheck_token"]
    do_import(bid, "v1.csv", V1, tok)

    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
                  json={"target_status": "pending_review"})
    requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
        "comment": "问题",
        "rejections": [
            {"item_key": "ITEM-001", "rejection_reason": "原因A"},
            {"item_key": "ITEM-002", "rejection_reason": "原因B"},
        ]
    })
    requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER)

    r = precheck(bid, "v2.csv", V2)
    case("预检查 status=200", r.status_code == 200)
    res = r.json()
    case("action_type=NEW_VERSION", res["action_type"] == "NEW_VERSION")
    case("has_conflict=True（有 warning）", res["has_conflict"] is True)
    case("can_import=True（warning 不阻塞）", res["can_import"] is True)

    rej_conflicts = [c for c in res["conflicts"]
                     if c["conflict_type"] == "UNRESOLVED_REJECTIONS"]
    case("conflict_type 包含 UNRESOLVED_REJECTIONS",
         len(rej_conflicts) >= 1)
    if rej_conflicts:
        rc = rej_conflicts[0]
        case("冲突 severity=warning", rc["severity"] == "warning")
        case("冲突 title 含条数",
             "2 条" in rc["title"] or "2" in rc["title"],
             f"title={rc['title']}")
        case("冲突 suggestion 包含查看驳回的提示",
             rc["suggestion"] is not None and "rejections" in (rc["suggestion"] or ""))

    case("reasons 提示有驳回提醒",
         any("驳回" in x for x in res["reasons"]))
    case("message 包含提醒说明",
         "注意" in res["message"] or "提醒" in res["message"],
         f"message={res['message']}")

    tok_v2 = res["precheck_token"]
    r = do_import(bid, "v2.csv", V2, tok_v2)
    case("导入 v2 success=True", r.json()["success"] is True)
    case("导入 v2 version=2", r.json()["version_number"] == 2)

    r = requests.get(f"{API}/api/batches/{bid}/rejections?only_unresolved=true",
                     headers=H_ADMIN)
    case("导入后未解决驳回=0", len(r.json()) == 0)

    r = requests.get(f"{API}/api/batches/{bid}/manifests/prechecks/latest",
                     headers=H_ADMIN)
    case("最近预检查记录可查", r.status_code == 200)
    case("最近预检查 consumed=True", r.json()["consumed"] is True)

    r = requests.get(f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
    pre_logs = [l for l in r.json() if l["action"] == "PRECHECK_IMPORT"]
    case("审批日志含 PRECHECK_IMPORT", len(pre_logs) >= 2,
         f"actual={len(pre_logs)}")


# ========================================================================
# 回归 5：按文档走完整流程 → 成功
# ========================================================================
def reg5_complete_doc_flow():
    print("\n" + "=" * 70)
    print("回归 5：按文档完整流程 → 预检查→导入→校验→驳回→返修→再预检查→再导入→通过→归档")
    print("=" * 70)
    bid, code = make_batch("R5")
    print(f"  批次 id={bid}, code={code}")

    # 5.1 预检查 v1
    r = precheck(bid, "v1.csv", V1)
    case("步骤1: 预检查 v1 → 200", r.status_code == 200)
    d = r.json()
    case("步骤1: action_type=NEW_VERSION", d["action_type"] == "NEW_VERSION")
    case("步骤1: can_import=True", d["can_import"] is True)
    case("步骤1: planned_version_number=1", d["planned_version_number"] == 1)
    tok1 = d["precheck_token"]

    # 5.2 正式导入 v1
    r = do_import(bid, "v1.csv", V1, tok1)
    case("步骤2: 导入 v1 → success", r.json()["success"] is True)
    case("步骤2: version=1", r.json()["version_number"] == 1)

    # 5.3 校验 + 提交 + 驳回 + 返修
    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
                  json={"target_status": "pending_review", "comment": "请验收"})
    requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
        "comment": "2项问题",
        "rejections": [
            {"item_key": "ITEM-001", "rejection_reason": "BIOS报告缺失"},
            {"item_key": "ITEM-002", "rejection_reason": "ECC标注缺失"},
        ]
    })
    r = requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER)
    case("步骤3: 进入返修状态", r.json().get("batch_status") == "repairing")

    # 5.4 预检查 v2（含未解决驳回 WARNING）
    r = precheck(bid, "v2.csv", V2)
    d = r.json()
    case("步骤4: v2 预检查 action_type=NEW_VERSION", d["action_type"] == "NEW_VERSION")
    case("步骤4: v2 预检查 has_conflict=True", d["has_conflict"] is True)
    case("步骤4: v2 预检查 can_import=True", d["can_import"] is True)
    rej_c = [c for c in d["conflicts"] if c["conflict_type"] == "UNRESOLVED_REJECTIONS"]
    case("步骤4: 冲突含 UNRESOLVED_REJECTIONS", len(rej_c) >= 1)
    tok2 = d["precheck_token"]

    # 5.5 正式导入 v2
    r = do_import(bid, "v2.csv", V2, tok2)
    case("步骤5: 导入 v2 → success", r.json()["success"] is True)
    case("步骤5: version=2", r.json()["version_number"] == 2)

    # 5.6 验证驳回已解决
    r = requests.get(f"{API}/api/batches/{bid}/rejections?only_unresolved=true",
                     headers=H_ADMIN)
    case("步骤6: 未解决驳回=0", len(r.json()) == 0)

    # 5.7 查询最近预检查记录（可追溯性）
    r = requests.get(f"{API}/api/batches/{bid}/manifests/prechecks/latest",
                     headers=H_ADMIN)
    case("步骤7: 最近预检查可查", r.status_code == 200)
    case("步骤7: 最近预检查 consumed=True", r.json()["consumed"] is True)

    # 5.8 审批日志含 PRECHECK_IMPORT
    r = requests.get(f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
    pre_logs = [l for l in r.json() if l["action"] == "PRECHECK_IMPORT"]
    imp_logs = [l for l in r.json() if l["action"] == "IMPORT_MANIFEST"]
    case("步骤8: PRECHECK_IMPORT 日志数=2", len(pre_logs) == 2,
         f"actual={len(pre_logs)}")
    case("步骤8: IMPORT_MANIFEST 日志数=2", len(imp_logs) == 2,
         f"actual={len(imp_logs)}")

    # 5.9 重复预检查同一文件 → REUSE_VERSION
    r = precheck(bid, "dup.csv", V2)
    case("步骤9: 重复预检查 REUSE_VERSION",
         r.json()["action_type"] == "REUSE_VERSION",
         f"actual={r.json()['action_type']}")
    tok_dup = r.json()["precheck_token"]
    r = do_import(bid, "dup.csv", V2, tok_dup)
    case("步骤9: 重复导入 version=2（无新版本）",
         r.json()["version_number"] == 2)

    # 5.10 完成归档
    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
                  json={"target_status": "pending_review", "comment": "v2已修"})
    requests.post(f"{API}/api/batches/{bid}/approve", headers=H_LEAD,
                  data={"comment": "通过"})
    requests.post(f"{API}/api/batches/{bid}/archive", headers=H_LEAD,
                  data={"comment": "归档"})

    r = requests.get(f"{API}/api/batches/{bid}", headers=H_ADMIN)
    case("步骤10: 批次状态=archived", r.json()["status"] == "archived")

    r = requests.get(f"{API}/api/batches/{bid}/acceptance-report", headers=H_ADMIN)
    report = r.json()
    case("步骤10: 报告 total_versions=2", report["total_versions"] == 2)
    case("步骤10: 报告 total_rejections=2, resolved=2",
         report["total_rejections"] == 2 and report["resolved_rejections"] == 2)

    # 5.11 归档后预检查 → CONFLICT
    r = precheck(bid, "x.csv", V1)
    case("步骤11: 归档后预检查 CONFLICT",
         r.json()["action_type"] == "CONFLICT")
    case("步骤11: 归档后 can_import=False",
         r.json()["can_import"] is False)


def run_all():
    print("=" * 70)
    print("清单导入回归测试套件（5 项核心场景）")
    print("=" * 70)
    try:
        r = requests.get(f"{API}/health", timeout=3)
        r.raise_for_status()
    except Exception as e:
        print(f"[FATAL] 无法连接 {API}: {e}")
        print("        请先启动服务: python -m uvicorn main:app --port 8000")
        sys.exit(2)

    reg1_direct_import_error()
    reg2_token_expiry()
    reg3_duplicate_reuse()
    reg4_unresolved_rejection_conflict()
    reg5_complete_doc_flow()

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
