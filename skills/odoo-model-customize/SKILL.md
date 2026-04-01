---
name: odoo-model-customize
description: "Customize Odoo models at runtime without custom modules. Set field defaults via ir.default, change list sort order via window actions, create saved filters, add custom x_ fields, create inherited views with XPath, and set up automated actions. WHEN: change default value, set default, sort order, reorder list, default filter, add custom field, customize form, add field to view, change dropdown order, groupby default, saved filter, automated action. DO NOT USE WHEN: override Python methods, change _order class attribute, add stored computed fields, modify core field constraints — those require a custom module."
license: MIT
metadata:
  author: OdooCLI
  version: "1.0.0"
---

# Odoo Model Customization

> **SAFETY BOUNDARY — RUNTIME-SAFE vs MODULE-REQUIRED**
>
> This skill covers customizations that can be done **safely at runtime via RPC** without writing Python code or creating a custom module. Operations that require a custom module are explicitly flagged — do NOT attempt them via RPC.

## Triggers

Activate this skill when user wants to:
- Set or change a field's default value
- Change the sort order of a list/tree view
- Add a default filter or groupby to a menu
- Create a custom field on a model
- Add a field to a form or list view
- Create a saved search filter
- Set up an automated action (on create/write/delete)
- Understand what can be customized at runtime vs what needs a module

> **Scope**: This skill handles runtime-safe customizations only. For creating full Odoo modules with Python model inheritance, use the `odoo-19` development skill.

## Rules

1. **Always start with `odoo_model_info`** — get the full picture before making changes
2. **Runtime-safe operations only** — see [Safety Boundary](references/safety-boundary.md)
3. **Verify after every write** — re-read the record to confirm the change took effect
4. **Confirm destructive changes** — ask user before modifying window actions or views
5. **Use specialized tools** — prefer `odoo_set_default`, `odoo_modify_action`, `odoo_get_view` over raw `odoo_update`

---

## Steps

| # | Action | Reference |
|---|--------|-----------|
| 1 | **Discover** — Run `odoo_model_info(model)` to see fields, views, actions, defaults, and sort order | [Discovery Guide](references/discovery.md) |
| 2 | **Classify** — Is this runtime-safe or module-required? | [Safety Boundary](references/safety-boundary.md) |
| 3 | **Execute** — Use the appropriate method for the operation type | See operation guides below |
| 4 | **Verify** — Re-read the affected record to confirm success | — |
| 5 | **Report** — Show the user what changed (before → after) | — |

---

## Operation Guides

| Operation | Method | Reference |
|-----------|--------|-----------|
| **Set field defaults** | `odoo_set_default` → ir.default | [Defaults Guide](references/defaults.md) |
| **Change list sort order** | `odoo_modify_action` → context.default_order | [Sort Order Guide](references/sort-order.md) |
| **Add default filters/groupby** | `odoo_modify_action` → domain/context | [Filters Guide](references/filters.md) |
| **Create custom fields** | `odoo_create` → ir.model.fields (x_ prefix) | [Custom Fields Guide](references/custom-fields.md) |
| **Modify form/tree/search views** | `odoo_create` → ir.ui.view (inherited + XPath) | [View Inheritance Guide](references/view-inheritance.md) |
| **Create saved filters** | `odoo_create` → ir.filters | [Saved Filters Guide](references/saved-filters.md) |
| **Set up automated actions** | `odoo_create` → base.automation | [Automation Guide](references/automation.md) |

---

## What Requires a Custom Module

These operations **cannot** be done at runtime. If the user asks for them, explain that a Python module is needed:

| Operation | Why | What to suggest instead |
|-----------|-----|------------------------|
| Change `_order` on a model | Python class attribute | Use `odoo_modify_action` to set `default_order` on the window action |
| Override methods (create, write, name_get) | Python inheritance | Suggest a custom module with `_inherit` |
| Add stored computed fields | Needs `compute=` + `store=True` | Add a non-computed x_ field + base.automation to populate it |
| Change field `required`/`readonly` at model level | Python class definition | Use inherited view with `required="1"` or `readonly="1"` (visual only) |
| Add Python constraints | `@api.constrains` decorator | Use base.automation with validation logic |
| Add onchange logic | `@api.onchange` decorator | Use base.automation triggered on write |

## MCP Tools

| Tool | Purpose |
|------|---------|
| `odoo_model_info` | Get comprehensive model metadata in one call |
| `odoo_set_default` | Set/update/clear field default values |
| `odoo_get_view` | Get fully rendered (merged) view XML |
| `odoo_modify_action` | Change window action domain/context/sort/limit |
| `odoo_search_read` | Query any Odoo model |
| `odoo_create` | Create records (ir.model.fields, ir.ui.view, ir.filters, base.automation) |
| `odoo_get_fields` | Get field definitions for a model |
