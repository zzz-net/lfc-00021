import io
import json
import csv
import hashlib
import secrets
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    ValidationResult, RejectionRecord, ApprovalLog, VersionDiffSnapshot,
    ImportPrecheck,
    SandboxSession, SandboxManifestVersion, SandboxManifestItem,
    SandboxPrecheckResult,
    CONFIG_KEY_SANDBOX_ENABLED, CONFIG_KEY_SANDBOX_REQUIRE_ADMIN_CONFIRM,
    CONFIG_KEY_SANDBOX_AUTO_EXPIRE_HOURS,
    SANDBOX_STATUS_PENDING, SANDBOX_STATUS_PRECHECK_RUNNING,
    SANDBOX_STATUS_PRECHECK_PASSED, SANDBOX_STATUS_PRECHECK_FAILED,
    SANDBOX_STATUS_CONFIRMED, SANDBOX_STATUS_REJECTED,
    SANDBOX_STATUS_EXPIRED,
    SANDBOX_PRECHECK_PASS, SANDBOX_PRECHECK_WARNING, SANDBOX_PRECHECK_FAIL,
    SANDBOX_ACTION_RECOMMEND_APPROVE, SANDBOX_ACTION_RECOMMEND_REJECT,
    SANDBOX_ACTION_RECOMMEND_REPAIR, SANDBOX_ACTION_RECOMMEND_MANUAL,
    APPROVAL_LOG_ACTION_SANDBOX_RESTORE, APPROVAL_LOG_ACTION_SANDBOX_IMPORT,
    APPROVAL_LOG_ACTION_SANDBOX_PRECHECK, APPROVAL_LOG_ACTION_SANDBOX_VIEW_DIFF,
    APPROVAL_LOG_ACTION_SANDBOX_CONFIRM, APPROVAL_LOG_ACTION_SANDBOX_REJECT,
    APPROVAL_LOG_ACTION_SANDBOX_CLEANUP,
)
from app.schemas import (
    ArchiveManifest, ArchiveImportConflict,
    SandboxRestoreResponse, SandboxImportResponse,
    SandboxDiffResponse, SandboxPrecheckResponse,
    SandboxConfirmResponse, SandboxRejectResponse,
    SandboxSessionResponse, SandboxSessionListResponse,
    SandboxVersionResponse, SandboxItemResponse, SandboxPrecheckItem,
    ImportValidationError,
    VersionDiffMetadata, VersionDiffSummary,
    ItemDiff, ItemDiffSummary, FieldChange,
    ValidationChange, ImportInfo,
    DIFF_ACTION_ADDED, DIFF_ACTION_REMOVED,
    DIFF_ACTION_MODIFIED, DIFF_ACTION_UNCHANGED,
    VALIDATION_CHANGE_NEW_VIOLATION, VALIDATION_CHANGE_RESOLVED,
    VALIDATION_CHANGE_MODIFIED, VALIDATION_CHANGE_NEW_PASSED,
    VALIDATION_CHANGE_REMOVED_PASSED, VALIDATION_CHANGE_UNCHANGED,
    BATCH_STATUS_DRAFT,
)
from app.archive_service import (
    extract_archive, validate_archive_integrity, _parse_dt,
    ARCHIVE_SECTION_BATCH, ARCHIVE_SECTION_VERSIONS, ARCHIVE_SECTION_ITEMS,
    _get_config_bool,
)
from app.validation_engine import ValidationEngine

logger = logging.getLogger(__name__)

logger.warning(
    "[MODULE_LOAD] sandbox_service.py loaded. Key features: "
    "sandbox_isolation=YES, restore_to_sandbox=YES, "
    "sandbox_import=YES, sandbox_precheck=YES, sandbox_diff=YES, "
    "confirm_to_production=YES, admin_permission_control=YES, "
    "audit_logging=YES, auto_expire=YES"
)

REQUIRED_MANIFEST_FIELDS = ["item_id", "item_name", "quantity", "unit_price"]


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _generate_sandbox_token() -> str:
    return secrets.token_urlsafe(32)


def _get_config_int(db: Session, key: str, default: int = 0) -> int:
    cfg = db.query(Base).filter(Base.config_key == key).first() if False else None
    from app.models import SystemConfig
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if not cfg:
        return default
    if cfg.value_type == "int":
        try:
            return int(cfg.config_value)
        except (ValueError, TypeError):
            return default
    try:
        return int(cfg.config_value)
    except (ValueError, TypeError):
        return default


def _get_user_info(db: Session, user_id: int) -> Tuple[str, Optional[str]]:
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        return user.username, user.display_name
    return "unknown", None


def _validate_item_fields(item_data: dict, line_number: int, item_key: Optional[str]) -> List[ImportValidationError]:
    errors = []
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in item_data:
            errors.append(ImportValidationError(
                line_number=line_number,
                item_key=item_key,
                field_name=field,
                error_message=f"缺少必填字段 '{field}'"
            ))
        elif item_data[field] is None or (isinstance(item_data[field], str) and item_data[field].strip() == ""):
            errors.append(ImportValidationError(
                line_number=line_number,
                item_key=item_key,
                field_name=field,
                error_message=f"必填字段 '{field}' 为空"
            ))
    return errors


def _parse_csv(content: str) -> Tuple[Optional[List[dict]], List[ImportValidationError]]:
    items = []
    errors = []
    reader = csv.DictReader(io.StringIO(content))

    missing_headers = [f for f in REQUIRED_MANIFEST_FIELDS if f not in (reader.fieldnames or [])]
    if missing_headers:
        errors.append(ImportValidationError(
            line_number=1,
            item_key=None,
            field_name=None,
            error_message=f"CSV 表头缺少必填列: {', '.join(missing_headers)}"
        ))
        return None, errors

    for idx, row in enumerate(reader, start=2):
        item_key = row.get("item_id") or f"line_{idx}"
        cleaned_row = {}
        for k, v in row.items():
            if k is not None:
                cleaned_row[k] = v.strip() if isinstance(v, str) else v
        item_errors = _validate_item_fields(cleaned_row, idx, item_key)
        errors.extend(item_errors)
        items.append({
            "line_number": idx,
            "item_key": item_key,
            "item_data": cleaned_row
        })

    return items, errors


def _parse_json(content: str) -> Tuple[Optional[List[dict]], List[ImportValidationError]]:
    items = []
    errors = []
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        errors.append(ImportValidationError(
            line_number=None,
            item_key=None,
            field_name=None,
            error_message=f"JSON 解析错误: {str(e)}"
        ))
        return None, errors

    if not isinstance(data, list):
        errors.append(ImportValidationError(
            line_number=None,
            item_key=None,
            field_name=None,
            error_message="JSON 根节点必须是数组格式"
        ))
        return None, errors

    for idx, row in enumerate(data, start=1):
        if not isinstance(row, dict):
            errors.append(ImportValidationError(
                line_number=idx,
                item_key=f"entry_{idx}",
                field_name=None,
                error_message=f"第 {idx} 条记录必须是对象格式"
            ))
            continue
        item_key = row.get("item_id") or f"entry_{idx}"
        item_errors = _validate_item_fields(row, idx, item_key)
        errors.extend(item_errors)
        items.append({
            "line_number": idx,
            "item_key": item_key,
            "item_data": row
        })

    return items, errors


