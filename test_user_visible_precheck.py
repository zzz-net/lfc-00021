#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清单导入「预检查 + 冲突确认」用户可见验收脚本
使用 Python requests 跑通完整用户视角链路

使用方式：
  1. 启动服务:  python -m uvicorn main:app --port 8000
  2. 运行脚本:  python test_user_visible_precheck.py

输出：按用户视角打印每一步的请求、响应关键信息、以及人工核对点
"""
import json
import requests
import sys
import uuid

API = "http://127.0.0.1:8000"
H_SUB = {"X-User-Id": "5"}
H_REV = {"X-User-Id": "3"}
H_LEAD = {"X-User-Id": "2"}
H_ADM = {"X-User-Id": "1"}

V1_CSV = "samples/manifest_sample_good.csv"
V2_CSV = "samples/manifest_sample_repaired_v2.csv"

LINE = "─" * 68


def hr(title=""):
    print()
    if title:
        pad = max(1, (68 - len(title) - 2) // 2)
        print("─" * pad + f" {title} " + "─" * pad)
    else:
        print(LINE)


def section(n, title):
    print()
    print("=" * 68)
    print(f"[{n}] {title}")
    print("=" * 68)


def check(name, cond, detail=""):
    status = "✅" if cond else "❌"
    print(f"  {status} {name}", end="")
    if not cond:
        print(f"  —— {detail}")
    else:
        print()
    return cond


def ensure_ok(r, expected=200):
    if r.status_code != expected:
        print(f"  [!] HTTP {r.status_code} != {expected}")
        try:
            print("      响应:", json.dumps(r.json(), ensure_ascii=False, indent=4)[:800])
        except Exception:
            print("      响应:", r.text[:200])
        return False
    return True


ok_all = True
try:
    requests.get(f"{API}/health", timeout=3).raise_for_status()
except Exception as e:
    print(f"[FATAL] 服务未启动: {e}")
    print("       请先执行: python -m uvicorn main:app --port 8000")
    sys.exit(2)

SUFFIX = uuid.uuid4().hex[:6].upper()
BATCH_CODE = f"USER-VIS-{SUFFIX}"

# ──────────────────────────────────────────────────────────────────────
section(1, "角色 & 场景说明")
# ──────────────────────────────────────────────────────────────────────
print("""
  场景：提交人 submitter_chen (ID=5) 发起一个服务器配件交付批次
       → 导入清单前先预检查 → 确认后正式导入
       → 提交待验收 → reviewer 驳回 → 返修
       → 再次预检查（看到驳回提醒）→ 确认后导入 v2
       → 重新校验 → 通过 → 归档

  涉及角色：
    • ID=5 submitter_chen  批次提交人（执行预检查+导入）
    • ID=3 reviewer_li     评审员（驳回问题项）
    • ID=2 lead_wang       主管（批准通过 / 归档）
    • ID=1 admin           超级管理员（随时可代操作）
