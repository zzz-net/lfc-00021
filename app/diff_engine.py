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
    DIFF_ACTION_MODIFIED, DIFF_ACTION_UNCHANGED
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
                    change_type="new_violation"
                ))
        elif new_r is None:
            if not old_r.passed:
                changes.append(ValidationChange(
                    item_key=old_r.item_key,
                    field_name=old_r.field_name,
                    rule_code=old_r.rule_code,
                    old_severity=old_r.severity,
                    old_passed=old_r.passed,
                    old_message=old_r.message,
                    change_type="resolved"
                ))
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
                    change_type="resolved"
                ))
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
                    change_type="new_violation"
                ))
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
                    change_type="modified"
                ))

    return changes


def _count_validation_issues(db: Session, version_id: int) -> tuple:
    results = db.query(ValidationResult).filter(
        ValidationResult.manifest_version_id == version_id
    ).all()
    errors = sum(1 for r in results if not r.passed and r.severity == "error")
    warnings = sum(1 for r in results if not r.passed and r.severity == "warning")
    return errors, warnings


def _count_unresolved_rejections(db: Session, batch_id: int, version_id: int) -> int:
    return db.query(RejectionRecord).filter(
        RejectionRecord.batch_id == batch_id,
        RejectionRecord.manifest_version_id == version_id,
        RejectionRecord.resolved == False
    ).count()


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

    val_errors_old, val_warnings_old = _count_validation_issues(db, old_version.id)
    val_errors_new, val_warnings_new = _count_validation_issues(db, new_version.id)

    unresolved_rejections = _collect_unresolved_rejections(
        db, batch.id, old_version.id, new_version.id
    )
    validation_changes = _collect_validation_changes(db, old_version.id, new_version.id)

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
        validation_warnings_new=val_warnings_new
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
            return existing
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
    return snapshot


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
