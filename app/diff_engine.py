from sqlalchemy.orm import Session
from typing import List, Optional, Tuple
import hashlib
import json
from datetime import datetime

from app.models import (
    User, DeliveryBatch, ManifestVersion, ManifestItem,
    RejectionRecord, ValidationResult, VersionDiffSnapshot,
    SNAPSHOT_VALID, SNAPSHOT_SUPERSEDED
)
from app.schemas import (
    VersionDiffResponse, VersionDiffMetadata, VersionDiffSummary,
    ItemDiff, ItemDiffSummary, FieldChange,
    RejectionInfo, ValidationChange, ImportInfo,
    DIFF_ACTION_ADDED, DIFF_ACTION_REMOVED,
    DIFF_ACTION_MODIFIED, DIFF_ACTION_UNCHANGED,
    VALIDATION_CHANGE_NEW_VIOLATION, VALIDATION_CHANGE_RESOLVED,
    VALIDATION_CHANGE_MODIFIED, VALIDATION_CHANGE_NEW_PASSED,
    VALIDATION_CHANGE_REMOVED_PASSED, VALIDATION_CHANGE_UNCHANGED
)
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# MODULE LOAD 标记 —— 服务启动时必打，用于确认代码版本
# ═══════════════════════════════════════════════════════════════════════════════
logger.warning(
    "[MODULE_LOAD] diff_engine.py loaded. Key features: "
    "schema_stale_check=YES, "
    "save_diff_snapshot_summary_fields_log=YES, "
    "refresh_snapshot_schema_upgrade_force_rewrite=YES, "
    "validation_status_none_shortcut_fixed=YES"
)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _get_user_info(db: Session, user_id: int) -> tuple:
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        return user.username, user.display_name
    return "unknown", None


def _build_import_info(db: Session, version: ManifestVersion) -> ImportInfo:
    username, display_name = _get_user_info(db, version.imported_by)
    return ImportInfo(
        version_number=version.version_number,
        imported_by_username=username,
        imported_by_display_name=display_name,
        imported_at=version.imported_at,
        item_count=version.item_count,
        import_format=version.import_format
    )


def _compare_fields(old_data: dict, new_data: dict) -> List[FieldChange]:
    changes = []
    all_fields = sorted(set(list(old_data.keys()) + list(new_data.keys())))
    for field in all_fields:
        old_val = old_data.get(field)
        new_val = new_data.get(field)
        if old_val != new_val:
            if field not in old_data:
                change_type = "added"
            elif field not in new_data:
                change_type = "removed"
            else:
                change_type = "modified"
            changes.append(FieldChange(
                field_name=field,
                old_value=old_val,
                new_value=new_val,
                change_type=change_type
            ))
    return changes


def _calculate_item_diffs(
    old_items: List[ManifestItem],
    new_items: List[ManifestItem]
) -> tuple:
    old_map = {item.item_key: item for item in old_items}
    new_map = {item.item_key: item for item in new_items}

    added = []
    removed = []
    modified = []
    unchanged = []

    all_keys = sorted(set(list(old_map.keys()) + list(new_map.keys())))

    for key in all_keys:
        old_item = old_map.get(key)
        new_item = new_map.get(key)

        if old_item is None:
            added.append(ItemDiff(
                item_key=key,
                action=DIFF_ACTION_ADDED,
                line_number_new=new_item.line_number,
                new_data=new_item.item_data,
                field_changes=[
                    FieldChange(
                        field_name=k,
                        new_value=v,
                        change_type="added"
                    )
                    for k, v in sorted(new_item.item_data.items())
                ]
            ))
        elif new_item is None:
            removed.append(ItemDiff(
                item_key=key,
                action=DIFF_ACTION_REMOVED,
                line_number_old=old_item.line_number,
                old_data=old_item.item_data,
                field_changes=[
                    FieldChange(
                        field_name=k,
                        old_value=v,
                        change_type="removed"
                    )
                    for k, v in sorted(old_item.item_data.items())
                ]
            ))
        else:
            field_changes = _compare_fields(old_item.item_data, new_item.item_data)
            if field_changes:
                modified.append(ItemDiff(
                    item_key=key,
                    action=DIFF_ACTION_MODIFIED,
                    line_number_old=old_item.line_number,
                    line_number_new=new_item.line_number,
                    old_data=old_item.item_data,
                    new_data=new_item.item_data,
                    field_changes=field_changes
                ))
            else:
                unchanged.append(ItemDiffSummary(
                    item_key=key,
                    action=DIFF_ACTION_UNCHANGED,
                    change_summary="内容无变更",
                    changed_fields=[]
                ))

    return added, removed, modified, unchanged


