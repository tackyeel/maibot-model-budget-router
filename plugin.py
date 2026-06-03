from __future__ import annotations

import asyncio
import json
import math
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from maibot_sdk import LLMProvider, MaiBotPlugin


CLIENT_TYPE = "budget_router"
ROUTER_MODEL_PREFIX = "router:"


@dataclass(slots=True)
class Candidate:
    name: str
    model_identifier: str
    provider_name: str
    base_url: str
    api_key: str
    timeout: float
    price_in: float
    price_out: float
    cache_read_price_in: float
    cache_create_price_in: float
    visual: bool
    extra_params: dict[str, Any]
    score: float = 0.0


class ModelBudgetRouterPlugin(MaiBotPlugin):
    """OpenAI 兼容中转站预算和延迟路由 Provider。"""

    def __init__(self) -> None:
        super().__init__()
        self._config: dict[str, Any] = {}
        self._state: dict[str, Any] = {}
        self._state_lock = asyncio.Lock()
        self._model_config_cache: tuple[float, dict[str, Any]] | None = None
        self._plugin_dir = Path(__file__).resolve().parent

    async def on_load(self) -> None:
        self._config = self._load_plugin_config()
        self._sync_providers_from_model_config(save=True)
        self._state = self._load_state()
        self.ctx.logger.info("模型预算分配器已加载")

    async def on_unload(self) -> None:
        await self._save_state()
        self.ctx.logger.info("模型预算分配器已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        self._config = self._load_plugin_config()
        self._model_config_cache = None
        self.ctx.logger.info("模型预算分配器配置已更新")

    def set_plugin_config(self, config: dict[str, Any]) -> None:
        if isinstance(config, dict):
            normalized, _ = self.normalize_plugin_config(config)
            self._config = normalized
            self._model_config_cache = None

    def get_default_config(self) -> dict[str, Any]:
        return {
            "plugin": {
                "enabled": True,
                "config_version": "1.4.0",
                "config_path": "/MaiMBot/config/model_config.toml",
                "state_path": "data/router_state.json",
                "log_detail": True,
                "health_penalty_seconds": 45,
                "max_failover_attempts": 3,
                "latency_weight": 1.0,
                "cost_weight": 0.35,
                "balance_weight": 0.65,
                "auto_sync_providers": True,
                "auto_disable_on_429": True,
                "auto_disable_on_errors": True,
                "auto_disable_error_threshold": 3,
            },
            "pools": {
                "default": ["gemini-3.1-flash-lite", "gemini-3.1-pro"],
                "timing_gate": ["gemini-3.1-flash-lite", "gemini-2.5-flash"],
                "planner": ["gemini-3.1-flash-lite", "gemini-3.1-pro"],
                "memory": ["gemini-3.1-flash-lite", "gemini-3.1-pro", "gpt-5.5", "gemini-3-flash-preview", "gemini-2.5-flash"],
                "mid_memory": ["gemini-3.1-flash-lite", "gemini-3.1-pro", "gemini-3-flash-preview", "gemini-2.5-flash", "gpt-5.5"],
                "replyer": [
                    "deepseek-v4-flash11",
                    "gemini-3.1-flash-lite",
                    "gemini-3.1-pro",
                    "gemini-2.5-flash",
                ],
                "utils": ["gemini-3.1-flash-lite", "gemini-3.1-pro"],
                "learner": ["gemini-3.1-flash-lite", "gemini-3.1-pro"],
                "emoji": ["gemini-3.1-flash-lite", "gemini-2.5-flash", "deepseek-v4-flash11"],
                "vlm": ["gemini-3.1-flash-lite", "gemini-3.1-pro"],
            },
            "providers": {
                "default_balance_yuan": 9999.0,
                "default_daily_budget_yuan": 9999.0,
                "default_token_balance": 0,
                "default_daily_token_budget": 0,
                "overrides": {},
            },
        }

    @staticmethod
    def _provider_default(
        *,
        balance_yuan: float,
        daily_budget_yuan: float,
        weight: float,
        billing_mode: str = "按模型价格",
        price_per_call_yuan: float = 0.0,
        token_balance: int = 0,
        daily_token_budget: int = 0,
    ) -> dict[str, Any]:
        return {
            "enabled": True,
            "balance_yuan": balance_yuan,
            "daily_budget_yuan": daily_budget_yuan,
            "weight": weight,
            "billing_mode": billing_mode,
            "price_per_call_yuan": price_per_call_yuan,
            "token_balance": token_balance,
            "daily_token_budget": daily_token_budget,
        }

    def normalize_plugin_config(self, config_data: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
        default_config = self.get_default_config()
        current = config_data if isinstance(config_data, dict) else {}
        normalized = self._merge_config_defaults(default_config, current)

        plugin = normalized.setdefault("plugin", {})
        if isinstance(plugin, dict):
            plugin["config_version"] = "1.4.0"
            plugin.setdefault("auto_sync_providers", True)
            plugin.setdefault("auto_disable_on_429", True)
            plugin.setdefault("auto_disable_on_errors", True)
            plugin.setdefault("auto_disable_error_threshold", 3)

        pools = normalized.setdefault("pools", {})
        if isinstance(pools, dict):
            for key, value in list(pools.items()):
                if isinstance(value, str):
                    pools[key] = [{"name": value, "enabled": True}]
                elif isinstance(value, list):
                    pools[key] = [self._normalize_pool_item(item) for item in value]
                elif not isinstance(value, list):
                    pools[key] = []

        providers = normalized.setdefault("providers", {})
        if isinstance(providers, dict):
            overrides = providers.setdefault("overrides", {})
            if isinstance(overrides, dict):
                for provider_name, provider_config in list(overrides.items()):
                    base = {
                        "enabled": True,
                        "balance_yuan": float(providers.get("default_balance_yuan", 9999.0) or 0.0),
                        "daily_budget_yuan": float(providers.get("default_daily_budget_yuan", 9999.0) or 0.0),
                        "weight": 1.0,
                        "billing_mode": "按模型价格",
                        "price_per_call_yuan": 0.0,
                        "token_balance": int(providers.get("default_token_balance", 0) or 0),
                        "daily_token_budget": int(providers.get("default_daily_token_budget", 0) or 0),
                    }
                    if isinstance(provider_config, dict):
                        base.update(provider_config)
                    base["billing_mode"] = self._provider_billing_mode_label_from_value(base.get("billing_mode"))
                    if "token_balance" not in base:
                        legacy_input = float(base.get("price_per_million_input_yuan") or 0.0)
                        legacy_output = float(base.get("price_per_million_output_yuan") or 0.0)
                        base["token_balance"] = int(max(legacy_input, legacy_output, 0.0) * 1_000_000)
                    for numeric_key in (
                        "balance_yuan",
                        "daily_budget_yuan",
                        "weight",
                        "price_per_call_yuan",
                    ):
                        base[numeric_key] = float(base.get(numeric_key) or 0.0)
                    for numeric_key in ("token_balance", "daily_token_budget"):
                        base[numeric_key] = int(float(base.get(numeric_key) or 0))
                    overrides[provider_name] = base

        return normalized, normalized != current

    def get_webui_config_schema(
        self,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> dict[str, Any]:
        del plugin_name, plugin_description
        if not isinstance(self._config, dict) or not self._config:
            self._config = self._load_plugin_config()
        if bool(self._cfg("plugin", "auto_sync_providers", default=True)):
            self._sync_providers_from_model_config(save=True)
        config = self._config if isinstance(self._config, dict) and self._config else self.get_default_config()
        provider_overrides = self._provider_overrides_for_schema(config)

        sections: dict[str, Any] = {
            "plugin": {
                "name": "plugin",
                "title": "基础设置",
                "description": "控制分配器是否启用，以及失败切换、日志和打分权重。",
                "icon": "settings",
                "collapsed": False,
                "order": 0,
                "fields": {
                    "enabled": self._schema_field("enabled", "boolean", True, "启用插件", "是否启用模型预算分配器", "switch", 0),
                    "config_version": self._schema_field(
                        "config_version", "string", "1.4.0", "配置版本", "配置文件版本，请勿手动修改。", "text", 1, disabled=True
                    ),
                    "config_path": self._schema_field(
                        "config_path",
                        "string",
                        "/MaiMBot/config/model_config.toml",
                        "主模型配置路径",
                        "插件从这里读取真实中转站、API Key 和模型名。",
                        "text",
                        2,
                    ),
                    "state_path": self._schema_field(
                        "state_path",
                        "string",
                        "data/router_state.json",
                        "状态文件路径",
                        "记录今日消耗、延迟、失败次数和临时冷却。",
                        "text",
                        3,
                    ),
                    "log_detail": self._schema_field("log_detail", "boolean", True, "记录详细日志", "输出每次实际选中的模型和站点。", "switch", 4),
                    "health_penalty_seconds": self._schema_field(
                        "health_penalty_seconds", "integer", 45, "失败冷却秒数", "模型连续失败后临时降低优先级的秒数。", "slider", 5, min_value=0, max_value=300, step=5
                    ),
                    "max_failover_attempts": self._schema_field(
                        "max_failover_attempts", "integer", 3, "最多切换次数", "单次请求最多尝试几个候选模型。", "slider", 6, min_value=1, max_value=8, step=1
                    ),
                    "latency_weight": self._schema_field(
                        "latency_weight", "number", 1.0, "延迟权重", "越大越偏向速度快的模型。", "slider", 7, min_value=0, max_value=5, step=0.1
                    ),
                    "cost_weight": self._schema_field(
                        "cost_weight", "number", 0.35, "价格权重", "越大越偏向便宜模型。", "slider", 8, min_value=0, max_value=5, step=0.05
                    ),
                    "balance_weight": self._schema_field(
                        "balance_weight", "number", 0.65, "预算权重", "越大越偏向余额和每日预算充足的站点。", "slider", 9, min_value=0, max_value=5, step=0.05
                    ),
                    "auto_sync_providers": self._schema_field(
                        "auto_sync_providers", "boolean", True, "自动同步中转站", "打开插件配置页或插件启动时，从模型管理配置自动补齐新中转站。", "switch", 10
                    ),
                    "auto_disable_on_429": self._schema_field(
                        "auto_disable_on_429", "boolean", True, "429/403 自动关闭池内模型", "上游返回 429、403、余额不足或额度耗尽时，自动关闭模型池里的对应模型，避免继续命中没额度的模型。", "switch", 11
                    ),
                    "auto_disable_on_errors": self._schema_field(
                        "auto_disable_on_errors", "boolean", True, "普通错误自动关闭模型", "排除 429、403、额度不足和超时后，普通模型错误累计达到阈值时，自动关闭模型池里的对应模型。", "switch", 12
                    ),
                    "auto_disable_error_threshold": self._schema_field(
                        "auto_disable_error_threshold", "integer", 3, "普通错误关闭阈值", "普通模型错误累计多少次后关闭模型池里的对应模型。", "slider", 13, min_value=1, max_value=10, step=1
                    ),
                },
            },
            "pools": {
                "name": "pools",
                "title": "任务模型池",
                "description": "填写 model_config.toml 里真实模型的 name，决定每类任务能用哪些模型。",
                "icon": "list-tree",
                "collapsed": False,
                "order": 1,
                "fields": {
                    "default": self._schema_list_field("default", ["gemini-3.1-flash-lite", "gemini-3.1-pro"], "默认模型池", "没有匹配到具体任务时使用。", 0),
                    "timing_gate": self._schema_list_field("timing_gate", ["gemini-3.1-flash-lite", "gemini-2.5-flash"], "时机判断模型池", "判断是否回复、何时回复。", 1),
                    "planner": self._schema_list_field("planner", ["gemini-3.1-flash-lite", "gemini-3.1-pro"], "规划器模型池", "负责对话规划。", 2),
                    "memory": self._schema_list_field(
                        "memory",
                        ["gemini-3.1-flash-lite", "gemini-3.1-pro", "gpt-5.5", "gemini-3-flash-preview", "gemini-2.5-flash"],
                        "长期记忆模型池",
                        "长期记忆总结、抽取和写回任务使用。",
                        3,
                    ),
                    "mid_memory": self._schema_list_field(
                        "mid_memory",
                        ["gemini-3.1-flash-lite", "gemini-3.1-pro", "gemini-3-flash-preview", "gemini-2.5-flash", "gpt-5.5"],
                        "中期记忆模型池",
                        "中期记忆压缩、整理和回顾任务使用。",
                        4,
                    ),
                    "replyer": self._schema_list_field(
                        "replyer",
                        ["deepseek-v4-flash11", "gemini-3.1-flash-lite", "gemini-3.1-pro", "gemini-2.5-flash"],
                        "正式回复模型池",
                        "负责生成最终回复。",
                        3,
                    ),
                    "utils": self._schema_list_field("utils", ["gemini-3.1-flash-lite", "gemini-3.1-pro"], "工具任务模型池", "工具类任务使用。", 4),
                    "learner": self._schema_list_field("learner", ["gemini-3.1-flash-lite", "gemini-3.1-pro"], "学习任务模型池", "学习/记忆类任务使用。", 5),
                    "emoji": self._schema_list_field("emoji", ["gemini-3.1-flash-lite", "gemini-2.5-flash", "deepseek-v4-flash11"], "表情任务模型池", "表情相关任务使用。", 6),
                    "vlm": self._schema_list_field("vlm", ["gemini-3.1-flash-lite", "gemini-3.1-pro"], "视觉任务模型池", "图片和视觉理解任务使用。", 7),
                },
            },
            "providers": {
                "name": "providers",
                "title": "中转站默认预算",
                "description": "没有单独配置的中转站会使用这里的默认余额和每日预算。",
                "icon": "wallet",
                "collapsed": False,
                "order": 2,
                "fields": {
                    "default_balance_yuan": self._schema_field(
                        "default_balance_yuan", "number", 9999.0, "默认余额", "未单独配置站点时使用的余额估算。", "number", 0, min_value=0, step=0.01
                    ),
                    "default_daily_budget_yuan": self._schema_field(
                        "default_daily_budget_yuan", "number", 9999.0, "默认每日预算", "未单独配置站点时每天最多允许花多少钱。", "number", 1, min_value=0, step=0.01
                    ),
                    "default_token_balance": self._schema_field(
                        "default_token_balance", "integer", 0, "默认 Token 额度", "Token 额度模式下，未单独配置站点时默认还有多少 token；填 0 会跳过。", "number", 2, min_value=0, step=1000
                    ),
                    "default_daily_token_budget": self._schema_field(
                        "default_daily_token_budget", "integer", 0, "默认每日 Token 预算", "Token 额度模式下，未单独配置站点时每天最多允许消耗多少 token；填 0 表示不限制每日预算。", "number", 3, min_value=0, step=1000
                    ),
                },
            },
        }

        provider_section_names: list[str] = []
        for index, provider_name in enumerate(provider_overrides):
            section_name = f"providers.overrides.{provider_name}"
            provider_section_names.append(section_name)
            sections[section_name] = {
                "name": section_name,
                "title": f"站点：{provider_name}",
                "description": "余额填 0 会跳过该站点；权重越大越优先使用。",
                "icon": "server",
                "collapsed": index >= 3,
                "order": 10 + index,
                "fields": {
                    "enabled": self._schema_field("enabled", "boolean", True, "启用站点", "是否允许分配请求到这个中转站。", "switch", 0),
                    "balance_yuan": self._schema_field(
                        "balance_yuan", "number", 9999.0, "站点余额", "这个中转站当前大概还剩多少钱；填 0 会跳过。", "number", 1, min_value=0, step=0.01
                    ),
                    "daily_budget_yuan": self._schema_field(
                        "daily_budget_yuan", "number", 9999.0, "每日预算", "这个中转站每天最多允许花多少钱；填 0 表示不限制每日预算。", "number", 2, min_value=0, step=0.01
                    ),
                    "weight": self._schema_field(
                        "weight", "number", 1.0, "优先级权重", "稳定又便宜的站点建议 1.0，不稳定或想少用的站点建议 0.2 到 0.6。", "slider", 3, min_value=0, max_value=5, step=0.1
                    ),
                    "billing_mode": self._schema_field(
                        "billing_mode",
                        "string",
                        "按模型价格",
                        "计费方式",
                        "按模型价格=使用模型管理里的输入、补全、缓存读取、缓存创建价格；按次扣费=每次成功调用固定扣钱；Token 额度=按站点剩余 token 数扣额度。",
                        "select",
                        4,
                        choices=["按模型价格", "按次扣费", "Token 额度"],
                    ),
                    "price_per_call_yuan": self._schema_field(
                        "price_per_call_yuan",
                        "number",
                        0.0,
                        "每次调用价格",
                        "计费方式选“按次扣费”时使用。例如一次 0.2 元就填 0.2。",
                        "number",
                        5,
                        min_value=0,
                        step=0.001,
                        depends_on="billing_mode",
                        depends_value="按次扣费",
                    ),
                    "token_balance": self._schema_field(
                        "token_balance",
                        "integer",
                        0,
                        "Token 余额",
                        "计费方式选“Token 额度”时使用，表示这个站点当前还剩多少 token；填 0 会跳过。",
                        "number",
                        6,
                        min_value=0,
                        step=1000,
                        depends_on="billing_mode",
                        depends_value="Token 额度",
                    ),
                    "daily_token_budget": self._schema_field(
                        "daily_token_budget",
                        "integer",
                        0,
                        "每日 Token 预算",
                        "计费方式选“Token 额度”时使用，表示这个站点每天最多允许消耗多少 token；填 0 表示不限制每日预算。",
                        "number",
                        7,
                        min_value=0,
                        step=1000,
                        depends_on="billing_mode",
                        depends_value="Token 额度",
                    ),
                },
            }

        return {
            "plugin_id": plugin_id or "local.model-budget-router-cn",
            "plugin_info": {
                "name": "模型预算分配器",
                "version": plugin_version or "1.0.4",
                "description": "按任务、余额、预算、延迟和失败率自动选择中转站与模型。",
                "author": plugin_author,
            },
            "sections": sections,
            "layout": {
                "type": "tabs",
                "tabs": [
                    {"id": "basic", "title": "基础", "sections": ["plugin"], "icon": "settings", "order": 0},
                    {"id": "pools", "title": "模型池", "sections": ["pools"], "icon": "list-tree", "order": 1},
                    {
                        "id": "providers",
                        "title": "中转站",
                        "sections": ["providers", *provider_section_names],
                        "icon": "wallet",
                        "order": 2,
                    },
                ],
            },
        }

    @LLMProvider(
        CLIENT_TYPE,
        name="模型预算分配器",
        description="按余额、预算、延迟、失败率路由 OpenAI 兼容模型请求",
        version="1.0.4",
    )
    async def budget_router_provider(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        if operation != "response":
            raise ValueError(f"模型预算分配器暂不支持操作: {operation}")
        if not self._cfg("plugin", "enabled", default=True):
            raise ValueError("模型预算分配器已禁用")
        return await self._handle_response(request)

    async def _handle_response(self, request: dict[str, Any]) -> dict[str, Any]:
        task_name = self._resolve_task_name(request)
        candidates = self._build_candidates(task_name, request)
        if not candidates:
            raise ValueError(f"任务 {task_name} 没有可用候选模型")

        max_attempts = max(1, int(self._cfg("plugin", "max_failover_attempts", default=3)))
        errors: list[str] = []
        for candidate in candidates[:max_attempts]:
            started = time.perf_counter()
            try:
                result = await self._request_openai_compatible(candidate, request)
                elapsed = time.perf_counter() - started
                await self._record_success(candidate, result, elapsed)
                self._log_route(task_name, candidate, elapsed, result)
                return result
            except Exception as exc:
                elapsed = time.perf_counter() - started
                await self._record_failure(candidate, elapsed, str(exc))
                errors.append(f"{candidate.name}@{candidate.provider_name}: {type(exc).__name__}: {exc}")
                self.ctx.logger.warning(
                    "模型预算分配失败，切换候选: task=%s model=%s provider=%s elapsed=%.2fs error=%s",
                    task_name,
                    candidate.name,
                    candidate.provider_name,
                    elapsed,
                    exc,
                )

        raise RuntimeError("所有候选模型请求失败: " + " | ".join(errors[-max_attempts:]))

    def _plugin_config_path(self) -> Path:
        return self._plugin_dir / "config.toml"

    def _sync_providers_from_model_config(self, *, save: bool = False) -> bool:
        if not bool(self._cfg("plugin", "auto_sync_providers", default=True)):
            return False
        try:
            model_config = self._load_model_config()
        except Exception as exc:
            self.ctx.logger.warning("model budget router provider sync failed: %s", exc)
            return False

        providers = self._config.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            self._config["providers"] = providers
        overrides = providers.setdefault("overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
            providers["overrides"] = overrides

        changed = False
        default_balance = float(providers.get("default_balance_yuan", 9999.0) or 0.0)
        default_daily = float(providers.get("default_daily_budget_yuan", 9999.0) or 0.0)
        default_tokens = int(float(providers.get("default_token_balance", 0) or 0))
        default_daily_tokens = int(float(providers.get("default_daily_token_budget", 0) or 0))
        for provider in model_config.get("api_providers", []):
            if not isinstance(provider, dict):
                continue
            name = str(provider.get("name") or "").strip()
            client_type = str(provider.get("client_type") or "").strip()
            if not name or client_type == CLIENT_TYPE or name == "?????":
                continue
            if name in overrides:
                continue
            overrides[name] = self._provider_default(
                balance_yuan=default_balance,
                daily_budget_yuan=default_daily,
                weight=1.0,
                token_balance=default_tokens,
                daily_token_budget=default_daily_tokens,
            )
            changed = True

        if changed and save:
            self._write_plugin_config()
            self.ctx.logger.info("model budget router synced new provider overrides")
        return changed

    def _write_plugin_config(self) -> None:
        path = self._plugin_config_path()
        path.write_text(self._config_to_toml(self._config), encoding="utf-8")

    @classmethod
    def _config_to_toml(cls, config: dict[str, Any]) -> str:
        lines: list[str] = []
        plugin = config.get("plugin") if isinstance(config.get("plugin"), dict) else {}
        lines.append("[plugin]")
        for key in (
            "enabled", "config_version", "config_path", "state_path", "log_detail",
            "health_penalty_seconds", "max_failover_attempts", "latency_weight", "cost_weight",
            "balance_weight", "auto_sync_providers", "auto_disable_on_429",
            "auto_disable_on_errors", "auto_disable_error_threshold",
        ):
            if key in plugin:
                lines.append(f"{key} = {cls._toml_value(plugin[key])}")
        lines.append("")

        pools = config.get("pools") if isinstance(config.get("pools"), dict) else {}
        lines.append("[pools]")
        for key, value in pools.items():
            lines.append(f"{key} = {cls._toml_value(value)}")
        lines.append("")

        providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
        lines.append("[providers]")
        for key in ("default_balance_yuan", "default_daily_budget_yuan", "default_token_balance", "default_daily_token_budget"):
            if key in providers:
                lines.append(f"{key} = {cls._toml_value(providers[key])}")
        lines.append("")
        lines.append("[providers.overrides]")
        overrides = providers.get("overrides") if isinstance(providers.get("overrides"), dict) else {}
        for name, value in overrides.items():
            if isinstance(value, dict):
                items = ", ".join(f"{key} = {cls._toml_value(item)}" for key, item in value.items())
                lines.append(f"{cls._toml_key(str(name))} = {{ {items} }}")
        lines.append("")
        return "\n".join(lines)

    @classmethod
    def _toml_key(cls, key: str) -> str:
        if key.replace("_", "").replace("-", "").isalnum() and key and not any(ord(ch) > 127 for ch in key):
            return key
        return cls._toml_value(key)

    @classmethod
    def _toml_value(cls, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, float):
            return repr(float(value))
        if isinstance(value, list):
            return "[" + ", ".join(cls._toml_value(item) for item in value) + "]"
        if isinstance(value, dict):
            items = ", ".join(f"{cls._toml_key(str(key))} = {cls._toml_value(item)}" for key, item in value.items())
            return "{ " + items + " }"
        text = str(value)
        return json.dumps(text, ensure_ascii=False)

    def _load_plugin_config(self) -> dict[str, Any]:
        config_path = self._plugin_config_path()
        raw_config: dict[str, Any] = {}
        if config_path.exists():
            raw_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        normalized, changed = self.normalize_plugin_config(raw_config)
        if changed:
            self._config = normalized
            self._write_plugin_config()
        return normalized

    def _state_path(self) -> Path:
        raw_path = str(self._cfg("plugin", "state_path", default="data/router_state.json"))
        path = Path(raw_path)
        if not path.is_absolute():
            path = self._plugin_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return {"providers": {}, "models": {}, "date": self._today()}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {"providers": {}, "models": {}, "date": self._today()}
        except Exception:
            return {"providers": {}, "models": {}, "date": self._today()}

    async def _save_state(self) -> None:
        async with self._state_lock:
            self._rotate_daily_state_locked()
            self._state_path().write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_model_config(self) -> dict[str, Any]:
        config_path = Path(str(self._cfg("plugin", "config_path", default="/MaiMBot/config/model_config.toml")))
        stat = config_path.stat()
        if self._model_config_cache and self._model_config_cache[0] == stat.st_mtime:
            return self._model_config_cache[1]
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        self._model_config_cache = (stat.st_mtime, data)
        return data

    def _build_candidates(self, task_name: str, request: dict[str, Any]) -> list[Candidate]:
        model_config = self._load_model_config()
        provider_by_name = {
            str(item.get("name") or ""): item
            for item in model_config.get("api_providers", [])
            if isinstance(item, dict)
        }
        model_by_name = {
            str(item.get("name") or ""): item
            for item in model_config.get("models", [])
            if isinstance(item, dict)
        }

        raw_names = self._pool_model_names(task_name)
        candidates: list[Candidate] = []
        disabled_candidates: list[Candidate] = []
        for name in raw_names:
            model = model_by_name.get(name)
            if not isinstance(model, dict):
                continue
            if str(model.get("api_provider") or "") == self._router_provider_name(request):
                continue

            provider_name = str(model.get("api_provider") or "")
            provider = provider_by_name.get(provider_name)
            if not self._provider_enabled(provider_name) or not isinstance(provider, dict):
                continue
            if not self._provider_has_budget(provider_name):
                continue
            if self._candidate_auto_disabled(name, provider_name):
                disabled_candidates.append(
                    Candidate(
                        name=name,
                        model_identifier=str(model.get("model_identifier") or name),
                        provider_name=provider_name,
                        base_url=str(provider.get("base_url") or "").rstrip("/"),
                        api_key=str(provider.get("api_key") or ""),
                        timeout=float(provider.get("timeout") or 30),
                        price_in=float(model.get("price_in") or 0),
                        price_out=float(model.get("price_out") or 0),
                        cache_read_price_in=self._first_float(
                            model,
                            "cache_read_price_in",
                            "cache_price_read",
                            "cache_price_read_in",
                            "price_cache_read",
                            "cache_price_in",
                        ),
                        cache_create_price_in=self._first_float(
                            model,
                            "cache_create_price_in",
                            "cache_write_price_in",
                            "cache_price_create",
                            "cache_price_create_in",
                            "price_cache_create",
                        ),
                        visual=bool(model.get("visual", False)),
                        extra_params=dict(model.get("extra_params") or {}),
                    )
                )
                continue

            candidates.append(
                Candidate(
                    name=name,
                    model_identifier=str(model.get("model_identifier") or name),
                    provider_name=provider_name,
                    base_url=str(provider.get("base_url") or "").rstrip("/"),
                    api_key=str(provider.get("api_key") or ""),
                    timeout=float(provider.get("timeout") or 30),
                    price_in=float(model.get("price_in") or 0),
                    price_out=float(model.get("price_out") or 0),
                    cache_read_price_in=self._first_float(
                        model,
                        "cache_read_price_in",
                        "cache_price_read",
                        "cache_price_read_in",
                        "price_cache_read",
                        "cache_price_in",
                    ),
                    cache_create_price_in=self._first_float(
                        model,
                        "cache_create_price_in",
                        "cache_write_price_in",
                        "cache_price_create",
                        "cache_price_create_in",
                        "price_cache_create",
                    ),
                    visual=bool(model.get("visual", False)),
                    extra_params=dict(model.get("extra_params") or {}),
                )
            )

        if not candidates and disabled_candidates:
            self.ctx.logger.warning(
                "模型池 %s 的候选都处于自动关闭状态，临时启用兜底候选避免任务无模型",
                task_name,
            )
            candidates = disabled_candidates

        for candidate in candidates:
            candidate.score = self._score_candidate(candidate)
        return sorted(candidates, key=lambda item: item.score, reverse=True)

    def _pool_model_names(self, task_name: str) -> list[str]:
        pools = self._config.get("pools") if isinstance(self._config.get("pools"), dict) else {}
        raw_pool = pools.get(task_name) or pools.get("default") or []
        if isinstance(raw_pool, str):
            return [raw_pool]
        if not isinstance(raw_pool, list):
            return []
        names: list[str] = []
        for item in raw_pool:
            pool_item = self._normalize_pool_item(item)
            if not self._as_bool(pool_item.get("enabled", True), default=True):
                continue
            name = str(pool_item.get("name") or "").strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def _first_float(source: dict[str, Any], *keys: str) -> float:
        for key in keys:
            value = source.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _score_candidate(self, candidate: Candidate) -> float:
        provider_state = self._provider_state(candidate.provider_name)
        model_state = self._model_state(candidate.name)
        provider_weight = float(self._provider_override(candidate.provider_name).get("weight", 1.0) or 1.0)

        avg_latency = float(model_state.get("avg_latency_sec") or provider_state.get("avg_latency_sec") or 2.0)
        failures = float(model_state.get("consecutive_failures") or 0) + float(provider_state.get("consecutive_failures") or 0)
        cooldown_until = float(model_state.get("cooldown_until") or provider_state.get("cooldown_until") or 0)
        if cooldown_until > time.time():
            return -1e9

        latency_weight = float(self._cfg("plugin", "latency_weight", default=1.0))
        cost_weight = float(self._cfg("plugin", "cost_weight", default=0.35))
        balance_weight = float(self._cfg("plugin", "balance_weight", default=0.65))
        cost_score = 1.0 / (1.0 + self._candidate_cost_for_score(candidate))
        latency_score = 1.0 / (1.0 + avg_latency)
        balance_score = self._provider_budget_ratio(candidate.provider_name)
        failure_score = 1.0 / (1.0 + failures * 2.0)
        return provider_weight * failure_score * (
            latency_score * latency_weight + cost_score * cost_weight + balance_score * balance_weight
        )

    async def _request_openai_compatible(self, candidate: Candidate, request: dict[str, Any]) -> dict[str, Any]:
        if not candidate.base_url:
            raise ValueError("候选 provider 缺少 base_url")
        if not candidate.api_key:
            raise ValueError("候选 provider 缺少 api_key")

        payload: dict[str, Any] = {
            "model": candidate.model_identifier,
            "messages": self._to_openai_messages(request.get("message_list")),
            "temperature": request.get("temperature"),
            "max_tokens": request.get("max_tokens"),
        }
        payload.update(candidate.extra_params)
        tools = request.get("tool_options")
        if isinstance(tools, list) and tools:
            payload["tools"] = tools
        response_format = self._to_openai_response_format(request.get("response_format"))
        if response_format:
            payload["response_format"] = response_format

        payload = {key: value for key, value in payload.items() if value is not None}
        headers = {"Authorization": f"Bearer {candidate.api_key}", "Content-Type": "application/json"}
        timeout = min(max(candidate.timeout, 5.0), 120.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{candidate.base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        return self._parse_openai_response(data, candidate)

    def _to_openai_messages(self, raw_messages: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_messages, list):
            raise ValueError("request.message_list 必须是列表")
        messages: list[dict[str, Any]] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                continue
            role = str(raw_message.get("role") or "user")
            message: dict[str, Any] = {"role": role}
            if raw_message.get("tool_call_id"):
                message["tool_call_id"] = raw_message.get("tool_call_id")
            if raw_message.get("tool_calls"):
                message["tool_calls"] = self._to_openai_tool_calls(raw_message.get("tool_calls"))
            parts = raw_message.get("parts")
            message["content"] = self._to_openai_content(parts)
            messages.append(message)
        return messages

    @classmethod
    def _to_openai_tool_calls(cls, raw_tool_calls: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_tool_calls, list):
            return []
        tool_calls: list[dict[str, Any]] = []
        for index, raw_tool_call in enumerate(raw_tool_calls):
            if not isinstance(raw_tool_call, dict):
                continue
            function = raw_tool_call.get("function")
            if isinstance(function, dict) and function.get("name"):
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, str):
                    arguments = raw_arguments
                else:
                    arguments = json.dumps(raw_arguments if isinstance(raw_arguments, dict) else {}, ensure_ascii=False)
                tool_calls.append(
                    {
                        "id": str(raw_tool_call.get("id") or raw_tool_call.get("call_id") or f"tool-call-{index + 1}"),
                        "type": "function",
                        "function": {
                            "name": str(function.get("name") or ""),
                            "arguments": arguments,
                        },
                    }
                )
                continue

            func_name = str(raw_tool_call.get("func_name") or raw_tool_call.get("name") or "").strip()
            if not func_name:
                continue
            args = raw_tool_call.get("args")
            if isinstance(args, str):
                arguments = args
            else:
                arguments = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
            tool_calls.append(
                {
                    "id": str(raw_tool_call.get("call_id") or raw_tool_call.get("id") or f"tool-call-{index + 1}"),
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "arguments": arguments,
                    },
                }
            )
        return tool_calls

    @staticmethod
    def _to_openai_content(parts: Any) -> Any:
        if not isinstance(parts, list):
            return ""
        text_parts: list[str] = []
        rich_parts: list[dict[str, Any]] = []
        has_image = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = str(part.get("text") or "")
                text_parts.append(text)
                rich_parts.append({"type": "text", "text": text})
            elif part.get("type") == "image":
                has_image = True
                image_format = str(part.get("image_format") or "png").lower()
                image_base64 = str(part.get("image_base64") or "")
                rich_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/{image_format};base64,{image_base64}"},
                    }
                )
        return rich_parts if has_image else "".join(text_parts)

    @staticmethod
    def _to_openai_response_format(raw_format: Any) -> dict[str, Any] | None:
        if not isinstance(raw_format, dict):
            return None
        format_type = raw_format.get("format_type")
        if format_type == "json_object":
            return {"type": "json_object"}
        if format_type == "json_schema":
            schema = raw_format.get("schema")
            return {"type": "json_schema", "json_schema": schema} if isinstance(schema, dict) else None
        return None

    @staticmethod
    def _parse_openai_response(data: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("上游响应缺少 choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            raise ValueError("上游响应缺少 message")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        cache_hit_tokens = (
            usage.get("prompt_cache_hit_tokens")
            or usage.get("cache_read_input_tokens")
            or usage.get("cached_tokens")
            or prompt_details.get("cached_tokens")
            or 0
        )
        cache_miss_tokens = (
            usage.get("prompt_cache_miss_tokens")
            or usage.get("cache_creation_input_tokens")
            or usage.get("cache_write_input_tokens")
            or 0
        )
        return {
            "content": message.get("content") or "",
            "reasoning_content": message.get("reasoning_content") or message.get("reasoning") or "",
            "tool_calls": ModelBudgetRouterPlugin._parse_openai_tool_calls(message.get("tool_calls")),
            "usage": {
                "model_name": candidate.name,
                "provider_name": candidate.provider_name,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "prompt_cache_hit_tokens": int(cache_hit_tokens or 0),
                "prompt_cache_miss_tokens": int(cache_miss_tokens or 0),
            },
            "raw_data": {"router": {"model": candidate.name, "provider": candidate.provider_name}, "upstream": data},
        }

    @staticmethod
    def _parse_openai_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_tool_calls, list):
            return []
        tool_calls: list[dict[str, Any]] = []
        for index, raw_tool_call in enumerate(raw_tool_calls):
            if not isinstance(raw_tool_call, dict):
                continue

            if raw_tool_call.get("func_name"):
                args = raw_tool_call.get("args")
                if isinstance(args, str):
                    try:
                        parsed_args = json.loads(args)
                    except json.JSONDecodeError:
                        parsed_args = {}
                else:
                    parsed_args = args if isinstance(args, dict) else {}
                tool_calls.append(
                    {
                        "id": str(raw_tool_call.get("call_id") or raw_tool_call.get("id") or f"tool-call-{index + 1}"),
                        "type": "function",
                        "function": {
                            "name": str(raw_tool_call.get("func_name") or ""),
                            "arguments": parsed_args,
                        },
                        "extra_content": raw_tool_call.get("extra_content") if isinstance(raw_tool_call.get("extra_content"), dict) else None,
                    }
                )
                continue

            function = raw_tool_call.get("function")
            if not isinstance(function, dict):
                continue
            func_name = str(function.get("name") or "").strip()
            if not func_name:
                continue
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, dict):
                args = raw_arguments
            elif isinstance(raw_arguments, str) and raw_arguments.strip():
                try:
                    parsed = json.loads(raw_arguments)
                    args = parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    args = {}
            else:
                args = {}
            tool_calls.append(
                {
                    "id": str(raw_tool_call.get("id") or raw_tool_call.get("call_id") or f"tool-call-{index + 1}"),
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "arguments": args,
                    },
                    "extra_content": None,
                }
            )
        return tool_calls

    async def _record_success(self, candidate: Candidate, result: dict[str, Any], elapsed: float) -> None:
        async with self._state_lock:
            self._rotate_daily_state_locked()
            provider = self._provider_state(candidate.provider_name)
            model = self._model_state(candidate.name)
            self._update_latency(provider, elapsed)
            self._update_latency(model, elapsed)
            provider["consecutive_failures"] = 0
            model["consecutive_failures"] = 0
            model["general_error_count"] = 0
            provider["success_count"] = int(provider.get("success_count") or 0) + 1
            model["success_count"] = int(model.get("success_count") or 0) + 1
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            charge = self._estimate_charge(candidate, usage)
            money_yuan = charge["money_yuan"]
            token_count = charge["tokens"]
            provider["spent_yuan_today"] = float(provider.get("spent_yuan_today") or 0.0) + money_yuan
            provider["spent_tokens_today"] = int(provider.get("spent_tokens_today") or 0) + token_count
            if self._provider_billing_mode(candidate.provider_name) == "token_quota":
                provider["estimated_token_balance"] = max(0, self._provider_token_balance(candidate.provider_name) - token_count)
            else:
                provider["estimated_balance_yuan"] = max(0.0, self._provider_balance(candidate.provider_name) - money_yuan)
        await self._save_state()

    async def _record_failure(self, candidate: Candidate, elapsed: float, error: str) -> None:
        async with self._state_lock:
            self._rotate_daily_state_locked()
            provider = self._provider_state(candidate.provider_name)
            model = self._model_state(candidate.name)
            self._update_latency(provider, elapsed)
            self._update_latency(model, elapsed)
            provider["consecutive_failures"] = int(provider.get("consecutive_failures") or 0) + 1
            model["consecutive_failures"] = int(model.get("consecutive_failures") or 0) + 1
            provider["failure_count"] = int(provider.get("failure_count") or 0) + 1
            model["failure_count"] = int(model.get("failure_count") or 0) + 1
            provider["last_error"] = error[-300:]
            model["last_error"] = error[-300:]
            if bool(self._cfg("plugin", "auto_disable_on_429", default=True)) and self._is_quota_or_429_error(error):
                self._mark_candidate_auto_disabled_locked(candidate, error, disable_type="quota_or_429_403")
                self._disable_model_in_pools(candidate.name, error, disable_type="quota_or_429_403")
                self.ctx.logger.warning(
                    "模型池模型已因 429/403/额度错误自动关闭: model=%s provider=%s reason=%s",
                    candidate.name,
                    candidate.provider_name,
                    error[-180:],
                )
            elif self._should_count_general_error(error):
                general_errors = int(model.get("general_error_count") or 0) + 1
                model["general_error_count"] = general_errors
                threshold = max(1, int(self._cfg("plugin", "auto_disable_error_threshold", default=3)))
                if bool(self._cfg("plugin", "auto_disable_on_errors", default=True)) and general_errors >= threshold:
                    self._mark_candidate_auto_disabled_locked(candidate, error, disable_type="general_error")
                    self._disable_model_in_pools(candidate.name, error, disable_type="general_error")
                    self.ctx.logger.warning(
                        "模型池模型已因普通错误累计 %s 次自动关闭: model=%s provider=%s reason=%s",
                        general_errors,
                        candidate.name,
                        candidate.provider_name,
                        error[-180:],
                    )
            penalty = float(self._cfg("plugin", "health_penalty_seconds", default=45))
            if int(model.get("consecutive_failures") or 0) >= 2:
                model["cooldown_until"] = time.time() + penalty
        await self._save_state()

    @staticmethod
    def _update_latency(target: dict[str, Any], elapsed: float) -> None:
        previous = target.get("avg_latency_sec")
        if previous is None:
            target["avg_latency_sec"] = round(elapsed, 3)
        else:
            target["avg_latency_sec"] = round(float(previous) * 0.75 + elapsed * 0.25, 3)
        target["last_latency_sec"] = round(elapsed, 3)
        target["last_seen_at"] = int(time.time())

    def _estimate_charge(self, candidate: Candidate, usage: dict[str, Any]) -> dict[str, Any]:
        override = self._provider_override(candidate.provider_name)
        billing_mode = self._provider_billing_mode(candidate.provider_name)
        total_tokens = self._usage_total_tokens(usage)
        if billing_mode == "token_quota":
            return {"money_yuan": 0.0, "tokens": total_tokens}
        if billing_mode == "per_call":
            return {"money_yuan": max(0.0, float(override.get("price_per_call_yuan") or 0.0)), "tokens": 0}

        return {"money_yuan": self._estimate_model_price_cost(candidate, usage), "tokens": 0}

    def _estimate_cost(self, candidate: Candidate, usage: dict[str, Any]) -> float:
        return float(self._estimate_charge(candidate, usage)["money_yuan"])

    @staticmethod
    def _usage_total_tokens(usage: dict[str, Any]) -> int:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        return max(total_tokens, prompt_tokens + completion_tokens, 0)

    @staticmethod
    def _usage_int(usage: dict[str, Any], key: str) -> int:
        return max(0, int(usage.get(key) or 0))

    def _estimate_model_price_cost(self, candidate: Candidate, usage: dict[str, Any]) -> float:
        prompt_tokens = self._usage_int(usage, "prompt_tokens")
        completion_tokens = self._usage_int(usage, "completion_tokens")
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return 0.0

        cache_hit_tokens = self._usage_int(usage, "prompt_cache_hit_tokens")
        cache_miss_tokens = self._usage_int(usage, "prompt_cache_miss_tokens")
        cache_read_price = candidate.cache_read_price_in if candidate.cache_read_price_in > 0 else candidate.price_in
        cache_create_price = candidate.cache_create_price_in if candidate.cache_create_price_in > 0 else candidate.price_in

        if cache_hit_tokens > 0 or cache_miss_tokens > 0:
            normal_prompt_tokens = max(0, prompt_tokens - cache_hit_tokens - cache_miss_tokens)
            input_cost = (
                normal_prompt_tokens * candidate.price_in
                + cache_hit_tokens * cache_read_price
                + cache_miss_tokens * cache_create_price
            )
        else:
            input_cost = prompt_tokens * candidate.price_in
        output_cost = completion_tokens * candidate.price_out
        return (input_cost + output_cost) / 1_000_000.0

    def _resolve_task_name(self, request: dict[str, Any]) -> str:
        model_info = request.get("model_info") if isinstance(request.get("model_info"), dict) else {}
        identifier = str(model_info.get("model_identifier") or "")
        name = str(model_info.get("name") or "")
        for value in (identifier, name):
            if value.startswith(ROUTER_MODEL_PREFIX):
                return value[len(ROUTER_MODEL_PREFIX) :].strip() or "default"
        return "default"

    def _router_provider_name(self, request: dict[str, Any]) -> str:
        api_provider = request.get("api_provider") if isinstance(request.get("api_provider"), dict) else {}
        return str(api_provider.get("name") or "")

    def _provider_override(self, provider_name: str) -> dict[str, Any]:
        providers = self._config.get("providers") if isinstance(self._config.get("providers"), dict) else {}
        overrides = providers.get("overrides") if isinstance(providers.get("overrides"), dict) else {}
        value = overrides.get(provider_name)
        return value if isinstance(value, dict) else {}

    def _provider_billing_mode(self, provider_name: str) -> str:
        return self._provider_billing_mode_from_value(self._provider_override(provider_name).get("billing_mode"))

    @classmethod
    def _provider_billing_mode_label_from_value(cls, value: Any) -> str:
        labels = {
            "model_price": "按模型价格",
            "per_call": "按次扣费",
            "token_quota": "Token 额度",
        }
        return labels.get(cls._provider_billing_mode_from_value(value), "按模型价格")

    @staticmethod
    def _provider_billing_mode_from_value(value: Any) -> str:
        raw_mode = str(value or "model_price").strip()
        aliases = {
            "按模型价格": "model_price",
            "模型价格": "model_price",
            "model": "model_price",
            "model_price": "model_price",
            "按次扣费": "per_call",
            "按次": "per_call",
            "per_call": "per_call",
            "call": "per_call",
            "Token 额度": "token_quota",
            "token额度": "token_quota",
            "token 额度": "token_quota",
            "token_quota": "token_quota",
            "quota_tokens": "token_quota",
            "按百万token": "token_quota",
            "按百万 token": "token_quota",
            "百万token": "token_quota",
            "百万 token": "token_quota",
            "per_million": "token_quota",
            "per_million_tokens": "token_quota",
        }
        return aliases.get(raw_mode, "model_price")

    def _candidate_cost_for_score(self, candidate: Candidate) -> float:
        override = self._provider_override(candidate.provider_name)
        billing_mode = self._provider_billing_mode(candidate.provider_name)
        if billing_mode == "per_call":
            return max(0.0, float(override.get("price_per_call_yuan") or 0.0)) * 10.0
        if billing_mode == "token_quota":
            return 0.0
        cache_read_price = candidate.cache_read_price_in if candidate.cache_read_price_in > 0 else candidate.price_in
        cache_create_price = candidate.cache_create_price_in if candidate.cache_create_price_in > 0 else candidate.price_in
        return max(0.0, candidate.price_in + candidate.price_out + cache_read_price + cache_create_price) / 200.0

    def _provider_enabled(self, provider_name: str) -> bool:
        override = self._provider_override(provider_name)
        return bool(override.get("enabled", True))

    def _provider_balance(self, provider_name: str) -> float:
        state_balance = self._provider_state(provider_name).get("estimated_balance_yuan")
        if state_balance is not None:
            return float(state_balance)
        override = self._provider_override(provider_name)
        providers = self._config.get("providers") if isinstance(self._config.get("providers"), dict) else {}
        return float(override.get("balance_yuan", providers.get("default_balance_yuan", 9999.0)) or 0.0)

    def _provider_daily_budget(self, provider_name: str) -> float:
        override = self._provider_override(provider_name)
        providers = self._config.get("providers") if isinstance(self._config.get("providers"), dict) else {}
        return float(override.get("daily_budget_yuan", providers.get("default_daily_budget_yuan", 9999.0)) or 0.0)

    def _provider_token_balance(self, provider_name: str) -> int:
        state_balance = self._provider_state(provider_name).get("estimated_token_balance")
        if state_balance is not None:
            return int(float(state_balance))
        override = self._provider_override(provider_name)
        providers = self._config.get("providers") if isinstance(self._config.get("providers"), dict) else {}
        return int(float(override.get("token_balance", providers.get("default_token_balance", 0)) or 0))

    def _provider_daily_token_budget(self, provider_name: str) -> int:
        override = self._provider_override(provider_name)
        providers = self._config.get("providers") if isinstance(self._config.get("providers"), dict) else {}
        return int(float(override.get("daily_token_budget", providers.get("default_daily_token_budget", 0)) or 0))

    def _provider_has_budget(self, provider_name: str) -> bool:
        if self._provider_billing_mode(provider_name) == "token_quota":
            balance = self._provider_token_balance(provider_name)
            daily_budget = self._provider_daily_token_budget(provider_name)
            spent = int(self._provider_state(provider_name).get("spent_tokens_today") or 0)
            return balance > 0 and (daily_budget <= 0 or spent < daily_budget)
        balance = self._provider_balance(provider_name)
        daily_budget = self._provider_daily_budget(provider_name)
        spent = float(self._provider_state(provider_name).get("spent_yuan_today") or 0.0)
        return balance > 0 and (daily_budget <= 0 or spent < daily_budget)

    def _provider_budget_ratio(self, provider_name: str) -> float:
        if self._provider_billing_mode(provider_name) == "token_quota":
            balance = self._provider_token_balance(provider_name)
            daily_budget = self._provider_daily_token_budget(provider_name)
            spent = int(self._provider_state(provider_name).get("spent_tokens_today") or 0)
            if balance <= 0:
                return 0.0
            if daily_budget <= 0:
                return 1.0
            return max(0.0, min(1.0, (daily_budget - spent) / max(daily_budget, 1)))
        balance = self._provider_balance(provider_name)
        daily_budget = self._provider_daily_budget(provider_name)
        spent = float(self._provider_state(provider_name).get("spent_yuan_today") or 0.0)
        if balance <= 0:
            return 0.0
        if daily_budget <= 0:
            return 1.0
        return max(0.0, min(1.0, (daily_budget - spent) / max(daily_budget, 0.001)))

    def _candidate_auto_disabled(self, model_name: str, provider_name: str) -> bool:
        if not (
            bool(self._cfg("plugin", "auto_disable_on_429", default=True))
            or bool(self._cfg("plugin", "auto_disable_on_errors", default=True))
        ):
            return False
        provider_disabled = self._provider_state(provider_name).get("auto_disabled_models")
        if isinstance(provider_disabled, dict) and provider_disabled.get(model_name):
            return True
        model_disabled = self._model_state(model_name).get("auto_disabled_providers")
        return isinstance(model_disabled, dict) and bool(model_disabled.get(provider_name))

    @staticmethod
    def _is_quota_or_429_error(error: str) -> bool:
        text = error.lower()
        patterns = (
            "429",
            "403",
            "rate limit",
            "too many requests",
            "insufficient",
            "quota",
            "quota is not enough",
            "balance",
            "no credit",
            "not enough",
            "余额不足",
            "额度不足",
            "额度耗尽",
            "余额耗尽",
            "欠费",
            "无额度",
            "rate_limit",
            "model_capacity_exhausted",
        )
        return any(pattern in text for pattern in patterns)

    @staticmethod
    def _is_timeout_error(error: str) -> bool:
        text = error.lower()
        return any(pattern in text for pattern in ("timeout", "timed out", "readtimeout", "connecttimeout", "超时"))

    def _should_count_general_error(self, error: str) -> bool:
        return not self._is_quota_or_429_error(error) and not self._is_timeout_error(error)

    def _mark_candidate_auto_disabled_locked(self, candidate: Candidate, error: str, *, disable_type: str) -> None:
        provider = self._provider_state(candidate.provider_name)
        model = self._model_state(candidate.name)
        disabled_at = int(time.time())
        payload = {"disabled_at": disabled_at, "reason": error[-300:], "type": disable_type}
        provider.setdefault("auto_disabled_models", {})[candidate.name] = payload
        model.setdefault("auto_disabled_providers", {})[candidate.provider_name] = payload

    def _disable_model_in_pools(self, model_name: str, error: str, *, disable_type: str) -> bool:
        pools = self._config.get("pools") if isinstance(self._config.get("pools"), dict) else {}
        if not isinstance(pools, dict):
            return False
        changed = False
        disabled_at = int(time.time())
        for pool_name, value in list(pools.items()):
            items = value if isinstance(value, list) else [value]
            normalized_items: list[dict[str, Any]] = []
            enabled_count = sum(1 for item in items if self._as_bool(self._normalize_pool_item(item).get("enabled", True), default=True))
            for item in items:
                pool_item = self._normalize_pool_item(item)
                if str(pool_item.get("name") or "").strip() == model_name and self._as_bool(pool_item.get("enabled", True), default=True):
                    if enabled_count <= 1:
                        pool_item["disabled_reason"] = "保留为模型池最后一个可用候选，未自动关闭；原始错误：" + error[-120:]
                        pool_item["disabled_type"] = "kept_last_candidate"
                        normalized_items.append(pool_item)
                        continue
                    pool_item["enabled"] = False
                    pool_item["disabled_reason"] = error[-180:]
                    pool_item["disabled_type"] = disable_type
                    pool_item["disabled_at"] = disabled_at
                    changed = True
                    enabled_count -= 1
                    self.ctx.logger.warning("模型已从模型池关闭: pool=%s model=%s type=%s", pool_name, model_name, disable_type)
                normalized_items.append(pool_item)
            pools[pool_name] = normalized_items
        if changed:
            self._write_plugin_config()
        return changed

    @staticmethod
    def _normalize_pool_item(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("model") or item.get("model_name") or "").strip()
            extra = {str(key): value for key, value in item.items() if str(key) not in {"name", "model", "model_name", "enabled"}}
            return {"name": name, "enabled": ModelBudgetRouterPlugin._as_bool(item.get("enabled", True), default=True), **extra}
        return {"name": str(item).strip(), "enabled": True}

    @staticmethod
    def _as_bool(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "on", "enabled", "启用", "开"}:
            return True
        if text in {"false", "0", "no", "off", "disabled", "禁用", "关"}:
            return False
        return default

    def _provider_state(self, provider_name: str) -> dict[str, Any]:
        providers = self._state.setdefault("providers", {})
        return providers.setdefault(provider_name, {})

    def _model_state(self, model_name: str) -> dict[str, Any]:
        models = self._state.setdefault("models", {})
        return models.setdefault(model_name, {})

    def _rotate_daily_state_locked(self) -> None:
        today = self._today()
        if self._state.get("date") == today:
            return
        self._state["date"] = today
        for provider in self._state.setdefault("providers", {}).values():
            if isinstance(provider, dict):
                provider["spent_yuan_today"] = 0.0
                provider["spent_tokens_today"] = 0

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _cfg(self, section: str, key: str, default: Any = None) -> Any:
        value = self._config.get(section)
        if isinstance(value, dict):
            return value.get(key, default)
        return default

    @classmethod
    def _merge_config_defaults(cls, default: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for key, default_value in default.items():
            current_value = current.get(key)
            if isinstance(default_value, dict) and isinstance(current_value, dict):
                merged[key] = cls._merge_config_defaults(default_value, current_value)
            elif key in current:
                merged[key] = current_value
            else:
                merged[key] = default_value
        for key, current_value in current.items():
            if key not in merged:
                merged[key] = current_value
        return merged

    @staticmethod
    def _provider_overrides_for_schema(config: dict[str, Any]) -> dict[str, Any]:
        providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
        overrides = providers.get("overrides") if isinstance(providers.get("overrides"), dict) else {}
        return dict(overrides)

    @staticmethod
    def _schema_field(
        name: str,
        field_type: str,
        default: Any,
        label: str,
        hint: str,
        ui_type: str,
        order: int,
        *,
        disabled: bool = False,
        min_value: float | None = None,
        max_value: float | None = None,
        step: float | None = None,
        choices: list[Any] | None = None,
        depends_on: str | None = None,
        depends_value: Any = None,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "type": field_type,
            "default": default,
            "description": hint,
            "label": label,
            "ui_type": ui_type,
            "required": False,
            "hidden": False,
            "disabled": disabled,
            "order": order,
            "placeholder": None,
            "hint": hint,
            "icon": None,
            "example": None,
            "choices": choices,
            "min": min_value,
            "max": max_value,
            "step": step,
            "pattern": None,
            "max_length": None,
            "input_type": None,
            "rows": 3,
            "group": None,
            "depends_on": depends_on,
            "depends_value": depends_value,
            "item_type": None,
            "item_fields": None,
            "min_items": None,
            "max_items": None,
        }

    @classmethod
    def _schema_list_field(cls, name: str, default: list[str], label: str, hint: str, order: int) -> dict[str, Any]:
        default_items = [{"name": item, "enabled": True} for item in default]
        field = cls._schema_field(name, "array", default_items, label, hint, "list", order)
        field["item_type"] = "object"
        field["item_fields"] = {
            "enabled": cls._schema_field("enabled", "boolean", True, "启用", "是否允许分配请求到这个模型。", "switch", 0),
            "name": cls._schema_field("name", "string", "", "模型名称", "模型管理里的真实模型名称。", "text", 1),
        }
        field["min_items"] = 0
        field["max_items"] = None
        return field

    def _log_route(self, task_name: str, candidate: Candidate, elapsed: float, result: dict[str, Any]) -> None:
        if not bool(self._cfg("plugin", "log_detail", default=True)):
            return
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        self.ctx.logger.info(
            "模型预算分配: task=%s model=%s provider=%s elapsed=%.2fs tokens=%s score=%.3f",
            task_name,
            candidate.name,
            candidate.provider_name,
            elapsed,
            usage.get("total_tokens", 0),
            candidate.score,
        )


def create_plugin() -> ModelBudgetRouterPlugin:
    return ModelBudgetRouterPlugin()
