from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import json

from app.database import get_db
from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    RejectionRecord, ApprovalLog, ValidationResult
)
from app.schemas import (
    AcceptanceReportResponse, ApprovalLogResponse,
    BATCH_STATUS_APPROVED, BATCH_STATUS_ARCHIVED,
    ManifestVersionResponse,
    VersionDiffResponse, VersionDiffExportResponse,
    VersionDiffMetadata, VersionDiffSummary,
    ItemDiff, ItemDiffSummary, FieldChange,
    RejectionInfo, ValidationChange, ImportInfo,
    DIFF_ACTION_ADDED, DIFF_ACTION_REMOVED,
    DIFF_ACTION_MODIFIED, DIFF_ACTION_UNCHANGED,
    APPROVAL_LOG_ACTION_VIEW_DIFF, APPROVAL_LOG_ACTION_EXPORT_DIFF
)
from app.dependencies import get_current_user, get_batch_or_404, require_lead, require_version_diff_access

router = APIRouter(prefix="/api", tags=["报告与历史查询"])


@router.get("/batches/{batch_id}/version-history", response_model=List[ManifestVersionResponse])
def get_version_history(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id
    ).order_by(ManifestVersion.version_number.asc()).all()
    return versions


@router.get("/batches/{batch_id}/approval-logs", response_model=List[ApprovalLogResponse])
def get_approval_logs(
    batch_id: int,
    action: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    query = db.query(ApprovalLog).filter(ApprovalLog.batch_id == batch_id)
    if action:
        query = query.filter(ApprovalLog.action == action)
    return query.order_by(ApprovalLog.created_at.asc()).all()


@router.get("/batches/{batch_id}/rejection-history")
def get_rejection_history(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    rejections = db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id
    ).order_by(RejectionRecord.created_at.asc()).all()

    result = []
    for r in rejections:
        rejector = db.query(User).filter(User.id == r.rejector_id).first()
        resolved_version = None
        if r.resolved_by_manifest_version_id:
            v = db.query(ManifestVersion).filter(
                ManifestVersion.id == r.resolved_by_manifest_version_id
            ).first()
            if v:
                resolved_version = f"v{v.version_number}"

        result.append({
            "id": r.id,
            "item_key": r.item_key,
            "line_number": r.line_number,
            "rejection_reason": r.rejection_reason,
            "rejector": {
                "id": rejector.id if rejector else None,
                "username": rejector.username if rejector else None,
                "display_name": rejector.display_name if rejector else None
            },
            "rejected_at": r.created_at.isoformat() if r.created_at else None,
            "manifest_version": f"v{db.query(ManifestVersion).filter(ManifestVersion.id == r.manifest_version_id).first().version_number}" if db.query(ManifestVersion).filter(ManifestVersion.id == r.manifest_version_id).first() else None,
            "resolved": r.resolved,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "resolved_by_version": resolved_version
        })

    return JSONResponse(content={
        "batch_id": batch_id,
        "total_rejections": len(rejections),
        "resolved_count": len([r for r in rejections if r.resolved]),
        "unresolved_count": len([r for r in rejections if not r.resolved]),
        "rejections": result
    })


@router.get("/batches/{batch_id}/acceptance-report", response_model=AcceptanceReportResponse)
def get_acceptance_report(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = get_batch_or_404(db, batch_id)

    versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id
    ).order_by(ManifestVersion.version_number.asc()).all()

    total_versions = len(versions)
    current_version = versions[-1].version_number if versions else 0
    current_manifest = versions[-1] if versions else None
    item_count = current_manifest.item_count if current_manifest else 0

    rejections = db.query(RejectionRecord).filter(RejectionRecord.batch_id == batch_id).all()
    total_rejections = len(rejections)
    resolved_rejections = len([r for r in rejections if r.resolved])

    validation_summary = {}
    validation_passed = False
    if current_manifest and current_manifest.validation_summary:
        validation_summary = current_manifest.validation_summary
        validation_passed = current_manifest.validation_status == "passed"

    approval_logs = db.query(ApprovalLog).filter(
        ApprovalLog.batch_id == batch_id
    ).order_by(ApprovalLog.created_at.asc()).all()

    approved_at = None
    approved_by = None
    for log in approval_logs:
        if log.action == "APPROVE":
            approved_at = log.created_at
            approved_by = log.actor_id
            break

    return AcceptanceReportResponse(
        batch_id=batch.id,
        batch_code=batch.batch_code,
        batch_name=batch.name,
        status=batch.status,
        submitter_id=batch.submitter_id,
        created_at=batch.created_at,
        approved_at=approved_at,
        approved_by=approved_by,
        total_versions=total_versions,
        current_version=current_version,
        item_count=item_count,
        total_rejections=total_rejections,
        resolved_rejections=resolved_rejections,
        validation_passed=validation_passed,
        validation_summary=validation_summary,
        approval_logs=approval_logs,
        rejection_history=rejections,
        generated_at=datetime.now()
    )


@router.get("/batches/{batch_id}/export-report")
def export_acceptance_report(
    batch_id: int,
    format: str = "json",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = get_batch_or_404(db, batch_id)

    versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id
    ).order_by(ManifestVersion.version_number.asc()).all()

    submitter = db.query(User).filter(User.id == batch.submitter_id).first()

    report = get_acceptance_report(batch_id, db, current_user)

    detailed_versions = []
    for v in versions:
        importer = db.query(User).filter(User.id == v.imported_by).first()
        items = db.query(ManifestItem).filter(ManifestItem.manifest_version_id == v.id).all()
        rejections = db.query(RejectionRecord).filter(
            RejectionRecord.manifest_version_id == v.id
        ).all()
        validation_results = db.query(ValidationResult).filter(
            ValidationResult.manifest_version_id == v.id,
            ValidationResult.passed == False
        ).all()

        detailed_versions.append({
            "version": v.version_number,
            "import_format": v.import_format,
            "imported_by": {
                "id": importer.id if importer else None,
                "username": importer.username if importer else None,
                "display_name": importer.display_name if importer else None
            },
            "imported_at": v.imported_at.isoformat() if v.imported_at else None,
            "item_count": v.item_count,
            "validation_status": v.validation_status,
            "validation_summary": v.validation_summary,
            "items": [
                {
                    "line_number": item.line_number,
                    "item_key": item.item_key,
                    "data": item.item_data
                }
                for item in items
            ],
            "rejections_in_this_version": [
                {
                    "item_key": r.item_key,
                    "line_number": r.line_number,
                    "reason": r.rejection_reason,
                    "resolved": r.resolved
                }
                for r in rejections
            ],
            "validation_errors": [
                {
                    "item_key": vr.item_key,
                    "line_number": vr.line_number,
                    "field": vr.field_name,
                    "rule": vr.rule_code,
                    "severity": vr.severity,
                    "message": vr.message
                }
                for vr in validation_results
            ]
        })

    approval_logs = db.query(ApprovalLog).filter(
        ApprovalLog.batch_id == batch_id
    ).order_by(ApprovalLog.created_at.asc()).all()

    detailed_logs = []
    for log in approval_logs:
        actor = db.query(User).filter(User.id == log.actor_id).first()
        detailed_logs.append({
            "timestamp": log.created_at.isoformat() if log.created_at else None,
            "actor": {
                "id": actor.id if actor else None,
                "username": actor.username if actor else None,
                "role": actor.role if actor else None,
                "display_name": actor.display_name if actor else None
            },
            "action": log.action,
            "from_status": log.from_status,
            "to_status": log.to_status,
            "comment": log.comment,
            "extra_data": log.extra_data
        })

    full_report = {
        "report_title": "交付批次验收报告",
        "generated_at": datetime.now().isoformat(),
        "batch_summary": {
            "id": batch.id,
            "batch_code": batch.batch_code,
            "name": batch.name,
            "description": batch.description,
            "status": batch.status,
            "submitter": {
                "id": submitter.id if submitter else None,
                "username": submitter.username if submitter else None,
                "display_name": submitter.display_name if submitter else None
            },
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
            "archived_at": batch.archived_at.isoformat() if batch.archived_at else None
        },
        "validation_summary": report.validation_summary if hasattr(report, 'validation_summary') else {},
        "version_history": detailed_versions,
        "approval_workflow": detailed_logs,
        "conclusion": {
            "validation_passed": report.validation_passed if hasattr(report, 'validation_passed') else False,
            "total_rejections": report.total_rejections if hasattr(report, 'total_rejections') else 0,
            "resolved_rejections": report.resolved_rejections if hasattr(report, 'resolved_rejections') else 0,
            "final_status": batch.status
        }
    }

    if format.lower() == "json":
        filename = f"acceptance_report_{batch.batch_code}.json"
        return JSONResponse(
            content=full_report,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format '{format}'. Use 'json'."
        )


def _get_user_info(db: Session, user_id: int) -> tuple:
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        return user.username, user.display_name
    return "unknown", None


def _build_import_info(db: Session, version: ManifestVersion) -> ImportInfo:
    username, display_name = _get_user_info(db, version.imported_by)
    return ImportInfo(
        version_number=version.version_number,
        imported_by_username=username,
        imported_by_display_name=display_name,
        imported_at=version.imported_at,
        item_count=version.item_count,
        import_format=version.import_format
    )


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


def _calculate_item_diffs(
    old_items: List[ManifestItem],
    new_items: List[ManifestItem]
) -> tuple:
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


def _collect_unresolved_rejections(
    db: Session,
    batch_id: int,
    old_version_id: int,
    new_version_id: int
) -> List[RejectionInfo]:
    rejections = db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id,
        RejectionRecord.resolved == False
    ).order_by(RejectionRecord.created_at.asc()).all()

    result = []
    for r in rejections:
        username, display_name = _get_user_info(db, r.rejector_id)
        result.append(RejectionInfo(
            id=r.id,
            item_key=r.item_key,
            line_number=r.line_number,
            rejection_reason=r.rejection_reason,
            rejector_username=username,
            rejector_display_name=display_name,
            created_at=r.created_at,
            resolved=r.resolved,
            resolved_at=r.resolved_at
        ))
    return result


