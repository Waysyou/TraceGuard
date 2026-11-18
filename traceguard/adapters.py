"""Explicit local adapters; importing a plugin is executing trusted local code."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path

from .io import DataError


class InvoiceAdapter:
    name = "invoice"
    domain = "invoice"
    success_label = "处理正确率"
    outcome_label = "发票生成率"

    @staticmethod
    def normalize_case(case: dict) -> dict:
        if not isinstance(case, dict):
            raise DataError("开票适配器输入必须是 Case 对象")
        normalized = deepcopy(case)
        normalized["agent_domain"] = "invoice"
        return normalized

    @staticmethod
    def evaluate_case(case: dict) -> dict:
        from .engine import evaluate_case
        return evaluate_case(case)


def load_adapter(spec: str = "invoice"):
    """Resolve built-ins or an explicitly provided .py file exporting ADAPTER.

    JSON input cannot select plugins. No remote URL, package installation, or
    implicit Python module lookup is supported.
    """
    if spec == "invoice":
        return InvoiceAdapter()
    if spec == "advisor":
        from .advisor_adapter import AdvisorAdapter
        return AdvisorAdapter()
    path = Path(spec).expanduser()
    if path.suffix.lower() != ".py" or not path.is_file():
        raise DataError(f"未知适配器 {spec}；可用 invoice、advisor 或明确的本地 .py 路径")
    path = path.resolve()
    module_name = "traceguard_local_adapter_" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise DataError("不能加载此适配器文件")
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        raise DataError(f"本地适配器加载失败：{type(exc).__name__}: {exc}") from exc
    adapter = getattr(module, "ADAPTER", None)
    for key in ("name", "domain", "success_label", "outcome_label"):
        if not isinstance(getattr(adapter, key, None), str) or not getattr(adapter, key).strip():
            raise DataError(f"适配器须导出 ADAPTER 对象及非空文本属性 {key}")
    for method in ("normalize_case", "evaluate_case"):
        if not callable(getattr(adapter, method, None)):
            raise DataError(f"适配器 ADAPTER 缺少方法 {method}")
    return adapter
