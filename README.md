# 织物洗护适配判定库

描述织物属性、污渍、洗剂、设备和护理规则之间的适配事件，并在契约之上提供判定引擎与运行时服务。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/fabric_care/`：契约校验、适配判定引擎、运行时服务与命令行入口。
- `tests/`：信封、时间、版本、事件载荷以及判定与服务行为测试。
- `docs/domain.md`：领域对象、事件语义与运行时服务语义。

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