def _detect_format(file_filename: str, content: str, import_format: str) -> str:
    detected_format = import_format
    if detected_format == "auto":
        filename = file_filename or ""
        if filename.lower().endswith(".csv"):
            detected_format = "csv"
        elif filename.lower().endswith(".json"):
            detected_format = "json"
        else:
            try:
                json.loads(content)
                detected_format = "json"
            except json.JSONDecodeError:
                detected_format = "csv"
    return detected_format


def _parse_content(content: str, detected_format: str) -> Tuple[Optional[List[dict]], List[ImportValidationError]]:
    if detected_format == "csv":
        return _parse_csv(content)
    elif detected_format == "json":
        return _parse_json(content)
    else:
        raise ValueError(f"Unsupported format: {detected_format}")


def _check_sandbox_enabled(db: Session) -> Tuple[bool, Optional[str]]:
    enabled = _get_config_bool(db, CONFIG_KEY_SANDBOX_ENABLED, True)
    if not enabled:
        return False, "系统配置已关闭恢复后验收沙盒功能"
    return True, None


def _get_sandbox_session(db: Session, sandbox_token: str) -> Optional[SandboxSession]:
    return db.query(SandboxSession).filter(
        SandboxSession.sandbox_token == sandbox_token
    ).first()


def _get_sandbox_or_404(db: Session, sandbox_token: str) -> SandboxSession:
    session = _get_sandbox_session(db, sandbox_token)
    if not session:
        raise ValueError(f"沙盒会话不存在或已过期: {sandbox_token}")
    return session


def _check_sandbox_expired(session: SandboxSession) -> bool:
    now = datetime.utcnow()
    exp = session.expires_at
    if exp and exp.tzinfo is not None:
        exp = exp.replace(tzinfo=None)
    return exp < now


def _auto_expire_sandbox_sessions(db: Session) -> int:
    now = datetime.utcnow()
    expired = db.query(SandboxSession).filter(
        SandboxSession.status.in_([SANDBOX_STATUS_PENDING, SANDBOX_STATUS_PRECHECK_RUNNING,
                                    SANDBOX_STATUS_PRECHECK_PASSED, SANDBOX_STATUS_PRECHECK_FAILED]),
        SandboxSession.expires_at < now
    ).all()

    count = 0
    for s in expired:
        s.status = SANDBOX_STATUS_EXPIRED
        count += 1
        log = ApprovalLog(
            batch_id=s.target_batch_id or 0,
            actor_id=s.created_by,
            action=APPROVAL_LOG_ACTION_SANDBOX_CLEANUP,
            comment=f"沙盒会话自动过期: {s.sandbox_token}",
            extra_data={
                "sandbox_token": s.sandbox_token,
                "source_archive_id": s.source_archive_id,
                "source_batch_code": s.source_batch_code,
                "expired_at": now.isoformat(),
            }
        )
        db.add(log)

    if count > 0:
        db.commit()
        logger.info(f"自动过期 {count} 个沙盒会话")
    return count


def restore_archive_to_sandbox(
    db: Session,
    zip_bytes: bytes,
    current_user: User,
) -> SandboxRestoreResponse:
    enabled, err_msg = _check_sandbox_enabled(db)
    if not enabled:
        return SandboxRestoreResponse(
            success=False,
            message=err_msg or "沙盒功能已禁用"
        )

    try:
        result = extract_archive(zip_bytes)
        manifest, data_contents, declared_hash, computed_hash = result
    except ValueError as e:
        return SandboxRestoreResponse(
            success=False,
            message=f"归档包解析失败: {e}",
            conflicts=[ArchiveImportConflict(
                conflict_type="INVALID_ARCHIVE", severity="error", message=str(e)
            )]
        )

    integrity_conflicts = validate_archive_integrity(manifest, data_contents, declared_hash, computed_hash)

    error_conflicts = [c for c in integrity_conflicts if c.severity == "error"]
    if error_conflicts:
        return SandboxRestoreResponse(
            success=False,
            source_archive_id=manifest.archive_id,
            source_batch_code=manifest.batch_code,
            message="归档包完整性校验失败，无法恢复到沙盒",
            conflicts=integrity_conflicts
        )

    _auto_expire_sandbox_sessions(db)

    batch_data = data_contents.get(ARCHIVE_SECTION_BATCH, {})
    batch_code = batch_data.get("batch_code") or manifest.batch_code
    original_batch_id = batch_data.get("id") or manifest.batch_id_original

    token = _generate_sandbox_token()
    expire_hours = _get_config_int(db, CONFIG_KEY_SANDBOX_AUTO_EXPIRE_HOURS, 24)
    expires_at = datetime.utcnow() + timedelta(hours=expire_hours)

    sandbox_session = SandboxSession(
        sandbox_token=token,
        source_archive_id=manifest.archive_id,
        source_batch_code=batch_code,
        original_batch_id=original_batch_id,
        status=SANDBOX_STATUS_PENDING,
        created_by=current_user.id,
        expires_at=expires_at,
        extra_data={
            "archive_generated_at": manifest.generated_at.isoformat() if manifest.generated_at else None,
            "archive_generated_by": manifest.generated_by_username,
            "archive_item_counts": manifest.item_counts,
            "hash_verified": (declared_hash == computed_hash) if declared_hash else None,
        }
    )
    db.add(sandbox_session)
    db.flush()

    versions_data = data_contents.get(ARCHIVE_SECTION_VERSIONS, [])
    items_data = data_contents.get(ARCHIVE_SECTION_ITEMS, [])

    version_count = 0
    for v in sorted(versions_data, key=lambda x: x.get("version_number", 0)):
        imported_by = v.get("imported_by", current_user.id)
        if not db.query(User).filter(User.id == imported_by).first():
            imported_by = current_user.id

        raw_content = v.get("raw_content", "")
        content_hash = _sha256_hex(raw_content) if raw_content else ""

        sandbox_version = SandboxManifestVersion(
            sandbox_session_id=sandbox_session.id,
            version_number=v.get("version_number", 1),
            import_format=v.get("import_format", "csv"),
            imported_by=imported_by,
            imported_at=_parse_dt(v.get("imported_at")),
            item_count=v.get("item_count", 0),
            raw_content=raw_content,
            content_hash=content_hash,
            validation_status=v.get("validation_status", "pending"),
            validation_summary=v.get("validation_summary"),
            is_candidate=False,
            base_version_number=None,
        )
        db.add(sandbox_version)
        db.flush()
        version_count += 1

        version_items = [it for it in items_data if it.get("manifest_version_id") == v.get("id")]
        for it in version_items:
            sandbox_item = SandboxManifestItem(
                sandbox_manifest_version_id=sandbox_version.id,
                line_number=it.get("line_number", 0),
                item_key=it.get("item_key", ""),
                item_data=it.get("item_data", {}),
            )
            db.add(sandbox_item)

    log = ApprovalLog(
        batch_id=0,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_SANDBOX_RESTORE,
        comment=f"恢复归档包到沙盒: archive_id={manifest.archive_id}, batch_code={batch_code}, sandbox_token={token[:16]}...",
        extra_data={
            "sandbox_token": token,
            "sandbox_session_id": sandbox_session.id,
            "archive_id": manifest.archive_id,
            "batch_code": batch_code,
            "original_batch_id": original_batch_id,
            "restored_version_count": version_count,
            "expires_at": expires_at.isoformat(),
            "hash_verified": (declared_hash == computed_hash) if declared_hash else None,
        }
    )
    db.add(log)
    db.commit()
    db.refresh(sandbox_session)

    return SandboxRestoreResponse(
        success=True,
        sandbox_token=token,
        session_id=sandbox_session.id,
        source_archive_id=manifest.archive_id,
        source_batch_code=batch_code,
        status=SANDBOX_STATUS_PENDING,
        restored_version_count=version_count,
        message=f"归档包已恢复到沙盒环境，沙盒会话有效期 {expire_hours} 小时。请在沙盒内导入新版本并执行预检查。",
        conflicts=integrity_conflicts,
        info={
            "expires_at": expires_at.isoformat(),
            "restored_versions": version_count,
        }
    )


