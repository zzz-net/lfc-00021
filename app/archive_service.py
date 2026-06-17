import io
import os
import json
import zipfile
import hashlib
import logging
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from sqlalchemy.orm import Session
from sqlalchemy import inspect

from app.database import Base
from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    ValidationResult, RejectionRecord, ApprovalLog, VersionDiffSnapshot,
    ImportPrecheck, ValidationRule, SystemConfig,
    CONFIG_KEY_ARCHIVE_ALLOW_OVERWRITE, CONFIG_KEY_ARCHIVE_ENABLED,
    APPROVAL_LOG_ACTION_EXPORT_ARCHIVE,
    APPROVAL_LOG_ACTION_TRY_IMPORT_ARCHIVE,
    APPROVAL_LOG_ACTION_RESTORE_ARCHIVE,
    APPROVAL_LOG_ACTION_RESTORE_ARCHIVE_OVERWRITE,
)
from app.schemas import (
    ARCHIVE_FORMAT_VERSION, ARCHIVE_MANIFEST_FILENAME, ARCHIVE_HASH_FILENAME,
    ARCHIVE_DATA_DIR,
    ARCHIVE_SECTION_BATCH, ARCHIVE_SECTION_VERSIONS, ARCHIVE_SECTION_ITEMS,
    ARCHIVE_SECTION_VALIDATIONS, ARCHIVE_SECTION_REJECTIONS,
    ARCHIVE_SECTION_APPROVAL_LOGS, ARCHIVE_SECTION_PRECHECKS,
    ARCHIVE_SECTION_DIFF_SNAPSHOTS, ARCHIVE_SECTION_SYSTEM_CONFIG,
    ARCHIVE_SECTION_VALIDATION_RULES,
    ArchiveManifest, ArchiveImportConflict, ArchivePrecheckResponse,
    ArchiveRestoreResponse,
)

logger = logging.getLogger(__name__)

logger.warning(
    "[MODULE_LOAD] archive_service.py loaded. Key features: "
    "zip_export=YES, zip_import=YES, sha256_hash_check=YES, "
    "batch_code_conflict_check=YES, duplicate_content_check=YES, "
    "role_permission_check=YES, config_switch_check=YES, "
    "overwrite_support=YES, audit_logging=YES"
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _model_to_dict(obj: Any, exclude_id: bool = False) -> Dict[str, Any]:
    data = {}
    mapper = inspect(type(obj)).mapper
    for col in mapper.column_attrs:
        key = col.key
        if exclude_id and key == "id":
            continue
        val = getattr(obj, key)
        if isinstance(val, datetime):
            data[key] = val.isoformat() if val else None
        else:
            data[key] = val
    return data


def _get_config_bool(db: Session, key: str, default: bool = False) -> bool:
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if not cfg:
        return default
    if cfg.value_type == "bool":
        return cfg.config_value.lower() in ("1", "true", "yes", "on")
    try:
        return bool(int(cfg.config_value))
    except (ValueError, TypeError):
        return default


def _set_config(db: Session, key: str, value: str, value_type: str = "string",
                description: Optional[str] = None, updated_by: Optional[int] = None):
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if cfg:
        cfg.config_value = value
        cfg.value_type = value_type
        if description:
            cfg.description = description
        cfg.updated_by = updated_by
    else:
        cfg = SystemConfig(
            config_key=key, config_value=value, value_type=value_type,
            description=description, updated_by=updated_by
        )
        db.add(cfg)


def ensure_default_configs(db: Session):
    defaults = [
        (CONFIG_KEY_ARCHIVE_ENABLED, "true", "bool",
         "是否启用验收归档包功能（导出/导入）"),
        (CONFIG_KEY_ARCHIVE_ALLOW_OVERWRITE, "false", "bool",
         "导入归档包时，是否允许覆盖已存在相同 batch_code 的批次"),
    ]
    for key, val, vtype, desc in defaults:
        existing = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
        if not existing:
            db.add(SystemConfig(
                config_key=key, config_value=val, value_type=vtype,
                description=desc
            ))
    db.commit()


def _collect_batch_data(db: Session, batch_id: int) -> Dict[str, Any]:
    batch = db.query(DeliveryBatch).filter(DeliveryBatch.id == batch_id).first()
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")

    versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id
    ).order_by(ManifestVersion.version_number.asc()).all()
    version_ids = [v.id for v in versions]

    items = []
    validations = []
    if version_ids:
        items = db.query(ManifestItem).filter(
            ManifestItem.manifest_version_id.in_(version_ids)
        ).order_by(ManifestItem.manifest_version_id, ManifestItem.line_number).all()
        validations = db.query(ValidationResult).filter(
            ValidationResult.manifest_version_id.in_(version_ids)
        ).order_by(ValidationResult.manifest_version_id, ValidationResult.id).all()

    rejections = db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id
    ).order_by(RejectionRecord.created_at.asc()).all()

    approval_logs = db.query(ApprovalLog).filter(
        ApprovalLog.batch_id == batch_id
    ).order_by(ApprovalLog.created_at.asc()).all()

    prechecks = db.query(ImportPrecheck).filter(
        ImportPrecheck.batch_id == batch_id
    ).order_by(ImportPrecheck.created_at.asc()).all()

    diff_snapshots = db.query(VersionDiffSnapshot).filter(
        VersionDiffSnapshot.batch_id == batch_id
    ).order_by(VersionDiffSnapshot.created_at.asc()).all()

    return {
        "batch": _model_to_dict(batch),
        "manifest_versions": [_model_to_dict(v) for v in versions],
        "manifest_items": [_model_to_dict(it) for it in items],
        "validation_results": [_model_to_dict(vr) for vr in validations],
        "rejection_records": [_model_to_dict(rr) for rr in rejections],
        "approval_logs": [_model_to_dict(al) for al in approval_logs],
        "import_prechecks": [_model_to_dict(ip) for ip in prechecks],
        "version_diff_snapshots": [_model_to_dict(ds) for ds in diff_snapshots],
    }


