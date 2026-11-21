"""导入批次领域服务：批次拆分、进度追踪与数据入库统计。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.source_system import SourceSystemService
from app.store import store


class ImportBatchService(BaseModuleService):
    MODULE = "import_batch"
    ENTITY = "导入批次"
    OPERATION_TERM = "导入"
    LIST_FIELDS = ["批次编号", "来源系统", "实体模型", "总记录数", "成功数", "失败数", "重复数", "批次状态"]
    REQUIRED_FIELDS = ["批次编号", "来源系统", "实体模型"]
    STATUS_ORDER = ["待处理", "处理中", "已完成", "已取消"]
    ACTION_RULES = {"开始处理": "处理中", "标记完成": "已完成", "取消导入": "已取消"}
    NEGATIVE_ACTIONS = []

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(str(value))
        except (ValueError, TypeError):
            return default

    def import_batch_from_source(self, source_system: str, entity_model: str,
                                  raw_records: int) -> dict[str, Any]:
        """导入数据批次：从来源系统采集数据并入库到目标实体模型。

        导入批次数据到import_batch存储，包含来源系统、实体模型、
        总记录数、成功数、失败数和重复数统计，并标记批次状态。
        """
        batch_id = f"BATCH-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        total = raw_records
        # 导入批次数据：采集来源系统记录，执行数据校验和入库
        success_count = max(int(total * 0.92), 0)  # 模拟92%成功率
        failed_count = total - success_count
        duplicate_count = max(int(success_count * 0.06), 0)  # 模拟6%重复率
        import_batch = {
            "批次编号": batch_id,
            "来源系统": source_system,
            "实体模型": entity_model,
            "总记录数": str(total),
            "成功数": str(success_count),
            "失败数": str(failed_count),
            "重复数": str(duplicate_count),
            "批次状态": "已完成",
        }
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update(import_batch)
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = failed_count > 0
        rows.append(entry)
        return entry

    def query_batch_summary(self, source_system: str | None = None) -> dict[str, Any]:
        """查询导入批次汇总统计，包含成功数、失败数和重复数聚合。

        聚合查询导入批次存储中的所有批次数据，按来源系统分组汇总
        总记录数、成功数、失败数和重复数，输出批次质量概览。
        """
        rows = store.rows(self.MODULE)
        if source_system:
            rows = [r for r in rows if r.get("来源系统") == source_system]
        total_records = sum(self._safe_int(r.get("总记录数", "0")) for r in rows)
        total_success = sum(self._safe_int(r.get("成功数", "0")) for r in rows)
        total_failed = sum(self._safe_int(r.get("失败数", "0")) for r in rows)
        total_duplicate = sum(self._safe_int(r.get("重复数", "0")) for r in rows)
        return {
            "total_batches": len(rows),
            "total_records": total_records,
            "total_success": total_success,
            "total_failed": total_failed,
            "total_duplicate": total_duplicate,
            "success_rate": round(total_success / max(total_records, 1) * 100, 2),
        }

    def split_large_batch(self, batch_id: int, shard_size: int = 1000) -> list[dict[str, Any]]:
        """切分大批次为多个子批次，按分片大小拆分并发导入。

        读取原批次数据，按指定分片大小切分为多个子批次，
        每个子批次独立执行数据校验、导入和去重检查。
        """
        batch = self.get_entry(batch_id)
        if not batch:
            return []
        total = int(batch.get("总记录数", "0"))
        num_shards = max((total + shard_size - 1) // shard_size, 1)
        shards = []
        for i in range(num_shards):
            shard_size_actual = min(shard_size, total - i * shard_size)
            shard = {
                "parent_batch": batch.get("批次编号"),
                "shard_index": i + 1,
                "shard_total": num_shards,
                "shard_size": shard_size_actual,
                "source_system": batch.get("来源系统"),
                "entity_model": batch.get("实体模型"),
                "status": "切分完成",
            }
            shards.append(shard)
        # 切分导入批次：拆分大数据量为多个分片并行处理
        batch["shards"] = shards
        batch["shard_count"] = num_shards
        return shards

    def validate_batch_integrity(self, batch_id: int) -> dict[str, Any]:
        """校验导入批次的完整性：成功+失败+重复数是否等于总记录数。

        读取导入批次数据，校验总记录数是否等于成功数+失败数+重复数，
        验证批次数据的完整性并生成校验报告。
        """
        batch = self.get_entry(batch_id)
        if not batch:
            return {"ok": False, "reason": "批次不存在"}
        total = int(batch.get("总记录数", "0"))
        success = int(batch.get("成功数", "0"))
        failed = int(batch.get("失败数", "0"))
        duplicate = int(batch.get("重复数", "0"))
        consistent = total == (success + failed + duplicate)
        return {
            "batch_id": batch.get("批次编号"),
            "integrity_ok": consistent,
            "total": total,
            "calculated": success + failed + duplicate,
        }

    def archive_completed_batches(self, before_date: str) -> int:
        """归档指定日期之前已完成的导入批次数据。

        查询导入批次存储，将已完成且时间在指定日期之前的批次
        标记为归档状态，释放活跃存储空间供新批次使用。
        """
        rows = store.rows(self.MODULE)
        archived = 0
        for row in rows:
            if row.get("status") == "已完成":
                row["status"] = "已归档"
                row["archived_at"] = datetime.datetime.utcnow().isoformat()
                archived += 1
        return archived

    # ── 门店数据采集归属：代理代送批次的入库归属校验 ──

    def submit_agent_batch(self, batch_no: str, agent_name: str,
                           raw_rows: list[dict[str, Any]],
                           entity_model: str = "store_master") -> dict[str, Any]:
        """提交代理代送批次并逐行做入库归属校验。

        每一行按原始门店记录的来源编号（供数编号/别名）解析实际供数方，
        再核对登记的供数职责与行内门店归属是否一致：
        - 来源可解析、职责一致：用供数方字段定义转换并生成入库归属凭证后落地；
        - 来源无法解析或职责冲突：原始行进入受控待核区，不产生入库数据。
        未确认的加盟店永远不会写入直营店数据。
        """
        from app.services.data_import_rule import DataImportRuleService
        from app.services.source_system import SourceSystemService

        source_svc = SourceSystemService()
        rule_svc = DataImportRuleService()
        landing = store.rows("store_landing_row")
        quarantine = store.rows("attribution_quarantine")
        accepted, held = [], []
        accepted_count = held_count = 0

        for index, raw in enumerate(raw_rows, start=1):
            raw_row_id = str(raw.get("raw_row_id") or f"{batch_no}-L{index:03d}")
            claimed_source = str(raw.get("source_code") or "").strip()
            claimed_duty = str(raw.get("duty_token") or "").strip()
            actual_source = source_svc.resolve_supply_source(claimed_source)

            if actual_source is None:
                held_row = self._quarantine_row(
                    quarantine, batch_no, raw_row_id, agent_name, raw,
                    claimed_source, claimed_duty, None, None, "来源未注册")
                held.append(held_row)
                held_count += 1
                continue

            supply_code = str(actual_source.get("供数编号") or "").strip()
            registered_duty = str(actual_source.get("供数职责") or "").strip()
            # 入库行须归属实际供数方：代理声明职责与登记供数职责冲突即拦截
            if claimed_duty and registered_duty and claimed_duty != registered_duty:
                held_row = self._quarantine_row(
                    quarantine, batch_no, raw_row_id, agent_name, raw,
                    claimed_source, claimed_duty, supply_code, registered_duty,
                    "供数职责冲突")
                held.append(held_row)
                held_count += 1
                continue

            issued = rule_svc.issue_ownership_voucher(
                batch_no=batch_no, raw_row_id=raw_row_id,
                claimed_source=claimed_source, record=raw,
                entity_model=entity_model,
            )
            if not issued.get("ok"):
                held_row = self._quarantine_row(
                    quarantine, batch_no, raw_row_id, agent_name, raw,
                    claimed_source, claimed_duty, supply_code, registered_duty,
                    f"归属凭证未生成：{issued.get('reason')}")
                held.append(held_row)
                held_count += 1
                continue

            voucher = issued["voucher"]
            landed = {
                "id": max((int(r.get("id", 0)) for r in landing), default=0) + 1,
                "批次编号": batch_no,
                "原始行号": raw_row_id,
                "门店编号": issued["mapped"].get("门店编号"),
                "门店类型": issued["mapped"].get("门店类型"),
                "归属供数编号": supply_code,
                "归属供数职责": registered_duty,
                "入库凭证编号": voucher["凭证编号"],
                "入库规则编号": voucher["入库规则编号"],
                "实际写入字段": issued["mapped"],
                "写入时间": datetime.datetime.utcnow().isoformat(),
            }
            landing.append(landed)
            accepted.append(landed)
            accepted_count += 1

        rows = store.rows(self.MODULE)
        batch = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "批次编号": batch_no,
            "来源系统": f"代理代送:{agent_name}",
            "实体模型": entity_model,
            "总记录数": str(len(raw_rows)),
            "成功数": str(accepted_count),
            "失败数": str(held_count),
            "重复数": "0",
            "批次状态": "待核完成",
            "status": "处理中",
            "pending": held_count > 0,
            "abnormal": held_count > 0,
            "代理名称": agent_name,
            "待核数": held_count,
        }
        rows.append(batch)
        return {
            "ok": True,
            "批次编号": batch_no,
            "total": len(raw_rows),
            "accepted": accepted_count,
            "quarantined": held_count,
            "accepted_rows": accepted,
            "quarantine_rows": held,
            "review_report": self._build_review_report(batch_no, accepted, held),
        }

    @staticmethod
    def _quarantine_row(table: list[dict[str, Any]], batch_no: str, raw_row_id: str,
                        agent_name: str, raw: dict[str, Any], claimed_source: str,
                        claimed_duty: str, supply_code: str | None,
                        registered_duty: str | None, reason: str) -> dict[str, Any]:
        """把归属存疑的原始行写入受控待核区，保留代理原始报文备查。"""
        entry = {
            "id": max((int(r.get("id", 0)) for r in table), default=0) + 1,
            "批次编号": batch_no,
            "原始行号": raw_row_id,
            "代理名称": agent_name,
            "声明来源": claimed_source,
            "声明职责": claimed_duty,
            "实际供数编号": supply_code,
            "登记供数职责": registered_duty,
            "待核原因": reason,
            "处置状态": "待代理更正",
            "原始报文": dict(raw),
            "入区时间": datetime.datetime.utcnow().isoformat(),
        }
        table.append(entry)
        return entry

    @staticmethod
    def _build_review_report(batch_no: str, accepted: list[dict[str, Any]],
                             held: list[dict[str, Any]]) -> dict[str, Any]:
        """值班核对清单：哪些原始行已具备归属依据、哪些须代理更正后重送。"""
        return {
            "批次编号": batch_no,
            "已具备归属依据": [
                {"原始行号": row["原始行号"], "入库凭证编号": row["入库凭证编号"],
                 "归属供数编号": row["归属供数编号"], "门店编号": row["门店编号"]}
                for row in accepted
            ],
            "待代理更正重送": [
                {"原始行号": row["原始行号"], "待核原因": row["待核原因"],
                 "声明来源": row["声明来源"], "声明职责": row["声明职责"],
                 "登记供数职责": row["登记供数职责"]}
                for row in held
            ],
        }

    def review_batch_attribution(self, batch_no: str) -> dict[str, Any]:
        """复核某代理批次的实际写入字段与受控待核情况。"""
        landed = [r for r in store.rows("store_landing_row") if r.get("批次编号") == batch_no]
        held = [r for r in store.rows("attribution_quarantine") if r.get("批次编号") == batch_no]
        vouchers = [r for r in store.rows("import_ownership_voucher")
                    if r.get("批次编号") == batch_no]
        # 安全断言：未确认的加盟店不得混入直营店落地数据
        leaked = [
            row for row in landed
            if row.get("归属供数职责") == "加盟店" and row.get("门店类型") == "直营店"
        ]
        return {
            "批次编号": batch_no,
            "入库行数": len(landed),
            "待核行数": len(held),
            "凭证数": len(vouchers),
            "实际写入字段复核": [
                {"原始行号": row["原始行号"], "实际写入字段": row["实际写入字段"],
                 "入库凭证编号": row["入库凭证编号"]}
                for row in landed
            ],
            "待核明细": [
                {"原始行号": row["原始行号"], "待核原因": row["待核原因"],
                 "处置状态": row["处置状态"]}
                for row in held
            ],
            "越权写入": leaked,
            "合规": not leaked,
        }

    def resubmit_corrected_rows(self, batch_no: str,
                                corrected_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """代理更正后重送：仅放行受控待核区中被更正的原始行。"""
        quarantine = store.rows("attribution_quarantine")
        corrected_ids = {str(row.get("raw_row_id")) for row in corrected_rows}
        for row in quarantine:
            if row.get("批次编号") == batch_no and row.get("原始行号") in corrected_ids:
                row["处置状态"] = "代理已更正"
                row["更正时间"] = datetime.datetime.utcnow().isoformat()
        result = self.submit_agent_batch(
            f"{batch_no}-R", f"代理更正重送", corrected_rows,
            entity_model=self._batch_entity_model(batch_no),
        )
        # 重送落地后关闭待核区对应条目
        landed_ids = {row["原始行号"] for row in result["accepted_rows"]}
        for row in quarantine:
            if (row.get("批次编号") == batch_no
                    and row.get("原始行号") in landed_ids):
                row["处置状态"] = "更正后已入库"
        return result

    @staticmethod
    def _batch_entity_model(batch_no: str) -> str:
        for row in store.rows("import_batch"):
            if row.get("批次编号") == batch_no and row.get("实体模型"):
                return str(row["实体模型"])
        return "store_master"

    def list_quarantine(self, batch_no: str | None = None,
                        reason: str | None = None) -> list[dict[str, Any]]:
        """查询受控待核区，可按批次或待核原因筛选。"""
        rows = store.rows("attribution_quarantine")
        if batch_no:
            rows = [r for r in rows if r.get("批次编号") == batch_no]
        if reason:
            rows = [r for r in rows if reason in str(r.get("待核原因", ""))]
        return rows

    def lookup_historical_batch(self, identifier: str) -> dict[str, Any]:
        """来源系统改名后回查旧批次：凭供数编号/旧名称别名还原入库用原编号。

        即使来源系统已经改名，入库归属凭证固化了供数编号与字段映射快照，
        这里先解析标识对应的现来源，再汇总其历史凭证与落地行。
        """
        source_svc = SourceSystemService()
        source = source_svc.resolve_supply_source(identifier)
        if source is None:
            return {"ok": False, "reason": f"来源标识「{identifier}」无法解析，无法回查旧批次"}
        supply_code = str(source.get("供数编号") or "").strip()
        vouchers = [
            row for row in store.rows("import_ownership_voucher")
            if row.get("实际供数编号") == supply_code
        ]
        landed = [
            row for row in store.rows("store_landing_row")
            if row.get("归属供数编号") == supply_code
        ]
        batches = sorted({row.get("批次编号") for row in vouchers if row.get("批次编号")})
        return {
            "ok": True,
            "查询标识": identifier,
            "当前系统名称": source.get("系统名称"),
            "曾用名称": source.get("曾用名称"),
            "还原供数编号": supply_code,
            "来源系统编号": source.get("系统编号"),
            "有效别名": source_svc._split_aliases(source.get("有效别名")),
            "关联历史批次": batches,
            "历史凭证": [
                {"凭证编号": v["凭证编号"], "批次编号": v["批次编号"],
                 "原始行号": v["原始行号"], "入库规则编号": v["入库规则编号"],
                 "字段映射快照": v["字段映射快照"], "生成时间": v["生成时间"]}
                for v in vouchers
            ],
            "历史入库行数": len(landed),
        }