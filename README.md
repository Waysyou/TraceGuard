# 票迹 TraceGuard

**把 AI Agent 的运行记录，变成能核对、能定位问题、能比较版本的报告。**

TraceGuard 是一个在本机运行的 Python 工具。它读取同一任务的 A/B 两份运行记录，分别判断机器人是否按要求处理、业务结果是否真的发生，并把错误结论关联到具体步骤和工具参数。

本仓库基于比赛作品 **0.2.3** 整理公开。保留评测引擎、图表工作台、回归案例库、可选模型接口及测试源码。公开演示为独立编写的合成数据，仓库不包含赛事官方资料和提交包。

## 三步体验

需要 **Python 3.11 或更新版本**。规则评测使用 Python 标准库，无需安装第三方依赖、GPU 或模型 Key。

下载或克隆本仓库，在项目目录执行：

```sh
python -X utf8 -B examples/invoice_demo.py
python -X utf8 -B -m traceguard evaluate --cases artifacts/demo/invoice.jsonl --output artifacts/workbench
python -X utf8 -B -m traceguard serve --port 8765 --output artifacts/workbench
```

打开 **http://127.0.0.1:8765**。保持运行窗口打开，按 Ctrl+C 停止。端口被占用时改用 `--port 8766`，并打开对应地址。

Windows 用户也可双击 **`启动工作台.cmd`**：首次启动会生成独立合成演示，后续保留上次工作台结果。`生成演示报告.cmd` 会重新生成演示报告。

首次启动可能提示“默认样例未加载”：这是未安装赛事官方资料的提示；上面的命令已生成可加载的独立演示报告。网页中的“看开票例子”是原比赛资料入口，公开版请使用上述独立演示或“顾问开发示例”。

## 演示里看什么

| 独立合成任务 | 对比内容 |
|---|---|
| PUBLIC-DEMO-01 | 旧版用错含税口径，新版修正；区分“已经出票”和“按要求办对” |
| PUBLIC-DEMO-02 | 旧版等待确认，新版提前提交；定位确认控制问题 |

在工作台查看任务结论、A/B 对照和图表；点击错误步骤查看原始输入输出与证据，再保存问题，填写人工预期用于后续复查。

这些例子用于展示使用流程，不代表准确率、真实开票结果或任何隐藏测试成绩。评测器只读取记录，不执行轨迹里描述的工具，不连接真实开票系统。

## 功能

- **分开看过程和结果**：等待用户确认可以是正确处理，未出票不一定意味着处理错误。
- **定位原因和证据**：核对字段、金额、确认、重复风险、工具异常与回调，展示首错及后续传播。
- **比较 A/B 版本**：保留改善、退化和证据不足，给出有依据的比较理由。
- **沉淀回归案例**：保存问题、填写人工预期，支持导入新版轨迹重新检查。
- **扩展业务场景**：内置开票及合成顾问示例，可显式加载可信本地 Python 适配器。
- **可选模型辅助复核**：支持 Chat Completions 风格接口；默认关闭，模型意见不会覆盖规则结果。

## 检查自己的记录

```sh
python -X utf8 -B -m traceguard evaluate --cases your-cases.jsonl --output artifacts/custom
```

每行一个完整 Case，包含两个 runs，具体结构可运行 `examples/invoice_demo.py` 后查看生成文件。外部框架的记录需要先转换或编写适配器，并不是任意 JSONL 都能直接评估。

输出包括：

| 文件 | 内容 |
|---|---|
| `report.html` | 可离线打开的交互图表、任务对比和原始证据 |
| `report.md` / `report.full.html` | 完整评测说明 |
| `submission.jsonl` | 结构化逐任务结果 |
| `summary.json` | 指标汇总和分母说明 |
| `run_metadata.json` | 输入哈希、程序版本和实际处理耗时 |

生成报告会嵌入输入轨迹。包含真实业务资料的输出应留在本地，`artifacts/` 和 `output/` 已加入忽略规则。

## 顾问与回归示例

```sh
python -X utf8 -B examples/advisor_demo.py
python -X utf8 -B -m traceguard evaluate --adapter advisor --cases artifacts/advisor-demo/input.jsonl --output artifacts/advisor-demo/result
```

保存问题案例：

```sh
python -X utf8 -B -m traceguard evaluate --cases artifacts/demo/invoice.jsonl --output artifacts/workbench --save-regressions artifacts/regression_library
python -X utf8 -B -m traceguard regression run --library artifacts/regression_library
```

新入库案例没有人工预期时，回归命令返回 3，表示仍待审定。在网页“保存的问题”中填写预期后再运行。用 `regression run --cases new-runs.jsonl --adapter invoice` 可以检查新版记录，需要保持 Case/run ID 对应关系。

## 可选模型接入

在工作台展开“模型接入（可选）”，填写地址、模型名和 Key，测试连接后显式启用，再重新检查记录。

Key 只保存在当前服务进程内存；启用后，相应输入会发送到你配置的服务。当前接口验证使用本机模拟服务，**尚未验证真实外部服务**。详见 [模型接口说明](docs/MODEL_API.md)。

## 测试

```sh
python -X utf8 -B -m unittest discover -s tests -v
```

不需要赛事数据的测试可以直接运行；依赖官方训练文件的 19 项测试方法在缺少资料时整类跳过。测试使用临时本机服务和合成输入，不调用真实外部模型。具体结果与复现方式见 [测试说明](docs/TESTING.md)。

## 能力边界

- 质量分采用固定规则扣分，未做独立校准，不是准确率或成功概率。
- 证据不足时，布尔字段存在兼容性限制；应结合 `diagnostics` 和未决说明阅读，详见 [评测方法](docs/METHOD.md)。
- 仅适用于已实现的业务规则和输入契约，未知 Agent 需要适配与验证。
- 本机工作台只监听回环地址，不应直接作为公网服务部署。
- 官方原始数据、标注、Schema、校验脚本和内嵌官方数据的报告均不公开；需要使用官方输入时，应自行取得相应授权。

## 文档与目录

- [适配器与其他 Agent](docs/ADAPTERS.md)
- [回归案例库](docs/REGRESSION.md)
- [数据与发布边界](docs/DATA_BOUNDARY.md)
- [AI 辅助开发说明](docs/AI_USAGE.md)
- `traceguard/`：评测核心、报告、本机服务和网页。
- `examples/`：独立合成演示和自定义适配器示例。
- `tests/`：单元测试、场景检查和本机服务集成测试。

## 许可

目前仅公开源码，**尚未选择项目开源许可证**，不额外授予通用复用许可。第三方 Chart.js 的 MIT 许可独立适用，完整文本见 [第三方声明](docs/CHART_LICENSE.md)。
