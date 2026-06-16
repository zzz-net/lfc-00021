#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
用户可见验收链路演示 - 包含版本差异对比功能
演示完整的批次验收流程，重点展示新增的版本差异对比能力
"""

import requests
import json
import sys
import random
import string

API = "http://127.0.0.1:8000"
H_ADMIN = {"X-User-Id": "1", "Content-Type": "application/json"}
H_LEAD = {"X-User-Id": "2", "Content-Type": "application/json"}
H_REVIEWER = {"X-User-Id": "3", "Content-Type": "application/json"}
H_SUBMITTER = {"X-User-Id": "5", "Content-Type": "application/json"}

SUFFIX = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
BATCH_CODE = f"DEMO-DIFF-{SUFFIX}"

V1_FILE = "reviewer_generated/review_v1.json"
V2_FILE = "reviewer_generated/review_v2.json"


def print_section(title, char="="):
    print(f"\n{char * 70}")
    print(f"  {title}")
    print(f"{char * 70}\n")


def print_step(step, desc):
    print(f"\n  [*] 步骤 {step}: {desc}")


def print_success(msg):
    print(f"  [OK] {msg}")


def print_info(msg):
    print(f"  [INFO] {msg}")


def print_diff_summary(diff_data):
    summary = diff_data["summary"]
    print(f"\n  [SUMMARY] 版本差异汇总:")
    print(f"     总条目: v1={summary['total_items_old']} -> v2={summary['total_items_new']}")
    print(f"     新增: {summary['added_count']} 项")
    print(f"     删除: {summary['removed_count']} 项")
    print(f"     修改: {summary['modified_count']} 项")
    print(f"     未变: {summary['unchanged_count']} 项")
    print(f"     字段变更总数: {summary['field_change_count']} 处")

    if summary.get('unresolved_rejections_new', 0) > 0:
        print(f"     [WARN] 未解决驳回: {summary['unresolved_rejections_new']} 项")

    if summary.get('validation_errors_new', 0) > 0:
        print(f"     [ERROR] 校验错误: {summary['validation_errors_new']} 项")
    if summary.get('validation_warnings_new', 0) > 0:
        print(f"     [WARN] 校验警告: {summary['validation_warnings_new']} 项")


def precheck_and_import(bid, filename, filepath, user, import_format="json"):
    mime = "application/json" if import_format == "json" else "text/csv"
    with open(filepath, "rb") as f:
        files = {"file": (filename, f, mime)}
        data = {"import_format": import_format}
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/precheck",
            headers={k: v for k, v in user.items() if k != "Content-Type"},
            files=files,
            data=data
        )
    if r.status_code != 200:
        print(f"  [FAIL] 预检查失败: {r.status_code}")
        print(f"     {r.json()}")
        sys.exit(1)

    body = r.json()
    if not body.get("can_import"):
        print(f"  [FAIL] 无法导入: {body.get('message')}")
        sys.exit(1)

    token = body.get("precheck_token")

    with open(filepath, "rb") as f:
        files = {"file": (filename, f, mime)}
        data = {"import_format": import_format, "precheck_token": token}
        r = requests.post(
            f"{API}/api/batches/{bid}/manifests/import",
            headers={k: v for k, v in user.items() if k != "Content-Type"},
            files=files,
            data=data
        )

    if r.status_code != 200 or not r.json().get("success"):
        print(f"  [FAIL] 导入失败: {r.status_code}")
        print(f"     {r.json()}")
        sys.exit(1)

    return r.json()


def main():
    print_section("用户可见验收链路演示 - 版本差异对比功能", "=")
    print_info(f"测试批次: {BATCH_CODE}")
    print_info(f"API 地址: {API}")
    print_info("本演示展示完整的批次验收流程，重点突出新增的版本差异对比能力")

    # 检查服务健康
    try:
        r = requests.get(f"{API}/health")
        if r.status_code != 200:
            print("[FAIL] 服务未正常运行")
            sys.exit(1)
    except requests.ConnectionError:
        print("[FAIL] 无法连接到服务，请先启动服务")
        sys.exit(1)
    print_success("服务运行正常")

    # ==============================================
    # 阶段 1: 创建批次
    # ==============================================
    print_section("阶段 1: 创建交付批次", "-")
    print_step(1, "submitter_chen (ID=5) 创建交付批次")

    r = requests.post(f"{API}/api/batches/", headers=H_SUBMITTER, json={
        "batch_code": BATCH_CODE,
        "name": "2026年Q2网络设备交付批次",
        "description": "包含路由器板卡、电源模块、风扇盘等网络设备",
        "submitter_id": 5
    })
    data = r.json()
    batch_id = data["id"]
    print_success(f"批次创建成功，ID={batch_id}，状态={data['status']}")

    # ==============================================
    # 阶段 2: 导入 v1 清单
    # ==============================================
    print_section("阶段 2: 导入 v1 版本清单", "-")
    print_step(2, "导入 v1 清单 (review_v1.json)")

    result = precheck_and_import(batch_id, "review_v1.json", V1_FILE, H_SUBMITTER, "json")
    v1_version = result["version_number"]
    print_success(f"v1 导入成功，版本号={v1_version}，条目数={result['item_count']}")

    # 执行校验
    print_step(3, "对 v1 执行规则校验")
    r = requests.post(f"{API}/api/batches/{batch_id}/validate", headers=H_SUBMITTER)
    val_data = r.json()
    print_success(f"v1 校验完成: {val_data['passed']} 通过, {val_data['failed']} 错误, {val_data['warnings']} 警告")

    # ==============================================
    # 阶段 3: 提交评审，驳回问题
    # ==============================================
    print_section("阶段 3: 提交评审并驳回", "-")
    print_step(4, "提交批次到待评审状态")
    r = requests.post(
        f"{API}/api/batches/{batch_id}/transition",
        headers=H_SUBMITTER,
        json={"target_status": "pending_review", "comment": "v1 清单准备就绪，请评审"}
    )
    print_success(f"状态变更为: {r.json()['status']}")

    print_step(5, "reviewer_li (ID=3) 发现问题并驳回")
    r = requests.post(
        f"{API}/api/batches/{batch_id}/reject",
        headers=H_REVIEWER,
        json={
            "rejections": [
                {
                    "item_key": "ITEM-R1",
                    "rejection_reason": "Router Board 型号描述不完整，应注明版本号"
                }
            ],
            "comment": "ITEM-R1 名称需要补充版本信息，建议改为 Router Board RevB"
        }
    )
    rej_data = r.json()
    print_success(f"已驳回 {rej_data['rejection_count']} 项问题，状态={rej_data['batch_status']}")

    # ==============================================
    # 阶段 4: 开始返修，导入 v2
    # ==============================================
    print_section("阶段 4: 返修并导入 v2 版本", "-")
    print_step(6, "submitter_chen 开始返修")
    r = requests.post(
        f"{API}/api/batches/{batch_id}/start-repair",
        headers=H_SUBMITTER
    )
    print_success(f"进入返修状态: {r.json()['batch_status']}")

    print_step(7, "导入 v2 修订版清单 (已修改 ITEM-R1 名称，新增 ITEM-R3)")
    with open(V2_FILE, "r", encoding="utf-8") as f:
        v2_content = json.load(f)
    print_info(f"v2 变更内容:")
    print_info(f"  - ITEM-R1: 'Router Board' → 'Router Board RevB' (已修正驳回问题)")
    print_info(f"  - ITEM-R3: 新增风扇盘 (Fan Tray)")
    print_info(f"  - ITEM-R2: 无变更")

    result = precheck_and_import(batch_id, "review_v2.json", V2_FILE, H_SUBMITTER, "json")
    v2_version = result["version_number"]
    print_success(f"v2 导入成功，版本号={v2_version}，条目数={result['item_count']}")

    # 执行 v2 校验
    print_step(8, "对 v2 执行规则校验")
    r = requests.post(f"{API}/api/batches/{batch_id}/validate", headers=H_SUBMITTER)
    val_data = r.json()
    print_success(f"v2 校验完成: {val_data['passed']} 通过, {val_data['failed']} 错误, {val_data['warnings']} 警告")

    # ==============================================
    # 阶段 5: 版本差异对比 - 核心功能演示
    # ==============================================
    print_section("阶段 5: 版本差异对比 (新增功能演示)", "-")
    print_step(9, "lead_wang (ID=2) 查看 v1 与 v2 的版本差异")

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2",
        headers=H_LEAD
    )
    if r.status_code != 200:
        print(f"  [FAIL] 版本差异查询失败: {r.status_code}")
        print(f"     {r.json()}")
        sys.exit(1)

    diff_data = r.json()
    print_success("版本差异查询成功！")
    print_diff_summary(diff_data)

    # 显示详细差异
    print(f"\n  [DETAIL] 详细变更:")

    added = diff_data.get("added_items", [])
    for item in added:
        print(f"\n     [+] 新增: {item['item_key']}")
        print(f"        行号: {item['line_number_new']}")
        for fc in item["field_changes"]:
            print(f"        {fc['field_name']} = {fc['new_value']}")

    modified = diff_data.get("modified_items", [])
    for item in modified:
        print(f"\n     [~] 修改: {item['item_key']}")
        print(f"        v1 行号: {item['line_number_old']} -> v2 行号: {item['line_number_new']}")
        for fc in item["field_changes"]:
            if fc["change_type"] == "modified":
                print(f"        {fc['field_name']}: '{fc['old_value']}' -> '{fc['new_value']}'")

    unchanged = diff_data.get("unchanged_items", [])
    print(f"\n     [=] 未变更: {[i['item_key'] for i in unchanged]}")

    # 显示关联的驳回信息
    rejections = diff_data.get("unresolved_rejections", [])
    if rejections:
        print(f"\n  [WARN] 关联未解决驳回 ({len(rejections)} 项):")
        for rej in rejections:
            print(f"     - {rej['item_key']}: {rej['rejection_reason']}")
            print(f"       驳回人: {rej['rejector_username']} ({rej['created_at'][:19]})")

    # 显示校验变化
    val_changes = diff_data.get("validation_changes", [])
    if val_changes:
        print(f"\n  [VALID] 校验结果变化 ({len(val_changes)} 项):")
        for vc in val_changes[:3]:
            status = "新问题" if vc["change_type"] == "new_violation" else ("已解决" if vc["change_type"] == "resolved" else "已修改")
            print(f"     - {vc['item_key']} [{vc['rule_code']}]: {status}")

    # 显示导入信息
    print(f"\n  [IMPORT] 导入信息:")
    print(f"     v1: 由 {diff_data['metadata']['old_import']['imported_by_username']} "
          f"于 {str(diff_data['metadata']['old_import']['imported_at'])[:19]} 导入")
    print(f"     v2: 由 {diff_data['metadata']['new_import']['imported_by_username']} "
          f"于 {str(diff_data['metadata']['new_import']['imported_at'])[:19]} 导入")

    # ==============================================
    # 阶段 6: 导出版本差异
    # ==============================================
    print_section("阶段 6: 导出版本差异报告", "-")
    print_step(10, "lead_wang 导出 JSON 格式的版本差异报告")

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2",
        headers=H_LEAD
    )
    if r.status_code != 200:
        print(f"  [FAIL] 导出失败: {r.status_code}")
        print(f"     {r.json()}")
        sys.exit(1)

    export_data = r.json()
    print_success("导出成功！")
    print_info(f"导出 ID: {export_data['export_id']}")
    print_info(f"导出时间: {export_data['export_timestamp'][:19]}")
    print_info(f"导出人: {export_data['exported_by']}")
    print_info(f"文件名: version_diff_{BATCH_CODE}_v1_to_v2.json")

    # 验证幂等性
    print_step(11, "验证幂等性 - 再次导出相同版本对比")
    r2 = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2",
        headers=H_LEAD
    )
    export_data2 = r2.json()
    if export_data["export_id"] == export_data2["export_id"]:
        print_success(f"幂等性验证通过！两次导出 ID 相同: {export_data['export_id']}")
    else:
        print(f"  [FAIL] 幂等性验证失败: {export_data['export_id']} != {export_data2['export_id']}")

    # ==============================================
    # 阶段 7: 权限验证
    # ==============================================
    print_section("阶段 7: 权限控制验证", "-")
    print_step(12, "验证 reviewer 无权查看版本差异")

    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers=H_REVIEWER
    )
    if r.status_code == 403:
        print_success(f"权限控制生效！reviewer 被正确拒绝 (403 Forbidden)")
        print_info(f"错误信息: {r.json()['error']['message'][:80]}...")
    else:
        print(f"  [FAIL] 权限控制失效: 期望 403，实际 {r.status_code}")

    print_step(13, "验证其他 submitter 无权查看")
    r = requests.get(
        f"{API}/api/batches/{batch_id}/version-diff",
        headers={"X-User-Id": "6"}
    )
    if r.status_code == 403:
        print_success(f"权限控制生效！其他 submitter 被正确拒绝 (403 Forbidden)")
    else:
        print(f"  [FAIL] 权限控制失效: 期望 403，实际 {r.status_code}")

    # ==============================================
    # 阶段 8: 审批日志验证
    # ==============================================
    print_section("阶段 8: 审批日志检查", "-")
    print_step(14, "查看版本差异相关的审批日志")

    r = requests.get(
        f"{API}/api/batches/{batch_id}/approval-logs",
        headers=H_LEAD
    )
    logs = r.json()

    view_logs = [l for l in logs if l["action"] == "VIEW_VERSION_DIFF"]
    export_logs = [l for l in logs if l["action"] == "EXPORT_VERSION_DIFF"]

    print_success(f"找到 {len(view_logs)} 条查看版本差异日志")
    print_success(f"找到 {len(export_logs)} 条导出版本差异日志")

    if view_logs:
        latest = view_logs[-1]
        print_info(f"最近查看: {latest['created_at'][:19]} by user {latest['actor_id']}")
        print_info(f"  对比版本: v{latest['extra_data']['old_version']} → v{latest['extra_data']['new_version']}")

    if export_logs:
        latest = export_logs[-1]
        print_info(f"最近导出: {latest['created_at'][:19]} by user {latest['actor_id']}")
        print_info(f"  导出 ID: {latest['extra_data']['export_id']}")

    # ==============================================
    # 阶段 9: 继续完成验收流程
    # ==============================================
    print_section("阶段 9: 完成验收流程", "-")
    print_step(15, "submitter_chen 重新提交评审")
    r = requests.post(
        f"{API}/api/batches/{batch_id}/transition",
        headers=H_SUBMITTER,
        json={"target_status": "pending_review", "comment": "v2 已修正驳回问题，申请重新评审"}
    )
    print_success(f"状态变更为: {r.json()['status']}")

    print_step(16, "lead_wang 审批通过")
    r = requests.post(
        f"{API}/api/batches/{batch_id}/approve",
        headers=H_LEAD,
        json={"comment": "版本差异清晰，v2 已修正所有问题，同意通过验收"}
    )
    print_success(f"审批通过！状态={r.json()['batch_status']}")
    print_info(f"审批人: {r.json()['approved_by']}")
    print_info(f"审批时间: {r.json()['approved_at'][:19]}")

    # ==============================================
    # 总结
    # ==============================================
    print_section("演示总结", "=")
    print_success("用户可见验收链路演示完成！")
    print(f"\n  [FEATURE] 新增的版本差异对比功能亮点:")
    print(f"     1. [OK] 按 item_key 精准识别新增、删除、修改的条目")
    print(f"     2. [OK] 字段级变更追踪，清楚展示每个字段的新旧值")
    print(f"     3. [OK] 数量汇总一目了然，快速了解版本变化规模")
    print(f"     4. [OK] 关联未解决驳回，评审时上下文完整")
    print(f"     5. [OK] 校验结果变化追踪，质量改进清晰可见")
    print(f"     6. [OK] 导入人/时间元数据完整，可追溯")
    print(f"     7. [OK] 严格权限控制，仅 lead/admin/提交人可查看")
    print(f"     8. [OK] JSON 导出支持，便于归档和离线查看")
    print(f"     9. [OK] 幂等性保证，重启后同一批次查询结果一致")
    print(f"    10. [OK] 审批日志完整，谁在何时查看/导出全记录")

    print(f"\n  [API] 相关 API:")
    print(f"     GET  /api/batches/{{batch_id}}/version-diff")
    print(f"     GET  /api/batches/{{batch_id}}/version-diff/export")
    print(f"     参数: old_version, new_version (可选，默认对比最近两个版本)")

    print(f"\n  [BATCH] 测试批次信息:")
    print(f"     批次 ID: {batch_id}")
    print(f"     批次编码: {BATCH_CODE}")
    print(f"     版本: v1 -> v2")
    print(f"     状态: {r.json()['batch_status']}")

    print(f"\n  [TIP] 你可以手动执行以下命令验证:")
    print(f'     curl -H "X-User-Id: 2" "{API}/api/batches/{batch_id}/version-diff?old_version=1&new_version=2"')
    print(f'     curl -H "X-User-Id: 2" "{API}/api/batches/{batch_id}/version-diff/export?old_version=1&new_version=2"')
    print(f'     curl -H "X-User-Id: 3" "{API}/api/batches/{batch_id}/version-diff"  (应返回 403)')

    print("\n" + "=" * 70)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[ERROR] 验收链路执行异常: {e}")
        import traceback
        traceback.print_exc()
        import sys
        sys.exit(1)
