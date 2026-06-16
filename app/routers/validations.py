from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models import User, ValidationRule, DeliveryBatch, ManifestVersion
from app.schemas import (
    ValidationRuleCreate, ValidationRuleResponse,
    ValidationRunResponse, ValidationResultResponse
)
from app.dependencies import get_current_user, require_admin, get_batch_or_404
from app.validation_engine import ValidationEngine

router = APIRouter(prefix="/api", tags=["校验规则与执行"])


@router.post("/validation-rules", response_model=ValidationRuleResponse, status_code=status.HTTP_201_CREATED)
def create_validation_rule(
    rule_data: ValidationRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    existing = db.query(ValidationRule).filter(
        ValidationRule.rule_code == rule_data.rule_code
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Validation rule with code '{rule_data.rule_code}' already exists"
        )
    rule = ValidationRule(**rule_data.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/validation-rules", response_model=List[ValidationRuleResponse])
def list_validation_rules(
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(ValidationRule)
    if is_active is not None:
        query = query.filter(ValidationRule.is_active == is_active)
    return query.order_by(ValidationRule.rule_code.asc()).all()


@router.get("/validation-rules/{rule_id}", response_model=ValidationRuleResponse)
def get_validation_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    rule = db.query(ValidationRule).filter(ValidationRule.id == rule_id).first()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Validation rule with id {rule_id} not found"
        )
    return rule


@router.patch("/validation-rules/{rule_id}", response_model=ValidationRuleResponse)
def update_validation_rule(
    rule_id: int,
    rule_data: ValidationRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    rule = db.query(ValidationRule).filter(ValidationRule.id == rule_id).first()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Validation rule with id {rule_id} not found"
        )
    update_dict = rule_data.model_dump()
    for key, value in update_dict.items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/batches/{batch_id}/validate", response_model=ValidationRunResponse)
def run_validation(
    batch_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = get_batch_or_404(db, batch_id)
    if not batch.current_manifest_version_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No manifest imported for this batch. Please import manifest first."
        )

    engine = ValidationEngine(db)
    try:
        result = engine.run_validation(batch.current_manifest_version_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    from app.diff_engine import refresh_snapshots_for_batch
    refreshed = refresh_snapshots_for_batch(db, batch_id, current_user)

    summary = result["summary"]
    result_models = result["results"]

    return ValidationRunResponse(
        success=True,
        manifest_version_id=batch.current_manifest_version_id,
        total_rules=result["total_rules"],
        total_checks=summary["total_checks"],
        passed=summary["passed"],
        failed=summary["failed"],
        warnings=summary["warnings"],
        validation_summary=summary,
        results=result_models,
        message=f"校验完成: {summary['passed']} 通过, {summary['failed']} 错误, {summary['warnings']} 警告"
    )


@router.get("/batches/{batch_id}/validation-results", response_model=List[ValidationResultResponse])
def get_validation_results(
    batch_id: int,
    only_failed: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    batch = get_batch_or_404(db, batch_id)
    if not batch.current_manifest_version_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No manifest imported for this batch"
        )
    query = db.query(__import__("app.models", fromlist=["ValidationResult"]).ValidationResult).filter(
        __import__("app.models", fromlist=["ValidationResult"]).ValidationResult.manifest_version_id == batch.current_manifest_version_id
    )
    if only_failed:
        query = query.filter(__import__("app.models", fromlist=["ValidationResult"]).ValidationResult.passed == False)
    return query.order_by(
        __import__("app.models", fromlist=["ValidationResult"]).ValidationResult.line_number.asc(),
        __import__("app.models", fromlist=["ValidationResult"]).ValidationResult.rule_code.asc()
    ).all()


@router.get("/manifests/{manifest_version_id}/validation-results", response_model=List[ValidationResultResponse])
def get_manifest_validation_results(
    manifest_version_id: int,
    only_failed: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    version = db.query(ManifestVersion).filter(ManifestVersion.id == manifest_version_id).first()
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manifest version {manifest_version_id} not found"
        )
    from app.models import ValidationResult
    query = db.query(ValidationResult).filter(ValidationResult.manifest_version_id == manifest_version_id)
    if only_failed:
        query = query.filter(ValidationResult.passed == False)
    return query.order_by(ValidationResult.line_number.asc(), ValidationResult.rule_code.asc()).all()
