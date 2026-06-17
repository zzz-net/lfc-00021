from fastapi import APIRouter, Depends, HTTPException, status as http_status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import json
import io
import csv
import hashlib
import logging

logger = logging.getLogger(__name__)

from app.database import get_db
from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    RejectionRecord, ApprovalLog, ValidationResult, VersionDiffSnapshot
)
from app.schemas import (
    AcceptanceReportResponse, ApprovalLogResponse,
    BATCH_STATUS_APPROVED, BATCH_STATUS_ARCHIVED,
    ManifestVersionResponse,
    VersionDiffResponse, VersionDiffExportResponse,
    VersionDiffSnapshotResponse, VersionDiffSnapshotDetailResponse,
    SnapshotListResponse,
    APPROVAL_LOG_ACTION_VIEW_DIFF, APPROVAL_LOG_ACTION_EXPORT_DIFF,
    APPROVAL_LOG_ACTION_QUERY_SNAPSHOT, APPROVAL_LOG_ACTION_EXPORT_SNAPSHOT_CSV,
    VALID_EXPORT_FORMATS, DEFAULT_EXPORT_FORMAT,
    SNAPSHOT_DEFAULT_LIMIT, SNAPSHOT_MAX_LIMIT, VALID_SNAPSHOT_STATUSES
)
from app.dependencies import get_current_user, get_batch_or_404, require_lead, require_version_diff_access
from app.diff_engine import (
    calculate_version_diff, save_diff_snapshot,
    get_snapshot_by_versions, get_latest_snapshot, list_snapshots,
    snapshot_to_diff_response, _is_snapshot_stale, refresh_snapshot,
    get_or_compute_diff as _engine_get_or_compute_diff
)

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
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format '{format}'. Use 'json'."
        )


def _log_diff_action(
    db: Session,
    batch_id: int,
    old_version_number: int,
    new_version_number: int,
    actor_id: int,
    action: str,
    extra_data: dict = None
):
    log = ApprovalLog(
        batch_id=batch_id,
        manifest_version_id=None,
        actor_id=actor_id,
        action=action,
        from_status=None,
        to_status=None,
        comment=f"查看版本差异: v{old_version_number} -> v{new_version_number}" if action == APPROVAL_LOG_ACTION_VIEW_DIFF
                else (f"导出版本差异: v{old_version_number} -> v{new_version_number}" if action == APPROVAL_LOG_ACTION_EXPORT_DIFF
                      else (f"查询版本差异快照: v{old_version_number} -> v{new_version_number}" if action == APPROVAL_LOG_ACTION_QUERY_SNAPSHOT
                            else f"CSV导出版本差异: v{old_version_number} -> v{new_version_number}")),
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
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"该批次仅有 {len(versions)} 个版本，至少需要 2 个版本才能进行对比。"
                   f" 请先导入新清单创建新版本。"
        )

    if new_version_number is None:
        new_version = versions[-1]
    else:
        new_version = next((v for v in versions if v.version_number == new_version_number), None)
        if not new_version:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
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
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"旧版本号 v{old_version_number} 不存在"
            )

    if old_version.version_number > new_version.version_number:
        old_version, new_version = new_version, old_version

    return old_version, new_version


def _snapshot_to_list_response(snap: VersionDiffSnapshot) -> dict:
    summary = snap.summary_json or {}
    return {
        "id": snap.id,
        "batch_id": snap.batch_id,
        "old_version_id": snap.old_version_id,
        "new_version_id": snap.new_version_id,
        "old_version_number": snap.old_version_number,
        "new_version_number": snap.new_version_number,
        "snapshot_key": snap.snapshot_key,
        "status": snap.status,
        "created_by": snap.created_by,
        "created_at": snap.created_at,
        "invalidated_at": snap.invalidated_at,
        "invalidated_by": snap.invalidated_by,
        "content_hash": snap.content_hash,
        "metadata": snap.metadata_json or {},
        "summary": summary,
        "has_added": summary.get("added_count", 0) > 0,
        "has_removed": summary.get("removed_count", 0) > 0,
        "has_modified": summary.get("modified_count", 0) > 0,
        "has_unresolved_rejections": summary.get("unresolved_rejections_new", 0) > 0,
        "has_validation_changes": len(snap.validation_changes_json or []) > 0,
    }