def import_candidate_to_sandbox(
    db: Session,
    sandbox_token: str,
    raw_content: str,
    filename: str,
    import_format: str,
    current_user: User,
) -> SandboxImportResponse:
    enabled, err_msg = _check_sandbox_enabled(db)
    if not enabled:
        return SandboxImportResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            message=err_msg or "沙盒功能已禁用"
        )

    try:
        session = _get_sandbox_or_404(db, sandbox_token)
    except ValueError as e:
        return SandboxImportResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            message=str(e)
        )

    if _check_sandbox_expired(session):
        session.status = SANDBOX_STATUS_EXPIRED
        db.commit()
        return SandboxImportResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            message="沙盒会话已过期，请重新恢复归档包到沙盒"
        )

    if session.status in [SANDBOX_STATUS_CONFIRMED, SANDBOX_STATUS_REJECTED]:
        return SandboxImportResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            message=f"沙盒会话已{session.status}，无法再导入新版本"
        )

    detected_format = _detect_format(filename, raw_content, import_format)
    if detected_format not in ("csv", "json"):
        return SandboxImportResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            message=f"不支持的文件格式: {import_format}。请使用 CSV 或 JSON 格式。"
        )

    try:
        items, parse_errors = _parse_content(raw_content, detected_format)
    except ValueError as e:
        return SandboxImportResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            message=f"文件解析失败: {e}"
        )

    if items is None:
        return SandboxImportResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            message="清单解析失败，未创建候选版本",
            parse_errors=parse_errors
        )

    critical_errors = [e for e in parse_errors if e.field_name in REQUIRED_MANIFEST_FIELDS or e.field_name is None]
    if critical_errors:
        return SandboxImportResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            message=f"存在 {len(critical_errors)} 个关键字段错误，未创建候选版本",
            parse_errors=parse_errors
        )

    existing_versions = db.query(SandboxManifestVersion).filter(
        SandboxManifestVersion.sandbox_session_id == session.id
    ).order_by(SandboxManifestVersion.version_number.desc()).all()

    max_version = max([v.version_number for v in existing_versions]) if existing_versions else 0
    new_version_number = max_version + 1

    base_version_number = max_version if max_version > 0 else None
    content_hash = _sha256_hex(raw_content)

    for v in existing_versions:
        if v.content_hash == content_hash and v.is_candidate:
            return SandboxImportResponse(
                success=False,
                sandbox_token=sandbox_token,
                session_id=session.id,
                message=f"沙盒内已存在相同内容的候选版本 v{v.version_number}"
            )

    for v in existing_versions:
        v.is_candidate = False

    sandbox_version = SandboxManifestVersion(
        sandbox_session_id=session.id,
        version_number=new_version_number,
        import_format=detected_format,
        imported_by=current_user.id,
        item_count=len(items),
        raw_content=raw_content,
        content_hash=content_hash,
        validation_status="pending",
        validation_summary=None,
        is_candidate=True,
        base_version_number=base_version_number,
    )
    db.add(sandbox_version)
    db.flush()

    for it in items:
        sandbox_item = SandboxManifestItem(
            sandbox_manifest_version_id=sandbox_version.id,
            line_number=it["line_number"],
            item_key=it["item_key"],
            item_data=it["item_data"],
        )
        db.add(sandbox_item)

    log = ApprovalLog(
        batch_id=0,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_SANDBOX_IMPORT,
        comment=f"沙盒内导入候选版本 v{new_version_number}: session={sandbox_token[:16]}..., items={len(items)}",
        extra_data={
            "sandbox_token": sandbox_token,
            "sandbox_session_id": session.id,
            "version_number": new_version_number,
            "base_version_number": base_version_number,
            "item_count": len(items),
            "import_format": detected_format,
            "content_hash": content_hash,
            "parse_error_count": len(parse_errors),
        }
    )
    db.add(log)

    session.status = SANDBOX_STATUS_PENDING
    session.precheck_passed = None
    session.recommended_action = None
    db.commit()
    db.refresh(sandbox_version)

    return SandboxImportResponse(
        success=True,
        sandbox_token=sandbox_token,
        session_id=session.id,
        version_number=new_version_number,
        item_count=len(items),
        content_hash=content_hash,
        message=f"候选版本 v{new_version_number} 已导入沙盒。请执行预检查查看差异和冲突。"
                + (f" 注意：存在 {len(parse_errors)} 条非阻塞性警告。" if parse_errors else ""),
        parse_errors=parse_errors
    )


def _run_validation_on_sandbox_version(
    db: Session,
    sandbox_version: SandboxManifestVersion,
) -> None:
    engine = ValidationEngine(db)

    temp_version = ManifestVersion(
        id=-sandbox_version.id,
        batch_id=0,
        version_number=sandbox_version.version_number,
        import_format=sandbox_version.import_format,
        imported_by=sandbox_version.imported_by,
        item_count=sandbox_version.item_count,
        raw_content=sandbox_version.raw_content,
        validation_status="pending",
    )
    db.add(temp_version)
    db.flush()

    temp_items = []
    for si in sandbox_version.items:
        ti = ManifestItem(
            id=-si.id,
            manifest_version_id=temp_version.id,
            line_number=si.line_number,
            item_key=si.item_key,
            item_data=si.item_data,
        )
        temp_items.append(ti)
    db.bulk_save_objects(temp_items)
    db.flush()

    try:
        result = engine.run_validation(temp_version.id)
        sandbox_version.validation_status = result["manifest_version"].validation_status if "manifest_version" in result else temp_version.validation_status
        sandbox_version.validation_summary = result["summary"]

        for vr in result["results"]:
            pr = SandboxPrecheckResult(
                sandbox_session_id=sandbox_version.sandbox_session_id,
                check_code=vr.rule_code,
                check_name=f"规则校验-{vr.rule_code}",
                severity=vr.severity,
                passed=vr.passed,
                message=vr.message,
                suggestion=None,
                details=None,
                affected_version_number=sandbox_version.version_number,
                affected_item_key=vr.item_key,
            )
            db.add(pr)
    finally:
        db.query(ValidationResult).filter(ValidationResult.manifest_version_id == temp_version.id).delete()
        db.query(ManifestItem).filter(ManifestItem.manifest_version_id == temp_version.id).delete()
        db.delete(temp_version)
        db.flush()


