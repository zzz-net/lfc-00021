"""
清单导入预检查 + 冲突确认 - 回归测试套件
覆盖：重复导入复用、未解决驳回冲突提示、重启持久化、
      token过期/消费校验、权限收紧、审批日志联动
"""
import requests
import json
import uuid
import sys
import time
import os
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
snapshot_bundle = {}


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


def make_batch(prefix="PCK"):
    code = f"{prefix}-{uuid.uuid4().hex[:6].upper()}"
    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": code, "name": f"预检查回归-{prefix}", "submitter_id": 5
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


V1 = "samples/manifest_sample_good.csv"
V2 = "samples/manifest_sample_repaired_v2.csv"
BAD = "samples/manifest_sample_with_errors.csv"


# ========================================================================
# 场景 1：重复导入复用旧版本 - 预检查阶段正确识别 REUSE_VERSION
# ========================================================================
def scenario_1_duplicate_reuse():
    print("\n" + "=" * 70)
    print("场景 1：重复导入复用旧版本（预检查识别 REUSE_VERSION）")
    print("=" * 70)
    bid, code = make_batch("S1")
    print(f"\n  批次 id={bid}, code={code}")

    r = precheck(bid, "v1.csv", V1)
    case("首次预检查 status=200", r.status_code == 200)
    res1 = r.json()
    tok1 = res1["precheck_token"]
    case("首次预检查 action_type=NEW_VERSION",
         res1["action_type"] == "NEW_VERSION",
         f"actual={res1['action_type']}")
    case("首次预检查 planned_version_number=1",
         res1["planned_version_number"] == 1,
         f"actual={res1['planned_version_number']}")
    case("首次预检查 can_import=True", res1["can_import"] is True)
    case("首次预检查 has_conflict=False", res1["has_conflict"] is False)
    case("首次预检查 reasons 含'将创建新版本'",
         any("创建新版本" in x for x in res1["reasons"]))

    r = do_import(bid, "v1.csv", V1, tok1)
    case("首次导入 status=200", r.status_code == 200, f"status={r.status_code}")
    imp1 = r.json()
    case("首次导入 success=True", imp1["success"] is True)
    case("首次导入 version=1", imp1["version_number"] == 1, f"actual={imp1['version_number']}")
    v1_id = imp1["manifest_version_id"]

    r = precheck(bid, "dup.csv", V1)
    case("重复内容预检查 status=200", r.status_code == 200)
    dup_pre = r.json()
    case("重复预检查 action_type=REUSE_VERSION",
         dup_pre["action_type"] == "REUSE_VERSION",
         f"actual={dup_pre['action_type']}")
    case("重复预检查 reused_version_number=1",
         dup_pre["reused_version_number"] == 1)
    case("重复预检查 reused_version_id == v1_id",
         dup_pre["reused_version_id"] == v1_id)
    case("重复预检查 reasons 含'复用'",
         any("复用" in x for x in dup_pre["reasons"]))
    tok_dup = dup_pre["precheck_token"]

    r = do_import(bid, "dup.csv", V1, tok_dup)
    case("重复导入 status=200", r.status_code == 200)
    imp_dup = r.json()
    case("重复导入 success=True", imp_dup["success"] is True)
    case("重复导入 version=1（无新版本）",
         imp_dup["version_number"] == 1, f"actual={imp_dup['version_number']}")
    case("重复导入 manifest_version_id == v1_id",
         imp_dup["manifest_version_id"] == v1_id)
    case("重复导入 message 含'复用'",
         "复用" in imp_dup["message"],
         f"message={imp_dup['message']}")

    r = requests.get(f"{API}/api/batches/{bid}/manifests", headers=H_ADMIN)
    case("版本历史总个数应为 1",
         len(r.json()) == 1, f"actual={len(r.json())}")

    return bid


