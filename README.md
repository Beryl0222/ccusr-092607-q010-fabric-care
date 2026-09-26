# 织物洗护适配判定库

混纺衣物洗护的适配判定与工艺平台：在标签最高温度、污渍类型、洗剂酶活性与消毒诉求
互相冲突时，给出有依据的保守方案，而不是按“温度越高清洁越强”排序。

## 能力

- **适配判定**（`rules.assess`）：面料成分与置信度、护理标签版本、染色/辅料限制、
  污渍观察、预处理记录、洗剂配方效期与酶活、设备能力、消毒产品许可、顾客风险确认
  统一折算为推荐温度与步骤；区分日常去污与专业消毒；证据不足给保守方案与待确认项。
- **角色边界**（`authz`）：门店录入人员不能独自批准高风险例外；配方供应方无权改写
  标签或规则，只能登记自己名下批次；高风险例外需双人批准与顾客书面确认。
- **工艺冻结与标签更正**（`service.release_plan` / `correct_label`）：发布时冻结
  规则版本、标签版本、物料批次与设备；标签更正只影响可追踪的未处理订单，
  已完成订单保留原依据并生成复查义务。
- **幂等与隔离**：重复扫描不重复扣料/启动设备；同单号衣物、温度或配方冲突即隔离。
- **原子排程与重启续期**：设备窗口事务内占用；浸泡、复检、取件期限持久化，
  服务重启后继续。
- **解释与责任链**：推荐温度与步骤理由、单项拒绝原因、结果偏离预期后的
  标签—观察—规则—批准—配方—设备责任链。

## 目录

- `contracts/domain.schema.json`：事件信封、事件类型与载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/fabric_care/`
  - `model.py`：领域值对象（证据、标签、污渍、配方、设备、许可、判定结论、版本化规则）。
  - `rules.py`：纯函数判定引擎。
  - `authz.py`：角色与权限。
  - `store.py`：原子落盘的 JSON 仓储（可重启）。
  - `service.py`：工艺平台服务（冻结、幂等、隔离、排程、续期、责任链、解释）。
  - `contracts.py` / `cli.py`：基础契约校验与命令行入口。
- `tests/`：契约、判定引擎、服务层测试。
- `docs/domain.md`：领域语义、状态机与业务规则。

## 最小示例

```python
from datetime import date
from fabric_care import (
    AssessmentInput, CareLabel, EquipmentCapability, FiberShare,
    Formula, StainKind, StainObservation, TreatmentMode, assess,
)
from datetime import datetime, timezone

result = assess(AssessmentInput(
    order_no="O-1", garment_ref="COAT-1",
    fibers=(FiberShare("wool", 0.7, 0.9), FiberShare("polyester", 0.3, 0.9)),
    label=CareLabel("L-1", 1, 60, False, True, False, False, False),
    mode=TreatmentMode.STAIN_REMOVAL,
    stains=(StainObservation(StainKind.PROTEIN, "clerk-1",
                             datetime.now(timezone.utc)),),
    formulas=(Formula("F-1", "LOT-9", "Acme", 80, 50, False, True,
                      date(2027, 1, 1)),),
    equipment=EquipmentCapability("EQ-1", min_temp_c=20, max_temp_c=90,
                                  supports_sanitize=False, supports_soak=True),
))
# result.recommended_temp_c == 30（羊毛 30°C 上限，先冷水冲除蛋白污渍）
```

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m fabric_care.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
