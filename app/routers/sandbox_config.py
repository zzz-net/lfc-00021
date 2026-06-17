from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)

from app.database import get_db
from app.models import User
from app.schemas import (
    SandboxConfigUpdateRequest, SandboxConfigBatchUpdateRequest,
    SandboxConfigResponse, SandboxConfigListResponse,
    SandboxConfigAuditLogResponse, SandboxConfigEligibilityResponse,
    ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER,
)
from app.dependencies import (
    get_current_user, require_admin, require_reviewer,
)
from app.sandbox_service import (
    list_sandbox_configs,
    get_sandbox_config,
    update_sandbox_config,
    batch_update_sandbox_configs,
    get_sandbox_config_audit_logs,
)

router = APIRouter(prefix="/api/sandbox-config", tags=["沙盒配置管理台"])


@router.get("/", response_model=SandboxConfigListResponse)
def get_sandbox_config_list(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_reviewer),
):
    try:
        return list_sandbox_configs(db, current_user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/eligibility", response_model=SandboxConfigEligibilityResponse)
def check_config_eligibility(
    current_user: User = Depends(get_current_user),
):
    is_admin = current_user.role == ROLE_ADMIN
    is_lead = current_user.role == ROLE_LEAD
    is_reviewer = current_user.role == ROLE_REVIEWER
    can_view = is_admin or is_lead or is_reviewer
    can_edit = is_admin
    return SandboxConfigEligibilityResponse(
        can_view=can_view,
        can_edit=can_edit,
        is_admin=is_admin,
        is_lead=is_lead,
        is_reviewer=is_reviewer,
    )


@router.get("/audit-logs", response_model=List[SandboxConfigAuditLogResponse])
def get_config_audit_logs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_reviewer),
):
    try:
        return get_sandbox_config_audit_logs(db, current_user, limit, offset)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.patch("/batch")
def batch_update_sandbox_config_list(
    batch_data: SandboxConfigBatchUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    try:
        result = batch_update_sandbox_configs(db, batch_data.updates, current_user)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/{config_key}", response_model=SandboxConfigResponse)
def get_single_sandbox_config(
    config_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_reviewer),
):
    try:
        return get_sandbox_config(db, config_key, current_user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST
            if "白名单" in str(e) else status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.put("/{config_key}", response_model=SandboxConfigResponse)
def update_single_sandbox_config(
    config_key: str,
    update_data: SandboxConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    try:
        return update_sandbox_config(db, config_key, update_data.config_value, current_user)
    except ValueError as e:
        status_code = status.HTTP_400_BAD_REQUEST
        if "不存在" in str(e) and "白名单" not in str(e):
            status_code = status.HTTP_404_NOT_FOUND
        raise HTTPException(
            status_code=status_code,
            detail=str(e)
        )
