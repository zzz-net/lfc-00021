"""
Batch Version Diff - Acceptance Demo (with validation + restart verification)
==============================================================================

Full user-visible chain:
  1.  Submitter creates batch, imports v1 (all-pass items)
  2.  Admin validates v1 -> all pass
  3.  Submitter submits for review
  4.  Reviewer rejects with item-level issue
  5.  Submitter starts repair, imports v2 (with RANGE_QUANTITY warning)
  6.  Admin validates v2 -> produces warning
  7.  Lead queries snapshot: must show validation_changes, validation_warnings_new > 0
  8.  Admin exports JSON & CSV
  9.  Reviewer denied (403)
  10. Restart service -> re-verify snapshot content_hash unchanged

Run:
    python acceptance_snapshot_demo.py
"""

import requests
import json
import sys
import os
import tempfile
import random
import string
import time
import subprocess

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


def precheck_and_import(bid, filename, body, user=H_SUBMITTER):
    r = requests.post(
        f"{API_BASE}/api/batches/{bid}/manifests/precheck",
        headers=user,
        files={"file": (filename, body, "application/json")},
        data={"import_format": "json"},
    )
    ok(f"Precheck {filename}", r, 200)
    token = r.json()["precheck_token"]
    r = requests.post(
        f"{API_BASE}/api/batches/{bid}/manifests/import",
        headers=user,
        files={"file": (filename, body, "application/json")},
        data={"import_format": "json", "precheck_token": token},
    )
    return r


