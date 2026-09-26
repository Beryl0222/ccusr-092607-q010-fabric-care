"""适配判定引擎：把事实（面料、标签、污渍、配方、设备、许可）折算成可执行方案。

设计原则：
- 温度绝不按“越高越干净”排序；取标签、纤维、辅料、染色各项上限的最低值。
- 蛋白污渍必须先冷水冲除，再进入温区；热水固化风险写入解释。
- 日常去污与专业消毒分开判定；消毒条件不满足时拒绝消毒、降级为保守去污，
  并列出待确认项，而不是悄悄升温。
- 证据不足不猜材质：按最保守温区处理并挂待确认项。
"""

from __future__ import annotations

from datetime import date

from .model import (
    AssessmentInput,
    AssessmentResult,
    CareRuleSet,
    CustomerAcknowledgement,
    Formula,
    PlannedStep,
    Rejection,
    RiskLevel,
    StainKind,
    TreatmentMode,
    RULE_REGISTRY,
)

# 证据不足、未登记纤维、未说明余量时统一采用的保守水温
CONSERVATIVE_TEMP_C = 30
# 占比低于该值的纤维不参与温区决策
INSIGNIFICANT_SHARE = 0.05
# 成分占比闭合容差
SHARE_TOLERANCE = 0.02


def assess(
    data: AssessmentInput,
    rules: CareRuleSet | None = None,
) -> AssessmentResult:
    """返回适配判定结论。纯函数，不修改输入，不做 IO。"""

    rules = rules or RULE_REGISTRY[max(RULE_REGISTRY)]
    on_date = data.on_date or date.today()

    rationale: list[str] = []
    confirmations: list[str] = []
    refusals: list[Rejection] = []
    rejections: list[Rejection] = []
    steps: list[PlannedStep] = []

    # -- 1. 材质证据与温度上限 -------------------------------------------
    ceiling, evidence_weak = _material_ceiling(data, rules, confirmations, rationale)

    # -- 2. 染色与辅料限制 -----------------------------------------------
    ceiling = _dye_and_trimming_ceiling(data, ceiling, confirmations, rationale)

    stains = data.stains
    has_protein = any(s.kind is StainKind.PROTEIN for s in stains)
    has_oil = any(s.kind is StainKind.OIL for s in stains)
    protein_heat_set = has_protein and any(
        p.heat_applied for p in data.pretreatments
    )
    if protein_heat_set:
        confirmations.append("蛋白污渍已接触热源，可能固化；需先冷水复检再决定主洗")
        rationale.append("存在蛋白污渍且已被热处理，按固化风险保守处理")

    # -- 3. 专业消毒可行性（不满足则拒绝消毒、降级） ----------------------
    effective_mode = data.mode
    sanitize_ok = False
    if data.mode is TreatmentMode.SANITIZATION:
        sanitize_ok = _check_sanitization(
            data, rules, on_date, ceiling, evidence_weak, confirmations, refusals,
            rationale,
        )
        if not sanitize_ok:
            effective_mode = TreatmentMode.STAIN_REMOVAL
            refusals.append(Rejection(
                code="sanitization_downgraded",
                subject="sanitization",
                message="专业消毒条件不满足，已降级为日常去污保守方案，不得按消毒温度执行",
            ))

    # -- 4. 配方选择（有效期、酶活、漂白、许可） --------------------------
    main_formula, sanitize_formula = _select_formulas(
        data, on_date, effective_mode, sanitize_ok, confirmations, refusals,
        rejections, rationale,
    )

    # -- 5. 推荐温度 ------------------------------------------------------
    target = 40 if has_oil else 30
    if has_protein:
        target = min(target, 40)
    temp = min(target, ceiling)
    if main_formula and main_formula.contains_enzyme:
        lo, hi = rules.enzyme_window_c
        if temp > hi:
            rationale.append(f"配方含酶，主洗温度压到酶活上限 {hi}°C")
        temp = min(temp, hi)
        temp = max(temp, lo) if ceiling >= lo else temp
    if effective_mode is TreatmentMode.SANITIZATION and not sanitize_ok:
        temp = min(temp, CONSERVATIVE_TEMP_C)
    rationale.append(f"推荐主洗温度 {temp}°C（标签上限 {data.label.max_temp_c}°C，综合上限 {ceiling}°C）")

    # -- 6. 设备能力 ------------------------------------------------------
    _check_equipment(data, effective_mode, sanitize_ok, temp, rules,
                     confirmations, rejections, rationale)

    # -- 7. 组装步骤 ------------------------------------------------------
    seq = 1
    if has_protein:
        steps.append(PlannedStep(
            seq=seq, action="cold_flush", temp_c=rules.protein_prefeer_temp_c,
            reason="先以冷水冲除蛋白污渍，防止高温使蛋白质固化",
        ))
        seq += 1
        already_enzymed = any(p.action == "enzyme_dab" for p in data.pretreatments)
        if not already_enzymed and main_formula and main_formula.contains_enzyme:
            steps.append(PlannedStep(
                seq=seq, action="enzyme_soak",
                temp_c=rules.protein_prefeer_temp_c,
                duration_min=rules.soak_duration_min,
                formula_lot=main_formula.lot,
                reason="冷温酶浸泡分解蛋白；该批次酶活在有效期内且高于下限",
            ))
            seq += 1

    steps.append(PlannedStep(
        seq=seq, action="main_wash", temp_c=temp,
        formula_lot=main_formula.lot if main_formula else None,
        reason=_main_wash_reason(data, temp, main_formula),
    ))
    seq += 1

    if sanitize_ok and sanitize_formula is not None:
        steps.append(PlannedStep(
            seq=seq, action="sanitize", temp_c=rules.sanitize_temp_c,
            formula_lot=sanitize_formula.lot,
            reason="标签允许、设备支持、消毒许可覆盖全部已确认纤维，执行专业消毒",
        ))
        seq += 1

    steps.append(PlannedStep(
        seq=seq, action="pending_recheck", temp_c=None,
        reason=f"处理后 {rules.recheck_deadline_hours} 小时内复检；逾期未复检不得放行取件",
    ))

    # -- 8. 顾客风险确认 --------------------------------------------------
    needs_ack = sanitize_ok
    if needs_ack:
        _require_ack(data.customer_ack, confirmations)
    if data.mode is TreatmentMode.SANITIZATION and not sanitize_ok:
        confirmations.append(
            "顾客的专业消毒诉求未能满足，需顾客书面确认仅按日常去污保守方案处理"
        )

    # -- 9. 风险等级 ------------------------------------------------------
    if rejections:
        risk = RiskLevel.REJECTED
    elif sanitize_ok:
        risk = RiskLevel.HIGH
    elif confirmations or refusals or evidence_weak or protein_heat_set:
        risk = RiskLevel.ELEVATED
    else:
        risk = RiskLevel.ROUTINE

    return AssessmentResult(
        order_no=data.order_no,
        garment_ref=data.garment_ref,
        mode=data.mode,
        effective_mode=effective_mode,
        risk_level=risk,
        recommended_temp_c=temp,
        steps=tuple(steps),
        confirmations_needed=tuple(dict.fromkeys(confirmations)),
        rejections=tuple(rejections),
        refusals=tuple(refusals),
        formula_lot=main_formula.lot if main_formula else None,
        equipment_id=data.equipment.equipment_id if data.equipment else None,
        rationale=tuple(rationale),
    )