def _collect_unresolved_rejections(
    db: Session,
    batch_id: int,
    old_version_id: int,
    new_version_id: int
) -> List[RejectionInfo]:
    rejections = db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id,
        RejectionRecord.resolved == False
    ).order_by(RejectionRecord.created_at.asc()).all()

    result = []
    for r in rejections:
        username, display_name = _get_user_info(db, r.rejector_id)
        result.append(RejectionInfo(
            id=r.id,
            item_key=r.item_key,
            line_number=r.line_number,
            rejection_reason=r.rejection_reason,
            rejector_username=username,
            rejector_display_name=display_name,
            created_at=r.created_at,
            resolved=r.resolved,
            resolved_at=r.resolved_at
        ))
    return result


def _collect_validation_changes(
    db: Session,
    old_version_id: int,
    new_version_id: int
) -> List[ValidationChange]:
    old_results = db.query(ValidationResult).filter(
        ValidationResult.manifest_version_id == old_version_id
    ).all()
    new_results = db.query(ValidationResult).filter(
        ValidationResult.manifest_version_id == new_version_id
    ).all()

    def key(r):
        return (r.item_key or "", r.rule_code, r.field_name or "")

    old_map = {key(r): r for r in old_results}
    new_map = {key(r): r for r in new_results}

    changes = []
    all_keys = sorted(set(list(old_map.keys()) + list(new_map.keys())))

    new_violation_count = 0
    resolved_count = 0
    modified_count = 0
    new_passed_count = 0
    removed_passed_count = 0
    unchanged_count = 0

    for k in all_keys:
        old_r = old_map.get(k)
        new_r = new_map.get(k)

        if old_r is None:
            if not new_r.passed:
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    new_severity=new_r.severity,
                    new_passed=new_r.passed,
                    new_message=new_r.message,
                    change_type=VALIDATION_CHANGE_NEW_VIOLATION
                ))
                new_violation_count += 1
            else:
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    new_severity=new_r.severity,
                    new_passed=new_r.passed,
                    new_message=new_r.message,
                    change_type=VALIDATION_CHANGE_NEW_PASSED
                ))
                new_passed_count += 1
        elif new_r is None:
            if not old_r.passed:
                changes.append(ValidationChange(
                    item_key=old_r.item_key,
                    field_name=old_r.field_name,
                    rule_code=old_r.rule_code,
                    old_severity=old_r.severity,
                    old_passed=old_r.passed,
                    old_message=old_r.message,
                    change_type=VALIDATION_CHANGE_RESOLVED
                ))
                resolved_count += 1
            else:
                changes.append(ValidationChange(
                    item_key=old_r.item_key,
                    field_name=old_r.field_name,
                    rule_code=old_r.rule_code,
                    old_severity=old_r.severity,
                    old_passed=old_r.passed,
                    old_message=old_r.message,
                    change_type=VALIDATION_CHANGE_REMOVED_PASSED
                ))
                removed_passed_count += 1
        else:
            old_failed = not old_r.passed
            new_failed = not new_r.passed
            if old_failed and not new_failed:
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    old_severity=old_r.severity,
                    new_severity=new_r.severity,
                    old_passed=old_r.passed,
                    new_passed=new_r.passed,
                    old_message=old_r.message,
                    new_message=new_r.message,
                    change_type=VALIDATION_CHANGE_RESOLVED
                ))
                resolved_count += 1
            elif not old_failed and new_failed:
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    old_severity=old_r.severity,
                    new_severity=new_r.severity,
                    old_passed=old_r.passed,
                    new_passed=new_r.passed,
                    old_message=old_r.message,
                    new_message=new_r.message,
                    change_type=VALIDATION_CHANGE_NEW_VIOLATION
                ))
                new_violation_count += 1
            elif old_failed and new_failed and (
                old_r.severity != new_r.severity or
                old_r.message != new_r.message
            ):
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    old_severity=old_r.severity,
                    new_severity=new_r.severity,
                    old_passed=old_r.passed,
                    new_passed=new_r.passed,
                    old_message=old_r.message,
                    new_message=new_r.message,
                    change_type=VALIDATION_CHANGE_MODIFIED
                ))
                modified_count += 1
            else:
                changes.append(ValidationChange(
                    item_key=new_r.item_key,
                    field_name=new_r.field_name,
                    rule_code=new_r.rule_code,
                    old_severity=old_r.severity,
                    new_severity=new_r.severity,
                    old_passed=old_r.passed,
                    new_passed=new_r.passed,
                    old_message=old_r.message,
                    new_message=new_r.message,
                    change_type=VALIDATION_CHANGE_UNCHANGED
                ))
                unchanged_count += 1

    logger.debug(
        f"Validation changes computed: old_version=%s, new_version=%s, "
        "new_violation=%d, resolved=%d, modified=%d, "
        "new_passed=%d, removed_passed=%d, unchanged=%d, total=%d",
        old_version_id, new_version_id,
        new_violation_count, resolved_count, modified_count,
        new_passed_count, removed_passed_count, unchanged_count,
        len(changes)
    )

    return changes


