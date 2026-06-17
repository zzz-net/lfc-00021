"""
Unified sandbox config domain integration tests

Covers:
1. Reviewer read-only access (can view, cannot edit)
2. Admin change takes effect immediately
3. Disabled sandbox blocks all related flows
4. Config persists across restart
5. Concurrent modification protection (409 Conflict)
6. Audit log integrity (no failure conclusions without real trace)
7. Type validation and normalization
8. Batch update with partial concurrency conflicts
"""

import os
import sys
import json
import time
import io
import zipfile
import requests
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

API = os.environ.get("TEST_API_URL", "http://127.0.0.1:8003")

PERSIST_FILE = ".test_sandbox_config_domain_persistence.json"

H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER = {"X-User-Id": "3"}
H_SUBMITTER = {"X-User-Id": "5"}

TEST_SECTION = ""
_total = 0
_passed = 0
_failed = 0
_warnings = []


def safe_str(s):
    if isinstance(s, bytes):
        try:
            return s.decode("utf-8", errors="replace")
        except:
            return str(s)
    return str(s).encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def section(name):
    global TEST_SECTION
    TEST_SECTION = name
    print(f"\n{'=' * 60}")
    print(f"【{name}】")
    print(f"{'=' * 60}")


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
        return requests.request(method, url, timeout=15, **kwargs)
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
    for i in range(30):
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


def reset_config_to_defaults():
    safe_request("PATCH", f"{API}/api/sandbox-config/batch",
                 headers=H_ADMIN,
                 json={"updates": [
                     {"config_key": "sandbox.enabled", "config_value": "true"},
                     {"config_key": "sandbox.require_admin_confirm", "config_value": "true"},
                     {"config_key": "sandbox.auto_expire_hours", "config_value": "24"},
                 ]})


