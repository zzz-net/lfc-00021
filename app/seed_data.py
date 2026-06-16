from sqlalchemy.orm import Session
from app.models import User, ValidationRule
from app.schemas import (
    ROLE_ADMIN, ROLE_LEAD, ROLE_REVIEWER, ROLE_SUBMITTER
)


SEED_USERS = [
    {"username": "admin", "role": ROLE_ADMIN, "display_name": "系统管理员"},
    {"username": "lead_wang", "role": ROLE_LEAD, "display_name": "王组长"},
    {"username": "reviewer_li", "role": ROLE_REVIEWER, "display_name": "李评审"},
    {"username": "reviewer_zhang", "role": ROLE_REVIEWER, "display_name": "张评审"},
    {"username": "submitter_chen", "role": ROLE_SUBMITTER, "display_name": "陈交付"},
    {"username": "submitter_zhao", "role": ROLE_SUBMITTER, "display_name": "赵交付"},
]


SEED_RULES = [
    {
        "rule_code": "REQ_ITEM_ID",
        "name": "必填字段-项目编号",
        "description": "item_id 字段必须存在且非空",
        "rule_type": "required",
        "target_field": "item_id",
        "rule_config": None,
        "severity": "error",
    },
    {
        "rule_code": "REQ_ITEM_NAME",
        "name": "必填字段-项目名称",
        "description": "item_name 字段必须存在且非空",
        "rule_type": "required",
        "target_field": "item_name",
        "rule_config": None,
        "severity": "error",
    },
    {
        "rule_code": "REQ_QUANTITY",
        "name": "必填字段-数量",
        "description": "quantity 字段必须存在且为正整数",
        "rule_type": "required",
        "target_field": "quantity",
        "rule_config": None,
        "severity": "error",
    },
    {
        "rule_code": "REQ_UNIT_PRICE",
        "name": "必填字段-单价",
        "description": "unit_price 字段必须存在且为正数",
        "rule_type": "required",
        "target_field": "unit_price",
        "rule_config": None,
        "severity": "error",
    },
    {
        "rule_code": "TYPE_QUANTITY_INT",
        "name": "类型校验-数量正整数",
        "description": "quantity 必须是正整数",
        "rule_type": "positive_integer",
        "target_field": "quantity",
        "rule_config": None,
        "severity": "error",
    },
    {
        "rule_code": "TYPE_PRICE_POSITIVE",
        "name": "类型校验-单价正数",
        "description": "unit_price 必须是正数",
        "rule_type": "positive_number",
        "target_field": "unit_price",
        "rule_config": None,
        "severity": "error",
    },
    {
        "rule_code": "RANGE_QUANTITY",
        "name": "范围校验-数量",
        "description": "quantity 必须在 1 到 10000 之间",
        "rule_type": "range",
        "target_field": "quantity",
        "rule_config": {"min": 1, "max": 10000},
        "severity": "warning",
    },
    {
        "rule_code": "FORMAT_ITEM_ID",
        "name": "格式校验-项目编号",
        "description": "item_id 必须以 ITEM- 开头",
        "rule_type": "pattern",
        "target_field": "item_id",
        "rule_config": {"pattern": "^ITEM-"},
        "severity": "warning",
    },
    {
        "rule_code": "CALC_TOTAL_AMOUNT",
        "name": "计算校验-总金额",
        "description": "total_amount 应等于 quantity * unit_price",
        "rule_type": "calculation",
        "target_field": "total_amount",
        "rule_config": {"formula": "quantity * unit_price"},
        "severity": "error",
    },
]


def initialize_seed_data(db: Session):
    for user_data in SEED_USERS:
        existing = db.query(User).filter(User.username == user_data["username"]).first()
        if not existing:
            user = User(**user_data)
            db.add(user)

    for rule_data in SEED_RULES:
        existing = db.query(ValidationRule).filter(ValidationRule.rule_code == rule_data["rule_code"]).first()
        if not existing:
            rule = ValidationRule(**rule_data)
            db.add(rule)

    db.commit()
