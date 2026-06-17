"""
沙盒配置回归测试 —— 专门盯住规则分叉和边界条件

覆盖范围：
A. /api/system-configs (旧入口) vs /api/sandbox-config (新入口) 的规则分叉
B. reviewer / admin / lead 三种角色的读写边界
C. 旧入口拒改 sandbox.* 键
D. 新入口 expected_old_value 并发保护 —— 陈旧值写回 409 且不脏写
E. sandbox.require_admin_confirm 切换前后 eligibility 结果变化
F. reviewer 不能确认；lead 能力随配置切换
G. 重启后配置值 + 权限判断仍一致
"""

import os
import sys
import json
import time
import io
import zipfile
import requests
from typing import Optional, Dict, Any

API = os.environ.get("TEST_API_URL", "http://127.0.0.1:8003")

PERSIST_FILE = ".test_sandbox_config_regression_persistence.json"

H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER = {"X-User-Id": "3"}
H_SUBMITTER = {"X-User-Id": "5"}

SANDBOX_KEYS = [
    "sandbox.enabled",
    "sandbox.require_admin_confirm",
    "sandbox.auto_expire_hours",
]
OLD_ENTRY_ALLOWED_KEYS = [
    "archive.enabled",
    "archive.allow_overwrite_existing_batch",
]

TEST_SECTION = ""
_total = 0
_passed = 0
_failed = 0
_warnings = []


def safe_str(s):
    if isinstance(s, bytes):
        try:
            return s.decode("utf-8", errors="replace")
        except Exception:
            return str(s)
    return str(s).encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def section(name):
    global TEST_SECTION
    TEST_SECTION = name
    print(f"\n{'=' * 70}")
    print(f"【{name}】")
    print(f"{'=' * 70}")


def check(label, cond, detail=""):
    global _total, _passed, _failed
    _total += 1
    if cond:
        _passed += 1
        print(f"  [OK] {label}  --  {safe_str(detail) if detail else ''}")
    else:
        _failed += 1
        print(f"  [FAIL] {label}  --  {safe_str(detail) if detail else ''}")


def warn(msg):
    global _warnings
    _warnings.append(msg)
    print(f"  [WARN] {msg}")


def safe_request(method, url, **kwargs):
    try:
        return requests.request(method, url, timeout=20, **kwargs)
    except Exception as e:
        print(f"  [ERROR] request {method} {url} failed: {e}")
        class _R:
            status_code = 999
            text = str(e)

            def json(self):
                return {}
        return _R()


def wait_for_api():
    print(f"\n等待 API 服务启动 ({API}) ...")
    for i in range(40):
        try:
            r = requests.get(f"{API}/", timeout=3)
            if r.status_code < 500:
                print(f"  API 服务已就绪 (等待 {i}s)")
                return True
        except Exception:
            pass
        time.sleep(1)
    print("  [WARN] API 服务可能未启动，继续尝试...")
    return False


def ensure_users():
    users = [
        (1, "admin", "admin"),
        (2, "lead1", "lead"),
        (3, "reviewer1", "reviewer"),
        (5, "submitter1", "submitter"),
    ]
    for uid, uname, role in users:
        safe_request("POST", f"{API}/api/users/",
            json={"id": uid, "username": uname, "role": role, "display_name": uname})


def save_persistence_state(state: dict):
    with open(PERSIST_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"  [INFO] 持久化状态已保存到 {PERSIST_FILE}")


