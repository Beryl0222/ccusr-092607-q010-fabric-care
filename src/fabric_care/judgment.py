"""适配判定引擎：把面料、标签、污渍、配方、设备与诉求转成可解释的方案。

判定不追求“温度越高清洁越强”，而是取标签、设备、染色、辅料与污渍规则
的最小约束；证据不足时输出保守方案与待确认项，绝不臆测材质。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import (
    PROFESSIONAL_DISINFECTION,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    ROUTINE_CLEANING,
    DetergentFormula,
    DisinfectionLicense,
    Equipment,
    GarmentProfile,
    Refusal,
    StainObservation,
)


@dataclass(frozen=True)
class CareRuleSet:
    """判定规则集；版本随规则调整递增，方案发布时冻结所采用的版本。"""

    version: int
    confidence_threshold: float = 0.6
    protein_stain_cap_c: int = 40
    conservative_temperature_cap_c: int = 30
    enzyme_min_activity: float = 0.3
    thermal_disinfection_min_c: int = 60


@dataclass(frozen=True)
class JudgmentResult:
    kind: str
    risk_level: str
    conservative: bool
    recommended_temperature_c: int
    steps: tuple[str, ...]
    refusals: tuple[Refusal, ...]
    pending_confirmations: tuple[str, ...]
    explanations: tuple[str, ...]
    rule_version: int


def assess(
    *,
    garment: GarmentProfile,
    stains: list[StainObservation],
    formula: DetergentFormula,
    equipment: Equipment,
    license: DisinfectionLicense | None,
    requests: tuple[str, ...],
    rules: CareRuleSet,
    now: datetime,
) -> JudgmentResult:
    explanations: list[str] = []
    refusals: list[Refusal] = []
    pending: list[str] = []
    pretreat: list[str] = []
    extras: list[str] = []

    # 1) 材质证据：证据不足时保守处理，绝不臆测材质
    verified = [c for c in garment.composition if c.confidence >= rules.confidence_threshold]
    conservative = False
    if not garment.composition:
        conservative = True
        pending.append("材质组成缺失：需门店补录面料信息")
        explanations.append("未记录面料组成，按未知材质保守处理，不臆测材质")
    elif not verified:
        conservative = True
        pending.append("面料置信度不足：需人工复检确认材质")
        explanations.append(f"面料组成置信度均低于阈值{rules.confidence_threshold:.2f}，按未知材质保守处理")
    else:
        desc = "、".join(f"{c.fiber}{c.percentage:g}%" for c in verified)
        explanations.append(f"已核实面料组成：{desc}")

    # 2) 温度上限：标签、设备、染色、辅料逐项收紧
    label = garment.current_label()
    ceiling = label.max_temperature_c
    explanations.append(f"护理标签v{label.version}限定最高洗涤温度{ceiling}°C")
    if equipment.max_temperature_c < ceiling:
        ceiling = equipment.max_temperature_c
        explanations.append(f"设备{equipment.equipment_id}最高温度{equipment.max_temperature_c}°C，成为约束上限")
    if garment.dye and garment.dye.max_temperature_c is not None and garment.dye.max_temperature_c < ceiling:
        ceiling = garment.dye.max_temperature_c
        explanations.append(f"染色牢度{garment.dye.colorfastness}，温度上限压至{ceiling}°C以防褪色")
    for accessory in garment.accessories:
        if accessory.max_temperature_c is not None and accessory.max_temperature_c < ceiling:
            ceiling = accessory.max_temperature_c
            explanations.append(f"辅料{accessory.kind}耐温上限{accessory.max_temperature_c}°C，成为约束上限")

    # 3) 污渍规则：蛋白防固化，酶预处理受配方有效期与活性约束
    def enzyme_step(enzyme: str, step: str) -> None:
        activity = formula.enzyme_activity.get(enzyme, 0.0)
        if not formula.valid_at(now):
            refusals.append(Refusal(step, f"配方批次{formula.lot}不在有效期（至{formula.effective_until.isoformat()}）"))
            pending.append("更换在有效期内的配方批次后重新评估")
        elif activity < rules.enzyme_min_activity:
            refusals.append(Refusal(step, f"配方{enzyme}活性{activity:.2f}低于阈值{rules.enzyme_min_activity:.2f}"))
        else:
            pretreat.append(step)

    stain_types = {s.stain_type for s in stains}
    if "unknown" in stain_types:
        conservative = True
        pending.append("污渍类型未确认：需补充观察或送检")
        explanations.append("存在未确认污渍，保守处理以防高温固化或损伤")
    if "protein" in stain_types:
        if ceiling > rules.protein_stain_cap_c:
            ceiling = rules.protein_stain_cap_c
            explanations.append(f"蛋白类污渍遇高温固化，温度上限压至{rules.protein_stain_cap_c}°C")
        enzyme_step("protease", "蛋白酶预处理分解蛋白污渍")
    if "oil" in stain_types:
        enzyme_step("lipase", "脂肪酶预处理分解油渍")
    if "pigment" in stain_types:
        pretreat.append("氧系漂白预处理色素污渍")

    # 4) 顾客诉求与禁忌
    requested = set(requests)
    if "chlorine_bleach" in requested:
        if not label.chlorine_bleach_allowed:
            refusals.append(Refusal("氯漂", f"护理标签v{label.version}禁止氯漂"))
        elif garment.dye and not garment.dye.chlorine_allowed:
            refusals.append(Refusal("氯漂", "染色限制禁止氯漂以防褪色"))
        else:
            extras.append("氯漂处理")
    if "high_temp_wash" in requested and ceiling < 60:
        refusals.append(Refusal("高温洗涤", f"各项约束上限{ceiling}°C，无法满足高温诉求"))

    # 5) 日常去污与专业消毒分流：许可、设备与温度路径缺一不可
    kind = ROUTINE_CLEANING
    if "disinfection" in requested:
        kind = PROFESSIONAL_DISINFECTION
        if license is None:
            refusals.append(Refusal("专业消毒", "无消毒产品许可记录"))
            pending.append("补充有效消毒产品许可")
            kind = ROUTINE_CLEANING
        elif license.valid_until < now:
            refusals.append(Refusal("专业消毒", f"消毒产品许可已于{license.valid_until.isoformat()}过期"))
            pending.append("更新消毒产品许可后重新评估")
            kind = ROUTINE_CLEANING
        elif not equipment.supports_disinfection:
            refusals.append(Refusal("专业消毒", f"设备{equipment.equipment_id}不支持消毒程序"))
            kind = ROUTINE_CLEANING
        elif ceiling >= rules.thermal_disinfection_min_c:
            extras.append(f"热力消毒{rules.thermal_disinfection_min_c}°C以上")
            explanations.append("温度约束满足热力消毒条件")
        elif license.scope == "textile_chemical":
            extras.append("使用许可化学消毒产品低温处理")
            explanations.append(f"温度上限{ceiling}°C低于热力消毒所需{rules.thermal_disinfection_min_c}°C，改用许可化学消毒")
        else:
            refusals.append(Refusal("热力消毒", f"温度上限{ceiling}°C低于{rules.thermal_disinfection_min_c}°C且无化学消毒许可"))
            pending.append("顾客确认替代消毒方案")
            kind = ROUTINE_CLEANING

    # 6) 保守约束最后压顶
    if conservative:
        if ceiling > rules.conservative_temperature_cap_c:
            ceiling = rules.conservative_temperature_cap_c
        explanations.append(f"证据不足，采用保守方案：温度不超过{ceiling}°C、轻柔机械力")

    wash = f"主洗{ceiling}°C" + ("轻柔程序" if conservative else "常规程序")
    steps = pretreat + [wash] + extras
    if not label.tumble_dry_allowed:
        steps.append("禁止翻滚烘干，平铺阴干")

    if kind == PROFESSIONAL_DISINFECTION and (
        garment.accessories or (garment.dye is not None and garment.dye.colorfastness == "low")
    ):
        risk = RISK_HIGH
    elif conservative or refusals:
        risk = RISK_MEDIUM
    else:
        risk = RISK_LOW

    return JudgmentResult(
        kind=kind,
        risk_level=risk,
        conservative=conservative,
        recommended_temperature_c=ceiling,
        steps=tuple(steps),
        refusals=tuple(refusals),
        pending_confirmations=tuple(dict.fromkeys(pending)),
        explanations=tuple(explanations),
        rule_version=rules.version,
    )
