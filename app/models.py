import os
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

PRECHECK_ACTION_NEW_VERSION = "NEW_VERSION"
PRECHECK_ACTION_REUSE_VERSION = "REUSE_VERSION"
PRECHECK_ACTION_CONFLICT = "CONFLICT"

PRECHECK_CONFLICT_STATUS = "STATUS_CONFLICT"
PRECHECK_CONFLICT_UNRESOLVED_REJECTIONS = "UNRESOLVED_REJECTIONS"

PRECHECK_TOKEN_TTL_SECONDS = int(os.environ.get("PRECHECK_TOKEN_TTL_SECONDS", "1800"))


class ImportPrecheck(Base):
    __tablename__ = "import_prechecks"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("delivery_batches.id"), nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    precheck_token = Column(String(64), unique=True, nullable=False, index=True)
    content_hash = Column(String(64), nullable=False)
    import_format = Column(String(10), nullable=False)
    item_count = Column(Integer, nullable=False, default=0)
    action_type = Column(String(20), nullable=False)
    has_conflict = Column(Boolean, nullable=False, default=False)
    conflict_types = Column(JSON, nullable=True)
    conflict_details = Column(JSON, nullable=True)
    reused_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=True)
    reused_version_number = Column(Integer, nullable=True)
    planned_version_number = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed = Column(Boolean, nullable=False, default=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    extra_data = Column(JSON, nullable=True)

    batch = relationship("DeliveryBatch", foreign_keys=[batch_id])
    reused_version = relationship("ManifestVersion", foreign_keys=[reused_version_id])


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    role = Column(String(20), nullable=False)
    display_name = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    approval_logs = relationship("ApprovalLog", back_populates="actor")
    rejection_records = relationship("RejectionRecord", back_populates="rejector")


class DeliveryBatch(Base):
    __tablename__ = "delivery_batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(30), nullable=False, default="draft")
    submitter_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    current_manifest_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    archived_at = Column(DateTime(timezone=True), nullable=True)
    archived_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    manifest_versions = relationship("ManifestVersion", back_populates="batch", foreign_keys="ManifestVersion.batch_id", order_by="ManifestVersion.version_number")
    current_manifest_version = relationship("ManifestVersion", foreign_keys=[current_manifest_version_id], post_update=True)
    approval_logs = relationship("ApprovalLog", back_populates="batch", order_by="ApprovalLog.created_at")


class ManifestVersion(Base):
    __tablename__ = "manifest_versions"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("delivery_batches.id"), nullable=False)
    version_number = Column(Integer, nullable=False, default=1)
    import_format = Column(String(10), nullable=False)
    imported_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    imported_at = Column(DateTime(timezone=True), server_default=func.now())
    item_count = Column(Integer, nullable=False, default=0)
    raw_content = Column(Text, nullable=False)
    validation_status = Column(String(20), nullable=False, default="pending")
    validation_summary = Column(JSON, nullable=True)

    batch = relationship("DeliveryBatch", back_populates="manifest_versions", foreign_keys=[batch_id])
    items = relationship("ManifestItem", back_populates="manifest_version", cascade="all, delete-orphan")
    validation_results = relationship("ValidationResult", back_populates="manifest_version", cascade="all, delete-orphan")
    rejection_records = relationship("RejectionRecord", back_populates="manifest_version", foreign_keys="RejectionRecord.manifest_version_id")
    resolved_rejections = relationship("RejectionRecord", back_populates="resolved_by_manifest", foreign_keys="RejectionRecord.resolved_by_manifest_version_id")


class ManifestItem(Base):
    __tablename__ = "manifest_items"

    id = Column(Integer, primary_key=True, index=True)
    manifest_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=False)
    line_number = Column(Integer, nullable=False)
    item_key = Column(String(100), nullable=False, index=True)
    item_data = Column(JSON, nullable=False)

    manifest_version = relationship("ManifestVersion", back_populates="items")
    validation_results = relationship("ValidationResult", back_populates="item", cascade="all, delete-orphan")
    rejection_records = relationship("RejectionRecord", back_populates="item")


class ValidationRule(Base):
    __tablename__ = "validation_rules"

    id = Column(Integer, primary_key=True, index=True)
    rule_code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    rule_type = Column(String(30), nullable=False)
    target_field = Column(String(100), nullable=False)
    rule_config = Column(JSON, nullable=True)
    severity = Column(String(10), nullable=False, default="error")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id = Column(Integer, primary_key=True, index=True)
    manifest_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=False)
    manifest_item_id = Column(Integer, ForeignKey("manifest_items.id"), nullable=True)
    rule_id = Column(Integer, ForeignKey("validation_rules.id"), nullable=True)
    rule_code = Column(String(50), nullable=False)
    severity = Column(String(10), nullable=False)
    passed = Column(Boolean, nullable=False)
    message = Column(Text, nullable=False)
    field_name = Column(String(100), nullable=True)
    line_number = Column(Integer, nullable=True)
    item_key = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    manifest_version = relationship("ManifestVersion", back_populates="validation_results")
    item = relationship("ManifestItem", back_populates="validation_results")


