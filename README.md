# MaiBot 模型预算分配器

一个 MaiBot LLM Provider 插件，用来把不同任务的模型请求分配到不同中转站和模型。

适合有很多免费、试用、低价中转站的用户：可以按任务分类模型池，并根据余额、每日预算、延迟、价格、失败情况自动选择更合适的站点。

## 功能

- 按任务配置不同模型池。
- 每个模型池条目都可以单独开启或关闭。
- 自动从 MaiBot `model_config.toml` 同步中转站。
- 按延迟、价格、余额、每日预算、站点权重打分。
- 候选模型失败时自动切换下一个。
- 支持 429、403、余额不足、额度耗尽时自动关闭池内模型。
- 支持同一中转站配置多个备用 API Key，某个 Key 没额度时自动切换下一个。
- 支持普通模型错误累计到阈值后自动关闭池内模型，默认 3 次。
- 支持三种计费方式：按模型价格、按次扣费、Token 额度。

## 安装

在 MaiBot WebUI 的插件管理里安装：

```text
https://github.com/tackyeel/maibot-model-budget-router
```

安装后，在“模型管理”里添加一个模型分配器 API 提供商：

```toml
[[api_providers]]
name = "模型分配器"
base_url = "http://budget-router.local/v1"
api_key = "budget-router"
client_type = "budget_router"
max_retry = 0
timeout = 80
retry_interval = 1
```

然后添加逻辑模型，例如：

```toml
[[models]]
model_identifier = "router:replyer"
name = "分配器-正式回复"
api_provider = "模型分配器"
price_in = 0
price_out = 0
cache = false
cache_price_in = 0
visual = true
force_stream_mode = false

[models.extra_params]
```

把 `分配器-正式回复` 放到 `[model_task_config.replyer].model_list` 里即可。

## 支持的任务标识

```text
router:replyer
router:planner
router:timing_gate
router:memory
router:mid_memory
router:utils
router:learner
router:emoji
router:vlm
```

`embedding` 和 `voice` 不建议走这个插件，因为它目前只处理 OpenAI 兼容的聊天补全请求。

## 模型池

`[pools]` 里填写模型管理页面里的真实模型名称。新版配置支持每个模型单独开关：

```toml
[pools]
replyer = [
  { name = "deepseek-v3", enabled = true },
  { name = "gemini-2.5-flash", enabled = true },
]
planner = [
  { name = "gemini-2.5-flash", enabled = true },
]
```

旧版字符串列表仍然兼容，插件启动后会自动迁移成带开关的格式。

## 中转站预算

插件会自动把模型管理里的中转站同步到 `providers.overrides`。

每个站点可以配置：

- `enabled`：是否启用这个中转站。
- `api_keys`：备用 API Key 列表；主 Key 仍来自模型管理。
- `balance_yuan`：估算余额。
- `daily_budget_yuan`：每日预算。
- `weight`：站点权重，越大越优先。
- `billing_mode`：计费方式。
- `price_per_call_yuan`：按次扣费时每次调用多少钱。
- `token_balance`：Token 额度模式下的剩余 token。
- `daily_token_budget`：每日 token 预算。

计费方式支持：

```text
model_price   使用模型管理里的输入/输出价格
per_call      每次成功调用固定扣费
token_quota   直接按 token 额度扣
```

插件不会登录中转站后台查询真实余额，余额和额度是根据你的配置与调用量估算的。

## 自动切换 API Key

主 API Key 仍然在 MaiBot 模型管理里的中转站配置中填写。备用 Key 可以在插件配置页对应站点的“备用 API Keys”里填写，也可以直接写 TOML：

```toml
[providers.overrides]
"沐阳" = { enabled = true, api_keys = ["sk-backup-1", "sk-backup-2"], balance_yuan = 9999.0, daily_budget_yuan = 9999.0, weight = 1.0, billing_mode = "按模型价格", price_per_call_yuan = 0.0, token_balance = 0, daily_token_budget = 0 }
```

如果某个 Key 返回 `429`、`403`、`402`、余额不足、额度不足、欠费等错误，插件会先禁用这个 Key，并继续尝试同一站点的下一个 Key。只有这个站点没有可用 Key 时，才会按原逻辑关闭模型池里的模型。

## 自动关闭

开启 `auto_disable_on_429` 后，如果上游返回 429、403、402、限流、额度不足、余额不足、额度耗尽等错误，插件会优先关闭对应 API Key；没有备用 Key 时才会关闭模型池里的对应模型。

开启 `auto_disable_on_errors` 后，排除 429、403、额度不足和超时以外的普通模型错误会计数；达到 `auto_disable_error_threshold` 后，插件会自动关闭模型池里的对应模型。默认阈值是 3 次。

如果给站点充值或模型恢复了，可以在插件配置页把模型池里的开关重新打开。

## 状态文件

默认状态文件：

```text
data/router_state.json
```

里面记录今日消耗、近期延迟、失败次数和自动关闭记录。

## 许可证

MIT
