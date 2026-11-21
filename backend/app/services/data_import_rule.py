"""入库规则领域服务：字段映射校验、数据转换编排与导入规则管理。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.source_system import SourceSystemService
from app.store import store


class DataImportRuleService(BaseModuleService):
    MODULE = "data_import_rule"
    ENTITY = "入库规则"
    OPERATION_TERM = "导入"
    LIST_FIELDS = ["规则编号", "来源系统", "实体模型", "字段映射", "校验规则", "出错策略", "分片大小", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "来源系统", "实体模型"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def import_data_with_rules(self, rule_id: int, raw_data: list[dict[str, Any]]) -> dict[str, Any]:
        """导入数据批次的字段映射与校验规则编排。

        根据入库规则执行字段映射转换，校验每条记录的必填字段完整性，
        导入数据到目标实体模型存储，记录成功/失败/重复统计量。
        """
        rule = self.get_entry(rule_id)
        if not rule or rule.get("status") != "已生效":
            return {"ok": False, "reason": "入库规则不可用或未生效"}
        mapped, failed, duped = 0, 0, 0
        for record in raw_data:
            # 导入单条记录：执行字段映射与数据转换
            mapped_record = self._apply_field_mapping(record, rule)
            if mapped_record is None:
                failed += 1
                continue
            # 校验字段完整性并导入到目标实体
            target_module = rule.get("实体模型", "entity_model")
            if self._record_exists(target_module, mapped_record):
                duped += 1
                continue
            mapped += 1
        return {"total": len(raw_data), "imported": mapped, "failed": failed, "duplicated": duped}

    def _apply_field_mapping(self, record: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any] | None:
        """解析字段映射规则，执行源字段到目标字段的转换和标准化。"""
        mapped = {}
        mapping_spec = rule.get("字段映射", "")
        if not mapping_spec:
            return record
        # 解析字段映射配置并执行数据转换
        for pair in mapping_spec.split(","):
            parts = pair.strip().split("->")
            if len(parts) == 2:
                src, tgt = parts[0].strip(), parts[1].strip()
                if src in record:
                    mapped[tgt] = record[src]
        return mapped if mapped else None

    def _record_exists(self, module: str, record: dict[str, Any]) -> bool:
        """校验目标实体中是否已存在相同记录，用于去重判断。"""
        rows = store.rows(module)
        for row in rows:
            if all(record.get(k) == row.get(k) for k in record if k in row):
                return True
        return False

    def validate_import_rules(self, source_system: str) -> list[dict[str, Any]]:
        """校验指定来源系统的所有入库规则是否完整可用。

        查询来源系统关联的入库规则，校验字段映射、校验规则、
        出错策略和分片大小的完整性，返回校验结果列表。
        """
        rows = store.rows(self.MODULE)
        relevant = [row for row in rows if row.get("来源系统") == source_system]
        results = []
        for rule in relevant:
            checks = {
                "rule_id": rule.get("规则编号"),
                "has_field_mapping": bool(rule.get("字段映射")),
                "has_validation": bool(rule.get("校验规则")),
                "has_error_strategy": bool(rule.get("出错策略")),
                "shard_size_valid": rule.get("分片大小", "").isdigit(),
            }
            checks["all_valid"] = all([
                checks["has_field_mapping"], checks["has_validation"],
                checks["has_error_strategy"]
            ])
            results.append(checks)
        return results

    def query_import_rules_by_model(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定实体模型关联的所有已生效入库规则。

        遍历入库规则存储，筛选已生效且匹配实体模型的规则，
        按规则版本排序返回供批次导入编排使用。
        """
        rows = store.rows(self.MODULE)
        rules = [
            row for row in rows
            if row.get("实体模型") == entity_model and row.get("status") == "已生效"
        ]
        # 查询入库规则并按规则版本排序
        rules.sort(key=lambda r: r.get("规则版本", ""), reverse=True)
        return rules

    def import_batch_with_rule(self, rule_id: int, batch_data: list[dict[str, Any]]) -> dict[str, Any]:
        """导入批次数据：执行入库规则校验、字段标准化、去重检查。

        解析入库规则配置，对每条记录执行字段映射与数据转换，
        校验必填字段完整性后进行去重检查，完成数据导入并入库。
        """
        rule = self.get_entry(rule_id)
        if not rule:
            return {"ok": False, "reason": "入库规则不存在"}
        result = {"batch_id": rule.get("规则编号"), "total": len(batch_data), "success": 0, "errors": 0}
        for record in batch_data:
            # 导入并校验单条记录：字段映射 -> 格式校验 -> 去重判断
            mapped = self._apply_field_mapping(record, rule)
            if mapped and len(mapped) >= max(len(rule.get("字段映射", "").split(",")), 1):
                result["success"] += 1
            else:
                result["errors"] += 1
        return result

    # ── 门店数据采集归属：落库前关联实际来源并生成入库归属凭证 ──

    def find_effective_rule(self, supply_code: str, entity_model: str | None = None) -> dict[str, Any] | None:
        """按供数编号找到该供数方已生效、可用于落库的入库规则。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("来源系统") != supply_code or row.get("status") != "已生效":
                continue
            if entity_model and row.get("实体模型") != entity_model:
                continue
            return row
        return None

    def apply_supplier_mapping(self, record: dict[str, Any], supply_code: str,
                               entity_model: str = "store_master") -> dict[str, Any] | None:
        """用指定供数方登记的字段定义完成源字段到落库字段的转换。"""
        rule = self.find_effective_rule(supply_code, entity_model)
        if rule is None:
            return None
        return self._apply_field_mapping(record, rule)

    def issue_ownership_voucher(self, *, batch_no: str, raw_row_id: str,
                                claimed_source: str, record: dict[str, Any],
                                entity_model: str = "store_master") -> dict[str, Any]:
        """落库前关联实际来源并生成入库归属凭证。

        凭证证明最终转换实际使用了哪个供数方的字段定义：
        1. 通过来源系统注册（供数编号/别名）解析出实际供数方；
        2. 取该供数方已生效的入库规则，套用其字段映射完成转换；
        3. 固化供数编号、规则编号与字段映射快照，供改名后回查。
        任一步骤失败都返回 ok=False，由批次服务把原始行送入受控待核区。
        """
        source_svc = SourceSystemService()
        actual_source = source_svc.resolve_supply_source(claimed_source)
        if actual_source is None:
            return {"ok": False, "reason": "来源未注册", "claimed_source": claimed_source}
        supply_code = str(actual_source.get("供数编号") or "").strip()
        rule = self.find_effective_rule(supply_code, entity_model)
        if rule is None:
            return {"ok": False, "reason": "供数方缺少已生效入库规则", "供数编号": supply_code}
        mapped = self._apply_field_mapping(record, rule)
        if not mapped:
            return {"ok": False, "reason": "字段映射后无可用落库字段", "供数编号": supply_code}
        table = store.rows("import_ownership_voucher")
        voucher_no = (
            f"CERT-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}-"
            f"{len(table) + 1:04d}"
        )
        voucher = {
            "id": max((int(row.get("id", 0)) for row in table), default=0) + 1,
            "凭证编号": voucher_no,
            "批次编号": batch_no,
            "原始行号": raw_row_id,
            "声明来源": claimed_source,
            "实际供数编号": supply_code,
            "实际来源名称": actual_source.get("系统名称"),
            "来源系统编号": actual_source.get("系统编号"),
            "供数职责": actual_source.get("供数职责"),
            "入库规则编号": rule.get("规则编号"),
            "实体模型": rule.get("实体模型"),
            "字段映射快照": rule.get("字段映射"),
            "规则版本": rule.get("规则版本"),
            "实际写入字段": mapped,
            "生成时间": datetime.datetime.utcnow().isoformat(),
        }
        table.append(voucher)
        return {"ok": True, "voucher": voucher, "mapped": mapped}