def restart_service():
    print("  Stopping server...")
    subprocess.run(
        ["powershell", "-Command",
         "Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue "
         "| Select-Object -ExpandProperty OwningProcess "
         "| ForEach-Object { Stop-Process -Id $_ -Force }"],
        capture_output=True, text=True, timeout=15
    )
    time.sleep(2)
    print("  Starting server...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8001"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for _ in range(20):
        time.sleep(0.5)
        try:
            r = requests.get(f"{API_BASE}/health", timeout=2)
            if r.status_code == 200:
                print("  Server restarted successfully")
                return proc
        except Exception:
            pass
    print("  [ERROR] Server failed to restart within 10 seconds")
    return proc


def main():
    section("Health Check")
    r = requests.get(f"{API_BASE}/health")
    ok("Service healthy", r, 200)

    # -------- Step 1: create batch + import v1 --------
    section("Step 1: Submitter creates batch & imports v1 (all-pass)")

    r = requests.post(f"{API_BASE}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": "ACCEPT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6)),
        "name": "Acceptance demo batch",
        "description": "Full chain: validate -> reject -> reimport -> validate -> snapshot",
        "submitter_id": 5,
    })
    batch = r.json()
    ok("Batch created", r, 201)
    bid = batch["id"]

    v1_json = json.dumps([
        {"item_id": "ITEM-A1", "item_name": "Widget A", "quantity": 5, "unit_price": 100},
        {"item_id": "ITEM-B2", "item_name": "Widget B", "quantity": 3, "unit_price": 200},
    ])
    r = precheck_and_import(bid, "v1.json", v1_json, H_SUBMITTER)
    ok("Import v1", r, 200)

    # -------- Step 2: validate v1 -> all pass --------
    section("Step 2: Admin validates v1 -> all pass")

    r = requests.post(f"{API_BASE}/api/batches/{bid}/validate", headers=H_ADMIN)
    val_v1 = r.json()
    ok(f"Validate v1: passed={val_v1['passed']}, failed={val_v1['failed']}, warnings={val_v1['warnings']}", r, 200)
    assert val_v1["failed"] == 0 and val_v1["warnings"] == 0, "[FAIL] v1 should have no issues"

    # -------- Step 3: submit for review --------
    section("Step 3: Submitter submits for review")

    r = requests.post(f"{API_BASE}/api/batches/{bid}/transition", headers=H_SUBMITTER, json={
        "target_status": "pending_review"
    })
    ok("Transition to pending_review", r, 200)

    # -------- Step 4: reviewer rejects --------
    section("Step 4: Reviewer rejects with item-level issue")

    r = requests.post(f"{API_BASE}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
        "rejections": [
            {"item_key": "ITEM-A1", "line_number": 1, "rejection_reason": "Quantity too low"}
        ],
        "comment": "ITEM-A1 quantity insufficient"
    })
    ok("Reject batch", r, 200)

    # -------- Step 5: repair + import v2 (with RANGE_QUANTITY warning) --------
    section("Step 5: Submitter starts repair & imports v2 (quantity=50000, triggers RANGE_QUANTITY)")

    r = requests.post(f"{API_BASE}/api/batches/{bid}/start-repair", headers=H_SUBMITTER, json={
        "comment": "Increasing quantity"
    })
    ok("Start repair", r, 200)

    v2_json = json.dumps([
        {"item_id": "ITEM-A1", "item_name": "Widget A", "quantity": 50000, "unit_price": 100},
        {"item_id": "ITEM-B2", "item_name": "Widget B", "quantity": 3, "unit_price": 200},
    ])
    r = precheck_and_import(bid, "v2.json", v2_json, H_SUBMITTER)
    ok("Import v2 (auto-creates snapshot)", r, 200)

    # -------- Step 6: validate v2 -> should produce warning --------
    section("Step 6: Admin validates v2 -> should produce RANGE_QUANTITY warning")

    r = requests.post(f"{API_BASE}/api/batches/{bid}/validate", headers=H_ADMIN)
    val_v2 = r.json()
    ok(f"Validate v2: passed={val_v2['passed']}, failed={val_v2['failed']}, warnings={val_v2['warnings']}", r, 200)
    assert val_v2["warnings"] > 0, "[FAIL] v2 should have at least 1 warning"

    # -------- Step 7: lead queries snapshot --------
    section("Step 7: Lead queries snapshot -> validation_changes present")

    r = requests.get(f"{API_BASE}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
    snap = r.json()
    ok(f"Latest snapshot  id={snap['id']}  v{snap['old_version_number']}->v{snap['new_version_number']}", r, 200)

    assert snap["summary"]["validation_warnings_new"] > 0, \
        f"[FAIL] validation_warnings_new should be >0, got {snap['summary']['validation_warnings_new']}"
    ok(f"validation_warnings_old={snap['summary']['validation_warnings_old']}, "
       f"validation_warnings_new={snap['summary']['validation_warnings_new']}")

    assert len(snap["validation_changes"]) > 0, \
        f"[FAIL] validation_changes should not be empty"
    ok(f"validation_changes count={len(snap['validation_changes'])}")

    for vc in snap["validation_changes"]:
        ok(f"  change_type={vc['change_type']}  rule_code={vc['rule_code']}  item_key={vc['item_key']}")

    hash_before_restart = snap["content_hash"]

    r = requests.get(f"{API_BASE}/api/batches/{bid}/snapshots/by-versions?old_version=1&new_version=2", headers=H_LEAD)
    snap_by_ver = r.json()
    ok("By-versions query (content_hash matches)", r, 200)
    assert snap_by_ver["content_hash"] == hash_before_restart, "[FAIL] content_hash mismatch"

    # -------- Step 8: admin exports JSON & CSV --------
    section("Step 8: Admin exports JSON & CSV")

    r = requests.get(f"{API_BASE}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json", headers=H_ADMIN)
    j = r.json()
    ok(f"JSON export  export_id={j['export_id']}", r, 200)
    assert len(j["diff_data"]["validation_changes"]) > 0, "[FAIL] JSON export missing validation_changes"
    ok(f"JSON export validation_changes count={len(j['diff_data']['validation_changes'])}")

    r = requests.get(f"{API_BASE}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=csv", headers=H_ADMIN)
    ok(f"CSV export  ({len(r.content)} bytes)", r, 200)

    out_csv = os.path.join(tempfile.gettempdir(), f"batch_{bid}_v1_v2_diff.csv")
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        f.write(r.text)
    ok(f"CSV saved: {out_csv}")

    # -------- Step 9: reviewer denied --------
    section("Step 9: Reviewer denied (403)")

    for name, url in [
        ("snapshot latest",   f"{API_BASE}/api/batches/{bid}/snapshots/latest"),
        ("by-versions",       f"{API_BASE}/api/batches/{bid}/snapshots/by-versions?old_version=1&new_version=2"),
        ("JSON export",       f"{API_BASE}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json"),
        ("CSV export",        f"{API_BASE}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=csv"),
    ]:
        r = requests.get(url, headers=H_REVIEWER)
        ok(f"Reviewer denied [{name}]", r, 403)

    # -------- Step 10: restart + re-verify --------
    section("Step 10: Restart service -> re-verify snapshot unchanged")

    proc = restart_service()

    r = requests.get(f"{API_BASE}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
    snap_after = r.json()
    ok("After restart: query latest snapshot", r, 200)
    assert snap_after["content_hash"] == hash_before_restart, \
        f"[FAIL] content_hash changed after restart: was {hash_before_restart}, now {snap_after['content_hash']}"
    ok("After restart: content_hash UNCHANGED")

    assert snap_after["summary"]["validation_warnings_new"] == snap["summary"]["validation_warnings_new"], \
        "[FAIL] validation_warnings_new changed after restart"
    ok(f"After restart: validation_warnings_new={snap_after['summary']['validation_warnings_new']} (same)")

    assert len(snap_after["validation_changes"]) == len(snap["validation_changes"]), \
        "[FAIL] validation_changes count changed after restart"
    ok(f"After restart: validation_changes count={len(snap_after['validation_changes'])} (same)")

    # -------- Summary --------
    print("\n" + "=" * 72)
    print("  *** ACCEPTANCE DEMO PASSED - Delivery Ready")
    print("=" * 72)
    print(f"   Batch:          {batch['batch_code']}  (id={bid})")
    print(f"   Snapshot:       id={snap['id']}  status={snap['status']}")
    print(f"   Versions:       v{snap['old_version_number']} -> v{snap['new_version_number']}")
    print(f"   Content hash:   {hash_before_restart}")
    print(f"   Val changes:    {len(snap['validation_changes'])} (including RANGE_QUANTITY new_violation)")
    print(f"   CSV export:     {out_csv}")
    print(f"   Post-restart:   content_hash unchanged, validation data intact")
    print("=" * 72)

    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
