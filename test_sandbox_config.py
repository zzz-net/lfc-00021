"""
沙盒配置管理台 完整验证测试

覆盖：
1. 配置列表可见性
2. 三项配置可改（sandbox.enabled, sandbox.require_admin_confirm, sandbox.auto_expire_hours）
3. 开关前后接口行为变化（关闭沙盒 -> 所有入口拒绝）
4. 冲突场景（白名单校验、类型校验、越权操作）
5. 重启后的持久化（配置 + 审计日志）
"""

import os
import sys
import json
import time
import io
import zipfile
import requests
from typing import Optional

API = os.environ.get("TEST_API_URL", "http://127.0.0.1:8003")

PERSIST_FILE = ".test_sandbox_config_persistence.json"

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
        r = safe_request("POST", f"{API}/api/users/",
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


def test_config_list_visibility():
    """阶段1：配置列表可见性 + 权限控制"""
    section("阶段1：配置列表可见性和权限")

    # 1.1 admin 可以查看
    print("\n--- 1.1 admin 查看配置列表 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    check("1.1.1 admin 查看配置 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("1.1.2 total=3", data.get("total") == 3, f"total={data.get('total')}")
        check("1.1.3 包含 sandbox_enabled 汇总字段", "sandbox_enabled" in data)
        check("1.1.4 包含 require_admin_confirm 汇总字段", "require_admin_confirm" in data)
        check("1.1.5 包含 auto_expire_hours 汇总字段", "auto_expire_hours" in data)
        keys = [item.get("config_key") for item in data.get("items", [])]
        check("1.1.6 包含 sandbox.enabled", "sandbox.enabled" in keys, f"keys={keys}")
        check("1.1.7 包含 sandbox.require_admin_confirm", "sandbox.require_admin_confirm" in keys)
        check("1.1.8 包含 sandbox.auto_expire_hours", "sandbox.auto_expire_hours" in keys)
        print(f"  [INFO] 配置列表 keys: {keys}")
        print(f"  [INFO] 汇总: enabled={data.get('sandbox_enabled')}, "
              f"require_admin={data.get('require_admin_confirm')}, "
              f"expire_hours={data.get('auto_expire_hours')}")

    # 1.2 reviewer 可以查看（只读）
    print("\n--- 1.2 reviewer 查看配置列表 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_REVIEWER)
    check("1.2.1 reviewer 查看配置 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("1.2.2 reviewer 看到 total=3", data.get("total") == 3)

    # 1.3 submitter 不能查看
    print("\n--- 1.3 submitter 查看配置列表（应拒绝） ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_SUBMITTER)
    check("1.3.1 submitter 查看被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 1.4 查看单条配置
    print("\n--- 1.4 查看单条配置详情 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/sandbox.enabled", headers=H_ADMIN)
    check("1.4.1 查看 sandbox.enabled status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("1.4.2 config_key 正确", data.get("config_key") == "sandbox.enabled")
        check("1.4.3 value_type=bool", data.get("value_type") == "bool")
        check("1.4.4 parsed_value 存在", "parsed_value" in data)

    # 1.5 查看不在白名单的配置
    print("\n--- 1.5 查看不在白名单的配置（应拒绝） ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/not.exist.key", headers=H_ADMIN)
    check("1.5.1 非白名单配置被拒绝 status!=200", r.status_code != 200, f"status={r.status_code}")

    # 1.6 资格检查接口
    print("\n--- 1.6 资格检查接口 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/eligibility", headers=H_ADMIN)
    check("1.6.1 admin eligibility status=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("1.6.2 admin can_view=True", data.get("can_view") == True)
        check("1.6.3 admin can_edit=True", data.get("can_edit") == True)

    r = safe_request("GET", f"{API}/api/sandbox-config/eligibility", headers=H_REVIEWER)
    if r.status_code == 200:
        data = r.json()
        check("1.6.4 reviewer can_view=True", data.get("can_view") == True)
        check("1.6.5 reviewer can_edit=False", data.get("can_edit") == False)


def test_config_update_and_validation():
    """阶段2：三项配置修改 + 白名单/类型校验"""
    section("阶段2：配置修改、白名单校验和类型校验")

    # 先备份当前配置
    print("\n--- 2.0 备份当前配置 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    backup = {}
    if r.status_code == 200:
        for item in r.json().get("items", []):
            backup[item["config_key"]] = item["config_value"]
    print(f"  [INFO] 备份配置: {backup}")

    # 2.1 修改 sandbox.enabled
    print("\n--- 2.1 修改 sandbox.enabled ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "false"})
    check("2.1.1 修改 enabled=false status=200", r.status_code == 200,
        f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code == 200:
        data = r.json()
        check("2.1.2 返回值 false", data.get("config_value") == "false")
        check("2.1.3 parsed_value=False", data.get("parsed_value") == False)

    # 2.2 修改 sandbox.require_admin_confirm
    print("\n--- 2.2 修改 sandbox.require_admin_confirm ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "false"})
    check("2.2.1 修改 require_admin=false status=200", r.status_code == 200,
        f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code == 200:
        data = r.json()
        check("2.2.2 返回值 false", data.get("config_value") == "false")

    # 2.3 修改 sandbox.auto_expire_hours
    print("\n--- 2.3 修改 sandbox.auto_expire_hours ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "48"})
    check("2.3.1 修改 expire_hours=48 status=200", r.status_code == 200,
        f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code == 200:
        data = r.json()
        check("2.3.2 返回值 48", data.get("config_value") == "48")
        check("2.3.3 parsed_value=48", data.get("parsed_value") == 48)

    # 2.4 验证修改后汇总字段
    print("\n--- 2.4 验证修改后汇总字段 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    if r.status_code == 200:
        data = r.json()
        check("2.4.1 sandbox_enabled=False", data.get("sandbox_enabled") == False)
        check("2.4.2 require_admin_confirm=False", data.get("require_admin_confirm") == False)
        check("2.4.3 auto_expire_hours=48", data.get("auto_expire_hours") == 48)

    # 2.5 reviewer 不能修改
    print("\n--- 2.5 reviewer 尝试修改（应拒绝） ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_REVIEWER, json={"config_value": "true"})
    check("2.5.1 reviewer 修改被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 2.6 lead 不能修改
    print("\n--- 2.6 lead 尝试修改（应拒绝） ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_LEAD, json={"config_value": "true"})
    check("2.6.1 lead 修改被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 2.7 白名单校验 - 修改不在白名单的配置
    print("\n--- 2.7 白名单校验：修改非白名单配置 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/not.in.whitelist",
        headers=H_ADMIN, json={"config_value": "true"})
    check("2.7.1 非白名单配置修改被拒绝 status!=200", r.status_code != 200, f"status={r.status_code}")

    # 2.8 类型校验 - bool 类型输入非法值
    print("\n--- 2.8 类型校验：bool 类型输入非法值 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "not_a_bool"})
    check("2.8.1 bool 非法值被拒绝 status=400", r.status_code == 400, f"status={r.status_code}")

    # 2.9 类型校验 - int 类型输入非法值
    print("\n--- 2.9 类型校验：int 类型输入非法值 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "not_an_int"})
    check("2.9.1 int 非法值被拒绝 status=400", r.status_code == 400, f"status={r.status_code}")

    # 2.10 类型校验 - int 超出范围
    print("\n--- 2.10 类型校验：int 超出范围 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "0"})
    check("2.10.1 int 最小值=1，0 被拒绝 status=400", r.status_code == 400, f"status={r.status_code}")

    # 2.11 批量修改
    print("\n--- 2.11 批量修改配置 ---")
    r = safe_request("PATCH", f"{API}/api/sandbox-config/batch",
        headers=H_ADMIN,
        json={"updates": {
            "sandbox.enabled": "true",
            "sandbox.require_admin_confirm": "true",
            "sandbox.auto_expire_hours": "24",
        }})
    check("2.11.1 批量修改 status=200", r.status_code == 200,
        f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code == 200:
        data = r.json()
        check("2.11.2 批量修改 updated_count=3", data.get("updated_count") == 3,
            f"updated_count={data.get('updated_count')}")
        check("2.11.3 批量修改 success=True", data.get("success") == True)

    # 2.12 批量修改部分失败场景
    print("\n--- 2.12 批量修改：部分非法值场景 ---")
    r = safe_request("PATCH", f"{API}/api/sandbox-config/batch",
        headers=H_ADMIN,
        json={"updates": {
            "sandbox.enabled": "true",
            "sandbox.auto_expire_hours": "invalid",
            "not.in.whitelist": "true",
        }})
    check("2.12.1 部分失败 status=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("2.12.2 updated_count=1", data.get("updated_count") == 1,
            f"updated_count={data.get('updated_count')}")
        check("2.12.3 failed_count=2", data.get("failed_count") == 2,
            f"failed_count={data.get('failed_count')}")
        check("2.12.4 success=False", data.get("success") == False)

    # 2.13 bool 值多种写法归一化
    print("\n--- 2.13 bool 值多种写法归一化（yes/on/1 -> true） ---")
    for test_val in ["yes", "ON", "1", "True"]:
        r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
            headers=H_ADMIN, json={"config_value": test_val})
        if r.status_code == 200:
            data = r.json()
            check(f"2.13.{test_val} 归一化为 true", data.get("config_value") == "true",
                f"input={test_val}, output={data.get('config_value')}")

    for test_val in ["no", "OFF", "0", "False"]:
        r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
            headers=H_ADMIN, json={"config_value": test_val})
        if r.status_code == 200:
            data = r.json()
            check(f"2.13.{test_val} 归一化为 false", data.get("config_value") == "false",
                f"input={test_val}, output={data.get('config_value')}")


def create_test_archive() -> bytes:
    """创建一个符合 ArchiveManifest schema 的测试归档 ZIP"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "format_version": "1.0.0",
            "archive_id": f"test-config-{int(time.time())}",
            "batch_code": f"CFG-TEST-{int(time.time())}",
            "batch_id_original": None,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generated_by_user_id": 1,
            "generated_by_username": "admin",
            "source_api_version": "1.0.0",
            "sections": ["batch", "versions", "items"],
            "total_bytes": 0,
            "item_counts": {"versions": 1, "items": 1},
            "notes": "sandbox config test",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        batch_data = {
            "batch_code": manifest["batch_code"],
            "name": "配置管理测试批次",
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


def test_sandbox_switch_behavior():
    """阶段3：沙盒开关前后接口行为变化"""
    section("阶段3：沙盒开关前后接口行为变化")

    # 3.0 先确保沙盒开启，测试沙盒入口不被 403 拒绝
    print("\n--- 3.0 先开启沙盒，沙盒入口不被拒绝 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "true"})
    assert r.status_code == 200, "开启沙盒失败"

    archive_bytes, batch_code = create_test_archive()
    files = {"file": ("test.zip", archive_bytes, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files)
    check("3.0.1 开启时恢复归档不是 403（沙盒入口可用）", r.status_code != 403,
        f"status={r.status_code}, body={r.text[:200]}")

    sandbox_token = None
    if r.status_code == 200:
        sandbox_token = r.json().get("sandbox_token")
        print(f"  [INFO] 创建沙盒会话: token={sandbox_token[:16]}...")

    # 3.1 关闭沙盒
    print("\n--- 3.1 关闭沙盒功能 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "false"})
    check("3.1.1 关闭沙盒 status=200", r.status_code == 200)

    # 3.2 关闭时，恢复归档被拒绝
    print("\n--- 3.2 关闭沙盒：恢复归档被拒绝 ---")
    archive_bytes2, _ = create_test_archive()
    files2 = {"file": ("test2.zip", archive_bytes2, "application/zip")}
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files2)
    check("3.2.1 关闭时恢复归档被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 3.3 关闭时，所有已有沙盒会话的操作都被拒绝
    if sandbox_token:
        print("\n--- 3.3 关闭沙盒：已有会话操作被拒绝 ---")

        # 导入候选版本
        r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/import",
            headers=H_LEAD,
            files={"file": ("v2.csv", b"item_id,item_name,quantity,unit_price\n", "text/csv")},
            data={"import_format": "auto"})
        check("3.3.1 关闭时导入被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

        # 查看差异
        r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}/diff", headers=H_LEAD)
        check("3.3.2 关闭时查看差异被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

        # 预检查
        r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/precheck", headers=H_LEAD)
        check("3.3.3 关闭时预检查被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

        # 最终确认
        r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/confirm",
            headers=H_ADMIN, json={"comment": "test"})
        check("3.3.4 关闭时确认被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

        # 拒绝
        r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token}/reject",
            headers=H_ADMIN, json={"reason": "test"})
        check("3.3.5 关闭时拒绝被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

        # 查看列表
        r = safe_request("GET", f"{API}/api/sandbox/", headers=H_LEAD)
        check("3.3.6 关闭时查看列表被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

        # 查看详情
        r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}", headers=H_LEAD)
        check("3.3.7 关闭时查看详情被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 3.4 配置管理台始终可用（不依赖 sandbox.enabled）
    print("\n--- 3.4 配置管理台始终可用（不依赖 sandbox.enabled） ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    check("3.4.1 沙盒关闭时配置列表仍可查看 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("3.4.2 关闭时 sandbox_enabled=False", data.get("sandbox_enabled") == False)

    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "true"})
    check("3.4.3 沙盒关闭时仍能修改配置 status=200", r.status_code == 200)

    # 3.5 重新开启沙盒后操作恢复正常（入口不再被 403 拒绝）
    print("\n--- 3.5 重新开启沙盒，操作恢复正常 ---")
    r = safe_request("POST", f"{API}/api/sandbox/restore", headers=H_LEAD, files=files2)
    check("3.5.1 开启后恢复归档不是 403（入口恢复）", r.status_code != 403, f"status={r.status_code}")


def test_persistence_and_audit_logs():
    """阶段4：持久化（重启后配置+日志仍在）"""
    section("阶段4：持久化验证 - 配置和审计日志")

    # 4.1 修改配置并记录状态
    print("\n--- 4.1 修改配置并保存状态 ---")
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.enabled",
        headers=H_ADMIN, json={"config_value": "true"})
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.require_admin_confirm",
        headers=H_ADMIN, json={"config_value": "true"})
    r = safe_request("PUT", f"{API}/api/sandbox-config/sandbox.auto_expire_hours",
        headers=H_ADMIN, json={"config_value": "36"})

    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    if r.status_code == 200:
        data = r.json()
        state = {
            "sandbox_enabled": data.get("sandbox_enabled"),
            "require_admin_confirm": data.get("require_admin_confirm"),
            "auto_expire_hours": data.get("auto_expire_hours"),
            "timestamp": time.time(),
            "timestamp_human": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        save_persistence_state(state)

    # 4.2 查看审计日志（修改前至少有几条）
    print("\n--- 4.2 查看配置审计日志 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_ADMIN)
    check("4.2.1 审计日志 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        logs = r.json()
        check("4.2.2 有审计日志记录", len(logs) > 0, f"log_count={len(logs)}")
        actions = set(log.get("action") for log in logs)
        print(f"  [INFO] 审计日志条数: {len(logs)}, actions: {actions}")
        # 检查是否有配置变更日志
        has_update = any("CONFIG_UPDATE" in log.get("action", "") for log in logs)
        check("4.2.3 包含 CONFIG_UPDATE 日志", has_update)
        has_view = any("CONFIG_VIEW" in log.get("action", "") for log in logs)
        check("4.2.4 包含 CONFIG_VIEW 日志", has_view)

        if logs:
            latest = logs[0]
            check("4.2.5 日志包含 actor_username", latest.get("actor_username") is not None)
            check("4.2.6 日志包含 created_at", latest.get("created_at") is not None)

    # 4.3 reviewer 也可以查看审计日志
    print("\n--- 4.3 reviewer 查看审计日志 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_REVIEWER)
    check("4.3.1 reviewer 查看审计日志 status=200", r.status_code == 200, f"status={r.status_code}")

    # 4.4 submitter 不能查看审计日志
    print("\n--- 4.4 submitter 查看审计日志（应拒绝） ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_SUBMITTER)
    check("4.4.1 submitter 查看审计日志被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 4.5 保存持久化状态供重启后验证
    print("\n--- 4.5 持久化状态已保存，等待服务重启验证 ---")
    state = load_persistence_state()
    if state:
        print(f"  [INFO] 将在重启后验证: {json.dumps(state, ensure_ascii=False)}")


def test_after_restart_persistence():
    """阶段5：重启后验证"""
    section("阶段5：重启后持久化验证")

    state = load_persistence_state()
    if not state:
        warn("5.0 没有找到持久化状态文件，跳过重启验证")
        return

    # 5.1 重启后验证配置值
    print("\n--- 5.1 重启后验证配置值 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/", headers=H_ADMIN)
    check("5.1.1 重启后配置列表 status=200", r.status_code == 200)
    if r.status_code == 200:
        data = r.json()
        check("5.1.2 sandbox_enabled 保留",
            data.get("sandbox_enabled") == state.get("sandbox_enabled"),
            f"expected={state.get('sandbox_enabled')}, actual={data.get('sandbox_enabled')}")
        check("5.1.3 require_admin_confirm 保留",
            data.get("require_admin_confirm") == state.get("require_admin_confirm"),
            f"expected={state.get('require_admin_confirm')}, actual={data.get('require_admin_confirm')}")
        check("5.1.4 auto_expire_hours 保留",
            data.get("auto_expire_hours") == state.get("auto_expire_hours"),
            f"expected={state.get('auto_expire_hours')}, actual={data.get('auto_expire_hours')}")

    # 5.2 重启后验证审计日志保留
    print("\n--- 5.2 重启后验证审计日志保留 ---")
    r = safe_request("GET", f"{API}/api/sandbox-config/audit-logs", headers=H_ADMIN)
    check("5.2.1 重启后审计日志 status=200", r.status_code == 200)
    if r.status_code == 200:
        logs = r.json()
        check("5.2.2 重启后日志未丢失", len(logs) > 0, f"log_count={len(logs)}")
        print(f"  [INFO] 重启后审计日志条数: {len(logs)}")


def main():
    print()
    print("=" * 60)
    print("沙盒配置管理台 - 完整验证测试")
    print("=" * 60)
    print(f"测试目标 API: {API}")
    print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    wait_for_api()
    ensure_users()

    # 如果有持久化状态，说明是重启后的第二次运行，直接跑重启后验证
    existing_state = load_persistence_state()
    if existing_state and os.environ.get("TEST_RESTART_PHASE") == "1":
        test_after_restart_persistence()
    else:
        # 正常完整流程
        test_config_list_visibility()
        test_config_update_and_validation()
        test_sandbox_switch_behavior()
        test_persistence_and_audit_logs()

        print("\n" + "=" * 60)
        print("提示: 如需验证重启后持久化，请:")
        print("  1. 重启服务")
        print("  2. 设置 TEST_RESTART_PHASE=1 重新运行本脚本")
        print("=" * 60)

    # 汇总
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
        print("\n🎉 所有测试通过！")
    else:
        print(f"\n❌ 有 {_failed} 项检查失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