class RejectionRecord(Base):
    __tablename__ = "rejection_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("delivery_batches.id"), nullable=False)
    manifest_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=False)
    manifest_item_id = Column(Integer, ForeignKey("manifest_items.id"), nullable=True)
    rejector_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rejection_reason = Column(Text, nullable=False)
    item_key = Column(String(100), nullable=True)
    line_number = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved = Column(Boolean, nullable=False, default=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_manifest_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=True)

    manifest_version = relationship("ManifestVersion", back_populates="rejection_records", foreign_keys=[manifest_version_id])
    resolved_by_manifest = relationship("ManifestVersion", back_populates="resolved_rejections", foreign_keys=[resolved_by_manifest_version_id])
    item = relationship("ManifestItem", back_populates="rejection_records")
    rejector = relationship("User", back_populates="rejection_records")


class ApprovalLog(Base):
    __tablename__ = "approval_logs"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("delivery_batches.id"), nullable=False)
    manifest_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(30), nullable=False)
    from_status = Column(String(30), nullable=True)
    to_status = Column(String(30), nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    extra_data = Column(JSON, nullable=True)

    batch = relationship("DeliveryBatch", back_populates="approval_logs")
    actor = relationship("User", back_populates="approval_logs")


SNAPSHOT_VALID = "valid"
SNAPSHOT_INVALID = "invalid"
SNAPSHOT_SUPERSEDED = "superseded"


class VersionDiffSnapshot(Base):
    __tablename__ = "version_diff_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("delivery_batches.id"), nullable=False, index=True)
    old_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=False, index=True)
    new_version_id = Column(Integer, ForeignKey("manifest_versions.id"), nullable=False, index=True)
    old_version_number = Column(Integer, nullable=False)
    new_version_number = Column(Integer, nullable=False)
    snapshot_key = Column(String(128), unique=True, nullable=False, index=True)
    status = Column(String(20), nullable=False, default=SNAPSHOT_VALID)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    invalidated_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    metadata_json = Column(JSON, nullable=False)
    summary_json = Column(JSON, nullable=False)
    added_items_json = Column(JSON, nullable=False, default=list)
    removed_items_json = Column(JSON, nullable=False, default=list)
    modified_items_json = Column(JSON, nullable=False, default=list)
    unchanged_items_json = Column(JSON, nullable=False, default=list)
    unresolved_rejections_json = Column(JSON, nullable=False, default=list)
    validation_changes_json = Column(JSON, nullable=False, default=list)

    content_hash = Column(String(64), nullable=False)

    batch = relationship("DeliveryBatch", foreign_keys=[batch_id])
    old_version = relationship("ManifestVersion", foreign_keys=[old_version_id])
    new_version = relationship("ManifestVersion", foreign_keys=[new_version_id])
    creator = relationship("User", foreign_keys=[created_by])
    invalidator = relationship("User", foreign_keys=[invalidated_by])


CONFIG_KEY_ARCHIVE_ALLOW_OVERWRITE = "archive.allow_overwrite_existing_batch"
CONFIG_KEY_ARCHIVE_ENABLED = "archive.enabled"
CONFIG_KEY_ARCHIVE_ROLE_REQUIRED = "archive.role_required"

CONFIG_KEY_SANDBOX_ENABLED = "sandbox.enabled"
CONFIG_KEY_SANDBOX_REQUIRE_ADMIN_CONFIRM = "sandbox.require_admin_confirm"
CONFIG_KEY_SANDBOX_AUTO_EXPIRE_HOURS = "sandbox.auto_expire_hours"

SANDBOX_STATUS_PENDING = "pending"
SANDBOX_STATUS_PRECHECK_RUNNING = "precheck_running"
SANDBOX_STATUS_PRECHECK_PASSED = "precheck_passed"
SANDBOX_STATUS_PRECHECK_FAILED = "precheck_failed"
SANDBOX_STATUS_CONFIRMED = "confirmed"
SANDBOX_STATUS_REJECTED = "rejected"
SANDBOX_STATUS_EXPIRED = "expired"

SANDBOX_PRECHECK_PASS = "PASS"
SANDBOX_PRECHECK_WARNING = "WARNING"
SANDBOX_PRECHECK_FAIL = "FAIL"

SANDBOX_ACTION_RECOMMEND_APPROVE = "APPROVE"
SANDBOX_ACTION_RECOMMEND_REJECT = "REJECT"
SANDBOX_ACTION_RECOMMEND_REPAIR = "REPAIR"
SANDBOX_ACTION_RECOMMEND_MANUAL = "MANUAL_REVIEW"

