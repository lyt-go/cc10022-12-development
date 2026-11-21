"""门店数据采集归属校验场景测试。

覆盖代理代送批次的入库归属凭证链路：
1. 来源系统注册提供供数编号及有效别名、登记供数职责；
2. 数据入库规则在落库前关联实际来源并生成入库归属凭证；
3. 同一代理批含直营/加盟两个来源，错标的加盟店进入受控待核区而非直营数据；
4. 值班复核实际写入字段、确认待代理更正重送的原始行；
5. 来源系统改名后，旧凭证仍能还原入库用的原编号并回查旧批次；
6. 代理更正后重送，待核行落地并关闭待核条目。

直接 `python app/tests/test_attribution_chain.py` 或 pytest 均可运行。
"""
from __future__ import annotations

import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.data_import_rule import DataImportRuleService
from app.services.import_batch import ImportBatchService
from app.services.source_system import SourceSystemService
from app.store import store


class TestAttributionChain:
    """代理代送门店数据的入库归属校验链路。"""

    def test_alias_resolution_and_duty_registration(self):
        """供数编号与有效别名都能解析到同一实际来源及其供数职责。"""
        svc = SourceSystemService()
        direct = svc.resolve_supply_source("SUP-DIRECT")
        assert direct is not None, "种子数据应已注册直营供数方"
        assert direct["供数职责"] == "直营店"
        # 别名解析
        assert svc.resolve_supply_source("DIRECT")["供数编号"] == "SUP-DIRECT"
        assert svc.lookup_supply_duty("SUP-FRANCHISE") == "加盟店"
        assert svc.lookup_supply_duty("FRANCHISE") == "加盟店"
        # 未注册标识不可解析
        assert svc.resolve_supply_source("NOT-REGISTERED") is None

    def test_agent_batch_attribution_quarantine(self):
        """同一代理批两个来源：直营放行，错标成直营的加盟店进受控待核区。"""
        batch_svc = ImportBatchService()
        batch_no = "IMPO-AGENT-20261121-01"
        raw_rows = [
            # R1：直营店，来源标识正确，职责一致 → 落地
            {"raw_row_id": "R1", "source_code": "SUP-DIRECT", "duty_token": "直营店",
             "store_code": "D001", "store_name": "城东直营店",
             "store_type": "直营店", "region": "华东"},
            # R2：加盟店，正确使用加盟供数编号 → 落地
            {"raw_row_id": "R2", "source_code": "SUP-FRANCHISE", "duty_token": "加盟店",
             "shop_no": "F001", "shop_title": "城西加盟店",
             "shop_mode": "加盟店", "area": "华西"},
            # R3：加盟店被代送服务错标成直营：来源是加盟供数方，却声明直营职责 → 待核
            {"raw_row_id": "R3", "source_code": "SUP-FRANCHISE", "duty_token": "直营店",
             "shop_no": "F002", "shop_title": "城南加盟店",
             "shop_mode": "直营店", "area": "华南"},
            # R4：来源编号无法解析 → 待核，请代理更正来源标识后重送
            {"raw_row_id": "R4", "source_code": "SUP-UNKNOWN", "duty_token": "加盟店",
             "shop_no": "F003", "shop_title": "城北加盟店",
             "shop_mode": "加盟店", "area": "华北"},
        ]
        result = batch_svc.submit_agent_batch(batch_no, "华通代送服务", raw_rows)

        assert result["total"] == 4
        assert result["accepted"] == 2, "仅 R1/R2 具备归属依据"
        assert result["quarantined"] == 2, "R3/R4 必须进入受控待核区"

        report = result["review_report"]
        accepted_ids = {row["原始行号"] for row in report["已具备归属依据"]}
        held_ids = {row["原始行号"] for row in report["待代理更正重送"]}
        assert accepted_ids == {"R1", "R2"}
        assert held_ids == {"R3", "R4"}
        held_reasons = {row["原始行号"]: row["待核原因"]
                        for row in report["待代理更正重送"]}
        assert "职责冲突" in held_reasons["R3"]
        assert "未注册" in held_reasons["R4"]

        # 未确认的加盟店不应进入直营数据
        landed = store.rows("store_landing_row")
        batch_landed = [r for r in landed if r["批次编号"] == batch_no]
        assert {r["原始行号"] for r in batch_landed} == {"R1", "R2"}
        assert all(r["门店类型"] != "加盟店" or r["归属供数职责"] != "直营店"
                   for r in batch_landed), "加盟店不得借直营通道入库"
        direct_landed = [r for r in batch_landed if r["归属供数职责"] == "直营店"]
        assert all(r["门店类型"] == "直营店" for r in direct_landed)

    def test_ownership_voucher_proves_supplier_field_definition(self):
        """入库归属凭证须证明最终转换实际使用了哪个供数方的字段定义。"""
        rule_svc = DataImportRuleService()
        issued = rule_svc.issue_ownership_voucher(
            batch_no="IMPO-VOUCHER-CHECK", raw_row_id="V1",
            claimed_source="FRANCHISE",  # 用别名而非编号，验证落库前关联实际来源
            record={"shop_no": "F900", "shop_title": "凭证加盟店",
                    "shop_mode": "加盟店", "area": "华中"},
        )
        assert issued["ok"] is True
        voucher = issued["voucher"]
        assert voucher["实际供数编号"] == "SUP-FRANCHISE"
        assert voucher["入库规则编号"] == "DATA-1005"
        # 加盟方字段定义 shop_no->门店编号 被实际采用
        assert voucher["实际写入字段"]["门店编号"] == "F900"
        assert "shop_no->门店编号" in voucher["字段映射快照"]
        assert voucher["供数职责"] == "加盟店"

        # 未注册来源无法出凭证
        rejected = rule_svc.issue_ownership_voucher(
            batch_no="IMPO-VOUCHER-CHECK", raw_row_id="V2",
            claimed_source="GHOST", record={"store_code": "X"},
        )
        assert rejected["ok"] is False
        assert rejected["reason"] == "来源未注册"

    def test_review_written_fields_and_compliance(self):
        """值班复核：实际写入字段可追溯凭证，且无越权写入。"""
        batch_svc = ImportBatchService()
        review = batch_svc.review_batch_attribution("IMPO-AGENT-20261121-01")
        assert review["入库行数"] == 2
        assert review["待核行数"] == 2
        assert review["凭证数"] == 2
        assert review["合规"] is True
        assert review["越权写入"] == []
        fields_by_row = {row["原始行号"]: row["实际写入字段"]
                         for row in review["实际写入字段复核"]}
        # R1 走直营字段定义（store_code），R2 走加盟字段定义（shop_no）
        assert fields_by_row["R1"]["门店编号"] == "D001"
        assert fields_by_row["R2"]["门店编号"] == "F001"
        # 每个落地行都能对上凭证
        vouchers = {v["原始行号"]: v["凭证编号"]
                    for v in store.rows("import_ownership_voucher")
                    if v["批次编号"] == "IMPO-AGENT-20261121-01"}
        landed = [r for r in store.rows("store_landing_row")
                  if r["批次编号"] == "IMPO-AGENT-20261121-01"]
        assert all(r["入库凭证编号"] in vouchers.values() for r in landed)

    def test_rename_source_then_lookup_historical_batch(self):
        """来源改名后旧名称成为别名，旧凭证仍可还原入库原编号并回查旧批次。"""
        source_svc = SourceSystemService()
        rename = source_svc.rename_source("SUP-FRANCHISE", "加盟门店数据中台(新)")
        assert rename["ok"] is True
        assert rename["旧名称"] == "加盟店数据平台"
        assert rename["供数编号"] == "SUP-FRANCHISE"

        # 旧名称仍可解析回同一供数方
        by_old_name = source_svc.resolve_supply_source("加盟店数据平台")
        assert by_old_name is not None
        assert by_old_name["供数编号"] == "SUP-FRANCHISE"
        # 新名称与别名均可解析
        assert source_svc.resolve_supply_source("加盟门店数据中台(新)")["供数编号"] == "SUP-FRANCHISE"
        assert source_svc.resolve_supply_source("FRANCHISE")["供数编号"] == "SUP-FRANCHISE"

        # 改名后回查旧批次：凭证固化的原编号与字段映射快照不变
        batch_svc = ImportBatchService()
        history = batch_svc.lookup_historical_batch("加盟店数据平台")
        assert history["ok"] is True
        assert history["还原供数编号"] == "SUP-FRANCHISE"
        assert "IMPO-AGENT-20261121-01" in history["关联历史批次"]
        assert history["历史入库行数"] >= 1
        certs = history["历史凭证"]
        assert any(c["字段映射快照"].startswith("shop_no->门店编号") for c in certs)
        assert all(c["入库规则编号"] == "DATA-1005" for c in certs)

    def test_corrected_resubmission_releases_quarantine(self):
        """代理更正 R3/R4 后重送：待核行落地，待核条目关闭，仍不混入直营数据。"""
        batch_svc = ImportBatchService()
        batch_no = "IMPO-AGENT-20261121-01"
        corrected = [
            # R3 更正：职责改回加盟店（来源本就是加盟供数方）
            {"raw_row_id": "R3", "source_code": "SUP-FRANCHISE", "duty_token": "加盟店",
             "shop_no": "F002", "shop_title": "城南加盟店",
             "shop_mode": "加盟店", "area": "华南"},
            # R4 更正：补上有效供数编号
            {"raw_row_id": "R4", "source_code": "FRANCHISE", "duty_token": "加盟店",
             "shop_no": "F003", "shop_title": "城北加盟店",
             "shop_mode": "加盟店", "area": "华北"},
        ]
        result = batch_svc.resubmit_corrected_rows(batch_no, corrected)
        assert result["accepted"] == 2
        assert result["quarantined"] == 0

        # 原待核区条目已关闭
        quarantine = batch_svc.list_quarantine(batch_no)
        assert all(row["处置状态"] == "更正后已入库" for row in quarantine)

        # 更正后的加盟店全部归属加盟供数方，直营数据中没有加盟店
        review = batch_svc.review_batch_attribution(batch_no)
        assert review["合规"] is True
        direct_rows = [r for r in store.rows("store_landing_row")
                       if r["归属供数职责"] == "直营店"]
        assert all(r["门店类型"] == "直营店" for r in direct_rows)
        franchise_landed = [r for r in store.rows("store_landing_row")
                            if r["原始行号"] in {"R3", "R4"}]
        assert all(r["归属供数编号"] == "SUP-FRANCHISE" for r in franchise_landed)
        assert all(r["入库凭证编号"] for r in franchise_landed)


if __name__ == "__main__":
    runner = TestAttributionChain()
    tests = [
        ("别名解析与供数职责登记", runner.test_alias_resolution_and_duty_registration),
        ("代理批次归属校验与受控待核", runner.test_agent_batch_attribution_quarantine),
        ("入库归属凭证证明供数方字段定义", runner.test_ownership_voucher_proves_supplier_field_definition),
        ("值班复核实际写入字段与合规性", runner.test_review_written_fields_and_compliance),
        ("来源改名后回查旧批次还原原编号", runner.test_rename_source_then_lookup_historical_batch),
        ("代理更正后重送释放待核行", runner.test_corrected_resubmission_releases_quarantine),
    ]
    passed = failed = 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001 - 场景脚本需要暴露全部失败
            print(f"  FAIL: {name} -> {exc}")
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"结果: {passed}/{len(tests)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 条归属链路测试失败！"