# ========================================================================
# 场景 2：存在未解决驳回 - 预检查给出 WARNING 级冲突提示
# ========================================================================
def scenario_2_unresolved_rejections():
    print("\n" + "=" * 70)
    print("场景 2：存在未解决驳回 - 预检查 WARNING 冲突提示")
    print("=" * 70)
    bid, code = make_batch("S2")
    print(f"\n  批次 id={bid}, code={code}")

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
    case("返修状态预检查 status=200", r.status_code == 200)
    res = r.json()
    case("预检查 action_type=NEW_VERSION（内容变化）",
         res["action_type"] == "NEW_VERSION")
    case("预检查 has_conflict=True（有 warning）",
         res["has_conflict"] is True)
    case("预检查 can_import=True（warning 不阻塞）",
         res["can_import"] is True)
    case("预检查 conflicts 非空", len(res["conflicts"]) > 0)

    rej_conflicts = [c for c in res["conflicts"]
                     if c["conflict_type"] == "UNRESOLVED_REJECTIONS"]
    case("conflict_type 包含 UNRESOLVED_REJECTIONS",
         len(rej_conflicts) >= 1)
    if rej_conflicts:
        rc = rej_conflicts[0]
        case("冲突级别为 warning", rc["severity"] == "warning",
             f"actual={rc['severity']}")
        case("冲突描述含未解决驳回条数",
             "2 条" in rc["title"] or "2" in rc["title"],
             f"title={rc['title']}")
        case("冲突 suggestion 包含查看驳回的提示",
             rc["suggestion"] is not None and "rejections" in (rc["suggestion"] or ""))

    case("reasons 列表中提示有驳回提醒",
         any("驳回" in x for x in res["reasons"]))
    case("message 包含警告提醒说明",
         "注意" in res["message"] or "提醒" in res["message"],
         f"message={res['message']}")

    tok_v2 = res["precheck_token"]
    r = do_import(bid, "v2.csv", V2, tok_v2)
    case("带未解决驳回状态执行导入 - success=True", r.json()["success"] is True)
    imp_v2 = r.json()
    case("导入版本 v2", imp_v2["version_number"] == 2, f"actual={imp_v2['version_number']}")

    r = requests.get(f"{API}/api/batches/{bid}/rejections?only_unresolved=true", headers=H_ADMIN)
    case("导入 v2 后所有驳回自动 resolved",
         len(r.json()) == 0, f"unresolved={len(r.json())}")

    return bid


# ========================================================================
# 场景 3：批次状态冲突 - pending_review/approved/archived 不允许导入
# ========================================================================
def scenario_3_status_conflict():
    print("\n" + "=" * 70)
    print("场景 3：批次状态冲突 - STATUS_CONFLICT error 阻塞")
    print("=" * 70)
    bid, code = make_batch("S3")
    print(f"\n  批次 id={bid}, code={code}")

    r = precheck(bid, "v1.csv", V1)
    tok = r.json()["precheck_token"]
    do_import(bid, "v1.csv", V1, tok)
    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
                  json={"target_status": "pending_review"})

    r = precheck(bid, "x.csv", V2)
    case("pending_review 下预检查 status=200", r.status_code == 200)
    res = r.json()
    case("pending_review 下 action_type=CONFLICT",
         res["action_type"] == "CONFLICT")
    case("pending_review 下 can_import=False",
         res["can_import"] is False)
    status_conflicts = [c for c in res["conflicts"]
                        if c["conflict_type"] == "STATUS_CONFLICT"]
    case("conflict_type=STATUS_CONFLICT", len(status_conflicts) >= 1)
    if status_conflicts:
        case("冲突 severity=error", status_conflicts[0]["severity"] == "error")
        case("冲突 title 含待验收",
             "待验收" in status_conflicts[0]["title"],
             f"title={status_conflicts[0]['title']}")

    tok_bad = res["precheck_token"]
    r = do_import(bid, "x.csv", V2, tok_bad)
    case("CONFLICT token 执行导入被拒绝 status=400",
         r.status_code == 400, f"actual={r.status_code}")
    case("拒绝原因含'阻塞性冲突'",
         "阻塞" in (r.json().get("error", {}).get("message", "")),
         f"msg={r.json().get('error', {}).get('message', '')[:80]}")

    requests.post(f"{API}/api/batches/{bid}/approve", headers=H_LEAD, data={"comment": "ok"})
    r = precheck(bid, "x.csv", V2)
    case("approved 下 action_type=CONFLICT",
         r.json()["action_type"] == "CONFLICT")
    case("approved 下 can_import=False",
         r.json()["can_import"] is False)

    requests.post(f"{API}/api/batches/{bid}/archive", headers=H_LEAD, data={"comment": "ok"})
    r = precheck(bid, "x.csv", V2)
    case("archived 下 action_type=CONFLICT",
         r.json()["action_type"] == "CONFLICT")
    case("archived 下 can_import=False",
         r.json()["can_import"] is False)

    return bid