# ---------------------------------------------------------------------------
# 子判定
# ---------------------------------------------------------------------------


def _material_ceiling(
    data: AssessmentInput,
    rules: CareRuleSet,
    confirmations: list[str],
    rationale: list[str],
) -> tuple[int, bool]:
    """从面料证据求温度上限。返回 (上限, 证据是否不足)。"""

    ceiling = data.label.max_temp_c
    weak = False
    accounted = 0.0

    for fiber in data.fibers:
        accounted += fiber.share
        if fiber.share < INSIGNIFICANT_SHARE:
            continue
        if fiber.confidence < rules.evidence_confidence_floor:
            weak = True
            ceiling = min(ceiling, CONSERVATIVE_TEMP_C)
            confirmations.append(
                f"纤维 {fiber.fiber} 证据置信度 {fiber.confidence:.2f} 低于"
                f" {rules.evidence_confidence_floor:.2f}，按 30°C 保守处理，需补充检测或标签佐证"
            )
            rationale.append(f"{fiber.fiber} 证据不足，不按其标称耐热性放行")
            continue
        mapped = rules.fiber_temp_ceiling.get(fiber.fiber)
        if mapped is None:
            weak = True
            ceiling = min(ceiling, CONSERVATIVE_TEMP_C)
            confirmations.append(f"未登记纤维 {fiber.fiber} 的水温上限，需工艺主管确认")
        else:
            ceiling = min(ceiling, mapped)

    unaccounted = 1.0 - accounted
    if unaccounted > SHARE_TOLERANCE:
        weak = True
        ceiling = min(ceiling, CONSERVATIVE_TEMP_C)
        confirmations.append(
            f"面料组成未闭合（缺 {unaccounted:.0%}），未知部分按 30°C 保守处理"
        )

    rationale.append(f"标签与已确认纤维的综合温度上限为 {ceiling}°C")
    return ceiling, weak


