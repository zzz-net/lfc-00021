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
