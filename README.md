# MaiBot Model Budget Router

A MaiBot LLM provider plugin for routing model calls across many OpenAI-compatible relay providers.

It is designed for users who have several free, trial, or low-cost relay stations and want to classify them by task, budget, balance, latency, and failure status.

## Features

- Route different MaiBot tasks to different model pools.
- Auto-sync relay providers from MaiBot `model_config.toml`.
- Score candidates by latency, estimated price, remaining balance, daily budget, and priority weight.
- Fail over to the next available model when one provider fails.
- Auto-disable a model/provider pair when upstream returns `429`, rate-limit, quota, or balance errors.
- Support three billing modes:
  - model price
  - fixed price per call
  - token quota

## Install

Install from MaiBot WebUI:

```text
https://github.com/tackyeel/maibot-model-budget-router
```

Then add a provider in MaiBot model management:

```toml
[[api_providers]]
name = "Model Budget Router"
base_url = "http://budget-router.local/v1"
api_key = "budget-router"
client_type = "budget_router"
max_retry = 0
timeout = 80
retry_interval = 1
```

Add logical router models, for example:

```toml
[[models]]
model_identifier = "router:replyer"
name = "Router Replyer"
api_provider = "Model Budget Router"
price_in = 0
price_out = 0
cache = false
cache_price_in = 0
visual = true
force_stream_mode = false

[models.extra_params]
```

Use `Router Replyer` as the first model in `[model_task_config.replyer].model_list`.

Supported task identifiers:

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

## Configure

Open the plugin settings in MaiBot WebUI.

Edit `[pools]` so each task contains real model `name` values from your MaiBot model management page.

Relay providers are auto-synced into `providers.overrides`. For each provider you can set:

- `enabled`: whether this relay can be used
- `balance_yuan`: estimated remaining balance
- `daily_budget_yuan`: daily money limit
- `weight`: priority weight
- `billing_mode`: `model_price`, `per_call`, or `token_quota`
- `price_per_call_yuan`: cost per successful call
- `token_balance`: remaining token quota
- `daily_token_budget`: daily token limit

The plugin does not log in to relay dashboards. Balance and quotas are estimates maintained from your settings and usage.

## Notes

- This plugin only handles OpenAI-compatible chat completion requests.
- Embedding and voice models should stay on their normal providers.
- If a relay is refilled, disable `auto_disable_on_429` temporarily or clear `data/router_state.json` to re-enable automatically disabled candidates.

## License

MIT
