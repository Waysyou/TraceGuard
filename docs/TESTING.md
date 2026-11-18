# 测试与复现

在仓库目录运行，需要 Python 3.11 或更新版本：

```sh
python -X utf8 -B -m unittest discover -s tests -v
```

测试覆盖金额、确认、风险、取消、超时、证据传播、适配器、报告、回归库和本机模拟模型接口。它们不会调用真实外部模型。

2026-11-18，公开目录在未配置官方资料的 Windows 环境下实际运行：126 项自包含测试通过。另有一个含 19 项方法的官方数据回归类跳过；日志显示 `Ran 126 tests`、`OK (skipped=1)`，不是把 19 项未执行方法记为通过。

## 官方文件缺失时

`EngineRegressionTests` 包含 19 项依赖官方训练文件的测试方法。没有文件时，整个测试类跳过；日志显示一条 `setUpClass ... skipped`，不是只有一项方法未执行。

测试可能读取 `TRACEGUARD_DATA_DIR`，或查找仓库相邻的官方资料目录。公开仓库不包含这些资料。

## 独立演示

```sh
python -X utf8 -B examples/invoice_demo.py
python -X utf8 -B -m traceguard evaluate --cases artifacts/demo/invoice.jsonl --output artifacts/workbench
python -X utf8 -B -m traceguard validate --cases artifacts/demo/invoice.jsonl --submission artifacts/workbench/submission.jsonl
```

第二个命令生成六类结果文件，第三个命令检查本地输出契约、Case 覆盖和证据引用，不代表官方 Schema 检查或业务准确率。

## 用户自备官方输入

在 PowerShell 中设置仅用于本机测试的目录：

```powershell
$env:TRACEGUARD_DATA_DIR = "你的官方资料解压目录"
python -X utf8 -B -m unittest discover -s tests -v
```

不要将官方文件或含官方原始轨迹的报告推送到公共仓库。`tools/benchmark.py` 是独立标注比较工具，不被运行评测核心导入。

质量分未做独立校准；工程测试结果不能用于宣称隐藏集成绩。当前模型接口仅通过本机模拟服务验证。
