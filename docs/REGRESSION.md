# 本地回归案例库

评测发现失败、证据不足或候选版退化时，可以保存整个 Case 的 A/B 轨迹及本次评测快照。相同输入与适配器重复保存会去重；同一 Case 修改轨迹后保存为独立内容版本，不覆盖旧案例、旧基线或人工预期。

## 库内文件

- `cases/<sha256>.json`：原始标准化 Case、adapter、本次评测基线、可编辑的人工预期。文件名仅由适配器和输入内容哈希生成，Case ID 不参与路径拼接。
- `runs/<时间>-<唯一编号>.json`：本次重放输入、评估结果、人工断言差异与快照变化。
- 同名 `.md`：简要回归检查报告。

库中的官方轨迹仍是赛事资料，应留在本地 `artifacts/`，不加入源码交付包或公共仓库。路径中出现自定义 `.py` 适配器时，是否允许执行由调用入口授权控制，回归模块只把 adapter 原样传给注入的评测回调。

## 人工确认预期

初次保存的 `expected` 是 `{"reviewed": false, "runs": {}}`。快照只是当前评测器输出，不是专家真值，不能自动变成预期答案。人工核对业务规则和事件证据后，可只修改该文件的 `expected`，例如：

```json
{
  "reviewed": true,
  "runs": {
    "实际运行ID": {
      "agent_task_success": true,
      "business_invoice_created": false,
      "root_labels": []
    }
  },
  "version_direction": "equal",
  "note": "经人工检查，应正确等待用户确认，不生成发票。"
}
```

示例中的运行 ID 和方向需按实际案例填写，不能直接照抄。可断言字段为 `agent_task_success`、`business_invoice_created`、`business_artifact_created`、`root_labels`；前三项必须是布尔值，`root_labels` 是精确的根因标签集合，空数组表示不得出现已确认根因。`version_direction` 可选，允许四个标准版本比较方向。未指定的字段不参与人工断言。

`reviewed: true` 至少要有一条有效断言，拼写错误或未知字段会报错。不要直接修改存档的 `case` 或其哈希；新 Agent 轨迹应作为新版重放输入，或重新保存一个内容版本。

## 两种检查

1. **重复检查旧轨迹**：使用当前评测器重放库中输入，可发现评测器修改后判定是否变化。它检验评测器稳定性，不能说明开票 Agent 已修复。
2. **检查新版 Agent 轨迹**：通过重放接口提供 `replacements`，按 `case_id` 替换本次待评测轨迹，保留稳定 `run_id` 与已人工审定的业务期望。原件、旧基线不变。本次报告记录替换标识和实际重放输入哈希。同 Case 有多个存档版本时逐版本列出，不覆盖或合并它们。

新版输入可只替换库中的部分 Case；其他 Case 继续使用存档输入。未知 Case、重复 ID 或与任一同 ID 存档不一致的运行 ID 集合均报错。原始 `user_request/context/reference_result/agent_domain` 必须保持一致，不能修改业务目标来制造回归通过。任务改变应另存新的案例版本；新版重放主要允许 `runs` 中事件、Agent 版本与资源信息更新。

## 结果含义

| 状态 | 含义 |
|---|---|
| `passed` | 本次结果满足已人工确认的明确断言 |
| `failed` | 本次结果至少违反一条已人工确认的断言 |
| `unreviewed` | 该案例尚未人工审定，不计作通过 |
| 整体 `needs_review` | 没有人工断言失败，但仍有未审定案例 |
| `snapshot_changed` | 与保存时评测器快照的关键判定不同，独立于人工断言通过/失败 |

**快照不变不等于修复成功。** 评测异常、缺失/损坏记录、哈希不一致或未知适配器会明确报错，不伪装成通过，也不覆写旧基线。报告会保存完整本次重放输入与结果，以便追溯。

## 程序接口

```python
save_cases(cases, assessments, library, adapter="invoice")
run_regression(library, evaluate, replacements=None)
review_expectation(library, record_id, expected)
```

其中 `evaluate(cases, adapter)` 返回标准化评估数组。`save_cases` 返回 `saved`、`existing`、`selected` 计数；`run_regression` 返回总体 `status`、`passed/failed/unreviewed/snapshot_changed` 计数、逐记录 `records` 及 `report_json/report_markdown` 路径。

人工审定入口 `review_expectation` 只接受 64 位小写十六进制记录 ID，先验证库记录，再检查 `reviewed:true` 及至少一条有效断言，只原子更新 `expected`；无效请求不会改写原件。可由工作台表单调用，无需用户手工改 JSON。

独立验证：`python -X utf8 -B -m unittest discover -s tests -p test_regression_library.py -v`。测试覆盖去重、版本隔离、人工预期、损坏数据、异常保护、路径安全，以及新版 Agent 轨迹通过预期但旧快照保持不变的情形。
