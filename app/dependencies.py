from fastapi import Header, HTTPException, Depends, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, DeliveryBatch, CONFIG_KEY_SANDBOX_ENABLED, CONFIG_KEY_SANDBOX_REQUIRE_ADMIN_CONFIRM
from app.schemas import (
    ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER, ROLE_SUBMITTER,
    VALID_STATUS_TRANSITIONS
)
from app.archive_service import _get_config_bool


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


def require_version_diff_access(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> User:
    batch = get_batch_or_404(db, batch_id)
    if current_user.role in [ROLE_LEAD, ROLE_ADMIN]:
        return current_user
    if current_user.role == ROLE_SUBMITTER and batch.submitter_id == current_user.id:
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"Permission denied. Only lead, admin, or the batch submitter can view version differences. "
            f"Your role: {current_user.role}, your user id: {current_user.id}, "
            f"batch submitter id: {batch.submitter_id}"
        )
    )


def check_sandbox_enabled(db: Session = Depends(get_db)):
    enabled = _get_config_bool(db, CONFIG_KEY_SANDBOX_ENABLED, True)
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="系统配置已关闭恢复后验收沙盒功能"
        )
    return True


def require_sandbox_confirm_permission(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    require_admin = _get_config_bool(db, CONFIG_KEY_SANDBOX_REQUIRE_ADMIN_CONFIRM, True)
    if require_admin:
        if current_user.role != ROLE_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足：系统配置要求只有 admin 角色才能执行沙盒确认/拒绝操作。您的角色: {current_user.role}"
            )
    else:
        if current_user.role not in [ROLE_ADMIN, ROLE_LEAD]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足：只有 admin 或 lead 角色才能执行沙盒确认/拒绝操作。您的角色: {current_user.role}"
            )
    return current_user


def require_sandbox_view_permission(
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role not in [ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"权限不足：只有 reviewer 及以上角色才能查看沙盒会话。您的角色: {current_user.role}"
        )
    return current_user