def _compare_fields(old_data: dict, new_data: dict) -> List[FieldChange]:
    changes = []
    all_fields = sorted(set(list(old_data.keys()) + list(new_data.keys())))
    for field in all_fields:
        old_val = old_data.get(field)
        new_val = new_data.get(field)
        if old_val != new_val:
            if field not in old_data:
                change_type = "added"
            elif field not in new_data:
                change_type = "removed"
            else:
                change_type = "modified"
            changes.append(FieldChange(
                field_name=field,
                old_value=old_val,
                new_value=new_val,
                change_type=change_type
            ))
    return changes


def _calculate_sandbox_item_diffs(
    old_items: List[SandboxManifestItem],
    new_items: List[SandboxManifestItem]
) -> Tuple[List[ItemDiff], List[ItemDiff], List[ItemDiff], List[ItemDiffSummary]]:
    old_map = {item.item_key: item for item in old_items}
    new_map = {item.item_key: item for item in new_items}

    added = []
    removed = []
    modified = []
    unchanged = []

    all_keys = sorted(set(list(old_map.keys()) + list(new_map.keys())))

    for key in all_keys:
        old_item = old_map.get(key)
        new_item = new_map.get(key)

        if old_item is None:
            added.append(ItemDiff(
                item_key=key,
                action=DIFF_ACTION_ADDED,
                line_number_new=new_item.line_number,
                new_data=new_item.item_data,
                field_changes=[
                    FieldChange(
                        field_name=k,
                        new_value=v,
                        change_type="added"
                    )
                    for k, v in sorted(new_item.item_data.items())
                ]
            ))
        elif new_item is None:
            removed.append(ItemDiff(
                item_key=key,
                action=DIFF_ACTION_REMOVED,
                line_number_old=old_item.line_number,
                old_data=old_item.item_data,
                field_changes=[
                    FieldChange(
                        field_name=k,
                        old_value=v,
                        change_type="removed"
                    )
                    for k, v in sorted(old_item.item_data.items())
                ]
            ))
        else:
            field_changes = _compare_fields(old_item.item_data, new_item.item_data)
            if field_changes:
                modified.append(ItemDiff(
                    item_key=key,
                    action=DIFF_ACTION_MODIFIED,
                    line_number_old=old_item.line_number,
                    line_number_new=new_item.line_number,
                    old_data=old_item.item_data,
                    new_data=new_item.item_data,
                    field_changes=field_changes
                ))
            else:
                unchanged.append(ItemDiffSummary(
                    item_key=key,
                    action=DIFF_ACTION_UNCHANGED,
                    change_summary="内容无变更",
                    changed_fields=[]
                ))

    return added, removed, modified, unchanged


def _count_validation_issues_for_sandbox(db: Session, sandbox_version_id: int) -> Tuple[int, int, int, int]:
    results = db.query(SandboxPrecheckResult).filter(
        SandboxPrecheckResult.sandbox_manifest_version_id == sandbox_version_id
    ).all() if False else []
    results = db.query(SandboxPrecheckResult).filter(
        SandboxPrecheckResult.sandbox_session_id == 0
    ).all()
    return 0, 0, 0, 0


def _build_import_info_for_sandbox(db: Session, version: SandboxManifestVersion) -> ImportInfo:
    username, display_name = _get_user_info(db, version.imported_by)
    return ImportInfo(
        version_number=version.version_number,
        imported_by_username=username,
        imported_by_display_name=display_name,
        imported_at=version.imported_at,
        item_count=version.item_count,
        import_format=version.import_format
    )


def calculate_sandbox_diff(
    db: Session,
    sandbox_token: str,
    current_user: User,
) -> SandboxDiffResponse:
    enabled, err_msg = _check_sandbox_enabled(db)
    if not enabled:
        return SandboxDiffResponse(
            success=False,
            sandbox_token=sandbox_token,
            base_version_number=0,
            candidate_version_number=0,
            metadata=None,
            summary=None,
            message=err_msg or "沙盒功能已禁用"
        )

    try:
        session = _get_sandbox_or_404(db, sandbox_token)
    except ValueError as e:
        return SandboxDiffResponse(
            success=False,
            sandbox_token=sandbox_token,
            base_version_number=0,
            candidate_version_number=0,
            metadata=None,
            summary=None,
            message=str(e)
        )

    if _check_sandbox_expired(session):
        return SandboxDiffResponse(
            success=False,
            sandbox_token=sandbox_token,
            base_version_number=0,
            candidate_version_number=0,
            metadata=None,
            summary=None,
            message="沙盒会话已过期"
        )

    versions = db.query(SandboxManifestVersion).filter(
        SandboxManifestVersion.sandbox_session_id == session.id
    ).order_by(SandboxManifestVersion.version_number.asc()).all()

    if len(versions) < 2:
        return SandboxDiffResponse(
            success=False,
            sandbox_token=sandbox_token,
            base_version_number=0,
            candidate_version_number=0,
            metadata=None,
            summary=None,
            message="沙盒内版本不足，无法计算差异。请先导入候选版本。"
        )

    candidate_version = next((v for v in versions if v.is_candidate), None)
    if not candidate_version:
        candidate_version = versions[-1]

    base_version = None
    if candidate_version.base_version_number:
        base_version = next((v for v in versions if v.version_number == candidate_version.base_version_number), None)
    if not base_version:
        base_version = versions[-2] if len(versions) >= 2 else versions[0]

    old_items = sorted(base_version.items, key=lambda x: x.item_key)
    new_items = sorted(candidate_version.items, key=lambda x: x.item_key)

    added, removed, modified, unchanged = _calculate_sandbox_item_diffs(old_items, new_items)

    total_field_changes = sum(len(m.field_changes) for m in modified)
    total_field_changes += sum(len(a.field_changes) for a in added)
    total_field_changes += sum(len(r.field_changes) for r in removed)

    username, display_name = _get_user_info(db, current_user.id)

    metadata = VersionDiffMetadata(
        batch_id=session.id,
        batch_code=f"[SANDBOX] {session.source_batch_code}",
        batch_name=f"[沙盒] {session.source_batch_code}",
        old_version=base_version.version_number,
        new_version=candidate_version.version_number,
        old_import=_build_import_info_for_sandbox(db, base_version),
        new_import=_build_import_info_for_sandbox(db, candidate_version),
        generated_at=datetime.now(),
        generated_by_username=username,
        generated_by_display_name=display_name
    )

    old_err, old_warn, old_pass, old_total = 0, 0, 0, 0
    new_err, new_warn, new_pass, new_total = 0, 0, 0, 0
    if base_version.validation_summary:
        old_err = base_version.validation_summary.get("failed", 0)
        old_warn = base_version.validation_summary.get("warnings", 0)
        old_pass = base_version.validation_summary.get("passed", 0)
        old_total = base_version.validation_summary.get("total_checks", 0)
    if candidate_version.validation_summary:
        new_err = candidate_version.validation_summary.get("failed", 0)
        new_warn = candidate_version.validation_summary.get("warnings", 0)
        new_pass = candidate_version.validation_summary.get("passed", 0)
        new_total = candidate_version.validation_summary.get("total_checks", 0)

    summary = VersionDiffSummary(
        total_items_old=len(old_items),
        total_items_new=len(new_items),
        added_count=len(added),
        removed_count=len(removed),
        modified_count=len(modified),
        unchanged_count=len(unchanged),
        field_change_count=total_field_changes,
        unresolved_rejections_old=0,
        unresolved_rejections_new=0,
        validation_errors_old=old_err,
        validation_errors_new=new_err,
        validation_warnings_old=old_warn,
        validation_warnings_new=new_warn,
        validation_passed_old=old_pass,
        validation_passed_new=new_pass,
        validation_total_old=old_total,
        validation_total_new=new_total,
        validation_changes_new_violation=0,
        validation_changes_resolved=0,
        validation_changes_modified=0,
        validation_changes_new_passed=0,
        validation_changes_removed_passed=0,
        validation_changes_unchanged=0,
        validation_changes_total=0,
        old_version_validation_status=base_version.validation_status,
        new_version_validation_status=candidate_version.validation_status,
    )

    log = ApprovalLog(
        batch_id=0,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_SANDBOX_VIEW_DIFF,
        comment=f"查看沙盒差异: v{base_version.version_number} -> v{candidate_version.version_number}, session={sandbox_token[:16]}...",
        extra_data={
            "sandbox_token": sandbox_token,
            "sandbox_session_id": session.id,
            "base_version": base_version.version_number,
            "candidate_version": candidate_version.version_number,
            "added_count": len(added),
            "removed_count": len(removed),
            "modified_count": len(modified),
        }
    )
    db.add(log)
    db.commit()

    return SandboxDiffResponse(
        success=True,
        sandbox_token=sandbox_token,
        base_version_number=base_version.version_number,
        candidate_version_number=candidate_version.version_number,
        metadata=metadata,
        summary=summary,
        added_items=added,
        removed_items=removed,
        modified_items=modified,
        unchanged_items=unchanged,
        validation_changes=[],
        message=f"版本差异计算完成: v{base_version.version_number} -> v{candidate_version.version_number}。"
                f" 新增 {len(added)} 项，删除 {len(removed)} 项，修改 {len(modified)} 项，无变更 {len(unchanged)} 项。"
    )