def _collect_validation_changes(
    db: Session,
    old_version_id: int,
    new_version_id: int
) -> List[ValidationChange]:
    old_results = db.query(ValidationResult).filter(
        ValidationResult.manifest_version_id == old_version_id
    ).all()
    new_results = db.query(ValidationResult).filter(
        ValidationResult.manifest_version_id == new_version_id
    ).all()

    def key(r):
        return (r.item_key or "", r.rule_code, r.field_name or "")

    old_map = {key(r): r for r in old_results}
    new_map = {key(r): r for r in new_results}

    changes = []
    all_keys = sorted(set(list(old_map.keys()) + list(new_map.keys())))

    for k in all_keys:
        old_r = old_map.get(k)
        new_r = new_map.get(k)

        if old_r is None:
            if not new_r.passed:
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    new_severity=new_r.severity,
                    new_passed=new_r.passed,
                    new_message=new_r.message,
                    change_type="new_violation"
                ))
        elif new_r is None:
            if not old_r.passed:
                changes.append(ValidationChange(
                    item_key=old_r.item_key,
                    field_name=old_r.field_name,
                    rule_code=old_r.rule_code,
                    old_severity=old_r.severity,
                    old_passed=old_r.passed,
                    old_message=old_r.message,
                    change_type="resolved"
                ))
        else:
            old_failed = not old_r.passed
            new_failed = not new_r.passed
            if old_failed and not new_failed:
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    old_severity=old_r.severity,
                    new_severity=new_r.severity,
                    old_passed=old_r.passed,
                    new_passed=new_r.passed,
                    old_message=old_r.message,
                    new_message=new_r.message,
                    change_type="resolved"
                ))
            elif not old_failed and new_failed:
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    old_severity=old_r.severity,
                    new_severity=new_r.severity,
                    old_passed=old_r.passed,
                    new_passed=new_r.passed,
                    old_message=old_r.message,
                    new_message=new_r.message,
                    change_type="new_violation"
                ))
            elif old_failed and new_failed and (
                old_r.severity != new_r.severity or
                old_r.message != new_r.message
            ):
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    old_severity=old_r.severity,
                    new_severity=new_r.severity,
                    old_passed=old_r.passed,
                    new_passed=new_r.passed,
                    old_message=old_r.message,
                    new_message=new_r.message,
                    change_type="modified"
                ))

    return changes