def _count_validation_issues(db: Session, version_id: int) -> tuple:
    results = db.query(ValidationResult).filter(
        ValidationResult.manifest_version_id == version_id
    ).all()
    errors = sum(1 for r in results if not r.passed and r.severity == "error")
    warnings = sum(1 for r in results if not r.passed and r.severity == "warning")
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    return errors, warnings, passed, total


def _count_unresolved_rejections(db: Session, batch_id: int, version_id: int) -> int:
    return db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id,
        RejectionRecord.manifest_version_id == version_id,
        RejectionRecord.resolved == False
    ).count()


def _count_validation_change_types(changes: List[ValidationChange]) -> dict:
    counts = {
        "new_violation": 0,
        "resolved": 0,
        "modified": 0,
        "new_passed": 0,
        "removed_passed": 0,
        "unchanged": 0,
    }
    for c in changes:
        ct = c.change_type
        if ct in counts:
            counts[ct] += 1
    counts["total"] = len(changes)
    return counts


def calculate_version_diff(
    db: Session,
    batch: DeliveryBatch,
    old_version: ManifestVersion,
    new_version: ManifestVersion,
    current_user: User
) -> VersionDiffResponse:
    old_items = sorted(old_version.items, key=lambda x: x.item_key)
    new_items = sorted(new_version.items, key=lambda x: x.item_key)

    added, removed, modified, unchanged = _calculate_item_diffs(old_items, new_items)

    total_field_changes = sum(len(m.field_changes) for m in modified)
    total_field_changes += sum(len(a.field_changes) for a in added)
    total_field_changes += sum(len(r.field_changes) for r in removed)

    unresolved_old = _count_unresolved_rejections(db, batch.id, old_version.id)
    unresolved_new = _count_unresolved_rejections(db, batch.id, new_version.id)

    val_errors_old, val_warnings_old, val_passed_old, val_total_old = _count_validation_issues(db, old_version.id)
    val_errors_new, val_warnings_new, val_passed_new, val_total_new = _count_validation_issues(db, new_version.id)

    unresolved_rejections = _collect_unresolved_rejections(
        db, batch.id, old_version.id, new_version.id
    )
    validation_changes = _collect_validation_changes(db, old_version.id, new_version.id)

    val_change_counts = _count_validation_change_types(validation_changes)

    username, display_name = _get_user_info(db, current_user.id)

    metadata = VersionDiffMetadata(
        batch_id=batch.id,
        batch_code=batch.batch_code,
        batch_name=batch.name,
        old_version=old_version.version_number,
        new_version=new_version.version_number,
        old_import=_build_import_info(db, old_version),
        new_import=_build_import_info(db, new_version),
        generated_at=datetime.now(),
        generated_by_username=username,
        generated_by_display_name=display_name
    )

    summary = VersionDiffSummary(
        total_items_old=len(old_items),
        total_items_new=len(new_items),
        added_count=len(added),
        removed_count=len(removed),
        modified_count=len(modified),
        unchanged_count=len(unchanged),
        field_change_count=total_field_changes,
        unresolved_rejections_old=unresolved_old,
        unresolved_rejections_new=unresolved_new,
        validation_errors_old=val_errors_old,
        validation_errors_new=val_errors_new,
        validation_warnings_old=val_warnings_old,
        validation_warnings_new=val_warnings_new,
        validation_passed_old=val_passed_old,
        validation_passed_new=val_passed_new,
        validation_total_old=val_total_old,
        validation_total_new=val_total_new,
        validation_changes_new_violation=val_change_counts["new_violation"],
        validation_changes_resolved=val_change_counts["resolved"],
        validation_changes_modified=val_change_counts["modified"],
        validation_changes_new_passed=val_change_counts["new_passed"],
        validation_changes_removed_passed=val_change_counts["removed_passed"],
        validation_changes_unchanged=val_change_counts["unchanged"],
        validation_changes_total=val_change_counts["total"],
        old_version_validation_status=old_version.validation_status,
        new_version_validation_status=new_version.validation_status,
    )

    logger.info(
        "Version diff calculated: batch_id=%s, v%d -> v%d, "
        "added=%d, removed=%d, modified=%d, unchanged=%d, "
        "validation_changes=%d (new_violation=%d, resolved=%d, modified=%d, "
        "new_passed=%d, removed_passed=%d, unchanged=%d)",
        batch.id, old_version.version_number, new_version.version_number,
        len(added), len(removed), len(modified), len(unchanged),
        val_change_counts["total"],
        val_change_counts["new_violation"], val_change_counts["resolved"],
        val_change_counts["modified"], val_change_counts["new_passed"],
        val_change_counts["removed_passed"], val_change_counts["unchanged"],
    )

    return VersionDiffResponse(
        metadata=metadata,
        summary=summary,
        added_items=added,
        removed_items=removed,
        modified_items=modified,
        unchanged_items=unchanged,
        unresolved_rejections=unresolved_rejections,
        validation_changes=validation_changes
    )