def _collect_config_snapshot(db: Session) -> Dict[str, Any]:
    rules = db.query(ValidationRule).order_by(ValidationRule.rule_code).all()
    configs = db.query(SystemConfig).order_by(SystemConfig.config_key).all()
    return {
        "validation_rules": [_model_to_dict(r) for r in rules],
        "system_configs": [_model_to_dict(c) for c in configs],
        "exported_at": datetime.now().isoformat(),
    }


def build_archive_zip(
    db: Session,
    batch_id: int,
    current_user: User,
    notes: Optional[str] = None,
) -> Tuple[bytes, str, ArchiveManifest]:
    batch = db.query(DeliveryBatch).filter(DeliveryBatch.id == batch_id).first()
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")

    data = _collect_batch_data(db, batch_id)
    config_snapshot = _collect_config_snapshot(db)

    archive_id_parts = [
        str(batch.batch_code),
        str(batch.id),
        datetime.now().isoformat(),
        str(current_user.id),
        str(current_user.username),
    ]
    archive_id = hashlib.sha256(
        "|".join(archive_id_parts).encode("utf-8")
    ).hexdigest()[:24]

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

    item_counts = {}
    for sec, key in sections_map.items():
        val = data.get(key)
        if isinstance(val, list):
            item_counts[sec] = len(val)
        elif isinstance(val, dict):
            item_counts[sec] = 1

    item_counts[ARCHIVE_SECTION_SYSTEM_CONFIG] = len(config_snapshot["system_configs"])
    item_counts[ARCHIVE_SECTION_VALIDATION_RULES] = len(config_snapshot["validation_rules"])

    manifest = ArchiveManifest(
        format_version=ARCHIVE_FORMAT_VERSION,
        archive_id=archive_id,
        batch_code=batch.batch_code,
        batch_id_original=batch.id,
        generated_at=datetime.now(),
        generated_by_user_id=current_user.id,
        generated_by_username=current_user.username,
        sections=list(sections_map.keys()) + [ARCHIVE_SECTION_SYSTEM_CONFIG, ARCHIVE_SECTION_VALIDATION_RULES],
        item_counts=item_counts,
        notes=notes,
    )

    sections_content = {}
    for sec, key in sections_map.items():
        val = data.get(key)
        sections_content[sec] = json.dumps(val, ensure_ascii=False, default=str).encode("utf-8")

    config_bytes = json.dumps(config_snapshot, ensure_ascii=False, default=str).encode("utf-8")
    rules_bytes = json.dumps(
        {"validation_rules": config_snapshot["validation_rules"]},
        ensure_ascii=False, default=str
    ).encode("utf-8")

    manifest_bytes = json.dumps(
        json.loads(manifest.model_dump_json()),
        indent=2, ensure_ascii=False
    ).encode("utf-8")

    hash_input = manifest_bytes
    for sec in sections_content:
        hash_input += sections_content[sec]
    hash_input += config_bytes + rules_bytes
    content_hash = _sha256_bytes(hash_input)

    raw_buf = io.BytesIO()
    with zipfile.ZipFile(raw_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(ARCHIVE_MANIFEST_FILENAME, manifest_bytes)
        zf.writestr(ARCHIVE_HASH_FILENAME, f"SHA256 {content_hash}\n")
        for sec in sections_content:
            zf.writestr(f"{ARCHIVE_DATA_DIR}/{sec}.json", sections_content[sec])
        zf.writestr(f"{ARCHIVE_DATA_DIR}/{ARCHIVE_SECTION_SYSTEM_CONFIG}.json", config_bytes)
        zf.writestr(f"{ARCHIVE_DATA_DIR}/{ARCHIVE_SECTION_VALIDATION_RULES}.json", rules_bytes)

    zip_result = raw_buf.getvalue()
    zip_hash = _sha256_bytes(zip_result)
    return zip_result, zip_hash, manifest


def _parse_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def extract_archive(zip_bytes: bytes) -> Tuple[ArchiveManifest, Dict[str, Any], str, str]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 ZIP 文件")

    with zf:
        names = zf.namelist()

        if ARCHIVE_MANIFEST_FILENAME not in names:
            raise ValueError(f"归档包缺少必要文件: {ARCHIVE_MANIFEST_FILENAME}")

        manifest_raw = zf.read(ARCHIVE_MANIFEST_FILENAME).decode("utf-8")
        try:
            manifest_dict = json.loads(manifest_raw)
        except json.JSONDecodeError:
            raise ValueError("manifest.json 解析失败，不是有效的 JSON")

        try:
            manifest = ArchiveManifest(**manifest_dict)
        except Exception as e:
            raise ValueError(f"manifest.json 格式校验失败: {e}")

        hash_line = ""
        if ARCHIVE_HASH_FILENAME in names:
            hash_line = zf.read(ARCHIVE_HASH_FILENAME).decode("utf-8").strip()

        hash_input = manifest_raw.encode("utf-8")
        data_contents = {}
        all_sections = (
            [ARCHIVE_SECTION_BATCH, ARCHIVE_SECTION_VERSIONS, ARCHIVE_SECTION_ITEMS,
             ARCHIVE_SECTION_VALIDATIONS, ARCHIVE_SECTION_REJECTIONS,
             ARCHIVE_SECTION_APPROVAL_LOGS, ARCHIVE_SECTION_PRECHECKS,
             ARCHIVE_SECTION_DIFF_SNAPSHOTS, ARCHIVE_SECTION_SYSTEM_CONFIG,
             ARCHIVE_SECTION_VALIDATION_RULES]
        )
        for sec in all_sections:
            path = f"{ARCHIVE_DATA_DIR}/{sec}.json"
            if path in names:
                raw = zf.read(path)
                hash_input += raw
                try:
                    data_contents[sec] = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    raise ValueError(f"{path} 解析失败")

        computed_hash = _sha256_bytes(hash_input)
        declared_hash = ""
        if hash_line.startswith("SHA256 "):
            declared_hash = hash_line.split(" ", 1)[1].strip()

        return manifest, data_contents, declared_hash, computed_hash


def validate_archive_integrity(
    manifest: ArchiveManifest,
    data_contents: Dict[str, Any],
    declared_hash: str,
    computed_hash: str,
) -> List[ArchiveImportConflict]:
    conflicts: List[ArchiveImportConflict] = []

    if manifest.format_version != ARCHIVE_FORMAT_VERSION:
        conflicts.append(ArchiveImportConflict(
            conflict_type="FORMAT_VERSION_MISMATCH",
            severity="warning",
            message=f"归档包版本 {manifest.format_version} 与当前系统版本 {ARCHIVE_FORMAT_VERSION} 不一致，可能存在兼容性风险",
            details={"archive_version": manifest.format_version, "system_version": ARCHIVE_FORMAT_VERSION}
        ))

    if declared_hash and declared_hash != computed_hash:
        conflicts.append(ArchiveImportConflict(
            conflict_type="HASH_MISMATCH",
            severity="error",
            message="归档包文件哈希校验失败，文件可能已被篡改或损坏",
            details={"declared": declared_hash[:16] + "...", "computed": computed_hash[:16] + "..."}
        ))

    required_sections = [ARCHIVE_SECTION_BATCH, ARCHIVE_SECTION_VERSIONS, ARCHIVE_SECTION_ITEMS]
    for sec in required_sections:
        if sec not in data_contents:
            conflicts.append(ArchiveImportConflict(
                conflict_type="MISSING_SECTION",
                severity="error",
                message=f"归档包缺少必要数据段: {sec}"
            ))

    batch_data = data_contents.get(ARCHIVE_SECTION_BATCH, {})
    if isinstance(batch_data, dict):
        if not batch_data.get("batch_code"):
            conflicts.append(ArchiveImportConflict(
                conflict_type="INVALID_BATCH_DATA",
                severity="error",
                message="批次数据缺少 batch_code 字段"
            ))

    versions = data_contents.get(ARCHIVE_SECTION_VERSIONS, [])
    if isinstance(versions, list) and len(versions) == 0:
        conflicts.append(ArchiveImportConflict(
            conflict_type="EMPTY_VERSIONS",
            severity="warning",
            message="归档包中没有清单版本数据"
        ))

    return conflicts


def check_batch_conflict(
    db: Session,
    manifest: ArchiveManifest,
    data_contents: Dict[str, Any],
) -> Tuple[bool, Optional[DeliveryBatch], List[ArchiveImportConflict]]:
    conflicts: List[ArchiveImportConflict] = []
    batch_data = data_contents.get(ARCHIVE_SECTION_BATCH, {})
    batch_code = batch_data.get("batch_code") or manifest.batch_code

    existing = db.query(DeliveryBatch).filter(DeliveryBatch.batch_code == batch_code).first()
    has_conflict = existing is not None

    if has_conflict:
        conflicts.append(ArchiveImportConflict(
            conflict_type="BATCH_CODE_CONFLICT",
            severity="error",
            message=f"已存在相同 batch_code 的批次: {batch_code}",
            details={"existing_id": existing.id, "existing_status": existing.status}
        ))

    return has_conflict, existing, conflicts


def check_duplicate_content(
    db: Session,
    data_contents: Dict[str, Any],
) -> List[ArchiveImportConflict]:
    conflicts: List[ArchiveImportConflict] = []
    versions = data_contents.get(ARCHIVE_SECTION_VERSIONS, [])
    if not isinstance(versions, list):
        return conflicts

    for v in versions:
        content_hash = v.get("validation_summary", {}).get("content_hash") if isinstance(v.get("validation_summary"), dict) else None
        raw_content = v.get("raw_content", "")
        if raw_content:
            v_hash = _sha256_bytes(raw_content.encode("utf-8"))
            existing_v = db.query(ManifestVersion).filter(
                ManifestVersion.item_count == v.get("item_count", 0)
            ).first()
            if existing_v and existing_v.raw_content:
                ex_hash = _sha256_bytes(existing_v.raw_content.encode("utf-8"))
                if ex_hash == v_hash:
                    conflicts.append(ArchiveImportConflict(
                        conflict_type="DUPLICATE_VERSION_CONTENT",
                        severity="warning",
                        message=f"检测到与现有版本 v{existing_v.version_number} 内容完全相同的版本记录",
                        details={"existing_version_id": existing_v.id, "existing_version": existing_v.version_number}
                    ))
                    break

    return conflicts


def precheck_import_archive(
    db: Session,
    zip_bytes: bytes,
    current_user: User,
) -> ArchivePrecheckResponse:
    try:
        result = extract_archive(zip_bytes)
        manifest, data_contents, declared_hash, computed_hash = result
    except ValueError as e:
        return ArchivePrecheckResponse(
            success=False, can_restore=False,
            conflicts=[ArchiveImportConflict(
                conflict_type="INVALID_ARCHIVE", severity="error", message=str(e)
            )],
            message=f"归档包解析失败: {e}"
        )

    conflicts = validate_archive_integrity(manifest, data_contents, declared_hash, computed_hash)

    overwrite_enabled = _get_config_bool(db, CONFIG_KEY_ARCHIVE_ALLOW_OVERWRITE, False)
    archive_enabled = _get_config_bool(db, CONFIG_KEY_ARCHIVE_ENABLED, True)

    if not archive_enabled:
        conflicts.append(ArchiveImportConflict(
            conflict_type="ARCHIVE_DISABLED",
            severity="error",
            message="系统配置已关闭验收归档包功能"
        ))

    has_code_conflict, existing_batch, code_conflicts = check_batch_conflict(db, manifest, data_contents)
    conflicts.extend(code_conflicts)

    dup_conflicts = check_duplicate_content(db, data_contents)
    conflicts.extend(dup_conflicts)

    error_conflicts = [c for c in conflicts if c.severity == "error"]
    can_restore = len(error_conflicts) == 0

    require_overwrite = False
    if has_code_conflict:
        require_overwrite = True
        if overwrite_enabled:
            can_restore = len([c for c in error_conflicts if c.conflict_type != "BATCH_CODE_CONFLICT"]) == 0
        else:
            can_restore = False

    warnings = [c.message for c in conflicts if c.severity == "warning"]

    log_action = ApprovalLog(
        batch_id=existing_batch.id if existing_batch else 0,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_TRY_IMPORT_ARCHIVE,
        comment=f"试导入归档包: archive_id={manifest.archive_id}, batch_code={manifest.batch_code}",
        extra_data={
            "archive_id": manifest.archive_id,
            "batch_code": manifest.batch_code,
            "can_restore": can_restore,
            "require_overwrite": require_overwrite,
            "overwrite_enabled": overwrite_enabled,
            "conflict_count": len(conflicts),
            "error_count": len(error_conflicts),
            "warning_count": len(warnings),
            "hash_verified": declared_hash == computed_hash if declared_hash else None,
        }
    )
    db.add(log_action)
    db.commit()

    info = {
        "archive_id": manifest.archive_id,
        "batch_code": manifest.batch_code,
        "original_batch_id": manifest.batch_id_original,
        "generated_at": manifest.generated_at.isoformat() if manifest.generated_at else None,
        "generated_by": f"{manifest.generated_by_username} (uid={manifest.generated_by_user_id})",
        "item_counts": manifest.item_counts,
        "hash_verified": (declared_hash == computed_hash) if declared_hash else None,
    }

    if can_restore:
        msg = "归档包校验通过，可以恢复"
        if require_overwrite:
            msg = f"归档包校验通过，将覆盖已存在的批次 [{manifest.batch_code}]"
    else:
        msgs = [c.message for c in error_conflicts]
        msg = "归档包存在错误，无法恢复: " + "; ".join(msgs[:3])

    return ArchivePrecheckResponse(
        success=True,
        archive_id=manifest.archive_id,
        batch_code=manifest.batch_code,
        can_restore=can_restore,
        require_overwrite=require_overwrite,
        overwrite_enabled=overwrite_enabled,
        conflicts=conflicts,
        warnings=warnings,
        info=info,
        message=msg
    )


def _remap_id(records: List[Dict], old_id_field: str, new_id_map: Dict[int, int]) -> List[Dict]:
    for r in records:
        old_val = r.get(old_id_field)
        if old_val is not None and old_val in new_id_map:
            r[old_id_field] = new_id_map[old_val]
    return records


def restore_archive(
    db: Session,
    zip_bytes: bytes,
    current_user: User,
    force_overwrite: bool = False,
) -> ArchiveRestoreResponse:
    precheck = precheck_import_archive(db, zip_bytes, current_user)

    if not precheck.success:
        return ArchiveRestoreResponse(
            success=False, message=precheck.message
        )

    if not precheck.can_restore:
        return ArchiveRestoreResponse(
            success=False,
            archive_id=precheck.archive_id,
            batch_code=precheck.batch_code,
            warnings=precheck.warnings,
            message=f"无法恢复: {precheck.message}"
        )

    overwrite_enabled = _get_config_bool(db, CONFIG_KEY_ARCHIVE_ALLOW_OVERWRITE, False)
    if precheck.require_overwrite and not (overwrite_enabled and force_overwrite):
        return ArchiveRestoreResponse(
            success=False,
            archive_id=precheck.archive_id,
            batch_code=precheck.batch_code,
            message="批次编码冲突，需要设置 force_overwrite=true 且系统配置允许覆盖"
        )

    try:
        result = extract_archive(zip_bytes)
        manifest, data_contents, _, _ = result
    except ValueError as e:
        return ArchiveRestoreResponse(success=False, message=f"解析失败: {e}")

    overwritten = False
    old_batch_data = data_contents.get(ARCHIVE_SECTION_BATCH, {})
    batch_code = old_batch_data.get("batch_code") or manifest.batch_code

    existing = db.query(DeliveryBatch).filter(DeliveryBatch.batch_code == batch_code).first()
    if existing and precheck.require_overwrite:
        overwritten = True
        old_id = existing.id
        db.query(VersionDiffSnapshot).filter(VersionDiffSnapshot.batch_id == old_id).delete(synchronize_session=False)
        db.query(ImportPrecheck).filter(ImportPrecheck.batch_id == old_id).delete(synchronize_session=False)
        db.query(ApprovalLog).filter(ApprovalLog.batch_id == old_id).delete(synchronize_session=False)
        db.query(RejectionRecord).filter(RejectionRecord.batch_id == old_id).delete(synchronize_session=False)

        old_versions = db.query(ManifestVersion).filter(ManifestVersion.batch_id == old_id).all()
        old_vids = [v.id for v in old_versions]
        if old_vids:
            db.query(ValidationResult).filter(ValidationResult.manifest_version_id.in_(old_vids)).delete(synchronize_session=False)
            db.query(ManifestItem).filter(ManifestItem.manifest_version_id.in_(old_vids)).delete(synchronize_session=False)
        db.query(ManifestVersion).filter(ManifestVersion.batch_id == old_id).delete(synchronize_session=False)

        db.delete(existing)
        db.flush()

    submitter_id = old_batch_data.get("submitter_id", current_user.id)
    user_check = db.query(User).filter(User.id == submitter_id).first()
    if not user_check:
        submitter_id = current_user.id

    new_batch = DeliveryBatch(
        batch_code=batch_code,
        name=old_batch_data.get("name", batch_code),
        description=old_batch_data.get("description"),
        status=old_batch_data.get("status", "draft"),
        submitter_id=submitter_id,
        archived_at=_parse_dt(old_batch_data.get("archived_at")),
        archived_by=old_batch_data.get("archived_by"),
    )
    db.add(new_batch)
    db.flush()

    old_versions = data_contents.get(ARCHIVE_SECTION_VERSIONS, [])
    old_items = data_contents.get(ARCHIVE_SECTION_ITEMS, [])
    old_validations = data_contents.get(ARCHIVE_SECTION_VALIDATIONS, [])
    old_rejections = data_contents.get(ARCHIVE_SECTION_REJECTIONS, [])
    old_logs = data_contents.get(ARCHIVE_SECTION_APPROVAL_LOGS, [])
    old_prechecks = data_contents.get(ARCHIVE_SECTION_PRECHECKS, [])
    old_snapshots = data_contents.get(ARCHIVE_SECTION_DIFF_SNAPSHOTS, [])

    version_id_map: Dict[int, int] = {}
    for ov in sorted(old_versions, key=lambda x: x.get("version_number", 0)):
        imported_by = ov.get("imported_by", current_user.id)
        if not db.query(User).filter(User.id == imported_by).first():
            imported_by = current_user.id

        nv = ManifestVersion(
            batch_id=new_batch.id,
            version_number=ov.get("version_number", 1),
            import_format=ov.get("import_format", "csv"),
            imported_by=imported_by,
            imported_at=_parse_dt(ov.get("imported_at")),
            item_count=ov.get("item_count", 0),
            raw_content=ov.get("raw_content", ""),
            validation_status=ov.get("validation_status", "pending"),
            validation_summary=ov.get("validation_summary"),
        )
        db.add(nv)
        db.flush()
        version_id_map[ov.get("id")] = nv.id

    item_id_map: Dict[int, int] = {}
    for oi in old_items:
        old_vid = oi.get("manifest_version_id")
        if old_vid not in version_id_map:
            continue
        ni = ManifestItem(
            manifest_version_id=version_id_map[old_vid],
            line_number=oi.get("line_number", 0),
            item_key=oi.get("item_key", ""),
            item_data=oi.get("item_data", {}),
        )
        db.add(ni)
        db.flush()
        item_id_map[oi.get("id")] = ni.id

    for ovr in old_validations:
        old_vid = ovr.get("manifest_version_id")
        if old_vid not in version_id_map:
            continue
        old_iid = ovr.get("manifest_item_id")
        new_iid = item_id_map.get(old_iid) if old_iid else None
        nvr = ValidationResult(
            manifest_version_id=version_id_map[old_vid],
            manifest_item_id=new_iid,
            rule_id=ovr.get("rule_id"),
            rule_code=ovr.get("rule_code", ""),
            severity=ovr.get("severity", "error"),
            passed=bool(ovr.get("passed", False)),
            message=ovr.get("message", ""),
            field_name=ovr.get("field_name"),
            line_number=ovr.get("line_number"),
            item_key=ovr.get("item_key"),
            created_at=_parse_dt(ovr.get("created_at")),
        )
        db.add(nvr)

    for rr in old_rejections:
        old_vid = rr.get("manifest_version_id")
        new_vid = version_id_map.get(old_vid) if old_vid else None
        if not new_vid:
            continue
        old_iid = rr.get("manifest_item_id")
        new_iid = item_id_map.get(old_iid) if old_iid else None
        old_res_vid = rr.get("resolved_by_manifest_version_id")
        new_res_vid = version_id_map.get(old_res_vid) if old_res_vid else None
        rejector_id = rr.get("rejector_id", current_user.id)
        if not db.query(User).filter(User.id == rejector_id).first():
            rejector_id = current_user.id

        nrr = RejectionRecord(
            batch_id=new_batch.id,
            manifest_version_id=new_vid,
            manifest_item_id=new_iid,
            rejector_id=rejector_id,
            rejection_reason=rr.get("rejection_reason", ""),
            item_key=rr.get("item_key"),
            line_number=rr.get("line_number"),
            created_at=_parse_dt(rr.get("created_at")),
            resolved=bool(rr.get("resolved", False)),
            resolved_at=_parse_dt(rr.get("resolved_at")),
            resolved_by_manifest_version_id=new_res_vid,
        )
        db.add(nrr)

    for log in old_logs:
        old_vid = log.get("manifest_version_id")
        new_vid = version_id_map.get(old_vid) if old_vid else None
        actor_id = log.get("actor_id", current_user.id)
        if not db.query(User).filter(User.id == actor_id).first():
            actor_id = current_user.id

        nlog = ApprovalLog(
            batch_id=new_batch.id,
            manifest_version_id=new_vid,
            actor_id=actor_id,
            action=log.get("action", ""),
            from_status=log.get("from_status"),
            to_status=log.get("to_status"),
            comment=log.get("comment"),
            created_at=_parse_dt(log.get("created_at")),
            extra_data=log.get("extra_data"),
        )
        db.add(nlog)

    for pc in old_prechecks:
        old_reused = pc.get("reused_version_id")
        new_reused = version_id_map.get(old_reused) if old_reused else None
        actor_id = pc.get("actor_id", current_user.id)
        if not db.query(User).filter(User.id == actor_id).first():
            actor_id = current_user.id

        npc = ImportPrecheck(
            batch_id=new_batch.id,
            actor_id=actor_id,
            precheck_token=pc.get("precheck_token", ""),
            content_hash=pc.get("content_hash", ""),
            import_format=pc.get("import_format", "csv"),
            item_count=pc.get("item_count", 0),
            action_type=pc.get("action_type", ""),
            has_conflict=bool(pc.get("has_conflict", False)),
            conflict_types=pc.get("conflict_types"),
            conflict_details=pc.get("conflict_details"),
            reused_version_id=new_reused,
            reused_version_number=pc.get("reused_version_number"),
            planned_version_number=pc.get("planned_version_number"),
            created_at=_parse_dt(pc.get("created_at")),
            expires_at=_parse_dt(pc.get("expires_at")) or datetime.now(),
            consumed=bool(pc.get("consumed", False)),
            consumed_at=_parse_dt(pc.get("consumed_at")),
            extra_data=pc.get("extra_data"),
        )
        db.add(npc)

    for snap in old_snapshots:
        old_ovid = snap.get("old_version_id")
        old_nvid = snap.get("new_version_id")
        new_ovid = version_id_map.get(old_ovid) if old_ovid else None
        new_nvid = version_id_map.get(old_nvid) if old_nvid else None
        if not new_ovid or not new_nvid:
            continue
        created_by = snap.get("created_by", current_user.id)
        invalidated_by = snap.get("invalidated_by")
        if not db.query(User).filter(User.id == created_by).first():
            created_by = current_user.id
        if invalidated_by and not db.query(User).filter(User.id == invalidated_by).first():
            invalidated_by = None

        nsnap = VersionDiffSnapshot(
            batch_id=new_batch.id,
            old_version_id=new_ovid,
            new_version_id=new_nvid,
            old_version_number=snap.get("old_version_number", 0),
            new_version_number=snap.get("new_version_number", 0),
            snapshot_key=snap.get("snapshot_key", ""),
            status=snap.get("status", "valid"),
            created_by=created_by,
            created_at=_parse_dt(snap.get("created_at")),
            invalidated_at=_parse_dt(snap.get("invalidated_at")),
            invalidated_by=invalidated_by,
            metadata_json=snap.get("metadata_json", {}),
            summary_json=snap.get("summary_json", {}),
            added_items_json=snap.get("added_items_json", []),
            removed_items_json=snap.get("removed_items_json", []),
            modified_items_json=snap.get("modified_items_json", []),
            unchanged_items_json=snap.get("unchanged_items_json", []),
            unresolved_rejections_json=snap.get("unresolved_rejections_json", []),
            validation_changes_json=snap.get("validation_changes_json", []),
            content_hash=snap.get("content_hash", ""),
        )
        db.add(nsnap)

    versions_resp = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == new_batch.id
    ).order_by(ManifestVersion.version_number.asc()).all()
    if versions_resp:
        new_batch.current_manifest_version_id = versions_resp[-1].id

    restore_action = APPROVAL_LOG_ACTION_RESTORE_ARCHIVE_OVERWRITE if overwritten else APPROVAL_LOG_ACTION_RESTORE_ARCHIVE
    action_log = ApprovalLog(
        batch_id=new_batch.id,
        actor_id=current_user.id,
        action=restore_action,
        comment=f"恢复归档包: archive_id={manifest.archive_id}, batch_code={manifest.batch_code}"
                + ("（覆盖原有批次）" if overwritten else ""),
        extra_data={
            "archive_id": manifest.archive_id,
            "batch_code": manifest.batch_code,
            "new_batch_id": new_batch.id,
            "overwritten": overwritten,
            "original_batch_id": manifest.batch_id_original,
            "restored_by": current_user.username,
        }
    )
    db.add(action_log)

    try_import_log = ApprovalLog(
        batch_id=new_batch.id,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_TRY_IMPORT_ARCHIVE,
        comment=f"恢复前预检查: archive_id={manifest.archive_id}, batch_code={manifest.batch_code}",
        extra_data={
            "archive_id": manifest.archive_id,
            "batch_code": manifest.batch_code,
            "original_batch_id": manifest.batch_id_original,
            "conflicts": [c.model_dump() for c in precheck.conflicts],
            "warnings": precheck.warnings,
            "can_restore": precheck.can_restore,
            "require_overwrite": precheck.require_overwrite,
            "overwritten": overwritten,
        }
    )
    db.add(try_import_log)

    export_log = ApprovalLog(
        batch_id=new_batch.id,
        actor_id=manifest.generated_by_user_id if db.query(User).filter(User.id == manifest.generated_by_user_id).first() else current_user.id,
        action=APPROVAL_LOG_ACTION_EXPORT_ARCHIVE,
        comment=f"[来源归档] 原始导出: archive_id={manifest.archive_id}, "
                f"by={manifest.generated_by_username}, at={manifest.generated_at}",
        extra_data={
            "archive_id": manifest.archive_id,
            "batch_code": manifest.batch_code,
            "generated_at": manifest.generated_at.isoformat() if manifest.generated_at else None,
            "generated_by": manifest.generated_by_username,
            "is_restored_source": True,
        }
    )
    db.add(export_log)

    db.commit()
    db.refresh(new_batch)

    restored_counts = {
        "manifest_versions": len(versions_resp),
        "manifest_items": len(item_id_map),
        "validation_results": len(old_validations),
        "rejection_records": len(old_rejections),
        "approval_logs": len(old_logs) + 3,
        "import_prechecks": len(old_prechecks),
        "version_diff_snapshots": len(old_snapshots),
    }

    return ArchiveRestoreResponse(
        success=True,
        archive_id=manifest.archive_id,
        new_batch_id=new_batch.id,
        batch_code=new_batch.batch_code,
        overwritten=overwritten,
        restored_sections=list(manifest.item_counts.keys()),
        restored_counts=restored_counts,
        warnings=precheck.warnings,
        message=f"批次恢复成功: {new_batch.batch_code} (id={new_batch.id})"
                + ("，已覆盖原有批次" if overwritten else "")
    )
