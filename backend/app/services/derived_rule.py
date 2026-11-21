"""派生规则服务：模板参数校验、带归属与版本的派生规则编译、越权防护与复核回写。

核心链路：
  规则模板 (dq_template，含参数规格/共用默认值/模板版本)
    ├─ 本域参数覆盖 (dq_param_override) ── 本域覆盖来源
    ├─ 目标表归属 (dq_table_ownership) ── 越权防护边界
    ├─ 编译会话 (dq_compile_session) ── 中断/恢复，识别当次编译来源
    ├─ 派生规则复核清单 (dq_derived_rule) ── 带参数归属与版本，可回查
    └─ 质量规则 (dq_rule_def) ── 登记实际绑定的表和字段

每个参数都标注生产来源：
  - 本域绑定：table/column 等必填标识参数，由业务域按表归属绑定
  - 本域覆盖：业务域在 dq_param_override 中显式覆盖，带覆盖版本
  - 继承共用默认：未覆盖，取共享模板该版本的共用默认值，带模板版本
"""
from __future__ import annotations

import ast
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 参数生产来源
SOURCE_BINDING = "本域绑定"
SOURCE_OVERRIDE = "本域覆盖"
SOURCE_INHERITED = "继承共用默认"
# 编译来源（当次编译整体口径）
COMPILE_FROM_OVERRIDE = "本域覆盖"
COMPILE_FROM_INHERIT = "继承共用默认"

SHARED_TEAM = "共享模板组"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_container(value: Any) -> Any:
    """种子数据里 list/dict 以字符串存储，读取时还原；已是容器则原样返回。"""
    if isinstance(value, (list, dict)):
        return value
    if value in (None, ""):
        return []
    try:
        return ast.literal_eval(str(value))
    except (ValueError, SyntaxError):
        return []


def _version_key(version: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in nums) if nums else (0,)


class DerivedRuleError(Exception):
    """派生规则编译业务异常（参数校验失败、越权等）。"""


class DqParamOverrideService(BaseModuleService):
    """本域参数覆盖登记：固化某业务域对某模板版本的覆盖参数。"""

    MODULE = "dq_param_override"
    ENTITY = "参数覆盖"
    OPERATION_TERM = "登记"
    LIST_FIELDS = ["覆盖编号", "模板族", "模板版本", "业务域", "归属团队", "覆盖参数", "覆盖版本", "来源"]
    REQUIRED_FIELDS = ["覆盖编号", "模板族", "模板版本", "业务域"]
    STATUS_ORDER = ["草稿", "已生效", "已停用"]
    ACTION_RULES = {"标记生效": "已生效", "停用覆盖": "已停用"}
    NEGATIVE_ACTIONS = ["停用覆盖"]


class DqTableOwnershipService(BaseModuleService):
    """目标表归属登记：有归属的表只能由归属域绑定，共享模板不得越权修改。"""

    MODULE = "dq_table_ownership"
    ENTITY = "表归属"
    OPERATION_TERM = "登记"
    LIST_FIELDS = ["归属编号", "目标表名", "业务域", "归属团队", "绑定字段", "登记时间"]
    REQUIRED_FIELDS = ["归属编号", "目标表名", "业务域"]
    STATUS_ORDER = ["草稿", "已生效", "已下线"]
    ACTION_RULES = {"确认登记": "已生效", "标记下线": "已下线"}
    NEGATIVE_ACTIONS = ["标记下线"]


class DqCompileSessionService(BaseModuleService):
    """编译会话：记录每次编译的入参快照与来源，支持中断后恢复。"""

    MODULE = "dq_compile_session"
    ENTITY = "编译会话"
    OPERATION_TERM = "编排"
    LIST_FIELDS = ["会话编号", "编译批次", "模板族", "模板版本", "业务域", "目标表名", "校验字段", "编译来源", "状态"]
    REQUIRED_FIELDS = ["会话编号", "编译批次"]
    STATUS_ORDER = ["进行中", "中断", "已完成"]
    ACTION_RULES: dict[str, str] = {}


class DqDerivedRuleService(BaseModuleService):
    """派生规则复核清单：编译结果回写，带参数归属与模板/覆盖版本，支持按权限范围核对。"""

    MODULE = "dq_derived_rule"
    ENTITY = "派生规则"
    OPERATION_TERM = "编译"
    LIST_FIELDS = [
        "派生编号", "目标表名", "校验字段", "业务域", "归属团队",
        "模板编号", "模板版本", "覆盖版本", "编译来源", "派生版本", "复核状态", "编译SQL",
    ]
    REQUIRED_FIELDS = ["派生编号", "目标表名", "业务域", "模板编号", "派生版本"]
    STATUS_ORDER = ["待复核", "复核通过", "复核驳回"]
    ACTION_RULES = {"复核通过": "复核通过", "复核驳回": "复核驳回"}

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------
    def _find_template(self, template_id: int) -> dict[str, Any]:
        template = store.find("dq_template", template_id)
        if template is None:
            raise DerivedRuleError(f"模板{template_id}不存在")
        if "参数规格" not in template:
            raise DerivedRuleError(f"模板{template_id}缺少参数规格，无法实例化派生规则")
        return template

    def _find_override(self, family: str, template_version: str,
                       domain: str) -> dict[str, Any] | None:
        """查找业务域对该模板版本的覆盖；无精确版本时回退到不高于目标版本的最新覆盖。"""
        candidates = [
            row for row in store.rows("dq_param_override")
            if row.get("模板族") == family
            and row.get("业务域") == domain
            and row.get("status") == "已生效"
            and _version_key(row["模板版本"]) <= _version_key(template_version)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: _version_key(row["模板版本"]))

    def _find_ownership(self, table_name: str) -> dict[str, Any] | None:
        for row in store.rows("dq_table_ownership"):
            if row.get("目标表名") == table_name and row.get("status") == "已生效":
                return row
        return None

    def _next_id(self, module: str) -> int:
        return max((int(r.get("id", 0)) for r in store.rows(module)), default=0) + 1

    # ------------------------------------------------------------------
    # 1. 模板参数校验
    # ------------------------------------------------------------------
    def validate_template_params(self, template_id: int,
                                 param_values: dict[str, Any]) -> dict[str, Any]:
        """按模板参数规格校验入参：必填、类型、未声明参数、占位符一致性。"""
        template = self._find_template(template_id)
        specs = template["参数规格"]
        spec_by_name = {spec["参数名"]: spec for spec in specs}

        issues: list[str] = []
        unknown = [name for name in param_values if name not in spec_by_name]
        if unknown:
            issues.append(f"未在模板声明的参数：{'、'.join(sorted(unknown))}")

        for spec in specs:
            name = spec["参数名"]
            value = param_values.get(name)
            if value in (None, ""):
                if spec.get("必填"):
                    issues.append(f"必填参数「{name}」缺失")
                continue
            if spec.get("类型") == "number":
                if not re.fullmatch(r"-?\d+(\.\d+)?", str(value).strip()):
                    issues.append(f"参数「{name}」应为数值，实际值：{value}")
            elif spec.get("类型") == "identifier":
                if not _IDENT_RE.fullmatch(str(value).strip()):
                    issues.append(f"参数「{name}」应为合法标识符（表名/字段名），实际值：{value}")

        placeholders = set(_PLACEHOLDER_RE.findall(template.get("校验SQL", "")))
        declared = set(spec_by_name)
        if placeholders != declared:
            issues.append(
                "SQL占位符与参数规格不一致："
                f"SQL多余{sorted(placeholders - declared)}，规格多余{sorted(declared - placeholders)}"
            )

        return {
            "模板编号": template.get("模板编号"),
            "模板版本": template.get("模板版本"),
            "校验结果": "通过" if not issues else "不通过",
            "问题详情": issues,
            "参数规格": specs,
            "校验时间": _now(),
        }

    # ------------------------------------------------------------------
    # 2. 参数归属解析：共用默认 / 本域覆盖 / 本域绑定
    # ------------------------------------------------------------------
    def resolve_params(self, template: dict[str, Any], domain: str,
                       bindings: dict[str, Any]) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
        """逐参数解析实际取值与生产来源，并给出口径级编译来源。"""
        override = self._find_override(template["模板族"], template["模板版本"], domain)
        override_params = _parse_container(override["覆盖参数"]) if override else {}

        resolved: list[dict[str, Any]] = []
        compile_source = COMPILE_FROM_INHERIT
        for spec in template["参数规格"]:
            name = spec["参数名"]
            if name in bindings and bindings[name] not in (None, ""):
                entry = {
                    "参数名": name, "参数值": str(bindings[name]),
                    "来源": SOURCE_BINDING, "来源版本": None,
                }
            elif name in override_params:
                entry = {
                    "参数名": name, "参数值": str(override_params[name]),
                    "来源": SOURCE_OVERRIDE, "来源版本": override["覆盖版本"],
                }
                compile_source = COMPILE_FROM_OVERRIDE
            elif spec.get("共用默认值") is not None:
                entry = {
                    "参数名": name, "参数值": str(spec["共用默认值"]),
                    "来源": SOURCE_INHERITED, "来源版本": template["模板版本"],
                }
            else:
                raise DerivedRuleError(f"参数「{name}」既无本域绑定/覆盖，也无共用默认值")
            entry["参数类型"] = spec.get("类型")
            resolved.append(entry)
        return resolved, compile_source, override

    def _render_sql(self, template: dict[str, Any], resolved: list[dict[str, Any]]) -> str:
        sql = template["校验SQL"]
        for item in resolved:
            if item["参数类型"] == "identifier" and not _IDENT_RE.fullmatch(item["参数值"]):
                raise DerivedRuleError(f"标识符参数「{item['参数名']}」取值非法：{item['参数值']}")
            if item["参数类型"] == "number" and not re.fullmatch(r"-?\d+(\.\d+)?", item["参数值"]):
                raise DerivedRuleError(f"数值参数「{item['参数名']}」取值非法：{item['参数值']}")
            sql = sql.replace("{" + item["参数名"] + "}", item["参数值"])
        leftover = _PLACEHOLDER_RE.findall(sql)
        if leftover:
            raise DerivedRuleError(f"编译后仍存在未替换占位符：{sorted(set(leftover))}")
        return sql

    def _check_permission(self, table_name: str, column: str,
                          domain: str, caller_team: str) -> dict[str, Any]:
        """越权防护：有归属的表只能由归属域绑定，共享模板团队不得直接修改。"""
        ownership = self._find_ownership(table_name)
        if ownership is None:
            return {"目标表名": table_name, "业务域": domain, "归属团队": caller_team, "是否受归属保护": False}
        if caller_team == SHARED_TEAM:
            raise DerivedRuleError(
                f"越权拦截：表「{table_name}」归属{ownership['业务域']}，共享模板团队不得直接修改其派生规则"
            )
        if domain != ownership["业务域"]:
            raise DerivedRuleError(
                f"越权拦截：表「{table_name}」归属{ownership['业务域']}，{domain}无权绑定"
            )
        bound_columns = _parse_container(ownership.get("绑定字段"))
        if bound_columns and column not in bound_columns:
            raise DerivedRuleError(
                f"越权拦截：字段「{column}」未登记在表「{table_name}」的可绑定字段清单 {bound_columns}"
            )
        return {
            "目标表名": table_name, "业务域": ownership["业务域"],
            "归属团队": ownership["归属团队"], "是否受归属保护": True,
        }

    # ------------------------------------------------------------------
    # 3. 编译派生规则（含中断/恢复与复核清单回写）
    # ------------------------------------------------------------------
    def _open_session(self, payload: dict[str, Any], batch_no: str) -> dict[str, Any]:
        session_id = self._next_id("dq_compile_session")
        session = {
            "id": session_id,
            "会话编号": f"DQ_CS-{session_id:04d}",
            "编译批次": batch_no,
            "模板族": payload.get("模板族"),
            "模板版本": payload.get("模板版本"),
            "业务域": payload.get("业务域"),
            "目标表名": payload.get("目标表名"),
            "校验字段": payload.get("校验字段"),
            "调用团队": payload.get("调用团队"),
            "入参快照": payload.get("bindings", {}),
            "编译来源": None,
            "状态": "进行中",
            "中断步骤": None,
            "结果派生编号": None,
            "创建时间": _now(),
            "更新时间": _now(),
            "status": "进行中",
            "pending": True,
            "abnormal": False,
        }
        store.rows("dq_compile_session").append(session)
        return session

    def compile_derived_rule(self, template_id: int, domain: str, table_name: str,
                             column: str, bindings: dict[str, Any] | None = None,
                             caller_team: str | None = None,
                             interrupt: bool = False) -> dict[str, Any]:
        """编译派生规则：校验→鉴权→归属解析→渲染SQL→回写复核清单与质量规则绑定。

        interrupt=True 时在编译中段制造中断：会话停在「中断」，不产生派生规则，
        需调用 resume_compile 恢复；恢复时仍能识别当次编译来源。
        """
        template = self._find_template(template_id)
        caller_team = caller_team or domain
        bindings = dict(bindings or {})
        bindings.setdefault("table", table_name)
        bindings.setdefault("column", column)

        # 标识参数（表/字段）走本域绑定；规则参数只能来自本域覆盖或共用默认，
        # 杜绝把规则参数混作绑定传入而模糊生产来源。
        identifier_names = {
            spec["参数名"] for spec in template["参数规格"]
            if spec.get("类型") == "identifier"
        }
        illegal = sorted(name for name in bindings if name not in identifier_names)
        if illegal:
            raise DerivedRuleError(
                f"参数「{'、'.join(illegal)}」为规则参数，不能通过绑定传入；"
                "请在本域参数覆盖(dq_param_override)登记或继承共用默认值"
            )

        # 1) 模板参数校验
        validation = self.validate_template_params(template_id, bindings)
        if validation["校验结果"] != "通过":
            raise DerivedRuleError("；".join(validation["问题详情"]))

        # 2) 越权防护
        scope = self._check_permission(table_name, column, domain, caller_team)

        batch_no = f"BATCH-{uuid.uuid4().hex[:12]}"
        session = self._open_session(
            {
                "模板族": template["模板族"], "模板版本": template["模板版本"],
                "业务域": domain, "目标表名": table_name, "校验字段": column,
                "调用团队": caller_team, "bindings": bindings,
            },
            batch_no,
        )

        # 3) 参数归属解析（中断点：解析完成、写回前）
        resolved, compile_source, override = self.resolve_params(template, domain, bindings)
        session["编译来源"] = compile_source
        session["更新时间"] = _now()

        if interrupt:
            session["状态"] = "中断"
            session["status"] = "中断"
            session["中断步骤"] = "参数归属解析完成，SQL渲染前"
            session["更新时间"] = _now()
            return {
                "状态": "中断",
                "会话编号": session["会话编号"],
                "编译批次": batch_no,
                "已识别编译来源": compile_source,
                "参数明细": resolved,
                "提示": "编译已中断，可凭会话编号调用 resume_compile 恢复",
            }

        return self._finish_compile(session, template, resolved, compile_source,
                                    override, scope, caller_team, batch_no,
                                    change_trigger=None)

    def resume_compile(self, session_id: int) -> dict[str, Any]:
        """从编译会话恢复：先识别当次编译来源（继承/本域覆盖），再完成编译并回写。"""
        session = store.find("dq_compile_session", session_id)
        if session is None:
            raise DerivedRuleError(f"编译会话{session_id}不存在")
        if session["状态"] != "中断":
            raise DerivedRuleError(f"会话{session['会话编号']}状态为{session['状态']}，无需恢复")

        template = self._find_template_by_family_version(session["模板族"], session["模板版本"])
        bindings = dict(session.get("入参快照", {}))
        # 恢复时重新解析，并与会话固化的来源比对，确认当次编译口径
        resolved, compile_source, override = self.resolve_params(
            template, session["业务域"], bindings
        )
        source_match = compile_source == session.get("编译来源")
        scope = self._check_permission(
            session["目标表名"], session["校验字段"],
            session["业务域"], session["调用团队"],
        )
        result = self._finish_compile(
            session, template, resolved, compile_source, override, scope,
            session["调用团队"], session["编译批次"],
            change_trigger=f"中断恢复({session['会话编号']})",
        )
        result["恢复识别"] = {
            "会话固化来源": session.get("编译来源"),
            "恢复重算来源": compile_source,
            "来源一致": source_match,
        }
        result["状态"] = "已恢复"
        return result

    def _find_template_by_family_version(self, family: str, version: str) -> dict[str, Any]:
        for row in store.rows("dq_template"):
            if row.get("模板族") == family and row.get("模板版本") == version:
                return row
        raise DerivedRuleError(f"模板族{family}@{version}已不存在，无法恢复编译")

    def _next_revision(self, family: str, domain: str, table_name: str, column: str) -> int:
        revs = [
            int(r.get("修订序号", 0)) for r in store.rows("dq_derived_rule")
            if r.get("模板族") == family and r.get("业务域") == domain
            and r.get("目标表名") == table_name and r.get("校验字段") == column
        ]
        return (max(revs) if revs else 0) + 1

    def _finish_compile(self, session: dict[str, Any], template: dict[str, Any],
                        resolved: list[dict[str, Any]], compile_source: str,
                        override: dict[str, Any] | None, scope: dict[str, Any],
                        caller_team: str, batch_no: str,
                        change_trigger: str | None = None) -> dict[str, Any]:
        sql = self._render_sql(template, resolved)
        revision = self._next_revision(
            template["模板族"], session["业务域"], session["目标表名"], session["校验字段"],
        )
        prev_version = self._latest_derived_version(
            template["模板族"], session["业务域"], session["目标表名"], session["校验字段"],
        )
        derived_version = (
            f"{template['模板版本']}|{override['覆盖版本'] if override else 'inherit'}|rev{revision}"
        )

        derived_id = self._next_id("dq_derived_rule")
        derived = {
            "id": derived_id,
            "派生编号": f"DQ_D-{derived_id:04d}",
            "模板族": template["模板族"],
            "模板编号": template["模板编号"],
            "模板版本": template["模板版本"],
            "覆盖编号": override["覆盖编号"] if override else None,
            "覆盖版本": override["覆盖版本"] if override else "inherit",
            "业务域": session["业务域"],
            "归属团队": scope["归属团队"],
            "目标表名": session["目标表名"],
            "校验字段": session["校验字段"],
            "编译来源": compile_source,
            "参数明细": resolved,
            "编译SQL": sql,
            "编译批次": batch_no,
            "派生版本": derived_version,
            "修订序号": revision,
            "上一派生版本": prev_version,
            "复核状态": "待复核",
            "复核人": None,
            "复核意见": None,
            "变更触发": change_trigger,
            "编译时间": _now(),
            "status": "待复核",
            "pending": True,
            "abnormal": False,
        }
        store.rows("dq_derived_rule").append(derived)

        # 质量规则读取派生规则数据，登记实际绑定的表和字段
        self._register_rule_binding(derived)

        session["状态"] = "已完成"
        session["status"] = "已完成"
        session["pending"] = False
        session["编译来源"] = compile_source
        session["结果派生编号"] = derived["派生编号"]
        session["更新时间"] = _now()

        return {
            "状态": "成功",
            "派生编号": derived["派生编号"],
            "派生版本": derived_version,
            "上一派生版本": prev_version,
            "编译批次": batch_no,
            "会话编号": session["会话编号"],
            "模板编号": template["模板编号"],
            "模板版本": template["模板版本"],
            "覆盖版本": override["覆盖版本"] if override else "inherit",
            "编译来源": compile_source,
            "参数明细": resolved,
            "归属信息": scope,
            "编译SQL": sql,
            "复核状态": "待复核",
            "编译时间": derived["编译时间"],
        }

    def _latest_derived_version(self, family: str, domain: str,
                                table_name: str, column: str) -> str | None:
        rows = [
            r for r in store.rows("dq_derived_rule")
            if r.get("模板族") == family and r.get("业务域") == domain
            and r.get("目标表名") == table_name and r.get("校验字段") == column
        ]
        if not rows:
            return None
        return max(rows, key=lambda r: int(r.get("修订序号", 0)))["派生版本"]

    def _register_rule_binding(self, derived: dict[str, Any]) -> dict[str, Any]:
        """把派生规则实际绑定的表/字段与版本回写到质量规则定义。"""
        rule_rows = store.rows("dq_rule_def")
        rule_id = self._next_id("dq_rule_def")
        rule = {
            "id": rule_id,
            "规则编号": f"DQ_R-B{rule_id:04d}",
            "规则名称": f"{derived['模板编号']}_{derived['目标表名']}_{derived['校验字段']}",
            "目标表名": derived["目标表名"],
            "校验字段": derived["校验字段"],
            "规则类型": "派生规则",
            "期望值": derived["派生版本"],
            "容差范围": derived["编译来源"],
            "规则版本": derived["模板版本"],
            # 扩展登记信息：实际绑定与可回查版本
            "绑定派生编号": derived["派生编号"],
            "绑定模板版本": derived["模板版本"],
            "绑定覆盖版本": derived["覆盖版本"],
            "绑定业务域": derived["业务域"],
            "归属团队": derived["归属团队"],
            "绑定编译批次": derived["编译批次"],
            "status": "待校验",
            "pending": True,
            "abnormal": False,
        }
        rule_rows.append(rule)
        return rule

    # ------------------------------------------------------------------
    # 4. 复核清单：按团队权限范围核对与复核
    # ------------------------------------------------------------------
    def list_derived_rules(self, team: str | None = None,
                           domain: str | None = None) -> list[dict[str, Any]]:
        """按权限范围返回派生复核清单。

        - 共享模板组：只看由共用默认驱动（继承口径）的派生规则，核对模板版本与默认值；
        - 本域团队：只看本域归属的派生规则，核对覆盖版本与实际绑定；
        - 不传团队：返回全量（管理员口径）。
        """
        rows = list(store.rows("dq_derived_rule"))
        if domain:
            rows = [r for r in rows if r.get("业务域") == domain]
        if team == SHARED_TEAM:
            rows = [r for r in rows if r.get("编译来源") == COMPILE_FROM_INHERIT]
        elif team:
            rows = [r for r in rows if r.get("归属团队") == team]
        return rows

    def review_derived_rule(self, derived_id: int, reviewer_team: str,
                            conclusion: str, note: str = "") -> dict[str, Any]:
        """复核派生规则；共享模板组与本域团队只能在各自权限范围内复核。"""
        if conclusion not in ("复核通过", "复核驳回"):
            raise DerivedRuleError("复核结论只能是 复核通过 / 复核驳回")
        derived = store.find("dq_derived_rule", derived_id)
        if derived is None:
            raise DerivedRuleError(f"派生规则{derived_id}不存在")

        scoped = self.list_derived_rules(team=reviewer_team)
        if not any(r["id"] == derived_id for r in scoped):
            raise DerivedRuleError(
                f"越权拦截：{reviewer_team}无权复核该派生规则（超出其权限范围）"
            )
        derived["复核状态"] = conclusion
        derived["status"] = conclusion
        derived["pending"] = conclusion != "复核通过"
        derived["复核人"] = reviewer_team
        derived["复核意见"] = note
        derived["复核时间"] = _now()
        return dict(derived)

    def list_rule_bindings(self, team: str | None = None) -> list[dict[str, Any]]:
        """质量规则绑定登记查询，同样按团队权限范围收敛。"""
        rows = [
            r for r in store.rows("dq_rule_def")
            if r.get("规则类型") == "派生规则"
        ]
        if team == SHARED_TEAM:
            rows = [r for r in rows if r.get("容差范围") == COMPILE_FROM_INHERIT]
        elif team:
            rows = [r for r in rows if r.get("归属团队") == team]
        return rows

    # ------------------------------------------------------------------
    # 5. 共用字段默认值调整后的重编译对比
    # ------------------------------------------------------------------
    def change_shared_default_and_recompile(self, family: str, param_name: str,
                                            new_default: str, new_version: str,
                                            caller_team: str) -> dict[str, Any]:
        """共享模板组调整共用默认值并发布新版本，对各域最新派生规则重编译并对比。

        仅共享模板组可改共用默认值；有归属的表仍由各域会话完成编译，共享团队不直接改数据。
        本域覆盖的参数不受共用默认调整影响（覆盖优先），继承口径的规则SQL随之变化。
        """
        if caller_team != SHARED_TEAM:
            raise DerivedRuleError("越权拦截：仅共享模板组可调整共用默认值")

        latest = max(
            (r for r in store.rows("dq_template") if r.get("模板族") == family),
            key=lambda r: _version_key(r["模板版本"]),
            default=None,
        )
        if latest is None:
            raise DerivedRuleError(f"模板族{family}不存在")
        spec_names = [s["参数名"] for s in latest["参数规格"]]
        if param_name not in spec_names:
            raise DerivedRuleError(f"参数「{param_name}」不在模板{family}参数规格内")
        target_spec = next(s for s in latest["参数规格"] if s["参数名"] == param_name)
        if target_spec.get("共用默认值") is None:
            raise DerivedRuleError(f"参数「{param_name}」是必填绑定参数，无共用默认值可调整")
        if target_spec.get("类型") == "number" and not re.fullmatch(r"-?\d+(\.\d+)?", str(new_default)):
            raise DerivedRuleError(f"新默认值「{new_default}」与参数数值类型不匹配")
        old_default = target_spec["共用默认值"]

        # 发布新版本模板（复制规格并替换默认值）
        new_specs = [
            dict(spec, **({"共用默认值": str(new_default)} if spec["参数名"] == param_name else {}))
            for spec in latest["参数规格"]
        ]
        new_id = self._next_id("dq_template")
        new_template = {
            "id": new_id,
            "模板编号": latest["模板编号"],
            "模板名称": latest["模板名称"],
            "规则类型": latest["规则类型"],
            "校验SQL": latest["校验SQL"],
            "参数列表": latest["参数列表"],
            "适用场景": f"{latest['适用场景']}（共用默认调整 {param_name}: {old_default}→{new_default}）",
            "使用次数": "0",
            "模板版本": new_version,
            "归属团队": SHARED_TEAM,
            "模板族": family,
            "参数规格": new_specs,
            "status": "已发布",
            "pending": False,
            "abnormal": False,
        }
        store.rows("dq_template").append(new_template)

        # 取每个（域/表/字段）口径下最新一条派生规则，按新版本重编译
        latest_by_target: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in store.rows("dq_derived_rule"):
            if row.get("模板族") != family:
                continue
            key = (row["业务域"], row["目标表名"], row["校验字段"])
            cur = latest_by_target.get(key)
            if cur is None or int(row.get("修订序号", 0)) > int(cur.get("修订序号", 0)):
                latest_by_target[key] = row

        comparisons: list[dict[str, Any]] = []
        for (domain, table_name, column), before in sorted(latest_by_target.items()):
            before_sql = before["编译SQL"]
            recompiled = self.compile_derived_rule(
                new_id, domain, table_name, column,
                bindings={"table": table_name, "column": column},
                caller_team=before["归属团队"],
            )
            # 标注新版本派生规则的变更触发来源，便于复核清单回查
            new_derived = next(
                r for r in store.rows("dq_derived_rule")
                if r["派生编号"] == recompiled["派生编号"]
            )
            new_derived["变更触发"] = (
                f"共用默认调整 {param_name}: {old_default}→{new_default}（{new_version}）"
            )
            comparisons.append({
                "业务域": domain,
                "目标表名": table_name,
                "校验字段": column,
                "调整参数": param_name,
                "共用默认值变更": f"{old_default}→{new_default}",
                "编译来源_变更前": before["编译来源"],
                "编译来源_变更后": recompiled["编译来源"],
                "派生版本_变更前": before["派生版本"],
                "派生版本_变更后": recompiled["派生版本"],
                "可运行SQL_变更前": before_sql,
                "可运行SQL_变更后": recompiled["编译SQL"],
                "SQL是否变化": before_sql != recompiled["编译SQL"],
                "是否受共用调整影响": before_sql != recompiled["编译SQL"],
                "新派生复核状态": "待复核",
            })

        return {
            "状态": "成功",
            "模板族": family,
            "新版本": new_version,
            "变更参数": param_name,
            "旧共用默认值": old_default,
            "新共用默认值": str(new_default),
            "重编译口径数": len(comparisons),
            "对比结果": comparisons,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "编译"
        snapshot["desc"] = "模板参数校验并编译带归属与版本的派生规则，回写复核清单"
        snapshot["artifact"] = "派生规则复核清单"
        snapshot["artifact_desc"] = "带参数归属、模板/覆盖版本与编译来源的派生规则集"
        return snapshot