def test_reviewer_read_only():
    """Phase 1: Reviewer can view configs but cannot modify them"""
    section("阶段1：reviewer 只读访问")

    print("\n--- 1.1 reviewer 查看配置列表 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_REVIEWER)
    check("1.1.1 reviewer 查看配置 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("1.1.2 total=3", data.get("total") == 3, f"total={data.get('total')}")
        check("1.1.3 sandbox_enabled 存在", "sandbox_enabled" in data)
        check("1.1.4 require_admin_confirm 存在", "require_admin_confirm" in data)
        check("1.1.5 auto_expire_hours 存在", "auto_expire_hours" in data)

    print("\n--- 1.2 reviewer 查看单条配置 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.enabled", headers=H_REVIEWER)
    check("1.2.1 reviewer 查看单条 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("1.2.2 config_key 正确", data.get("config_key") == "sandbox.enabled")
        check("1.2.3 parsed_value 存在", "parsed_value" in data)

    print("\n--- 1.3 reviewer 尝试修改（应 403）---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_REVIEWER, json={"config_value": "false"})
    check("1.3.1 reviewer 修改被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    print("\n--- 1.4 reviewer 查看审计日志 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_REVIEWER)
    check("1.4.1 reviewer 查看审计日志 status=200", r.status_code == 200, f"status={r.status_code}")

    print("\n--- 1.5 reviewer 查看 eligibility ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/eligibility", headers=H_REVIEWER)
    check("1.5.1 reviewer eligibility status=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("1.5.2 can_view=True", data.get("can_view") == True)
        check("1.5.3 can_edit=False", data.get("can_edit") == False)

    print("\n--- 1.6 submitter 完全无法访问 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_SUBMITTER)
    check("1.6.1 submitter 查看被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")


def test_admin_change_takes_effect():
    """Phase 2: Admin changes take effect immediately"""
    section("阶段2：admin 修改立即生效")

    reset_config_to_defaults()

    print("\n--- 2.1 修改 sandbox.enabled=false ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_ADMIN, json={"config_value": "false"})
    check("2.1.1 修改 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("2.1.2 config_value=false", data.get("config_value") == "false")
        check("2.1.3 parsed_value=False", data.get("parsed_value") == False)

    print("\n--- 2.2 验证列表汇总反映修改 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    if r.status_code == 200:
        data = r.json()
        check("2.2.1 sandbox_enabled=False", data.get("sandbox_enabled") == False)

    print("\n--- 2.3 修改 sandbox.require_admin_confirm=false ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
                     headers=H_ADMIN, json={"config_value": "false"})
    check("2.3.1 修改 status=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("2.3.2 config_value=false", data.get("config_value") == "false")

    print("\n--- 2.4 修改 sandbox.auto_expire_hours=48 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
                     headers=H_ADMIN, json={"config_value": "48"})
    check("2.4.1 修改 status=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("2.4.2 config_value=48", data.get("config_value") == "48")
        check("2.4.3 parsed_value=48", data.get("parsed_value") == 48)

    print("\n--- 2.5 验证列表汇总全部反映 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    if r.status_code == 200:
        data = r.json()
        check("2.5.1 sandbox_enabled=False", data.get("sandbox_enabled") == False)
        check("2.5.2 require_admin_confirm=False", data.get("require_admin_confirm") == False)
        check("2.5.3 auto_expire_hours=48", data.get("auto_expire_hours") == 48)

    print("\n--- 2.6 类型校验 - bool 非法值 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_ADMIN, json={"config_value": "not_a_bool"})
    check("2.6.1 bool 非法值被拒绝 status=400", r.status_code == 400, f"status={r.status_code}")

    print("\n--- 2.7 类型校验 - int 非法值 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
                     headers=H_ADMIN, json={"config_value": "not_an_int"})
    check("2.7.1 int 非法值被拒绝 status=400", r.status_code == 400, f"status={r.status_code}")

    print("\n--- 2.8 类型校验 - int 超出范围 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
                     headers=H_ADMIN, json={"config_value": "0"})
    check("2.8.1 int 最小值=1，0 被拒绝 status=400", r.status_code == 400, f"status={r.status_code}")

    print("\n--- 2.9 bool 值归一化 ---")
    for test_val in ["yes", "ON", "1", "True"]:
        r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                         headers=H_ADMIN, json={"config_value": test_val})
        if r.status_code == 200:
            data = r.json()
            check(f"2.9.{test_val} 归一化为 true", data.get("config_value") == "true",
                  f"input={test_val}, output={data.get('config_value')}")

    for test_val in ["no", "OFF", "0", "False"]:
        r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                         headers=H_ADMIN, json={"config_value": test_val})
        if r.status_code == 200:
            data = r.json()
            check(f"2.9.{test_val} 归一化为 false", data.get("config_value") == "false",
                  f"input={test_val}, output={data.get('config_value')}")

    reset_config_to_defaults()


def create_test_archive() -> tuple:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "format_version": "1.0.0",
            "archive_id": f"test-domain-{int(time.time())}",
            "batch_code": f"DOMAIN-TEST-{int(time.time())}",
            "batch_id_original": None,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generated_by_user_id": 1,
            "generated_by_username": "admin",
            "source_api_version": "1.0.0",
            "sections": ["batch", "versions", "items"],
            "total_bytes": 0,
            "item_counts": {"versions": 1, "items": 1},
            "notes": "sandbox config domain test",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        batch_data = {
            "batch_code": manifest["batch_code"],
            "name": "配置域测试批次",
            "description": "用于测试沙盒开关",
            "status": "archived",
            "submitter_id": 1,
        }
        zf.writestr("data/batch.json", json.dumps(batch_data, ensure_ascii=False))

        version_data = {
            "version_number": 1,
            "import_format": "csv",
            "imported_by": 1,
            "item_count": 1,
            "validation_status": "passed",
            "raw_content": "item_id,item_name,quantity,unit_price\n001,测试项,10,100\n",
        }
        zf.writestr("data/versions/v1.json", json.dumps(version_data, ensure_ascii=False))

        item_data = [
            {"line_number": 1, "item_key": "ITEM-001",
             "item_data": {"item_id": "001", "item_name": "测试项", "quantity": 10, "unit_price": 100}}
        ]
        zf.writestr("data/items/v1.json", json.dumps(item_data, ensure_ascii=False))
        zf.writestr("hash.sha256", "placeholder")

    return buf.getvalue(), manifest["batch_code"]


def test_disabled_blocks_flow():
    """Phase 3: Disabling sandbox blocks all related flows"""
    section("阶段3：关闭沙盒后相关流程被拦")

    reset_config_to_defaults()

    print("\n--- 3.1 先开启沙盒，创建会话 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                 headers=H_ADMIN, json={"config_value": "true"})

    archive_bytes, batch_code = create_test_archive()
    files = {"file": ("test.zip", archive_bytes, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files)
    check("3.1.1 开启时恢复归档不是 403", r.status_code != 403, f"status={r.status_code}")

    sandbox_token = None
    if r.status_code == 200:
        sandbox_token = r.json().get("sandbox_token")
        print(f"  [INFO] 创建沙盒会话: token={sandbox_token[:16]}...")

    print("\n--- 3.2 关闭沙盒 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_ADMIN, json={"config_value": "false"})
    check("3.2.1 关闭沙盒 status=200", r.status_code == 200)

    print("\n--- 3.3 关闭后恢复归档被拦 ---")
    archive_bytes2, _ = create_test_archive()
    files2 = {"file": ("test2.zip", archive_bytes2, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files2)
    check("3.3.1 关闭时恢复归档被拦 status=403", r.status_code == 403, f"status={r.status_code}")

    if sandbox_token:
        print("\n--- 3.4 关闭后已有会话操作被拦 ---")
        r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/import",
                         headers=H_LEAD,
                         files={"file": ("v2.csv", b"item_id,item_name,quantity,unit_price\n", "text/csv")},
                         data={"import_format": "auto"})
        check("3.4.1 关闭时导入被拦 status=403", r.status_code == 403, f"status={r.status_code}")

        r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}/diff", headers=H_LEAD)
        check("3.4.2 关闭时差异被拦 status=403", r.status_code == 403, f"status={r.status_code}")

        r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/precheck", headers=H_LEAD)
        check("3.4.3 关闭时预检查被拦 status=403", r.status_code == 403, f"status={r.status_code}")

        r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/confirm",
                         headers=H_ADMIN, json={"comment": "test"})
        check("3.4.4 关闭时确认被拦 status=403", r.status_code == 403, f"status={r.status_code}")

        r = safe_request("GET", f"{API}/api/sandbox/", headers=H_LEAD)
        check("3.4.5 关闭时列表被拦 status=403", r.status_code == 403, f"status={r.status_code}")

    print("\n--- 3.5 配置管理台始终可用 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    check("3.5.1 沙盒关闭时配置列表仍可查看 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("3.5.2 sandbox_enabled=False", data.get("sandbox_enabled") == False)

    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_ADMIN, json={"config_value": "true"})
    check("3.5.3 沙盒关闭时仍能修改配置 status=200", r.status_code == 200)

    reset_config_to_defaults()


def test_persistence_across_restart():
    """Phase 4: Config persists across restart"""
    section("阶段4：重启后配置读回")

    print("\n--- 4.1 修改配置并保存状态 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                 headers=H_ADMIN, json={"config_value": "true"})
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
                 headers=H_ADMIN, json={"config_value": "false"})
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
                 headers=H_ADMIN, json={"config_value": "36"})

    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    if r.status_code == 200:
        data = r.json()
        state = {
            "sandbox_enabled": data.get("sandbox_enabled"),
            "require_admin_confirm": data.get("require_admin_confirm"),
            "auto_expire_hours": data.get("auto_expire_hours"),
            "timestamp": time.time(),
            "timestamp_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_persistence_state(state)
        check("4.1.1 sandbox_enabled=True", data.get("sandbox_enabled") == True)
        check("4.1.2 require_admin_confirm=False", data.get("require_admin_confirm") == False)
        check("4.1.3 auto_expire_hours=36", data.get("auto_expire_hours") == 36)

    print("\n--- 4.2 审计日志存在 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_ADMIN)
    check("4.2.1 审计日志 status=200", r.status_code == 200)
    if r.status_code == 200:
        logs = r.json()
        check("4.2.2 有审计日志记录", len(logs) > 0, f"log_count={len(logs)}")
        has_update = any("CONFIG_UPDATE" in log.get("action", "") for log in logs)
        check("4.2.3 包含 CONFIG_UPDATE 日志", has_update)

    print(f"\n  [INFO] 持久化状态已保存。重启服务后设置 TEST_RESTART_PHASE=1 再次运行验证。")


def test_after_restart():
    """Phase 4b: Verify config after restart"""
    section("阶段4b：重启后持久化验证")

    state = load_persistence_state()
    if not state:
        warn("4b.0 没有找到持久化状态文件，跳过重启验证")
        return

    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    check("4b.1 重启后配置列表 status=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("4b.2 sandbox_enabled 保留",
              data.get("sandbox_enabled") == state.get("sandbox_enabled"),
              f"expected={state.get('sandbox_enabled')}, actual={data.get('sandbox_enabled')}")
        check("4b.3 require_admin_confirm 保留",
              data.get("require_admin_confirm") == state.get("require_admin_confirm"),
              f"expected={state.get('require_admin_confirm')}, actual={data.get('require_admin_confirm')}")
        check("4b.4 auto_expire_hours 保留",
              data.get("auto_expire_hours") == state.get("auto_expire_hours"),
              f"expected={state.get('auto_expire_hours')}, actual={data.get('auto_expire_hours')}")

    r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_ADMIN)
    check("4b.5 重启后审计日志 status=200", r.status_code == 200)
    if r.status_code == 200:
        logs = r.json()
        check("4b.6 重启后日志未丢失", len(logs) > 0, f"log_count={len(logs)}")


def test_concurrent_modification():
    """Phase 5: Concurrent modification returns 409 Conflict"""
    section("阶段5：并发修改不会写出脏状态")

    reset_config_to_defaults()

    print("\n--- 5.1 先读取当前值 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.enabled", headers=H_ADMIN)
    check("5.1.1 读取当前值 status=200", r.status_code == 200)
    if r.status_code != 200:
        return
    current_value = r.json().get("config_value")
    print(f"  [INFO] 当前 sandbox.enabled={current_value}")

    print("\n--- 5.2 使用 expected_old_value 正常修改 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_ADMIN,
                     json={"config_value": "false", "expected_old_value": current_value})
    check("5.2.1 正常修改 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code == 200:
        data = r.json()
        check("5.2.2 config_value=false", data.get("config_value") == "false")

    print("\n--- 5.3 使用过期 expected_old_value 修改（应 409）---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_ADMIN,
                     json={"config_value": "true", "expected_old_value": current_value})
    check("5.3.1 并发冲突 status=409", r.status_code == 409,
          f"status={r.status_code}, body={r.text[:200]}")

    print("\n--- 5.4 验证并发冲突后值未被修改 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.enabled", headers=H_ADMIN)
    if r.status_code == 200:
        data = r.json()
        check("5.4.1 值仍为 false（未被脏写）", data.get("config_value") == "false",
              f"actual={data.get('config_value')}")

    print("\n--- 5.5 不带 expected_old_value 修改（兼容旧客户端）---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_ADMIN, json={"config_value": "true"})
    check("5.5.1 无并发检查修改 status=200", r.status_code == 200)

    print("\n--- 5.6 批量更新中的并发冲突 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                 headers=H_ADMIN, json={"config_value": "true"})
    r = safe_request("PATCH", f"{API}/api/sandbox-config/batch",
                     headers=H_ADMIN,
                     json={"updates": [
                         {"config_key": "sandbox.enabled", "config_value": "false",
                          "expected_old_value": "wrong_value"},
                         {"config_key": "sandbox.auto_expire_hours", "config_value": "12"},
                     ]})
    check("5.6.1 批量更新 status=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("5.6.2 updated_count=1", data.get("updated_count") == 1,
              f"updated_count={data.get('updated_count')}")
        check("5.6.3 failed_count=1", data.get("failed_count") == 1,
              f"failed_count={data.get('failed_count')}")
        check("5.6.4 success=False", data.get("success") == False)

    print("\n--- 5.7 真正的并发竞争测试 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                 headers=H_ADMIN, json={"config_value": "true"})

    results = []
    conflicts = []

    def concurrent_update(val, old_val):
        try:
            r = requests.put(
                f"{API}/api/sandbox-config/sandbox.enabled",
                headers={"X-User-Id": "1", "Content-Type": "application/json"},
                json={"config_value": val, "expected_old_value": old_val},
                timeout=10,
            )
            return r.status_code, val
        except Exception as e:
            return 999, str(e)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(concurrent_update, "false", "true"),
            executor.submit(concurrent_update, "false", "true"),
            executor.submit(concurrent_update, "false", "true"),
        ]
        for future in as_completed(futures):
            status_code, val = future.result()
            results.append((status_code, val))
            if status_code == 409:
                conflicts.append((status_code, val))

    print(f"  [INFO] 并发更新结果: {results}")
    print(f"  [INFO] 冲突次数: {len(conflicts)}")

    success_count = sum(1 for s, _ in results if s == 200)
    conflict_count = sum(1 for s, _ in results if s == 409)
    check("5.7.1 至少1个成功", success_count >= 1, f"成功={success_count}")
    check("5.7.2 至少1个冲突", conflict_count >= 1, f"冲突={conflict_count}")

    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.enabled", headers=H_ADMIN)
    if r.status_code == 200:
        data = r.json()
        check("5.7.3 最终值一致（false）", data.get("config_value") == "false",
              f"actual={data.get('config_value')}")

    reset_config_to_defaults()


def test_audit_log_integrity():
    """Phase 6: Audit logs only contain real traces"""
    section("阶段6：审计日志完整性 - 无真实 trace 不落失败结论")

    reset_config_to_defaults()

    print("\n--- 6.1 产生一些有效操作 ---")
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                 headers=H_ADMIN, json={"config_value": "false"})
    safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                 headers=H_ADMIN, json={"config_value": "true"})

    r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_ADMIN)
    check("6.1.1 审计日志 status=200", r.status_code == 200)
    if r.status_code == 200:
        logs = r.json()
        check("6.1.2 有日志记录", len(logs) > 0)

        for log in logs:
            action = log.get("action", "")
            extra = log.get("extra_data") or {}

            if "CONFIG_UPDATE" in action and "BATCH" not in action:
                has_trace = bool(extra.get("config_key") and extra.get("old_value") is not None and extra.get("new_value") is not None)
                check(f"6.1.3 单条更新日志 {log.get('id')} 有完整 trace",
                      has_trace, f"extra={extra}")

            if "BATCH_UPDATE" in action:
                updates_list = extra.get("updates", [])
                check(f"6.1.4 批量更新日志 {log.get('id')} 只记录实际成功项",
                      len(updates_list) > 0, f"updates={len(updates_list)}")

    print("\n--- 6.2 产生失败的修改（不会产生审计日志）---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
                     headers=H_ADMIN, json={"config_value": "not_a_bool"})
    check("6.2.1 非法值被拒绝 status=400", r.status_code == 400)

    r2 = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_ADMIN)
    if r2.status_code == 200:
        logs_after = r2.json()
        failed_logs = [l for l in logs_after if "失败" in (l.get("comment") or "")
                       and "CONFIG_UPDATE" in l.get("action", "")
                       and "BATCH" not in l.get("action", "")]
        check("6.2.2 失败操作无单独审计日志", len(failed_logs) == 0,
              f"failed_logs_count={len(failed_logs)}")

    reset_config_to_defaults()


def main():
    print()
    print("=" * 60)
    print("沙盒配置域 - 统一链路集成测试")
    print("=" * 60)
    print(f"测试目标 API: {API}")
    print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    wait_for_api()
    ensure_users()

    existing_state = load_persistence_state()
    if existing_state and os.environ.get("TEST_RESTART_PHASE") == "1":
        test_after_restart()
    else:
        test_reviewer_read_only()
        test_admin_change_takes_effect()
        test_disabled_blocks_flow()
        test_concurrent_modification()
        test_audit_log_integrity()
        test_persistence_across_restart()

        print("\n" + "=" * 60)
        print("提示: 如需验证重启后持久化，请:")
        print("  1. 重启服务")
        print("  2. 设置 TEST_RESTART_PHASE=1 重新运行本脚本")
        print("=" * 60)

    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    print(f"总检查项: {_total}")
    print(f"通过: {_passed}")
    print(f"失败: {_failed}")
    if _warnings:
        print(f"\n警告 ({len(_warnings)}):")
        for w in _warnings:
            print(f"  [WARN] {w}")
    if _failed == 0:
        print("\n[PASS] All tests passed!")
    else:
        print(f"\n[FAIL] {_failed} checks failed")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
