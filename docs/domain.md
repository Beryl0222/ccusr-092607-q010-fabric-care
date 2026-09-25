# 领域约定

描述织物属性、污渍、洗剂、设备和护理规则之间的适配事件。

聚合对象包括`garment_profile`、`care_rule`、`treatment_plan`、`processing_batch`。事件类型包括`GARMENT_ASSESSED`、`RULE_VERSIONED`、`PLAN_APPROVED`、`BATCH_STARTED`、`OUTCOME_REVIEWED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `GARMENT_ASSESSED`：载荷还需包含 `material_evidence`, `stain_observations`。
- `PLAN_APPROVED`：载荷还需包含 `rule_version`, `risk_level`。
- `BATCH_STARTED`：载荷还需包含 `equipment_ref`, `formula_lot`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