def _ensure_snapshot_fresh(db: Session, snap, current_user: User, trigger: str = "query"):
    if _is_snapshot_stale(db, snap):
        logger.info(
            "[SNAP_QUERY:%s] Snapshot %s is STALE, refreshing before returning to caller...",
            trigger, snap.snapshot_key
        )
        snap = refresh_snapshot(db, snap, current_user, trigger=trigger)
        db.commit()
        db.refresh(snap)
        logger.info(
            "[SNAP_QUERY:%s] Snapshot %s refreshed (committed), new content_hash=%s...",
            trigger, snap.snapshot_key, snap.content_hash[:16]
        )
    else:
        logger.debug(
            "[SNAP_QUERY:%s] Snapshot %s is FRESH, reusing (hash=%s...).",
            trigger, snap.snapshot_key, snap.content_hash[:16]
        )
    return snap


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

    logger.info(
        "[ENTRY:version-diff] batch_id=%d, v%d->v%d, user=%d",
        batch_id, old_ver.version_number, new_ver.version_number, current_user.id
    )

    diff_result, from_snapshot, snap = _engine_get_or_compute_diff(
        db, batch, old_ver, new_ver, current_user, auto_persist=True
    )

    extra = {
        "old_version": old_ver.version_number,
        "new_version": new_ver.version_number,
        "from_snapshot": from_snapshot,
        "entry": "version-diff",
    }
    if snap:
        extra["snapshot_id"] = snap.id
        extra["snapshot_key"] = snap.snapshot_key
        extra["snapshot_hash"] = snap.content_hash[:16]

    _log_diff_action(
        db=db,
        batch_id=batch_id,
        old_version_number=old_ver.version_number,
        new_version_number=new_ver.version_number,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_VIEW_DIFF,
        extra_data=extra
    )

    logger.info(
        "[ENTRY:version-diff] DONE batch_id=%d, from_snapshot=%s, snap_id=%s, "
        "val_changes=%d (breakdown: new_violation=%d, resolved=%d, modified=%d, "
        "new_passed=%d, removed_passed=%d, unchanged=%d)",
        batch_id, from_snapshot, snap.id if snap else None,
        diff_result.summary.validation_changes_total,
        diff_result.summary.validation_changes_new_violation,
        diff_result.summary.validation_changes_resolved,
        diff_result.summary.validation_changes_modified,
        diff_result.summary.validation_changes_new_passed,
        diff_result.summary.validation_changes_removed_passed,
        diff_result.summary.validation_changes_unchanged,
    )

    return diff_result


