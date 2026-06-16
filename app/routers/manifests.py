from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional, Tuple
import csv
import json
import io
import hashlib
import secrets
from datetime import datetime, timedelta, timezone as _tz

from app.database import get_db
from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    ApprovalLog, RejectionRecord, ImportPrecheck,
    PRECHECK_ACTION_NEW_VERSION, PRECHECK_ACTION_REUSE_VERSION,
    PRECHECK_ACTION_CONFLICT, PRECHECK_CONFLICT_STATUS,
    PRECHECK_CONFLICT_UNRESOLVED_REJECTIONS, PRECHECK_TOKEN_TTL_SECONDS,
)
from app.schemas import (
    ManifestVersionResponse, ImportResponse, ImportValidationError,
    BATCH_STATUS_DRAFT, BATCH_STATUS_REPAIRING, BATCH_STATUS_PARTIALLY_REJECTED,
    BATCH_STATUS_PENDING, BATCH_STATUS_APPROVED, BATCH_STATUS_ARCHIVED,
    ROLE_SUBMITTER,
    ImportPrecheckResponse, PrecheckConflictDetail,
    ImportPrecheckQueryResponse,
)
from app.dependencies import (
    get_current_user, require_submitter_or_admin, get_batch_or_404
)

router = APIRouter(prefix="/api/batches", tags=["清单管理"])

REQUIRED_MANIFEST_FIELDS = ["item_id", "item_name", "quantity", "unit_price"]
ALLOWED_IMPORT_STATUSES = [BATCH_STATUS_DRAFT, BATCH_STATUS_REPAIRING, BATCH_STATUS_PARTIALLY_REJECTED]


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


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _check_status_conflict(batch: DeliveryBatch) -> Optional[PrecheckConflictDetail]:
    if batch.status in ALLOWED_IMPORT_STATUSES:
        return None
    label_map = {
        BATCH_STATUS_PENDING: "待验收（审批中）",
        BATCH_STATUS_APPROVED: "已通过验收",
        BATCH_STATUS_ARCHIVED: "已归档",
    }
    label = label_map.get(batch.status, batch.status)
    return PrecheckConflictDetail(
        conflict_type=PRECHECK_CONFLICT_STATUS,
        severity="error",
        title=f"批次状态不允许导入：{label}",
        description=f"当前批次状态为 '{batch.status}'（{label}），不允许导入新清单。"
                    f" 允许导入的状态为：草稿(draft)、返修中(repairing)、部分驳回(partially_rejected)。",
        suggestion="请等待审批流程结束或联系评审人员将状态流转回可编辑状态后再操作。",
        meta={"current_status": batch.status, "allowed_statuses": ALLOWED_IMPORT_STATUSES},
    )


def _check_unresolved_rejections(db: Session, batch_id: int) -> Optional[PrecheckConflictDetail]:
    unresolved = db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id,
        RejectionRecord.resolved == False
    ).count()
    if unresolved == 0:
        return None
    return PrecheckConflictDetail(
        conflict_type=PRECHECK_CONFLICT_UNRESOLVED_REJECTIONS,
        severity="warning",
        title=f"存在 {unresolved} 条未解决的驳回记录",
        description=f"该批次有 {unresolved} 条驳回记录尚未标记为已解决。"
                    f" 导入新清单后这些驳回会自动标记为已解决。",
        suggestion="请确认新清单中已针对这些驳回项作出修订。"
                   f" 可以通过 GET /api/batches/{batch_id}/rejections?only_unresolved=true 查看详情。",
        meta={"unresolved_count": unresolved},
    )


def _find_duplicate_version(db: Session, batch_id: int, raw_content: str, detected_format: str) -> Optional[ManifestVersion]:
    return db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id,
        ManifestVersion.raw_content == raw_content,
        ManifestVersion.import_format == detected_format
    ).first()