def load_persistence_state() -> Optional[dict]:
    if os.path.exists(PERSIST_FILE):
        with open(PERSIST_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        print(f"  [INFO] 加载持久化状态: {json.dumps(state, ensure_ascii=False, indent=2)}")
        return state
    return None


def create_test_archive() -> bytes:
    """
    生成能通过完整性校验的归档包。
    严格匹配 build_archive_zip 的结构和哈希算法。
    """
    import hashlib

    ARCHIVE_FORMAT_VERSION = "1.0"
    ARCHIVE_MANIFEST_FILENAME = "manifest.json"
    ARCHIVE_HASH_FILENAME = "hash.sha256"
    ARCHIVE_DATA_DIR = "data"
    ARCHIVE_SECTION_BATCH = "batch"
    ARCHIVE_SECTION_VERSIONS = "manifest_versions"
    ARCHIVE_SECTION_ITEMS = "manifest_items"
    ARCHIVE_SECTION_VALIDATIONS = "validation_results"
    ARCHIVE_SECTION_REJECTIONS = "rejection_records"
    ARCHIVE_SECTION_APPROVAL_LOGS = "approval_logs"
    ARCHIVE_SECTION_PRECHECKS = "import_prechecks"
    ARCHIVE_SECTION_DIFF_SNAPSHOTS = "version_diff_snapshots"
    ARCHIVE_SECTION_SYSTEM_CONFIG = "system_config_snapshot"
    ARCHIVE_SECTION_VALIDATION_RULES = "validation_rules_snapshot"

    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _sha256_hex(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    ts = int(time.time())
    archive_id = _sha256_hex(f"reg-test-{ts}|1|admin")[:24]
    batch_code = f"REG-{ts}"

    sections_map = {
        ARCHIVE_SECTION_BATCH: "batch",
        ARCHIVE_SECTION_VERSIONS: "manifest_versions",
        ARCHIVE_SECTION_ITEMS: "manifest_items",
        ARCHIVE_SECTION_VALIDATIONS: "validation_results",
        ARCHIVE_SECTION_REJECTIONS: "rejection_records",
        ARCHIVE_SECTION_APPROVAL_LOGS: "approval_logs",
        ARCHIVE_SECTION_PRECHECKS: "import_prechecks",
        ARCHIVE_SECTION_DIFF_SNAPSHOTS: "version_diff_snapshots",
    }

    batch_data = {
        "id": None,
        "batch_code": batch_code,
        "name": "回归测试批次",
        "description": "用于 eligibility 边界测试",
        "status": "archived",
        "submitter_id": 1,
        "current_manifest_version_id": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "archived_by": 1,
    }

    version_id = ts  # 用时间戳作为假 version_id
    raw_csv = "item_id,item_name,quantity,unit_price\n001,测试项,10,100\n"

    versions_data = [{
        "id": version_id,
        "batch_id": None,
        "version_number": 1,
        "import_format": "csv",
        "imported_by": 1,
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "item_count": 1,
        "validation_status": "passed",
        "validation_summary": {"total_rules": 0, "passed": 0, "failed": 0},
        "content_hash": _sha256_hex(raw_csv),
    }]

    items_data = [{
        "id": ts,
        "manifest_version_id": version_id,
        "line_number": 2,
        "item_key": "001",
        "item_data": {"item_id": "001", "item_name": "测试项", "quantity": "10", "unit_price": "100"},
    }]

    data_obj = {
        "batch": batch_data,
        "manifest_versions": versions_data,
        "manifest_items": items_data,
        "validation_results": [],
        "rejection_records": [],
        "approval_logs": [],
        "import_prechecks": [],
        "version_diff_snapshots": [],
    }

    config_snapshot = {
        "validation_rules": [],
        "system_configs": [],
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    item_counts = {}
    for sec, key in sections_map.items():
        val = data_obj.get(key)
        if isinstance(val, list):
            item_counts[sec] = len(val)
        elif isinstance(val, dict):
            item_counts[sec] = 1
    item_counts[ARCHIVE_SECTION_SYSTEM_CONFIG] = len(config_snapshot["system_configs"])
    item_counts[ARCHIVE_SECTION_VALIDATION_RULES] = len(config_snapshot["validation_rules"])

    total_bytes = 0
    manifest = {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "archive_id": archive_id,
        "batch_code": batch_code,
        "batch_id_original": None,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_by_user_id": 1,
        "generated_by_username": "admin",
        "source_api_version": "1.0.0",
        "sections": list(sections_map.keys()) + [ARCHIVE_SECTION_SYSTEM_CONFIG, ARCHIVE_SECTION_VALIDATION_RULES],
        "total_bytes": 0,
        "item_counts": item_counts,
        "notes": "sandbox config regression test",
    }

    sections_content = {}
    for sec, key in sections_map.items():
        val = data_obj.get(key)
        sections_content[sec] = json.dumps(val, ensure_ascii=False, default=str).encode("utf-8")

    config_bytes = json.dumps(config_snapshot, ensure_ascii=False, default=str).encode("utf-8")
    rules_bytes = json.dumps(
        {"validation_rules": config_snapshot["validation_rules"]},
        ensure_ascii=False, default=str
    ).encode("utf-8")

    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")

    hash_input = manifest_bytes
    for sec in sections_content:
        hash_input += sections_content[sec]
    hash_input += config_bytes + rules_bytes
    content_hash = _sha256_bytes(hash_input)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(ARCHIVE_MANIFEST_FILENAME, manifest_bytes)
        zf.writestr(ARCHIVE_HASH_FILENAME, f"SHA256 {content_hash}\n")
        for sec in sections_content:
            zf.writestr(f"{ARCHIVE_DATA_DIR}/{sec}.json", sections_content[sec])
        zf.writestr(f"{ARCHIVE_DATA_DIR}/{ARCHIVE_SECTION_SYSTEM_CONFIG}.json", config_bytes)
        zf.writestr(f"{ARCHIVE_DATA_DIR}/{ARCHIVE_SECTION_VALIDATION_RULES}.json", rules_bytes)

    return buf.getvalue()


# ============ A. 规则分叉：旧入口 vs 新入口 ============

def test_A_rule_fork_old_vs_new_entry():
    """A. 规则分叉：/api/system-configs (旧入口) vs /api/sandbox-config (新入口)"""
    section("A. 规则分叉：旧入口(/api/system-configs) vs 新入口(/api/sandbox-config)")

    # A.1 旧入口只有 admin 能访问（read）
    print("\n--- A.1 旧入口访问权限（仅 admin） ---")
    r = safe_request("GET", f"{API}/api/system-configs/", headers=H_ADMIN)
    check("A.1.1 admin GET 旧入口列表=200", r.status_code == 200, f"status={r.status_code}")

    r = safe_request("GET", f"{API}/api/system-configs/", headers=H_LEAD)
    check("A.1.2 lead GET 旧入口列表=403", r.status_code == 403, f"status={r.status_code}")

    r = safe_request("GET", f"{API}/api/system-configs/", headers=H_REVIEWER)
    check("A.1.3 reviewer GET 旧入口列表=403", r.status_code == 403, f"status={r.status_code}")

    # A.2 新入口：reviewer+ 能读
    print("\n--- A.2 新入口读取权限（reviewer 及以上可读） ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    check("A.2.1 admin GET 新入口=200", r.status_code == 200)

    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_LEAD)
    check("A.2.2 lead GET 新入口=200", r.status_code == 200)

    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_REVIEWER)
    check("A.2.3 reviewer GET 新入口=200", r.status_code == 200)

    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_SUBMITTER)
    check("A.2.4 submitter GET 新入口=403", r.status_code == 403, f"status={r.status_code}")

    # A.3 新入口：只有 admin 能写
    print("\n--- A.3 新入口写入权限（仅 admin 可写） ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.enabled", headers=H_ADMIN)
    current_val = r.json().get("config_value", "true") if r.status_code == 200 else "true"

    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_LEAD, json={"config_value": "true"})
    check("A.3.1 lead PUT 新入口=403", r.status_code == 403, f"status={r.status_code}")

    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_REVIEWER, json={"config_value": "true"})
    check("A.3.2 reviewer PUT 新入口=403", r.status_code == 403, f"status={r.status_code}")

    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": current_val})
    check("A.3.3 admin PUT 新入口=200", r.status_code == 200, f"status={r.status_code}")

    # A.4 新入口 batch：只有 admin 能写
    print("\n--- A.4 新入口 batch 写入权限（仅 admin） ---")
    r = safe_request("PATCH", f"{API}/api/sandbox-config/batch",
        headers=H_LEAD, json={"updates": [{"config_key": "sandbox.enabled", "config_value": "true"}]})
    check("A.4.1 lead PATCH batch=403", r.status_code == 403)

    r = safe_request("PATCH", f"{API}/api/sandbox-config/batch",
        headers=H_REVIEWER, json={"updates": [{"config_key": "sandbox.enabled", "config_value": "true"}]})
    check("A.4.2 reviewer PATCH batch=403", r.status_code == 403)

    r = safe_request("PATCH", f"{API}/api/sandbox-config/batch",
        headers=H_ADMIN, json={"updates": [{"config_key": "sandbox.enabled", "config_value": "true"}]})
    check("A.4.3 admin PATCH batch=200", r.status_code == 200)


