from __future__ import annotations

from typing import Any, Dict, List

from maibot_sdk import Field, PluginConfigBase


class PoolModelItemConfig(PluginConfigBase):
    """模型池中的单个候选模型。"""

    enabled: bool = Field(
        default=True,
        description="是否允许分配请求到这个模型",
        json_schema_extra={"label": "启用"},
    )
    name: str = Field(
        default="",
        description="模型管理里的真实模型名称",
        json_schema_extra={"label": "模型名称"},
    )


class PluginSectionConfig(PluginConfigBase):
    """模型预算分配器基础配置。"""

    __section_name__ = "模型预算分配器"
    __section_description__ = "按任务、站点余额、每日预算、延迟和失败率自动选择中转站与模型。"
    __ui_label__ = "预算分配"
    __ui_icon__ = "route"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用模型预算分配器",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.1.1",
        description="配置文件版本",
        json_schema_extra={"label": "配置版本", "disabled": True},
    )
    config_path: str = Field(
        default="/MaiMBot/config/model_config.toml",
        description="MaiBot 主模型配置路径，插件会从这里读取真实中转站、API Key 和模型名",
        json_schema_extra={"label": "主模型配置路径"},
    )
    state_path: str = Field(
        default="data/router_state.json",
        description="路由状态文件路径，记录今日消耗、延迟、失败次数和临时冷却",
        json_schema_extra={"label": "状态文件路径"},
    )
    log_detail: bool = Field(
        default=True,
        description="是否在日志里输出每次实际选中的模型和站点",
        json_schema_extra={"label": "记录详细日志"},
    )
    health_penalty_seconds: int = Field(
        default=45,
        ge=0,
        le=600,
        description="模型连续失败后，临时降低优先级的秒数",
        json_schema_extra={"label": "失败冷却秒数", "x-widget": "slider", "min": 0, "max": 300, "step": 5},
    )
    max_failover_attempts: int = Field(
        default=3,
        ge=1,
        le=8,
        description="单次请求最多尝试几个候选模型",
        json_schema_extra={"label": "最多切换次数", "x-widget": "slider", "min": 1, "max": 8, "step": 1},
    )
    latency_weight: float = Field(
        default=1.0,
        ge=0.0,
        le=5.0,
        description="越大越偏向速度快的模型",
        json_schema_extra={"label": "延迟权重", "x-widget": "slider", "min": 0.0, "max": 5.0, "step": 0.1},
    )
    cost_weight: float = Field(
        default=0.35,
        ge=0.0,
        le=5.0,
        description="越大越偏向便宜模型",
        json_schema_extra={"label": "价格权重", "x-widget": "slider", "min": 0.0, "max": 5.0, "step": 0.05},
    )
    balance_weight: float = Field(
        default=0.65,
        ge=0.0,
        le=5.0,
        description="越大越偏向余额和每日预算充足的站点",
        json_schema_extra={"label": "预算权重", "x-widget": "slider", "min": 0.0, "max": 5.0, "step": 0.05},
    )
    auto_sync_providers: bool = Field(
        default=True,
        description="打开插件配置页或插件启动时，从模型管理配置自动补齐新中转站",
        json_schema_extra={"label": "自动同步中转站"},
    )
    auto_prune_removed_providers: bool = Field(
        default=True,
        description="模型管理里删掉某个中转站后，自动移除插件里的对应站点配置",
        json_schema_extra={"label": "自动清理已删除中转站"},
    )
    auto_switch_api_key_on_quota: bool = Field(
        default=True,
        description="上游返回 429、403、402、余额不足或额度耗尽时，只关闭当前 API Key，并自动切换同站点的下一个 Key",
        json_schema_extra={"label": "额度错误自动切换 Key"},
    )
    auto_disable_model_when_all_keys_failed: bool = Field(
        default=False,
        description="仅当这个中转站所有 API Key 都因额度/限流错误失效时，才自动关闭模型池里的对应模型",
        json_schema_extra={"label": "所有 Key 失效后关闭模型"},
    )
    auto_disable_on_errors: bool = Field(
        default=True,
        description="排除 429、403、402、额度不足和超时后，普通模型错误累计达到阈值时自动关闭池内模型",
        json_schema_extra={"label": "普通错误自动关闭模型"},
    )
    auto_disable_error_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description="普通模型错误累计多少次后关闭模型池里的对应模型",
        json_schema_extra={"label": "普通错误关闭阈值", "x-widget": "slider", "min": 1, "max": 10, "step": 1},
    )


