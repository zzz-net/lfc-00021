from fastapi import APIRouter, Depends, HTTPException, status as http_status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import io
import logging

logger = logging.getLogger(__name__)

from app.database import get_db
from app.models import (
    User, DeliveryBatch, ApprovalLog, SystemConfig,
    APPROVAL_LOG_ACTION_EXPORT_ARCHIVE,
    CONFIG_KEY_ARCHIVE_ALLOW_OVERWRITE, CONFIG_KEY_ARCHIVE_ENABLED,
)
from app.schemas import (
    ArchivePrecheckResponse, ArchiveRestoreResponse,
    SystemConfigResponse, SystemConfigUpdateRequest,
)
from app.dependencies import (
    get_current_user, require_admin, get_batch_or_404,
)
from app.archive_service import (
    build_archive_zip, precheck_import_archive, restore_archive,
    _get_config_bool, ensure_default_configs,
)

router = APIRouter(prefix="/api", tags=["验收归档包"])


@router.post("/batches/{batch_id}/archive/export")
@router.get("/batches/{batch_id}/archive/export")
def export_batch_archive(
    batch_id: int,
    notes: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    batch = get_batch_or_404(db, batch_id)

    archive_enabled = _get_config_bool(db, CONFIG_KEY_ARCHIVE_ENABLED, True)
    if not archive_enabled:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="系统配置已关闭验收归档包导出功能"
        )

    try:
        zip_bytes, zip_hash, manifest = build_archive_zip(db, batch_id, current_user, notes)
    except ValueError as e:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    log = ApprovalLog(
        batch_id=batch.id,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_EXPORT_ARCHIVE,
        comment=f"导出验收归档包: {manifest.archive_id}",
        extra_data={
            "archive_id": manifest.archive_id,
            "batch_code": manifest.batch_code,
            "archive_size_bytes": len(zip_bytes),
            "archive_sha256": zip_hash,
            "section_counts": manifest.item_counts,
            "notes": notes,
        }
    )
    db.add(log)
    db.commit()

    filename = f"acceptance_archive_{batch.batch_code}_{manifest.archive_id[:8]}.zip"

    from fastapi.responses import Response
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Archive-Id": manifest.archive_id,
            "X-Archive-Hash": zip_hash,
            "X-Batch-Code": batch.batch_code,
        }
    )


@router.post("/archive/precheck", response_model=ArchivePrecheckResponse)
def try_import_archive_precheck(
    file: UploadFile = File(..., description="验收归档包 ZIP 文件"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    archive_enabled = _get_config_bool(db, CONFIG_KEY_ARCHIVE_ENABLED, True)
    if not archive_enabled:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="系统配置已关闭验收归档包导入功能"
        )

    zip_bytes = file.file.read()
    if len(zip_bytes) == 0:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="上传的文件为空"
        )

    result = precheck_import_archive(db, zip_bytes, current_user)
    return result


@router.post("/archive/restore", response_model=ArchiveRestoreResponse)
def restore_archive_package(
    file: UploadFile = File(..., description="验收归档包 ZIP 文件"),
    force_overwrite: bool = Form(False, description="是否允许覆盖已存在相同 batch_code 的批次"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    archive_enabled = _get_config_bool(db, CONFIG_KEY_ARCHIVE_ENABLED, True)
    if not archive_enabled:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="系统配置已关闭验收归档包导入功能"
        )

    zip_bytes = file.file.read()
    if len(zip_bytes) == 0:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="上传的文件为空"
        )

    result = restore_archive(db, zip_bytes, current_user, force_overwrite=force_overwrite)
    if not result.success:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=result.message
        )
    return result


@router.get("/system-configs/", response_model=List[SystemConfigResponse])
def list_system_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    ensure_default_configs(db)
    configs = db.query(SystemConfig).order_by(SystemConfig.config_key).all()
    return configs


@router.get("/system-configs/{config_key}", response_model=SystemConfigResponse)
def get_system_config(
    config_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    ensure_default_configs(db)
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()
    if not cfg:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"配置项 {config_key} 不存在"
        )
    return cfg


@router.put("/system-configs/{config_key}", response_model=SystemConfigResponse)
def update_system_config(
    config_key: str,
    update_data: SystemConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    ensure_default_configs(db)
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()
    if not cfg:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"配置项 {config_key} 不存在"
        )

    valid_keys = [CONFIG_KEY_ARCHIVE_ENABLED, CONFIG_KEY_ARCHIVE_ALLOW_OVERWRITE]
    if config_key not in valid_keys:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"不允许修改配置项 {config_key}。允许修改的配置: {valid_keys}"
        )

    cfg.config_value = update_data.config_value
    if update_data.value_type:
        cfg.value_type = update_data.value_type
    cfg.updated_by = current_user.id
    db.commit()
    db.refresh(cfg)

    log = ApprovalLog(
        batch_id=0,
        actor_id=current_user.id,
        action="UPDATE_SYSTEM_CONFIG",
        comment=f"修改系统配置: {config_key} = '{cfg.config_value}' ({cfg.value_type})",
        extra_data={
            "config_key": config_key,
            "new_value": cfg.config_value,
            "value_type": cfg.value_type,
        }
    )
    db.add(log)
    db.commit()

    return cfg


@router.get("/batches/{batch_id}/archive/eligibility")
def check_archive_export_eligibility(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    batch = get_batch_or_404(db, batch_id)
    archive_enabled = _get_config_bool(db, CONFIG_KEY_ARCHIVE_ENABLED, True)
    overwrite_enabled = _get_config_bool(db, CONFIG_KEY_ARCHIVE_ALLOW_OVERWRITE, False)

    from sqlalchemy.orm import Session as SaSession
    versions_count = db.query(ApprovalLog).filter(ApprovalLog.batch_id == batch_id).count()

    return {
        "batch_id": batch.id,
        "batch_code": batch.batch_code,
        "status": batch.status,
        "can_export": archive_enabled,
        "archive_enabled": archive_enabled,
        "overwrite_enabled": overwrite_enabled,
        "approval_log_count": versions_count,
        "role_required_for_import": "admin",
        "your_role": current_user.role,
        "you_can_import": current_user.role == "admin",
    }
