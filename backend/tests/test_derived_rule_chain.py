"""派生规则治理链路测试。

覆盖需求点：
1. 模板参数校验 + 带归属/版本的派生规则数据
2. 同一模板两个版本对同一目标字段给出不同共用默认值
3. A 域（交易域）本域覆盖该参数，B 域（用户域）只继承共用默认值
4. 有归属的目标表不能被共享模板越权修改，参数与版本可回查
5. 编译中断恢复时识别当次编译来自继承还是本域覆盖
6. 编译结果回写派生复核清单；质量规则登记实际绑定的表和字段；
   共享模板团队与本域团队各按权限范围核对
7. 两域各自参数生成规则，共用字段调整前后比较可运行 SQL 与需复核的派生版本
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.derived_rule import (  # noqa: E402
    COMPILE_FROM_INHERIT,
    COMPILE_FROM_OVERRIDE,
    SHARED_TEAM,
    SOURCE_BINDING,
    SOURCE_INHERITED,
    SOURCE_OVERRIDE,
    DerivedRuleError,
    DqDerivedRuleService,
)
from app.store import store  # noqa: E402

FAMILY = "shared_null_check"
TPL_V1 = 101   # threshold 共用默认 100
TPL_V2 = 102   # threshold 共用默认 50
TRADE_DOMAIN = "交易域"
TRADE_TEAM = "交易域质量组"
USER_DOMAIN = "用户域"
USER_TEAM = "用户域质量组"


def _service() -> DqDerivedRuleService:
    return DqDerivedRuleService()


def _by_id(rows, row_id):
    return next(r for r in rows if r["id"] == row_id)


class TestTemplateParamValidation:
    """需求1：新增模板参数校验。"""

    def test_valid_params_pass(self):
        result = _service().validate_template_params(
            TPL_V1, {"table": "dwd_trade_order", "column": "amount", "threshold": "200"}
        )
        assert result["校验结果"] == "通过"
        assert result["模板版本"] == "v1.0"

    def test_invalid_number_type_rejected(self):
        result = _service().validate_template_params(
            TPL_V1, {"table": "t", "column": "c", "threshold": "abc"}
        )
        assert result["校验结果"] == "不通过"
        assert any("threshold" in issue and "数值" in issue for issue in result["问题详情"])

    def test_unknown_param_rejected(self):
        result = _service().validate_template_params(
            TPL_V1, {"table": "t", "column": "c", "not_declared": "x"}
        )
        assert any("未在模板声明" in issue for issue in result["问题详情"])

    def test_missing_required_identifier_rejected(self):
        result = _service().validate_template_params(TPL_V1, {"table": "t"})
        assert any("column" in issue and "必填" in issue for issue in result["问题详情"])

    def test_illegal_identifier_rejected(self):
        result = _service().validate_template_params(
            TPL_V1, {"table": "t; DROP TABLE x", "column": "c"}
        )
        assert any("标识符" in issue for issue in result["问题详情"])

    def test_rule_param_cannot_enter_via_binding(self):
        with pytest.raises(DerivedRuleError) as exc:
            _service().compile_derived_rule(
                TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "amount",
                bindings={"table": "dwd_trade_order", "column": "amount", "threshold": "9"},
                caller_team=TRADE_TEAM,
            )
        assert "不能通过绑定传入" in str(exc.value)


class TestTwoTemplateVersionsDefaults:
    """需求2：同一模板两个版本对同一目标字段 threshold 给出不同共用默认值。"""

    def test_two_versions_carry_different_shared_defaults(self):
        v1 = _by_id(store.rows("dq_template"), TPL_V1)
        v2 = _by_id(store.rows("dq_template"), TPL_V2)
        spec1 = next(s for s in v1["参数规格"] if s["参数名"] == "threshold")
        spec2 = next(s for s in v2["参数规格"] if s["参数名"] == "threshold")
        assert spec1["共用默认值"] == "100"
        assert spec2["共用默认值"] == "50"
        assert v1["模板族"] == v2["模板族"] == FAMILY
        assert v1["模板版本"] != v2["模板版本"]


class TestDomainProvenanceCompilation:
    """需求3：交易域本域覆盖 threshold；用户域只继承共用默认。"""

    def test_trade_domain_uses_override_with_override_version(self):
        result = _service().compile_derived_rule(
            TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "amount",
            bindings={"table": "dwd_trade_order", "column": "amount"},
            caller_team=TRADE_TEAM,
        )
        assert result["编译来源"] == COMPILE_FROM_OVERRIDE
        assert result["覆盖版本"] == "ov-v1"
        threshold = next(p for p in result["参数明细"] if p["参数名"] == "threshold")
        assert threshold["参数值"] == "200"          # 本域覆盖值，不是共用默认 100
        assert threshold["来源"] == SOURCE_OVERRIDE
        assert threshold["来源版本"] == "ov-v1"
        assert "amount >= 200" in result["编译SQL"]
        # 标识参数标注为本域绑定
        table_param = next(p for p in result["参数明细"] if p["参数名"] == "table")
        assert table_param["来源"] == SOURCE_BINDING

    def test_user_domain_inherits_shared_default_with_template_version(self):
        result = _service().compile_derived_rule(
            TPL_V1, USER_DOMAIN, "dws_user_profile", "age",
            bindings={"table": "dws_user_profile", "column": "age"},
            caller_team=USER_TEAM,
        )
        assert result["编译来源"] == COMPILE_FROM_INHERIT
        assert result["覆盖版本"] == "inherit"
        threshold = next(p for p in result["参数明细"] if p["参数名"] == "threshold")
        assert threshold["参数值"] == "100"          # v1.0 共用默认
        assert threshold["来源"] == SOURCE_INHERITED
        assert threshold["来源版本"] == "v1.0"       # 版本可回查到模板版本
        assert "age >= 100" in result["编译SQL"]

    def test_same_param_differs_across_domains_in_runnable_sql(self):
        trade = _service().compile_derived_rule(
            TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "order_id",
            caller_team=TRADE_TEAM,
        )
        user = _service().compile_derived_rule(
            TPL_V1, USER_DOMAIN, "dws_user_profile", "user_id",
            caller_team=USER_TEAM,
        )
        assert "order_id >= 200" in trade["编译SQL"]
        assert "user_id >= 100" in user["编译SQL"]
        assert trade["编译SQL"] != user["编译SQL"]

    def test_user_domain_picks_up_v2_shared_default_50(self):
        result = _service().compile_derived_rule(
            TPL_V2, USER_DOMAIN, "dws_user_profile", "age",
            bindings={"table": "dws_user_profile", "column": "age"},
            caller_team=USER_TEAM,
        )
        threshold = next(p for p in result["参数明细"] if p["参数名"] == "threshold")
        assert threshold["参数值"] == "50"           # 新版共用默认
        assert "age >= 50" in result["编译SQL"]
        assert result["模板版本"] == "v2.0"


class TestOwnershipPermission:
    """需求4：有归属的目标表不能被共享模板越权修改。"""

    def test_shared_team_cannot_modify_owned_table(self):
        with pytest.raises(DerivedRuleError) as exc:
            _service().compile_derived_rule(
                TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "amount",
                caller_team=SHARED_TEAM,
            )
        assert "越权拦截" in str(exc.value)
        assert "共享模板" in str(exc.value)

    def test_cross_domain_binding_blocked(self):
        with pytest.raises(DerivedRuleError) as exc:
            _service().compile_derived_rule(
                TPL_V1, USER_DOMAIN, "dwd_trade_order", "amount",
                caller_team=USER_TEAM,
            )
        assert "无权绑定" in str(exc.value)

    def test_unregistered_column_blocked(self):
        with pytest.raises(DerivedRuleError) as exc:
            _service().compile_derived_rule(
                TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "secret_col",
                caller_team=TRADE_TEAM,
            )
        assert "可绑定字段清单" in str(exc.value)

    def test_unowned_table_allowed_with_domain_claim(self):
        result = _service().compile_derived_rule(
            TPL_V1, "运营域", "tmp_adhoc_table", "metric",
            caller_team="运营域质量组",
        )
        assert result["归属信息"]["是否受归属保护"] is False


class TestCompileInterruptResume:
    """需求5：编译中断恢复，仍识别当次编译来源（继承/本域覆盖）。"""

    def test_interrupt_then_resume_preserves_override_source(self):
        svc = _service()
        interrupted = svc.compile_derived_rule(
            TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "amount",
            caller_team=TRADE_TEAM, interrupt=True,
        )
        assert interrupted["状态"] == "中断"
        assert interrupted["已识别编译来源"] == COMPILE_FROM_OVERRIDE

        session = next(
            s for s in store.rows("dq_compile_session")
            if s["会话编号"] == interrupted["会话编号"]
        )
        assert session["状态"] == "中断"
        assert session["编译来源"] == COMPILE_FROM_OVERRIDE

        resumed = svc.resume_compile(session["id"])
        assert resumed["恢复识别"]["来源一致"] is True
        assert resumed["恢复识别"]["会话固化来源"] == COMPILE_FROM_OVERRIDE
        assert resumed["编译来源"] == COMPILE_FROM_OVERRIDE
        assert "amount >= 200" in resumed["编译SQL"]
        # 恢复完成后会话关闭，不能二次恢复
        with pytest.raises(DerivedRuleError):
            svc.resume_compile(session["id"])

    def test_interrupt_then_resume_preserves_inherited_source(self):
        svc = _service()
        interrupted = svc.compile_derived_rule(
            TPL_V1, USER_DOMAIN, "dws_user_profile", "age",
            caller_team=USER_TEAM, interrupt=True,
        )
        assert interrupted["已识别编译来源"] == COMPILE_FROM_INHERIT
        session = next(
            s for s in store.rows("dq_compile_session")
            if s["会话编号"] == interrupted["会话编号"]
        )
        resumed = svc.resume_compile(session["id"])
        assert resumed["恢复识别"]["来源一致"] is True
        assert resumed["编译来源"] == COMPILE_FROM_INHERIT
        assert "age >= 100" in resumed["编译SQL"]


class TestReviewChecklistAndBindings:
    """需求6：复核清单回写 + 质量规则绑定登记 + 分团队权限核对。"""

    def test_compile_writes_review_checklist_entry(self):
        result = _service().compile_derived_rule(
            TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "amount",
            caller_team=TRADE_TEAM,
        )
        derived = next(
            r for r in store.rows("dq_derived_rule")
            if r["派生编号"] == result["派生编号"]
        )
        assert derived["复核状态"] == "待复核"
        assert derived["模板版本"] == "v1.0"
        assert derived["覆盖版本"] == "ov-v1"
        assert derived["派生版本"] == result["派生版本"]
        assert derived["编译SQL"] == result["编译SQL"]
        # 参数与版本可回查
        assert all("来源" in p and "来源版本" in p for p in derived["参数明细"])

    def test_quality_rule_registers_actual_table_and_field_binding(self):
        result = _service().compile_derived_rule(
            TPL_V1, USER_DOMAIN, "dws_user_profile", "age",
            caller_team=USER_TEAM,
        )
        rule = next(
            r for r in store.rows("dq_rule_def")
            if r.get("绑定派生编号") == result["派生编号"]
        )
        assert rule["目标表名"] == "dws_user_profile"
        assert rule["校验字段"] == "age"
        assert rule["绑定模板版本"] == "v1.0"
        assert rule["绑定覆盖版本"] == "inherit"
        assert rule["绑定业务域"] == USER_DOMAIN
        assert rule["绑定编译批次"] == result["编译批次"]

    def test_team_scopes_see_only_their_permission_range(self):
        svc = _service()
        svc.compile_derived_rule(TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "amount",
                                 caller_team=TRADE_TEAM)
        svc.compile_derived_rule(TPL_V1, USER_DOMAIN, "dws_user_profile", "age",
                                 caller_team=USER_TEAM)

        shared_view = svc.list_derived_rules(team=SHARED_TEAM)
        trade_view = svc.list_derived_rules(team=TRADE_TEAM)
        user_view = svc.list_derived_rules(team=USER_TEAM)

        # 共享模板组只核对继承共用默认的派生规则
        assert all(r["编译来源"] == COMPILE_FROM_INHERIT for r in shared_view)
        assert all(r["业务域"] == TRADE_DOMAIN for r in trade_view)
        assert all(r["业务域"] == USER_DOMAIN for r in user_view)
        # 两类视角互不串权
        assert {r["id"] for r in trade_view}.isdisjoint({r["id"] for r in user_view})

    def test_binding_list_uses_same_scope_rules(self):
        svc = _service()
        svc.compile_derived_rule(TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "amount",
                                 caller_team=TRADE_TEAM)
        svc.compile_derived_rule(TPL_V1, USER_DOMAIN, "dws_user_profile", "age",
                                 caller_team=USER_TEAM)
        assert all(r["绑定业务域"] == TRADE_DOMAIN
                   for r in svc.list_rule_bindings(team=TRADE_TEAM))
        assert all(r["绑定业务域"] == USER_DOMAIN
                   for r in svc.list_rule_bindings(team=USER_TEAM))

    def test_review_permission_enforced_and_status_written_back(self):
        svc = _service()
        result = svc.compile_derived_rule(
            TPL_V1, TRADE_DOMAIN, "dwd_trade_order", "amount", caller_team=TRADE_TEAM,
        )
        derived_id = next(
            r["id"] for r in store.rows("dq_derived_rule")
            if r["派生编号"] == result["派生编号"]
        )
        with pytest.raises(DerivedRuleError):
            svc.review_derived_rule(derived_id, USER_TEAM, "复核通过")
        reviewed = svc.review_derived_rule(
            derived_id, TRADE_TEAM, "复核通过", "本域参数与绑定核对无误"
        )
        assert reviewed["复核状态"] == "复核通过"
        assert reviewed["复核人"] == TRADE_TEAM
        assert reviewed["status"] == "复核通过"


class TestSharedDefaultChangeComparison:
    """需求7：共用字段调整前后，比较可运行 SQL 与需复核的派生版本。"""

    def test_change_default_recompiles_and_compares_both_domains(self):
        svc = _service()
        # 两域各自参数先编译一版（v2.0，共用默认 50）
        trade_before = svc.compile_derived_rule(
            TPL_V2, TRADE_DOMAIN, "dwd_trade_order", "amount", caller_team=TRADE_TEAM,
        )
        user_before = svc.compile_derived_rule(
            TPL_V2, USER_DOMAIN, "dws_user_profile", "age", caller_team=USER_TEAM,
        )

        report = svc.change_shared_default_and_recompile(
            FAMILY, "threshold", "10", "v3.0", SHARED_TEAM,
        )
        assert report["旧共用默认值"] == "50"
        assert report["新共用默认值"] == "10"
        assert report["新版本"] == "v3.0"

        by_target = {
            (c["业务域"], c["目标表名"], c["校验字段"]): c
            for c in report["对比结果"]
        }
        trade_cmp = by_target[(TRADE_DOMAIN, "dwd_trade_order", "amount")]
        user_cmp = by_target[(USER_DOMAIN, "dws_user_profile", "age")]

        # 交易域本域覆盖：共用默认调整不影响实际 SQL，但派生版本升级仍需复核
        assert trade_cmp["SQL是否变化"] is False
        assert trade_cmp["编译来源_变更后"] == COMPILE_FROM_OVERRIDE
        assert trade_cmp["派生版本_变更前"] != trade_cmp["派生版本_变更后"]
        assert "amount >= 200" in trade_cmp["可运行SQL_变更后"]
        assert trade_cmp["新派生复核状态"] == "待复核"

        # 用户域继承共用默认：SQL 实际变化（50→10），派生版本升级待复核
        assert user_cmp["SQL是否变化"] is True
        assert "age >= 50" in user_cmp["可运行SQL_变更前"]
        assert "age >= 10" in user_cmp["可运行SQL_变更后"]
        assert user_cmp["编译来源_变更后"] == COMPILE_FROM_INHERIT
        assert user_cmp["派生版本_变更前"] != user_cmp["派生版本_变更后"]

    def test_only_shared_team_may_change_shared_default(self):
        with pytest.raises(DerivedRuleError) as exc:
            _service().change_shared_default_and_recompile(
                FAMILY, "threshold", "1", "v9.0", TRADE_TEAM,
            )
        assert "仅共享模板组" in str(exc.value)

    def test_new_template_version_persisted_for_lookup(self):
        svc = _service()
        svc.compile_derived_rule(TPL_V2, USER_DOMAIN, "dws_user_profile", "age",
                                 caller_team=USER_TEAM)
        svc.change_shared_default_and_recompile(
            FAMILY, "threshold", "8", "v4.0", SHARED_TEAM,
        )
        versions = {
            r["模板版本"]: next(
                s for s in r["参数规格"] if s["参数名"] == "threshold"
            )["共用默认值"]
            for r in store.rows("dq_template") if r.get("模板族") == FAMILY
        }
        assert versions["v4.0"] == "8"
