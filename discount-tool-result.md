# Loyalty Discount Tool — raw result payloads

Captured: 2026-09-13T15:10:54

The AgentCore runtime logs record tool names but not tool results, so these payloads were captured by invoking the tool directly. Input for both runs:

```json
{
  "loyalty_points": 4250,
  "tier": "Gold",
  "order_total": 450.0,
  "product_category": "standard"
}
```


## Success path — AgentCore Code Interpreter

```json
{
  "order_total": 450.0,
  "tier": "Gold",
  "tier_discount_rate": 0.1,
  "tier_discount_pct": 10.0,
  "points_redeemed": 4000,
  "points_value_usd": 40.0,
  "tier_discount": 41.0,
  "final_total": 369.0,
  "total_savings": 81.0,
  "points_earned": 369,
  "remaining_points": 250
}
```


## Fallback path — Code Interpreter unavailable

`code_session` was patched to raise, forcing the tool's fallback branch.

```json
{
  "order_total": 450.0,
  "tier": "Gold",
  "tier_discount_rate": 0.1,
  "tier_discount_pct": 10.0,
  "points_redeemed": 0,
  "points_value_usd": 0.0,
  "tier_discount": 45.0,
  "final_total": 405.0,
  "total_savings": 45.0,
  "points_earned": 405,
  "remaining_points": 4250,
  "note": "Fallback calculation (tier discount only): Simulated Code Interpreter outage"
}
```


## Key-set comparison

| Required field | Success path | Fallback path |
|---|---|---|
| `points_redeemed` | yes | yes |
| `tier_discount_pct` | yes | yes |
| `final_total` | yes | yes |
| `remaining_points` | yes | yes |

Keys present in both paths: `final_total`, `order_total`, `points_earned`, `points_redeemed`, `points_value_usd`, `remaining_points`, `tier`, `tier_discount`, `tier_discount_pct`, `tier_discount_rate`, `total_savings`

Fallback adds: `note` (a `note` field labelling the degraded result).