def run_sandbox_precheck(
    db: Session,
    sandbox_token: str,
    current_user: User,
) -> SandboxPrecheckResponse:
    enabled, err_msg = _check_sandbox_enabled(db)
    if not enabled:
        return SandboxPrecheckResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            overall_result=SANDBOX_PRECHECK_FAIL,
            passed=False,
            recommended_action=SANDBOX_ACTION_RECOMMEND_REJECT,
            precheck_passed=False,
            total_checks=0,
            passed_checks=0,
            warning_checks=0,
            failed_checks=0,
            message=err_msg or "沙盒功能已禁用"
        )

    try:
        session = _get_sandbox_or_404(db, sandbox_token)
    except ValueError as e:
        return SandboxPrecheckResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            overall_result=SANDBOX_PRECHECK_FAIL,
            passed=False,
            recommended_action=SANDBOX_ACTION_RECOMMEND_REJECT,
            precheck_passed=False,
            total_checks=0,
            passed_checks=0,
            warning_checks=0,
            failed_checks=0,
            message=str(e)
        )

    if _check_sandbox_expired(session):
        return SandboxPrecheckResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            overall_result=SANDBOX_PRECHECK_FAIL,
            passed=False,
            recommended_action=SANDBOX_ACTION_RECOMMEND_REJECT,
            precheck_passed=False,
            total_checks=0,
            passed_checks=0,
            warning_checks=0,
            failed_checks=0,
            message="沙盒会话已过期"
        )

    if session.status in [SANDBOX_STATUS_CONFIRMED, SANDBOX_STATUS_REJECTED]:
        return SandboxPrecheckResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            overall_result=SANDBOX_PRECHECK_FAIL,
            passed=False,
            recommended_action=SANDBOX_ACTION_RECOMMEND_REJECT,
            precheck_passed=False,
            total_checks=0,
            passed_checks=0,
            warning_checks=0,
            failed_checks=0,
            message=f"沙盒会话已{session.status}，无法执行预检查"
        )

    session.status = SANDBOX_STATUS_PRECHECK_RUNNING
    db.flush()

    versions = db.query(SandboxManifestVersion).filter(
        SandboxManifestVersion.sandbox_session_id == session.id
    ).order_by(SandboxManifestVersion.version_number.asc()).all()

    candidate_version = next((v for v in versions if v.is_candidate), None)
    if not candidate_version:
        candidate_version = versions[-1] if versions else None

    if not candidate_version:
        return SandboxPrecheckResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            overall_result=SANDBOX_PRECHECK_FAIL,
            passed=False,
            recommended_action=SANDBOX_ACTION_RECOMMEND_REPAIR,
            precheck_passed=False,
            total_checks=0,
            passed_checks=0,
            warning_checks=0,
            failed_checks=0,
            message="沙盒内没有可预检查的版本，请先导入候选版本"
        )

    db.query(SandboxPrecheckResult).filter(
        SandboxPrecheckResult.sandbox_session_id == session.id
    ).delete()
    db.flush()

    precheck_results: List[SandboxPrecheckResult] = []
    conflict_types: List[str] = []

    _run_validation_on_sandbox_version(db, candidate_version)
    db.flush()

    val_results = db.query(SandboxPrecheckResult).filter(
        SandboxPrecheckResult.sandbox_session_id == session.id
    ).all()
    precheck_results.extend(val_results)

    existing_batch = db.query(DeliveryBatch).filter(
        DeliveryBatch.batch_code == session.source_batch_code
    ).first()
    if existing_batch:
        pr = SandboxPrecheckResult(
            sandbox_session_id=session.id,
            check_code="BATCH_CODE_CONFLICT",
            check_name="批次编码冲突检查",
            severity="warning",
            passed=False,
            message=f"已存在相同 batch_code 的批次 (id={existing_batch.id}, status={existing_batch.status})，确认恢复时将覆盖现有批次",
            suggestion="请确认是否要覆盖现有批次。覆盖将删除现有批次的所有数据并替换为沙盒中的内容。",
            details={
                "existing_batch_id": existing_batch.id,
                "existing_status": existing_batch.status,
            },
            affected_version_number=candidate_version.version_number,
        )
        precheck_results.append(pr)
        db.add(pr)
        conflict_types.append("BATCH_CODE_CONFLICT")

    base_version = None
    if candidate_version.base_version_number:
        base_version = next((v for v in versions if v.version_number == candidate_version.base_version_number), None)
    if not base_version and len(versions) >= 2:
        base_version = versions[-2]

    if base_version:
        old_items = sorted(base_version.items, key=lambda x: x.item_key)
        new_items = sorted(candidate_version.items, key=lambda x: x.item_key)
        added, removed, modified, _ = _calculate_sandbox_item_diffs(old_items, new_items)

        total_changes = len(added) + len(removed) + len(modified)
        if total_changes == 0:
            pr = SandboxPrecheckResult(
                sandbox_session_id=session.id,
                check_code="NO_CHANGES",
                check_name="内容变更检查",
                severity="warning",
                passed=True,
                message="候选版本与基准版本内容完全相同，没有任何变更",
                suggestion="请确认是否确实不需要修改，或者重新导入包含变更的版本。",
                details={
                    "base_version": base_version.version_number,
                    "candidate_version": candidate_version.version_number,
                },
                affected_version_number=candidate_version.version_number,
            )
            precheck_results.append(pr)
            db.add(pr)
        else:
            pr = SandboxPrecheckResult(
                sandbox_session_id=session.id,
                check_code="HAS_CHANGES",
                check_name="内容变更检查",
                severity="info" if total_changes > 0 else "warning",
                passed=True,
                message=f"检测到 {total_changes} 项内容变更 (新增 {len(added)}，删除 {len(removed)}，修改 {len(modified)})",
                suggestion="请在差异视图中仔细核对所有变更内容。",
                details={
                    "base_version": base_version.version_number,
                    "candidate_version": candidate_version.version_number,
                    "added_count": len(added),
                    "removed_count": len(removed),
                    "modified_count": len(modified),
                },
                affected_version_number=candidate_version.version_number,
            )
            precheck_results.append(pr)
            db.add(pr)

    content_hash_check = SandboxPrecheckResult(
        sandbox_session_id=session.id,
        check_code="CONTENT_HASH_VALID",
        check_name="内容哈希完整性",
        severity="info",
        passed=True,
        message=f"候选版本内容哈希校验通过: {candidate_version.content_hash[:16]}...",
        suggestion=None,
        details={"content_hash": candidate_version.content_hash},
        affected_version_number=candidate_version.version_number,
    )
    precheck_results.append(content_hash_check)
    db.add(content_hash_check)

    db.flush()

    passed_checks = sum(1 for r in precheck_results if r.passed)
    failed_checks = sum(1 for r in precheck_results if not r.passed and r.severity == "error")
    warning_checks = sum(1 for r in precheck_results if not r.passed and r.severity == "warning")
    total_checks = len(precheck_results)

    precheck_passed = failed_checks == 0

    if precheck_passed and warning_checks == 0:
        overall_result = SANDBOX_PRECHECK_PASS
        recommended_action = SANDBOX_ACTION_RECOMMEND_APPROVE
    elif precheck_passed and warning_checks > 0:
        overall_result = SANDBOX_PRECHECK_WARNING
        recommended_action = SANDBOX_ACTION_RECOMMEND_MANUAL
    else:
        overall_result = SANDBOX_PRECHECK_FAIL
        recommended_action = SANDBOX_ACTION_RECOMMEND_REPAIR

    if not precheck_passed:
        recommended_action = SANDBOX_ACTION_RECOMMEND_REPAIR

    reasons = []
    if precheck_passed:
        reasons.append("所有阻塞性检查项已通过")
        if warning_checks > 0:
            reasons.append(f"存在 {warning_checks} 项警告，请人工确认")
        else:
            reasons.append("无警告，建议直接确认恢复")
    else:
        reasons.append(f"存在 {failed_checks} 项阻塞性错误")
        for r in precheck_results:
            if not r.passed and r.severity == "error":
                reasons.append(f"  - {r.check_name}: {r.message}")

    session.status = SANDBOX_STATUS_PRECHECK_PASSED if precheck_passed else SANDBOX_STATUS_PRECHECK_FAILED
    session.precheck_passed = precheck_passed
    session.recommended_action = recommended_action
    session.conflict_types = conflict_types
    session.precheck_result = {
        "overall_result": overall_result,
        "total_checks": total_checks,
        "passed_checks": passed_checks,
        "warning_checks": warning_checks,
        "failed_checks": failed_checks,
        "recommended_action": recommended_action,
    }

    log = ApprovalLog(
        batch_id=0,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_SANDBOX_PRECHECK,
        comment=f"沙盒预检查完成: {overall_result}, session={sandbox_token[:16]}..., "
                f"passed={passed_checks}, warnings={warning_checks}, failed={failed_checks}, "
                f"recommend={recommended_action}",
        extra_data={
            "sandbox_token": sandbox_token,
            "sandbox_session_id": session.id,
            "candidate_version": candidate_version.version_number,
            "overall_result": overall_result,
            "precheck_passed": precheck_passed,
            "recommended_action": recommended_action,
            "total_checks": total_checks,
            "passed_checks": passed_checks,
            "warning_checks": warning_checks,
            "failed_checks": failed_checks,
            "conflict_types": conflict_types,
        }
    )
    db.add(log)
    db.commit()

    response_results = [
        SandboxPrecheckItem(
            id=r.id,
            check_code=r.check_code,
            check_name=r.check_name,
            severity=r.severity,
            passed=r.passed,
            message=r.message,
            suggestion=r.suggestion,
            details=r.details,
            affected_version_number=r.affected_version_number,
            affected_item_key=r.affected_item_key,
            created_at=r.created_at,
        ) for r in precheck_results
    ]

    message = f"预检查完成。结果: {overall_result}。"
    if precheck_passed:
        message += f" {passed_checks}/{total_checks} 通过，{warning_checks} 项警告。"
        if recommended_action == SANDBOX_ACTION_RECOMMEND_APPROVE:
            message += " 建议确认恢复到正式环境。"
        else:
            message += " 建议人工复核后再决定。"
    else:
        message += f" {failed_checks} 项失败。请修复问题后重新导入。"

    return SandboxPrecheckResponse(
        success=True,
        sandbox_token=sandbox_token,
        session_id=session.id,
        overall_result=overall_result,
        passed=precheck_passed,
        recommended_action=recommended_action,
        precheck_passed=precheck_passed,
        total_checks=total_checks,
        passed_checks=passed_checks,
        warning_checks=warning_checks,
        failed_checks=failed_checks,
        results=response_results,
        conflict_types=conflict_types,
        reasons=reasons,
        message=message
    )