def _dye_and_trimming_ceiling(
    data: AssessmentInput,
    ceiling: int,
    confirmations: list[str],
    rationale: list[str],
) -> int:
    if data.dye is not None:
        if data.dye.bleed_risk or not data.dye.colorfast_wet:
            ceiling = min(ceiling, CONSERVATIVE_TEMP_C)
            rationale.append("染色牢度不足或存在串色风险，压到 30°C 并禁用氧化剂")
            confirmations.append("易串色衣物需单独处理，顾客需确认褪色风险")
    for trim in data.trimmings:
        if trim.max_temp_c is not None:
            ceiling = min(ceiling, trim.max_temp_c)
            rationale.append(f"辅料“{trim.description}”限温 {trim.max_temp_c}°C")
    return ceiling


def _check_sanitization(
    data: AssessmentInput,
    rules: CareRuleSet,
    on_date: date,
    ceiling: int,
    evidence_weak: bool,
    confirmations: list[str],
    refusals: list[Rejection],
    rationale: list[str],
) -> bool:
    """专业消毒的全部门槛；任一不满足即拒绝消毒。"""

    ok = True
    if not data.label.sanitizable:
        ok = False
        refusals.append(Rejection(
            code="label_forbids_sanitization", subject="sanitization",
            message="护理标签未允许专业消毒温区/药剂，不得通过升温实现消毒",
        ))
    if evidence_weak:
        ok = False
        refusals.append(Rejection(
            code="material_evidence_insufficient", subject="sanitization",
            message="材质证据不足，无法确认消毒工艺对全部纤维安全",
        ))
    if data.equipment is None or not data.equipment.supports_sanitize:
        ok = False
        refusals.append(Rejection(
            code="equipment_unsupported", subject="sanitization",
            message="无支持专业消毒的设备能力记录",
        ))
    if ceiling < rules.sanitize_temp_c:
        ok = False
        refusals.append(Rejection(
            code="temp_conflict", subject="sanitization",
            message=f"综合温度上限 {ceiling}°C 低于消毒目标 {rules.sanitize_temp_c}°C，"
                    "升温会导致缩水/褪色/辅料损伤",
        ))
    license_ = data.sanitizer_license
    if license_ is None:
        ok = False
        refusals.append(Rejection(
            code="sanitizer_license_missing", subject="sanitization",
            message="缺少消毒产品许可记录",
        ))
    else:
        for fiber in data.fibers:
            if fiber.share < INSIGNIFICANT_SHARE:
                continue
            if not license_.covers(fiber.fiber, on_date):
                ok = False
                refusals.append(Rejection(
                    code="sanitizer_license_not_covering_fiber",
                    subject=f"sanitizer:{fiber.fiber}",
                    message=f"消毒许可未覆盖纤维 {fiber.fiber} 或许可已过期",
                ))
    if ok:
        rationale.append("专业消毒门槛全部通过：标签、设备、许可、温度")
        confirmations.append("专业消毒属高风险处理，发布前需顾客书面风险确认与高风险双人批准")
    return ok


def _select_formulas(
    data: AssessmentInput,
    on_date: date,
    effective_mode: TreatmentMode,
    sanitize_ok: bool,
    confirmations: list[str],
    refusals: list[Rejection],
    rejections: list[Rejection],
    rationale: list[str],
) -> tuple[Formula | None, Formula | None]:
    """在配方中选可用主洗配方与消毒配方；过期/失活/越权的配方逐个记录拒绝原因。"""

    bleach_allowed = data.label.allow_bleach and not (
        data.dye is not None and data.dye.no_oxygen_bleach
    )
    if data.dye is not None and (data.dye.bleed_risk or not data.dye.colorfast_wet):
        bleach_allowed = False

    main_candidate: Formula | None = None
    sanitize_candidate: Formula | None = None
    usable: list[Formula] = []

    for formula in data.formulas:
        reason = _formula_blocker(formula, on_date, bleach_allowed, data)
        if reason is not None:
            refusals.append(Rejection(
                code=reason, subject=f"formula_lot:{formula.lot}",
                message=_FORMULA_BLOCK_MESSAGE[reason].format(
                    lot=formula.lot, supplier=formula.supplier,
                ),
            ))
            continue
        usable.append(formula)

    if sanitize_ok:
        sanitize_candidate = next(
            (f for f in usable if f.sanitizer_licensed), None
        )
    # 主洗配方取首个可用批次；消毒配方在无独立主洗批次时可兼任
    main_candidate = usable[0] if usable else None

    if data.formulas and main_candidate is None:
        rejections.append(Rejection(
            code="no_usable_formula", subject="formula",
            message="登记的配方批次全部不可用（过期/酶活不足/漂白受限），无主洗配方",
        ))
    if not data.formulas:
        confirmations.append("未登记洗剂配方批次，发布前需补录批次与有效期")

    if sanitize_ok and sanitize_candidate is None:
        rejections.append(Rejection(
            code="no_licensed_sanitizer_lot", subject="formula",
            message="消毒门槛通过但没有持许可且在有效期内的消毒配方批次",
        ))

    if main_candidate is not None:
        rationale.append(
            f"主洗选用配方批次 {main_candidate.lot}（供应方 {main_candidate.supplier}，"
            f"有效期至 {main_candidate.valid_until.isoformat()}）"
        )
    if sanitize_candidate is not None:
        rationale.append(f"消毒选用持许可批次 {sanitize_candidate.lot}")
    return main_candidate, sanitize_candidate