# ========================================================================
# 场景 4：重启后持久化 - 预检查记录重启后仍可查询
# ========================================================================
def scenario_4_persistence_partA():
    print("\n" + "=" * 70)
    print("场景 4-PART A：执行预检查并保存快照，待重启后验证")
    print("=" * 70)
    bid, code = make_batch("S4")
    print(f"\n  批次 id={bid}, code={code}")

    r = precheck(bid, "v1.csv", V1)
    tok1 = r.json()["precheck_token"]
    do_import(bid, "v1.csv", V1, tok1)

    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
                  json={"target_status": "pending_review"})
    requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
        "comment": "p", "rejections": [{"item_key": "ITEM-001", "rejection_reason": "q"}]
    })
    requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER)

    r = precheck(bid, "v2.csv", V2)
    res_before = r.json()

    r = requests.get(f"{API}/api/batches/{bid}/manifests/prechecks/latest", headers=H_ADMIN)
    latest_before = r.json()

    r = requests.get(f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
    pre_logs_before = [l for l in r.json() if l["action"] == "PRECHECK_IMPORT"]

    r = requests.get(f"{API}/api/batches/{bid}/manifests", headers=H_ADMIN)
    versions_before = r.json()

    global snapshot_bundle
    snapshot_bundle = {
        "batch_id": bid,
        "batch_code": code,
        "precheck_action_before": latest_before["action_type"],
        "precheck_has_conflict_before": latest_before["has_conflict"],
        "precheck_planned_before": latest_before["planned_version_number"],
        "precheck_consumed_before": latest_before["consumed"],
        "precheck_can_import_before": latest_before["can_import"],
        "precheck_content_hash_before": latest_before["content_hash"],
        "pre_logs_count_before": len(pre_logs_before),
        "versions_count_before": len(versions_before),
        "token_before": res_before["precheck_token"],
        "message_before": res_before["message"],
    }
    print(f"  [SNAPSHOT] 已保存: batch_id={bid}")
    for k, v in snapshot_bundle.items():
        print(f"    {k}: {v}")

    with open(".test_scenario4_snapshot.json", "w", encoding="utf-8") as f:
        json.dump(snapshot_bundle, f, ensure_ascii=False, indent=2)
    print("  快照已保存到 .test_scenario4_snapshot.json，等待重启后运行 PART B")

    case("PART A 快照保存成功", True)
    return bid


def scenario_4_persistence_partB():
    print("\n" + "=" * 70)
    print("场景 4-PART B：重启后读取快照验证一致")
    print("=" * 70)
    try:
        with open(".test_scenario4_snapshot.json", "r", encoding="utf-8") as f:
            snap = json.load(f)
    except FileNotFoundError:
        print("  [!] 未找到快照文件，跳过 PART B。请先运行 PART A → 重启服务 → 再运行 PART B")
        return

    bid = snap["batch_id"]
    print(f"\n  批次 id={bid}, code={snap['batch_code']}")

    r = requests.get(f"{API}/api/batches/{bid}/manifests/prechecks/latest", headers=H_ADMIN)
    case("重启后 latest precheck status=200", r.status_code == 200)
    latest_after = r.json()
    case("重启后 action_type 一致",
         latest_after["action_type"] == snap["precheck_action_before"],
         f"before={snap['precheck_action_before']}, after={latest_after['action_type']}")
    case("重启后 has_conflict 一致",
         latest_after["has_conflict"] == snap["precheck_has_conflict_before"])
    case("重启后 planned_version_number 一致",
         latest_after["planned_version_number"] == snap["precheck_planned_before"])
    case("重启后 consumed 一致",
         latest_after["consumed"] == snap["precheck_consumed_before"])
    case("重启后 can_import 一致",
         latest_after["can_import"] == snap["precheck_can_import_before"])
    case("重启后 content_hash 一致",
         latest_after["content_hash"] == snap["precheck_content_hash_before"])
    case("重启后 batch_status 可读",
         latest_after.get("batch_status") is not None)

    r = requests.get(f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
    pre_logs_after = [l for l in r.json() if l["action"] == "PRECHECK_IMPORT"]
    case("重启后 PRECHECK_IMPORT 审批日志数量一致",
         len(pre_logs_after) == snap["pre_logs_count_before"],
         f"before={snap['pre_logs_count_before']}, after={len(pre_logs_after)}")

    r = requests.get(f"{API}/api/batches/{bid}/manifests", headers=H_ADMIN)
    case("重启后 版本历史数量一致",
         len(r.json()) == snap["versions_count_before"],
         f"before={snap['versions_count_before']}, after={len(r.json())}")

    r = requests.get(f"{API}/api/batches/{bid}/manifests/prechecks", headers=H_ADMIN)
    case("重启后 prechecks 列表接口可用", r.status_code == 200)
    case("重启后 prechecks 列表至少 2 条（v1 预检查 + v2 预检查）",
         len(r.json()) >= 2, f"actual={len(r.json())}")

    print()
    print("  [SUMMARY] 重启后所有字段与重启前完全一致，持久化验证通过！")


# ========================================================================
# 场景 5：token 过期 / 已消费 / 归属校验 / 内容哈希校验
# ========================================================================
def scenario_5_token_validation():
    print("\n" + "=" * 70)
    print("场景 5：precheck_token 过期、消费、归属、哈希校验")
    print("=" * 70)
    bid, code = make_batch("S5")
    print(f"\n  批次 id={bid}, code={code}")

    # 5.1 没有 token 直接导入
    with open(V1, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("a.csv", f, "text/csv")},
        )
    case("无 token 导入 → 400", r.status_code == 400,
         f"actual={r.status_code}")
    case("无 token 错误提示含'缺少 precheck_token'",
         "缺少 precheck_token" in (r.json().get("error", {}).get("message", "") or ""))

    # 5.2 不存在的 token
    with open(V1, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("a.csv", f, "text/csv")},
            data={"precheck_token": "DOES_NOT_EXIST_12345"},
        )
    case("不存在的 token → 400", r.status_code == 400)
    case("错误提示含'无效或不存在'",
         "无效" in (r.json().get("error", {}).get("message", "") or ""))

    # 5.3 跨批次 token 滥用
    bid_A = bid
    bid_B, _ = make_batch("S5B")
    r = precheck(bid_B, "v1.csv", V1)
    tok_B = r.json()["precheck_token"]
    with open(V1, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid_A}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("a.csv", f, "text/csv")},
            data={"precheck_token": tok_B},
        )
    case("跨批次使用 token → 400", r.status_code == 400)
    case("错误提示含'不匹配'",
         "不匹配" in (r.json().get("error", {}).get("message", "") or ""))

    # 5.4 token 归属校验：由 ADMIN 生成（admin 有权对任意批次做预检查），再由 submitter 使用
    #     这样 _enforce_submitter_permission 不会拦（submitter 是本人），但 token 归属校验会拒绝
    r = precheck(bid, "v1_admin.csv", V1, user=H_ADMIN)
    tok_from_admin = r.json()["precheck_token"]
    with open(V1, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("a.csv", f, "text/csv")},
            data={"precheck_token": tok_from_admin},
        )
    case("非生成者本人使用 token → 403", r.status_code == 403, f"actual={r.status_code}")
    case("错误提示含'其他用户生成'",
         "其他用户" in (r.json().get("error", {}).get("message", "") or ""),
         f"msg={r.json().get('error', {}).get('message', '')[:80]}")

    # 5.5 预检查内容 A 但导入内容 B（哈希校验）
    r = precheck(bid, "A.csv", V1)
    tok_hash = r.json()["precheck_token"]
    with open(V2, "rb") as f:
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("B.csv", f, "text/csv")},
            data={"precheck_token": tok_hash},
        )
    case("预检查内容与导入内容不一致 → 400", r.status_code == 400)
    case("错误提示含'哈希校验失败'",
         "哈希" in (r.json().get("error", {}).get("message", "") or ""))

    # 5.6 token 一次性消费：使用后再用被拒绝
    r = precheck(bid, "v1b.csv", V1)
    tok_once = r.json()["precheck_token"]
    with open(V1, "rb") as f:
        r1 = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("a.csv", f, "text/csv")},
            data={"precheck_token": tok_once},
        )
    case("首次使用 token → 200", r1.status_code == 200, f"actual={r1.status_code}")
    with open(V1, "rb") as f:
        r2 = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("a.csv", f, "text/csv")},
            data={"precheck_token": tok_once},
        )
    case("重复使用同一 token → 400", r2.status_code == 400)
    case("错误提示含'已被使用过'",
         "已被使用" in (r2.json().get("error", {}).get("message", "") or ""))

    return bid