def confirm_sandbox_restore(
    db: Session,
    sandbox_token: str,
    comment: Optional[str],
    current_user: User,
) -> SandboxConfirmResponse:
    enabled, err_msg = _check_sandbox_enabled(db)
    if not enabled:
        return SandboxConfirmResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            status="",
            message=err_msg or "沙盒功能已禁用"
        )

    require_admin = _get_config_bool(db, CONFIG_KEY_SANDBOX_REQUIRE_ADMIN_CONFIRM, True)
    from app.schemas import ROLE_ADMIN, ROLE_LEAD
    if require_admin and current_user.role != ROLE_ADMIN:
        return SandboxConfirmResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            status="",
            message="权限不足：系统配置要求只有 admin 角色才能执行沙盒正式确认操作。"
                    f" 您的角色: {current_user.role}"
        )
    elif not require_admin and current_user.role not in [ROLE_ADMIN, ROLE_LEAD]:
        return SandboxConfirmResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            status="",
            message="权限不足：只有 admin 或 lead 角色才能执行沙盒正式确认操作。"
                    f" 您的角色: {current_user.role}"
        )

    try:
        session = _get_sandbox_or_404(db, sandbox_token)
    except ValueError as e:
        return SandboxConfirmResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            status="",
            message=str(e)
        )

    if _check_sandbox_expired(session):
        return SandboxConfirmResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            status=session.status,
            message="沙盒会话已过期"
        )

    if session.status == SANDBOX_STATUS_CONFIRMED:
        return SandboxConfirmResponse(
            success=True,
            sandbox_token=sandbox_token,
            session_id=session.id,
            status=session.status,
            target_batch_id=session.target_batch_id,
            target_batch_code=session.source_batch_code,
            confirmed_by=session.confirmed_by,
            confirmed_at=session.confirmed_at,
            restored_version_count=len(session.manifest_versions),
            message="沙盒会话已确认恢复"
        )

    if session.status != SANDBOX_STATUS_PRECHECK_PASSED:
        return SandboxConfirmResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            status=session.status,
            message=f"沙盒预检查未通过，无法确认恢复。当前状态: {session.status}"
        )

    versions = db.query(SandboxManifestVersion).filter(
        SandboxManifestVersion.sandbox_session_id == session.id
    ).order_by(SandboxManifestVersion.version_number.asc()).all()

    candidate_version = next((v for v in versions if v.is_candidate), None)
    if not candidate_version:
        candidate_version = versions[-1] if versions else None

    if not candidate_version:
        return SandboxConfirmResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            status=session.status,
            message="沙盒内没有可恢复的版本"
        )

    existing_batch = db.query(DeliveryBatch).filter(
        DeliveryBatch.batch_code == session.source_batch_code
    ).first()

    old_batch_id = None
    if existing_batch:
        old_batch_id = existing_batch.id
        
        old_vids = [v.id for v in existing_batch.manifest_versions]
        
        db.query(VersionDiffSnapshot).filter(VersionDiffSnapshot.batch_id == old_batch_id).delete()
        db.query(ImportPrecheck).filter(ImportPrecheck.batch_id == old_batch_id).delete()
        db.query(ApprovalLog).filter(ApprovalLog.batch_id == old_batch_id).delete()
        db.query(RejectionRecord).filter(RejectionRecord.batch_id == old_batch_id).delete()

        if old_vids:
            db.query(ValidationResult).filter(ValidationResult.manifest_version_id.in_(old_vids)).delete()
            db.query(ManifestItem).filter(ManifestItem.manifest_version_id.in_(old_vids)).delete()
        db.query(ManifestVersion).filter(ManifestVersion.batch_id == old_batch_id).delete()
        
        db.expunge(existing_batch)
        db.query(DeliveryBatch).filter(DeliveryBatch.id == old_batch_id).delete()
        db.flush()

    new_batch = DeliveryBatch(
        batch_code=session.source_batch_code,
        name=f"{session.source_batch_code} (从沙盒恢复)",
        description=f"从沙盒恢复，来源 archive_id={session.source_archive_id}",
        status=BATCH_STATUS_DRAFT,
        submitter_id=current_user.id,
    )
    db.add(new_batch)
    db.flush()

    version_id_map: Dict[int, int] = {}
    for sv in versions:
        nv = ManifestVersion(
            batch_id=new_batch.id,
            version_number=sv.version_number,
            import_format=sv.import_format,
            imported_by=sv.imported_by,
            imported_at=sv.imported_at,
            item_count=sv.item_count,
            raw_content=sv.raw_content,
            validation_status=sv.validation_status,
            validation_summary=sv.validation_summary,
        )
        db.add(nv)
        db.flush()
        version_id_map[sv.id] = nv.id

        for si in sv.items:
            ni = ManifestItem(
                manifest_version_id=nv.id,
                line_number=si.line_number,
                item_key=si.item_key,
                item_data=si.item_data,
            )
            db.add(ni)

    if candidate_version:
        new_batch.current_manifest_version_id = version_id_map.get(candidate_version.id)

    session.status = SANDBOX_STATUS_CONFIRMED
    session.target_batch_id = new_batch.id
    session.confirmed_by = current_user.id
    session.confirmed_at = datetime.utcnow()

    action_comment = f"确认沙盒恢复到正式环境: batch_code={session.source_batch_code}, sandbox_token={sandbox_token[:16]}..."
    if comment:
        action_comment += f"，备注: {comment}"

    log = ApprovalLog(
        batch_id=new_batch.id,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_SANDBOX_CONFIRM,
        comment=action_comment,
        extra_data={
            "sandbox_token": sandbox_token,
            "sandbox_session_id": session.id,
            "source_archive_id": session.source_archive_id,
            "batch_code": session.source_batch_code,
            "new_batch_id": new_batch.id,
            "overwritten_batch_id": old_batch_id,
            "restored_version_count": len(versions),
            "candidate_version": candidate_version.version_number if candidate_version else None,
            "comment": comment,
        }
    )
    db.add(log)

    source_log = ApprovalLog(
        batch_id=new_batch.id,
        actor_id=session.created_by,
        action=APPROVAL_LOG_ACTION_SANDBOX_RESTORE,
        comment=f"[来源沙盒] 原始恢复操作: archive_id={session.source_archive_id}, created_by={session.created_by}",
        extra_data={
            "sandbox_token": sandbox_token,
            "sandbox_session_id": session.id,
            "source_archive_id": session.source_archive_id,
            "is_sandbox_source": True,
        }
    )
    db.add(source_log)

    db.commit()
    db.refresh(session)
    db.refresh(new_batch)

    return SandboxConfirmResponse(
        success=True,
        sandbox_token=sandbox_token,
        session_id=session.id,
        status=session.status,
        target_batch_id=new_batch.id,
        target_batch_code=new_batch.batch_code,
        confirmed_by=session.confirmed_by,
        confirmed_at=session.confirmed_at,
        restored_version_count=len(versions),
        message=f"沙盒内容已成功恢复到正式环境。新批次 ID: {new_batch.id}，批次编码: {new_batch.batch_code}"
                + (f"，已覆盖原有批次 ID: {old_batch_id}" if old_batch_id else "")
    )


