from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


BATCH_STATUS_DRAFT = "draft"
BATCH_STATUS_PENDING = "pending_review"
BATCH_STATUS_PARTIALLY_REJECTED = "partially_rejected"
BATCH_STATUS_REPAIRING = "repairing"
BATCH_STATUS_APPROVED = "approved"
BATCH_STATUS_ARCHIVED = "archived"

BATCH_STATUSES = [
    BATCH_STATUS_DRAFT,
    BATCH_STATUS_PENDING,
    BATCH_STATUS_PARTIALLY_REJECTED,
    BATCH_STATUS_REPAIRING,
    BATCH_STATUS_APPROVED,
    BATCH_STATUS_ARCHIVED,
]

VALID_STATUS_TRANSITIONS = {
    BATCH_STATUS_DRAFT: [BATCH_STATUS_PENDING],
    BATCH_STATUS_PENDING: [BATCH_STATUS_PARTIALLY_REJECTED, BATCH_STATUS_APPROVED, BATCH_STATUS_DRAFT],
    BATCH_STATUS_PARTIALLY_REJECTED: [BATCH_STATUS_REPAIRING, BATCH_STATUS_PENDING],
    BATCH_STATUS_REPAIRING: [BATCH_STATUS_PENDING, BATCH_STATUS_DRAFT],
    BATCH_STATUS_APPROVED: [BATCH_STATUS_ARCHIVED, BATCH_STATUS_PENDING],
    BATCH_STATUS_ARCHIVED: [],
}

ROLE_SUBMITTER = "submitter"
ROLE_REVIEWER = "reviewer"
ROLE_LEAD = "lead"
ROLE_ADMIN = "admin"

VALID_ROLES = [ROLE_SUBMITTER, ROLE_REVIEWER, ROLE_LEAD, ROLE_ADMIN]


class UserBase(BaseModel):
    username: str = Field(..., max_length=50)
    role: str = Field(..., max_length=20)
    display_name: Optional[str] = Field(None, max_length=100)


class UserCreate(UserBase):
    pass


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    display_name: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class DeliveryBatchBase(BaseModel):
    batch_code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=200)
    description: Optional[str] = None


class DeliveryBatchCreate(DeliveryBatchBase):
    submitter_id: int


class DeliveryBatchUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None


class DeliveryBatchResponse(BaseModel):
    id: int
    batch_code: str
    name: str
    description: Optional[str]
    status: str
    submitter_id: int
    current_manifest_version_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    archived_at: Optional[datetime]
    archived_by: Optional[int]

    class Config:
        from_attributes = True


class ManifestItemResponse(BaseModel):
    id: int
    manifest_version_id: int
    line_number: int
    item_key: str
    item_data: Dict[str, Any]

    class Config:
        from_attributes = True


class ManifestVersionResponse(BaseModel):
    id: int
    batch_id: int
    version_number: int
    import_format: str
    imported_by: int
    imported_at: datetime
    item_count: int
    validation_status: str
    validation_summary: Optional[Dict[str, Any]]
    items: List[ManifestItemResponse] = []

    class Config:
        from_attributes = True


class ValidationRuleBase(BaseModel):
    rule_code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    rule_type: str = Field(..., max_length=30)
    target_field: str = Field(..., max_length=100)
    rule_config: Optional[Dict[str, Any]] = None
    severity: str = Field("error", max_length=10)
    is_active: bool = True


class ValidationRuleCreate(ValidationRuleBase):
    pass


class ValidationRuleResponse(BaseModel):
    id: int
    rule_code: str
    name: str
    description: Optional[str]
    rule_type: str
    target_field: str
    rule_config: Optional[Dict[str, Any]]
    severity: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ValidationResultResponse(BaseModel):
    id: int
    manifest_version_id: int
    manifest_item_id: Optional[int]
    rule_code: str
    severity: str
    passed: bool
    message: str
    field_name: Optional[str]
    line_number: Optional[int]
    item_key: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class RejectionRecordCreate(BaseModel):
    manifest_item_id: Optional[int] = None
    item_key: Optional[str] = Field(None, max_length=100)
    line_number: Optional[int] = None
    rejection_reason: str


