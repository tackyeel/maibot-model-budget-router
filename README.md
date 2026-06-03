# MaiBot 模型预算分配器

一个 MaiBot LLM Provider 插件，用来把不同任务的模型请求分配到不同中转站和模型。

适合有很多免费、试用、低价中转站的用户：可以按任务分类模型池，并根据余额、每日预算、延迟、价格和失败情况自动选择更合适的站点。

## 功能

- 按任务配置不同模型池
- 自动从 MaiBot `model_config.toml` 同步中转站
- 按延迟、价格、余额、每日预算、站点权重打分
- 候选模型失败时自动切换下一个
- 上游返回 `429`、限流、额度不足、余额不足时，可自动跳过对应 `模型@站点`
- 支持三种计费方式：
  - 按模型价格
  - 按次扣费
  - Token 额度

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

把 `分配器-正式回复` 放到 `[model_task_config.replyer].model_list` 的第一位即可。

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

## 配置

进入插件配置页后，主要改两部分：

### 模型池

`[pools]` 里填写真实模型的 `name`，也就是模型管理页面里的模型名称。

例如：

```toml
[pools]
replyer = ["deepseek-v3", "gemini-2.5-flash"]
planner = ["gemini-2.5-flash"]
```

### 中转站预算

插件会自动把模型管理里的中转站同步到 `providers.overrides`。

每个站点可以配置：

- `enabled`：是否启用这个站点
- `balance_yuan`：估算余额
- `daily_budget_yuan`：每日预算
- `weight`：站点权重，越大越优先
- `billing_mode`：计费方式
- `price_per_call_yuan`：按次扣费时，每次调用多少钱
- `token_balance`：Token 额度模式下的剩余 token
- `daily_token_budget`：每日 token 预算

计费方式支持：

```text
model_price   按模型管理里的输入/输出价格扣
per_call      每次成功调用固定扣费
token_quota   直接按 token 额度扣
```

插件不会登录中转站后台查询真实余额，余额和额度是根据你的配置与调用量估算的。

## 429 自动禁用

开启 `auto_disable_on_429` 后，如果上游返回 429、限流、额度不足、余额不足等错误，插件会把对应的 `模型@站点` 写入状态文件，后续自动跳过。

如果你给站点充值或恢复额度，可以：

- 临时关闭 `429 自动禁用模型`
- 或删除 `data/router_state.json`

## 状态文件

默认状态文件：

```text
data/router_state.json
```

里面记录今日消耗、近期延迟、失败次数和自动禁用记录。

## 许可证

MIT