def _build_snapshot_key(batch_id: int, old_vn: int, new_vn: int) -> str:
    return f"batch_{batch_id}_v{old_vn}_to_v{new_vn}"


def _compute_content_hash(diff: VersionDiffResponse) -> str:
    canonical = json.dumps(diff.model_dump(mode='json'), sort_keys=True, default=str, ensure_ascii=False)
    return _sha256_hex(canonical)


def save_diff_snapshot(
    db: Session,
    batch: DeliveryBatch,
    old_version: ManifestVersion,
    new_version: ManifestVersion,
    diff: VersionDiffResponse,
    creator: User
) -> VersionDiffSnapshot:
    snapshot_key = _build_snapshot_key(batch.id, old_version.version_number, new_version.version_number)
    content_hash = _compute_content_hash(diff)

    existing = db.query(VersionDiffSnapshot).filter(
        VersionDiffSnapshot.snapshot_key == snapshot_key
    ).first()

    if existing:
        if existing.content_hash == content_hash and existing.status == SNAPSHOT_VALID:
            logger.debug(
                "Snapshot %s already exists with same content hash, reusing (id=%d)",
                snapshot_key, existing.id
            )
            return existing
        if existing.status == SNAPSHOT_VALID:
            logger.info(
                "Superseding existing snapshot %s (id=%d)",
                snapshot_key, existing.id
            )
            existing.status = SNAPSHOT_SUPERSEDED
            existing.invalidated_at = datetime.now()
            existing.invalidated_by = creator.id
            db.flush()

    snapshot = VersionDiffSnapshot(
        batch_id=batch.id,
        old_version_id=old_version.id,
        new_version_id=new_version.id,
        old_version_number=old_version.version_number,
        new_version_number=new_version.version_number,
        snapshot_key=snapshot_key,
        status=SNAPSHOT_VALID,
        created_by=creator.id,
        content_hash=content_hash,
        metadata_json=diff.metadata.model_dump(mode='json'),
        summary_json=diff.summary.model_dump(mode='json'),
        added_items_json=[item.model_dump(mode='json') for item in diff.added_items],
        removed_items_json=[item.model_dump(mode='json') for item in diff.removed_items],
        modified_items_json=[item.model_dump(mode='json') for item in diff.modified_items],
        unchanged_items_json=[item.model_dump(mode='json') for item in diff.unchanged_items],
        unresolved_rejections_json=[r.model_dump(mode='json') for r in diff.unresolved_rejections],
        validation_changes_json=[c.model_dump(mode='json') for c in diff.validation_changes],
    )
    db.add(snapshot)
    db.flush()

    from app.schemas import VersionDiffSummary
    schema_count = len(VersionDiffSummary.model_fields)
    actual_count = len(snapshot.summary_json)
    missing = sorted(set(VersionDiffSummary.model_fields.keys()) - set(snapshot.summary_json.keys()))
    logger.info(
        "Created new snapshot %s (id=%d, hash=%s...), "
        "validation_changes=%d items, summary_fields=%d/%d, missing=%s",
        snapshot_key, snapshot.id, content_hash[:16],
        len(diff.validation_changes),
        actual_count, schema_count, missing
    )

    return snapshot