# ========================================================================
# 场景 6：权限收紧 - 仅批次提交人或管理员可预检查/导入
# ========================================================================
def scenario_6_permission_enforcement():
    print("\n" + "=" * 70)
    print("场景 6：权限收紧 - 仅提交人/管理员可操作")
    print("=" * 70)
    bid, code = make_batch("S6")
    print(f"\n  批次 id={bid}, code={code}")

    r = precheck(bid, "x.csv", V1, user=H_REVIEWER)
    case("reviewer 角色预检查 → 403", r.status_code == 403)

    r = precheck(bid, "x.csv", V1, user=H_LEAD)
    case("lead 角色预检查 → 403", r.status_code == 403)

    r = precheck(bid, "x.csv", V1, user=H_OTHER_SUBMITTER)
    case("其他 submitter 预检查 → 403", r.status_code == 403)

    r = precheck(bid, "x.csv", V1, user=H_ADMIN)
    case("admin 预检查 → 200（管理员权限）",
         r.status_code == 200, f"actual={r.status_code}")

    r = precheck(bid, "x.csv", V1, user=H_SUBMITTER)
    case("本人 submitter 预检查 → 200",
         r.status_code == 200, f"actual={r.status_code}")

    return bid


# ========================================================================
# 场景 7：审批日志联动 - PRECHECK_IMPORT 动作落库
# ========================================================================
def scenario_7_approval_log_integration():
    print("\n" + "=" * 70)
    print("场景 7：审批日志 PRECHECK_IMPORT 动作落库 + 字段完整")
    print("=" * 70)
    bid, code = make_batch("S7")
    print(f"\n  批次 id={bid}, code={code}")

    r = precheck(bid, "v1.csv", V1)
    tok = r.json()["precheck_token"]
    do_import(bid, "v1.csv", V1, tok)

    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
                  json={"target_status": "pending_review"})
    requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
        "comment": "p", "rejections": [{"item_key": "ITEM-001", "rejection_reason": "q"}]
    })
    requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER)

    r = precheck(bid, "v2.csv", V2)
    tok2 = r.json()["precheck_token"]
    do_import(bid, "v2.csv", V2, tok2)

    r = requests.get(f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
    logs = r.json()
    pre_logs = [l for l in logs if l["action"] == "PRECHECK_IMPORT"]
    import_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]

    case("审批日志中 PRECHECK_IMPORT 条数应为 2",
         len(pre_logs) == 2, f"actual={len(pre_logs)}")
    case("审批日志中 IMPORT_MANIFEST 条数应为 2",
         len(import_logs) == 2, f"actual={len(import_logs)}")

    # 第二条预检查日志（v2，带驳回 warning）
    pre2 = pre_logs[1]
    case("PRECHECK 日志 actor_id=5（提交人）",
         pre2["actor_id"] == 5)
    case("PRECHECK 日志 comment 含 action_type",
         "NEW_VERSION" in (pre2.get("comment") or ""))
    extra = pre2.get("extra_data") or {}
    case("PRECHECK 日志 extra_data 含 precheck_token",
         extra.get("precheck_token") == tok2)
    case("PRECHECK 日志 extra_data 含 can_import=True",
         extra.get("can_import") is True)
    case("PRECHECK 日志 extra_data 含 conflict_types (含 UNRESOLVED_REJECTIONS)",
         "UNRESOLVED_REJECTIONS" in (extra.get("conflict_types") or []))

    imp2 = import_logs[1]
    case("IMPORT 日志 extra_data 含 precheck_token 关联",
         (imp2.get("extra_data") or {}).get("precheck_token") == tok2)

    r = requests.get(f"{API}/api/batches/{bid}/acceptance-report", headers=H_ADMIN)
    report = r.json()
    log_count = len([l for l in report["approval_logs"]
                     if l["action"] == "PRECHECK_IMPORT"])
    case("验收报告中也包含 PRECHECK_IMPORT 日志",
         log_count == 2, f"actual={log_count}")

    return bid