def _count_validation_issues(db: Session, version_id: int) -> tuple:
    results = db.query(ValidationResult).filter(
        ValidationResult.manifest_version_id == version_id
    ).all()
    errors = sum(1 for r in results if not r.passed and r.severity == "error")
    warnings = sum(1 for r in results if not r.passed and r.severity == "warning")
    return errors, warnings


def _count_unresolved_rejections(db: Session, batch_id: int, version_id: int) -> int:
    return db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id,
        RejectionRecord.manifest_version_id == version_id,
        RejectionRecord.resolved == False
    ).count()


def _calculate_version_diff(
    db: Session,
    batch: DeliveryBatch,
    old_version: ManifestVersion,
    new_version: ManifestVersion,
    current_user: User
) -> VersionDiffResponse:
    old_items = sorted(old_version.items, key=lambda x: x.item_key)
    new_items = sorted(new_version.items, key=lambda x: x.item_key)

    added, removed, modified, unchanged = _calculate_item_diffs(old_items, new_items)

    total_field_changes = sum(len(m.field_changes) for m in modified)
    total_field_changes += sum(len(a.field_changes) for a in added)
    total_field_changes += sum(len(r.field_changes) for r in removed)

    unresolved_old = _count_unresolved_rejections(db, batch.id, old_version.id)
    unresolved_new = _count_unresolved_rejections(db, batch.id, new_version.id)

    val_errors_old, val_warnings_old = _count_validation_issues(db, old_version.id)
    val_errors_new, val_warnings_new = _count_validation_issues(db, new_version.id)

    unresolved_rejections = _collect_unresolved_rejections(
        db, batch.id, old_version.id, new_version.id
    )
    validation_changes = _collect_validation_changes(db, old_version.id, new_version.id)

    username, display_name = _get_user_info(db, current_user.id)

    metadata = VersionDiffMetadata(
        batch_id=batch.id,
        batch_code=batch.batch_code,
        batch_name=batch.name,
        old_version=old_version.version_number,
        new_version=new_version.version_number,
        old_import=_build_import_info(db, old_version),
        new_import=_build_import_info(db, new_version),
        generated_at=datetime.now(),
        generated_by_username=username,
        generated_by_display_name=display_name
    )

    summary = VersionDiffSummary(
        total_items_old=len(old_items),
        total_items_new=len(new_items),
        added_count=len(added),
        removed_count=len(removed),
        modified_count=len(modified),
        unchanged_count=len(unchanged),
        field_change_count=total_field_changes,
        unresolved_rejections_old=unresolved_old,
        unresolved_rejections_new=unresolved_new,
        validation_errors_old=val_errors_old,
        validation_errors_new=val_errors_new,
        validation_warnings_old=val_warnings_old,
        validation_warnings_new=val_warnings_new
    )

    return VersionDiffResponse(
        metadata=metadata,
        summary=summary,
        added_items=added,
        removed_items=removed,
        modified_items=modified,
        unchanged_items=unchanged,
        unresolved_rejections=unresolved_rejections,
        validation_changes=validation_changes
    )