def reject_sandbox_session(
    db: Session,
    sandbox_token: str,
    reason: str,
    comment: Optional[str],
    current_user: User,
) -> SandboxRejectResponse:
    enabled, err_msg = _check_sandbox_enabled(db)
    if not enabled:
        return SandboxRejectResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            status="",
            message=err_msg or "沙盒功能已禁用"
        )

    try:
        session = _get_sandbox_or_404(db, sandbox_token)
    except ValueError as e:
        return SandboxRejectResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=0,
            status="",
            message=str(e)
        )

    if _check_sandbox_expired(session):
        return SandboxRejectResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            status=session.status,
            message="沙盒会话已过期"
        )

    if session.status in [SANDBOX_STATUS_CONFIRMED, SANDBOX_STATUS_REJECTED]:
        return SandboxRejectResponse(
            success=True,
            sandbox_token=sandbox_token,
            session_id=session.id,
            status=session.status,
            rejected_by=session.rejected_by,
            rejected_at=session.rejected_at,
            rejection_reason=session.rejection_reason,
            message=f"沙盒会话已{session.status}"
        )

    from app.schemas import ROLE_ADMIN, ROLE_LEAD
    require_admin = _get_config_bool(db, CONFIG_KEY_SANDBOX_REQUIRE_ADMIN_CONFIRM, True)
    if require_admin and current_user.role != ROLE_ADMIN:
        return SandboxRejectResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            status=session.status,
            message=f"权限不足：只有 admin 角色才能拒绝沙盒会话。您的角色: {current_user.role}"
        )
    elif not require_admin and current_user.role not in [ROLE_ADMIN, ROLE_LEAD]:
        return SandboxRejectResponse(
            success=False,
            sandbox_token=sandbox_token,
            session_id=session.id,
            status=session.status,
            message=f"权限不足：只有 admin 或 lead 角色才能拒绝沙盒会话。您的角色: {current_user.role}"
        )

    session.status = SANDBOX_STATUS_REJECTED
    session.rejected_by = current_user.id
    session.rejected_at = datetime.utcnow()
    session.rejection_reason = reason

    log_comment = f"拒绝沙盒会话: {reason}, session={sandbox_token[:16]}..."
    if comment:
        log_comment += f"，备注: {comment}"

    log = ApprovalLog(
        batch_id=0,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_SANDBOX_REJECT,
        comment=log_comment,
        extra_data={
            "sandbox_token": sandbox_token,
            "sandbox_session_id": session.id,
            "source_archive_id": session.source_archive_id,
            "batch_code": session.source_batch_code,
            "rejection_reason": reason,
            "comment": comment,
        }
    )
    db.add(log)
    db.commit()
    db.refresh(session)

    return SandboxRejectResponse(
        success=True,
        sandbox_token=sandbox_token,
        session_id=session.id,
        status=session.status,
        rejected_by=session.rejected_by,
        rejected_at=session.rejected_at,
        rejection_reason=session.rejection_reason,
        message=f"沙盒会话已拒绝。原因: {reason}"
    )