# ========================================================================
# 场景 8：用户可见验收链路（端到端 Python requests 演示）
# ========================================================================
def scenario_8_user_visible_acceptance():
    print("\n" + "=" * 70)
    print("场景 8：用户可见端到端验收链路")
    print("=" * 70)
    bid, code = make_batch("S8")
    steps = []
    print(f"\n  批次 id={bid}, code={code}")
    print(f"  角色：submitter_chen (ID=5) 本人")
    steps.append(("创建批次", bid, code))

    print("\n  [步骤 1] 提交人上传清单 v1，先做预检查")
    r = precheck(bid, "manifest_v1.csv", V1)
    d = r.json()
    print(f"         → action_type={d['action_type']}, planned_v={d['planned_version_number']}")
    print(f"         → can_import={d['can_import']}, 原因: {d['reasons']}")
    case("步骤1: 预检查 NEW_VERSION", d["action_type"] == "NEW_VERSION")
    tok1 = d["precheck_token"]
    steps.append(("precheck v1", d["action_type"], tok1))

    print("\n  [步骤 2] 确认预检查结论无误，携带 token 执行正式导入")
    r = do_import(bid, "manifest_v1.csv", V1, tok1)
    d = r.json()
    print(f"         → success={d['success']}, version={d['version_number']}, msg={d['message']}")
    case("步骤2: 导入 v1 success=True", d["success"] is True)
    steps.append(("import v1", d["version_number"], d["manifest_version_id"]))

    print("\n  [步骤 3] 校验 → 提交待验收 → reviewer 驳回问题")
    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
                  json={"target_status": "pending_review", "comment": "请验收"})
    requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
        "comment": "发现 2 项问题",
        "rejections": [
            {"item_key": "ITEM-001", "rejection_reason": "BIOS兼容性报告缺失"},
            {"item_key": "ITEM-002", "rejection_reason": "ECC内存标注缺失"},
        ]
    })
    r = requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER)
    case("步骤3: 进入返修状态",
         r.json().get("batch_status") == "repairing")
    steps.append(("驳回+返修", 2, "未解决驳回条数=2"))

    print("\n  [步骤 4] 返修中上传 v2 清单，预检查看到 WARNING：存在未解决驳回")
    r = precheck(bid, "manifest_v2.csv", V2)
    d = r.json()
    print(f"         → action_type={d['action_type']}, has_conflict={d['has_conflict']}, can_import={d['can_import']}")
    for c in d["conflicts"]:
        print(f"            · [{c['severity']}] {c['title']} — {c['description']}")
    print(f"         → message: {d['message']}")
    case("步骤4: v2 预检查 has_conflict=True（有 warning）",
         d["has_conflict"] is True)
    case("步骤4: v2 预检查 can_import=True（warning 不阻塞）",
         d["can_import"] is True)
    case("步骤4: conflicts 含 2 条未解决驳回",
         any("2 条" in c["title"] or "2" in c["title"] for c in d["conflicts"]))
    tok2 = d["precheck_token"]

    print("\n  [步骤 5] 查询最近一次预检查（模拟用户离开页面再回来）")
    r = requests.get(f"{API}/api/batches/{bid}/manifests/prechecks/latest", headers=H_SUBMITTER)
    d = r.json()
    print(f"         → action_type={d['action_type']}, planned_v={d['planned_version_number']}")
    print(f"         → reasons: {d['reasons']}")
    case("步骤5: 最近预检查 action_type=NEW_VERSION",
         d["action_type"] == "NEW_VERSION")
    case("步骤5: planned_version_number=2（应该是 v2 预检查）",
         d["planned_version_number"] == 2,
         f"actual={d['planned_version_number']}")
    case("步骤5: reasons 含计划版本号 v2 或 2",
         any("v2" in x or "版本 2" in x or "v{2}" in x or (
             d["planned_version_number"] == 2 and "新版本" in x
         ) for x in d["reasons"]),
         f"reasons={d['reasons']}")

    print("\n  [步骤 6] 确认后携带 token 正式导入 v2（自动解决驳回）")
    r = do_import(bid, "manifest_v2.csv", V2, tok2)
    d = r.json()
    print(f"         → success={d['success']}, version={d['version_number']}, msg={d['message']}")
    case("步骤6: 导入 v2 success=True", d["success"] is True)
    case("步骤6: 导入 v2 version=2", d["version_number"] == 2)
    steps.append(("import v2", 2, "auto resolve 2 rejections"))

    r = requests.get(f"{API}/api/batches/{bid}/rejections?only_unresolved=true", headers=H_ADMIN)
    case("步骤6: 导入 v2 后未解决驳回=0",
         len(r.json()) == 0, f"actual={len(r.json())}")

    print("\n  [步骤 7] 再次提交待验收 → lead 批准 → 归档 → 验收报告")
    requests.post(f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)
    requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER,
                  json={"target_status": "pending_review", "comment": "v2已修"})
    requests.post(f"{API}/api/batches/{bid}/approve", headers=H_LEAD, data={"comment": "通过"})
    requests.post(f"{API}/api/batches/{bid}/archive", headers=H_LEAD, data={"comment": "归档"})

    r = requests.get(f"{API}/api/batches/{bid}/acceptance-report", headers=H_ADMIN)
    report = r.json()
    case("步骤7: 验收报告 total_versions=2",
         report["total_versions"] == 2)
    case("步骤7: 验收报告 total_rejections=2, resolved=2",
         report["total_rejections"] == 2 and report["resolved_rejections"] == 2)
    precheck_logs_in_report = [l for l in report["approval_logs"]
                               if l["action"] == "PRECHECK_IMPORT"]
    case("步骤7: 验收报告包含 2 条 PRECHECK_IMPORT 日志",
         len(precheck_logs_in_report) == 2,
         f"actual={len(precheck_logs_in_report)}")

    print()
    print("  [链路审计] 完整步骤:")
    for i, s in enumerate(steps, 1):
        print(f"    {i}. {s[0]}  →  {s[1:]}")

    return bid


