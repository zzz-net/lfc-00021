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


DIFF_ACTION_ADDED = "added"
DIFF_ACTION_REMOVED = "removed"
DIFF_ACTION_MODIFIED = "modified"
DIFF_ACTION_UNCHANGED = "unchanged"

VALID_DIFF_ACTIONS = [DIFF_ACTION_ADDED, DIFF_ACTION_REMOVED, DIFF_ACTION_MODIFIED, DIFF_ACTION_UNCHANGED]

APPROVAL_LOG_ACTION_VIEW_DIFF = "VIEW_VERSION_DIFF"
APPROVAL_LOG_ACTION_EXPORT_DIFF = "EXPORT_VERSION_DIFF"


class FieldChange(BaseModel):
    field_name: str
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    change_type: str


class ItemDiff(BaseModel):
    item_key: str
    action: str
    line_number_old: Optional[int] = None
    line_number_new: Optional[int] = None
    old_data: Optional[Dict[str, Any]] = None
    new_data: Optional[Dict[str, Any]] = None
    field_changes: List[FieldChange] = []


class ItemDiffSummary(BaseModel):
    item_key: str
    action: str
    change_summary: str
    changed_fields: List[str] = []


class RejectionInfo(BaseModel):
    id: int
    item_key: Optional[str] = None
    line_number: Optional[int] = None
    rejection_reason: str
    rejector_username: str
    rejector_display_name: Optional[str] = None
    created_at: datetime
    resolved: bool
    resolved_at: Optional[datetime] = None


VALIDATION_CHANGE_NEW_VIOLATION = "new_violation"
VALIDATION_CHANGE_RESOLVED = "resolved"
VALIDATION_CHANGE_MODIFIED = "modified"
VALIDATION_CHANGE_NEW_PASSED = "new_passed"
VALIDATION_CHANGE_REMOVED_PASSED = "removed_passed"
VALIDATION_CHANGE_UNCHANGED = "unchanged"

VALID_VALIDATION_CHANGE_TYPES = [
    VALIDATION_CHANGE_NEW_VIOLATION,
    VALIDATION_CHANGE_RESOLVED,
    VALIDATION_CHANGE_MODIFIED,
    VALIDATION_CHANGE_NEW_PASSED,
    VALIDATION_CHANGE_REMOVED_PASSED,
    VALIDATION_CHANGE_UNCHANGED,
]


class ValidationChange(BaseModel):
    item_key: Optional[str] = None
    field_name: Optional[str] = None
    rule_code: str
    old_severity: Optional[str] = None
    new_severity: Optional[str] = None
    old_passed: Optional[bool] = None
    new_passed: Optional[bool] = None
    old_message: Optional[str] = None
    new_message: Optional[str] = None
    change_type: str


class ImportInfo(BaseModel):
    version_number: int
    imported_by_username: str
    imported_by_display_name: Optional[str] = None
    imported_at: datetime
    item_count: int
    import_format: str


class VersionDiffMetadata(BaseModel):
    batch_id: int
    batch_code: str
    batch_name: str
    old_version: int
    new_version: int
    old_import: ImportInfo
    new_import: ImportInfo
    generated_at: datetime
    generated_by_username: str
    generated_by_display_name: Optional[str] = None


class VersionDiffSummary(BaseModel):
    total_items_old: int
    total_items_new: int
    added_count: int
    removed_count: int
    modified_count: int
    unchanged_count: int
    field_change_count: int
    unresolved_rejections_old: int
    unresolved_rejections_new: int
    validation_errors_old: int
    validation_errors_new: int
    validation_warnings_old: int
    validation_warnings_new: int
    validation_passed_old: int
    validation_passed_new: int
    validation_total_old: int
    validation_total_new: int
    validation_changes_new_violation: int
    validation_changes_resolved: int
    validation_changes_modified: int
    validation_changes_new_passed: int
    validation_changes_removed_passed: int
    validation_changes_unchanged: int
    validation_changes_total: int


class VersionDiffResponse(BaseModel):
    metadata: VersionDiffMetadata
    summary: VersionDiffSummary
    added_items: List[ItemDiff] = []
    removed_items: List[ItemDiff] = []
    modified_items: List[ItemDiff] = []
    unchanged_items: List[ItemDiffSummary] = []
    unresolved_rejections: List[RejectionInfo] = []
    validation_changes: List[ValidationChange] = []


class VersionDiffExportResponse(BaseModel):
    export_id: str
    export_timestamp: datetime
    exported_by: str
    diff_data: VersionDiffResponse


SNAPSHOT_VALID = "valid"
SNAPSHOT_INVALID = "invalid"
SNAPSHOT_SUPERSEDED = "superseded"
VALID_SNAPSHOT_STATUSES = [SNAPSHOT_VALID, SNAPSHOT_INVALID, SNAPSHOT_SUPERSEDED]

APPROVAL_LOG_ACTION_CREATE_SNAPSHOT = "CREATE_DIFF_SNAPSHOT"
APPROVAL_LOG_ACTION_QUERY_SNAPSHOT = "QUERY_DIFF_SNAPSHOT"
APPROVAL_LOG_ACTION_EXPORT_SNAPSHOT_CSV = "EXPORT_DIFF_SNAPSHOT_CSV"

DEFAULT_EXPORT_FORMAT = "json"
VALID_EXPORT_FORMATS = ["json", "csv"]
SNAPSHOT_DEFAULT_LIMIT = 50
SNAPSHOT_MAX_LIMIT = 200


class VersionDiffSnapshotResponse(BaseModel):
    id: int
    batch_id: int
    old_version_id: int
    new_version_id: int
    old_version_number: int
    new_version_number: int
    snapshot_key: str
    status: str
    created_by: int
    created_at: datetime
    invalidated_at: Optional[datetime] = None
    invalidated_by: Optional[int] = None
    content_hash: str
    metadata: Dict[str, Any]
    summary: Dict[str, Any]
    has_added: bool
    has_removed: bool
    has_modified: bool
    has_unresolved_rejections: bool
    has_validation_changes: bool

    class Config:
        from_attributes = True


class VersionDiffSnapshotDetailResponse(VersionDiffSnapshotResponse):
    added_items: List[ItemDiff] = []
    removed_items: List[ItemDiff] = []
    modified_items: List[ItemDiff] = []
    unchanged_items: List[ItemDiffSummary] = []
    unresolved_rejections: List[RejectionInfo] = []
    validation_changes: List[ValidationChange] = []


class SnapshotListResponse(BaseModel):
    batch_id: int
    batch_code: str
    total: int
    snapshots: List[VersionDiffSnapshotResponse] = []
