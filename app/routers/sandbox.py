from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)

from app.database import get_db
from app.models import User
from app.schemas import (
    SandboxRestoreResponse, SandboxImportResponse,
    SandboxDiffResponse, SandboxPrecheckResponse,
    SandboxConfirmResponse, SandboxRejectResponse,
    SandboxSessionResponse, SandboxSessionListResponse,
    SandboxConfirmRequest, SandboxRejectRequest,
    SandboxAuditLogResponse,
    ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER,
)
from app.dependencies import (
    get_current_user, require_lead,
    check_sandbox_enabled,
    require_sandbox_confirm_permission,
    require_sandbox_view_permission,
)
from app.sandbox_config import (
    is_sandbox_enabled,
    require_admin_for_confirm,
    CONFIG_KEY_SANDBOX_ENABLED,
    CONFIG_KEY_SANDBOX_REQUIRE_ADMIN_CONFIRM,
)
from app.sandbox_service import (
    restore_archive_to_sandbox,
    import_candidate_to_sandbox,
    calculate_sandbox_diff,
    run_sandbox_precheck,
    confirm_sandbox_restore,
    reject_sandbox_session,
    list_sandbox_sessions,
    get_sandbox_session_detail,
    get_sandbox_audit_logs,
)

router = APIRouter(prefix="/api/sandbox", tags=["恢复后验收沙盒"])


def _check_sandbox_enabled(db: Session = Depends(get_db)):
    check_sandbox_enabled(db)


@router.post("/restore", response_model=SandboxRestoreResponse)
async def restore_to_sandbox(
    file: UploadFile = File(..., description="验收归档包 ZIP 文件"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_lead),
):
    _check_sandbox_enabled(db)

    zip_bytes = await file.read()
    if len(zip_bytes) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="上传的文件为空"
        )

    result = restore_archive_to_sandbox(db, zip_bytes, current_user)
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message
        )
    return result


@router.post("/{sandbox_token}/import", response_model=SandboxImportResponse)
async def import_candidate_version(
    sandbox_token: str,
    file: UploadFile = File(..., description="候选版本清单文件 (CSV/JSON)"),
    import_format: str = Form("auto", description="文件格式: auto, csv, json"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_lead),
):
    _check_sandbox_enabled(db)

    content_bytes = await file.read()
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = content_bytes.decode("gbk", errors="replace")
    raw_content = content

    result = import_candidate_to_sandbox(
        db=db,
        sandbox_token=sandbox_token,
        raw_content=raw_content,
        filename=file.filename or "",
        import_format=import_format,
        current_user=current_user,
    )
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message
        )
    return result


@router.get("/{sandbox_token}/diff", response_model=SandboxDiffResponse)
def get_sandbox_version_diff(
    sandbox_token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_sandbox_view_permission),
):
    _check_sandbox_enabled(db)

    result = calculate_sandbox_diff(db, sandbox_token, current_user)
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message
        )
    return result


@router.post("/{sandbox_token}/precheck", response_model=SandboxPrecheckResponse)
def run_precheck(
    sandbox_token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_lead),
):
    _check_sandbox_enabled(db)

    result = run_sandbox_precheck(db, sandbox_token, current_user)
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message
        )
    return result


@router.post("/{sandbox_token}/confirm", response_model=SandboxConfirmResponse)
def confirm_restore(
    sandbox_token: str,
    request_data: SandboxConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_sandbox_confirm_permission),
):
    _check_sandbox_enabled(db)

    result = confirm_sandbox_restore(
        db=db,
        sandbox_token=sandbox_token,
        comment=request_data.comment,
        current_user=current_user,
    )
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message
        )
    return result


@router.post("/{sandbox_token}/reject", response_model=SandboxRejectResponse)
def reject_session(
    sandbox_token: str,
    request_data: SandboxRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_sandbox_confirm_permission),
):
    _check_sandbox_enabled(db)

    result = reject_sandbox_session(
        db=db,
        sandbox_token=sandbox_token,
        reason=request_data.reason,
        comment=request_data.comment,
        current_user=current_user,
    )
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message
        )
    return result


@router.get("/", response_model=List[SandboxSessionListResponse])
def list_sessions(
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_sandbox_view_permission),
):
    _check_sandbox_enabled(db)

    limit = max(1, min(limit, 200))
    results, total = list_sandbox_sessions(
        db=db,
        current_user=current_user,
        status=status,
        limit=limit,
        offset=skip,
    )
    return results


@router.get("/{sandbox_token}", response_model=SandboxSessionResponse)
def get_session_detail(
    sandbox_token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_sandbox_view_permission),
):
    _check_sandbox_enabled(db)

    try:
        result = get_sandbox_session_detail(db, sandbox_token, current_user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    return result


@router.get("/{sandbox_token}/audit-logs", response_model=List[SandboxAuditLogResponse])
def get_audit_logs(
    sandbox_token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_sandbox_view_permission),
):
    _check_sandbox_enabled(db)

    try:
        results = get_sandbox_audit_logs(db, sandbox_token, current_user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    return results


@router.get("/{sandbox_token}/eligibility")
def check_sandbox_eligibility(
    sandbox_token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_sandbox_enabled(db)

    sandbox_enabled = is_sandbox_enabled(db)
    req_admin_confirm = require_admin_for_confirm(db)

    can_confirm = False
    if req_admin_confirm:
        can_confirm = current_user.role == ROLE_ADMIN
    else:
        can_confirm = current_user.role in [ROLE_ADMIN, ROLE_LEAD]

    can_view = current_user.role in [ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER]

    return {
        "sandbox_token": sandbox_token,
        "sandbox_enabled": sandbox_enabled,
        "require_admin_confirm": req_admin_confirm,
        "your_role": current_user.role,
        "can_view": can_view,
        "can_confirm": can_confirm,
        "can_import": current_user.role in [ROLE_ADMIN, ROLE_LEAD],
    }
