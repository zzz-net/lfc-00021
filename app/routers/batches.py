from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from app.database import get_db
from app.models import User, DeliveryBatch, ApprovalLog, ManifestVersion
from app.schemas import (
    DeliveryBatchCreate, DeliveryBatchUpdate, DeliveryBatchResponse,
    StatusTransitionRequest, ApprovalLogResponse,
    BATCH_STATUS_DRAFT, BATCH_STATUS_PENDING, BATCH_STATUS_APPROVED, BATCH_STATUS_ARCHIVED,
    BATCH_STATUSES, ROLE_LEAD, ROLE_ADMIN, ROLE_SUBMITTER
)
from app.dependencies import (
    get_current_user, require_submitter_or_admin,
    validate_status_transition, get_batch_or_404
)

router = APIRouter(prefix="/api/batches", tags=["交付批次管理"])


@router.post("/", response_model=DeliveryBatchResponse, status_code=status.HTTP_201_CREATED)
def create_batch(
    batch_data: DeliveryBatchCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_submitter_or_admin)
):
    existing = db.query(DeliveryBatch).filter(
        DeliveryBatch.batch_code == batch_data.batch_code
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch with code '{batch_data.batch_code}' already exists"
        )

    submitter_id = batch_data.submitter_id
    if current_user.role == ROLE_SUBMITTER:
        submitter_id = current_user.id

    batch = DeliveryBatch(
        batch_code=batch_data.batch_code,
        name=batch_data.name,
        description=batch_data.description,
        status=BATCH_STATUS_DRAFT,
        submitter_id=submitter_id,
    )
    db.add(batch)
    db.flush()

    log = ApprovalLog(
        batch_id=batch.id,
        actor_id=current_user.id,
        action="CREATE",
        from_status=None,
        to_status=BATCH_STATUS_DRAFT,
        comment=f"创建批次: {batch.name}"
    )
    db.add(log)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/", response_model=List[DeliveryBatchResponse])
def list_batches(
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(DeliveryBatch)
    if status:
        if status not in BATCH_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{status}'. Valid statuses: {BATCH_STATUSES}"
            )
        query = query.filter(DeliveryBatch.status == status)
    return query.order_by(DeliveryBatch.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/{batch_id}", response_model=DeliveryBatchResponse)
def get_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_batch_or_404(db, batch_id)


@router.patch("/{batch_id}", response_model=DeliveryBatchResponse)
def update_batch(
    batch_id: int,
    update_data: DeliveryBatchUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_submitter_or_admin)
):
    batch = get_batch_or_404(db, batch_id)
    if batch.status == BATCH_STATUS_ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot update an archived batch"
        )
    if current_user.role == ROLE_SUBMITTER and batch.submitter_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update batches you submitted"
        )

    update_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(batch, key, value)

    log = ApprovalLog(
        batch_id=batch.id,
        actor_id=current_user.id,
        action="UPDATE",
        from_status=batch.status,
        to_status=batch.status,
        comment=f"更新批次信息: {list(update_dict.keys())}"
    )
    db.add(log)
    db.commit()
    db.refresh(batch)
    return batch


@router.post("/{batch_id}/transition", response_model=DeliveryBatchResponse)
def transition_status(
    batch_id: int,
    transition_data: StatusTransitionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = get_batch_or_404(db, batch_id)
    target_status = transition_data.target_status

    if target_status not in BATCH_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status '{target_status}'. Valid statuses: {BATCH_STATUSES}"
        )

    validate_status_transition(batch.status, target_status, current_user.role)

    if target_status == BATCH_STATUS_PENDING:
        if batch.current_manifest_version_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot submit for review without a manifest. Please import manifest first."
            )
        if current_user.role == ROLE_SUBMITTER and batch.submitter_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the submitter can submit for review"
            )

    if target_status == BATCH_STATUS_APPROVED:
        if current_user.role not in [ROLE_LEAD, ROLE_ADMIN]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Lead or Admin can approve batches"
            )
        manifest = db.query(ManifestVersion).filter(
            ManifestVersion.id == batch.current_manifest_version_id
        ).first()
        if manifest and manifest.validation_status != "passed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot approve: validation status is '{manifest.validation_status}'. "
                       "Please run validation and ensure no errors."
            )

    if target_status == BATCH_STATUS_ARCHIVED:
        if current_user.role not in [ROLE_LEAD, ROLE_ADMIN]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Lead or Admin can archive batches"
            )
        if batch.status != BATCH_STATUS_APPROVED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only approved batches can be archived"
            )
        batch.archived_at = datetime.now()
        batch.archived_by = current_user.id

    from_status = batch.status
    batch.status = target_status

    log = ApprovalLog(
        batch_id=batch.id,
        manifest_version_id=batch.current_manifest_version_id,
        actor_id=current_user.id,
        action="STATUS_TRANSITION",
        from_status=from_status,
        to_status=target_status,
        comment=transition_data.comment or f"状态变更: {from_status} -> {target_status}"
    )
    db.add(log)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/{batch_id}/logs", response_model=List[ApprovalLogResponse])
def get_batch_logs(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    logs = db.query(ApprovalLog).filter(
        ApprovalLog.batch_id == batch_id
    ).order_by(ApprovalLog.created_at.asc()).all()
    return logs