def _log_diff_action(
    db: Session,
    batch_id: int,
    old_version_id: int,
    new_version_id: int,
    actor_id: int,
    action: str,
    extra_data: dict = None
):
    log = ApprovalLog(
        batch_id=batch_id,
        manifest_version_id=new_version_id,
        actor_id=actor_id,
        action=action,
        from_status=None,
        to_status=None,
        comment=f"查看版本差异: v{old_version_id} -> v{new_version_id}" if action == APPROVAL_LOG_ACTION_VIEW_DIFF
                else f"导出版本差异: v{old_version_id} -> v{new_version_id}",
        extra_data=extra_data or {}
    )
    db.add(log)
    db.commit()


def _get_versions_for_diff(
    db: Session,
    batch_id: int,
    old_version_number: Optional[int],
    new_version_number: Optional[int]
) -> tuple:
    versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id
    ).order_by(ManifestVersion.version_number.asc()).all()

    if len(versions) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"该批次仅有 {len(versions)} 个版本，至少需要 2 个版本才能进行对比。"
                   f" 请先导入新清单创建新版本。"
        )

    if new_version_number is None:
        new_version = versions[-1]
    else:
        new_version = next((v for v in versions if v.version_number == new_version_number), None)
        if not new_version:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"新版本号 v{new_version_number} 不存在"
            )

    if old_version_number is None:
        if len(versions) >= 2:
            old_version = versions[-2]
        else:
            old_version = versions[0]
    else:
        old_version = next((v for v in versions if v.version_number == old_version_number), None)
        if not old_version:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"旧版本号 v{old_version_number} 不存在"
            )

    if old_version.version_number > new_version.version_number:
        old_version, new_version = new_version, old_version

    return old_version, new_version