def get_or_compute_diff(
    db: Session,
    batch: DeliveryBatch,
    old_version: ManifestVersion,
    new_version: ManifestVersion,
    current_user: User,
    auto_persist: bool = True
) -> tuple:
    snapshot = get_snapshot_by_versions(
        db, batch.id, old_version.version_number, new_version.version_number
    )
    if snapshot:
        logger.debug(
            "Found existing snapshot %s for v%d -> v%d, checking staleness...",
            snapshot.snapshot_key, old_version.version_number, new_version.version_number
        )
        if _is_snapshot_stale(db, snapshot):
            logger.info(
                "Snapshot %s is stale, refreshing...",
                snapshot.snapshot_key
            )
            snapshot = refresh_snapshot(db, snapshot, current_user)
            db.commit()
            db.refresh(snapshot)
        else:
            logger.debug(
                "Snapshot %s is fresh (hash=%s...), reusing.",
                snapshot.snapshot_key, snapshot.content_hash[:16]
            )
        return snapshot_to_diff_response(snapshot), True, snapshot

    logger.info(
        "No snapshot found for v%d -> v%d, computing live diff (auto_persist=%s)...",
        old_version.version_number, new_version.version_number, auto_persist
    )
    diff = calculate_version_diff(db, batch, old_version, new_version, current_user)

    if auto_persist:
        logger.info(
            "Live diff computed for v%d -> v%d, persisting as new snapshot to ensure consistency...",
            old_version.version_number, new_version.version_number
        )
        try:
            snapshot = save_diff_snapshot(db, batch, old_version, new_version, diff, current_user)
            db.commit()
            db.refresh(snapshot)
            logger.info(
                "Persisted snapshot %s (id=%d, hash=%s...), validation_changes=%d items",
                snapshot.snapshot_key, snapshot.id, snapshot.content_hash[:16],
                len(diff.validation_changes)
            )
            return snapshot_to_diff_response(snapshot), False, snapshot
        except Exception as e:
            logger.warning(
                "Failed to persist snapshot for v%d -> v%d: %s. Returning in-memory diff anyway.",
                old_version.version_number, new_version.version_number, str(e)
            )
            return diff, False, None

    return diff, False, None


def get_snapshot_by_versions(
    db: Session,
    batch_id: int,
    old_version_number: int,
    new_version_number: int
) -> Optional[VersionDiffSnapshot]:
    key = _build_snapshot_key(batch_id, old_version_number, new_version_number)
    return db.query(VersionDiffSnapshot).filter(
        VersionDiffSnapshot.snapshot_key == key,
        VersionDiffSnapshot.status == SNAPSHOT_VALID
    ).first()


def get_latest_snapshot(
    db: Session,
    batch_id: int
) -> Optional[VersionDiffSnapshot]:
    return db.query(VersionDiffSnapshot).filter(
        VersionDiffSnapshot.batch_id == batch_id,
        VersionDiffSnapshot.status == SNAPSHOT_VALID
    ).order_by(
        VersionDiffSnapshot.new_version_number.desc(),
        VersionDiffSnapshot.created_at.desc()
    ).first()