@router.get("/batches/{batch_id}/snapshots", response_model=SnapshotListResponse)
def list_version_diff_snapshots(
    batch_id: int,
    status: Optional[str] = None,
    limit: int = SNAPSHOT_DEFAULT_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_version_diff_access)
):
    batch = get_batch_or_404(db, batch_id)

    if status and status not in VALID_SNAPSHOT_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status '{status}'. Valid statuses: {VALID_SNAPSHOT_STATUSES}"
        )

    limit = max(1, min(limit, SNAPSHOT_MAX_LIMIT))
    offset = max(0, offset)

    snapshots, total = list_snapshots(db, batch_id, status, limit, offset)

    log = ApprovalLog(
        batch_id=batch_id,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_QUERY_SNAPSHOT,
        comment=f"列出版本差异快照: {total} 条, offset={offset}, limit={limit}",
        extra_data={
            "status_filter": status,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
    db.add(log)
    db.commit()

    return SnapshotListResponse(
        batch_id=batch.id,
        batch_code=batch.batch_code,
        total=total,
        snapshots=[_snapshot_to_list_response(s) for s in snapshots]
    )


@router.get("/batches/{batch_id}/snapshots/latest", response_model=VersionDiffSnapshotDetailResponse)
def get_latest_version_diff_snapshot(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_version_diff_access)
):
    batch = get_batch_or_404(db, batch_id)

    snap = get_latest_snapshot(db, batch_id)
    if not snap:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="该批次尚无有效版本差异快照。请先导入至少两个版本的清单。"
        )

    logger.info(
        "[ENTRY:snap-latest] batch_id=%d, found snap_id=%d (%s), user=%d",
        batch_id, snap.id, snap.snapshot_key, current_user.id
    )

    snap = _ensure_snapshot_fresh(db, snap, current_user, trigger="snap-latest")
    diff = snapshot_to_diff_response(snap)

    log = ApprovalLog(
        batch_id=batch_id,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_QUERY_SNAPSHOT,
        comment=f"查询最新版本差异快照: v{snap.old_version_number} -> v{snap.new_version_number}",
        extra_data={
            "snapshot_id": snap.id,
            "snapshot_key": snap.snapshot_key,
            "snapshot_hash": snap.content_hash[:16],
            "old_version": snap.old_version_number,
            "new_version": snap.new_version_number,
            "entry": "snap-latest",
        }
    )
    db.add(log)
    db.commit()

    logger.info(
        "[ENTRY:snap-latest] DONE batch_id=%d, snap_id=%d, val_changes=%d, "
        "errors_new=%d, warnings_new=%d, unresolved=%d",
        batch_id, snap.id, len(diff.validation_changes),
        diff.summary.validation_errors_new, diff.summary.validation_warnings_new,
        diff.summary.unresolved_rejections_new,
    )

    base = _snapshot_to_list_response(snap)
    return VersionDiffSnapshotDetailResponse(
        **base,
        added_items=diff.added_items,
        removed_items=diff.removed_items,
        modified_items=diff.modified_items,
        unchanged_items=diff.unchanged_items,
        unresolved_rejections=diff.unresolved_rejections,
        validation_changes=diff.validation_changes,
    )


@router.get("/batches/{batch_id}/snapshots/by-versions", response_model=VersionDiffSnapshotDetailResponse)
def get_snapshot_by_version_pair(
    batch_id: int,
    old_version: int,
    new_version: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_version_diff_access)
):
    batch = get_batch_or_404(db, batch_id)

    snap = get_snapshot_by_versions(db, batch_id, old_version, new_version)
    if not snap:
        logger.info(
            "[ENTRY:snap-by-versions] batch_id=%d, v%d->v%d: NO snapshot yet, "
            "will compute+persist on-demand via version-diff path",
            batch_id, old_version, new_version
        )
        old_ver, new_ver = _get_versions_for_diff(db, batch_id, old_version, new_version)
        diff_result, _, snap = _engine_get_or_compute_diff(
            db, batch, old_ver, new_ver, current_user, auto_persist=True
        )
        if not snap:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"未找到 v{old_version} -> v{new_version} 的有效快照，且实时计算落库失败。"
            )
        diff = diff_result
    else:
        logger.info(
            "[ENTRY:snap-by-versions] batch_id=%d, v%d->v%d: found snap_id=%d (%s), user=%d",
            batch_id, old_version, new_version, snap.id, snap.snapshot_key, current_user.id
        )
        snap = _ensure_snapshot_fresh(db, snap, current_user, trigger="snap-by-versions")
        diff = snapshot_to_diff_response(snap)

    log = ApprovalLog(
        batch_id=batch_id,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_QUERY_SNAPSHOT,
        comment=f"按版本查询快照: v{old_version} -> v{new_version}",
        extra_data={
            "snapshot_id": snap.id,
            "snapshot_key": snap.snapshot_key,
            "snapshot_hash": snap.content_hash[:16],
            "old_version": old_version,
            "new_version": new_version,
            "entry": "snap-by-versions",
        }
    )
    db.add(log)
    db.commit()

    logger.info(
        "[ENTRY:snap-by-versions] DONE batch_id=%d, snap_id=%d, val_changes=%d",
        batch_id, snap.id, len(diff.validation_changes)
    )

    base = _snapshot_to_list_response(snap)
    return VersionDiffSnapshotDetailResponse(
        **base,
        added_items=diff.added_items,
        removed_items=diff.removed_items,
        modified_items=diff.modified_items,
        unchanged_items=diff.unchanged_items,
        unresolved_rejections=diff.unresolved_rejections,
        validation_changes=diff.validation_changes,
    )


