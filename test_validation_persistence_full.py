#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
校验结果持久化 + 多入口一致性 完整回归测试
================================================

贴近真实业务流程：
  Step 1 : 创建批次
  Step 2 : 导入 v1（有 error + 有 warning）→ 执行校验
  Step 3 : 提交审核 → 评审人员 item-level 驳回
  Step 4 : 开始返修 → 导入 v2（修复1个error，遗留1个warning，新增1个item带新error，删除1个item）
  Step 5 : 执行 v2 校验（应自动触发快照刷新）
  Step 6 : 5个入口一致性断言：
             /snapshots/latest
             /snapshots/by-versions?old=1&new=2
             /snapshots/{id}
             /version-diff?old=1&new=2
             /version-diff/export?format=json
             /version-diff/export?format=csv
           所有入口 summary 数字完全一致，validation_changes 数量、类型一致
  Step 7 : 重启服务（模拟进程重启）
  Step 8 : 重启后重新查询所有 5 个入口，数据完全一致、不丢失
  Step 9 : 强断言：重启前后 content_hash 100% 相同，validation_changes 列表逐项一致
  Step 10: 导出 JSON 与接口查询字节级一致性

关键断言点：
  ✓ validation_changes 包含 6 种类型：new_violation / resolved / modified / new_passed / removed_passed / unchanged
  ✓ summary 新增字段 old_version_validation_status / new_version_validation_status 有值
  ✓ 5 个入口的 content_hash / summary / validation_changes 数量完全一致
  ✓ 重启前后所有数据不变
  ✓ 导出 JSON 的 diff_data 与接口查询返回完全一致
  ✓ CSV 行数与 validation_changes 数量匹配