# ============ B. 三种角色读写边界 ============

def test_B_role_rw_boundaries():
    """B. reviewer / admin / lead 读写边界全面覆盖"""
    section("B. 三种角色读写边界：reviewer / lead / admin")

    # B.1 读边界：新入口的 GET 列表 / 单条 / 审计日志
    print("\n--- B.1 读边界：新入口 GET 系列 ---")
    for role_name, headers in [("admin", H_ADMIN), ("lead", H_LEAD), ("reviewer", H_REVIEWER)]:
        r = safe_request("GET", f"{API}/api/sandbox-config/", headers=headers)
        check(f"B.1.{role_name}_list=200", r.status_code == 200, f"status={r.status_code}")

        r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.enabled", headers=headers)
        check(f"B.1.{role_name}_single=200", r.status_code == 200, f"status={r.status_code}")

        r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=headers)
        check(f"B.1.{role_name}_audit_logs=200", r.status_code == 200, f"status={r.status_code}")

        r = safe_request("GET", f"{API}/api/sandbox-config/eligibility", headers=headers)
        check(f"B.1.{role_name}_eligibility=200", r.status_code == 200, f"status={r.status_code}")

    # submitter 不能读
    for path in ["/api/sandbox-config/", "/api/sandbox-config/sandbox.enabled",
                 "/api/sandbox-config/audit-logs"]:
        r = safe_request("GET", f"{API}{path}", headers=H_SUBMITTER)
        check(f"B.1.submitter_{path.split('/')[-1] or 'list'}=403", r.status_code == 403)

    # B.2 写边界：PUT 单条 + PATCH batch
    print("\n--- B.2 写边界：新入口 PUT/PATCH ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_ADMIN)
    cur_expire = r.json().get("config_value") if r.status_code == 200 else "24"

    for role_name, headers, expected in [("admin", H_ADMIN, 200), ("lead", H_LEAD, 403),
                                          ("reviewer", H_REVIEWER, 403), ("submitter", H_SUBMITTER, 403)]:
        r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
            headers=headers, json={"config_value": cur_expire})
        check(f"B.2.{role_name}_PUT={expected}", r.status_code == expected, f"status={r.status_code}")

        r = safe_request("PATCH", f"{API}/api/sandbox-config/batch",
            headers=headers, json={"updates": [{"config_key": "sandbox.auto_expire_hours", "config_value": cur_expire}]})
        expected_batch = 200 if role_name == "admin" else 403
        check(f"B.2.{role_name}_PATCH={expected_batch}", r.status_code == expected_batch, f"status={r.status_code}")