@router.get("/batches/{batch_id}/snapshots/{snapshot_id}", response_model=VersionDiffSnapshotDetailResponse)
def get_snapshot_by_id(
    batch_id: int,
    snapshot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_version_diff_access)
):
    get_batch_or_404(db, batch_id)

    snap = db.query(VersionDiffSnapshot).filter(
        VersionDiffSnapshot.id == snapshot_id,
        VersionDiffSnapshot.batch_id == batch_id
    ).first()
    if not snap:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"快照 ID {snapshot_id} 不存在或不属于该批次"
        )

    logger.info(
        "[ENTRY:snap-by-id] batch_id=%d, snap_id=%d (%s), user=%d",
        batch_id, snap.id, snap.snapshot_key, current_user.id
    )

    snap = _ensure_snapshot_fresh(db, snap, current_user, trigger="snap-by-id")
    diff = snapshot_to_diff_response(snap)

    log = ApprovalLog(
        batch_id=batch_id,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_QUERY_SNAPSHOT,
        comment=f"按ID查询快照: {snapshot_id}",
        extra_data={
            "snapshot_id": snap.id,
            "snapshot_key": snap.snapshot_key,
            "snapshot_hash": snap.content_hash[:16],
            "old_version": snap.old_version_number,
            "new_version": snap.new_version_number,
            "entry": "snap-by-id",
        }
    )
    db.add(log)
    db.commit()

    logger.info(
        "[ENTRY:snap-by-id] DONE batch_id=%d, snap_id=%d, val_changes=%d",
        batch_id, snap.id, len(diff.validation_changes)
    )

    base = _snapshot_to_list_response(snap)
    return VersionDiffSnapshotDetailResponse(
        **base,
        added_items=diff.added_items,
        removed_items=diff.removed_items,
        modified_items=diff.modified_items,
        unchanged_items=diff.unchanged_items,
        unresolved_rejections=diff.unresolved_rejections,
        validation_changes=diff.validation_changes,
    )


def _build_diff_csv_rows(diff: VersionDiffResponse) -> List[List[str]]:
    rows = []
    header = [
        "change_category",
        "item_key",
        "action",
        "field_name",
        "old_value",
        "new_value",
        "change_type",
        "line_number_old",
        "line_number_new",
    ]
    rows.append(header)

    for item in diff.added_items:
        for fc in item.field_changes:
            rows.append([
                "item_added",
                item.item_key,
                item.action,
                fc.field_name,
                "" if fc.old_value is None else str(fc.old_value),
                "" if fc.new_value is None else str(fc.new_value),
                fc.change_type,
                "" if item.line_number_old is None else str(item.line_number_old),
                "" if item.line_number_new is None else str(item.line_number_new),
            ])

    for item in diff.removed_items:
        for fc in item.field_changes:
            rows.append([
                "item_removed",
                item.item_key,
                item.action,
                fc.field_name,
                "" if fc.old_value is None else str(fc.old_value),
                "" if fc.new_value is None else str(fc.new_value),
                fc.change_type,
                "" if item.line_number_old is None else str(item.line_number_old),
                "" if item.line_number_new is None else str(item.line_number_new),
            ])

    for item in diff.modified_items:
        for fc in item.field_changes:
            rows.append([
                "item_modified",
                item.item_key,
                item.action,
                fc.field_name,
                "" if fc.old_value is None else str(fc.old_value),
                "" if fc.new_value is None else str(fc.new_value),
                fc.change_type,
                "" if item.line_number_old is None else str(item.line_number_old),
                "" if item.line_number_new is None else str(item.line_number_new),
            ])

    for r in diff.unresolved_rejections:
        rows.append([
            "unresolved_rejection",
            r.item_key or "",
            "rejection",
            "",
            "",
            r.rejection_reason,
            "rejection",
            "" if r.line_number is None else str(r.line_number),
            "",
        ])

    for vc in diff.validation_changes:
        old_display = ""
        new_display = ""
        if vc.old_passed is not None:
            status = "PASS" if vc.old_passed else "FAIL"
            sev = f"[{vc.old_severity}]" if vc.old_severity else ""
            msg = vc.old_message or ""
            old_display = f"{status}{sev} {msg}".strip()
        if vc.new_passed is not None:
            status = "PASS" if vc.new_passed else "FAIL"
            sev = f"[{vc.new_severity}]" if vc.new_severity else ""
            msg = vc.new_message or ""
            new_display = f"{status}{sev} {msg}".strip()

        rows.append([
            "validation_change",
            vc.item_key or "",
            vc.change_type,
            vc.field_name or "",
            old_display,
            new_display,
            vc.change_type,
            "",
            "",
        ])

    return rows


