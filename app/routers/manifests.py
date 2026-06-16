from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional
import csv
import json
import io

from app.database import get_db
from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    ApprovalLog, RejectionRecord
)
from app.schemas import (
    ManifestVersionResponse, ImportResponse, ImportValidationError,
    BATCH_STATUS_DRAFT, BATCH_STATUS_REPAIRING, BATCH_STATUS_PARTIALLY_REJECTED,
    ROLE_SUBMITTER
)
from app.dependencies import (
    get_current_user, require_submitter_or_admin, get_batch_or_404
)

router = APIRouter(prefix="/api/batches", tags=["清单管理"])

REQUIRED_MANIFEST_FIELDS = ["item_id", "item_name", "quantity", "unit_price"]


def _validate_item_fields(item_data: dict, line_number: int, item_key: Optional[str]) -> List[ImportValidationError]:
    errors = []
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in item_data:
            errors.append(ImportValidationError(
                line_number=line_number,
                item_key=item_key,
                field_name=field,
                error_message=f"缺少必填字段 '{field}'"
            ))
        elif item_data[field] is None or (isinstance(item_data[field], str) and item_data[field].strip() == ""):
            errors.append(ImportValidationError(
                line_number=line_number,
                item_key=item_key,
                field_name=field,
                error_message=f"必填字段 '{field}' 为空"
            ))
    return errors


def _parse_csv(content: str) -> tuple:
    items = []
    errors = []
    reader = csv.DictReader(io.StringIO(content))

    missing_headers = [f for f in REQUIRED_MANIFEST_FIELDS if f not in (reader.fieldnames or [])]
    if missing_headers:
        errors.append(ImportValidationError(
            line_number=1,
            item_key=None,
            field_name=None,
            error_message=f"CSV 表头缺少必填列: {', '.join(missing_headers)}"
        ))
        return None, errors

    for idx, row in enumerate(reader, start=2):
        item_key = row.get("item_id") or f"line_{idx}"
        cleaned_row = {}
        for k, v in row.items():
            if k is not None:
                cleaned_row[k] = v.strip() if isinstance(v, str) else v
        item_errors = _validate_item_fields(cleaned_row, idx, item_key)
        errors.extend(item_errors)
        items.append({
            "line_number": idx,
            "item_key": item_key,
            "item_data": cleaned_row
        })

    return items, errors


def _parse_json(content: str) -> tuple:
    items = []
    errors = []
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        errors.append(ImportValidationError(
            line_number=None,
            item_key=None,
            field_name=None,
            error_message=f"JSON 解析错误: {str(e)}"
        ))
        return None, errors

    if not isinstance(data, list):
        errors.append(ImportValidationError(
            line_number=None,
            item_key=None,
            field_name=None,
            error_message="JSON 根节点必须是数组格式"
        ))
        return None, errors

    for idx, row in enumerate(data, start=1):
        if not isinstance(row, dict):
            errors.append(ImportValidationError(
                line_number=idx,
                item_key=f"entry_{idx}",
                field_name=None,
                error_message=f"第 {idx} 条记录必须是对象格式"
            ))
            continue
        item_key = row.get("item_id") or f"entry_{idx}"
        item_errors = _validate_item_fields(row, idx, item_key)
        errors.extend(item_errors)
        items.append({
            "line_number": idx,
            "item_key": item_key,
            "item_data": row
        })

    return items, errors


