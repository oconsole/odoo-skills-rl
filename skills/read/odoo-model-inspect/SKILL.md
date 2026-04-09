---
name: odoo-model-inspect
description: "Inspect, query, and audit Odoo models and records WITHOUT making any changes. Use for: read field definitions, list models, get view XML, search records, count matches, audit data quality, run health diagnostics, explain how a model is structured. WHEN: inspect, look at, show me, what fields, what records, count, audit, list, find, query, check, verify, explore, describe. DO NOT USE WHEN: the user wants to create, modify, update, delete, set defaults, or change anything — switch to odoo-model-customize (write tier)."
license: MIT
metadata:
  author: oconsole
  version: "1.0.0"
  tier: read
---

# Odoo Model Inspect (read-only)

> **READ-ONLY GUARANTEE.** This skill only uses Odoo tools that read data. It must NEVER call `odoo_set_default`, `odoo_create`, `odoo_modify_action`, `odoo_delete`, or `odoo_execute` with mutating methods (`write`, `create`, `unlink`, `action_*`, `button_*`). If the user asks for a change, stop and tell them they need the `odoo-model-customize` skill from the write tier.

## Triggers

Activate this skill when the user wants to:
- See what fields exist on a model
- Look at the structure of a record (form view, tree view, search view)
- Count how many records match some condition
- Find records that meet a filter
- Audit data quality (negative stocks, missing required fields, broken references)
- Diagnose what's going on in the Odoo instance
- Understand how a custom field, view, or window action is configured
- Compare what's installed vs what's available

## Compatibility — Odoo 18 and Odoo 19

Verified on **Odoo 18.0** and **Odoo 19.0**. The read API (`search_read`, `search_count`, `fields_get`, `read`) is stable across both versions. A few schema details to remember:

| Field | Odoo 18 | Odoo 19 |
|---|---|---|
| `ir.model.order` (mirrors `_order`) | Available | Available |
| `ir.model.rec_name` | Does not exist | Does not exist |
| `ir.model.abstract`, `ir.model.fold_name` | Do not exist | Available |
| `ir.module.module.installed_version` | Available | Available |
| `ir.cron.nextcall` | Available | Available |

### Field names that exist in NEITHER version (do not query)

| Wrong | What to use |
|---|---|
| `ir.model.rec_name` | Infer from `ir.model.fields`: `name` if present, else `x_name`, else `id` |
| `ir.module.module.version` | `installed_version` |
| `ir.cron.numbercall`, `ir.cron.next_call` | `nextcall` (note the spelling) |
| `res.users.last_login` | `login_date` |
| `ir.logging.path_ids` | `path` (Char) |
| `ir.module.module.dependency.state` | Computed, not stored — read records and filter client-side |

## Allowed Tools (READ ONLY)

| Tool | Purpose |
|------|---------|
| `odoo_model_info` | Get comprehensive model metadata (fields, views, actions, defaults) |
| `odoo_get_fields` | Field definitions for a model |
| `odoo_get_view` | Fully merged view XML after inheritance |
| `odoo_search_read` | Read records matching a domain |
| `odoo_search_count` | Count records matching a domain (no data transfer) |
| `odoo_list_models` | List installed models, optional keyword filter |
| `odoo_doctor` | Run health diagnostics |
| `odoo_execute` | **Only with read methods**: `read`, `read_group`, `name_search`, `default_get`, `fields_get`, `name_get`, `search`, `search_count`. **NEVER** `write`, `create`, `unlink`, or any `action_*` / `button_*`. |

## Forbidden Tools

| Tool | Why forbidden in this skill |
|------|---|
| `odoo_set_default` | Mutates `ir.default` records |
| `odoo_create` | Creates records |
| `odoo_modify_action` | Mutates `ir.actions.act_window` |
| `odoo_delete` | Deletes records |
| `odoo_execute` with `write` / `create` / `unlink` / `action_*` / `button_*` | Mutates state |

If the user asks for any of these, respond:

> "That's a write operation — this skill is read-only. Install the `odoo-skills-write` plugin and the `odoo-model-customize` skill will handle it."

## Rules

1. **Verify scope before fetching detail.** Use `odoo_search_count` first to gauge how many records match. If the count is huge, narrow the domain before reading.
2. **Specific domains, not broad scans.** Always include the most discriminating filters you have. Avoid `odoo_search_read(model, [], …)` unless the user explicitly asked for "all".
3. **Limit reads.** Default `limit=50`. Bump to 200 only if the user wants a full listing.
4. **Project only the fields you need.** Don't ask for `*` — list field names so the response stays small.
5. **Confirm model existence first.** When unsure whether a model is installed, call `odoo_list_models(keyword=…)` or wrap the read in error handling. Do not assume `crm.lead`, `helpdesk.ticket`, etc. exist on every instance.
6. **Be honest about limits.** If the connected Odoo doesn't expose what the user asked for (e.g., `_order` for a model on Odoo 17 where `ir.model.order` was added later), say so plainly.

---

## Steps

| # | Action | Tool |
|---|--------|------|
| 1 | **Identify the model** — confirm it exists on this instance | `odoo_list_models` |
| 2 | **Get the shape** — fields, views, actions, defaults in one call | `odoo_model_info` |
| 3 | **Scope the query** — count first to gauge magnitude | `odoo_search_count` |
| 4 | **Read the data** — narrow domain, projected fields, sensible limit | `odoo_search_read` |
| 5 | **Cross-reference if needed** — related models for full picture | `odoo_search_read` on related model |
| 6 | **Report** — summarize findings, group by severity (OK / Warning / Critical) when auditing |

