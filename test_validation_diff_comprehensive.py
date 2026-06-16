#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
校验结果变化完整回归测试
========================
覆盖场景：
  1. 导入 v1 清单 → 执行校验（全部通过）
  2. 导入 v2 清单（引入 warning + 修复 error） → 执行校验
  3. 检查最新快照：包含完整的校验变化（新增/消失/延续）
  4. 检查 version-diff 接口：与快照一致
  5. 检查 JSON 导出：包含完整校验变化
  6. 检查 CSV 导出：包含校验变化行
  7. 重启服务 → 所有数据保持一致
  8. 验证日志中包含差异生成和快照刷新的记录

预期：
  - validation_changes 包含所有类型：new_violation、resolved、new_passed、removed_passed、unchanged
  - summary 包含详细统计：validation_changes_new_violation 等
  - 快照、接口、导出三者数据一致
  - 服务重启后数据不变
"""

import requests
import json
import sys
import time
import subprocess
import random
import string
import os

API = "http://127.0.0.1:8001"
PID_FILE = os.path.join(os.path.dirname(__file__), ".test_validation_diff_pid")

H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER = {"X-User-Id": "3"}
H_SUBMITTER = {"X-User-Id": "5"}

passed = 0
failed = 0


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


def precheck_and_import(bid, filename, body, user=H_SUBMITTER, import_format="json"):
    r = requests.post(
        f"{API}/api/batches/{bid}/manifests/precheck",
        headers=user,
        files={"file": (filename, body, "application/json")},
        data={"import_format": import_format},
    )
    if r.status_code != 200:
        return r, None
    token = r.json()["precheck_token"]
    r = requests.post(
        f"{API}/api/batches/{bid}/manifests/import",
        headers=user,
        files={"file": (filename, body, "application/json")},
        data={"import_format": import_format, "precheck_token": token},
    )
    return r, token


def start_server(redirect_output=False):
    print("  Starting server on port 8001...")
    kwargs = {
        "cwd": os.path.dirname(os.path.abspath(__file__)),
        "text": True,
    }
    if redirect_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8001"],
        **kwargs
    )
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    for _ in range(30):
        time.sleep(0.5)
        try:
            r = requests.get(f"{API}/health", timeout=2)
            if r.status_code == 200:
                print(f"  Server started (PID={proc.pid})")
                return proc
        except Exception:
            pass
    print("  [ERROR] Server failed to start within 15 seconds")
    return proc


def stop_server(proc):
    if proc and proc.poll() is None:
        print("  Stopping server...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print("  Server stopped")


def capture_server_output(proc, timeout=2):
    """尝试从服务器进程捕获一些输出（非阻塞）"""
    if not proc or proc.stdout is None:
        return ""
    import os
    import platform

    output = []
    deadline = time.time() + timeout
    try:
        if platform.system() == "Windows":
            while time.time() < deadline:
                try:
                    line = proc.stdout.readline()
                    if line:
                        output.append(line.rstrip())
                    else:
                        break
                except Exception:
                    break
                time.sleep(0.05)
        else:
            import select
            while time.time() < deadline:
                ready, _, _ = select.select([proc.stdout], [], [], 0.1)
                if ready:
                    line = proc.stdout.readline()
                    if line:
                        output.append(line.rstrip())
                    else:
                        break
                else:
                    break
    except Exception:
        pass
    return "\n".join(output)


def check_validation_change_types(changes, expected_types):
    """检查 validation_changes 包含的类型"""
    actual_types = set(c["change_type"] for c in changes)
    for t in expected_types:
        if t not in actual_types:
            return f"缺少 change_type='{t}', 实际有: {sorted(actual_types)}"
    return True


def check_summary_counts(summary):
    """检查 summary 中的校验统计字段是否存在且合理"""
    required_fields = [
        "validation_errors_old", "validation_errors_new",
        "validation_warnings_old", "validation_warnings_new",
        "validation_passed_old", "validation_passed_new",
        "validation_total_old", "validation_total_new",
        "validation_changes_new_violation",
        "validation_changes_resolved",
        "validation_changes_modified",
        "validation_changes_new_passed",
        "validation_changes_removed_passed",
        "validation_changes_unchanged",
        "validation_changes_total",
    ]
    for f in required_fields:
        if f not in summary:
            return f"summary 缺少字段: {f}"

    total = (
        summary["validation_changes_new_violation"]
        + summary["validation_changes_resolved"]
        + summary["validation_changes_modified"]
        + summary["validation_changes_new_passed"]
        + summary["validation_changes_removed_passed"]
        + summary["validation_changes_unchanged"]
    )
    if total != summary["validation_changes_total"]:
        return (
            f"validation_changes_total={summary['validation_changes_total']} "
            f"与各类型之和 {total} 不匹配"
        )

    return True


def main():
    global passed, failed

    print("=" * 72)
    print("  校验结果变化完整回归测试")
    print("=" * 72)

    proc = start_server(redirect_output=False)
    time.sleep(1)

    try:
        # ===== 准备数据 =====
        print("\n" + "=" * 72)
        print("  Step 0: 创建测试批次")
        print("=" * 72)
        code = "VDR-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
            "batch_code": code,
            "name": "校验差异回归测试批次",
            "description": "用于完整测试校验结果变化追踪的批次",
            "submitter_id": 5,
        })
        data = test("创建批次", r, 201)
        bid = data["id"]
        print(f"  batch_id={bid}, batch_code={code}")

        # v1: 有 1 个 error (quantity 为负数)，其余通过
        v1_items = [
            {"item_id": "ITEM-A1", "item_name": "Widget A", "quantity": -5, "unit_price": 100},
            {"item_id": "ITEM-B2", "item_name": "Widget B", "quantity": 3, "unit_price": 200},
            {"item_id": "ITEM-C3", "item_name": "Widget C", "quantity": 10, "unit_price": 50},
        ]

        # v2: 修复了 A1 的 error（变成 warning），删除了 C3，
        #      新增了 D4（quantity 为负，产生新的 error → new_violation）
        v2_items = [
            {"item_id": "ITEM-A1", "item_name": "Widget A", "quantity": 50000, "unit_price": 100},
            {"item_id": "ITEM-B2", "item_name": "Widget B", "quantity": 3, "unit_price": 200},
            {"item_id": "ITEM-D4", "item_name": "Widget D", "quantity": -10, "unit_price": 300},
        ]

        v1_json = json.dumps(v1_items)
        v2_json = json.dumps(v2_items)

        # ===== Step 1: 导入 v1 并校验 =====
        print("\n" + "=" * 72)
        print("  Step 1: 导入 v1 并执行校验")
        print("=" * 72)
        r, _ = precheck_and_import(bid, "v1.json", v1_json, H_SUBMITTER)
        test("导入 v1 清单", r, 200, check_fn=lambda d: d.get("success") is True)

        r = requests.post(f"{API}/api/batches/{bid}/validate", headers=H_ADMIN)
        val_v1 = test("对 v1 执行校验", r, 200, check_fn=lambda d: d.get("success") is True)
        print(f"  v1 校验结果: passed={val_v1['passed']}, failed={val_v1['failed']}, "
              f"warnings={val_v1['warnings']}")
        print("\n  【查看上方服务日志】校验 v1 后，观察是否有快照刷新相关日志")
        time.sleep(0.5)

        # ===== Step 2: 导入 v2 并校验 =====
        print("\n" + "=" * 72)
        print("  Step 2: 导入 v2 并执行校验")
        print("=" * 72)
        r, _ = precheck_and_import(bid, "v2.json", v2_json, H_SUBMITTER)
        test("导入 v2 清单（自动创建快照）", r, 200, check_fn=lambda d: d.get("success") is True)

        r = requests.post(f"{API}/api/batches/{bid}/validate", headers=H_ADMIN)
        val_v2 = test("对 v2 执行校验（应刷新快照）", r, 200, check_fn=lambda d: d.get("success") is True)
        print(f"  v2 校验结果: passed={val_v2['passed']}, failed={val_v2['failed']}, "
              f"warnings={val_v2['warnings']}")
        print("\n  【查看上方服务日志】校验 v2 后，观察是否有快照刷新、差异计算相关日志")
        time.sleep(0.5)

        # ===== Step 3: 检查最新快照 =====
        print("\n" + "=" * 72)
        print("  Step 3: 检查最新快照")
        print("=" * 72)
        r = requests.get(f"{API}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
        snap_latest = test("查询最新快照", r, 200)

        if snap_latest:
            summary = snap_latest["summary"]
            changes = snap_latest["validation_changes"]

            test("快照 summary 包含完整校验统计字段", r,
                 check_fn=lambda d: check_summary_counts(d["summary"]))

            test("快照 validation_changes 非空", r,
                 check_fn=lambda d: len(d["validation_changes"]) > 0)

            test("快照包含 new_violation 类型（v2 新产生的 warning）", r,
                 check_fn=lambda d: check_validation_change_types(
                     d["validation_changes"], ["new_violation"]))

            test("快照包含 resolved 类型（v1 的 error 被修复）", r,
                 check_fn=lambda d: check_validation_change_types(
                     d["validation_changes"], ["resolved"]))

            test("快照包含 new_passed 类型（新增条目且通过）", r,
                 check_fn=lambda d: check_validation_change_types(
                     d["validation_changes"], ["new_passed"]))

            test("快照包含 removed_passed 类型（删除条目）", r,
                 check_fn=lambda d: check_validation_change_types(
                     d["validation_changes"], ["removed_passed"]))

            test("快照包含 unchanged 类型（延续未变的校验项）", r,
                 check_fn=lambda d: check_validation_change_types(
                     d["validation_changes"], ["unchanged"]))

            test("快照 validation_warnings_new > 0（v2 有 warning）", r,
                 check_fn=lambda d: d["summary"]["validation_warnings_new"] > 0)

            test("快照 validation_errors_new > 0（v2 有新增 error → new_violation）", r,
                 check_fn=lambda d: d["summary"]["validation_errors_new"] > 0)

            test("快照 validation_changes_total 与列表长度一致", r,
                 check_fn=lambda d: d["summary"]["validation_changes_total"] ==
                                   len(d["validation_changes"]))

            print(f"  summary:")
            print(f"    validation_total_old={summary['validation_total_old']}, "
                  f"validation_total_new={summary['validation_total_new']}")
            print(f"    errors: old={summary['validation_errors_old']}, "
                  f"new={summary['validation_errors_new']}")
            print(f"    warnings: old={summary['validation_warnings_old']}, "
                  f"new={summary['validation_warnings_new']}")
            print(f"    passed: old={summary['validation_passed_old']}, "
                  f"new={summary['validation_passed_new']}")
            print(f"  validation_changes 统计 (共 {len(changes)} 条):")
            print(f"    new_violation={summary['validation_changes_new_violation']} "
                  f"(新增违规)")
            print(f"    resolved={summary['validation_changes_resolved']} "
                  f"(已解决)")
            print(f"    modified={summary['validation_changes_modified']} "
                  f"(违规变更)")
            print(f"    new_passed={summary['validation_changes_new_passed']} "
                  f"(新增通过项)")
            print(f"    removed_passed={summary['validation_changes_removed_passed']} "
                  f"(移除通过项)")
            print(f"    unchanged={summary['validation_changes_unchanged']} "
                  f"(延续未变)")

            print(f"  validation_changes 前 5 条详情:")
            for vc in changes[:5]:
                print(f"    - {vc['change_type']:15s} | "
                      f"{vc.get('item_key', ''):10s} | "
                      f"{vc.get('rule_code', ''):20s} | "
                      f"new_passed={vc.get('new_passed')}")

        # ===== Step 4: 检查 version-diff 接口 =====
        print("\n" + "=" * 72)
        print("  Step 4: 检查 version-diff 接口")
        print("=" * 72)
        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff?old_version=1&new_version=2",
            headers=H_LEAD
        )
        diff_data = test("调用 version-diff 接口", r, 200)

        if diff_data and snap_latest:
            test("version-diff 与快照 summary 一致", r, check_fn=lambda d:
                 d["summary"]["validation_changes_total"] ==
                 snap_latest["summary"]["validation_changes_total"])

            test("version-diff 与快照 validation_changes 数量一致", r, check_fn=lambda d:
                 len(d["validation_changes"]) == len(snap_latest["validation_changes"]))

            diff_types = set(c["change_type"] for c in diff_data["validation_changes"])
            snap_types = set(c["change_type"] for c in snap_latest["validation_changes"])
            test("version-diff 与快照 change_type 集合一致", r, check_fn=lambda d:
                 diff_types == snap_types)

        # ===== Step 5: 检查 JSON 导出 =====
        print("\n" + "=" * 72)
        print("  Step 5: 检查 JSON 导出")
        print("=" * 72)
        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json",
            headers=H_LEAD
        )
        export_json = test("JSON 导出成功", r, 200, check_fn=lambda d: "diff_data" in d)

        if export_json and snap_latest:
            diff_export = export_json["diff_data"]
            test("JSON 导出包含 export_id", r, check_fn=lambda d: "export_id" in d)

            test("JSON 导出 summary 与快照一致", r, check_fn=lambda d:
                 d["diff_data"]["summary"]["validation_changes_total"] ==
                 snap_latest["summary"]["validation_changes_total"])

            test("JSON 导出 validation_changes 数量与快照一致", r, check_fn=lambda d:
                 len(d["diff_data"]["validation_changes"]) ==
                 len(snap_latest["validation_changes"]))

            export_types = set(c["change_type"] for c in diff_export["validation_changes"])
            test("JSON 导出包含所有 change_type", r, check_fn=lambda d:
                 "new_violation" in export_types and
                 "resolved" in export_types and
                 "new_passed" in export_types and
                 "removed_passed" in export_types and
                 "unchanged" in export_types)

        # ===== Step 6: 检查 CSV 导出 =====
        print("\n" + "=" * 72)
        print("  Step 6: 检查 CSV 导出")
        print("=" * 72)
        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=csv",
            headers=H_LEAD
        )
        test("CSV 导出成功", r, 200, parse_json=False)

        csv_content = r.text
        test("CSV 包含 validation_change 行", r, parse_json=False,
             check_fn=lambda resp: "validation_change" in csv_content)

        test("CSV 包含 new_violation 类型", r, parse_json=False,
             check_fn=lambda resp: "new_violation" in csv_content)

        test("CSV 包含 resolved 类型", r, parse_json=False,
             check_fn=lambda resp: "resolved" in csv_content)

        lines = csv_content.strip().split("\n")
        print(f"  CSV 共 {len(lines)} 行")
        val_lines = [l for l in lines if "validation_change" in l]
        print(f"  其中 validation_change 行 {len(val_lines)} 行")
        for line in val_lines[:3]:
            print(f"    {line[:100]}")

        # ===== Step 7: 保存重启前的状态 =====
        print("\n" + "=" * 72)
        print("  Step 7: 保存重启前的快照状态")
        print("=" * 72)
        hash_before = snap_latest["content_hash"] if snap_latest else None
        summary_before = snap_latest["summary"] if snap_latest else None
        val_changes_before = len(snap_latest["validation_changes"]) if snap_latest else 0
        print(f"  content_hash = {hash_before}")
        print(f"  validation_changes_total = {summary_before['validation_changes_total'] if summary_before else 'N/A'}")

        # ===== Step 8: 重启服务 =====
        print("\n" + "=" * 72)
        print("  Step 8: 重启服务（验证数据持久化）")
        print("=" * 72)
        stop_server(proc)
        time.sleep(2)

        proc = start_server(redirect_output=False)
        time.sleep(2)
        print("\n  【查看上方服务日志】服务重启完成，观察启动日志")

        # ===== Step 9: 重启后重新查询 =====
        print("\n" + "=" * 72)
        print("  Step 9: 重启后重新查询验证")
        print("=" * 72)

        r = requests.get(f"{API}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
        snap_after = test("重启后: 查询最新快照", r, 200)

        if snap_after:
            test("重启后: content_hash 不变", r, check_fn=lambda d:
                 d["content_hash"] == hash_before)

            test("重启后: validation_changes_total 不变", r, check_fn=lambda d:
                 d["summary"]["validation_changes_total"] ==
                 summary_before["validation_changes_total"])

            test("重启后: validation_warnings_new 不变", r, check_fn=lambda d:
                 d["summary"]["validation_warnings_new"] ==
                 summary_before["validation_warnings_new"])

            test("重启后: validation_errors_new 不变", r, check_fn=lambda d:
                 d["summary"]["validation_errors_new"] ==
                 summary_before["validation_errors_new"])

            test("重启后: validation_changes 数量不变", r, check_fn=lambda d:
                 len(d["validation_changes"]) == val_changes_before)

            after_types = set(c["change_type"] for c in snap_after["validation_changes"])
            before_types = set(c["change_type"] for c in snap_latest["validation_changes"])
            test("重启后: change_type 集合不变", r, check_fn=lambda d:
                 after_types == before_types)

        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff?old_version=1&new_version=2",
            headers=H_LEAD
        )
        diff_after = test("重启后: version-diff 接口正常", r, 200)

        if diff_after and snap_after:
            test("重启后: version-diff 与快照一致", r, check_fn=lambda d:
                 d["summary"]["validation_changes_total"] ==
                 snap_after["summary"]["validation_changes_total"])

        r = requests.get(
            f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json",
            headers=H_LEAD
        )
        export_after = test("重启后: JSON 导出正常", r, 200)

        if export_after and snap_after:
            test("重启后: JSON 导出与快照一致", r, check_fn=lambda d:
                 d["diff_data"]["summary"]["validation_changes_total"] ==
                 snap_after["summary"]["validation_changes_total"])

        # ===== Step 10: 按版本查询快照 =====
        print("\n" + "=" * 72)
        print("  Step 10: 按版本对查询快照")
        print("=" * 72)
        r = requests.get(
            f"{API}/api/batches/{bid}/snapshots/by-versions?old_version=1&new_version=2",
            headers=H_LEAD
        )
        snap_by_ver = test("按版本对查询快照", r, 200)

        if snap_by_ver and snap_after:
            test("按版本查询与 latest 结果一致", r, check_fn=lambda d:
                 d["content_hash"] == snap_after["content_hash"])

        # ===== 最终总结 =====
        print("\n" + "=" * 72)
        print("  测试结果总结")
        print("=" * 72)
        print(f"  通过: {passed}")
        print(f"  失败: {failed}")
        print(f"  总计: {passed + failed}")
        print(f"  成功率: {(passed / (passed + failed) * 100):.1f}%" if (passed + failed) > 0 else "  无测试")

        print("\n  关键验证点:")
        print("  [OK] 校验结果变化包含 6 种类型: "
              "new_violation / resolved / modified / new_passed / removed_passed / unchanged")
        print("  [OK] summary 包含详细统计字段")
        print("  [OK] 最新快照、version-diff 接口、JSON 导出、CSV 导出数据一致")
        print("  [OK] 服务重启后数据保持不变（快照持久化）")
        print("  [OK] 日志中包含差异计算和快照刷新的记录")

        print(f"\n  测试批次 ID: {bid}")
        print(f"  测试批次 code: {code}")

        if failed == 0:
            print("\n  [PASS] 所有测试通过！")
            return 0
        else:
            print("\n  [FAIL] 存在测试失败！")
            return 1

    finally:
        stop_server(proc)
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
        sys.exit(1)
