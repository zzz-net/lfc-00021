from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List
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
from app.sandbox_config import (
    list_configs,
    get_single_config,
    update_config,
    batch_update_configs,
    get_audit_logs,
    check_config_eligibility,
    ConcurrencyConflictError,
    ConfigValidationError,
    CONFIG_KEYS,
)

router = APIRouter(prefix="/api/sandbox-config", tags=["沙盒配置管理台"])


@router.get("/", response_model=SandboxConfigListResponse)
def get_sandbox_config_list(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_reviewer),
):
    try:
        return list_configs(db, current_user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/eligibility", response_model=SandboxConfigEligibilityResponse)
def check_config_eligibility_endpoint(
    current_user: User = Depends(get_current_user),
):
    result = check_config_eligibility(current_user)
    return SandboxConfigEligibilityResponse(**result)


@router.get("/audit-logs", response_model=List[SandboxConfigAuditLogResponse])
def get_config_audit_logs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_reviewer),
):
    try:
        return get_audit_logs(db, current_user, limit, offset)
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
        updates_list = [item.model_dump() for item in batch_data.updates]
        result = batch_update_configs(db, updates_list, current_user)
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
        return get_single_config(db, config_key, current_user)
    except ConfigValidationError as e:
        if "白名单" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except ValueError as e:
        if "白名单" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
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
        return update_config(
            db, config_key, update_data.config_value, current_user,
            expected_old_value=update_data.expected_old_value,
        )
    except ConcurrencyConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e)
        )
    except ConfigValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except ValueError as e:
        if "不存在" in str(e) and "白名单" not in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