@router.get("/batches/{batch_id}/version-diff", response_model=VersionDiffResponse)
def get_version_diff(
    batch_id: int,
    old_version: Optional[int] = None,
    new_version: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_version_diff_access)
):
    batch = get_batch_or_404(db, batch_id)

    old_ver, new_ver = _get_versions_for_diff(db, batch_id, old_version, new_version)

    diff_result = _calculate_version_diff(db, batch, old_ver, new_ver, current_user)

    _log_diff_action(
        db=db,
        batch_id=batch_id,
        old_version_id=old_ver.version_number,
        new_version_id=new_ver.version_number,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_VIEW_DIFF,
        extra_data={
            "old_version": old_ver.version_number,
            "new_version": new_ver.version_number
        }
    )

    return diff_result


@router.get("/batches/{batch_id}/version-diff/export")
def export_version_diff(
    batch_id: int,
    old_version: Optional[int] = None,
    new_version: Optional[int] = None,
    format: str = "json",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_version_diff_access)
):
    batch = get_batch_or_404(db, batch_id)

    if format.lower() != "json":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format '{format}'. Use 'json'."
        )

    old_ver, new_ver = _get_versions_for_diff(db, batch_id, old_version, new_version)

    diff_result = _calculate_version_diff(db, batch, old_ver, new_ver, current_user)

    import hashlib
    export_id_parts = [
        str(batch_id),
        str(old_ver.version_number),
        str(new_ver.version_number),
        str(old_ver.id),
        str(new_ver.id),
        str(batch.batch_code)
    ]
    export_id = hashlib.sha256("|".join(export_id_parts).encode()).hexdigest()[:16]

    export_response = VersionDiffExportResponse(
        export_id=export_id,
        export_timestamp=datetime.now(),
        exported_by=current_user.username,
        diff_data=diff_result
    )

    _log_diff_action(
        db=db,
        batch_id=batch_id,
        old_version_id=old_ver.version_number,
        new_version_id=new_ver.version_number,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_EXPORT_DIFF,
        extra_data={
            "old_version": old_ver.version_number,
            "new_version": new_ver.version_number,
            "export_id": export_id,
            "format": format
        }
    )

    filename = f"version_diff_{batch.batch_code}_v{old_ver.version_number}_to_v{new_ver.version_number}.json"

    return JSONResponse(
        content=json.loads(export_response.model_dump_json()),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )
