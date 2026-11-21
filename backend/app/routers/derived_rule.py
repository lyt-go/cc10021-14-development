"""派生规则领域接口：参数校验、编译/恢复、权限范围复核、共用默认调整重编译。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.derived_rule import (
    DerivedRuleError,
    DqDerivedRuleService,
)

router = APIRouter(prefix="/api", tags=["派生规则治理"])


class ValidatePayload(BaseModel):
    template_id: int
    param_values: dict[str, Any] = Field(default_factory=dict)


class CompilePayload(BaseModel):
    template_id: int
    domain: str
    table_name: str
    column: str
    bindings: dict[str, Any] = Field(default_factory=dict)
    caller_team: str | None = None
    interrupt: bool = False


class ReviewPayload(BaseModel):
    reviewer_team: str
    conclusion: str
    note: str = ""


class SharedChangePayload(BaseModel):
    family: str
    param_name: str
    new_default: str
    new_version: str
    caller_team: str


def _derived_service() -> DqDerivedRuleService:
    return DqDerivedRuleService()


@router.post("/dq_derived_rule/validate-params")
def validate_params(payload: ValidatePayload) -> dict[str, Any]:
    """新增模板参数校验：按参数规格校验必填、类型与占位符一致性。"""
    try:
        return _derived_service().validate_template_params(
            payload.template_id, payload.param_values
        )
    except DerivedRuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/dq_derived_rule/compile")
def compile_derived(payload: CompilePayload) -> dict[str, Any]:
    """编译带归属与版本的派生规则，结果回写派生复核清单与质量规则绑定。"""
    try:
        return _derived_service().compile_derived_rule(
            payload.template_id,
            payload.domain,
            payload.table_name,
            payload.column,
            bindings=payload.bindings,
            caller_team=payload.caller_team,
            interrupt=payload.interrupt,
        )
    except DerivedRuleError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/dq_compile_session/{session_id}/resume")
def resume_compile(session_id: int) -> dict[str, Any]:
    """编译中断恢复：恢复时识别当次编译来自继承还是本域覆盖。"""
    try:
        return _derived_service().resume_compile(session_id)
    except DerivedRuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/dq_derived_rule/scope/list")
def list_scoped_derived_rules(team: str | None = None,
                              domain: str | None = None) -> dict[str, Any]:
    """按权限范围核对：共享模板组看继承口径，本域团队看本域归属。"""
    items = _derived_service().list_derived_rules(team=team, domain=domain)
    return {"items": items, "total": len(items), "scope_team": team, "scope_domain": domain}


@router.post("/dq_derived_rule/{derived_id}/review")
def review_derived(derived_id: int, payload: ReviewPayload) -> dict[str, Any]:
    """派生复核清单复核动作，复核人只能操作自己权限范围内的派生规则。"""
    try:
        return _derived_service().review_derived_rule(
            derived_id, payload.reviewer_team, payload.conclusion, payload.note
        )
    except DerivedRuleError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/dq_rule_def/bindings")
def list_rule_bindings(team: str | None = None) -> dict[str, Any]:
    """质量规则实际绑定的表和字段登记，按团队权限范围返回。"""
    items = _derived_service().list_rule_bindings(team=team)
    return {"items": items, "total": len(items), "scope_team": team}


@router.post("/dq_template/shared-change/recompile")
def shared_change_recompile(payload: SharedChangePayload) -> dict[str, Any]:
    """共用字段默认值调整：发布模板新版本，按各域参数重编译并对比SQL与派生版本。"""
    try:
        return _derived_service().change_shared_default_and_recompile(
            payload.family,
            payload.param_name,
            payload.new_default,
            payload.new_version,
            payload.caller_team,
        )
    except DerivedRuleError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