_FORMULA_BLOCK_MESSAGE = {
    "formula_expired": "配方批次 {lot} 已过有效期，不得使用",
    "enzyme_out_of_spec": "配方批次 {lot} 酶活低于下限或已失效",
    "bleach_forbidden": "配方批次 {lot} 含漂白成分，标签/染色限制禁止使用",
    "enzyme_forbidden_by_label": "配方批次 {lot} 含酶，但护理标签禁止酶处理",
}


def _formula_blocker(
    formula: Formula,
    on_date: date,
    bleach_allowed: bool,
    data: AssessmentInput,
) -> str | None:
    if on_date > formula.valid_until:
        return "formula_expired"
    if formula.contains_enzyme and not formula.enzyme_within_spec(on_date):
        return "enzyme_out_of_spec"
    if formula.contains_bleach and not bleach_allowed:
        return "bleach_forbidden"
    if formula.contains_enzyme and not data.label.allow_enzyme:
        return "enzyme_forbidden_by_label"
    return None


def _check_equipment(
    data: AssessmentInput,
    effective_mode: TreatmentMode,
    sanitize_ok: bool,
    temp: int,
    rules: CareRuleSet,
    confirmations: list[str],
    rejections: list[Rejection],
    rationale: list[str],
) -> None:
    equipment = data.equipment
    if equipment is None:
        confirmations.append("未指定设备，排程前需核验设备温区与窗口")
        return
    if temp < equipment.min_temp_c or temp > equipment.max_temp_c:
        rejections.append(Rejection(
            code="equipment_temp_out_of_range", subject=f"equipment:{equipment.equipment_id}",
            message=f"设备温区 {equipment.min_temp_c}~{equipment.max_temp_c}°C 无法达到推荐 {temp}°C",
        ))
    if sanitize_ok and rules.sanitize_temp_c > equipment.max_temp_c:
        rejections.append(Rejection(
            code="equipment_temp_out_of_range", subject=f"equipment:{equipment.equipment_id}",
            message=f"设备无法达到消毒温度 {rules.sanitize_temp_c}°C",
        ))
    if not equipment.windows:
        confirmations.append("设备未开放可排程窗口")
    rationale.append(
        f"设备 {equipment.equipment_id} 温区 {equipment.min_temp_c}~{equipment.max_temp_c}°C"
    )


def _require_ack(
    ack: CustomerAcknowledgement | None,
    confirmations: list[str],
) -> None:
    if ack is None or not ack.accepted_risk or ack.accepted_at is None:
        confirmations.append("缺少顾客书面风险确认（缩水/褪色等风险点）")
    elif not ack.items:
        confirmations.append("顾客风险确认未列明具体风险点，需补全后再发布")


def _main_wash_reason(
    data: AssessmentInput,
    temp: int,
    formula: Formula | None,
) -> str:
    bits = [f"主洗 {temp}°C：标签、纤维、染色、辅料上限取最低值"]
    if any(s.kind is StainKind.PROTEIN for s in data.stains):
        bits.append("蛋白污渍已先行冷水处理")
    if formula and formula.contains_enzyme:
        bits.append("温度处于酶活窗口内")
    if data.mode is TreatmentMode.SANITIZATION:
        bits.append("消毒诉求未满足时本步骤仅为日常去污，不具消毒承诺")
    return "；".join(bits)
