from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime

from app.database import get_db
from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    RejectionRecord, ApprovalLog
)
from app.schemas import (
    BatchRejectionRequest, RejectionRecordResponse,
    BATCH_STATUS_PENDING, BATCH_STATUS_PARTIALLY_REJECTED,
    BATCH_STATUS_REPAIRING, BATCH_STATUS_APPROVED, BATCH_STATUS_ARCHIVED,
    ROLE_LEAD, ROLE_ADMIN, ROLE_REVIEWER, ROLE_SUBMITTER
)
from app.dependencies import (
    get_current_user, require_reviewer, require_lead, require_submitter_or_admin,
    get_batch_or_404
)

router = APIRouter(prefix="/api/batches", tags=["审批流程"])


@router.post("/{batch_id}/reject", response_model=dict)
def reject_batch(
    batch_id: int,
    rejection_data: BatchRejectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_reviewer)
):
    batch = get_batch_or_404(db, batch_id)

    if batch.status != BATCH_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only reject batches in 'pending_review' status. Current status: '{batch.status}'"
        )

    if not batch.current_manifest_version_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No manifest found for this batch"
        )

    manifest_version = db.query(ManifestVersion).filter(
        ManifestVersion.id == batch.current_manifest_version_id
    ).first()

    if not rejection_data.rejections:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one rejection record is required"
        )

    created_rejections = []

    try:
        sp = db.begin_nested()
        for rej in rejection_data.rejections:
            manifest_item = None
            item_key = rej.item_key
            line_number = rej.line_number

            if rej.manifest_item_id:
                manifest_item = db.query(ManifestItem).filter(
                    ManifestItem.id == rej.manifest_item_id,
                    ManifestItem.manifest_version_id == batch.current_manifest_version_id
                ).first()
                if manifest_item:
                    item_key = manifest_item.item_key
                    line_number = manifest_item.line_number
            elif rej.item_key:
                manifest_item = db.query(ManifestItem).filter(
                    ManifestItem.manifest_version_id == batch.current_manifest_version_id,
                    ManifestItem.item_key == rej.item_key
                ).first()
                if manifest_item:
                    line_number = manifest_item.line_number

            if not rej.rejection_reason or not rej.rejection_reason.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Rejection reason cannot be empty"
                )

            record = RejectionRecord(
                batch_id=batch_id,
                manifest_version_id=batch.current_manifest_version_id,
                manifest_item_id=manifest_item.id if manifest_item else None,
                rejector_id=current_user.id,
                rejection_reason=rej.rejection_reason.strip(),
                item_key=item_key,
                line_number=line_number,
            )
            db.add(record)
            db.flush()
            created_rejections.append(record)
        sp.commit()
    except HTTPException:
        raise
    except Exception as e:
        sp.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建驳回记录失败: {str(e)}"
        )

    from_status = batch.status
    batch.status = BATCH_STATUS_PARTIALLY_REJECTED

    log = ApprovalLog(
        batch_id=batch.id,
        manifest_version_id=batch.current_manifest_version_id,
        actor_id=current_user.id,
        action="REJECT",
        from_status=from_status,
        to_status=BATCH_STATUS_PARTIALLY_REJECTED,
        comment=rejection_data.comment or f"驳回 {len(created_rejections)} 项问题",
        extra_data={
            "rejection_count": len(created_rejections),
            "rejections": [
                {
                    "id": r.id,
                    "item_key": r.item_key,
                    "line_number": r.line_number,
                    "reason": r.rejection_reason
                }
                for r in created_rejections
            ]
        }
    )
    db.add(log)
    db.commit()
    db.refresh(batch)

    from app.diff_engine import refresh_snapshots_for_batch
    refresh_snapshots_for_batch(db, batch_id, current_user)

    return {
        "success": True,
        "batch_id": batch.id,
        "batch_status": batch.status,
        "rejection_count": len(created_rejections),
        "message": f"批次已驳回，共记录 {len(created_rejections)} 项问题",
        "rejections": [
            {
                "id": r.id,
                "manifest_item_id": r.manifest_item_id,
                "item_key": r.item_key,
                "line_number": r.line_number,
                "rejection_reason": r.rejection_reason
            }
            for r in created_rejections
        ]
    }