# ============ C. 旧入口拒改 sandbox.* 键 ============

def test_C_old_entry_block_sandbox_keys():
    """C. 旧入口 /api/system-configs/ 拒改 sandbox.* 键（只允许改 archive.*）"""
    section("C. 旧入口拒改 sandbox.* 键 —— 只允许改 archive.*")

    # C.1 先用 admin 尝试修改 sandbox.enabled —— 必须被旧入口拒绝
    print("\n--- C.1 旧入口修改 sandbox.* 应拒绝（400 或 404） ---")
    for sk in SANDBOX_KEYS:
        r = safe_request("PUT", f"{API}/api/system-configs/{sk}",
            headers=H_ADMIN, json={"config_value": "true"})
        check(f"C.1.1 旧入口 PUT {sk} 被拒绝 (!=200)", r.status_code != 200, f"status={r.status_code}")
        if r.status_code == 400:
            body = r.json() if callable(getattr(r, 'json', None)) else {}
            msg = str(body.get("error", {}).get("message", "")) or str(body.get("detail", ""))
            check(f"C.1.2 旧入口拒绝 {sk} 的理由合法", len(msg) > 0, f"msg={msg}")

    # C.2 尝试通过旧入口读取 sandbox.* 键
    print("\n--- C.2 旧入口读取 sandbox.* 键（不保证能读到，但不能污染写） ---")
    for sk in SANDBOX_KEYS:
        r = safe_request("GET", f"{API}/api/system-configs/{sk}", headers=H_ADMIN)
        # 可能 404 或 200，但我们更关心写被拒绝
        print(f"  [INFO] GET 旧入口 {sk}: status={r.status_code}")

    # C.3 旧入口仍然可以修改自己的合法键
    print("\n--- C.3 旧入口修改合法键 archive.* 应成功 ---")
    for valid_key in OLD_ENTRY_ALLOWED_KEYS:
        # 先读当前值
        r = safe_request("GET", f"{API}/api/system-configs/{valid_key}", headers=H_ADMIN)
        cur_val = "true"
        if r.status_code == 200:
            cur_val = r.json().get("config_value", "true")
        # 写入相同值（避免真正改变配置）
        r = safe_request("PUT", f"{API}/api/system-configs/{valid_key}",
            headers=H_ADMIN, json={"config_value": cur_val})
        check(f"C.3 旧入口 PUT {valid_key}=200 或合理错误",
              r.status_code in (200, 400, 404), f"status={r.status_code}")


# ============ D. 新入口并发保护：陈旧值写回 409 且不脏写 ============