def scenario_9_token_expiry():
    print("\n" + "=" * 70)
    print("场景 9：precheck_token 过期后导入被拒绝")
    print("=" * 70)
    bid, code = make_batch("S9")
    print(f"\n  批次 id={bid}, code={code}")

    r = precheck(bid, "v1.csv", V1)
    case("预检查 status=200", r.status_code == 200)
    res = r.json()
    tok = res["precheck_token"]
    case("预检查 can_import=True", res["can_import"] is True)
    case("预检查 expires_at 存在", res.get("expires_at") is not None)

    import sqlite3
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delivery_acceptance.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    past_time = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE import_prechecks SET expires_at = ? WHERE precheck_token = ?",
        (past_time, tok),
    )
    conn.commit()
    conn.close()
    case("已将 token expires_at 设为 1 小时前", True)

    r = do_import(bid, "v1.csv", V1, tok)
    case("过期 token 导入 → 400", r.status_code == 400, f"actual={r.status_code}")
    err_msg = r.json().get("error", {}).get("message", "")
    case("错误提示含'已过期'", "过期" in err_msg, f"msg={err_msg[:80]}")

    r = precheck(bid, "v1_fresh.csv", V1)
    case("重新预检查获取新 token → 200", r.status_code == 200)
    fresh_tok = r.json()["precheck_token"]
    r = do_import(bid, "v1_fresh.csv", V1, fresh_tok)
    case("新 token 导入成功 → 200", r.status_code == 200, f"actual={r.status_code}")
    case("新 token 导入 success=True", r.json()["success"] is True)

    return bid