def list_snapshots(
    db: Session,
    batch_id: int,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> Tuple[List[VersionDiffSnapshot], int]:
    query = db.query(VersionDiffSnapshot).filter(
        VersionDiffSnapshot.batch_id == batch_id
    )
    if status:
        query = query.filter(VersionDiffSnapshot.status == status)
    total = query.count()
    snapshots = query.order_by(
        VersionDiffSnapshot.new_version_number.desc(),
        VersionDiffSnapshot.old_version_number.desc(),
        VersionDiffSnapshot.created_at.desc()
    ).offset(offset).limit(limit).all()
    return snapshots, total


def _is_snapshot_stale(db: Session, snapshot: VersionDiffSnapshot) -> bool:
    from app.schemas import VersionDiffSummary
    new_ver = db.query(ManifestVersion).filter(
        ManifestVersion.id == snapshot.new_version_id
    ).first()
    old_ver = db.query(ManifestVersion).filter(
        ManifestVersion.id == snapshot.old_version_id
    ).first()

    snap_new_warnings = snapshot.summary_json.get("validation_warnings_new", 0)
    snap_old_warnings = snapshot.summary_json.get("validation_warnings_old", 0)
    snap_new_errors = snapshot.summary_json.get("validation_errors_new", 0)
    snap_old_errors = snapshot.summary_json.get("validation_errors_old", 0)
    snap_new_passed = snapshot.summary_json.get("validation_passed_new", 0)
    snap_old_passed = snapshot.summary_json.get("validation_passed_old", 0)
    snap_new_total = snapshot.summary_json.get("validation_total_new", 0)
    snap_old_total = snapshot.summary_json.get("validation_total_old", 0)
    snap_new_status = snapshot.summary_json.get("new_version_validation_status", None)
    snap_old_status = snapshot.summary_json.get("old_version_validation_status", None)

    stale_reasons = []

    schema_field_count = len(VersionDiffSummary.model_fields)
    actual_field_count = len(snapshot.summary_json)
    if actual_field_count < schema_field_count:
        stale_reasons.append(
            f"schema upgrade: snapshot has {actual_field_count}/{schema_field_count} fields "
            f"(missing: {sorted(set(VersionDiffSummary.model_fields.keys()) - set(snapshot.summary_json.keys()))})"
        )

    if new_ver:
        actual_new_status = new_ver.validation_status
        if (snap_new_status is None and actual_new_status is not None):
            stale_reasons.append(
                f"new_version validation_status newly available: None->{actual_new_status}"
            )
        elif (snap_new_status is not None and snap_new_status != actual_new_status):
            stale_reasons.append(
                f"new_version validation_status changed: {snap_new_status}->{actual_new_status}"
            )
        actual_new_errors, actual_new_warnings, actual_new_passed, actual_new_total = _count_validation_issues(db, new_ver.id)
        if (actual_new_errors != snap_new_errors or actual_new_warnings != snap_new_warnings
                or actual_new_passed != snap_new_passed or actual_new_total != snap_new_total):
            stale_reasons.append(
                f"new_version validation counts changed: "
                f"errors {snap_new_errors}->{actual_new_errors}, "
                f"warnings {snap_new_warnings}->{actual_new_warnings}, "
                f"passed {snap_new_passed}->{actual_new_passed}, "
                f"total {snap_new_total}->{actual_new_total}"
            )
    elif snap_new_total > 0:
        stale_reasons.append("new_version record missing but snapshot had validation data")

    if old_ver:
        actual_old_status = old_ver.validation_status
        if (snap_old_status is None and actual_old_status is not None):
            stale_reasons.append(
                f"old_version validation_status newly available: None->{actual_old_status}"
            )
        elif (snap_old_status is not None and snap_old_status != actual_old_status):
            stale_reasons.append(
                f"old_version validation_status changed: {snap_old_status}->{actual_old_status}"
            )
        actual_old_errors, actual_old_warnings, actual_old_passed, actual_old_total = _count_validation_issues(db, old_ver.id)
        if (actual_old_errors != snap_old_errors or actual_old_warnings != snap_old_warnings
                or actual_old_passed != snap_old_passed or actual_old_total != snap_old_total):
            stale_reasons.append(
                f"old_version validation counts changed: "
                f"errors {snap_old_errors}->{actual_old_errors}, "
                f"warnings {snap_old_warnings}->{actual_old_warnings}, "
                f"passed {snap_old_passed}->{actual_old_passed}, "
                f"total {snap_old_total}->{actual_old_total}"
            )
    elif snap_old_total > 0:
        stale_reasons.append("old_version record missing but snapshot had validation data")

    actual_unresolved = db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == snapshot.batch_id,
        RejectionRecord.resolved == False
    ).count()
    snap_unresolved = snapshot.summary_json.get("unresolved_rejections_new", 0)
    if actual_unresolved != snap_unresolved:
        stale_reasons.append(
            f"unresolved_rejections changed: {snap_unresolved}->{actual_unresolved}"
        )

    snap_added = snapshot.summary_json.get("added_count", 0)
    snap_removed = snapshot.summary_json.get("removed_count", 0)
    snap_modified = snapshot.summary_json.get("modified_count", 0)
    snap_unchanged = snapshot.summary_json.get("unchanged_count", 0)
    if new_ver and old_ver:
        old_items = db.query(ManifestItem).filter(ManifestItem.manifest_version_id == old_ver.id).count()
        new_items = db.query(ManifestItem).filter(ManifestItem.manifest_version_id == new_ver.id).count()
        snap_old_items = snapshot.summary_json.get("total_items_old", 0)
        snap_new_items = snapshot.summary_json.get("total_items_new", 0)
        if snap_old_items != old_items:
            stale_reasons.append(f"old_version item_count changed: {snap_old_items}->{old_items}")
        if snap_new_items != new_items:
            stale_reasons.append(f"new_version item_count changed: {snap_new_items}->{new_items}")

    if stale_reasons:
        logger.info(
            "Snapshot %s (v%d -> v%d) marked STALE. Reasons: %s",
            snapshot.snapshot_key, snapshot.old_version_number, snapshot.new_version_number,
            "; ".join(stale_reasons)
        )
        return True

    logger.debug(
        "Snapshot %s (v%d -> v%d) is FRESH: errors_new=%d, warnings_new=%d, "
        "total_new=%d, unresolved=%d",
        snapshot.snapshot_key, snapshot.old_version_number, snapshot.new_version_number,
        snap_new_errors, snap_new_warnings, snap_new_total, snap_unresolved
    )
    return False


