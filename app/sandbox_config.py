import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import (
    SystemConfig, ApprovalLog, User,
    APPROVAL_LOG_ACTION_SANDBOX_CONFIG_UPDATE,
    APPROVAL_LOG_ACTION_SANDBOX_CONFIG_BATCH_UPDATE,
    APPROVAL_LOG_ACTION_SANDBOX_CONFIG_VIEW,
)
from app.schemas import (
    ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER,
    SandboxConfigResponse, SandboxConfigListResponse,
    SandboxConfigAuditLogResponse,
)

logger = logging.getLogger(__name__)


class SandboxConfigDef:
    __slots__ = ("key", "value_type", "default", "description", "min_val", "max_val")

    def __init__(
        self,
        key: str,
        value_type: str,
        default: str,
        description: str,
        min_val: Optional[int] = None,
        max_val: Optional[int] = None,
    ):
        self.key = key
        self.value_type = value_type
        self.default = default
        self.description = description
        self.min_val = min_val
        self.max_val = max_val


CONFIG_SANDBOX_ENABLED = SandboxConfigDef(
    key="sandbox.enabled",
    value_type="bool",
    default="true",
    description="是否启用恢复后验收沙盒功能",
)

CONFIG_SANDBOX_REQUIRE_ADMIN_CONFIRM = SandboxConfigDef(
    key="sandbox.require_admin_confirm",
    value_type="bool",
    default="true",
    description="沙盒正式确认是否需要 admin 权限（否则 lead 也可确认）",
)

CONFIG_SANDBOX_AUTO_EXPIRE_HOURS = SandboxConfigDef(
    key="sandbox.auto_expire_hours",
    value_type="int",
    default="24",
    description="沙盒会话自动过期时间（小时）",
    min_val=1,
    max_val=8760,
)

ALL_CONFIGS: Dict[str, SandboxConfigDef] = {
    cfg.key: cfg for cfg in [
        CONFIG_SANDBOX_ENABLED,
        CONFIG_SANDBOX_REQUIRE_ADMIN_CONFIRM,
        CONFIG_SANDBOX_AUTO_EXPIRE_HOURS,
    ]
}

CONFIG_KEYS = set(ALL_CONFIGS.keys())


CONFIG_KEY_SANDBOX_ENABLED = CONFIG_SANDBOX_ENABLED.key
CONFIG_KEY_SANDBOX_REQUIRE_ADMIN_CONFIRM = CONFIG_SANDBOX_REQUIRE_ADMIN_CONFIRM.key
CONFIG_KEY_SANDBOX_AUTO_EXPIRE_HOURS = CONFIG_SANDBOX_AUTO_EXPIRE_HOURS.key


class ConcurrencyConflictError(ValueError):
    pass


class ConfigValidationError(ValueError):
    pass


def _validate_bool_value(value: str) -> bool:
    return value.lower() in ("0", "1", "true", "false", "yes", "no", "on", "off")


def _validate_int_value(value: str, min_val: Optional[int] = None, max_val: Optional[int] = None) -> bool:
    try:
        v = int(value)
        if min_val is not None and v < min_val:
            return False
        if max_val is not None and v > max_val:
            return False
        return True
    except (ValueError, TypeError):
        return False


def _normalize_bool_value(config_value: str) -> str:
    v = config_value.lower()
    if v in ("1", "true", "yes", "on"):
        return "true"
    return "false"


def _parse_config_value(config_value: str, value_type: str) -> Any:
    if value_type == "bool":
        return config_value.lower() in ("1", "true", "yes", "on")
    elif value_type == "int":
        try:
            return int(config_value)
        except (ValueError, TypeError):
            return 0
    return config_value


