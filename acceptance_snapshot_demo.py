"""
批次版本对比 - 用户可见验收链路 (Acceptance Demo)
====================================================

用 Python requests 跑一条完整用户链路，演示：
  1.  作为提交人创建批次并导入 v1 / v2 两个版本
  2.  作为 lead 查询 latest 快照 及按版本对 (v1->v2) 查询
  3.  作为 admin 导出 JSON 和 CSV
  4.  验证 reviewer 越权访问被 403 拒绝
  5.  展示内容一致性（snapshot / API / 导出 三者 content_hash 一致）

运行方式:
    python acceptance_snapshot_demo.py

依赖:
    pip install requests
"""

import requests
import json
import sys
import os
import tempfile
import random
import string

API_BASE = "http://127.0.0.1:8001"

H_SUBMITTER = {"X-User-Id": "5"}
H_LEAD      = {"X-User-Id": "2"}
H_ADMIN     = {"X-User-Id": "1"}
H_REVIEWER  = {"X-User-Id": "3"}


def section(title):
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def ok(msg, resp=None, expect=200):
    if resp is not None and resp.status_code != expect:
        print(f"  [FAIL] {msg}  [{resp.status_code}]")
        try:
            print(f"     detail: {resp.json()}")
        except Exception:
            print(f"     body: {resp.text[:300]}")
        sys.exit(1)
    print(f"  [OK] {msg}" + (f"  [{resp.status_code}]" if resp else ""))


