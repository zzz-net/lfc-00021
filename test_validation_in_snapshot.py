"""
Regression test for: validation changes missing in snapshot after validate
==========================================================================
Reproduces: v1 -> validate -> reject -> v2(with warning) -> validate -> snapshot should show validation_changes

Flow:
  1. Create batch, import v1
  2. Run /validate on v1 (v1 passes)
  3. Submit for review (draft -> pending_review)
  4. Reject with item-level rejection (pending_review -> partially_rejected)
  5. Start repair (partially_rejected -> repairing)
  6. Import v2 (with item causing RANGE_QUANTITY warning)
  7. Run /validate on v2 (should produce warning)
  8. Check snapshot: validation_changes should NOT be empty, validation_warnings_new > 0
  9. Restart service (simulate by stopping/starting uvicorn)
  10. Re-check snapshot: same results after restart
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
PID_FILE = os.path.join(os.path.dirname(__file__), ".test_validation_pid")

H_SUBMITTER = {"X-User-Id": "5"}
H_REVIEWER  = {"X-User-Id": "3"}
H_LEAD      = {"X-User-Id": "2"}
H_ADMIN     = {"X-User-Id": "1"}

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
    if check_fn:
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


def start_server():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8001"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    for _ in range(20):
        time.sleep(0.5)
        try:
            r = requests.get(f"{API}/health", timeout=2)
            if r.status_code == 200:
                return proc
        except Exception:
            pass
    print("  [ERROR] Server failed to start within 10 seconds")
    return proc


def stop_server(proc):
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    global passed, failed

    print("=" * 72)
    print("  Validation Changes in Snapshot - Regression Test")
    print("=" * 72)

    # -- Step 1: create batch + import v1 --
    print("\n--- Step 1: Create batch + import v1 ---")
    code = "VALTEST-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": code,
        "name": "Validation regression test",
        "description": "Test that validate -> reject -> v2 -> validate produces correct snapshot",
        "submitter_id": 5,
    })
    data = test("Create batch", r, 201)
    bid = data["id"]
    print(f"  batch_id={bid}")

    v1_json = json.dumps([
        {"item_id": "ITEM-A1", "item_name": "Widget A", "quantity": 5, "unit_price": 100},
        {"item_id": "ITEM-B2", "item_name": "Widget B", "quantity": 3, "unit_price": 200},
    ])
    r, _ = precheck_and_import(bid, "v1.json", v1_json, H_SUBMITTER)
    test("Import v1", r, 200)

    # -- Step 2: validate v1 --
    print("\n--- Step 2: Validate v1 ---")
    r = requests.post(f"{API}/api/batches/{bid}/validate", headers=H_ADMIN)
    val_data = test("Validate v1", r, 200, check_fn=lambda d: d.get("success") is True)
    print(f"  v1 validation: passed={val_data.get('passed', '?')}, failed={val_data.get('failed', '?')}, warnings={val_data.get('warnings', '?')}")
    v1_all_passed = val_data.get("failed", 0) == 0 and val_data.get("warnings", 0) == 0

    # -- Step 3: submit for review (draft -> pending_review) --
    print("\n--- Step 3: Submit for review ---")
    r = requests.post(f"{API}/api/batches/{bid}/transition", headers=H_SUBMITTER, json={
        "target_status": "pending_review"
    })
    test("Transition to pending_review", r, 200)

    # -- Step 4: reject (pending_review -> partially_rejected) --
    print("\n--- Step 4: Reject with item-level issue ---")
    r = requests.post(f"{API}/api/batches/{bid}/reject", headers=H_REVIEWER, json={
        "rejections": [
            {"item_key": "ITEM-A1", "line_number": 1, "rejection_reason": "Quantity too low, need at least 10"}
        ],
        "comment": "ITEM-A1 quantity insufficient"
    })
    test("Reject batch", r, 200)

    # -- Step 5: start repair (partially_rejected -> repairing) --
    print("\n--- Step 5: Start repair ---")
    r = requests.post(f"{API}/api/batches/{bid}/start-repair", headers=H_SUBMITTER, json={
        "comment": "Fixing quantity"
    })
    test("Start repair", r, 200)

    # -- Step 6: import v2 (with item causing RANGE_QUANTITY warning) --
    print("\n--- Step 6: Import v2 (with out-of-range quantity) ---")
    v2_json = json.dumps([
        {"item_id": "ITEM-A1", "item_name": "Widget A", "quantity": 50000, "unit_price": 100},
        {"item_id": "ITEM-B2", "item_name": "Widget B", "quantity": 3, "unit_price": 200},
    ])
    r, _ = precheck_and_import(bid, "v2.json", v2_json, H_SUBMITTER)
    test("Import v2", r, 200)

    # -- Step 7: validate v2 --
    print("\n--- Step 7: Validate v2 ---")
    r = requests.post(f"{API}/api/batches/{bid}/validate", headers=H_ADMIN)
    val_v2 = test("Validate v2", r, 200, check_fn=lambda d: d.get("success") is True)
    print(f"  v2 validation: passed={val_v2.get('passed', '?')}, failed={val_v2.get('failed', '?')}, warnings={val_v2.get('warnings', '?')}")

    # -- Step 8: check snapshot for validation_changes --
    print("\n--- Step 8: Check snapshot for validation_changes ---")

    r = requests.get(f"{API}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
    snap = test("Query latest snapshot", r, 200)
    if not snap:
        print("  [ERROR] Cannot get snapshot, aborting")
        sys.exit(1)

    test("snapshot has validation_warnings_new > 0", r,
         check_fn=lambda d: d["summary"]["validation_warnings_new"] > 0)

    test("snapshot validation_changes is not empty", r,
         check_fn=lambda d: len(d["validation_changes"]) > 0)

    has_new_violation = any(
        vc.get("change_type") == "new_violation"
        for vc in snap.get("validation_changes", [])
    )
    test("snapshot has new_violation type in validation_changes", r,
         check_fn=lambda d: has_new_violation)

    has_validation_data = (
        snap["summary"]["validation_warnings_new"] > 0
        or snap["summary"]["validation_errors_new"] > 0
        or snap["summary"]["validation_warnings_old"] > 0
        or snap["summary"]["validation_errors_old"] > 0
        or len(snap["validation_changes"]) > 0
    )
    if has_validation_data:
        print(f"  validation_warnings_old={snap['summary']['validation_warnings_old']}, validation_warnings_new={snap['summary']['validation_warnings_new']}")
        print(f"  validation_errors_old={snap['summary']['validation_errors_old']}, validation_errors_new={snap['summary']['validation_errors_new']}")
        print(f"  validation_changes count={len(snap['validation_changes'])}")
        for vc in snap["validation_changes"][:3]:
            print(f"    -> {vc.get('change_type')}: {vc.get('rule_code')} on {vc.get('item_key')}")

    # -- Step 8b: check version-diff also shows validation data --
    r = requests.get(f"{API}/api/batches/{bid}/version-diff?old_version=1&new_version=2", headers=H_LEAD)
    diff_data = test("version-diff shows validation_changes", r, 200,
                     check_fn=lambda d: len(d["validation_changes"]) > 0)

    # -- Step 8c: check JSON export shows validation data --
    r = requests.get(f"{API}/api/batches/{bid}/version-diff/export?old_version=1&new_version=2&format=json", headers=H_ADMIN)
    test("JSON export shows validation_changes", r, 200,
         check_fn=lambda d: len(d["diff_data"]["validation_changes"]) > 0)

    # -- Step 8d: check unresolved_rejections in snapshot --
    unresolved_count = snap["summary"].get("unresolved_rejections_old", 0) + snap["summary"].get("unresolved_rejections_new", 0)
    unresolved_list_count = len(snap.get("unresolved_rejections", []))
    if unresolved_count > 0 or unresolved_list_count > 0:
        print(f"  unresolved_rejections: old={snap['summary']['unresolved_rejections_old']}, new={snap['summary']['unresolved_rejections_new']}, list={unresolved_list_count}")
    else:
        print(f"  [INFO] No unresolved_rejections in snapshot (rejection was on v1, v2 has no rejections)")


    # -- Step 9: restart service and re-check --
    print("\n--- Step 9: Restart service and re-check ---")

    # Save the snapshot content_hash for comparison
    hash_before_restart = snap["content_hash"]
    summary_before = snap["summary"]
    val_changes_count_before = len(snap["validation_changes"])

    print("  Stopping server...")
    stop_server_proc = subprocess.run(
        ["powershell", "-Command",
         f"Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | ForEach-Object {{ Stop-Process -Id $_ -Force }}"],
        capture_output=True, text=True, timeout=15
    )
    time.sleep(2)

    print("  Starting server...")
    proc = start_server()
    time.sleep(1)

    r = requests.get(f"{API}/api/batches/{bid}/snapshots/latest", headers=H_LEAD)
    snap_after = test("After restart: query latest snapshot", r, 200)
    if snap_after:
        test("After restart: content_hash unchanged", r,
             check_fn=lambda d: d["content_hash"] == hash_before_restart)
        test("After restart: validation_warnings_new same", r,
             check_fn=lambda d: d["summary"]["validation_warnings_new"] == summary_before["validation_warnings_new"])
        test("After restart: validation_errors_new same", r,
             check_fn=lambda d: d["summary"]["validation_errors_new"] == summary_before["validation_errors_new"])
        test("After restart: validation_changes count same", r,
             check_fn=lambda d: len(d["validation_changes"]) == val_changes_count_before)

    # -- Final summary --
    print("\n" + "=" * 72)
    print(f"  Results: {passed} passed, {failed} failed, total {passed + failed}")
    if failed == 0:
        print("  [PASS] All tests passed!")
    else:
        print("  [FAIL] Some tests failed!")
    print("=" * 72)

    stop_server(proc)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
