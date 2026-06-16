from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from app.database import get_db
from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    RejectionRecord, ApprovalLog, ValidationResult
)
from app.schemas import (
    AcceptanceReportResponse, ApprovalLogResponse,
    BATCH_STATUS_APPROVED, BATCH_STATUS_ARCHIVED,
    ManifestVersionResponse
)
from app.dependencies import get_current_user, get_batch_or_404, require_lead

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
