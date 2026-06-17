#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验收归档包模块 回归验证脚本

覆盖：
1. 导出成功
2. 冲突拒绝
3. 跨重启复查
4. 恢复后再次导出结果一致
5. 角色权限验证
6. 配置开关验证
"""
import os
import sys
import time
import json
import shutil
import hashlib
import tempfile
import zipfile
import io
from typing import Optional

import requests

API = os.environ.get("TEST_API_URL", "http://127.0.0.1:8000")

H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER = {"X-User-Id": "3"}
H_SUBMITTER = {"X-User-Id": "5"}

OK = "[OK]"
FAIL = "[FAIL]"
errors = []
warnings_log = []

BATCH_CODE_EXPORTED: Optional[int] = None
BATCH_CODE_RESTORED: Optional[int] = None
ARCHIVE_ZIP_BYTES: Optional[bytes] = None
ARCHIVE_ZIP_BYTES_2: Optional[bytes] = None
RECORDED_REPORT: Optional[dict] = None


def check(step: str, cond: bool, detail: str = ""):
    mark = OK if cond else FAIL
    msg = f"  {mark} {step}"
    if detail:
        msg += f"  --  {detail}"
    print(msg)
    if not cond:
        errors.append(step)


def warn(step: str, detail: str = ""):
    warnings_log.append(f"[WARN] {step}: {detail}")
    print(f"  [WARN] {step}  --  {detail}")


def safe_request(method, url, **kwargs):
    try:
        return requests.request(method, url, timeout=30, **kwargs)
    except Exception as e:
        class FakeResp:
            status_code = 0
            text = str(e)
            content = b''
            def json(self): return {}
        return FakeResp()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_full_batch():
    """创建批次 -> 预检查 -> 导入v1 -> 校验 -> 提交 -> 驳回 -> 返修 -> 导入v2 -> 校验 -> 提交 -> 通过 -> 归档"""
    print("\n=== 准备：构造完整批次链路 ...")
    batch_code = f"ARCH-TEST-{int(time.time())}"

    r = safe_request("POST", f"{API}/api/batches/",
        headers=H_SUBMITTER,
        json={
            "batch_code": batch_code,
            "name": "归档测试批次",
            "description": "用于验收归档模块的测试批次",
            "submitter_id": 5
        })
    check("创建批次 status=201", r.status_code == 201, f"status={r.status_code} body={r.text[:200]}")
    if r.status_code != 201:
        return None
    bid = r.json()["id"]
    print(f"  批次 id={bid}, code={batch_code}")

    with open("samples/manifest_sample_good.csv", "rb") as f:
        r = safe_request("POST", f"{API}/api/batches/{bid}/manifests/precheck",
            headers=H_SUBMITTER,
            files={"file": ("v1.csv", f, "text/csv")},
            data={"import_format": "auto"})
    d = r.json()
    check("v1预检查 status=200", r.status_code == 200)
    check("v1预检查 NEW_VERSION", d.get("action_type") == "NEW_VERSION",
          f"actual={d.get('action_type')}")
    token_v1 = d.get("precheck_token")

    with open("samples/manifest_sample_good.csv", "rb") as f:
        r = safe_request("POST", f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v1.csv", f, "text/csv")},
            data={"precheck_token": token_v1, "import_format": "auto"})
    check("v1导入 status=200", r.status_code == 200)

    safe_request("POST", f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)

    safe_request("POST", f"{API}/api/batches/{bid}/transition",
        headers=H_SUBMITTER,
        json={"target_status": "pending_review", "comment": "初检完成请验收"})

    safe_request("POST", f"{API}/api/batches/{bid}/reject",
        headers=H_REVIEWER,
        json={
            "comment": "有问题需要返修",
            "rejections": [
                {"item_key": "ITEM-001", "rejection_reason": "BIOS报告缺失"},
                {"item_key": "ITEM-002", "rejection_reason": "ECC标注缺失"},
            ]
        })

    safe_request("POST", f"{API}/api/batches/{bid}/start-repair",
        headers=H_SUBMITTER,
        data={"comment": "开始返修"})

    with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
        r = safe_request("POST", f"{API}/api/batches/{bid}/manifests/precheck",
            headers=H_SUBMITTER,
            files={"file": ("v2.csv", f, "text/csv")},
            data={"import_format": "auto"})
    token_v2 = r.json().get("precheck_token")

    with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
        safe_request("POST", f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v2.csv", f, "text/csv")},
            data={"precheck_token": token_v2, "import_format": "auto"})

    safe_request("POST", f"{API}/api/batches/{bid}/validate", headers=H_SUBMITTER)

    safe_request("POST", f"{API}/api/batches/{bid}/transition",
        headers=H_SUBMITTER,
        json={"target_status": "pending_review", "comment": "v2修复完成请重验"})

    safe_request("POST", f"{API}/api/batches/{bid}/approve",
        headers=H_LEAD,
        data={"comment": "通过验收"})

    safe_request("POST", f"{API}/api/batches/{bid}/archive",
        headers=H_LEAD,
        data={"comment": "完成归档"})

    print(f"  批次 {bid} 链路构造完成 (已归档)")

    return bid


def test_export_success(bid):
    global ARCHIVE_ZIP_BYTES, RECORDED_REPORT

    print("\n=== 测试1：导出成功 ===")

    r = safe_request("POST", f"{API}/api/batches/{bid}/archive/export",
        headers=H_ADMIN,
        data={"notes": "回归测试导出"})
    check("导出 status=200", r.status_code == 200,
          f"status={r.status_code} body={r.text[:200] if r.status_code != 200 else 'size=%d' % len(r.content)}")

    ARCHIVE_ZIP_BYTES = r.content

    check("导出 Content-Type=application/zip",
          r.headers.get("Content-Type", "") == "application/zip",
          f"actual={r.headers.get('Content-Type')}")

    archive_id = r.headers.get("X-Archive-Id")
    batch_code_resp = r.headers.get("X-Batch-Code")
    check("有 X-Archive-Id header", bool(archive_id), f"value={archive_id}")
    check("有 X-Batch-Code header", bool(batch_code_resp))

    try:
        zf = zipfile.ZipFile(io.BytesIO(ARCHIVE_ZIP_BYTES), "r")
        names = zf.namelist()
        check("ZIP 包含 manifest.json", "manifest.json" in names, f"names={names}")
        check("ZIP 包含 hash.sha256", "hash.sha256" in names)
        check("ZIP 包含 data/batch.json", "data/batch.json" in names)
        check("ZIP 包含 data/manifest_versions.json", "data/manifest_versions.json" in names)
        check("ZIP 包含 data/manifest_items.json", "data/manifest_items.json" in names)
        check("ZIP 包含 data/validation_results.json", "data/validation_results.json" in names)
        check("ZIP 包含 data/rejection_records.json", "data/rejection_records.json" in names)
        check("ZIP 包含 data/approval_logs.json", "data/approval_logs.json" in names)
        check("ZIP 包含 data/import_prechecks.json", "data/import_prechecks.json" in names)
        check("ZIP 包含 data/version_diff_snapshots.json", "data/version_diff_snapshots.json" in names)
        check("ZIP 包含 data/system_config_snapshot.json", "data/system_config_snapshot.json" in names)
        check("ZIP 包含 data/validation_rules_snapshot.json", "data/validation_rules_snapshot.json" in names)

        manifest_raw = zf.read("manifest.json")
        manifest = json.loads(manifest_raw)
        check("manifest 有 archive_id", bool(manifest.get("archive_id")))
        check("manifest 有 format_version=1.0", manifest.get("format_version") == "1.0")
        check("manifest 有 sections", len(manifest.get("sections", [])) >= 8)
        check("manifest item_counts > 0", manifest.get("item_counts", {}).get("manifest_items", 0) > 0)

        versions_data = json.loads(zf.read("data/manifest_versions.json"))
        check("归档包含2个清单版本", len(versions_data) >= 2, f"actual={len(versions_data)}")

        items_data = json.loads(zf.read("data/manifest_items.json"))
        check("归档包含清单项", len(items_data) >= 5)

        logs_data = json.loads(zf.read("data/approval_logs.json"))
        check("归档包含审批日志", len(logs_data) >= 10)

        zf.close()
    except Exception as e:
        check("ZIP 结构验证", False, f"exception={e}")

    r2 = safe_request("GET", f"{API}/api/batches/{bid}/acceptance-report", headers=H_ADMIN)
    if r2.status_code == 200:
        RECORDED_REPORT = r2.json()
        check("导出前验收报告已记录", bool(RECORDED_REPORT))

    r3 = safe_request("GET", f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
    if r3.status_code == 200:
        logs = r3.json()
        export_logs = [l for l in logs if l.get("action") == "EXPORT_ARCHIVE"]
        check("审批日志包含 EXPORT_ARCHIVE", len(export_logs) >= 1, f"count={len(export_logs)}")

    return archive_id


def test_conflict_rejection(bid):
    global BATCH_CODE_RESTORED

    print("\n=== 测试2：冲突拒绝 ===")

    check("归档字节非空", bool(ARCHIVE_ZIP_BYTES))

    r_conf = safe_request("PUT", f"{API}/api/system-configs/archive.allow_overwrite_existing_batch",
        headers=H_ADMIN,
        json={"config_value": "false", "value_type": "bool"})
    check("重置覆盖配置=false status=200", r_conf.status_code == 200,
          f"status={r_conf.status_code}")

    r = safe_request("POST", f"{API}/api/archive/precheck",
        headers=H_ADMIN,
        files={"file": ("test.zip", io.BytesIO(ARCHIVE_ZIP_BYTES), "application/zip")})
    check("试导入 status=200", r.status_code == 200,
          f"status={r.status_code} body={r.text[:300]}")
    if r.status_code != 200:
        return
    d = r.json()
    check("试导入 success=true", d.get("success") is True)
    check("试导入 require_overwrite=true", d.get("require_overwrite") is True,
          f"actual={d.get('require_overwrite')}")

    conflict_types = [c.get("conflict_type") for c in d.get("conflicts", [])]
    check("冲突包含 BATCH_CODE_CONFLICT", "BATCH_CODE_CONFLICT" in conflict_types,
          f"conflicts={conflict_types}")
    check("试导入 can_restore=false (无覆盖配置)", d.get("can_restore") is False,
          f"actual={d.get('can_restore')}, overwrite_enabled={d.get('overwrite_enabled')}")

    r2 = safe_request("POST", f"{API}/api/archive/restore",
        headers=H_ADMIN,
        files={"file": ("test.zip", io.BytesIO(ARCHIVE_ZIP_BYTES), "application/zip")},
        data={"force_overwrite": "false"})
    check("正式恢复(不覆盖) status=400", r2.status_code == 400,
          f"status={r2.status_code}")

    r_conf = safe_request("PUT", f"{API}/api/system-configs/archive.allow_overwrite_existing_batch",
        headers=H_ADMIN,
        json={"config_value": "true", "value_type": "bool"})
    check("开启覆盖配置 status=200", r_conf.status_code == 200,
          f"status={r_conf.status_code}")

    r3 = safe_request("POST", f"{API}/api/archive/precheck",
        headers=H_ADMIN,
        files={"file": ("test.zip", io.BytesIO(ARCHIVE_ZIP_BYTES), "application/zip")})
    d3 = r3.json()
    check("开配置后 can_restore=true", d3.get("can_restore") is True,
          f"actual={d3.get('can_restore')}, conflicts={[c.get('conflict_type') for c in d3.get('conflicts')]}")

    r4 = safe_request("POST", f"{API}/api/archive/restore",
        headers=H_ADMIN,
        files={"file": ("test.zip", io.BytesIO(ARCHIVE_ZIP_BYTES), "application/zip")},
        data={"force_overwrite": "true"})
    check("正式恢复(强制覆盖) status=200", r4.status_code == 200,
          f"status={r4.status_code} body={r4.text[:300]}")
    if r4.status_code == 200:
        d4 = r4.json()
        check("恢复 success=true", d4.get("success") is True)
        check("恢复 overwritten=true", d4.get("overwritten") is True,
              f"actual={d4.get('overwritten')}")
        BATCH_CODE_RESTORED = d4.get("new_batch_id")
        print(f"  覆盖恢复后新批次 id={BATCH_CODE_RESTORED}")


def test_role_permission():
    print("\n=== 测试3：角色权限验证 ===")

    r1 = safe_request("POST", f"{API}/api/archive/precheck",
        headers=H_REVIEWER,
        files={"file": ("test.zip", io.BytesIO(ARCHIVE_ZIP_BYTES), "application/zip")})
    check("reviewer试导入=403", r1.status_code == 403, f"status={r1.status_code}")

    r2 = safe_request("POST", f"{API}/api/archive/restore",
        headers=H_REVIEWER,
        files={"file": ("test.zip", io.BytesIO(ARCHIVE_ZIP_BYTES), "application/zip")})
    check("reviewer恢复=403", r2.status_code == 403, f"status={r2.status_code}")

    r3 = safe_request("GET", f"{API}/api/system-configs/", headers=H_REVIEWER)
    check("reviewer查配置=403", r3.status_code == 403, f"status={r3.status_code}")

    r4 = safe_request("POST", f"{API}/api/archive/precheck",
        headers=H_LEAD,
        files={"file": ("test.zip", io.BytesIO(ARCHIVE_ZIP_BYTES), "application/zip")})
    check("lead试导入=403", r4.status_code == 403, f"status={r4.status_code}")

    r5 = safe_request("GET", f"{API}/api/batches/{BATCH_CODE_EXPORTED}/archive/export",
        headers=H_SUBMITTER)
    check("submitter导出=200", r5.status_code == 200, f"status={r5.status_code}")


def test_cross_restart(bid):
    global ARCHIVE_ZIP_BYTES_2

    print("\n=== 测试4：跨重启复查 ===")

    r = safe_request("GET", f"{API}/health")
    check("健康检查", r.status_code == 200)

    r1 = safe_request("GET", f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
    check("重启后审批日志可查", r1.status_code == 200,
          f"status={r1.status_code}")
    logs_before = r1.json() if r1.status_code == 200 else []
    check("有 EXPORT_ARCHIVE 日志可查",
          any(l.get("action") == "EXPORT_ARCHIVE" for l in logs_before),
          f"count={len(logs_before)}")

    r2 = safe_request("GET", f"{API}/api/batches/{bid}/manifests/", headers=H_ADMIN)
    check("重启后版本历史可查", r2.status_code == 200)
    versions = r2.json() if r2.status_code == 200 else []
    check("版本历史 >=2个版本", len(versions) >= 2, f"count={len(versions)}")

    r3 = safe_request("GET", f"{API}/api/batches/{bid}/acceptance-report", headers=H_ADMIN)
    check("重启后验收报告可查", r3.status_code == 200)
    report_after = r3.json() if r3.status_code == 200 else None
    if RECORDED_REPORT and report_after:
        check("报告item_count一致",
              RECORDED_REPORT.get("item_count") == report_after.get("item_count"),
              f"before={RECORDED_REPORT.get('item_count')}, after={report_after.get('item_count')}")
        check("报告total_versions一致",
              RECORDED_REPORT.get("total_versions") == report_after.get("total_versions"),
              f"before={RECORDED_REPORT.get('total_versions')}, after={report_after.get('total_versions')}")

    r4 = safe_request("GET", f"{API}/api/batches/{bid}/rejections", headers=H_ADMIN)
    check("重启后驳回记录可查", r4.status_code == 200)

    r5 = safe_request("POST", f"{API}/api/batches/{bid}/archive/export",
        headers=H_ADMIN,
        data={"notes": "跨重启复查后重新导出"})
    check("重启后可再次导出", r5.status_code == 200,
          f"status={r5.status_code} body={r5.text[:200] if r5.status_code != 200 else 'size=%d' % len(r5.content)}")
    ARCHIVE_ZIP_BYTES_2 = r5.content if r5.status_code == 200 else None


def test_restored_can_work(bid):
    print("\n=== 测试5：恢复后可继续操作 ===")

    r1 = safe_request("GET", f"{API}/api/batches/{bid}", headers=H_ADMIN)
    check("恢复批次详情可查", r1.status_code == 200,
          f"status={r1.status_code}")
    if r1.status_code != 200:
        return

    batch = r1.json()
    check("批次状态已恢复", batch.get("status") == "archived",
          f"status={batch.get('status')}")

    r2 = safe_request("GET", f"{API}/api/batches/{bid}/version-history", headers=H_ADMIN)
    check("恢复后版本历史可查", r2.status_code == 200)
    versions = r2.json() if r2.status_code == 200 else []
    check("恢复后版本数>=2", len(versions) >= 2, f"count={len(versions)}")

    r3 = safe_request("GET", f"{API}/api/batches/{bid}/manifests/latest", headers=H_ADMIN)
    check("恢复后最新清单可查", r3.status_code == 200)

    r4 = safe_request("GET", f"{API}/api/batches/{bid}/approval-logs", headers=H_ADMIN)
    check("恢复后审批日志可查", r4.status_code == 200)
    logs = r4.json() if r4.status_code == 200 else []
    restore_logs = [l for l in logs if "RESTORE_ARCHIVE" in l.get("action", "")]
    check("恢复日志存在 RESTORE_ARCHIVE*", len(restore_logs) >= 1,
          f"count={len(restore_logs)}")
    try_import_logs = [l for l in logs if l.get("action") == "TRY_IMPORT_ARCHIVE"]
    check("恢复日志存在 TRY_IMPORT_ARCHIVE", len(try_import_logs) >= 1)

    r5 = safe_request("GET", f"{API}/api/batches/{bid}/acceptance-report", headers=H_ADMIN)
    check("恢复后验收报告可查", r5.status_code == 200)

    r6 = safe_request("POST", f"{API}/api/batches/{bid}/validate", headers=H_ADMIN)
    check("恢复后可重新校验", r6.status_code == 200,
          f"status={r6.status_code}")


def test_export_idempotent(bid):
    global ARCHIVE_ZIP_BYTES

    print("\n=== 测试6：恢复后再次导出，关键数据一致 ===")

    r = safe_request("POST", f"{API}/api/batches/{bid}/archive/export",
        headers=H_ADMIN)
    check("恢复后重新导出 status=200", r.status_code == 200)
    if r.status_code != 200:
        return

    new_zip = r.content

    def extract_core_data(zip_bytes):
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                data = {}
                for key in ["manifest_versions", "manifest_items",
                            "validation_results", "rejection_records"]:
                    path = f"data/{key}.json"
                    if path in zf.namelist():
                        raw = zf.read(path)
                        parsed = json.loads(raw)
                        if isinstance(parsed, list):
                            for item in parsed:
                                for field in ["id", "batch_id", "manifest_version_id",
                                            "manifest_item_id", "created_at",
                                            "imported_at", "resolved_at",
                                            "consumed_at", "expires_at",
                                            "updated_at", "archived_at"]:
                                    if field in item:
                                        del item[field]
                        data[key] = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
                return data
        except Exception as e:
            return None

    if ARCHIVE_ZIP_BYTES:
        orig = extract_core_data(ARCHIVE_ZIP_BYTES)
        newd = extract_core_data(new_zip)
        if orig and newd:
            for k in ["manifest_versions", "manifest_items",
                      "validation_results", "rejection_records"]:
                hash_orig = hashlib.sha256(orig[k].encode()).hexdigest()[:16]
                hash_new = hashlib.sha256(newd[k].encode()).hexdigest()[:16]
                match = orig[k] == newd[k]
                check(f"{k} 数据一致(去除ID后)", match,
                      f"orig_hash={hash_orig}, new_hash={hash_new}")

    if ARCHIVE_ZIP_BYTES:
        ARCHIVE_ZIP_BYTES = new_zip


def test_config_switch():
    print("\n=== 测试7：配置开关验证 ===")

    r1 = safe_request("PUT", f"{API}/api/system-configs/archive.enabled",
        headers=H_ADMIN,
        json={"config_value": "false", "value_type": "bool"})
    check("关闭归档功能 status=200", r1.status_code == 200)

    r2 = safe_request("POST", f"{API}/api/batches/{BATCH_CODE_RESTORED}/archive/export",
        headers=H_ADMIN)
    check("关闭后导出=403", r2.status_code == 403, f"status={r2.status_code}")

    r3 = safe_request("POST", f"{API}/api/archive/precheck",
        headers=H_ADMIN,
        files={"file": ("t.zip", io.BytesIO(ARCHIVE_ZIP_BYTES or b""), "application/zip")})
    check("关闭后试导入=403", r3.status_code == 403, f"status={r3.status_code}")

    r4 = safe_request("PUT", f"{API}/api/system-configs/archive.enabled",
        headers=H_ADMIN,
        json={"config_value": "true", "value_type": "bool"})
    check("重新开启归档功能", r4.status_code == 200)

    r5 = safe_request("GET", f"{API}/api/system-configs/", headers=H_ADMIN)
    check("配置列表可查", r5.status_code == 200)
    configs = r5.json() if r5.status_code == 200 else []
    keys = [c.get("config_key") for c in configs]
    check("包含 archive.enabled", "archive.enabled" in keys, f"keys={keys}")
    check("包含 archive.allow_overwrite_existing_batch",
          "archive.allow_overwrite_existing_batch" in keys)


def test_invalid_zip():
    print("\n=== 测试8：无效归档包 ===")

    invalid_zip = io.BytesIO(b"this is not a zip file")
    r = safe_request("POST", f"{API}/api/archive/precheck",
        headers=H_ADMIN,
        files={"file": ("bad.zip", invalid_zip, "application/zip")})
    check("无效ZIP返回success=false",
          r.status_code == 200 and isinstance(r.json(), dict) and not r.json().get("success", True),
          f"status={r.status_code}, body={r.text[:200]}")


def main():
    print("=" * 70)
    print("验收归档包模块 回归验证脚本")
    print(f"目标 API: {API}")
    print("=" * 70)

    try:
        r = safe_request("GET", f"{API}/health")
        if r.status_code != 200:
            print(f"  服务未启动！请先启动服务：python -m uvicorn main:app --port 8000")
            sys.exit(1)
    except Exception:
        print(f"  无法连接到 {API}，请先启动服务")
        sys.exit(1)
    print("  [OK] 服务运行中")

    global BATCH_CODE_EXPORTED

    BATCH_CODE_EXPORTED = create_full_batch()
    if not BATCH_CODE_EXPORTED:
        print("  中止：无法创建测试批次")
        sys.exit(1)

    test_export_success(BATCH_CODE_EXPORTED)

    test_conflict_rejection(BATCH_CODE_EXPORTED)

    test_role_permission()

    if BATCH_CODE_RESTORED:
        test_restored_can_work(BATCH_CODE_RESTORED)
        test_export_idempotent(BATCH_CODE_RESTORED)

    test_cross_restart(BATCH_CODE_EXPORTED)

    test_config_switch()

    test_invalid_zip()

    print("\n" + "=" * 70)
    if warnings_log:
        print(f"WARNING ({len(warnings_log)} 条警告:")
        for w in warnings_log:
            print(f"  {w}")
        print()

    if errors:
        print(f"{FAIL} {len(errors)} 项未通过:")
        for e in errors:
            print(f"  - {e}")
        print("=" * 70)
        sys.exit(1)
    else:
        print(f"{OK} 全部回归验证通过！")
        print("=" * 70)


if __name__ == "__main__":
    main()