def test_D_concurrency_stale_write_409():
    """D. expected_old_value 并发保护 —— 陈旧值写回 409，且不脏写"""
    section("D. 新入口并发保护：陈旧值写回 409 + 不脏写")

    # D.0 先重置一个确定的值
    print("\n--- D.0 重置配置到确定值 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "24"})
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_ADMIN)
    assert r.status_code == 200, "重置失败"
    base_val = r.json()["config_value"]
    print(f"  [INFO] 基准值: {base_val}")

    # D.1 不带 expected_old_value —— 直接修改，应成功
    print("\n--- D.1 不带 expected_old_value 直接修改（应成功） ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "36"})
    check("D.1.1 无 expected_old_value PUT=200", r.status_code == 200, f"status={r.status_code}")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_ADMIN)
    new_val = r.json()["config_value"]
    check("D.1.2 无 expected_old_value 实际生效", new_val == "36", f"actual={new_val}")

    # D.2 带正确 expected_old_value —— 应成功
    print("\n--- D.2 带正确 expected_old_value 修改（应成功） ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN,
        json={"config_value": "48", "expected_old_value": "36"})
    check("D.2.1 正确 expected_old_value PUT=200", r.status_code == 200, f"status={r.status_code}")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_ADMIN)
    new_val2 = r.json()["config_value"]
    check("D.2.2 正确 expected_old_value 实际生效", new_val2 == "48", f"actual={new_val2}")

    # D.3 带错误（陈旧） expected_old_value —— 返回 409 且不脏写
    print("\n--- D.3 带陈旧 expected_old_value（应 409 且不脏写） ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN,
        json={"config_value": "72", "expected_old_value": "36"})
    check("D.3.1 陈旧 expected_old_value PUT=409", r.status_code == 409, f"status={r.status_code}, body={r.text[:200]}")

    # 关键：验证值没有被脏写 —— 仍应为 48
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_ADMIN)
    final_val = r.json()["config_value"]
    check("D.3.2 陈旧值写回不脏写（仍=48）", final_val == "48", f"actual={final_val}")

    # D.4 再带正确值重试，应成功（验证冲突后的重试流程）
    print("\n--- D.4 冲突后刷新值重试（应成功） ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN,
        json={"config_value": "72", "expected_old_value": "48"})
    check("D.4.1 刷新后重试 PUT=200", r.status_code == 200, f"status={r.status_code}")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_ADMIN)
    retry_val = r.json()["config_value"]
    check("D.4.2 刷新后重试生效", retry_val == "72", f"actual={retry_val}")

    # D.5 batch 更新中的并发保护
    print("\n--- D.5 batch 更新中的并发保护 ---")
    # 先设回 24
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "24"})

    # batch：部分带陈旧 expected_old_value，应失败且不脏写
    r = safe_request("PATCH", f"{API}/api/sandbox-config/batch",
        headers=H_ADMIN,
        json={"updates": [
            {"config_key": "sandbox.auto_expire_hours", "config_value": "30", "expected_old_value": "WRONG_OLD_VALUE"},
            {"config_key": "sandbox.require_admin_confirm", "config_value": "true"},
        ]})
    check("D.5.1 batch 状态=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        # 第一个应失败，第二个成功
        check("D.5.2 batch updated_count=1", data.get("updated_count") == 1,
              f"updated_count={data.get('updated_count')}")
        check("D.5.3 batch failed_count=1", data.get("failed_count") == 1,
              f"failed_count={data.get('failed_count')}")
        check("D.5.4 batch success=False", data.get("success") == False)

    # 验证 sandbox.auto_expire_hours 未被脏写
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_ADMIN)
    batch_final = r.json()["config_value"]
    check("D.5.5 batch 陈旧项未脏写（仍=24）", batch_final == "24", f"actual={batch_final}")

    # D.6 恢复基准值
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "24"})


# ============ E. require_admin_confirm 切换前后 eligibility 变化 ============

def test_E_require_admin_confirm_switch_eligibility():
    """E. sandbox.require_admin_confirm 切换前后 /api/sandbox/{token}/eligibility 变化"""
    section("E. require_admin_confirm 切换前后 eligibility 结果变化")

    # E.0 先确保 sandbox.enabled=true
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "true"})

    # E.1 创建一个沙盒会话用于测试
    print("\n--- E.1 创建测试用沙盒会话 ---")
    archive_bytes = create_test_archive()
    files = {"file": ("test.zip", archive_bytes, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files)
    sandbox_token = None
    if r.status_code == 200:
        sandbox_token = r.json().get("sandbox_token")
        check("E.1.1 沙盒会话创建成功", sandbox_token is not None)
        print(f"  [INFO] sandbox_token={sandbox_token[:20]}...")
    else:
        warn(f"E.1 创建沙盒失败 status={r.status_code} body={r.text[:200]}")
        return

    # E.2 require_admin_confirm=true（默认）时 eligibility
    print("\n--- E.2 require_admin_confirm=true 时 eligibility ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "true"})

    # 各角色查 eligibility
    for role_name, headers, exp_can_view, exp_can_confirm in [
        ("admin",     H_ADMIN,     True,  True),
        ("lead",      H_LEAD,      True,  False),   # true 时 lead 不能确认
        ("reviewer",  H_REVIEWER,  True,  False),
    ]:
        r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}/eligibility", headers=headers)
        check(f"E.2.{role_name}_status=200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            check(f"E.2.{role_name}_can_view={exp_can_view}",
                  data.get("can_view") == exp_can_view, f"actual={data.get('can_view')}")
            check(f"E.2.{role_name}_can_confirm={exp_can_confirm}",
                  data.get("can_confirm") == exp_can_confirm, f"actual={data.get('can_confirm')}")
            check(f"E.2.{role_name}_require_admin_confirm=true",
                  data.get("require_admin_confirm") == True)

    # E.3 切换为 require_admin_confirm=false 时 eligibility
    print("\n--- E.3 require_admin_confirm=false 时 eligibility ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "false"})

    for role_name, headers, exp_can_view, exp_can_confirm in [
        ("admin",     H_ADMIN,     True,  True),
        ("lead",      H_LEAD,      True,  True),    # false 时 lead 可以确认了
        ("reviewer",  H_REVIEWER,  True,  False),   # reviewer 永远不能确认
    ]:
        r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}/eligibility", headers=headers)
        check(f"E.3.{role_name}_status=200", r.status_code == 200)
        if r.status_code == 200:
            data = r.json()
            check(f"E.3.{role_name}_can_view={exp_can_view}",
                  data.get("can_view") == exp_can_view)
            check(f"E.3.{role_name}_can_confirm={exp_can_confirm}",
                  data.get("can_confirm") == exp_can_confirm, f"actual={data.get('can_confirm')}")
            check(f"E.3.{role_name}_require_admin_confirm=false",
                  data.get("require_admin_confirm") == False)

    # E.4 对比 lead 在切换前后 can_confirm 的变化
    print("\n--- E.4 验证 lead 的 can_confirm 随配置翻转 ---")
    # 翻回 true 再次验证
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "true"})
    r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}/eligibility", headers=H_LEAD)
    if r.status_code == 200:
        check("E.4.1 lead 翻回 true 后 can_confirm=false", r.json().get("can_confirm") == False,
              f"actual={r.json().get('can_confirm')}")

    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "false"})
    r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}/eligibility", headers=H_LEAD)
    if r.status_code == 200:
        check("E.4.2 lead 翻为 false 后 can_confirm=true", r.json().get("can_confirm") == True,
              f"actual={r.json().get('can_confirm')}")

    # E.5 恢复默认值 true
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "true"})


