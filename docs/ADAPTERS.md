# Agent 适配器

默认 `invoice` 保留官方开票评测规则。新增的 `advisor` 是独立的本地知识顾问演示，验证明确答复与引用来源，不调用开票引擎、外部模型或真实业务系统。它证明另一业务域可以复用输入校验、A/B 输出和报告流程，不代表已接入真实企业顾问或会计系统。

## 运行顾问示例

```powershell
python -X utf8 -B examples/advisor_demo.py
python -X utf8 -B -m traceguard evaluate --adapter advisor --cases artifacts/advisor-demo/input.jsonl --output artifacts/advisor-demo/result
```

脚本产生 3 个完全独立的合成 Case：错误答复修正、引用失实导致退化、缺少最终结果导致未决。该示例与官方开票数据分开，不能提交成官方验证结果。

## 统一数据与结果

每个适配器提供 `name`、`domain`、`success_label`、`outcome_label` 和两个方法：

```python
normalize_case(raw_case: dict) -> dict
evaluate_case(canonical_case: dict) -> dict
```

规范 Case 保留 `case_id/user_request/context/reference_result/runs/events`，两条运行按基线/候选顺序。规范化应幂等、不修改原始输入，并强制使用适配器自身业务域，不能信任输入指定插件或业务域。顾问原始输入使用 `id/question/knowledge/target/versions[].steps`；标准化后可被统一输入校验读取。

统一输出保留 `case_id/run_assessments/version_comparison`。顾问以 `business_artifact_created` 表示答复交付；`business_invoice_created` 恒为 false，仅为旧结构兼容，绝不能将答复交付称为发票生成。`diagnostics.contract_profile=generic` 明确通用输出不是官方开票提交。

顾问正确性只检查明确的 `accepted_answers`（或 `expected_answer`）与 `required_citations`，对照 `context.sources[].source_id` 和最终 `ANSWER/RESPOND/COMPLETE_TASK` 的 `answer/source_ids/delivered`。答案及引用契约违反合并为一个 `FIELD_FABRICATION` 业务扣分项，扣35分；证据不足单列，不能将其虚构成确切错误。明确答案集合并非自然语言语义评判，释义需显式加入允许答案；这里只评最终答复，不重建多轮顾问推理。

## 新增业务域，无需修改核心模块

在可信的本地 `.py` 文件中导出 `ADAPTER` 对象，实现相同属性和方法，然后显式运行：

```powershell
python -X utf8 -B -m traceguard evaluate --adapter C:/path/to/my_adapter.py --cases input.jsonl --output artifacts/custom-domain
```

只需要新增适配器文件，不修改 `engine.py` 或核心注册表。适配器的文件路径由命令行显式选择，不允许轨迹 JSON 自动指定、远程下载或隐式导入其他 Python 包。加载 `.py` 会执行可信本地代码；加载器只检查接口，统一输入/输出校验仍由主入口执行，错误插件不得绕过校验。报告显示自己的业务名称与交付指标。

本项目另附 `examples/support_adapter.py`，将明确答复/引用契约复用到客服答复标签，用同一合成输入即可验证文件式插件接入：

```powershell
python -X utf8 -B -m traceguard evaluate --adapter examples/support_adapter.py --cases artifacts/advisor-demo/input.jsonl --output artifacts/support-demo
```

这是可信本地插件扩展的可运行模板，仍使用合成数据，不代表已经接入真实客服系统。

需新增领域规则时，在该适配器中实现，并为自己的真实输入写领域测试；示例适配器不是所有 Agent 的通用质量判官。