class PoolsSectionConfig(PluginConfigBase):
    """不同任务的候选模型池。"""

    __section_name__ = "任务模型池"
    __section_description__ = "填写 model_config.toml 里真实模型的 name，用来决定每类任务能用哪些模型。"
    __ui_label__ = "模型池"
    __ui_icon__ = "list-tree"
    __ui_order__ = 1

    default: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [PoolModelItemConfig(name="gemini-3.1-flash-lite"), PoolModelItemConfig(name="gemini-3.1-pro")],
        description="没有匹配到具体任务时使用的默认候选模型",
        json_schema_extra={"label": "默认模型池"},
    )
    timing_gate: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [PoolModelItemConfig(name="gemini-3.1-flash-lite"), PoolModelItemConfig(name="gemini-2.5-flash")],
        description="是否回复、回复时机判断使用的候选模型",
        json_schema_extra={"label": "时机判断模型池"},
    )
    planner: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [PoolModelItemConfig(name="gemini-3.1-flash-lite"), PoolModelItemConfig(name="gemini-3.1-pro")],
        description="规划器使用的候选模型",
        json_schema_extra={"label": "规划器模型池"},
    )
    memory: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [
            PoolModelItemConfig(name="gemini-3.1-flash-lite"),
            PoolModelItemConfig(name="gemini-3.1-pro"),
            PoolModelItemConfig(name="gpt-5.5"),
            PoolModelItemConfig(name="gemini-3-flash-preview"),
            PoolModelItemConfig(name="gemini-2.5-flash"),
        ],
        description="长期记忆总结、抽取和写回任务使用",
        json_schema_extra={"label": "长期记忆模型池"},
    )
    mid_memory: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [
            PoolModelItemConfig(name="gemini-3.1-flash-lite"),
            PoolModelItemConfig(name="gemini-3.1-pro"),
            PoolModelItemConfig(name="gemini-3-flash-preview"),
            PoolModelItemConfig(name="gemini-2.5-flash"),
            PoolModelItemConfig(name="gpt-5.5"),
        ],
        description="中期记忆压缩、整理和回顾任务使用",
        json_schema_extra={"label": "中期记忆模型池"},
    )
    replyer: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [
            PoolModelItemConfig(name="deepseek-v4-flash11"),
            PoolModelItemConfig(name="gemini-3.1-flash-lite"),
            PoolModelItemConfig(name="gemini-3.1-pro"),
            PoolModelItemConfig(name="gemini-2.5-flash"),
        ],
        description="正式回复生成使用的候选模型",
        json_schema_extra={"label": "回复模型池"},
    )
    utils: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [PoolModelItemConfig(name="gemini-3.1-flash-lite"), PoolModelItemConfig(name="gemini-3.1-pro")],
        description="工具类任务使用的候选模型",
        json_schema_extra={"label": "工具任务模型池"},
    )
    learner: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [PoolModelItemConfig(name="gemini-3.1-flash-lite"), PoolModelItemConfig(name="gemini-3.1-pro")],
        description="学习/记忆类任务使用的候选模型",
        json_schema_extra={"label": "学习任务模型池"},
    )
    emoji: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [
            PoolModelItemConfig(name="gemini-3.1-flash-lite"),
            PoolModelItemConfig(name="gemini-2.5-flash"),
            PoolModelItemConfig(name="deepseek-v4-flash11"),
        ],
        description="表情相关任务使用的候选模型",
        json_schema_extra={"label": "表情任务模型池"},
    )
    vlm: List[PoolModelItemConfig] = Field(
        default_factory=lambda: [PoolModelItemConfig(name="gemini-3.1-flash-lite"), PoolModelItemConfig(name="gemini-3.1-pro")],
        description="视觉理解任务使用的候选模型",
        json_schema_extra={"label": "视觉任务模型池"},
    )


class ModelBillingOverrideConfig(PluginConfigBase):
    """同一中转站内单个模型的计费覆盖。"""

    model_name: str = Field(default="", description="模型管理里的真实模型名称", json_schema_extra={"label": "模型名称"})
    billing_mode: str = Field(default="按模型价格", description="这个模型在当前中转站的计费方式", json_schema_extra={"label": "计费方式"})
    price_per_call_yuan: float = Field(default=0.0, ge=0.0, description="按次扣费时，每次成功调用固定扣多少钱", json_schema_extra={"label": "每次调用价格"})
    token_balance: int = Field(default=0, ge=0, description="Token 额度模式下，这个模型当前还剩多少 token", json_schema_extra={"label": "Token 余额"})
    daily_token_budget: int = Field(default=0, ge=0, description="Token 额度模式下，这个模型每天最多允许消耗多少 token；填 0 表示不限制", json_schema_extra={"label": "每日 Token 预算"})


class ApiKeyBudgetOverrideConfig(PluginConfigBase):
    """同一中转站内单个 API Key 的预算覆盖。"""

    key_index: int = Field(default=0, ge=0, description="0 是主 Key；1 是第 1 个备用 Key；2 是第 2 个备用 Key", json_schema_extra={"label": "Key 序号"})
    label: str = Field(default="", description="备注，方便区分这个 Key", json_schema_extra={"label": "备注"})
    balance_yuan: float = Field(default=0.0, ge=0.0, description="这个 Key 对应账号当前余额估算", json_schema_extra={"label": "Key 余额"})
    daily_budget_yuan: float = Field(default=0.0, ge=0.0, description="这个 Key 每天最多允许花多少钱，填 0 表示不限制每日预算", json_schema_extra={"label": "Key 每日预算"})
    token_balance: int = Field(default=0, ge=0, description="Token 额度模式下，这个 Key 当前还剩多少 token", json_schema_extra={"label": "Key Token 余额"})
    daily_token_budget: int = Field(default=0, ge=0, description="Token 额度模式下，这个 Key 每天最多允许消耗多少 token；填 0 表示不限制", json_schema_extra={"label": "Key 每日 Token 预算"})