@router.post("/{batch_id}/manifests/import", response_model=ImportResponse)
async def import_manifest(
    batch_id: int,
    file: UploadFile = File(...),
    import_format: str = Form("auto"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_submitter_or_admin)
):
    batch = get_batch_or_404(db, batch_id)

    if batch.status not in [BATCH_STATUS_DRAFT, BATCH_STATUS_REPAIRING, BATCH_STATUS_PARTIALLY_REJECTED]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot import manifest in status '{batch.status}'. "
                   f"Allowed statuses: draft, repairing, partially_rejected"
        )

    if current_user.role == ROLE_SUBMITTER and batch.submitter_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only import manifests for batches you submitted"
        )

    content_bytes = await file.read()
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = content_bytes.decode("gbk", errors="replace")

    raw_content = content

    detected_format = import_format
    if detected_format == "auto":
        filename = file.filename or ""
        if filename.lower().endswith(".csv"):
            detected_format = "csv"
        elif filename.lower().endswith(".json"):
            detected_format = "json"
        else:
            try:
                json.loads(content)
                detected_format = "json"
            except json.JSONDecodeError:
                detected_format = "csv"

    if detected_format == "csv":
        items, parse_errors = _parse_csv(content)
    elif detected_format == "json":
        items, parse_errors = _parse_json(content)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format: {import_format}. Use 'csv', 'json', or 'auto'."
        )

    if parse_errors and items is None:
        return ImportResponse(
            success=False,
            errors=parse_errors,
            message="清单解析失败，未创建新版本，旧清单保持不变"
        )

    critical_errors = [e for e in parse_errors if e.field_name in REQUIRED_MANIFEST_FIELDS or e.field_name is None]
    if critical_errors:
        return ImportResponse(
            success=False,
            errors=parse_errors,
            message=f"发现 {len(critical_errors)} 个字段错误，未创建新版本，旧清单保持不变。请修复后重新导入。"
        )

    duplicate_version = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id,
        ManifestVersion.raw_content == raw_content,
        ManifestVersion.import_format == detected_format
    ).first()
    if duplicate_version:
        return ImportResponse(
            success=True,
            manifest_version_id=duplicate_version.id,
            version_number=duplicate_version.version_number,
            item_count=duplicate_version.item_count,
            errors=[],
            message=f"内容无变更，复用现有版本 v{duplicate_version.version_number}。"
        )

    old_version_id = batch.current_manifest_version_id

    existing_versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id
    ).count()
    new_version_number = existing_versions + 1

    sp = db.begin_nested()
    try:
        manifest_version = ManifestVersion(
            batch_id=batch_id,
            version_number=new_version_number,
            import_format=detected_format,
            imported_by=current_user.id,
            item_count=len(items),
            raw_content=raw_content,
            validation_status="pending",
        )
        db.add(manifest_version)
        sp.commit()
    except Exception as e:
        sp.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建清单版本失败: {str(e)}"
        )

    try:
        manifest_items = []
        for item in items:
            mi = ManifestItem(
                manifest_version_id=manifest_version.id,
                line_number=item["line_number"],
                item_key=item["item_key"],
                item_data=item["item_data"],
            )
            manifest_items.append(mi)
        db.bulk_save_objects(manifest_items)
    except Exception as e:
        db.rollback()
        batch.current_manifest_version_id = old_version_id
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"保存清单条目失败，已回滚: {str(e)}"
        )

    batch.current_manifest_version_id = manifest_version.id
    batch.status = BATCH_STATUS_DRAFT if batch.status in [BATCH_STATUS_REPAIRING, BATCH_STATUS_PARTIALLY_REJECTED] else batch.status

    if old_version_id:
        old_rejections = db.query(RejectionRecord).filter(
            RejectionRecord.batch_id == batch_id,
            RejectionRecord.manifest_version_id == old_version_id,
            RejectionRecord.resolved == False
        ).all()
        for rej in old_rejections:
            rej.resolved = True
            rej.resolved_at = __import__("datetime").datetime.now()
            rej.resolved_by_manifest_version_id = manifest_version.id

    log = ApprovalLog(
        batch_id=batch.id,
        manifest_version_id=manifest_version.id,
        actor_id=current_user.id,
        action="IMPORT_MANIFEST",
        from_status=batch.status,
        to_status=batch.status,
        comment=f"导入清单 v{new_version_number} ({detected_format.upper()}), 共 {len(items)} 条记录",
        extra_data={
            "version_number": new_version_number,
            "import_format": detected_format,
            "item_count": len(items),
            "warnings": [e.model_dump() for e in parse_errors if e not in critical_errors]
        }
    )
    db.add(log)
    db.commit()

    return ImportResponse(
        success=True,
        manifest_version_id=manifest_version.id,
        version_number=new_version_number,
        item_count=len(items),
        errors=parse_errors,
        message=f"清单 v{new_version_number} 导入成功，共 {len(items)} 条记录。"
                + (f" {len(parse_errors)} 条非阻塞性警告。" if parse_errors else "")
    )


@router.get("/{batch_id}/manifests", response_model=List[ManifestVersionResponse])
def list_manifest_versions(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    versions = db.query(ManifestVersion).filter(
        ManifestVersion.batch_id == batch_id
    ).order_by(ManifestVersion.version_number.asc()).all()
    return versions


@router.get("/{batch_id}/manifests/latest", response_model=ManifestVersionResponse)
def get_latest_manifest(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = get_batch_or_404(db, batch_id)
    if not batch.current_manifest_version_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No manifest imported for this batch yet"
        )
    version = db.query(ManifestVersion).filter(
        ManifestVersion.id == batch.current_manifest_version_id
    ).first()
    return version


@router.get("/{batch_id}/manifests/{version_id}", response_model=ManifestVersionResponse)
def get_manifest_version(
    batch_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    get_batch_or_404(db, batch_id)
    version = db.query(ManifestVersion).filter(
        ManifestVersion.id == version_id,
        ManifestVersion.batch_id == batch_id
    ).first()
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manifest version {version_id} not found for batch {batch_id}"
        )
    return version