def _build_reasons(action_type: str, has_conflict: bool,
                   status_conflict: Optional[PrecheckConflictDetail],
                   rej_conflict: Optional[PrecheckConflictDetail],
                   duplicate: Optional[ManifestVersion]) -> List[str]:
    reasons = []
    if duplicate:
        reasons.append(f"清单内容与已存在的 v{duplicate.version_number} 完全一致，将复用现有版本（不产生新版本）。")
    else:
        reasons.append("清单内容与历史版本存在差异，将创建新版本。")

    if status_conflict:
        reasons.append(f"状态冲突：{status_conflict.title}")
    if rej_conflict:
        reasons.append(f"驳回提醒：{rej_conflict.title}")

    if not has_conflict and action_type == PRECHECK_ACTION_NEW_VERSION:
        reasons.append("未检测到阻塞性冲突，可以执行导入。")
    elif not has_conflict and action_type == PRECHECK_ACTION_REUSE_VERSION:
        reasons.append("未检测到阻塞性冲突，将复用历史版本。")
    return reasons


def _build_message(action_type: str, has_conflict: bool,
                   planned_version_number: Optional[int],
                   duplicate: Optional[ManifestVersion],
                   conflicts: List[PrecheckConflictDetail]) -> str:
    blocking = [c for c in conflicts if c.severity == "error"]
    warnings = [c for c in conflicts if c.severity == "warning"]

    if blocking:
        return f"预检查未通过：存在 {len(blocking)} 项阻塞性冲突，暂不能导入。"
    if action_type == PRECHECK_ACTION_REUSE_VERSION:
        msg = f"预检查通过：清单内容无变更，将复用现有版本 v{duplicate.version_number}。"
    else:
        msg = f"预检查通过：将创建新版本 v{planned_version_number}。"
    if warnings:
        msg += f" 注意：存在 {len(warnings)} 项提醒，请确认后继续。"
    return msg


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _run_precheck_core(
    db: Session,
    batch: DeliveryBatch,
    current_user: User,
    raw_content: str,
    detected_format: str,
    items: Optional[List[dict]],
    parse_errors: List[ImportValidationError],
) -> Tuple[ImportPrecheck, List[PrecheckConflictDetail], List[str], str]:
    content_hash = _sha256_hex(raw_content)
    item_count = len(items) if items else 0

    conflicts: List[PrecheckConflictDetail] = []

    status_conflict = _check_status_conflict(batch)
    if status_conflict:
        conflicts.append(status_conflict)

    rej_conflict = _check_unresolved_rejections(db, batch.id)
    if rej_conflict:
        conflicts.append(rej_conflict)

    duplicate = _find_duplicate_version(db, batch.id, raw_content, detected_format)

    critical_errors = [e for e in parse_errors if e.field_name in REQUIRED_MANIFEST_FIELDS or e.field_name is None]
    parse_ok = items is not None and len(critical_errors) == 0

    blocking_conflicts = [c for c in conflicts if c.severity == "error"]
    has_conflict = len(conflicts) > 0 or not parse_ok

    if duplicate and parse_ok and not blocking_conflicts:
        action_type = PRECHECK_ACTION_REUSE_VERSION
    elif blocking_conflicts or not parse_ok:
        action_type = PRECHECK_ACTION_CONFLICT
    else:
        action_type = PRECHECK_ACTION_NEW_VERSION

    if duplicate:
        reused_version_id = duplicate.id
        reused_version_number = duplicate.version_number
        planned_version_number = None
    else:
        reused_version_id = None
        reused_version_number = None
        existing_count = db.query(ManifestVersion).filter(ManifestVersion.batch_id == batch.id).count()
        planned_version_number = existing_count + 1

    can_import = parse_ok and len(blocking_conflicts) == 0

    reasons = _build_reasons(action_type, has_conflict, status_conflict, rej_conflict, duplicate)
    if not parse_ok:
        if items is None:
            reasons.insert(0, "清单解析失败（表头错误或格式错误），无法执行导入。")
        else:
            reasons.insert(0, f"存在 {len(critical_errors)} 个关键字段错误，暂不能写入。")

    message = _build_message(action_type, has_conflict, planned_version_number, duplicate, conflicts)
    if not parse_ok:
        message = f"预检查失败：清单解析/校验不通过。{message}"

    token = _generate_token()
    expires_at = _utcnow() + timedelta(seconds=PRECHECK_TOKEN_TTL_SECONDS)

    precheck = ImportPrecheck(
        batch_id=batch.id,
        actor_id=current_user.id,
        precheck_token=token,
        content_hash=content_hash,
        import_format=detected_format,
        item_count=item_count,
        action_type=action_type,
        has_conflict=has_conflict,
        conflict_types=[c.conflict_type for c in conflicts],
        conflict_details=[c.model_dump() for c in conflicts],
        reused_version_id=reused_version_id,
        reused_version_number=reused_version_number,
        planned_version_number=planned_version_number,
        expires_at=expires_at,
        consumed=False,
        extra_data={
            "parse_errors": [e.model_dump() for e in parse_errors],
            "parse_ok": parse_ok,
            "critical_error_count": len(critical_errors),
        }
    )
    db.add(precheck)
    db.flush()

    log_comment = (
        f"导入预检查[{action_type}]："
        + (f"复用 v{reused_version_number}" if duplicate else f"计划 v{planned_version_number}")
        + f"，条目 {item_count}，冲突 {len(conflicts)}，{'可导入' if can_import else '不可导入'}"
    )
    log = ApprovalLog(
        batch_id=batch.id,
        manifest_version_id=reused_version_id,
        actor_id=current_user.id,
        action="PRECHECK_IMPORT",
        from_status=batch.status,
        to_status=batch.status,
        comment=log_comment,
        extra_data={
            "precheck_token": token,
            "action_type": action_type,
            "has_conflict": has_conflict,
            "conflict_types": [c.conflict_type for c in conflicts],
            "can_import": can_import,
            "item_count": item_count,
            "content_hash": content_hash,
            "expires_at": expires_at.isoformat(),
        }
    )
    db.add(log)
    db.commit()
    db.refresh(precheck)

    return precheck, conflicts, reasons, message