# ============ F. reviewer 不能确认 + lead 能力随配置切换 ============

def test_F_reviewer_cannot_confirm_lead_switch():
    """F. reviewer 不能确认 + lead 能力随配置切换（实际 confirm/reject 调用）"""
    section("F. reviewer 不能确认；lead 能力随配置切换（实际调用 confirm/reject）")

    # F.0 确保沙盒开启并创建会话
    print("\n--- F.0 准备：确保沙盒开启 + 创建新会话 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "true"})

    archive_bytes = create_test_archive()
    files = {"file": ("test.zip", archive_bytes, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files)
    sandbox_token = None
    if r.status_code == 200:
        sandbox_token = r.json().get("sandbox_token")
    if not sandbox_token:
        warn("F.0 创建沙盒会话失败，跳过 F 组部分测试")
        return
    print(f"  [INFO] 测试用 sandbox_token={sandbox_token[:20]}...")

    # F.1 require_admin_confirm=true 时：reviewer 不能确认
    print("\n--- F.1 require_admin_confirm=true：reviewer 不能 confirm/reject ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "true"})

    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/confirm",
        headers=H_REVIEWER, json={"comment": "reviewer 尝试确认"})
    check("F.1.1 reviewer confirm=403", r.status_code == 403, f"status={r.status_code}")

    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/reject",
        headers=H_REVIEWER, json={"reason": "reviewer 尝试拒绝"})
    check("F.1.2 reviewer reject=403", r.status_code == 403, f"status={r.status_code}")

    # F.2 require_admin_confirm=true 时：lead 也不能确认
    print("\n--- F.2 require_admin_confirm=true：lead 也不能 confirm/reject ---")
    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/confirm",
        headers=H_LEAD, json={"comment": "lead 尝试确认（require_admin=true）"})
    check("F.2.1 lead confirm=403", r.status_code == 403, f"status={r.status_code}")

    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/reject",
        headers=H_LEAD, json={"reason": "lead 尝试拒绝（require_admin=true）"})
    check("F.2.2 lead reject=403", r.status_code == 403, f"status={r.status_code}")

    # F.3 require_admin_confirm=true 时：admin 能确认（但沙盒可能没有 precheck，所以先不实际确认，看错误是否非 403）
    print("\n--- F.3 require_admin_confirm=true：admin 调用 confirm（403 vs 其他错误） ---")
    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/confirm",
        headers=H_ADMIN, json={"comment": "admin 尝试确认"})
    check("F.3 admin confirm 不是 403（权限上通过了）", r.status_code != 403,
          f"status={r.status_code}")

    # F.4 require_admin_confirm=false 时：lead 能确认了
    print("\n--- F.4 require_admin_confirm=false：lead 可以 confirm/reject（权限通过） ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "false"})

    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/confirm",
        headers=H_LEAD, json={"comment": "lead 尝试确认（require_admin=false）"})
    check("F.4.1 lead confirm 不是 403（权限通过）", r.status_code != 403,
          f"status={r.status_code}")

    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/reject",
        headers=H_LEAD, json={"reason": "lead 尝试拒绝（require_admin=false）"})
    check("F.4.2 lead reject 不是 403（权限通过）", r.status_code != 403,
          f"status={r.status_code}")

    # F.5 require_admin_confirm=false 时：reviewer 还是不能确认
    print("\n--- F.5 require_admin_confirm=false：reviewer 仍然不能 confirm/reject ---")
    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/confirm",
        headers=H_REVIEWER, json={"comment": "reviewer 再试（false）"})
    check("F.5.1 reviewer confirm=403（false 时也不行）", r.status_code == 403,
          f"status={r.status_code}")

    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/reject",
        headers=H_REVIEWER, json={"reason": "reviewer 再试（false）"})
    check("F.5.2 reviewer reject=403（false 时也不行）", r.status_code == 403,
          f"status={r.status_code}")

    # F.6 翻回 true，lead 再次失去确认权限（翻回验证）
    print("\n--- F.6 翻回 true，lead 再次失去确认权限 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "true"})

    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/confirm",
        headers=H_LEAD, json={"comment": "lead 翻回 true 再试"})
    check("F.6 lead 翻回 true 后 confirm=403", r.status_code == 403,
          f"status={r.status_code}")

    # F.7 恢复默认值
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "true"})


