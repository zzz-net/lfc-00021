import re
from typing import List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from app.models import (
    ValidationRule, ManifestVersion, ManifestItem, ValidationResult
)


class ValidationEngine:
    def __init__(self, db: Session):
        self.db = db

    def _get_active_rules(self) -> List[ValidationRule]:
        return self.db.query(ValidationRule).filter(ValidationRule.is_active == True).all()

    def _check_required(self, item_data: Dict[str, Any], field: str) -> Tuple[bool, str]:
        if field not in item_data:
            return False, f"字段 '{field}' 缺失"
        value = item_data[field]
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return False, f"字段 '{field}' 不能为空"
        return True, ""

    def _check_positive_integer(self, item_data: Dict[str, Any], field: str) -> Tuple[bool, str]:
        if field not in item_data:
            return True, ""
        value = item_data[field]
        try:
            int_val = int(value)
            if int_val <= 0:
                return False, f"字段 '{field}' 必须是正整数，当前值: {value}"
        except (ValueError, TypeError):
            return False, f"字段 '{field}' 必须是整数类型，当前值: {value}"
        return True, ""

    def _check_positive_number(self, item_data: Dict[str, Any], field: str) -> Tuple[bool, str]:
        if field not in item_data:
            return True, ""
        value = item_data[field]
        try:
            float_val = float(value)
            if float_val <= 0:
                return False, f"字段 '{field}' 必须是正数，当前值: {value}"
        except (ValueError, TypeError):
            return False, f"字段 '{field}' 必须是数字类型，当前值: {value}"
        return True, ""

    def _check_range(self, item_data: Dict[str, Any], field: str, config: Dict[str, Any]) -> Tuple[bool, str]:
        if field not in item_data or config is None:
            return True, ""
        value = item_data[field]
        try:
            num_val = float(value)
            min_val = config.get("min")
            max_val = config.get("max")
            if min_val is not None and num_val < min_val:
                return False, f"字段 '{field}' 值 {value} 小于最小值 {min_val}"
            if max_val is not None and num_val > max_val:
                return False, f"字段 '{field}' 值 {value} 大于最大值 {max_val}"
        except (ValueError, TypeError):
            return True, ""
        return True, ""

    def _check_pattern(self, item_data: Dict[str, Any], field: str, config: Dict[str, Any]) -> Tuple[bool, str]:
        if field not in item_data or config is None:
            return True, ""
        value = str(item_data[field])
        pattern = config.get("pattern", "")
        if not re.search(pattern, value):
            return False, f"字段 '{field}' 值 '{value}' 不匹配模式 '{pattern}'"
        return True, ""

    def _check_calculation(self, item_data: Dict[str, Any], field: str, config: Dict[str, Any]) -> Tuple[bool, str]:
        if config is None or "formula" not in config:
            return True, ""
        formula = config["formula"]
        try:
            if formula == "quantity * unit_price":
                qty = float(item_data.get("quantity", 0))
                price = float(item_data.get("unit_price", 0))
                expected = qty * price
                if field in item_data:
                    actual = float(item_data[field])
                    if abs(actual - expected) > 0.01:
                        return False, f"字段 '{field}' 计算错误: 预期 {expected:.2f}, 实际 {actual:.2f}"
        except (ValueError, TypeError, KeyError):
            return False, f"字段 '{field}' 计算校验失败，数据缺失或类型错误"
        return True, ""

    def _apply_rule(self, rule: ValidationRule, item_data: Dict[str, Any]) -> Tuple[bool, str]:
        if rule.rule_type == "required":
            return self._check_required(item_data, rule.target_field)
        elif rule.rule_type == "positive_integer":
            return self._check_positive_integer(item_data, rule.target_field)
        elif rule.rule_type == "positive_number":
            return self._check_positive_number(item_data, rule.target_field)
        elif rule.rule_type == "range":
            return self._check_range(item_data, rule.target_field, rule.rule_config)
        elif rule.rule_type == "pattern":
            return self._check_pattern(item_data, rule.target_field, rule.rule_config)
        elif rule.rule_type == "calculation":
            return self._check_calculation(item_data, rule.target_field, rule.rule_config)
        return True, ""

    def run_validation(self, manifest_version_id: int) -> Dict[str, Any]:
        manifest_version = self.db.query(ManifestVersion).filter(
            ManifestVersion.id == manifest_version_id
        ).first()
        if not manifest_version:
            raise ValueError(f"Manifest version {manifest_version_id} not found")

        self.db.query(ValidationResult).filter(
            ValidationResult.manifest_version_id == manifest_version_id
        ).delete()
        self.db.commit()

        rules = self._get_active_rules()
        items = self.db.query(ManifestItem).filter(
            ManifestItem.manifest_version_id == manifest_version_id
        ).all()

        all_results = []
        total_passed = 0
        total_failed = 0
        total_warnings = 0

        for item in items:
            item_data = item.item_data
            for rule in rules:
                passed, message = self._apply_rule(rule, item_data)

                result = ValidationResult(
                    manifest_version_id=manifest_version_id,
                    manifest_item_id=item.id,
                    rule_id=rule.id,
                    rule_code=rule.rule_code,
                    severity=rule.severity,
                    passed=passed,
                    message=message if not passed else "校验通过",
                    field_name=rule.target_field,
                    line_number=item.line_number,
                    item_key=item.item_key,
                )
                self.db.add(result)
                all_results.append(result)

                if passed:
                    total_passed += 1
                else:
                    if rule.severity == "warning":
                        total_warnings += 1
                    else:
                        total_failed += 1

        total_checks = len(all_results)
        validation_passed = total_failed == 0

        summary = {
            "total_rules": len(rules),
            "total_items": len(items),
            "total_checks": total_checks,
            "passed": total_passed,
            "failed": total_failed,
            "warnings": total_warnings,
            "validation_passed": validation_passed,
            "failed_items_count": len(set(
                r.manifest_item_id for r in all_results if not r.passed and r.severity == "error"
            )),
            "warning_items_count": len(set(
                r.manifest_item_id for r in all_results if not r.passed and r.severity == "warning"
            )),
        }

        manifest_version.validation_status = "passed" if validation_passed else "failed"
        manifest_version.validation_summary = summary
        self.db.commit()
        self.db.refresh(manifest_version)

        return {
            "manifest_version_id": manifest_version_id,
            "summary": summary,
            "results": all_results,
            "total_rules": len(rules),
        }