def _enforce_submitter_permission(batch: DeliveryBatch, current_user: User):
    if current_user.role == ROLE_SUBMITTER and batch.submitter_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only perform this action for batches you submitted"
        )


def _validate_precheck_token_for_import(
    db: Session,
    batch: DeliveryBatch,
    current_user: User,
    raw_content: str,
    precheck_token: Optional[str],
):
    if not precheck_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少 precheck_token。请先调用 POST /api/batches/{batch_id}/manifests/precheck 执行导入预检查，"
                   "确认检查结论后再携带 precheck_token 调用本接口执行正式导入。"
        )

    precheck = db.query(ImportPrecheck).filter(
        ImportPrecheck.precheck_token == precheck_token
    ).first()
    if not precheck:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="precheck_token 无效或不存在，请重新执行预检查。"
        )

    if precheck.batch_id != batch.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"precheck_token 所属批次 (id={precheck.batch_id}) 与当前批次 (id={batch.id}) 不匹配。"
        )

    if precheck.actor_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该 precheck_token 由其他用户生成，仅生成者本人可使用。"
        )

    now = _utcnow()
    exp = precheck.expires_at
    if exp and exp.tzinfo is not None:
        exp = exp.replace(tzinfo=None)
    if exp < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"precheck_token 已过期（过期时间 {precheck.expires_at.isoformat()}）。请重新执行预检查。"
        )

    if precheck.consumed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该 precheck_token 已被使用过（用于一次导入或确认）。请重新执行预检查获取新 token。"
        )

    incoming_hash = _sha256_hex(raw_content)
    if incoming_hash != precheck.content_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="预检查时的清单内容与本次提交的清单内容不一致（哈希校验失败）。"
                   " 请使用相同文件重新执行预检查，或确认文件内容未被修改。"
        )

    if precheck.action_type == PRECHECK_ACTION_CONFLICT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该预检查结论存在阻塞性冲突，不允许执行导入。请先解决冲突后重新执行预检查。"
        )

    return precheck