def validate_config_value(config_key: str, config_value: str) -> Tuple[bool, Optional[str]]:
    if config_key not in ALL_CONFIGS:
        return False, f"配置项 {config_key} 不在沙盒配置白名单中。允许的配置: {sorted(CONFIG_KEYS)}"

    cfg_def = ALL_CONFIGS[config_key]

    if cfg_def.value_type == "bool":
        if not _validate_bool_value(config_value):
            return False, f"配置项 {config_key} 是 bool 类型，有效值: 0/1, true/false, yes/no, on/off（不区分大小写）"
    elif cfg_def.value_type == "int":
        if not _validate_int_value(config_value, cfg_def.min_val, cfg_def.max_val):
            range_msg = ""
            if cfg_def.min_val is not None or cfg_def.max_val is not None:
                range_msg = f"（范围: {cfg_def.min_val or ''}-{cfg_def.max_val or ''}）"
            return False, f"配置项 {config_key} 是 int 类型，必须输入有效整数{range_msg}"

    return True, None


def _get_user_info(db: Session, user_id: int) -> Tuple[str, Optional[str]]:
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        return user.username, user.display_name
    return "unknown", None


def get_config_bool(db: Session, cfg_def: SandboxConfigDef) -> bool:
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == cfg_def.key).first()
    if not cfg:
        return _parse_config_value(cfg_def.default, "bool")
    if cfg.value_type == "bool":
        return cfg.config_value.lower() in ("1", "true", "yes", "on")
    try:
        return bool(int(cfg.config_value))
    except (ValueError, TypeError):
        return _parse_config_value(cfg_def.default, "bool")


def get_config_int(db: Session, cfg_def: SandboxConfigDef) -> int:
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == cfg_def.key).first()
    if not cfg:
        return _parse_config_value(cfg_def.default, "int")
    try:
        return int(cfg.config_value)
    except (ValueError, TypeError):
        return _parse_config_value(cfg_def.default, "int")


def is_sandbox_enabled(db: Session) -> bool:
    return get_config_bool(db, CONFIG_SANDBOX_ENABLED)


def require_admin_for_confirm(db: Session) -> bool:
    return get_config_bool(db, CONFIG_SANDBOX_REQUIRE_ADMIN_CONFIRM)


def get_auto_expire_hours(db: Session) -> int:
    return get_config_int(db, CONFIG_SANDBOX_AUTO_EXPIRE_HOURS)


def ensure_default_configs(db: Session):
    for cfg_def in ALL_CONFIGS.values():
        existing = db.query(SystemConfig).filter(SystemConfig.config_key == cfg_def.key).first()
        if not existing:
            db.add(SystemConfig(
                config_key=cfg_def.key,
                config_value=cfg_def.default,
                value_type=cfg_def.value_type,
                description=cfg_def.description,
            ))
    db.commit()


def check_sandbox_enabled_or_raise(db: Session):
    if not is_sandbox_enabled(db):
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="系统配置已关闭恢复后验收沙盒功能",
        )


def check_confirm_permission(current_user: User, db: Session):
    if require_admin_for_confirm(db):
        if current_user.role != ROLE_ADMIN:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足：系统配置要求只有 admin 角色才能执行沙盒确认/拒绝操作。您的角色: {current_user.role}",
            )
    else:
        if current_user.role not in [ROLE_ADMIN, ROLE_LEAD]:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足：只有 admin 或 lead 角色才能执行沙盒确认/拒绝操作。您的角色: {current_user.role}",
            )


def check_view_permission(current_user: User):
    if current_user.role not in [ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER]:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"权限不足：只有 reviewer 及以上角色才能查看沙盒会话。您的角色: {current_user.role}",
        )


def read_config(db: Session, config_key: str) -> Optional[SystemConfig]:
    if config_key not in ALL_CONFIGS:
        return None
    return db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()


def list_configs(db: Session, current_user: User) -> SandboxConfigListResponse:
    ensure_default_configs(db)

    username, _ = _get_user_info(db, current_user.id)
    log = ApprovalLog(
        batch_id=0,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_SANDBOX_CONFIG_VIEW,
        comment="查看沙盒配置列表",
        extra_data={
            "viewer_username": username,
            "viewer_role": current_user.role,
        },
    )
    db.add(log)
    db.commit()

    configs = db.query(SystemConfig).filter(
        SystemConfig.config_key.in_(CONFIG_KEYS)
    ).order_by(SystemConfig.config_key).all()

    items = []
    for cfg in configs:
        updater_username = None
        if cfg.updated_by:
            updater_username, _ = _get_user_info(db, cfg.updated_by)
        items.append(SandboxConfigResponse(
            config_key=cfg.config_key,
            config_value=cfg.config_value,
            value_type=cfg.value_type,
            description=cfg.description,
            updated_by=cfg.updated_by,
            updated_by_username=updater_username,
            updated_at=cfg.updated_at,
            parsed_value=_parse_config_value(cfg.config_value, cfg.value_type),
        ))

    return SandboxConfigListResponse(
        items=items,
        total=len(items),
        sandbox_enabled=is_sandbox_enabled(db),
        require_admin_confirm=require_admin_for_confirm(db),
        auto_expire_hours=get_auto_expire_hours(db),
    )