class BatchRejectionRequest(BaseModel):
    rejections: List[RejectionRecordCreate]
    comment: Optional[str] = None


class RejectionRecordResponse(BaseModel):
    id: int
    batch_id: int
    manifest_version_id: int
    manifest_item_id: Optional[int]
    rejector_id: int
    rejection_reason: str
    item_key: Optional[str]
    line_number: Optional[int]
    created_at: datetime
    resolved: bool
    resolved_at: Optional[datetime]
    resolved_by_manifest_version_id: Optional[int]

    class Config:
        from_attributes = True


class StatusTransitionRequest(BaseModel):
    target_status: str = Field(..., max_length=30)
    comment: Optional[str] = None


class ApprovalLogResponse(BaseModel):
    id: int
    batch_id: int
    manifest_version_id: Optional[int]
    actor_id: int
    action: str
    from_status: Optional[str]
    to_status: Optional[str]
    comment: Optional[str]
    created_at: datetime
    extra_data: Optional[Dict[str, Any]]

    class Config:
        from_attributes = True


class ImportValidationError(BaseModel):
    line_number: Optional[int]
    item_key: Optional[str]
    field_name: Optional[str]
    error_message: str


class ImportResponse(BaseModel):
    success: bool
    manifest_version_id: Optional[int] = None
    version_number: Optional[int] = None
    item_count: Optional[int] = None
    errors: List[ImportValidationError] = []
    message: str


class ValidationRunResponse(BaseModel):
    success: bool
    manifest_version_id: int
    total_rules: int
    total_checks: int
    passed: int
    failed: int
    warnings: int
    validation_summary: Dict[str, Any]
    results: List[ValidationResultResponse] = []
    message: str


class AcceptanceReportResponse(BaseModel):
    batch_id: int
    batch_code: str
    batch_name: str
    status: str
    submitter_id: int
    created_at: datetime
    approved_at: Optional[datetime]
    approved_by: Optional[int]
    total_versions: int
    current_version: int
    item_count: int
    total_rejections: int
    resolved_rejections: int
    validation_passed: bool
    validation_summary: Dict[str, Any]
    approval_logs: List[ApprovalLogResponse]
    rejection_history: List[RejectionRecordResponse]
    generated_at: datetime


PRECHECK_ACTION_NEW_VERSION = "NEW_VERSION"
PRECHECK_ACTION_REUSE_VERSION = "REUSE_VERSION"
PRECHECK_ACTION_CONFLICT = "CONFLICT"

PRECHECK_CONFLICT_STATUS = "STATUS_CONFLICT"
PRECHECK_CONFLICT_UNRESOLVED_REJECTIONS = "UNRESOLVED_REJECTIONS"


class PrecheckConflictDetail(BaseModel):
    conflict_type: str
    severity: str = "error"
    title: str
    description: str
    suggestion: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class ImportPrecheckResponse(BaseModel):
    success: bool
    precheck_token: Optional[str] = None
    batch_id: int
    action_type: str
    has_conflict: bool
    import_format: str
    item_count: int
    content_hash: str
    planned_version_number: Optional[int] = None
    reused_version_id: Optional[int] = None
    reused_version_number: Optional[int] = None
    conflicts: List[PrecheckConflictDetail] = []
    batch_status: str
    can_import: bool
    expires_at: Optional[datetime] = None
    reasons: List[str] = []
    message: str
    parse_errors: List[ImportValidationError] = []


class ImportPrecheckQueryResponse(BaseModel):
    id: int
    batch_id: int
    actor_id: int
    precheck_token: str
    content_hash: str
    import_format: str
    item_count: int
    action_type: str
    has_conflict: bool
    conflict_types: Optional[List[str]] = None
    conflict_details: Optional[List[Dict[str, Any]]] = None
    reused_version_id: Optional[int] = None
    reused_version_number: Optional[int] = None
    planned_version_number: Optional[int] = None
    created_at: datetime
    expires_at: datetime
    consumed: bool
    consumed_at: Optional[datetime] = None
    can_import: bool
    reasons: List[str] = []
    batch_status: Optional[str] = None


class ConfirmImportRequest(BaseModel):
    precheck_token: str = Field(..., max_length=64)
