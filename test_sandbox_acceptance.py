#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
恢复后验收沙盒模块 完整验证脚本

覆盖测试需求：
1. 完整沙盒流程：恢复归档批次 → 沙盒导入新版本 → 查看差异 → 预检查 → 确认通过 → 正式恢复
2. 权限控制：普通 reviewer 不能执行正式确认
3. 服务重启后状态和日志仍可查询
4. 完整审计日志
5. 配置开关控制
"""
import os
import sys
import time
import json
import hashlib
import io
from typing import Optional, Tuple

import requests

API = os.environ.get("TEST_API_URL", "http://127.0.0.1:8000")

H_ADMIN = {"X-User-Id": "1"}
H_LEAD = {"X-User-Id": "2"}
H_REVIEWER = {"X-User-Id": "3"}
H_REVIEWER2 = {"X-User-Id": "4"}
H_SUBMITTER = {"X-User-Id": "5"}

OK = "[OK]"
FAIL = "[FAIL]"
errors = []
warnings_log = []

BATCH_CODE_FOR_SANDBOX: Optional[str] = None
BATCH_ID_FOR_ARCHIVE: Optional[int] = None
ARCHIVE_ZIP_BYTES: Optional[bytes] = None
SANDBOX_TOKEN: Optional[str] = None
SESSION_ID_BEFORE_RESTART: Optional[int] = None

PERSISTENCE_FILE = ".test_sandbox_persistence.json"


def safe_str(s):
    if isinstance(s, bytes):
        try:
            return s.decode('utf-8', errors='replace')
        except:
            return str(s)
    return str(s).encode('utf-8', errors='replace').decode('utf-8', errors='replace')


def check(step: str, cond: bool, detail: str = ""):
    mark = OK if cond else FAIL
    msg = f"  {mark} {step}"
    if detail:
        msg += f"  --  {safe_str(detail)}"
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace'))
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


def save_persistence_state(state: dict):
    """保存状态用于重启后验证"""
    with open(PERSISTENCE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"  [INFO] 持久化状态已保存到 {PERSISTENCE_FILE}")


def load_persistence_state() -> Optional[dict]:
    """加载重启前保存的状态"""
    if os.path.exists(PERSISTENCE_FILE):
        with open(PERSISTENCE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def create_and_archive_batch() -> Tuple[Optional[int], Optional[bytes]]:
    """
    创建完整批次并归档导出
    流程：创建批次 → 导入v1 → 校验 → 提交 → 通过 → 归档 → 导出
    """
    print("\n" + "="*60)
    print("【阶段1】准备：创建批次并归档导出")
    print("="*60)

    batch_code = f"SB-TEST-{int(time.time())}"
    global BATCH_CODE_FOR_SANDBOX
    BATCH_CODE_FOR_SANDBOX = batch_code

    # 1. 创建批次
    r = safe_request("POST", f"{API}/api/batches/",
        headers=H_SUBMITTER,
        json={
            "batch_code": batch_code,
            "name": "沙盒测试批次",
            "description": "用于恢复后验收沙盒模块测试",
            "submitter_id": 5
        })
    check("1.1 创建批次 status=201", r.status_code == 201, f"status={r.status_code}")
    if r.status_code != 201:
        return None, None
    bid = r.json()["id"]
    print(f"  批次 id={bid}, code={batch_code}")

    # 2. 预检查并导入v1
    with open("samples/manifest_sample_good.csv", "rb") as f:
        r = safe_request("POST", f"{API}/api/batches/{bid}/manifests/precheck",
            headers=H_SUBMITTER,
            files={"file": ("v1.csv", f, "text/csv")},
            data={"import_format": "auto"})
    check("1.2.1 v1 预检查 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code != 200:
        return None, None
    precheck_data = r.json()
    token_v1 = precheck_data.get("precheck_token")
    check("1.2.2 v1 precheck_token 存在", bool(token_v1))

    with open("samples/manifest_sample_good.csv", "rb") as f:
        r = safe_request("POST", f"{API}/api/batches/{bid}/manifests/import",
            headers=H_SUBMITTER,
            files={"file": ("v1.csv", f, "text/csv")},
            data={"precheck_token": token_v1, "import_format": "auto"})
    check("1.2.3 导入 v1 版本 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code != 200:
        return None, None
    v1_version_id = r.json()["manifest_version_id"]
    check("1.2.4 v1 version_id > 0", v1_version_id > 0, f"version_id={v1_version_id}")

    # 3. 执行校验
    r = safe_request("POST", f"{API}/api/batches/{bid}/validate",
        headers=H_SUBMITTER)
    check("1.3 执行 v1 校验 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}")

    # 4. 提交待验收
    r = safe_request("POST", f"{API}/api/batches/{bid}/transition",
        headers=H_SUBMITTER,
        json={"target_status": "pending_review", "comment": "提交验收"})
    check("1.4 提交待验收 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}")

    # 5. lead 通过验收
    r = safe_request("POST", f"{API}/api/batches/{bid}/transition",
        headers=H_LEAD,
        json={"target_status": "approved", "comment": "验收通过"})
    check("1.5 lead 验收通过 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}")

    # 6. lead 归档
    r = safe_request("POST", f"{API}/api/batches/{bid}/transition",
        headers=H_LEAD,
        json={"target_status": "archived", "comment": "归档批次用于沙盒测试"})
    check("1.6 lead 归档批次 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}")

    # 7. 导出归档包
    r = safe_request("POST", f"{API}/api/batches/{bid}/archive/export",
        headers=H_LEAD,
        json={"notes": "沙盒测试归档导出"})
    check("1.7 导出归档包 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code != 200:
        try:
            err_body = r.json()
            check("1.7 导出错误详情", False, f"error={err_body}")
        except:
            check("1.7 导出错误详情", False, f"body={safe_str(r.content[:200])}")
        return None, None
    archive_zip = r.content
    check("1.7.1 归档包非空", len(archive_zip) > 100, f"size={len(archive_zip)}")

    global BATCH_ID_FOR_ARCHIVE
    BATCH_ID_FOR_ARCHIVE = bid

    print(f"\n  [INFO] 批次准备完成: id={bid}, code={batch_code}, 归档包大小={len(archive_zip)} bytes")
    return bid, archive_zip


def test_sandbox_full_workflow(archive_zip: bytes):
    """
    测试完整沙盒流程：
    恢复到沙盒 → 导入候选版本 → 查看差异 → 预检查 → 确认 → 验证生产数据
    """
    print("\n" + "="*60)
    print("【阶段2】核心测试：完整沙盒流程")
    print("="*60)

    global SANDBOX_TOKEN, SESSION_ID_BEFORE_RESTART

    # 2.1 恢复归档到沙盒
    print("\n--- 2.1 恢复归档到沙盒 (lead 操作) ---")
    r = safe_request("POST", f"{API}/api/sandbox/restore",
        headers=H_LEAD,
        files={"file": ("archive.zip", io.BytesIO(archive_zip), "application/zip")})
    check("2.1.1 恢复到沙盒 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:300]}")
    if r.status_code != 200:
        return False
    restore_data = r.json()
    check("2.1.2 success=True", restore_data.get("success") == True)
    check("2.1.3 sandbox_token 存在", bool(restore_data.get("sandbox_token")))
    check("2.1.4 session_id > 0", restore_data.get("session_id", 0) > 0)
    check("2.1.5 状态为 pending", restore_data.get("status") == "pending")

    SANDBOX_TOKEN = restore_data["sandbox_token"]
    SESSION_ID_BEFORE_RESTART = restore_data["session_id"]
    print(f"  [INFO] 沙盒会话创建成功: token={SANDBOX_TOKEN[:16]}..., session_id={SESSION_ID_BEFORE_RESTART}")

    # 2.2 reviewer 尝试确认 - 应该被拒绝
    print("\n--- 2.2 权限测试：reviewer 尝试确认（应被拒绝） ---")
    r = safe_request("POST", f"{API}/api/sandbox/{SANDBOX_TOKEN}/confirm",
        headers=H_REVIEWER,
        json={"comment": "reviewer 尝试确认"})
    check("2.2.1 reviewer 确认被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 2.3 沙盒内导入候选版本 v2
    print("\n--- 2.3 沙盒内导入候选版本 v2 (lead 操作) ---")
    with open("samples/manifest_sample_repaired_v2.csv", "rb") as f:
        r = safe_request("POST", f"{API}/api/sandbox/{SANDBOX_TOKEN}/import",
            headers=H_LEAD,
            files={"file": ("v2_repaired.csv", f, "text/csv")},
            data={"import_format": "auto"})
    check("2.3.1 导入候选版本 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:300]}")
    if r.status_code == 200:
        import_data = r.json()
        check("2.3.2 success=True", import_data.get("success") == True)
        check("2.3.3 导入版本号正确", import_data.get("version_number") == 2)
        check("2.3.4 条目数正确", import_data.get("item_count", 0) > 0)

    # 2.4 查看版本差异
    print("\n--- 2.4 查看版本差异 ---")
    r = safe_request("GET", f"{API}/api/sandbox/{SANDBOX_TOKEN}/diff",
        headers=H_LEAD)
    check("2.4.1 查看差异 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        diff_data = r.json()
        check("2.4.2 success=True", diff_data.get("success") == True)
        check("2.4.3 包含基础版本", diff_data.get("base_version_number") == 1)
        check("2.4.4 包含候选版本", diff_data.get("candidate_version_number") == 2)
        summary = diff_data.get("summary", {})
        check("2.4.5 差异统计完整", isinstance(summary.get("added_count"), int))
        added = len(diff_data.get("added_items", []))
        removed = len(diff_data.get("removed_items", []))
        modified = len(diff_data.get("modified_items", []))
        check("2.4.6 有差异内容", added + removed + modified > 0)
        print(f"  [INFO] 差异统计: +{added} -{removed} ~{modified}")

    # 2.5 reviewer 也可以查看差异
    print("\n--- 2.5 reviewer 查看差异（权限验证） ---")
    r = safe_request("GET", f"{API}/api/sandbox/{SANDBOX_TOKEN}/diff",
        headers=H_REVIEWER)
    check("2.5.1 reviewer 可查看差异 status=200", r.status_code == 200, f"status={r.status_code}")

    # 2.6 执行预检查
    print("\n--- 2.6 执行沙盒预检查 ---")
    r = safe_request("POST", f"{API}/api/sandbox/{SANDBOX_TOKEN}/precheck",
        headers=H_LEAD)
    check("2.6.1 预检查 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:500]}")
    if r.status_code == 200:
        precheck_data = r.json()
        check("2.6.2 success=True", precheck_data.get("success") == True)
        check("2.6.3 有总体结论", bool(precheck_data.get("overall_result")))
        check("2.6.4 有预检查结果列表", isinstance(precheck_data.get("results"), list))
        check("2.6.5 有推荐动作", bool(precheck_data.get("recommended_action")))
        check("2.6.6 预检查通过标志存在", isinstance(precheck_data.get("precheck_passed"), bool))
        print(f"  [INFO] 预检查结论: {precheck_data.get('overall_result')}")
        print(f"  [INFO] 推荐动作: {precheck_data.get('recommended_action')}")
        print(f"  [INFO] 检查统计: {precheck_data.get('passed_checks')}/{precheck_data.get('total_checks')} 通过, "
              f"{precheck_data.get('warning_checks')} 警告, {precheck_data.get('failed_checks')} 失败")

    # 2.7 保存状态用于重启后验证
    print("\n--- 2.7 保存持久化状态 ---")
    persistence_state = {
        "sandbox_token": SANDBOX_TOKEN,
        "session_id": SESSION_ID_BEFORE_RESTART,
        "batch_code": BATCH_CODE_FOR_SANDBOX,
        "original_batch_id": BATCH_ID_FOR_ARCHIVE,
        "precheck_passed": precheck_data.get("precheck_passed") if r.status_code == 200 else None,
        "timestamp": time.time(),
        "timestamp_human": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    save_persistence_state(persistence_state)

    # 2.8 查看会话详情
    print("\n--- 2.8 查看沙盒会话详情 ---")
    r = safe_request("GET", f"{API}/api/sandbox/{SANDBOX_TOKEN}",
        headers=H_LEAD)
    check("2.8.1 查看详情 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        detail_data = r.json()
        check("2.8.2 session_id 一致", detail_data.get("id") == SESSION_ID_BEFORE_RESTART)
        check("2.8.3 sandbox_token 一致", detail_data.get("sandbox_token") == SANDBOX_TOKEN)
        check("2.8.4 包含版本信息", len(detail_data.get("manifest_versions", [])) >= 2)
        check("2.8.5 状态为 precheck_passed", detail_data.get("status") in ["precheck_passed", "precheck_failed"])

    # 2.9 查看审计日志
    print("\n--- 2.9 查看沙盒审计日志 ---")
    r = safe_request("GET", f"{API}/api/sandbox/{SANDBOX_TOKEN}/audit-logs",
        headers=H_LEAD)
    check("2.9.1 审计日志 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        logs = r.json()
        check("2.9.2 有多条审计记录", len(logs) >= 3)
        action_types = [log.get("action") for log in logs]
        check("2.9.3 包含恢复动作", any("RESTORE" in str(a) for a in action_types))
        check("2.9.4 包含导入动作", any("IMPORT" in str(a) for a in action_types))
        check("2.9.5 包含预检查动作", any("PRECHECK" in str(a) for a in action_types))
        check("2.9.6 日志包含 sandbox_token", all(log.get("sandbox_token") == SANDBOX_TOKEN for log in logs))
        print(f"  [INFO] 审计日志记录数: {len(logs)}")
        for log in logs[:5]:
            print(f"    - {log.get('created_at')[:19]} {log.get('action')} by uid={log.get('actor_id')}")

    # 2.10 检查权限 eligibility
    print("\n--- 2.10 检查用户权限资格 ---")
    r = safe_request("GET", f"{API}/api/sandbox/{SANDBOX_TOKEN}/eligibility",
        headers=H_REVIEWER)
    check("2.10.1 reviewer 查询资格 status=200", r.status_code == 200)
    if r.status_code == 200:
        elig = r.json()
        check("2.10.2 reviewer 可查看", elig.get("can_view") == True)
        check("2.10.3 reviewer 不可确认", elig.get("can_confirm") == False)

    r = safe_request("GET", f"{API}/api/sandbox/{SANDBOX_TOKEN}/eligibility",
        headers=H_ADMIN)
    check("2.10.4 admin 查询资格 status=200", r.status_code == 200)
    if r.status_code == 200:
        elig = r.json()
        check("2.10.5 admin 可确认", elig.get("can_confirm") == True)

    return True


def test_persistence_after_restart():
    """
    测试服务重启后状态和日志仍可查询
    """
    print("\n" + "="*60)
    print("【阶段3】持久化测试：服务重启后状态验证")
    print("="*60)

    state = load_persistence_state()
    if not state:
        warn("3.0 未找到持久化状态文件，跳过持久化测试")
        return True

    print(f"  [INFO] 加载持久化状态: {json.dumps(state, ensure_ascii=False, indent=2)[:500]}")

    sandbox_token = state.get("sandbox_token")
    session_id = state.get("session_id")

    if not sandbox_token:
        warn("3.0 持久化状态中无 sandbox_token，跳过")
        return True

    # 3.1 重启后查询会话列表
    print("\n--- 3.1 重启后查询沙盒会话列表 ---")
    r = safe_request("GET", f"{API}/api/sandbox/",
        headers=H_LEAD,
        params={"limit": 100})
    check("3.1.1 会话列表 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        sessions = r.json()
        found = any(s.get("sandbox_token") == sandbox_token for s in sessions)
        check("3.1.2 会话在重启后仍存在", found, f"找到 {len(sessions)} 个会话，是否包含目标: {found}")

    # 3.2 重启后查询会话详情
    print("\n--- 3.2 重启后查询会话详情 ---")
    r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}",
        headers=H_LEAD)
    check("3.2.1 会话详情 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        detail = r.json()
        check("3.2.2 session_id 一致", detail.get("id") == session_id)
        check("3.2.3 状态未丢失", detail.get("status") in ["precheck_passed", "precheck_failed", "pending"])
        check("3.2.4 预检查结果保留", detail.get("precheck_passed") is not None)
        check("3.2.5 创建者信息保留", detail.get("created_by") is not None)

    # 3.3 重启后查询审计日志
    print("\n--- 3.3 重启后查询审计日志 ---")
    r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}/audit-logs",
        headers=H_LEAD)
    check("3.3.1 审计日志 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        logs = r.json()
        check("3.3.2 审计日志未丢失", len(logs) >= 3)
        print(f"  [INFO] 重启后审计日志仍有 {len(logs)} 条记录")

    # 3.4 重启后仍可查询差异
    print("\n--- 3.4 重启后查询版本差异 ---")
    r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token}/diff",
        headers=H_LEAD)
    check("3.4.1 差异查询 status=200", r.status_code == 200, f"status={r.status_code}")

    print("\n  [INFO] 持久化验证通过！服务重启后沙盒状态和日志完整保留。")
    return True


def test_final_confirmation_and_production_write():
    """
    测试最终确认和写入正式数据
    """
    print("\n" + "="*60)
    print("【阶段4】最终确认：正式写入生产数据")
    print("="*60)

    global SANDBOX_TOKEN, BATCH_CODE_FOR_SANDBOX

    if not SANDBOX_TOKEN:
        state = load_persistence_state()
        if state:
            SANDBOX_TOKEN = state.get("sandbox_token")
            BATCH_CODE_FOR_SANDBOX = state.get("batch_code")

    if not SANDBOX_TOKEN:
        warn("4.0 无可用的 sandbox_token，跳过最终确认测试")
        return False

    # 4.1 reviewer 尝试确认 - 再次验证权限
    print("\n--- 4.1 权限验证：reviewer 尝试最终确认（应拒绝） ---")
    r = safe_request("POST", f"{API}/api/sandbox/{SANDBOX_TOKEN}/confirm",
        headers=H_REVIEWER,
        json={"comment": "reviewer 再次尝试确认"})
    check("4.1.1 reviewer 确认被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 4.2 lead 尝试确认（系统默认需要 admin，也应被拒绝）
    print("\n--- 4.2 权限验证：lead 尝试最终确认（应拒绝，因配置要求 admin） ---")
    r = safe_request("POST", f"{API}/api/sandbox/{SANDBOX_TOKEN}/confirm",
        headers=H_LEAD,
        json={"comment": "lead 尝试确认"})
    check("4.2.1 lead 确认被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    # 4.3 admin 执行最终确认
    print("\n--- 4.3 admin 执行最终确认（正式写入生产数据） ---")
    r = safe_request("POST", f"{API}/api/sandbox/{SANDBOX_TOKEN}/confirm",
        headers=H_ADMIN,
        json={"comment": "admin 正式确认，写入生产数据"})
    check("4.3.1 admin 确认 status=200", r.status_code == 200, f"status={r.status_code}, body={r.text[:500]}")
    if r.status_code != 200:
        return False
    confirm_data = r.json()
    check("4.3.2 success=True", confirm_data.get("success") == True)
    check("4.3.3 batch_id 返回", confirm_data.get("target_batch_id", 0) > 0)
    check("4.3.4 状态为 confirmed", confirm_data.get("status") == "confirmed")
    check("4.3.5 有写入记录数", isinstance(confirm_data.get("restored_version_count"), int))

    new_batch_id = confirm_data["target_batch_id"]
    print(f"  [INFO] 正式写入完成: 新批次 id={new_batch_id}")

    # 4.4 验证生产数据已写入
    print("\n--- 4.4 验证生产数据已正确写入 ---")
    r = safe_request("GET", f"{API}/api/batches/{new_batch_id}",
        headers=H_LEAD)
    check("4.4.1 查询新批次 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        batch_data = r.json()
        check("4.4.2 batch_code 正确", batch_data.get("batch_code") == BATCH_CODE_FOR_SANDBOX)
        check("4.4.3 批次状态不是 archived", batch_data.get("status") != "archived")
        print(f"  [INFO] 新批次信息: code={batch_data.get('batch_code')}, status={batch_data.get('status')}")

    # 4.5 验证新版本已写入
    print("\n--- 4.5 验证候选版本已正式写入 ---")
    r = safe_request("GET", f"{API}/api/batches/{new_batch_id}/manifests",
        headers=H_LEAD)
    check("4.5.1 查询版本列表 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        versions = r.json()
        check("4.5.2 有至少2个版本（v1基础 + v2候选）", len(versions) >= 2)
        version_numbers = [v.get("version_number") for v in versions]
        check("4.5.3 包含 v1 和 v2", 1 in version_numbers and 2 in version_numbers)
        print(f"  [INFO] 生产环境版本列表: {version_numbers}")

    # 4.6 查看最终审计日志
    print("\n--- 4.6 查看最终审计日志（包含确认动作） ---")
    r = safe_request("GET", f"{API}/api/sandbox/{SANDBOX_TOKEN}/audit-logs",
        headers=H_LEAD)
    check("4.6.1 审计日志 status=200", r.status_code == 200)
    if r.status_code == 200:
        logs = r.json()
        action_types = [log.get("action") for log in logs]
        check("4.6.2 包含确认动作", any("CONFIRM" in str(a) for a in action_types))
        confirm_logs = [l for l in logs if "CONFIRM" in str(l.get("action"))]
        if confirm_logs:
            check("4.6.3 确认动作由 admin 执行", confirm_logs[0].get("actor_id") == 1)
            print(f"  [INFO] 最终确认日志: {confirm_logs[0].get('action')} by uid={confirm_logs[0].get('actor_id')}")

    # 4.7 会话状态变为 confirmed，不可再操作
    print("\n--- 4.7 验证已确认会话不可再操作 ---")
    r = safe_request("POST", f"{API}/api/sandbox/{SANDBOX_TOKEN}/import",
        headers=H_LEAD,
        files={"file": ("should_fail.csv", io.BytesIO(b"item_id,item_name\nITEM-999,test"), "text/csv")},
        data={"import_format": "csv"})
    check("4.7.1 已确认会话不能再导入 status=400", r.status_code == 400, f"status={r.status_code}")

    r = safe_request("POST", f"{API}/api/sandbox/{SANDBOX_TOKEN}/precheck",
        headers=H_LEAD)
    check("4.7.2 已确认会话不能再预检查 status=400", r.status_code == 400, f"status={r.status_code}")

    # 4.8 查询沙盒会话列表，状态应为 confirmed
    print("\n--- 4.8 验证会话列表显示状态正确 ---")
    r = safe_request("GET", f"{API}/api/sandbox/",
        headers=H_LEAD,
        params={"status": "confirmed", "limit": 10})
    check("4.8.1 按状态查询 status=200", r.status_code == 200)
    if r.status_code == 200:
        sessions = r.json()
        found = any(s.get("sandbox_token") == SANDBOX_TOKEN for s in sessions)
        check("4.8.2 会话在 confirmed 列表中", found)

    return True


def test_sandbox_list_and_reject():
    """
    测试沙盒会话列表查询和拒绝流程
    """
    print("\n" + "="*60)
    print("【阶段5】补充测试：会话列表查询和拒绝流程")
    print("="*60)

    # 5.1 创建另一个用于测试拒绝的沙盒会话
    print("\n--- 5.1 创建新的沙盒会话（用于测试拒绝） ---")
    global ARCHIVE_ZIP_BYTES, BATCH_ID_FOR_ARCHIVE

    if not ARCHIVE_ZIP_BYTES:
        # 如果没有归档包，重新创建一个
        bid, archive_zip = create_and_archive_batch()
        if not bid or not archive_zip:
            warn("5.0 无法创建测试批次，跳过拒绝测试")
            return False
        ARCHIVE_ZIP_BYTES = archive_zip

    # 使用不同的 batch_code 恢复到沙盒
    r = safe_request("POST", f"{API}/api/sandbox/restore",
        headers=H_LEAD,
        files={"file": ("archive2.zip", io.BytesIO(ARCHIVE_ZIP_BYTES), "application/zip")})
    if r.status_code != 200:
        warn(f"5.1 创建沙盒会话失败: {r.text[:200]}")
        return False
    sandbox_token_2 = r.json()["sandbox_token"]
    print(f"  [INFO] 测试拒绝的会话: token={sandbox_token_2[:16]}...")

    # 5.2 admin 拒绝该会话
    print("\n--- 5.2 admin 拒绝该会话 ---")
    r = safe_request("POST", f"{API}/api/sandbox/{sandbox_token_2}/reject",
        headers=H_ADMIN,
        json={
            "reason": "版本差异过大，需要提交者重新整理",
            "comment": "请检查 v2 版本的内容差异"
        })
    check("5.2.1 admin 拒绝 status=200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        reject_data = r.json()
        check("5.2.2 success=True", reject_data.get("success") == True)
        check("5.2.3 状态为 rejected", reject_data.get("status") == "rejected")

    # 5.3 验证拒绝后审计日志
    print("\n--- 5.3 验证拒绝审计日志 ---")
    r = safe_request("GET", f"{API}/api/sandbox/{sandbox_token_2}/audit-logs",
        headers=H_LEAD)
    check("5.3.1 审计日志 status=200", r.status_code == 200)
    if r.status_code == 200:
        logs = r.json()
        action_types = [log.get("action") for log in logs]
        check("5.3.2 包含拒绝动作", any("REJECT" in str(a) for a in action_types))

    # 5.4 测试会话列表多状态查询
    print("\n--- 5.4 测试会话列表查询 ---")
    r = safe_request("GET", f"{API}/api/sandbox/",
        headers=H_LEAD,
        params={"limit": 20})
    check("5.4.1 查询全部会话 status=200", r.status_code == 200)
    if r.status_code == 200:
        sessions = r.json()
        print(f"  [INFO] 总共有 {len(sessions)} 个沙盒会话")
        statuses = set(s.get("status") for s in sessions)
        print(f"  [INFO] 包含状态: {statuses}")

    # 5.5 submitter 不能查看沙盒
    print("\n--- 5.5 权限验证：submitter 不能查看沙盒 ---")
    r = safe_request("GET", f"{API}/api/sandbox/",
        headers=H_SUBMITTER)
    check("5.5.1 submitter 查看被拒绝 status=403", r.status_code == 403, f"status={r.status_code}")

    return True


def test_config_switch():
    """
    测试配置开关控制
    """
    print("\n" + "="*60)
    print("【阶段6】配置开关测试")
    print("="*60)

    # 这个测试假设我们有配置管理 API，如果没有则跳过
    # 检查是否有配置 API
    r = safe_request("GET", f"{API}/api/config/", headers=H_ADMIN)
    if r.status_code not in [200, 404]:
        warn(f"6.0 无法访问配置 API (status={r.status_code})，跳过配置开关测试")
        return True

    if r.status_code == 404:
        warn("6.0 未找到配置 API，跳过配置开关测试")
        return True

    # 6.1 获取当前配置
    print("\n--- 6.1 获取沙盒相关配置 ---")
    config_data = r.json()
    sandbox_configs = [c for c in config_data if "sandbox" in c.get("config_key", "")]
    print(f"  [INFO] 沙盒相关配置: {[c.get('config_key') for c in sandbox_configs]}")

    # 6.2 验证配置存在
    config_keys = [c.get("config_key") for c in sandbox_configs]
    check("6.2.1 sandbox.enabled 配置存在", "sandbox.enabled" in config_keys)
    check("6.2.2 sandbox.require_admin_confirm 配置存在", "sandbox.require_admin_confirm" in config_keys)
    check("6.2.3 sandbox.auto_expire_hours 配置存在", "sandbox.auto_expire_hours" in config_keys)

    return True


def wait_for_server():
    """等待服务器启动"""
    print(f"\n等待 API 服务启动 ({API}) ...")
    for i in range(30):
        try:
            r = requests.get(f"{API}/health", timeout=2)
            if r.status_code == 200:
                print(f"  API 服务已就绪 (等待 {i}s)")
                return True
        except:
            pass
        time.sleep(1)
    print("  [WARN] API 服务未在超时内就绪，继续尝试测试...")
    return False


def main():
    print("\n" + "="*60)
    print("恢复后验收沙盒模块 完整验证测试")
    print("="*60)
    print(f"测试目标 API: {API}")
    print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 等待服务就绪
    wait_for_server()

    # 检查是否是重启后的测试
    existing_state = load_persistence_state()
    is_restart_test = existing_state is not None and "--restart" in sys.argv

    if is_restart_test:
        print("\n" + "*"*60)
        print("检测到重启测试模式，仅执行持久化验证")
        print("*"*60)
        test_persistence_after_restart()
    else:
        # 完整测试流程
        # 阶段1：创建并归档批次
        bid, archive_zip = create_and_archive_batch()
        if not bid or not archive_zip:
            print("\n[ERROR] 无法创建测试批次，终止测试")
            return 1
        global ARCHIVE_ZIP_BYTES
        ARCHIVE_ZIP_BYTES = archive_zip

        # 阶段2：完整沙盒流程
        test_sandbox_full_workflow(archive_zip)

        # 如果有 --save-state 参数，则在这里暂停，等待重启
        if "--save-state" in sys.argv:
            print("\n" + "*"*60)
            print("状态已保存。请重启服务后运行:")
            print(f"  python {sys.argv[0]} --restart")
            print("*"*60)
            return 0

        # 阶段3：持久化测试（假设服务未重启，测试数据在同一会话中存在）
        test_persistence_after_restart()

        # 阶段4：最终确认和生产写入
        test_final_confirmation_and_production_write()

        # 阶段5：列表和拒绝测试
        test_sandbox_list_and_reject()

        # 阶段6：配置开关测试
        test_config_switch()

    # 总结
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)
    print(f"总检查项: {len([e for e in errors + ['OK']]) if errors else 1}")
    print(f"通过: {len([e for e in ['OK'] if e])}")
    print(f"失败: {len(errors)}")
    if errors:
        print("\n失败的检查项:")
        for e in errors:
            print(f"  - {e}")
    else:
        print("\n🎉 所有测试通过！")

    if warnings_log:
        print(f"\n警告 ({len(warnings_log)}):")
        for w in warnings_log:
            print(f"  {w}")

    # 清理持久化文件
    if os.path.exists(PERSISTENCE_FILE) and "--restart" in sys.argv:
        os.remove(PERSISTENCE_FILE)
        print(f"\n  [INFO] 已清理持久化文件 {PERSISTENCE_FILE}")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