@router.post("/{batch_id}/start-repair", response_model=dict)
def start_repair(
    batch_id: int,
    comment: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_submitter_or_admin)
):
    batch = get_batch_or_404(db, batch_id)

    if current_user.role == ROLE_SUBMITTER and batch.submitter_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only start repair for batches you submitted"
        )

    if batch.status != BATCH_STATUS_PARTIALLY_REJECTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only start repair for batches in 'partially_rejected' status. Current: '{batch.status}'"
        )

    from_status = batch.status
    batch.status = BATCH_STATUS_REPAIRING

    log = ApprovalLog(
        batch_id=batch.id,
        manifest_version_id=batch.current_manifest_version_id,
        actor_id=current_user.id,
        action="START_REPAIR",
        from_status=from_status,
        to_status=BATCH_STATUS_REPAIRING,
        comment=comment or "开始返修，准备导入修订版清单"
    )
    db.add(log)
    db.commit()
    db.refresh(batch)

    return {
        "success": True,
        "batch_id": batch.id,
        "batch_status": batch.status,
        "message": "已进入返修状态，请导入修订版清单 (v2)"
    }


@router.post("/{batch_id}/approve", response_model=dict)
def approve_batch(
    batch_id: int,
    comment: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_lead)
):
    batch = get_batch_or_404(db, batch_id)

    if batch.status != BATCH_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only approve batches in 'pending_review' status. Current: '{batch.status}'"
        )

    if not batch.current_manifest_version_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No manifest found for this batch"
        )

    manifest = db.query(ManifestVersion).filter(
        ManifestVersion.id == batch.current_manifest_version_id
    ).first()

    unresolved_rejections = db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id,
        RejectionRecord.resolved == False
    ).count()

    if unresolved_rejections > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve: {unresolved_rejections} unresolved rejection(s) remain. "
                   "Please import new manifest to resolve them."
        )

    if manifest and manifest.validation_status != "passed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve: validation status is '{manifest.validation_status}'. "
                   "Please run validation first via POST /api/batches/{batch_id}/validate"
        )

    from_status = batch.status
    batch.status = BATCH_STATUS_APPROVED

    log = ApprovalLog(
        batch_id=batch.id,
        manifest_version_id=batch.current_manifest_version_id,
        actor_id=current_user.id,
        action="APPROVE",
        from_status=from_status,
        to_status=BATCH_STATUS_APPROVED,
        comment=comment or "验收通过，批次交付完成",
        extra_data={"approved_by": current_user.username, "approved_at": datetime.now().isoformat()}
    )
    db.add(log)
    db.commit()
    db.refresh(batch)

    return {
        "success": True,
        "batch_id": batch.id,
        "batch_status": batch.status,
        "approved_by": current_user.username,
        "approved_at": datetime.now().isoformat(),
        "message": "批次已通过验收！可以调用归档接口完成最终交付"
    }


@router.post("/{batch_id}/archive", response_model=dict)
def archive_batch(
    batch_id: int,
    comment: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_lead)
):
    batch = get_batch_or_404(db, batch_id)

    if batch.status != BATCH_STATUS_APPROVED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only archive batches in 'approved' status. Current: '{batch.status}'"
        )

    from_status = batch.status
    batch.status = BATCH_STATUS_ARCHIVED
    batch.archived_at = datetime.now()
    batch.archived_by = current_user.id

    log = ApprovalLog(
        batch_id=batch.id,
        manifest_version_id=batch.current_manifest_version_id,
        actor_id=current_user.id,
        action="ARCHIVE",
        from_status=from_status,
        to_status=BATCH_STATUS_ARCHIVED,
        comment=comment or "批次已归档，交付流程完成",
        extra_data={"archived_by": current_user.username}
    )
    db.add(log)
    db.commit()
    db.refresh(batch)

    return {
        "success": True,
        "batch_id": batch.id,
        "batch_status": batch.status,
        "archived_by": current_user.username,
        "archived_at": batch.archived_at.isoformat(),
        "message": "批次已归档！可导出验收报告"
    }


@router.get("/{batch_id}/rejections", response_model=List[RejectionRecordResponse])
def get_batch_rejections(
    batch_id: int,
    only_unresolved: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    query = db.query(RejectionRecord).filter(RejectionRecord.batch_id == batch_id)
    if only_unresolved:
        query = query.filter(RejectionRecord.resolved == False)
    return query.order_by(RejectionRecord.created_at.asc()).all()