def _stream_csv(rows: List[List[str]]) -> io.StringIO:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row)
    buffer.seek(0)
    return buffer


@router.get("/batches/{batch_id}/version-diff/export")
def export_version_diff(
    batch_id: int,
    old_version: Optional[int] = None,
    new_version: Optional[int] = None,
    format: str = DEFAULT_EXPORT_FORMAT,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_version_diff_access)
):
    batch = get_batch_or_404(db, batch_id)

    format_lower = format.lower()
    if format_lower not in VALID_EXPORT_FORMATS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format '{format}'. Valid formats: {VALID_EXPORT_FORMATS}"
        )

    old_ver, new_ver = _get_versions_for_diff(db, batch_id, old_version, new_version)

    logger.info(
        "[ENTRY:export-%s] batch_id=%d, v%d->v%d, user=%d, format=%s",
        format_lower, batch_id, old_ver.version_number, new_ver.version_number,
        current_user.id, format_lower
    )

    diff_result, from_snapshot, snap = _engine_get_or_compute_diff(
        db, batch, old_ver, new_ver, current_user, auto_persist=True
    )

    export_id_parts = [
        str(batch_id),
        str(old_ver.version_number),
        str(new_ver.version_number),
        str(old_ver.id),
        str(new_ver.id),
        str(batch.batch_code),
        format_lower,
        str(snap.id if snap else 0),
        str(snap.content_hash if snap else "no-snapshot"),
    ]
    export_id = hashlib.sha256("|".join(export_id_parts).encode()).hexdigest()[:16]

    extra = {
        "old_version": old_ver.version_number,
        "new_version": new_ver.version_number,
        "export_id": export_id,
        "format": format_lower,
        "from_snapshot": from_snapshot,
        "entry": f"export-{format_lower}",
    }
    if snap:
        extra["snapshot_id"] = snap.id
        extra["snapshot_key"] = snap.snapshot_key
        extra["snapshot_hash"] = snap.content_hash[:16]

    action_type = APPROVAL_LOG_ACTION_EXPORT_SNAPSHOT_CSV if format_lower == "csv" else APPROVAL_LOG_ACTION_EXPORT_DIFF
    _log_diff_action(
        db=db,
        batch_id=batch_id,
        old_version_number=old_ver.version_number,
        new_version_number=new_ver.version_number,
        actor_id=current_user.id,
        action=action_type,
        extra_data=extra
    )

    logger.info(
        "[ENTRY:export-%s] DONE batch_id=%d, from_snapshot=%s, snap_id=%s, "
        "export_id=%s, val_changes=%d, added=%d, removed=%d, modified=%d, unchanged=%d",
        format_lower, batch_id, from_snapshot, snap.id if snap else None,
        export_id, diff_result.summary.validation_changes_total,
        diff_result.summary.added_count, diff_result.summary.removed_count,
        diff_result.summary.modified_count, diff_result.summary.unchanged_count,
    )

    if format_lower == "json":
        export_response = VersionDiffExportResponse(
            export_id=export_id,
            export_timestamp=datetime.now(),
            exported_by=current_user.username,
            diff_data=diff_result
        )
        filename = f"version_diff_{batch.batch_code}_v{old_ver.version_number}_to_v{new_ver.version_number}.json"
        return JSONResponse(
            content=json.loads(export_response.model_dump_json()),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    else:
        rows = _build_diff_csv_rows(diff_result)
        buffer = _stream_csv(rows)
        filename = f"version_diff_{batch.batch_code}_v{old_ver.version_number}_to_v{new_ver.version_number}.csv"
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