---

## Common Inspection Recipes

### "What fields are on this model?"
```
odoo_model_info(model="sale.order")
```
Returns field count, types, custom fields, required fields, relational fields. One call.

### "How many X are there?"
```
odoo_search_count(model="sale.order", domain=[["state","=","sale"]])
```

### "Find products with negative stock"
```
odoo_search_count(model="stock.quant", domain=[["quantity","<",0]])
# If > 0, fetch details:
odoo_search_read(model="stock.quant",
    domain=[["quantity","<",0]],
    fields=["product_id","location_id","quantity","reserved_quantity"],
    limit=100)
```

### "Audit cron job health"
```
# Cron states
odoo_search_read(model="ir.cron",
    domain=[["active","=",True]],
    fields=["name","nextcall","interval_number","interval_type"],
    limit=200)
# Recent failures
odoo_search_count(model="ir.logging",
    domain=[["level","=","ERROR"],["create_date",">",one_day_ago]])
```
Note `ir.cron.nextcall` (not `numbercall`), `ir.logging.path` (not `path_ids`).

### "List installed modules"
```
odoo_search_read(model="ir.module.module",
    domain=[["state","=","installed"]],
    fields=["name","installed_version","summary"],
    limit=500)
```
Note `installed_version`, not `version`.

### "Show defaults set for a model"
```
odoo_search_read(model="ir.default",
    domain=[["field_id.model_id.model","=","sale.order"]],
    fields=["field_id","user_id","company_id","json_value"],
    limit=100)
```
`ir.default` has no `model_id`/`model` column — traverse `field_id`.

### "Get the rendered form view"
```
odoo_get_view(model="res.partner", view_type="form")
```
Returns merged XML after inheritance — what the user actually sees.

---

## Reporting

When you've finished an inspection, present results in the format that matches the question:

- **Single record / structure question** → field table or JSON snippet
- **Audit / health check** → grouped by severity (Critical / Warning / OK), with counts
- **Listing** → table with the most informative columns first
- **Comparison** → side-by-side table

Highlight anything surprising — broken references, unusual states, mismatches between related models.

---

## Common Pitfalls (auto-curated by RL)

_Maintained automatically by the SkillRL self-edit loop. Each bullet is a prescriptive rule learned from a real failed episode._

<!-- AUTO-CURATED-START -->
- NEVER use `numbercall` on `ir.cron`; use `nextcall` and `state` to identify stuck scheduled actions instead.
- ALWAYS use `installed_version` on `ir.module.module`; NEVER use `version` (Invalid field).
- WHEN querying date ranges on `ir.logging`, use `fields.Datetime.now()` in Python context, not `now()` in domain expressions.
- NEVER filter on `ir.module.module.dependency.state`; it is not stored and cannot be queried directly.
- WHEN checking module dependencies, query `ir.module.module.dependency` separately, not as a nested field filter.
- NEVER pass list values directly in domain filters; wrap multi-value conditions in proper OR/AND tuples.
- NEVER filter on `ir.module.module.category` or `installable`; use `state` field instead to identify uninstalled modules.
- NEVER pass bare list values in domain filters on `ir.module.module`; wrap conditions in proper tuple syntax like `[('state', 'in', ['uninstalled'])]`.
- ON `stock.move`, use `quantity` not `quantity_done`; `quantity_done` is invalid for filtering.
- NEVER query `product.product.qty_available` in domain filters; it is computed and not stored—fetch product IDs first, then read the field.
- ON `account.payment`, use `date` not `payment_date` to filter by payment timing.
- ON `account.move`, the field is `move_type` not `type`; use `move_type` in domain filters for invoice classification.
- NEVER use `%(date_minus_7_days)s` syntax in domain filters; calculate dates in Python and pass literal ISO strings like `'2024-01-15'`.
- WHEN filtering `account.move` by date range, compute the target date in Python first, then pass it as a string in the domain.
- ON `ir.cron`, use `nextcall` and `state` to identify stuck actions; NEVER use `last_call` (Invalid field).
- NEVER filter on `installable` field on `ir.module.module`; use `state` field to identify uninstalled modules.
- NEVER use `now()` in domain filters on `stock.move`; calculate the current datetime in Python first.
- NEVER read `name` field on `stock.move` for identification; use `id` or `reference` field instead.
- NEVER use `%(date_start)s` or similar placeholders in domain filters; calculate dates in Python and pass literal ISO strings.
- NEVER use `now()` in domain filter expressions; calculate the current datetime in Python first, then pass as ISO string.
- NEVER filter on `ir.module.module.dependency.state`; query `ir.module.module.dependency` separately with its own search.
- NEVER filter on `quantity_done` on `stock.move`; verify the correct field via `odoo_get_fields(model='stock.move')`.
- NEVER filter on `qty_done` on `stock.move.line`; verify the correct field via `odoo_get_fields(model='stock.move.line')`.
- NEVER query `qty_available` on `stock.quant` in domain filters; it is computed—fetch quant IDs first, then read the field.
- NEVER filter on `invoice_due_date` on `account.move`; verify the correct field via `odoo_get_fields(model='account.move')`.
- NEVER use placeholder syntax like `%(today-7d)s` in domain filters; calculate the target date in Python first, then pass as ISO string.
- NEVER filter on `numbercall` on `ir.cron`; verify the correct field via `odoo_get_fields(model='ir.cron')`.
<!-- AUTO-CURATED-END -->
