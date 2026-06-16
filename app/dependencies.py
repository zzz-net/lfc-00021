from fastapi import Header, HTTPException, Depends, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, DeliveryBatch
from app.schemas import (
    ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER, ROLE_SUBMITTER,
    VALID_STATUS_TRANSITIONS
)


def get_current_user(
    x_user_id: int = Header(..., alias="X-User-Id"),
    db: Session = Depends(get_db)
) -> User:
    user = db.query(User).filter(User.id == x_user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"User with id {x_user_id} not found. Please create user first via POST /api/users/"
        )
    return user


def require_role(allowed_roles: list):
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. Required roles: {allowed_roles}, your role: {current_user.role}"
            )
        return current_user
    return role_checker


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin role required. Your role: {current_user.role}"
        )
    return current_user


def require_lead(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in [ROLE_LEAD, ROLE_ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Lead or Admin role required. Your role: {current_user.role}"
        )
    return current_user


def require_reviewer(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in [ROLE_REVIEWER, ROLE_LEAD, ROLE_ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Reviewer or above role required. Your role: {current_user.role}"
        )
    return current_user


def require_submitter_or_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in [ROLE_SUBMITTER, ROLE_ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Submitter or Admin role required. Your role: {current_user.role}"
        )
    return current_user


def validate_status_transition(current_status: str, target_status: str, user_role: str):
    allowed_targets = VALID_STATUS_TRANSITIONS.get(current_status, [])
    if target_status not in allowed_targets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition from '{current_status}' to '{target_status}'. "
                   f"Allowed transitions: {allowed_targets}"
        )


def get_batch_or_404(db: Session, batch_id: int) -> DeliveryBatch:
    batch = db.query(DeliveryBatch).filter(DeliveryBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Delivery batch with id {batch_id} not found"
        )
    return batch