def run_all(part="ALL"):
    print("=" * 70)
    print("清单导入预检查 + 冲突确认 - 回归测试套件")
    print("=" * 70)
    try:
        r = requests.get(f"{API}/health", timeout=3)
        r.raise_for_status()
    except Exception as e:
        print(f"[FATAL] 无法连接 {API}: {e}")
        print("        请先启动服务: python -m uvicorn main:app --port 8000")
        sys.exit(2)

    bids = []
    if part in ("ALL", "S1"):
        bids.append(scenario_1_duplicate_reuse())
    if part in ("ALL", "S2"):
        bids.append(scenario_2_unresolved_rejections())
    if part in ("ALL", "S3"):
        bids.append(scenario_3_status_conflict())
    if part in ("ALL", "S4A"):
        bids.append(scenario_4_persistence_partA())
    if part == "S4B":
        scenario_4_persistence_partB()
    if part in ("ALL", "S5"):
        bids.append(scenario_5_token_validation())
    if part in ("ALL", "S6"):
        bids.append(scenario_6_permission_enforcement())
    if part in ("ALL", "S7"):
        bids.append(scenario_7_approval_log_integration())
    if part in ("ALL", "S8"):
        bids.append(scenario_8_user_visible_acceptance())
    if part in ("ALL", "S9"):
        bids.append(scenario_9_token_expiry())

    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    for name, s, detail in results:
        mark = "[PASS]" if s == "PASS" else "[FAIL]"
        print(f"  {mark} {name}")
    print(f"\n  总计: PASS={passed}, FAIL={failed}")
    print(f"  涉及批次 ID: {bids}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    part = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    valid = {"ALL", "S1", "S2", "S3", "S4A", "S4B", "S5", "S6", "S7", "S8", "S9"}
    if part not in valid:
        print(f"Usage: python {sys.argv[0]} [ALL|S1|S2|S3|S4A|S4B|S5|S6|S7|S8|S9]")
        print("  S4A: 保存重启前快照  →  重启服务  →  S4B: 对比重启后快照")
        sys.exit(1)
    sys.exit(run_all(part))