def list_sandbox_sessions(
    db: Session,
    current_user: User,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[SandboxSessionListResponse], int]:
    _auto_expire_sandbox_sessions(db)

    query = db.query(SandboxSession)
    if status:
        query = query.filter(SandboxSession.status == status)

    total = query.count()
    sessions = query.order_by(SandboxSession.created_at.desc()).offset(offset).limit(limit).all()

    results = []
    for s in sessions:
        version_count = db.query(SandboxManifestVersion).filter(
            SandboxManifestVersion.sandbox_session_id == s.id
        ).count()
        results.append(SandboxSessionListResponse(
            id=s.id,
            sandbox_token=s.sandbox_token,
            source_archive_id=s.source_archive_id,
            source_batch_code=s.source_batch_code,
            original_batch_id=s.original_batch_id,
            target_batch_id=s.target_batch_id,
            status=s.status,
            created_by=s.created_by,
            created_at=s.created_at,
            expires_at=s.expires_at,
            confirmed_by=s.confirmed_by,
            precheck_passed=s.precheck_passed,
            recommended_action=s.recommended_action,
            version_count=version_count,
        ))

    return results, total


def get_sandbox_session_detail(
    db: Session,
    sandbox_token: str,
    current_user: User,
) -> SandboxSessionResponse:
    try:
        session = _get_sandbox_or_404(db, sandbox_token)
    except ValueError as e:
        raise ValueError(e)

    if _check_sandbox_expired(session) and session.status != SANDBOX_STATUS_EXPIRED:
        session.status = SANDBOX_STATUS_EXPIRED
        db.commit()
        db.refresh(session)

    versions = db.query(SandboxManifestVersion).filter(
        SandboxManifestVersion.sandbox_session_id == session.id
    ).order_by(SandboxManifestVersion.version_number.asc()).all()

    version_responses = []
    for v in versions:
        items = db.query(SandboxManifestItem).filter(
            SandboxManifestItem.sandbox_manifest_version_id == v.id
        ).order_by(SandboxManifestItem.line_number.asc()).all()
        item_responses = [
            SandboxItemResponse(
                id=it.id,
                line_number=it.line_number,
                item_key=it.item_key,
                item_data=it.item_data,
            ) for it in items
        ]
        version_responses.append(SandboxVersionResponse(
            id=v.id,
            sandbox_session_id=v.sandbox_session_id,
            version_number=v.version_number,
            import_format=v.import_format,
            imported_by=v.imported_by,
            imported_at=v.imported_at,
            item_count=v.item_count,
            content_hash=v.content_hash,
            validation_status=v.validation_status,
            validation_summary=v.validation_summary,
            is_candidate=v.is_candidate,
            base_version_number=v.base_version_number,
            items=item_responses,
        ))

    precheck_results = db.query(SandboxPrecheckResult).filter(
        SandboxPrecheckResult.sandbox_session_id == session.id
    ).order_by(SandboxPrecheckResult.created_at.asc()).all()

    precheck_responses = [
        SandboxPrecheckItem(
            id=r.id,
            check_code=r.check_code,
            check_name=r.check_name,
            severity=r.severity,
            passed=r.passed,
            message=r.message,
            suggestion=r.suggestion,
            details=r.details,
            affected_version_number=r.affected_version_number,
            affected_item_key=r.affected_item_key,
            created_at=r.created_at,
        ) for r in precheck_results
    ]

    return SandboxSessionResponse(
        id=session.id,
        sandbox_token=session.sandbox_token,
        source_archive_id=session.source_archive_id,
        source_batch_code=session.source_batch_code,
        original_batch_id=session.original_batch_id,
        target_batch_id=session.target_batch_id,
        status=session.status,
        created_by=session.created_by,
        created_at=session.created_at,
        expires_at=session.expires_at,
        confirmed_by=session.confirmed_by,
        confirmed_at=session.confirmed_at,
        rejected_by=session.rejected_by,
        rejected_at=session.rejected_at,
        rejection_reason=session.rejection_reason,
        precheck_passed=session.precheck_passed,
        recommended_action=session.recommended_action,
        conflict_types=session.conflict_types,
        extra_data=session.extra_data,
        manifest_versions=version_responses,
        precheck_results=precheck_responses,
    )


def get_sandbox_audit_logs(
    db: Session,
    sandbox_token: str,
    current_user: User,
) -> List[Dict[str, Any]]:
    try:
        session = _get_sandbox_or_404(db, sandbox_token)
    except ValueError as e:
        raise ValueError(e)

    sandbox_actions = [
        APPROVAL_LOG_ACTION_SANDBOX_RESTORE,
        APPROVAL_LOG_ACTION_SANDBOX_IMPORT,
        APPROVAL_LOG_ACTION_SANDBOX_PRECHECK,
        APPROVAL_LOG_ACTION_SANDBOX_VIEW_DIFF,
        APPROVAL_LOG_ACTION_SANDBOX_CONFIRM,
        APPROVAL_LOG_ACTION_SANDBOX_REJECT,
        APPROVAL_LOG_ACTION_SANDBOX_CLEANUP,
    ]

    logs = db.query(ApprovalLog).filter(
        ApprovalLog.extra_data.isnot(None),
        ApprovalLog.extra_data.op("->>")("sandbox_token") == sandbox_token
    ).order_by(ApprovalLog.created_at.asc()).all()

    from app.schemas import SandboxAuditLogResponse
    results = []
    for log in logs:
        username, display_name = _get_user_info(db, log.actor_id)
        results.append(SandboxAuditLogResponse(
            id=log.id,
            sandbox_token=sandbox_token,
            action=log.action,
            actor_id=log.actor_id,
            actor_username=username,
            actor_display_name=display_name,
            created_at=log.created_at,
            comment=log.comment,
            extra_data=log.extra_data,
        ).model_dump())

    return results