def _do_import_write(
    db: Session,
    batch: DeliveryBatch,
    current_user: User,
    raw_content: str,
    detected_format: str,
    items: List[dict],
    duplicate: Optional[ManifestVersion],
) -> Tuple[ManifestVersion, bool, int, int]:
    """
    returns: (manifest_version, is_new_version, new_version_number, item_count)
    """
    if duplicate:
        batch.current_manifest_version_id = duplicate.id
        db.commit()
        return duplicate, False, duplicate.version_number, duplicate.item_count

    old_version_id = batch.current_manifest_version_id

    existing_versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch.id
    ).count()
    new_version_number = existing_versions + 1

    sp = db.begin_nested()
    try:
        manifest_version = ManifestVersion(
            batch_id=batch.id,
            version_number=new_version_number,
            import_format=detected_format,
            imported_by=current_user.id,
            item_count=len(items),
            raw_content=raw_content,
            validation_status="pending",
        )
        db.add(manifest_version)
        sp.commit()
    except Exception as e:
        sp.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建清单版本失败: {str(e)}"
        )

    try:
        manifest_items = []
        for item in items:
            mi = ManifestItem(
                manifest_version_id=manifest_version.id,
                line_number=item["line_number"],
                item_key=item["item_key"],
                item_data=item["item_data"],
            )
            manifest_items.append(mi)
        db.bulk_save_objects(manifest_items)
    except Exception as e:
        db.rollback()
        batch.current_manifest_version_id = old_version_id
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"保存清单条目失败，已回滚: {str(e)}"
        )

    batch.current_manifest_version_id = manifest_version.id
    batch.status = (
        BATCH_STATUS_DRAFT
        if batch.status in [BATCH_STATUS_REPAIRING, BATCH_STATUS_PARTIALLY_REJECTED]
        else batch.status
    )

    if old_version_id:
        old_rejections = db.query(RejectionRecord).filter(
            RejectionRecord.batch_id == batch.id,
            RejectionRecord.manifest_version_id == old_version_id,
            RejectionRecord.resolved == False
        ).all()
        for rej in old_rejections:
            rej.resolved = True
            rej.resolved_at = _utcnow().replace(tzinfo=None)
            rej.resolved_by_manifest_version_id = manifest_version.id

    return manifest_version, True, new_version_number, len(items)


@router.post("/{batch_id}/manifests/precheck", response_model=ImportPrecheckResponse)
async def precheck_manifest_import(
    batch_id: int,
    file: UploadFile = File(...),
    import_format: str = Form("auto"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_submitter_or_admin),
):
    batch = get_batch_or_404(db, batch_id)
    _enforce_submitter_permission(batch, current_user)

    content_bytes = await file.read()
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = content_bytes.decode("gbk", errors="replace")
    raw_content = content

    detected_format = _detect_format(file.filename or "", content, import_format)
    if detected_format not in ("csv", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format: {import_format}. Use 'csv', 'json', or 'auto'."
        )

    try:
        items, parse_errors = _parse_content(content, detected_format)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    precheck, conflicts, reasons, message = _run_precheck_core(
        db=db,
        batch=batch,
        current_user=current_user,
        raw_content=raw_content,
        detected_format=detected_format,
        items=items,
        parse_errors=parse_errors,
    )

    return ImportPrecheckResponse(
        success=True,
        precheck_token=precheck.precheck_token,
        batch_id=batch.id,
        action_type=precheck.action_type,
        has_conflict=precheck.has_conflict,
        import_format=detected_format,
        item_count=precheck.item_count,
        content_hash=precheck.content_hash,
        planned_version_number=precheck.planned_version_number,
        reused_version_id=precheck.reused_version_id,
        reused_version_number=precheck.reused_version_number,
        conflicts=conflicts,
        batch_status=batch.status,
        can_import=(precheck.action_type != PRECHECK_ACTION_CONFLICT),
        expires_at=precheck.expires_at,
        reasons=reasons,
        message=message,
        parse_errors=parse_errors,
    )


@router.get("/{batch_id}/manifests/prechecks/latest", response_model=ImportPrecheckQueryResponse)
def get_latest_precheck(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    batch = get_batch_or_404(db, batch_id)
    precheck = db.query(ImportPrecheck).filter(
        ImportPrecheck.batch_id == batch_id
    ).order_by(ImportPrecheck.created_at.desc(), ImportPrecheck.id.desc()).first()

    if not precheck:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该批次尚无导入预检查记录。请先调用 POST /api/batches/{batch_id}/manifests/precheck 执行预检查。"
        )

    details = precheck.conflict_details or []
    blocking_count = sum(1 for d in details if d.get("severity") == "error")
    can_import = precheck.action_type != PRECHECK_ACTION_CONFLICT and blocking_count == 0

    reasons = []
    if precheck.reused_version_id:
        reasons.append(f"清单内容与 v{precheck.reused_version_number} 一致，将复用现有版本。")
    elif precheck.planned_version_number:
        reasons.append(f"将创建新版本 v{precheck.planned_version_number}。")

    for d in details:
        reasons.append(f"{d.get('severity', 'info').upper()}: {d.get('title', '')}")

    if precheck.consumed:
        reasons.append("该预检查 token 已被消费，如需再次导入请重新执行预检查。")

    return ImportPrecheckQueryResponse(
        id=precheck.id,
        batch_id=precheck.batch_id,
        actor_id=precheck.actor_id,
        precheck_token=precheck.precheck_token,
        content_hash=precheck.content_hash,
        import_format=precheck.import_format,
        item_count=precheck.item_count,
        action_type=precheck.action_type,
        has_conflict=precheck.has_conflict,
        conflict_types=precheck.conflict_types,
        conflict_details=details,
        reused_version_id=precheck.reused_version_id,
        reused_version_number=precheck.reused_version_number,
        planned_version_number=precheck.planned_version_number,
        created_at=precheck.created_at,
        expires_at=precheck.expires_at,
        consumed=precheck.consumed,
        consumed_at=precheck.consumed_at,
        can_import=can_import,
        reasons=reasons,
        batch_status=batch.status,
    )