APPROVAL_LOG_ACTION_SANDBOX_RESTORE = "SANDBOX_RESTORE"
APPROVAL_LOG_ACTION_SANDBOX_IMPORT = "SANDBOX_IMPORT"
APPROVAL_LOG_ACTION_SANDBOX_PRECHECK = "SANDBOX_PRECHECK"
APPROVAL_LOG_ACTION_SANDBOX_VIEW_DIFF = "SANDBOX_VIEW_DIFF"
APPROVAL_LOG_ACTION_SANDBOX_CONFIRM = "SANDBOX_CONFIRM"
APPROVAL_LOG_ACTION_SANDBOX_REJECT = "SANDBOX_REJECT"
APPROVAL_LOG_ACTION_SANDBOX_CLEANUP = "SANDBOX_CLEANUP"
APPROVAL_LOG_ACTION_SANDBOX_CONFIG_UPDATE = "SANDBOX_CONFIG_UPDATE"
APPROVAL_LOG_ACTION_SANDBOX_CONFIG_BATCH_UPDATE = "SANDBOX_CONFIG_BATCH_UPDATE"
APPROVAL_LOG_ACTION_SANDBOX_CONFIG_VIEW = "SANDBOX_CONFIG_VIEW"

APPROVAL_LOG_ACTION_EXPORT_ARCHIVE = "EXPORT_ARCHIVE"
APPROVAL_LOG_ACTION_TRY_IMPORT_ARCHIVE = "TRY_IMPORT_ARCHIVE"
APPROVAL_LOG_ACTION_RESTORE_ARCHIVE = "RESTORE_ARCHIVE"
APPROVAL_LOG_ACTION_RESTORE_ARCHIVE_OVERWRITE = "RESTORE_ARCHIVE_OVERWRITE"


class SystemConfig(Base):
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String(100), unique=True, nullable=False, index=True)
    config_value = Column(Text, nullable=False)
    value_type = Column(String(20), nullable=False, default="string")
    description = Column(String(500), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SandboxSession(Base):
    __tablename__ = "sandbox_sessions"

    id = Column(Integer, primary_key=True, index=True)
    sandbox_token = Column(String(64), unique=True, nullable=False, index=True)
    source_archive_id = Column(String(64), nullable=False, index=True)
    source_batch_code = Column(String(50), nullable=False, index=True)
    original_batch_id = Column(Integer, nullable=True)
    target_batch_id = Column(Integer, ForeignKey("delivery_batches.id"), nullable=True)
    status = Column(String(30), nullable=False, default=SANDBOX_STATUS_PENDING)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    confirmed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    rejected_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    precheck_result = Column(JSON, nullable=True)
    precheck_passed = Column(Boolean, nullable=True)
    recommended_action = Column(String(30), nullable=True)
    conflict_types = Column(JSON, nullable=True)
    extra_data = Column(JSON, nullable=True)

    target_batch = relationship("DeliveryBatch", foreign_keys=[target_batch_id])
    creator = relationship("User", foreign_keys=[created_by])
    confirmer = relationship("User", foreign_keys=[confirmed_by])
    rejector = relationship("User", foreign_keys=[rejected_by])
    manifest_versions = relationship("SandboxManifestVersion", back_populates="sandbox_session", cascade="all, delete-orphan")


class SandboxManifestVersion(Base):
    __tablename__ = "sandbox_manifest_versions"

    id = Column(Integer, primary_key=True, index=True)
    sandbox_session_id = Column(Integer, ForeignKey("sandbox_sessions.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False, default=1)
    import_format = Column(String(10), nullable=False)
    imported_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    imported_at = Column(DateTime(timezone=True), server_default=func.now())
    item_count = Column(Integer, nullable=False, default=0)
    raw_content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    validation_status = Column(String(20), nullable=False, default="pending")
    validation_summary = Column(JSON, nullable=True)
    is_candidate = Column(Boolean, nullable=False, default=False)
    base_version_number = Column(Integer, nullable=True)
    extra_data = Column(JSON, nullable=True)

    sandbox_session = relationship("SandboxSession", back_populates="manifest_versions")
    items = relationship("SandboxManifestItem", back_populates="manifest_version", cascade="all, delete-orphan")
    import_user = relationship("User", foreign_keys=[imported_by])


class SandboxManifestItem(Base):
    __tablename__ = "sandbox_manifest_items"

    id = Column(Integer, primary_key=True, index=True)
    sandbox_manifest_version_id = Column(Integer, ForeignKey("sandbox_manifest_versions.id"), nullable=False, index=True)
    line_number = Column(Integer, nullable=False)
    item_key = Column(String(100), nullable=False, index=True)
    item_data = Column(JSON, nullable=False)

    manifest_version = relationship("SandboxManifestVersion", back_populates="items")


class SandboxPrecheckResult(Base):
    __tablename__ = "sandbox_precheck_results"

    id = Column(Integer, primary_key=True, index=True)
    sandbox_session_id = Column(Integer, ForeignKey("sandbox_sessions.id"), nullable=False, index=True)
    check_code = Column(String(50), nullable=False)
    check_name = Column(String(200), nullable=False)
    severity = Column(String(10), nullable=False)
    passed = Column(Boolean, nullable=False)
    message = Column(Text, nullable=False)
    suggestion = Column(Text, nullable=True)
    details = Column(JSON, nullable=True)
    affected_version_number = Column(Integer, nullable=True)
    affected_item_key = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sandbox_session = relationship("SandboxSession")