""")

# ──────────────────────────────────────────────────────────────────────
section(2, "创建交付批次")
# ──────────────────────────────────────────────────────────────────────
hr("请求: POST /api/batches/  (X-User-Id: 5)")
payload = {
    "batch_code": BATCH_CODE,
    "name": "2026-Q2 服务器配件交付（用户验收链路）",
    "description": "主板 ×1、内存 ×2、硬盘 ×2，标准服务器核心配置清单",
    "submitter_id": 5,
}
print(f"  batch_code : {BATCH_CODE}")
r = requests.post(f"{API}/api/batches/", headers=H_SUB, json=payload)
ensure_ok(r, 201)
BATCH_ID = r.json()["id"]
print(f"  ← batch_id = {BATCH_ID}")
print(f"  ← status   = {r.json()['status']}")
ok_all &= check("批次创建成功", r.status_code == 201)

# ──────────────────────────────────────────────────────────────────────
section(3, "【关键新能力】步骤 1：预检查清单 v1")
# ──────────────────────────────────────────────────────────────────────
hr("请求: POST /api/batches/{batch_id}/manifests/precheck")
print("  文件     : samples/manifest_sample_good.csv (5 条)")
print("  Header   : X-User-Id: 5")
print("  Form-Data: file=@v1.csv, import_format=auto")

with open(V1_CSV, "rb") as f:
    r = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/precheck",
        headers=H_SUB,
        files={"file": ("manifest_v1.csv", f, "text/csv")},
    )
ok_all &= ensure_ok(r, 200)
d = r.json()

hr("响应关键字段")
print(f"  precheck_token  : {d['precheck_token'][:32]}…")
print(f"  action_type     : {d['action_type']}    (NEW_VERSION=将新建, REUSE_VERSION=复用, CONFLICT=冲突)")
print(f"  planned_version : {d['planned_version_number']}")
print(f"  has_conflict    : {d['has_conflict']}")
print(f"  can_import      : {d['can_import']}     (用户是否可以点击确认导入)")
print(f"  item_count      : {d['item_count']}")
print(f"  import_format   : {d['import_format']}")
print(f"  content_hash    : {d['content_hash'][:24]}…")
print(f"  expires_at      : {d['expires_at']}  (30 分钟有效)")

hr("预检查结论明细")
print(f"  结论 message  : {d['message']}")
print(f"  原因 reasons  :")
for r_ in d["reasons"]:
    print(f"    • {r_}")

if d["conflicts"]:
    print(f"  冲突列表 conflicts:")
    for c in d["conflicts"]:
        print(f"    · [{c['severity']:<7}] {c['title']}")
        print(f"         描述: {c['description']}")
        print(f"         建议: {c['suggestion']}")
else:
    print("  冲突列表     : 无")

ok_all &= check("v1 预检查 action_type=NEW_VERSION", d["action_type"] == "NEW_VERSION")
ok_all &= check("v1 预检查 can_import=True", d["can_import"] is True)
ok_all &= check("v1 预检查 has_conflict=False", d["has_conflict"] is False)
TOKEN_V1 = d["precheck_token"]

# ──────────────────────────────────────────────────────────────────────
section(4, "【关键新能力】步骤 2：携带 precheck_token 正式导入 v1")
# ──────────────────────────────────────────────────────────────────────
hr("请求: POST /api/batches/{batch_id}/manifests/import")
print("  Form-Data: file=@v1.csv, import_format=auto, precheck_token=*****")
print("  作用     : 服务端校验 token 有效 + 内容哈希一致 → 才真正写入")

with open(V1_CSV, "rb") as f:
    r = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUB,
        files={"file": ("manifest_v1.csv", f, "text/csv")},
        data={"precheck_token": TOKEN_V1},
    )
ok_all &= ensure_ok(r, 200)
imp = r.json()

hr("导入结果")
print(f"  success         : {imp['success']}")
print(f"  version_number  : {imp['version_number']}")
print(f"  manifest_id     : {imp['manifest_version_id']}")
print(f"  item_count      : {imp['item_count']}")
print(f"  message         : {imp['message']}")

ok_all &= check("v1 导入 success=True", imp["success"] is True)
ok_all &= check("v1 版本号=1", imp["version_number"] == 1)
V1_ID = imp["manifest_version_id"]

hr("防重放验证（同一 token 再提交一次 → 应拒绝）")
with open(V1_CSV, "rb") as f:
    r2 = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUB,
        files={"file": ("manifest_v1.csv", f, "text/csv")},
        data={"precheck_token": TOKEN_V1},
    )
print(f"  再次提交 HTTP {r2.status_code}")
msg = r2.json().get("error", {}).get("message", "")
print(f"  错误: {msg[:70]}")
ok_all &= check("同 token 二次提交被拒", r2.status_code == 400)

# ──────────────────────────────────────────────────────────────────────
section(5, "常规流程：校验 → 提交待验收 → reviewer 驳回 2 项问题")
# ──────────────────────────────────────────────────────────────────────
hr("POST /api/batches/{id}/validate")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/validate", headers=H_SUB)
ok_all &= ensure_ok(r, 200)
print(f"  ← validation_passed = {r.json()['validation_summary']['validation_passed']}")

hr("POST /api/batches/{id}/transition → pending_review")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/transition", headers=H_SUB,
                  json={"target_status": "pending_review", "comment": "初检完成，请评审验收"})
ok_all &= ensure_ok(r, 200)
print(f"  ← status = {r.json()['status']}")

hr("POST /api/batches/{id}/reject  (X-User-Id: 3 reviewer)")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/reject", headers=H_REV, json={
    "comment": "清单存在 2 项问题，请返修",
    "rejections": [
        {"item_key": "ITEM-001", "rejection_reason": "主板 BIOS 兼容性测试报告未提供"},
        {"item_key": "ITEM-002", "rejection_reason": "内存规格未标注 ECC 校验支持，请补充"},
    ]
})
ok_all &= ensure_ok(r, 200)
rej = r.json()
print(f"  ← rejection_count = {rej['rejection_count']}")
print(f"  ← batch_status    = {rej['batch_status']}")

hr("POST /api/batches/{id}/start-repair")
r = requests.post(f"{API}/api/batches/{BATCH_ID}/start-repair", headers=H_SUB,
                  data={"comment": "已收到驳回，开始修订清单"})
ok_all &= ensure_ok(r, 200)
print(f"  ← status = {r.json()['batch_status']}  (repairing)")

hr("GET /api/batches/{id}/rejections?only_unresolved=true")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/rejections?only_unresolved=true", headers=H_ADM)
ok_all &= ensure_ok(r, 200)
print(f"  ← 未解决驳回条数 = {len(r.json())}")
ok_all &= check("此时应有 2 条未解决驳回", len(r.json()) == 2)

# ──────────────────────────────────────────────────────────────────────
section(6, "【关键新能力】步骤 3：返修中预检查 v2 → 看到未解决驳回 WARNING")
# ──────────────────────────────────────────────────────────────────────
hr("请求: POST /api/batches/{batch_id}/manifests/precheck  (文件=V2 7条)")
print("  预期: 内容变更 → NEW_VERSION；有驳回 → WARNING 冲突（不阻塞）")

with open(V2_CSV, "rb") as f:
    r = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/precheck",
        headers=H_SUB,
        files={"file": ("manifest_v2.csv", f, "text/csv")},
    )
ok_all &= ensure_ok(r, 200)
d2 = r.json()

hr("v2 预检查响应")
print(f"  action_type     : {d2['action_type']}")
print(f"  planned_version : {d2['planned_version_number']}")
print(f"  has_conflict    : {d2['has_conflict']}  (warning 也算有冲突)")
print(f"  can_import      : {d2['can_import']}    (warning 不阻塞)")

hr("冲突详情（用户能在 UI 明确看到）")
for c in d2["conflicts"]:
    print(f"  ● [{c['severity']:<7}] conflict_type={c['conflict_type']}")
    print(f"      标题  : {c['title']}")
    print(f"      描述  : {c['description']}")
    print(f"      建议  : {c['suggestion']}")

hr("给用户看的 reasons 摘要")
for r_ in d2["reasons"]:
    print(f"  • {r_}")

print()
ok_all &= check("v2 预检查 action_type=NEW_VERSION（内容不同）", d2["action_type"] == "NEW_VERSION")
ok_all &= check("v2 预检查 has_conflict=True（有 warning）", d2["has_conflict"] is True)
ok_all &= check("v2 预检查 can_import=True（warning 不阻塞）", d2["can_import"] is True)
ok_all &= check("conflict_type 含 UNRESOLVED_REJECTIONS",
                any(c["conflict_type"] == "UNRESOLVED_REJECTIONS" for c in d2["conflicts"]))
TOKEN_V2 = d2["precheck_token"]

# ──────────────────────────────────────────────────────────────────────
section(7, "【关键新能力】查询最近一次预检查（用户刷新页面仍可看到）")
# ──────────────────────────────────────────────────────────────────────
hr("请求: GET /api/batches/{batch_id}/manifests/prechecks/latest")
print("  场景  : 用户完成预检查后离开页面，重进页面点击最近结果查看")
print("        或管理员事后审计查询")

r = requests.get(f"{API}/api/batches/{BATCH_ID}/manifests/prechecks/latest", headers=H_ADM)
ok_all &= ensure_ok(r, 200)
lp = r.json()
hr("查询结果")
print(f"  id                  : {lp['id']}")
print(f"  actor_id            : {lp['actor_id']}  (谁做的预检查)")
print(f"  action_type         : {lp['action_type']}")
print(f"  planned_version     : {lp['planned_version_number']}")
print(f"  has_conflict        : {lp['has_conflict']}")
print(f"  consumed            : {lp['consumed']}  (是否已用于导入)")
print(f"  can_import          : {lp['can_import']}")
print(f"  content_hash        : {lp['content_hash'][:24]}…")
print(f"  created_at          : {lp['created_at']}")
print(f"  expires_at          : {lp['expires_at']}")
print(f"  batch_status        : {lp['batch_status']}")
print(f"  conflict_types      : {lp['conflict_types']}")
print(f"  reasons             :")
for r_ in lp["reasons"]:
    print(f"    • {r_}")

ok_all &= check("latest 预检查 action_type 一致", lp["action_type"] == d2["action_type"])
ok_all &= check("latest 预检查 consumed=False（尚未导入）", lp["consumed"] is False)

hr("独立接口：GET /api/batches/{batch_id}/manifests/prechecks?limit=10")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/manifests/prechecks?limit=10", headers=H_ADM)
ok_all &= ensure_ok(r, 200)
h = r.json()
print(f"  ← precheck 记录总条数 = {len(h)}  (v1 + v2 至少 2 条)")
ok_all &= check("precheck 列表 ≥ 2 条", len(h) >= 2)

# ──────────────────────────────────────────────────────────────────────
section(8, "【关键新能力】步骤 4：确认后携带 TOKEN 导入 v2（自动解决驳回）")
# ──────────────────────────────────────────────────────────────────────
hr("请求: POST /api/batches/{batch_id}/manifests/import  (token=v2 的)")
with open(V2_CSV, "rb") as f:
    r = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUB,
        files={"file": ("manifest_v2.csv", f, "text/csv")},
        data={"precheck_token": TOKEN_V2},
    )
ok_all &= ensure_ok(r, 200)
imp2 = r.json()
hr("导入 v2 结果")
print(f"  success       : {imp2['success']}")
print(f"  version       : {imp2['version_number']}")
print(f"  manifest_id   : {imp2['manifest_version_id']}")
print(f"  item_count    : {imp2['item_count']}")
print(f"  message       : {imp2['message']}")
ok_all &= check("v2 导入成功 version=2", imp2["version_number"] == 2)

hr("验证：导入 v2 后未解决驳回自动全部 resolved")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/rejections?only_unresolved=true", headers=H_ADM)
unresolved = len(r.json())
print(f"  ← 未解决驳回条数 = {unresolved}")
ok_all &= check("导入 v2 后未解决驳回=0", unresolved == 0)

hr("验证：latest precheck 的 consumed 已变为 True")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/manifests/prechecks/latest", headers=H_ADM)
print(f"  ← consumed = {r.json()['consumed']}")
ok_all &= check("消费后 consumed=True", r.json()["consumed"] is True)

# ──────────────────────────────────────────────────────────────────────
section(9, "【关键新能力】审批日志含 PRECHECK_IMPORT 动作（审计链路）")
# ──────────────────────────────────────────────────────────────────────
hr("GET /api/batches/{batch_id}/approval-logs")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/approval-logs", headers=H_ADM)
ok_all &= ensure_ok(r, 200)
logs = r.json()

pre_logs = [l for l in logs if l["action"] == "PRECHECK_IMPORT"]
imp_logs = [l for l in logs if l["action"] == "IMPORT_MANIFEST"]

print(f"  审批日志总数            : {len(logs)}")
print(f"  PRECHECK_IMPORT 条数   : {len(pre_logs)}")
print(f"  IMPORT_MANIFEST 条数   : {len(imp_logs)}")
print()
for i, l in enumerate(pre_logs, 1):
    extra = l.get("extra_data") or {}
    print(f"  PRECHECK #{i}")
    print(f"    actor_id={l['actor_id']}, comment={l['comment']}")
    print(f"    extra.precheck_token   = {(extra.get('precheck_token') or '')[:24]}…")
    print(f"    extra.action_type      = {extra.get('action_type')}")
    print(f"    extra.has_conflict     = {extra.get('has_conflict')}")
    print(f"    extra.conflict_types   = {extra.get('conflict_types')}")
    print(f"    extra.can_import       = {extra.get('can_import')}")
    print(f"    extra.content_hash     = {(extra.get('content_hash') or '')[:24]}…")
    print()

ok_all &= check("审批日志有 2 条 PRECHECK_IMPORT", len(pre_logs) == 2)
ok_all &= check("审批日志有 2 条 IMPORT_MANIFEST", len(imp_logs) == 2)

# ──────────────────────────────────────────────────────────────────────
section(10, "回归：v2 相同内容再次预检查 → 复用 v2 (REUSE_VERSION)")
# ──────────────────────────────────────────────────────────────────────
hr("POST /precheck 再次上传 V2 完全相同文件")
with open(V2_CSV, "rb") as f:
    r = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/precheck",
        headers=H_SUB,
        files={"file": ("dup_v2.csv", f, "text/csv")},
    )
ok_all &= ensure_ok(r, 200)
dup = r.json()
print(f"  action_type            : {dup['action_type']}   (期望 REUSE_VERSION)")
print(f"  reused_version_number  : {dup['reused_version_number']}")
print(f"  reused_version_id      : {dup['reused_version_id']}")
print(f"  planned_version_number : {dup['planned_version_number']}")
print(f"  message                : {dup['message']}")
ok_all &= check("重复 v2 → REUSE_VERSION", dup["action_type"] == "REUSE_VERSION")
ok_all &= check("重复 v2 → reused_version_number=2", dup["reused_version_number"] == 2)
ok_all &= check("重复 v2 → reused_version_id == V2 导入 id",
                dup["reused_version_id"] == imp2["manifest_version_id"])

# ──────────────────────────────────────────────────────────────────────
section(11, "回归：待验收(pending_review)状态下预检查 → STATUS_CONFLICT 阻塞")
# ──────────────────────────────────────────────────────────────────────
hr("先流转到 pending_review，再尝试预检查 V2（期望阻塞）")
requests.post(f"{API}/api/batches/{BATCH_ID}/validate", headers=H_SUB)
r = requests.post(f"{API}/api/batches/{BATCH_ID}/transition", headers=H_SUB,
                  json={"target_status": "pending_review"})
print(f"  当前 status = {r.json()['status']}")

with open(V2_CSV, "rb") as f:
    r = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/precheck",
        headers=H_SUB,
        files={"file": ("v2_try.csv", f, "text/csv")},
    )
ok_all &= ensure_ok(r, 200)
st = r.json()
print(f"  action_type  : {st['action_type']}   (期望 CONFLICT)")
print(f"  can_import   : {st['can_import']}    (期望 False)")
for c in st["conflicts"]:
    print(f"  · [{c['severity']}] {c['conflict_type']}: {c['title']}")
ok_all &= check("pending_review 下 action_type=CONFLICT", st["action_type"] == "CONFLICT")
ok_all &= check("pending_review 下 can_import=False", st["can_import"] is False)

hr("用 CONFLICT 的 token 尝试导入 → 必须拒绝")
with open(V2_CSV, "rb") as f:
    r = requests.post(
        f"{API}/api/batches/{BATCH_ID}/manifests/import",
        headers=H_SUB,
        files={"file": ("v2_try.csv", f, "text/csv")},
        data={"precheck_token": st["precheck_token"]},
    )
print(f"  HTTP {r.status_code}")
err_msg = r.json().get("error", {}).get("message", "")
print(f"  错误: {err_msg[:90]}")
ok_all &= check("CONFLICT token 被拒", r.status_code == 400 and "阻塞" in err_msg)

# ──────────────────────────────────────────────────────────────────────
section(12, "收尾：校验通过 → 再次提交待验收 → lead 批准 → 归档 → 验收报告")
# ──────────────────────────────────────────────────────────────────────
hr("POST validate  (此时已经是 pending_review? 先拉回 repairing 再走一遍)")
# 由于上面处于 pending_review，通过 start-repair 不允许（只能从 partially_rejected → repairing），
# 所以回到 draft 再重新走流程，简单用 transition 从 pending_review → draft
r = requests.post(f"{API}/api/batches/{BATCH_ID}/transition", headers=H_SUB,
                  json={"target_status": "draft", "comment": "回到草稿重提"})
print(f"  draft 流转: {r.status_code}")
requests.post(f"{API}/api/batches/{BATCH_ID}/validate", headers=H_SUB)
r = requests.post(f"{API}/api/batches/{BATCH_ID}/transition", headers=H_SUB,
                  json={"target_status": "pending_review"})
print(f"  → pending_review  {r.status_code}")

r = requests.post(f"{API}/api/batches/{BATCH_ID}/approve", headers=H_LEAD,
                  data={"comment": "v2 问题已修复，通过验收"})
ok_all &= ensure_ok(r, 200)
print(f"  approve: {r.json()['batch_status']}")

r = requests.post(f"{API}/api/batches/{BATCH_ID}/archive", headers=H_LEAD,
                  data={"comment": "交付完成，正式归档"})
ok_all &= ensure_ok(r, 200)
print(f"  archive: {r.json()['batch_status']}")

hr("验收报告 GET /api/batches/{batch_id}/acceptance-report")
r = requests.get(f"{API}/api/batches/{BATCH_ID}/acceptance-report", headers=H_ADM)
ok_all &= ensure_ok(r, 200)
report = r.json()
print(f"  batch_code          : {report['batch_code']}")
print(f"  status              : {report['status']}")
print(f"  total_versions      : {report['total_versions']}")
print(f"  current_version     : {report['current_version']}")
print(f"  item_count          : {report['item_count']}")
print(f"  total_rejections    : {report['total_rejections']}")
print(f"  resolved_rejections : {report['resolved_rejections']}")
precheck_logs_in_report = [l for l in report["approval_logs"]
                           if l["action"] == "PRECHECK_IMPORT"]
print(f"  PRECHECK_IMPORT 日志数: {len(precheck_logs_in_report)}  (出现在审计报告里)")

ok_all &= check("验收报告 total_versions=2", report["total_versions"] == 2)
ok_all &= check("验收报告 驳回全解决", report["total_rejections"] == report["resolved_rejections"] == 2)
ok_all &= check("验收报告 包含 PRECHECK_IMPORT 审计日志", len(precheck_logs_in_report) >= 2)

# ──────────────────────────────────────────────────────────────────────
section(13, "链路总览 & 结论")
# ──────────────────────────────────────────────────────────────────────
print(f"""
  批次 ID   : {BATCH_ID}
  批次 CODE : {BATCH_CODE}
  清单版本  : v1(5条) → v2(7条)
  预检查次数: 4 次 (v1 新建 / v2 警告 / v2 重复复用 / pending_review 冲突)
  审批日志  : 含 PRECHECK_IMPORT ×2 + IMPORT_MANIFEST ×2 + 驳回 ×1 + 批准 ×1 + 归档 ×1

  用户视角新增能力核对清单:
    □ 导入前必须先预检查（无 token 导入直接 400 并提示）
    □ 预检查明确给出 NEW_VERSION / REUSE_VERSION / CONFLICT 三种结论
    □ 能看到批次状态阻塞（pending/approved/archived 不允许导入）
    □ 能看到未解决驳回 WARNING（不阻塞，但明确提醒条数）
    □ 预检查结果通过独立接口可查（latest / list），服务重启仍在
    □ 正式导入时校验 token 未过期、未被消费、内容哈希一致
    □ 预检查记录落在审批日志（PRECHECK_IMPORT），审计可追溯
    □ 仅本人 submitter 或 admin 能执行预检查/导入（权限收紧）
""")

print()
print("=" * 68)
if ok_all:
    print("🎉 所有检查项通过！用户可见验收链路完整跑通。")
    rc = 0
else:
    print("⚠️  存在未通过的检查项，请查看上方 ❌ 标记。")
    rc = 1
print("=" * 68)
sys.exit(rc)