def refresh_snapshot(db: Session, snapshot: VersionDiffSnapshot, current_user: User, trigger: str = "unknown") -> VersionDiffSnapshot:
    batch = db.query(DeliveryBatch).filter(DeliveryBatch.id == snapshot.batch_id).first()
    old_version = db.query(ManifestVersion).filter(
        ManifestVersion.id == snapshot.old_version_id
    ).first()
    new_version = db.query(ManifestVersion).filter(
        ManifestVersion.id == snapshot.new_version_id
    ).first()
    if not batch or not old_version or not new_version:
        logger.warning(
            "[REFRESH:%s] Cannot refresh snapshot %s: missing batch or versions",
            trigger, snapshot.snapshot_key
        )
        return snapshot

    logger.info(
        "[REFRESH:%s] Start refreshing snapshot %s (v%d -> v%d), old_validation: v%d=%s, v%d=%s",
        trigger, snapshot.snapshot_key,
        snapshot.old_version_number, snapshot.new_version_number,
        snapshot.old_version_number, old_version.validation_status,
        snapshot.new_version_number, new_version.validation_status
    )

    diff = calculate_version_diff(db, batch, old_version, new_version, current_user)

    original_generated_at = snapshot.metadata_json.get("generated_at")
    if original_generated_at:
        diff.metadata.generated_at = original_generated_at

    new_hash = _compute_content_hash(diff)

    from app.schemas import VersionDiffSummary
    schema_field_count = len(VersionDiffSummary.model_fields)
    current_field_count = len(snapshot.summary_json)
    schema_upgrade_needed = current_field_count < schema_field_count

    if snapshot.content_hash == new_hash and not schema_upgrade_needed:
        logger.info(
            "[REFRESH:%s] Snapshot %s content UNCHANGED (hash=%s...), skipping update. validation_changes still %d items.",
            trigger, snapshot.snapshot_key, new_hash[:16],
            len(snapshot.validation_changes_json or [])
        )
        return snapshot

    force_reason = ""
    if schema_upgrade_needed:
        force_reason = (
            f" [SCHEMA_UPGRADE: {current_field_count}->{schema_field_count} fields, "
            f"missing: {sorted(set(VersionDiffSummary.model_fields.keys()) - set(snapshot.summary_json.keys()))}]"
        )

    old_hash = snapshot.content_hash
    old_val_changes = len(snapshot.validation_changes_json or [])
    snapshot.content_hash = new_hash
    snapshot.metadata_json = diff.metadata.model_dump(mode='json')
    snapshot.summary_json = diff.summary.model_dump(mode='json')
    snapshot.added_items_json = [item.model_dump(mode='json') for item in diff.added_items]
    snapshot.removed_items_json = [item.model_dump(mode='json') for item in diff.removed_items]
    snapshot.modified_items_json = [item.model_dump(mode='json') for item in diff.modified_items]
    snapshot.unchanged_items_json = [item.model_dump(mode='json') for item in diff.unchanged_items]
    snapshot.unresolved_rejections_json = [r.model_dump(mode='json') for r in diff.unresolved_rejections]
    snapshot.validation_changes_json = [c.model_dump(mode='json') for c in diff.validation_changes]
    db.flush()

    new_val_changes = len(diff.validation_changes)
    update_kind = "content_changed" if old_hash != new_hash else "schema_only"
    logger.info(
        "[REFRESH:%s] Snapshot %s UPDATED: hash %s... -> %s... (kind=%s%s), "
        "validation_changes: %d -> %d (delta=%+d), "
        "breakdown: new_violation=%d, resolved=%d, modified=%d, "
        "new_passed=%d, removed_passed=%d, unchanged=%d",
        trigger, snapshot.snapshot_key,
        old_hash[:16], new_hash[:16],
        update_kind, force_reason,
        old_val_changes, new_val_changes, new_val_changes - old_val_changes,
        diff.summary.validation_changes_new_violation,
        diff.summary.validation_changes_resolved,
        diff.summary.validation_changes_modified,
        diff.summary.validation_changes_new_passed,
        diff.summary.validation_changes_removed_passed,
        diff.summary.validation_changes_unchanged
    )

    return snapshot