@router.get("/{batch_id}/manifests/prechecks", response_model=List[ImportPrecheckQueryResponse])
def list_prechecks(
    batch_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    batch = get_batch_or_404(db, batch_id)
    prechecks = db.query(ImportPrecheck).filter(
        ImportPrecheck.batch_id == batch_id
    ).order_by(ImportPrecheck.created_at.desc(), ImportPrecheck.id.desc()).limit(max(1, min(limit, 100))).all()

    results = []
    for precheck in prechecks:
        details = precheck.conflict_details or []
        blocking_count = sum(1 for d in details if d.get("severity") == "error")
        can_import = precheck.action_type != PRECHECK_ACTION_CONFLICT and blocking_count == 0

        reasons = []
        if precheck.reused_version_id:
            reasons.append(f"复用 v{precheck.reused_version_number}")
        elif precheck.planned_version_number:
            reasons.append(f"计划 v{precheck.planned_version_number}")
        for d in details:
            reasons.append(f"{d.get('severity', 'info').upper()}: {d.get('title', '')}")
        if precheck.consumed:
            reasons.append("已消费")

        results.append(ImportPrecheckQueryResponse(
            id=precheck.id,
            batch_id=precheck.batch_id,
            actor_id=precheck.actor_id,
            precheck_token=precheck.precheck_token,
            content_hash=precheck.content_hash,
            import_format=precheck.import_format,
            item_count=precheck.item_count,
            action_type=precheck.action_type,
            has_conflict=precheck.has_conflict,
            conflict_types=precheck.conflict_types,
            conflict_details=details,
            reused_version_id=precheck.reused_version_id,
            reused_version_number=precheck.reused_version_number,
            planned_version_number=precheck.planned_version_number,
            created_at=precheck.created_at,
            expires_at=precheck.expires_at,
            consumed=precheck.consumed,
            consumed_at=precheck.consumed_at,
            can_import=can_import,
            reasons=reasons,
            batch_status=batch.status,
        ))
    return results


@router.post("/{batch_id}/manifests/import", response_model=ImportResponse)
async def import_manifest(
    batch_id: int,
    file: UploadFile = File(...),
    import_format: str = Form("auto"),
    precheck_token: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_submitter_or_admin),
):
    batch = get_batch_or_404(db, batch_id)
    _enforce_submitter_permission(batch, current_user)

    content_bytes = await file.read()
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = content_bytes.decode("gbk", errors="replace")
    raw_content = content

    precheck = _validate_precheck_token_for_import(
        db=db,
        batch=batch,
        current_user=current_user,
        raw_content=raw_content,
        precheck_token=precheck_token,
    )

    detected_format = _detect_format(file.filename or "", content, import_format)
    if detected_format not in ("csv", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format: {import_format}. Use 'csv', 'json', or 'auto'."
        )

    try:
        items, parse_errors = _parse_content(content, detected_format)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if items is None:
        precheck.consumed = True
        precheck.consumed_at = _utcnow().replace(tzinfo=None)
        db.commit()
        return ImportResponse(
            success=False,
            errors=parse_errors,
            message="清单解析失败，未创建新版本，旧清单保持不变"
        )

    critical_errors = [e for e in parse_errors if e.field_name in REQUIRED_MANIFEST_FIELDS or e.field_name is None]
    if critical_errors:
        precheck.consumed = True
        precheck.consumed_at = _utcnow().replace(tzinfo=None)
        db.commit()
        return ImportResponse(
            success=False,
            errors=parse_errors,
            message=f"发现 {len(critical_errors)} 个字段错误，未创建新版本，旧清单保持不变。请修复后重新导入。"
        )

    duplicate = None
    if precheck.action_type == PRECHECK_ACTION_REUSE_VERSION and precheck.reused_version_id:
        duplicate = db.query(ManifestVersion).filter(
            ManifestVersion.id == precheck.reused_version_id
        ).first()

    if duplicate is None and precheck.action_type == PRECHECK_ACTION_REUSE_VERSION:
        duplicate = _find_duplicate_version(db, batch.id, raw_content, detected_format)

    if duplicate is None and precheck.action_type == PRECHECK_ACTION_NEW_VERSION:
        duplicate = _find_duplicate_version(db, batch.id, raw_content, detected_format)
        if duplicate:
            log = ApprovalLog(
                batch_id=batch.id,
                manifest_version_id=duplicate.id,
                actor_id=current_user.id,
                action="PRECHECK_IMPORT_DRIFT",
                from_status=batch.status,
                to_status=batch.status,
                comment=f"预检查标记为 NEW_VERSION，但实际写入时发现内容与 v{duplicate.version_number} 一致，已自动降级为复用。",
                extra_data={
                    "planned_version_number": precheck.planned_version_number,
                    "actual_reused_version": duplicate.version_number,
                    "precheck_token": precheck.precheck_token,
                }
            )
            db.add(log)

    manifest_version, is_new, version_number, item_count = _do_import_write(
        db=db,
        batch=batch,
        current_user=current_user,
        raw_content=raw_content,
        detected_format=detected_format,
        items=items,
        duplicate=duplicate,
    )

    precheck.consumed = True
    precheck.consumed_at = _utcnow().replace(tzinfo=None)

    if is_new:
        log_comment = f"导入清单 v{version_number} ({detected_format.upper()}), 共 {item_count} 条记录"
        extra = {
            "version_number": version_number,
            "import_format": detected_format,
            "item_count": item_count,
            "precheck_token": precheck.precheck_token,
            "warnings": [e.model_dump() for e in parse_errors if e not in critical_errors]
        }
    else:
        log_comment = f"清单内容无变更，复用现有版本 v{version_number} ({detected_format.upper()})"
        extra = {
            "version_number": version_number,
            "import_format": detected_format,
            "item_count": item_count,
            "precheck_token": precheck.precheck_token,
            "reused": True,
        }

    log = ApprovalLog(
        batch_id=batch.id,
        manifest_version_id=manifest_version.id,
        actor_id=current_user.id,
        action="IMPORT_MANIFEST",
        from_status=batch.status,
        to_status=batch.status,
        comment=log_comment,
        extra_data=extra,
    )
    db.add(log)
    db.commit()

    if is_new:
        msg = (
            f"清单 v{version_number} 导入成功，共 {item_count} 条记录。"
            + (f" {len(parse_errors)} 条非阻塞性警告。" if parse_errors else "")
        )
    else:
        msg = f"内容无变更，复用现有版本 v{version_number}。"

    return ImportResponse(
        success=True,
        manifest_version_id=manifest_version.id,
        version_number=version_number,
        item_count=item_count,
        errors=parse_errors,
        message=msg,
    )


@router.get("/{batch_id}/manifests", response_model=List[ManifestVersionResponse])
def list_manifest_versions(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id
    ).order_by(ManifestVersion.version_number.asc()).all()
    return versions


@router.get("/{batch_id}/manifests/latest", response_model=ManifestVersionResponse)
def get_latest_manifest(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = get_batch_or_404(db, batch_id)
    if not batch.current_manifest_version_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No manifest imported for this batch yet"
        )
    version = db.query(ManifestVersion).filter(
        ManifestVersion.id == batch.current_manifest_version_id
    ).first()
    return version


@router.get("/{batch_id}/manifests/{version_id}", response_model=ManifestVersionResponse)
def get_manifest_version(
    batch_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    version = db.query(ManifestVersion).filter(
        ManifestVersion.id == version_id,
        ManifestVersion.batch_id == batch_id
    ).first()
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manifest version {version_id} not found for batch {batch_id}"
        )
    return version