def get_single_config(db: Session, config_key: str, current_user: User) -> SandboxConfigResponse:
    ensure_default_configs(db)

    if config_key not in ALL_CONFIGS:
        raise ConfigValidationError(
            f"配置项 {config_key} 不在沙盒配置白名单中。允许的配置: {sorted(CONFIG_KEYS)}"
        )

    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()
    if not cfg:
        raise ConfigValidationError(f"配置项 {config_key} 不存在")

    updater_username = None
    if cfg.updated_by:
        updater_username, _ = _get_user_info(db, cfg.updated_by)

    return SandboxConfigResponse(
        config_key=cfg.config_key,
        config_value=cfg.config_value,
        value_type=cfg.value_type,
        description=cfg.description,
        updated_by=cfg.updated_by,
        updated_by_username=updater_username,
        updated_at=cfg.updated_at,
        parsed_value=_parse_config_value(cfg.config_value, cfg.value_type),
    )


def _build_config_response(db: Session, cfg: SystemConfig) -> SandboxConfigResponse:
    updater_username = None
    if cfg.updated_by:
        updater_username, _ = _get_user_info(db, cfg.updated_by)
    return SandboxConfigResponse(
        config_key=cfg.config_key,
        config_value=cfg.config_value,
        value_type=cfg.value_type,
        description=cfg.description,
        updated_by=cfg.updated_by,
        updated_by_username=updater_username,
        updated_at=cfg.updated_at,
        parsed_value=_parse_config_value(cfg.config_value, cfg.value_type),
    )


def update_config(
    db: Session,
    config_key: str,
    config_value: str,
    current_user: User,
    expected_old_value: Optional[str] = None,
) -> SandboxConfigResponse:
    ensure_default_configs(db)

    valid, err_msg = validate_config_value(config_key, config_value)
    if not valid:
        raise ConfigValidationError(err_msg)

    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()
    if not cfg:
        raise ConfigValidationError(f"配置项 {config_key} 不存在")

    old_value = cfg.config_value

    if expected_old_value is not None and old_value != expected_old_value:
        raise ConcurrencyConflictError(
            f"配置项 {config_key} 已被其他操作修改（并发冲突）。"
            f"期望旧值: '{expected_old_value}'，实际当前值: '{old_value}'。"
            f"请刷新后重试。"
        )

    cfg_def = ALL_CONFIGS[config_key]
    normalized_value = config_value
    if cfg_def.value_type == "bool":
        normalized_value = _normalize_bool_value(config_value)

    cfg.config_value = normalized_value
    cfg.value_type = cfg_def.value_type
    if not cfg.description:
        cfg.description = cfg_def.description
    cfg.updated_by = current_user.id
    db.commit()
    db.refresh(cfg)

    updater_username, _ = _get_user_info(db, current_user.id)

    log = ApprovalLog(
        batch_id=0,
        actor_id=current_user.id,
        action=APPROVAL_LOG_ACTION_SANDBOX_CONFIG_UPDATE,
        comment=f"修改沙盒配置: {config_key} = '{old_value}' -> '{normalized_value}' ({cfg_def.value_type})",
        extra_data={
            "config_key": config_key,
            "old_value": old_value,
            "new_value": normalized_value,
            "value_type": cfg_def.value_type,
            "updated_by_username": updater_username,
        },
    )
    db.add(log)
    db.commit()

    return _build_config_response(db, cfg)