def refresh_snapshots_for_batch(db: Session, batch_id: int, current_user: User, trigger: str = "validate") -> int:
    snapshots = db.query(VersionDiffSnapshot).filter(
        VersionDiffSnapshot.batch_id == batch_id,
        VersionDiffSnapshot.status == SNAPSHOT_VALID
    ).all()

    logger.info(
        "[BATCH_REFRESH:%s] Batch %d: checking %d valid snapshots for staleness...",
        trigger, batch_id, len(snapshots)
    )

    if not snapshots:
        logger.info(
            "[BATCH_REFRESH:%s] Batch %d has no snapshots, nothing to refresh.",
            trigger, batch_id
        )
        return 0

    refreshed = 0
    for snap in snapshots:
        if _is_snapshot_stale(db, snap):
            refresh_snapshot(db, snap, current_user, trigger=trigger)
            refreshed += 1
        else:
            logger.debug(
                "[BATCH_REFRESH:%s] Snapshot %s (v%d->v%d) is fresh, skipping.",
                trigger, snap.snapshot_key, snap.old_version_number, snap.new_version_number
            )

    if refreshed:
        db.commit()
        logger.info(
            "[BATCH_REFRESH:%s] Batch %d: REFRESHED %d/%d snapshots (committed to DB).",
            trigger, batch_id, refreshed, len(snapshots)
        )
    else:
        logger.info(
            "[BATCH_REFRESH:%s] Batch %d: All %d snapshots already up to date, no DB write needed.",
            trigger, batch_id, len(snapshots)
        )
    return refreshed


def snapshot_to_diff_response(snapshot: VersionDiffSnapshot) -> VersionDiffResponse:
    return VersionDiffResponse(
        metadata=VersionDiffMetadata(**snapshot.metadata_json),
        summary=VersionDiffSummary(**snapshot.summary_json),
        added_items=[ItemDiff(**i) for i in snapshot.added_items_json],
        removed_items=[ItemDiff(**i) for i in snapshot.removed_items_json],
        modified_items=[ItemDiff(**i) for i in snapshot.modified_items_json],
        unchanged_items=[ItemDiffSummary(**i) for i in snapshot.unchanged_items_json],
        unresolved_rejections=[RejectionInfo(**r) for r in snapshot.unresolved_rejections_json],
        validation_changes=[ValidationChange(**c) for c in snapshot.validation_changes_json],
    )