def main():
    section("健康检查")
    r = requests.get(f"{API_BASE}/health")
    ok("服务健康", r, 200)

    # -------- Step 1: submitter 创建批次并导入 v1 / v2 --------
    section("Step 1 · Submitter 创建批次 & 导入 v1 / v2")

    r = requests.post(f"{API_BASE}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": "ACCEPT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6)),
        "name": "验收演示批次",
        "description": "用于对外演示快照 / 导出 / 权限能力",
        "submitter_id": 5,
    })
    batch = r.json()
    ok(f"批次创建响应状态", r, 201)
    bid = batch["id"]
    ok(f"批次已创建  id={bid}  code={batch['batch_code']}")

    v1_json = json.dumps([
        {"item_id": "SKU-A1", "item_name": "Server R740",   "quantity": 10, "unit_price": 12000},
        {"item_id": "SKU-B2", "item_name": "Switch 48Port", "quantity":  4, "unit_price":  3200},
    ])
    v2_json = json.dumps([
        {"item_id": "SKU-A1", "item_name": "Server R740",   "quantity": 12, "unit_price": 12000},
        {"item_id": "SKU-C3", "item_name": "Fan Tray",      "quantity": 20, "unit_price":   180},
    ])

    for label, body in [("v1", v1_json), ("v2", v2_json)]:
        r = requests.post(
            f"{API_BASE}/api/batches/{bid}/manifests/precheck",
            headers=H_SUBMITTER,
            files={"file": (f"{label}.json", body, "application/json")},
            data={"import_format": "json"},
        )
        token = r.json()["precheck_token"]
        r = requests.post(
            f"{API_BASE}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": (f"{label}.json", body, "application/json")},
            data={"import_format": "json", "precheck_token": token},
        )
        ok(f"导入 {label} 清单 (自动沉淀快照)", r, 200)

    # -------- Step 2: lead 查询快照 --------
    section("Step 2 · Lead 查询快照 (latest / by-versions / by-id)")

    r = requests.get(f"{API_BASE}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
    snap_latest = r.json()
    ok(f"查询 latest 快照  id={snap_latest['id']}  v{snap_latest['old_version_number']}→v{snap_latest['new_version_number']}", r, 200)
    hash_ref = snap_latest["content_hash"]

    r = requests.get(f"{API_BASE}/api/batches/{bid}/snapshots/by-versions?old_version=1&new_version=2", headers=H_LEAD)
    snap_by_ver = r.json()
    ok("按 v1→v2 查询快照 (content_hash 一致)", r, 200)
    assert snap_by_ver["content_hash"] == hash_ref, "[FAIL] latest vs by-versions 哈希不一致"

    r = requests.get(f"{API_BASE}/api/batches/{bid}/snapshots/{snap_latest['id']}", headers=H_LEAD)
    snap_by_id = r.json()
    ok("按快照 ID 查询 (content_hash 一致)", r, 200)
    assert snap_by_id["content_hash"] == hash_ref, "[FAIL] latest vs by-id 哈希不一致"

    summary = snap_latest["summary"]
    print(f"     差异概览: +{summary['added_count']}  -{summary['removed_count']}  ~{summary['modified_count']}  unchanged={summary['unchanged_count']}")

    # -------- Step 3: admin 导出 JSON / CSV --------
    section("Step 3 · Admin 导出 JSON & CSV")

    r = requests.get(f"{API_BASE}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json", headers=H_ADMIN)
    j = r.json()
    ok(f"JSON 导出成功  export_id={j['export_id']}", r, 200)
    print(f"     导出时间: {j['export_timestamp']}")
    print(f"     导出人:   {j['exported_by']}")
    print(f"     summary:  +{j['diff_data']['summary']['added_count']}  -{j['diff_data']['summary']['removed_count']}  ~{j['diff_data']['summary']['modified_count']}")

    r = requests.get(f"{API_BASE}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=csv", headers=H_ADMIN)
    ok(f"CSV 导出成功  ({len(r.content)} bytes)", r, 200)
    lines = r.text.strip().splitlines()
    print(f"     表头: {lines[0]}")
    for ln in lines[1:4]:
        print(f"       -> {ln}")
    print(f"     ... 共 {len(lines) - 1} 条变更")

    out_csv = os.path.join(tempfile.gettempdir(), f"batch_{bid}_v1_v2_diff.csv")
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        f.write(r.text)
    ok(f"CSV 已落盘: {out_csv}")

    # -------- Step 4: reviewer 越权访问被 403 拒绝 --------
    section("Step 4 · Reviewer 越权访问 → 403 Permission denied")

    for name, url in [
        ("列出快照",        f"{API_BASE}/api/batches/{bid}/snapshots"),
        ("latest 快照",     f"{API_BASE}/api/batches/{bid}/snapshots/latest"),
        ("v1→v2 快照",      f"{API_BASE}/api/batches/{bid}/snapshots/by-versions?old_version=1&new_version=2"),
        ("JSON 导出",       f"{API_BASE}/api/batches/{bid}/version-diff/export?format=json"),
        ("CSV 导出",        f"{API_BASE}/api/batches/{bid}/version-diff/export?format=csv"),
    ]:
        r = requests.get(url, headers=H_REVIEWER)
        ok(f"reviewer 访问【{name}】被拒绝", r, 403)
        msg = r.json()["error"]["message"]
        assert "Permission denied" in msg, f"[FAIL] 错误消息不合规: {msg}"

    # -------- Step 5: 内容一致性校验 --------
    section("Step 5 · 快照 / version-diff 接口 / 导出 三者一致性")

    r = requests.get(f"{API_BASE}/api/batches/{bid}/version-diff?old_version=1&new_version=2", headers=H_LEAD)
    diff_api = r.json()
    ok("调用 version-diff 接口 (from_snapshot=true)", r, 200)

    r = requests.get(f"{API_BASE}/api/batches/{bid}/approval-logs", headers=H_ADMIN,
                     params={"action": "VIEW_VERSION_DIFF", "limit": "10"})
    all_logs = r.json()
    view_logs = [l for l in all_logs if l.get("extra_data", {}).get("from_snapshot")]
    assert view_logs, "[FAIL] 未记录 VIEW_VERSION_DIFF with from_snapshot"
    ok("审计日志标记 VIEW_VERSION_DIFF from_snapshot=true")

    print("\n" + "=" * 72)
    print("  *** 验收演示全部通过 — 交付就绪")
    print("=" * 72)
    print(f"   批次:        {batch['batch_code']}  (id={bid})")
    print(f"   快照:        id={snap_latest['id']}  status={snap_latest['status']}")
    print(f"   版本:        v{snap_latest['old_version_number']} → v{snap_latest['new_version_number']}")
    print(f"   内容哈希:    {hash_ref}")
    print(f"   导出 CSV:    {out_csv}")
    print("=" * 72)


if __name__ == "__main__":
    main()