class ProviderOverrideConfig(PluginConfigBase):
    """单个中转站预算配置。"""

    enabled: bool = Field(default=True, description="是否启用这个中转站", json_schema_extra={"label": "启用站点"})
    api_keys: Any = Field(default_factory=list, description="备用 API Key；可为列表，也可为每行一个 Key 的文本", json_schema_extra={"label": "备用 API Keys"})
    api_key_budget_overrides: Any = Field(
        default_factory=list,
        description="按 API Key 序号单独覆盖余额和每日预算；可为列表，也可为每行一条的文本",
        json_schema_extra={"label": "API Key 预算覆盖"},
    )
    balance_yuan: float = Field(default=9999.0, ge=0.0, description="这个中转站当前余额估算，填 0 会跳过该站点", json_schema_extra={"label": "站点余额"})
    daily_budget_yuan: float = Field(default=9999.0, ge=0.0, description="这个中转站每天最多允许花多少钱，填 0 表示不限制每日预算", json_schema_extra={"label": "每日预算"})
    weight: float = Field(default=1.0, ge=0.0, le=10.0, description="站点优先级，越大越优先", json_schema_extra={"label": "优先级权重"})
    currency: str = Field(default="CNY", description="这个中转站后台余额和价格使用的币种：CNY 或 USD", json_schema_extra={"label": "计价币种"})
    usd_to_cny_rate: float = Field(default=7.2, ge=0.01, description="计价币种为 USD 时，1 美元折合多少人民币", json_schema_extra={"label": "美元兑人民币汇率"})
    billing_mode: str = Field(default="按模型价格", description="计费方式：按模型价格、按次扣费、Token 额度", json_schema_extra={"label": "计费方式"})
    price_per_call_yuan: float = Field(default=0.0, ge=0.0, description="按次扣费时，每次成功调用固定扣多少钱", json_schema_extra={"label": "每次调用价格"})
    token_balance: int = Field(default=0, ge=0, description="Token 额度模式下，这个站点当前还剩多少 token", json_schema_extra={"label": "Token 余额"})
    daily_token_budget: int = Field(default=0, ge=0, description="Token 额度模式下，这个站点每天最多允许消耗多少 token；填 0 表示不限制", json_schema_extra={"label": "每日 Token 预算"})
    model_billing_overrides: List[ModelBillingOverrideConfig] = Field(
        default_factory=list,
        description="同一个中转站里某些模型计费方式不同，在这里按模型名称单独覆盖",
        json_schema_extra={"label": "模型计费覆盖"},
    )


class ProvidersSectionConfig(PluginConfigBase):
    """中转站预算配置。"""

    __section_name__ = "中转站预算"
    __section_description__ = "配置每个中转站的余额、每日预算和优先级。"
    __ui_label__ = "中转站"
    __ui_icon__ = "wallet"
    __ui_order__ = 2

    default_balance_yuan: float = Field(default=9999.0, ge=0.0, description="没有单独配置的中转站默认余额", json_schema_extra={"label": "默认余额"})
    default_daily_budget_yuan: float = Field(default=9999.0, ge=0.0, description="没有单独配置的中转站默认每日预算", json_schema_extra={"label": "默认每日预算"})
    default_weight: float = Field(default=1.0, ge=0.0, le=10.0, description="新同步中转站默认优先权重", json_schema_extra={"label": "默认优先权重"})
    default_currency: str = Field(default="CNY", description="新同步中转站默认后台币种：CNY 或 USD", json_schema_extra={"label": "默认币种"})
    default_usd_to_cny_rate: float = Field(default=7.2, ge=0.01, description="默认币种为 USD 时，1 美元折合多少人民币", json_schema_extra={"label": "默认美元汇率"})
    default_billing_mode: str = Field(default="按模型价格", description="新同步中转站默认计费方式", json_schema_extra={"label": "默认计费方式"})
    default_price_per_call_yuan: float = Field(default=0.0, ge=0.0, description="默认计费方式为按次扣费时，每次调用固定价格", json_schema_extra={"label": "默认每次调用价格"})
    default_token_balance: int = Field(default=0, ge=0, description="Token 额度模式下，没有单独配置的中转站默认 token 余额", json_schema_extra={"label": "默认 Token 额度"})
    default_daily_token_budget: int = Field(default=0, ge=0, description="Token 额度模式下，没有单独配置的中转站默认每日 token 预算；填 0 表示不限制", json_schema_extra={"label": "默认每日 Token 预算"})
    overrides: Dict[str, ProviderOverrideConfig] = Field(default_factory=dict, description="按中转站名称覆盖余额、预算和权重", json_schema_extra={"label": "站点单独设置"})


class ModelBudgetRouterConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    pools: PoolsSectionConfig = Field(default_factory=PoolsSectionConfig)
    providers: ProvidersSectionConfig = Field(default_factory=ProvidersSectionConfig)
