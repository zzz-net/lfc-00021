"""
链路核对脚本 —— 独立抽 4 条关键链路，和回归测试的结论做交叉验证
"""
import requests, json, io, zipfile, time, hashlib

API = "http://127.0.0.1:8003"
H_A = {"X-User-Id": "1"}  # admin
H_L = {"X-User-Id": "2"}  # lead
H_R = {"X-User-Id": "3"}  # reviewer
PASS = 0
FAIL = 0

def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}  --  {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  --  {detail}")

def mkzip():
    ARCHIVE_FORMAT_VERSION = "1.0"
    ts = int(time.time())
    def shab(s): return hashlib.sha256(s).hexdigest()
    def shah(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()
    sections_map = {
        "batch": "batch", "manifest_versions": "manifest_versions",
        "manifest_items": "manifest_items", "validation_results": "validation_results",
        "rejection_records": "rejection_records", "approval_logs": "approval_logs",
        "import_prechecks": "import_prechecks", "version_diff_snapshots": "version_diff_snapshots",
    }
    batch_data = {"id": None, "batch_code": f"LINK{ts}", "name": "链路核对",
        "description": "", "status": "archived", "submitter_id": 1,
        "current_manifest_version_id": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "archived_by": 1}
    versions = [{"id": ts, "batch_id": None, "version_number": 1, "import_format": "csv",
        "imported_by": 1, "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "item_count": 1, "validation_status": "passed",
        "validation_summary": {"total_rules":0,"passed":0,"failed":0},
        "content_hash": shah("x")}]
    items = [{"id": ts, "manifest_version_id": ts, "line_number": 2, "item_key": "001",
        "item_data": {"item_id":"001","item_name":"x","quantity":"1","unit_price":"1"}}]
    data_obj = {"batch": batch_data, "manifest_versions": versions, "manifest_items": items,
        "validation_results":[], "rejection_records":[], "approval_logs":[],
        "import_prechecks":[], "version_diff_snapshots":[]}
    cfg = {"validation_rules":[], "system_configs":[],
        "exported_at":time.strftime("%Y-%m-%dT%H:%M:%S")}
    item_counts = {}
    for s,k in sections_map.items():
        v = data_obj.get(k)
        item_counts[s] = len(v) if isinstance(v, list) else 1
    item_counts["system_config_snapshot"]=0
    item_counts["validation_rules_snapshot"]=0
    manifest = {"format_version":ARCHIVE_FORMAT_VERSION,"archive_id":shah(str(ts))[:24],
        "batch_code":batch_data["batch_code"],"batch_id_original":None,
        "generated_at":time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_by_user_id":1,"generated_by_username":"admin",
        "source_api_version":"1.0.0",
        "sections":list(sections_map.keys())+["system_config_snapshot","validation_rules_snapshot"],
        "total_bytes":0,"item_counts":item_counts,"notes":"link check"}
    sec_content = {}
    for s,k in sections_map.items():
        sec_content[s] = json.dumps(data_obj.get(k), ensure_ascii=False, default=str).encode("utf-8")
    cb = json.dumps(cfg, ensure_ascii=False, default=str).encode("utf-8")
    rb = json.dumps({"validation_rules":[]}, ensure_ascii=False, default=str).encode("utf-8")
    mb = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    hi = mb
    for s in sec_content: hi += sec_content[s]
    hi += cb + rb
    ch = shab(hi)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", mb)
        zf.writestr("hash.sha256", f"SHA256 {ch}\n")
        for s in sec_content: zf.writestr(f"data/{s}.json", sec_content[s])
        zf.writestr("data/system_config_snapshot.json", cb)
        zf.writestr("data/validation_rules_snapshot.json", rb)
    return buf.getvalue()

# ===== 链路1：旧入口 vs 新入口 规则分叉 =====
print("\n===== 链路1：旧入口(/api/system-configs) vs 新入口(/api/sandbox-config) 规则分叉 =====")
# 1a: 旧入口改 sandbox.enabled 必须拒绝
r = requests.put(f"{API}/api/system-configs/sandbox.enabled", headers=H_A,
    json={"config_value": "false"}, timeout=15)
check("L1-1 旧入口 PUT sandbox.enabled !=200", r.status_code != 200, f"status={r.status_code}")
if r.status_code == 400:
    body = r.json()
    msg = body.get("error",{}).get("message","") or body.get("detail","")
    check("L1-2 拒绝理由中包含'不允许修改'或'允许修改'",
          "不允许修改" in msg or "允许修改" in msg, f"msg={msg}")
# 1b: 新入口同样的键可改（admin）
r = requests.put(f"{API}/api/sandbox-config/sandbox.enabled", headers=H_A,
    json={"config_value": "true"}, timeout=15)
check("L1-3 新入口 PUT sandbox.enabled =200", r.status_code == 200, f"status={r.status_code}")
# 1c: lead 改新入口 403
r = requests.put(f"{API}/api/sandbox-config/sandbox.enabled", headers=H_L,
    json={"config_value": "true"}, timeout=15)
check("L1-4 lead 改新入口 =403", r.status_code == 403, f"status={r.status_code}")

# ===== 链路2：并发保护 409 + 不脏写 =====
print("\n===== 链路2：新入口 expected_old_value 并发保护：陈旧值 409 + 不脏写 =====")
# 2a: 重置基准值
requests.put(f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_A,
    json={"config_value": "12"}, timeout=15)
# 2b: 陈旧值（WRONG_OLD=99）写入，返回 409
r = requests.put(f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_A,
    json={"config_value": "999", "expected_old_value": "99"}, timeout=15)
check("L2-1 陈旧 expected_old_value -> 409", r.status_code == 409,
      f"status={r.status_code}, body={r.text[:150]}")
# 2c: 验证未被脏写，值仍为 12
r = requests.get(f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_A, timeout=15)
actual = r.json()["config_value"]
check("L2-2 冲突后未脏写，仍=12", actual == "12", f"actual={actual}")
# 2d: 用正确旧值重试，成功
r = requests.put(f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_A,
    json={"config_value": "72", "expected_old_value": "12"}, timeout=15)
check("L2-3 刷新值后重试成功 =200", r.status_code == 200, f"status={r.status_code}")
r = requests.get(f"{API}/api/sandbox-config/sandbox.auto_expire_hours", headers=H_A, timeout=15)
check("L2-4 刷新后写入生效 =72", r.json()["config_value"] == "72", f"actual={r.json()['config_value']}")

# ===== 链路3：reviewer 永远不能确认；lead 随 require_admin_confirm 翻转 =====
print("\n===== 链路3：reviewer 不能确认 + lead 能力随配置翻转 =====")
# 3a: 创建沙盒
requests.put(f"{API}/api/sandbox-config/sandbox.enabled", headers=H_A,
    json={"config_value": "true"}, timeout=15)
f = {"file": ("t.zip", mkzip(), "application/zip")}
r = requests.post(f"{API}/api/sandbox/restore", headers=H_L, files=f, timeout=15)
tok = r.json().get("sandbox_token")
check("L3-0 创建沙盒成功", tok is not None, f"status={r.status_code}")
if tok:
    # 3b: require_admin_confirm=true 时 lead 确认 =403
    requests.put(f"{API}/api/sandbox-config/sandbox.require_admin_confirm", headers=H_A,
        json={"config_value": "true"}, timeout=15)
    r = requests.post(f"{API}/api/sandbox/{tok}/confirm", headers=H_L,
        json={"comment":"x"}, timeout=15)
    check("L3-1 require_admin=true 时 lead confirm=403", r.status_code == 403,
          f"status={r.status_code}")
    # 3c: reviewer 确认 =403
    r = requests.post(f"{API}/api/sandbox/{tok}/confirm", headers=H_R,
        json={"comment":"x"}, timeout=15)
    check("L3-2 reviewer confirm 永远=403", r.status_code == 403, f"status={r.status_code}")
    # 3d: 切为 false，lead 能确认了（非 403）
    requests.put(f"{API}/api/sandbox-config/sandbox.require_admin_confirm", headers=H_A,
        json={"config_value": "false"}, timeout=15)
    r = requests.post(f"{API}/api/sandbox/{tok}/confirm", headers=H_L,
        json={"comment":"x"}, timeout=15)
    check("L3-3 require_admin=false 时 lead confirm!=403", r.status_code != 403,
          f"status={r.status_code}")
    # 3e: reviewer 仍 403
    r = requests.post(f"{API}/api/sandbox/{tok}/reject", headers=H_R,
        json={"reason":"x"}, timeout=15)
    check("L3-4 切为 false 后 reviewer reject 仍=403", r.status_code == 403,
          f"status={r.status_code}")

# ===== 链路4：eligibility 随配置翻转 =====
print("\n===== 链路4：/api/sandbox/{token}/eligibility can_confirm 字段翻转 =====")
f2 = {"file": ("t2.zip", mkzip(), "application/zip")}
r = requests.post(f"{API}/api/sandbox/restore", headers=H_L, files=f2, timeout=15)
tok2 = r.json().get("sandbox_token")
if tok2:
    requests.put(f"{API}/api/sandbox-config/sandbox.require_admin_confirm", headers=H_A,
        json={"config_value": "true"}, timeout=15)
    r = requests.get(f"{API}/api/sandbox/{tok2}/eligibility", headers=H_L, timeout=15)
    cc_true = r.json().get("can_confirm")
    check("L4-1 require_admin=true 时 lead can_confirm=False", cc_true == False,
          f"actual={cc_true}")
    requests.put(f"{API}/api/sandbox-config/sandbox.require_admin_confirm", headers=H_A,
        json={"config_value": "false"}, timeout=15)
    r = requests.get(f"{API}/api/sandbox/{tok2}/eligibility", headers=H_L, timeout=15)
    cc_false = r.json().get("can_confirm")
    check("L4-2 require_admin=false 时 lead can_confirm=True", cc_false == True,
          f"actual={cc_false}")
    check("L4-3 lead 的 can_confirm 完成 true<->false 翻转",
          (cc_true == False and cc_false == True), f"true时={cc_true}, false时={cc_false}")

# ===== 收尾 =====
print("\n===== 链路核对总结 =====")
print(f"通过: {PASS}, 失败: {FAIL}")
assert FAIL == 0, f"链路核对有 {FAIL} 项失败"
print("[OK] 全部链路核对通过，与回归测试结论一致")