"""

import requests
import json
import sys
import time
import subprocess
import random
import string
import os
import hashlib

API = "http://127.0.0.1:8003"
PID_FILE = os.path.join(os.path.dirname(__file__), ".test_full_pid")
SERVER_LOG_FILE = os.path.join(os.path.dirname(__file__), "_test_full_server.log")
SERVER_PORT = 8003

H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER = {"X-User-Id": "3"}
H_SUBMITTER = {"X-User-Id": "5"}

passed = 0
failed = 0
_server_proc = None


def test(name, response, expect_status=None, parse_json=True, check_fn=None):
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
            if ok:
                ok = False
                msgs.append("invalid JSON response")
    if check_fn and (data is not None or not parse_json):
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
        if data is not None and parse_json:
            s = json.dumps(data, ensure_ascii=False)
            if len(s) > 1000:
                s = s[:1000] + "..."
            print(f"         response: {s}")
    return data


def precheck_and_import(bid, filename, body, user=H_SUBMITTER, import_format="json"):
    r = requests.post(
        f"{API}/api/batches/{bid}/manifests/precheck",
        headers=user,
        files={"file": (filename, body, "application/json")},
        data={"import_format": import_format},
    )
    if r.status_code != 200:
        return r, None
    d = r.json()
    if not d.get("can_import"):
        return r, None
    token = d.get("precheck_token")
    if not token:
        return r, None
    r = requests.post(
        f"{API}/api/batches/{bid}/manifests/import",
        headers=user,
        files={"file": (filename, body, "application/json")},
        data={"import_format": import_format, "precheck_token": token},
    )
    return r, token


def start_server():
    global _server_proc
    print(f"  Starting server on port {SERVER_PORT}...")
    log_f = open(SERVER_LOG_FILE, "w", encoding="utf-8", buffering=1)
    _server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(SERVER_PORT)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    with open(PID_FILE, "w") as f:
        f.write(str(_server_proc.pid))
    for _ in range(30):
        time.sleep(0.5)
        try:
            r = requests.get(f"{API}/health", timeout=2)
            if r.status_code == 200:
                print(f"  Server started (PID={_server_proc.pid})")
                return _server_proc
        except Exception:
            pass
    print(f"  [ERROR] Server failed to start within 15 seconds")
    return _server_proc


def stop_server():
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        print("  Stopping server...")
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
            _server_proc.wait()
        print("  Server stopped")
    _server_proc = None


def sha256_json(d) -> str:
    canonical = json.dumps(d, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_summary_keys(summary: dict) -> dict:
    want = [
        "added_count", "removed_count", "modified_count", "unchanged_count",
        "field_change_count",
        "validation_errors_old", "validation_errors_new",
        "validation_warnings_old", "validation_warnings_new",
        "validation_passed_old", "validation_passed_new",
        "validation_total_old", "validation_total_new",
        "validation_changes_new_violation", "validation_changes_resolved",
        "validation_changes_modified", "validation_changes_new_passed",
        "validation_changes_removed_passed", "validation_changes_unchanged",
        "validation_changes_total",
        "unresolved_rejections_old", "unresolved_rejections_new",
        "old_version_validation_status", "new_version_validation_status",
    ]
    return {k: summary.get(k) for k in want}


def collect_validation_change_fingerprints(changes: list) -> list:
    fps = []
    for c in changes:
        fp = (
            c.get("change_type"),
            c.get("rule_code"),
            c.get("item_key"),
            c.get("field_name"),
            c.get("old_passed"),
            c.get("new_passed"),
            c.get("old_severity"),
            c.get("new_severity"),
        )
        fps.append(fp)
    fps.sort()
    return fps


def main():
    global passed, failed

    print("=" * 78)
    print("  校验结果持久化 + 多入口一致性 完整回归测试")
    print("=" * 78)

    start_server()
    time.sleep(1)

    try:
        # =================================================================
        # Step 1: 创建批次
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 1: 创建测试批次")
        print("=" * 78)
        code = "FULL-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
            "batch_code": code,
            "name": f"[FULL-REGRESSION] {code} 校验差异持久化全链路",
            "description": "真实业务流程：导入v1(有问题)→驳回→返修→导入v2→校验→多入口对比→重启→再对比",
            "submitter_id": 5,
        })
        data = test("创建批次", r, 201)
        bid = data["id"]
        print(f"  batch_id={bid}, batch_code={code}")

        # =================================================================
        # Step 2: 导入 v1（有意构造 error + warning）
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 2: 导入 v1（包含 1 个 error + 1 个通过的 item + 1 个 warning）")
        print("=" * 78)
        v1_items = [
            {"item_id": "ITEM-A", "item_name": "Part A", "quantity": -3, "unit_price": 100},
            {"item_id": "ITEM-B", "item_name": "Part B", "quantity": 2,  "unit_price": 200},
            {"item_id": "ITEM-C", "item_name": "Part C", "quantity": 50000, "unit_price": 50},
        ]
        v2_items = [
            {"item_id": "ITEM-A", "item_name": "Part A", "quantity": 3,     "unit_price": 100},
            {"item_id": "ITEM-C", "item_name": "Part C", "quantity": 60000, "unit_price": 50},
            {"item_id": "ITEM-D", "item_name": "Part D", "quantity": -1,    "unit_price": 999},
        ]
        v1_json = json.dumps(v1_items)
        r, _ = precheck_and_import(bid, "v1.json", v1_json, H_SUBMITTER)
        test("导入 v1 成功", r, 200, check_fn=lambda d: d.get("success") is True)

        r = requests.post(f"{API}/api/batches/{bid}/validate", headers=H_ADMIN)
        val_v1 = test("对 v1 执行校验", r, 200, check_fn=lambda d: d.get("success") is True)
        print(f"  v1: passed={val_v1['passed']}, failed={val_v1['failed']}, warnings={val_v1['warnings']}")
        assert val_v1["failed"] >= 1, "v1 构造的 error 未生效"

        # =================================================================
        # Step 3: 提交审核 + 评审驳回
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 3: 提交审核 → 评审人员 item-level 驳回")
        print("=" * 78)
        r = requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER, json={
            "target_status": "pending_review",
            "comment": "v1 已准备好，请评审"
        })
        test("提交审核 (draft → pending_review)", r, 200)

        r = requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
            "rejections": [
                {"item_key": "ITEM-A", "line_number": 1, "rejection_reason": "数量为负，必须修复；建议改 3"},
                {"item_key": "ITEM-C", "line_number": 3, "rejection_reason": "数量异常高（50000），请确认后修改"},
            ],
            "comment": "ITEM-A 和 ITEM-C 需要返修"
        })
        test("评审驳回（2 条 item-level 驳回）", r, 200)

        # =================================================================
        # Step 4: 开始返修 → 导入 v2
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 4: 开始返修 → 导入 v2（修复 A，保留 C 的 warning，删除 B，新增 D 带新 error）")
        print("=" * 78)
        r = requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER, json={
            "comment": "收到，开始修复 ITEM-A 和 ITEM-C"
        })
        test("开始返修状态流转", r, 200)

        v2_json = json.dumps(v2_items)
        r, _ = precheck_and_import(bid, "v2.json", v2_json, H_SUBMITTER)
        test("导入 v2（修复+删除+新增）", r, 200, check_fn=lambda d: d.get("success") is True)

        # =================================================================
        # Step 5: 执行 v2 校验（自动刷新快照）
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 5: 对 v2 执行校验 → 应自动刷新快照（数据面沉底）")
        print("=" * 78)
        r = requests.post(f"{API}/api/batches/{bid}/validate", headers=H_ADMIN)
        val_v2 = test("对 v2 执行校验（触发快照刷新）", r, 200, check_fn=lambda d: d.get("success") is True)
        print(f"  v2: passed={val_v2['passed']}, failed={val_v2['failed']}, warnings={val_v2['warnings']}")

        msg = val_v2.get("message", "")
        test("校验返回 message 字段存在且非空", r, check_fn=lambda d: (
            True if (isinstance(msg, str) and len(msg) > 0)
            else f"message type={type(msg).__name__}, len={len(msg) if isinstance(msg, str) else 'N/A'}"
        ))
        print(f"  validate message: {msg[:80]}" + ("..." if len(msg) > 80 else ""))

        time.sleep(1.0)
        print("  【查看上方服务日志】→ 应能看到 [VALIDATE_START] / [VALIDATE_DONE] / "
              "[BATCH_REFRESH:validate] / [REFRESH:validate] 的完整链路")

        # =================================================================
        # Step 6: 5 个入口一致性断言
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 6: 5 个入口一致性 + 6 种 validation_changes 类型断言")
        print("=" * 78)

        r = requests.get(f"{API}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
        snap_latest = test("[1/5] GET /snapshots/latest", r, 200, check_fn=lambda d: "id" in d)

        r = requests.get(
            f"{API}/api/batches/{bid}/snapshots/by-versions?old_version=1&new_version=2",
            headers=H_LEAD
        )
        snap_byver = test("[2/5] GET /snapshots/by-versions", r, 200, check_fn=lambda d: "id" in d)

        snap_id = snap_latest["id"]
        r = requests.get(f"{API}/api/batches/{bid}/snapshots/{snap_id}", headers=H_LEAD)
        snap_byid = test("[3/5] GET /snapshots/{id}", r, 200, check_fn=lambda d: d["id"] == snap_id)

        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff?old_version=1&new_version=2",
            headers=H_LEAD
        )
        diff_resp = test("[4/5] GET /version-diff", r, 200, check_fn=lambda d: "summary" in d)

        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json",
            headers=H_LEAD
        )
        export_json = test("[5/5] GET /version-diff/export (json)", r, 200,
                           check_fn=lambda d: "diff_data" in d and "export_id" in d)

        # ---- 6 种 validation_changes 类型全部存在 ----
        types_present = set(c["change_type"] for c in snap_latest["validation_changes"])
        expected_types = {
            "new_violation", "resolved", "modified",
            "new_passed", "removed_passed", "unchanged"
        }
        test("validation_changes 包含全部 6 种 change_type（新增/消失/延续完整链路）",
             r, check_fn=lambda d: expected_types.issubset(types_present) or (
                 f"missing={expected_types - types_present}"
                 if not expected_types.issubset(types_present) else True
             ))
        print(f"  types present: {sorted(types_present)}")

        # ---- summary 新增 status 字段 ----
        s = snap_latest["summary"]
        test("summary 含 old_version_validation_status 字段", r,
             check_fn=lambda d: (
                 True if ("old_version_validation_status" in s and s.get("old_version_validation_status") is not None)
                 else f"keys={list(s.keys())[:20]}"
             ))
        test("summary 含 new_version_validation_status 字段", r,
             check_fn=lambda d: (
                 True if ("new_version_validation_status" in s and s.get("new_version_validation_status") is not None)
                 else f"keys={list(s.keys())[:20]}"
             ))
        print(f"  old_status={s.get('old_version_validation_status')}, "
              f"new_status={s.get('new_version_validation_status')}")

        # ---- 所有入口的 content_hash 100% 相同 ----
        hash_latest = snap_latest["content_hash"]
        hash_byver = snap_byver["content_hash"]
        hash_byid = snap_byid["content_hash"]
        test("3 个快照入口 content_hash 完全一致", r,
             check_fn=lambda d: hash_latest == hash_byver == hash_byid
             or (f"latest={hash_latest[:12]} byver={hash_byver[:12]} byid={hash_byid[:12]}"))
        print(f"  content_hash = {hash_latest[:24]}... (3 个快照入口一致)")

        # ---- 所有入口的 summary 规范化后完全一致 ----
        sum_latest_norm = normalize_summary_keys(snap_latest["summary"])
        sum_byver_norm = normalize_summary_keys(snap_byver["summary"])
        sum_byid_norm = normalize_summary_keys(snap_byid["summary"])
        sum_diff_norm = normalize_summary_keys(diff_resp["summary"])
        sum_export_norm = normalize_summary_keys(export_json["diff_data"]["summary"])
        test("5 个入口的 summary 数字完全一致", r,
             check_fn=lambda d:
             sum_latest_norm == sum_byver_norm == sum_byid_norm == sum_diff_norm == sum_export_norm
             or (f"latest={sum_latest_norm}\nbyver={sum_byver_norm}\nbyid={sum_byid_norm}\n"
                 f"diff={sum_diff_norm}\nexport={sum_export_norm}"))

        # ---- validation_changes 数量 + fingerprints 一致 ----
        vc_count_latest = len(snap_latest["validation_changes"])
        vc_count_byver = len(snap_byver["validation_changes"])
        vc_count_byid = len(snap_byid["validation_changes"])
        vc_count_diff = len(diff_resp["validation_changes"])
        vc_count_export = len(export_json["diff_data"]["validation_changes"])
        test("5 个入口的 validation_changes 数量一致", r,
             check_fn=lambda d:
             vc_count_latest == vc_count_byver == vc_count_byid == vc_count_diff == vc_count_export
             or (f"latest={vc_count_latest}, byver={vc_count_byver}, byid={vc_count_byid}, "
                 f"diff={vc_count_diff}, export={vc_count_export}"))

        fp_latest = collect_validation_change_fingerprints(snap_latest["validation_changes"])
        fp_export = collect_validation_change_fingerprints(export_json["diff_data"]["validation_changes"])
        test("latest 快照 与 JSON 导出的 validation_changes 逐项指纹一致", r,
             check_fn=lambda d: fp_latest == fp_export
             or (f"latest_fp={fp_latest[:5]}\nexport_fp={fp_export[:5]}"))

        # ---- validation_changes_total 与实际列表长度一致 ----
        vc_total_from_summary = s["validation_changes_total"]
        test("summary.validation_changes_total == 列表长度", r,
             check_fn=lambda d: vc_total_from_summary == vc_count_latest
             or f"summary_total={vc_total_from_summary}, actual={vc_count_latest}")

        # ---- CSV 导出包含 validation_change 行 ----
        r_csv = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=csv",
            headers=H_LEAD
        )
        test("CSV 导出返回 200", r_csv, 200, parse_json=False)
        csv_text = r_csv.text
        csv_lines = csv_text.strip().split("\n")
        test(f"CSV 包含 validation_change 行 (共 {len(csv_lines)} 行)", r_csv, parse_json=False,
             check_fn=lambda resp: any("validation_change" in l for l in csv_lines)
             or "未找到 validation_change 行")
        val_csv_count = sum(1 for l in csv_lines if l.startswith("validation_change"))
        test(f"CSV validation_change 行数 ({val_csv_count}) == summary 列表总数 ({vc_count_latest})",
             r_csv, parse_json=False,
             check_fn=lambda resp: val_csv_count == vc_count_latest
             or f"csv={val_csv_count}, list={vc_count_latest}")

        # ---- 导出 JSON 的 diff_data 与 /version-diff 接口响应字节级一致 ----
        export_canonal = sha256_json(export_json["diff_data"])
        diff_canonical = sha256_json(diff_resp)
        r_json_hdr_check = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json",
            headers=H_LEAD
        )
        test("JSON 导出的 diff_data 与 /version-diff 接口响应 hash 一致", r_json_hdr_check,
             check_fn=lambda d: export_canonal == diff_canonical
             or (f"export_sha={export_canonal[:16]}, diff_sha={diff_canonical[:16]}"))
        print(f"  导出 vs 接口一致性: sha256={export_canonal[:24]}...")

        # =================================================================
        # Step 7: 保存重启前的完整状态
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 7: 保存重启前完整状态（content_hash + validation_changes 指纹）")
        print("=" * 78)
        hash_before = hash_latest
        sum_before = sum_latest_norm
        vc_fp_before = fp_latest
        vc_count_before = vc_count_latest
        print(f"  重启前 content_hash              = {hash_before}")
        print(f"  重启前 validation_changes_total  = {sum_before['validation_changes_total']}")
        print(f"  重启前 errors_new / warnings_new = "
              f"{sum_before['validation_errors_new']} / {sum_before['validation_warnings_new']}")

        # =================================================================
        # Step 8: 重启服务
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 8: 重启服务（模拟进程退出，验证快照持久化在数据库）")
        print("=" * 78)
        stop_server()
        time.sleep(3)
        start_server()
        time.sleep(2)
        print("  服务已重启完成")
        time.sleep(0.5)

        # =================================================================
        # Step 9: 重启后重新查询所有入口，数据完全一致
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 9: 重启后重查 5 个入口 → content_hash 100% 相同")
        print("=" * 78)

        r = requests.get(f"{API}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
        after_latest = test("重启后: /snapshots/latest", r, 200)

        r = requests.get(
            f"{API}/api/batches/{bid}/snapshots/by-versions?old_version=1&new_version=2",
            headers=H_LEAD
        )
        after_byver = test("重启后: /snapshots/by-versions", r, 200)

        r = requests.get(f"{API}/api/batches/{bid}/snapshots/{snap_id}", headers=H_LEAD)
        after_byid = test("重启后: /snapshots/{id}", r, 200, check_fn=lambda d: d["id"] == snap_id)

        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff?old_version=1&new_version=2",
            headers=H_LEAD
        )
        after_diff = test("重启后: /version-diff", r, 200)

        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json",
            headers=H_LEAD
        )
        after_export = test("重启后: /export (json)", r, 200)

        # ---- 强断言：重启前后 content_hash 逐字节相同 ----
        r_after_consistency = requests.get(
            f"{API}/api/batches/{bid}/snapshots/latest", headers=H_LEAD
        )
        test("[CRITICAL] 重启后 latest content_hash 与重启前完全一致", r_after_consistency,
             check_fn=lambda d: after_latest["content_hash"] == hash_before
             or (f"before={hash_before[:24]}, after={after_latest['content_hash'][:24]}"))
        test("[CRITICAL] 重启后 by-versions content_hash 与重启前一致", r_after_consistency,
             check_fn=lambda d: after_byver["content_hash"] == hash_before)
        test("[CRITICAL] 重启后 by-id content_hash 与重启前一致", r_after_consistency,
             check_fn=lambda d: after_byid["content_hash"] == hash_before)

        # ---- 重启后 summary 完全一致 ----
        sum_after_norm = normalize_summary_keys(after_latest["summary"])
        test("[CRITICAL] 重启前后 summary 数字完全一致", r_after_consistency,
             check_fn=lambda d: sum_after_norm == sum_before
             or (f"before={sum_before}\nafter={sum_after_norm}"))

        # ---- 重启前后 validation_changes 指纹一致 ----
        fp_after = collect_validation_change_fingerprints(after_latest["validation_changes"])
        test("[CRITICAL] 重启前后 validation_changes 逐项指纹 100% 一致", r_after_consistency,
             check_fn=lambda d: fp_after == vc_fp_before
             or (f"before_count={len(vc_fp_before)}, after_count={len(fp_after)}"))
        test("[CRITICAL] 重启前后 validation_changes 条数一致", r_after_consistency,
             check_fn=lambda d: len(after_latest["validation_changes"]) == vc_count_before)

        # ---- 重启后 5 个入口仍然完全一致 ----
        ah_latest = after_latest["content_hash"]
        ah_byver = after_byver["content_hash"]
        ah_byid = after_byid["content_hash"]
        test("[CRITICAL] 重启后 3 个快照入口 content_hash 仍一致", r_after_consistency,
             check_fn=lambda d: ah_latest == ah_byver == ah_byid)

        # ---- 重启后 JSON 导出 vs version-diff 一致性（重新拉 JSON 响应） ----
        after_diff_export_sha = sha256_json(after_export["diff_data"])
        after_diff_sha = sha256_json(after_diff)
        r_after_chk = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json",
            headers=H_LEAD
        )
        test("[CRITICAL] 重启后 JSON 导出 与 version-diff 仍一致", r_after_chk,
             check_fn=lambda d: after_diff_export_sha == after_diff_sha
             or (f"export={after_diff_export_sha[:16]}, diff={after_diff_sha[:16]}"))

        # ---- CSV 导出重启后也一致 ----
        r_csv_after = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=csv",
            headers=H_LEAD
        )
        test("重启后 CSV 导出", r_csv_after, 200, parse_json=False)
        csv_after_lines = r_csv_after.text.strip().split("\n")
        test("重启前后 CSV 行数相同", r_csv_after, parse_json=False,
             check_fn=lambda resp: len(csv_after_lines) == len(csv_lines)
             or f"before={len(csv_lines)}, after={len(csv_after_lines)}")

        # =================================================================
        # Step 10: 审批日志链路验证
        # =================================================================
        print("\n" + "=" * 78)
        print("  Step 10: 审批日志链路验证（CREATE_DIFF_SNAPSHOT、QUERY、EXPORT 都有）")
        print("=" * 78)
        r = requests.get(f"{API}/api/batches/{bid}/approval-logs", headers=H_LEAD)
        logs = test("获取审批日志", r, 200)
        if isinstance(logs, list):
            actions = [l.get("action") for l in logs]
            test("日志包含 CREATE_DIFF_SNAPSHOT（导入v2自动创建）", r,
                 check_fn=lambda d: "CREATE_DIFF_SNAPSHOT" in actions)
            test("日志包含 QUERY_DIFF_SNAPSHOT（各入口查询）", r,
                 check_fn=lambda d: "QUERY_DIFF_SNAPSHOT" in actions)
            test("日志包含 EXPORT_VERSION_DIFF（JSON导出）", r,
                 check_fn=lambda d: "EXPORT_VERSION_DIFF" in actions)
            test("日志包含 EXPORT_DIFF_SNAPSHOT_CSV（CSV导出）", r,
                 check_fn=lambda d: "EXPORT_DIFF_SNAPSHOT_CSV" in actions)
            test("日志包含 VIEW_VERSION_DIFF（version-diff接口）", r,
                 check_fn=lambda d: "VIEW_VERSION_DIFF" in actions)

            create_logs = [l for l in logs if l.get("action") == "CREATE_DIFF_SNAPSHOT"]
            if create_logs:
                extra = create_logs[0].get("extra_data") or {}
                test("CREATE_DIFF_SNAPSHOT 日志含 snapshot_id / content_hash extra", r,
                     check_fn=lambda d: "snapshot_id" in extra and "content_hash" in extra)

        # =================================================================
        # 最终总结
        # =================================================================
        print("\n" + "=" * 78)
        print("  测试结果总结")
        print("=" * 78)
        print(f"  通过: {passed}")
        print(f"  失败: {failed}")
        print(f"  总计: {passed + failed}")
        rate = (passed / (passed + failed) * 100) if (passed + failed) > 0 else 0
        print(f"  成功率: {rate:.1f}%")

        print("\n  关键链路验证:")
        print("  [OK] 校验结果变化沉到数据面: validation_changes 6 种类型齐全")
        print("       (new_violation=新增违规, resolved=已解决, modified=违规变更,")
        print("        new_passed=新增通过, removed_passed=移除通过, unchanged=延续未变)")
        print("  [OK] 最新快照 / by-versions / by-id / version-diff / JSON导出 / CSV导出 → 同一份结果")
        print("  [OK] 服务重启后 content_hash 逐字节不变（快照持久化到 SQLite）")
        print("  [OK] summary 新增 old/new_version_validation_status 字段")
        print("  [OK] JSON导出的 diff_data 与 /version-diff 接口响应 SHA256 一致")
        print("  [OK] 审批日志完整记录 CREATE / QUERY / VIEW / EXPORT 全链路")

        print(f"\n  测试批次 ID  : {bid}")
        print(f"  测试批次 code: {code}")
        print(f"  快照 ID      : {snap_id}")
        print(f"  content_hash : {hash_before}")

        if failed == 0:
            print("\n  [PASS] 全链路回归通过 [OK]")
            return 0
        else:
            print("\n  [FAIL] 存在测试失败 [X]")
            # ---------- 失败时打印服务器关键诊断日志 ----------
            print("\n" + "=" * 78)
            print("  服务器关键诊断日志（失败场景分析）")
            print("=" * 78)
            try:
                with open(SERVER_LOG_FILE, "r", encoding="utf-8", errors="replace") as lf:
                    for line in lf:
                        line = line.rstrip()
                        if any(tok in line for tok in [
                            "Created new snapshot", "summary_fields=",
                            "[VALIDATE_START]", "[VALIDATE_DONE]",
                            "[BATCH_REFRESH:", "[REFRESH:",
                            "schema upgrade", "SCHEMA_UPGRADE", "stale_reasons",
                            "kind=schema_only", "kind=content_changed",
                        ]):
                            print("  " + line)
            except Exception as _e:
                print(f"  读取日志失败: {_e}")
            return 1

    finally:
        stop_server()
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[ERROR] 测试执行异常: {e}")
        import traceback
        traceback.print_exc()
        stop_server()
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        sys.exit(1)