# ============ G. 重启后配置 + 权限判断一致性 ============

def test_G_before_restart_save_state():
    """G.1 重启前：修改配置并保存期望状态"""
    section("G.1 重启前：修改配置 + 保存状态 + 记录权限判断快照")

    # G.1.1 设置一组确定的配置值
    print("\n--- G.1.1 设置确定的配置值 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "true"})
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "false"})
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "36"})

    # G.1.2 读取当前配置快照
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    assert r.status_code == 200, "G 组：读取配置快照失败"
    data = r.json()
    config_snapshot = {
        "sandbox_enabled": data.get("sandbox_enabled"),
        "require_admin_confirm": data.get("require_admin_confirm"),
        "auto_expire_hours": data.get("auto_expire_hours"),
        "items": {it["config_key"]: it["config_value"] for it in data.get("items", [])},
    }
    print(f"  [INFO] 配置快照: {json.dumps(config_snapshot, ensure_ascii=False)}")

    # G.1.3 创建沙盒会话并记录 eligibility 快照
    archive_bytes = create_test_archive()
    files = {"file": ("test.zip", archive_bytes, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files)
    eligibility_snapshot = {}
    if r.status_code == 200:
        token = r.json().get("sandbox_token")
        # 对三个角色分别记录 eligibility
        for role_name, headers in [("admin", H_ADMIN), ("lead", H_LEAD), ("reviewer", H_REVIEWER)]:
            re = safe_request("GET", f"{API}/api/sandbox/{token}/eligibility", headers=headers)
            if re.status_code == 200:
                d = re.json()
                eligibility_snapshot[role_name] = {
                    "can_view": d.get("can_view"),
                    "can_confirm": d.get("can_confirm"),
                    "require_admin_confirm": d.get("require_admin_confirm"),
                }
    print(f"  [INFO] eligibility 快照: {json.dumps(eligibility_snapshot, ensure_ascii=False)}")

    # G.1.4 记录审计日志数量
    audit_r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_ADMIN)
    audit_count = len(audit_r.json()) if audit_r.status_code == 200 else 0

    state = {
        "config_snapshot": config_snapshot,
        "eligibility_snapshot": eligibility_snapshot,
        "audit_log_count_before": audit_count,
        "timestamp": time.time(),
        "timestamp_human": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_persistence_state(state)
    check("G.1 持久化状态已保存", os.path.exists(PERSIST_FILE))


def test_G_after_restart_verify():
    """G.2 重启后：验证配置值 + 权限判断仍一致"""
    section("G.2 重启后：验证配置值 + 权限判断一致性")

    state = load_persistence_state()
    if not state:
        warn("G.2 没有持久化状态文件，跳过重启后验证")
        return

    config_before = state.get("config_snapshot", {})
    eligibility_before = state.get("eligibility_snapshot", {})
    audit_before = state.get("audit_log_count_before", 0)

    # G.2.1 验证配置值保留
    print("\n--- G.2.1 验证重启后配置值未变 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    check("G.2.1.1 重启后 GET 配置列表=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("G.2.1.2 sandbox_enabled 一致",
              data.get("sandbox_enabled") == config_before.get("sandbox_enabled"),
              f"before={config_before.get('sandbox_enabled')}, after={data.get('sandbox_enabled')}")
        check("G.2.1.3 require_admin_confirm 一致",
              data.get("require_admin_confirm") == config_before.get("require_admin_confirm"),
              f"before={config_before.get('require_admin_confirm')}, after={data.get('require_admin_confirm')}")
        check("G.2.1.4 auto_expire_hours 一致",
              data.get("auto_expire_hours") == config_before.get("auto_expire_hours"),
              f"before={config_before.get('auto_expire_hours')}, after={data.get('auto_expire_hours')}")

        # 逐项对比 items
        items_after = {it["config_key"]: it["config_value"] for it in data.get("items", [])}
        items_before = config_before.get("items", {})
        for k in SANDBOX_KEYS:
            check(f"G.2.1.5 items.{k} 一致",
                  items_after.get(k) == items_before.get(k),
                  f"before={items_before.get(k)}, after={items_after.get(k)}")

    # G.2.2 验证基于配置的权限判断仍一致（eligibility）
    print("\n--- G.2.2 验证重启后权限判断（eligibility）一致 ---")
    # 创建一个新沙盒来判断权限（因为旧沙盒可能过期）
    archive_bytes = create_test_archive()
    files = {"file": ("test.zip", archive_bytes, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files)
    if r.status_code == 200:
        token = r.json().get("sandbox_token")
        for role_name, headers in [("admin", H_ADMIN), ("lead", H_LEAD), ("reviewer", H_REVIEWER)]:
            re = safe_request("GET", f"{API}/api/sandbox/{token}/eligibility", headers=headers)
            if re.status_code == 200 and role_name in eligibility_before:
                d_after = re.json()
                d_before = eligibility_before[role_name]
                check(f"G.2.2.{role_name}_can_view 一致",
                      d_after.get("can_view") == d_before.get("can_view"),
                      f"before={d_before.get('can_view')}, after={d_after.get('can_view')}")
                check(f"G.2.2.{role_name}_can_confirm 一致",
                      d_after.get("can_confirm") == d_before.get("can_confirm"),
                      f"before={d_before.get('can_confirm')}, after={d_after.get('can_confirm')}")
                check(f"G.2.2.{role_name}_require_admin_confirm 一致",
                      d_after.get("require_admin_confirm") == d_before.get("require_admin_confirm"),
                      f"before={d_before.get('require_admin_confirm')}, after={d_after.get('require_admin_confirm')}")
    else:
        warn(f"G.2.2 创建新沙盒失败 status={r.status_code}")

    # G.2.3 审计日志：重启后应 >= 重启前
    print("\n--- G.2.3 审计日志未丢失 ---")
    audit_r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_ADMIN)
    if audit_r.status_code == 200:
        audit_after = len(audit_r.json())
        check(f"G.2.3 审计日志数量未减少 (before={audit_before}, after={audit_after})",
              audit_after >= audit_before,
              f"before={audit_before}, after={audit_after}")

    # G.2.4 基于配置的 confirm/reject 权限判断一致（调用接口验证 403 与否）
    print("\n--- G.2.4 重启后 confirm/reject 权限一致（require_admin=false，lead 可进非 403） ---")
    archive_bytes2 = create_test_archive()
    files2 = {"file": ("test2.zip", archive_bytes2, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files2)
    if r.status_code == 200:
        token2 = r.json().get("sandbox_token")
        # require_admin_confirm=false 时 lead 应非 403
        r = safe_request("POST", f"{API}/api/sandbox/{token2}/confirm",
            headers=H_LEAD, json={"comment": "重启后 lead 确认测试"})
        check("G.2.4.1 重启后 lead confirm!=403（require_admin=false 时）",
              r.status_code != 403, f"status={r.status_code}")

        # reviewer 永远 403
        r = safe_request("POST", f"{API}/api/sandbox/{token2}/reject",
            headers=H_REVIEWER, json={"reason": "重启后 reviewer 拒绝测试"})
        check("G.2.4.2 重启后 reviewer reject=403", r.status_code == 403, f"status={r.status_code}")


# ============ 主流程 ============

def main():
    print()
    print("=" * 70)
    print("沙盒配置回归测试 —— 规则分叉 & 边界条件")
    print("=" * 70)
    print(f"测试目标 API: {API}")
    print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    wait_for_api()
    ensure_users()

    # 检查是否为重启后阶段
    existing_state = load_persistence_state()
    is_restart_phase = bool(existing_state and os.environ.get("TEST_RESTART_PHASE") == "1")

    if is_restart_phase:
        # 只跑重启后验证
        test_G_after_restart_verify()
    else:
        # 完整流程
        test_A_rule_fork_old_vs_new_entry()
        test_B_role_rw_boundaries()
        test_C_old_entry_block_sandbox_keys()
        test_D_concurrency_stale_write_409()
        test_E_require_admin_confirm_switch_eligibility()
        test_F_reviewer_cannot_confirm_lead_switch()
        test_G_before_restart_save_state()

        print("\n" + "=" * 70)
        print("提示：如需验证重启后一致性（G 组），请：")
        print(f"  1. 重启 API 服务（端口不变 {API}）")
        print("  2. 设置 TEST_RESTART_PHASE=1 重新运行本脚本")
        print("=" * 70)

    # 汇总
    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    print(f"总检查项: {_total}")
    print(f"通过: {_passed}")
    print(f"失败: {_failed}")
    if _warnings:
        print(f"\n警告 ({len(_warnings)}):")
        for w in _warnings:
            print(f"  [WARN] {w}")
    if _failed == 0:
        print("\n[PASS] All regression tests passed!")
    else:
        print(f"\n[FAIL] {_failed} checks failed")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