def batch_update_configs(
    db: Session,
    updates: List[Dict[str, Any]],
    current_user: User,
) -> Dict[str, Any]:
    ensure_default_configs(db)

    results = {}
    errors = []
    actual_updates = []

    for item in updates:
        config_key = item.get("config_key", "")
        config_value = item.get("config_value", "")
        expected_old_value = item.get("expected_old_value")

        valid, err_msg = validate_config_value(config_key, config_value)
        if not valid:
            errors.append({"config_key": config_key, "error": err_msg})
            continue

        cfg = db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()
        if not cfg:
            errors.append({"config_key": config_key, "error": f"配置项 {config_key} 不存在"})
            continue

        old_value = cfg.config_value

        if expected_old_value is not None and old_value != expected_old_value:
            errors.append({
                "config_key": config_key,
                "error": (
                    f"并发冲突: 期望旧值 '{expected_old_value}'，"
                    f"实际当前值 '{old_value}'。请刷新后重试。"
                ),
            })
            continue

        cfg_def = ALL_CONFIGS[config_key]
        normalized_value = config_value
        if cfg_def.value_type == "bool":
            normalized_value = _normalize_bool_value(config_value)

        cfg.config_value = normalized_value
        cfg.value_type = cfg_def.value_type
        if not cfg.description:
            cfg.description = cfg_def.description
        cfg.updated_by = current_user.id

        actual_updates.append({
            "config_key": config_key,
            "old_value": old_value,
            "new_value": normalized_value,
            "value_type": cfg_def.value_type,
        })

    db.commit()

    updater_username, _ = _get_user_info(db, current_user.id)

    for up in actual_updates:
        cfg = db.query(SystemConfig).filter(SystemConfig.config_key == up["config_key"]).first()
        if cfg:
            results[up["config_key"]] = _build_config_response(db, cfg).model_dump()

    if actual_updates:
        log = ApprovalLog(
            batch_id=0,
            actor_id=current_user.id,
            action=APPROVAL_LOG_ACTION_SANDBOX_CONFIG_BATCH_UPDATE,
            comment=f"批量修改沙盒配置: {len(actual_updates)} 项，失败 {len(errors)} 项",
            extra_data={
                "updates": actual_updates,
                "errors": errors,
                "updated_by_username": updater_username,
            },
        )
        db.add(log)
        db.commit()

    return {
        "success": len(errors) == 0,
        "updated_count": len(actual_updates),
        "failed_count": len(errors),
        "results": results,
        "errors": errors,
    }


def get_audit_logs(
    db: Session,
    current_user: User,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    config_actions = [
        APPROVAL_LOG_ACTION_SANDBOX_CONFIG_UPDATE,
        APPROVAL_LOG_ACTION_SANDBOX_CONFIG_BATCH_UPDATE,
        APPROVAL_LOG_ACTION_SANDBOX_CONFIG_VIEW,
    ]

    logs = db.query(ApprovalLog).filter(
        ApprovalLog.action.in_(config_actions)
    ).order_by(ApprovalLog.created_at.desc()).offset(offset).limit(limit).all()

    results = []
    for log in logs:
        username, display_name = _get_user_info(db, log.actor_id)
        extra = log.extra_data or {}
        results.append(SandboxConfigAuditLogResponse(
            id=log.id,
            action=log.action,
            actor_id=log.actor_id,
            actor_username=username,
            actor_display_name=display_name,
            created_at=log.created_at,
            config_key=extra.get("config_key"),
            old_value=extra.get("old_value"),
            new_value=extra.get("new_value"),
            comment=log.comment,
            extra_data=log.extra_data,
        ).model_dump())

    return results


def check_config_eligibility(current_user: User) -> Dict[str, bool]:
    is_admin = current_user.role == ROLE_ADMIN
    is_lead = current_user.role == ROLE_LEAD
    is_reviewer = current_user.role == ROLE_REVIEWER
    can_view = is_admin or is_lead or is_reviewer
    can_edit = is_admin
    return {
        "can_view": can_view,
        "can_edit": can_edit,
        "is_admin": is_admin,
        "is_lead": is_lead,
        "is_reviewer": is_reviewer,
    }
