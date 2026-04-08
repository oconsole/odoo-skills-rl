# Model Discovery Guide

## Step 1: Get the Full Picture

Always start with `odoo_model_info(model)` before making any changes. This returns:

```json
{
  "model": "sale.order",
  "name": "Sales Order",
  "default_order": "date_order desc, id desc",
  "rec_name": "name",
  "field_count": 148,
  "fields_by_type": {"many2one": 25, "char": 18, "float": 12, ...},
  "custom_fields": [{"name": "x_notes", "type": "text", "label": "Internal Notes"}],
  "relational_fields": [{"name": "partner_id", "type": "many2one", "target": "res.partner"}],
  "required_fields": [{"name": "partner_id", "type": "many2one", "label": "Customer"}],
  "views": [{"id": 944, "name": "sale.order.form", "type": "form", "priority": 16}],
  "actions": [{"id": 312, "name": "Quotations", "domain": "...", "context": "..."}],
  "defaults": [{"field": "invoice_policy", "value": "\"order\""}]
}
```

## Step 2: Understand What You're Working With

| Field in response | What it tells you |
|-------------------|-------------------|
| `default_order` | Current `_order` — **cannot change at runtime** |
| `rec_name` | Field used for display name in dropdowns |
| `custom_fields` | Existing x_ fields already on the model |
| `required_fields` | Fields that are required at model level |
| `views` | Base views — use their IDs for `inherit_id` |
| `actions` | Window actions — use their IDs for `odoo_modify_action` |
| `defaults` | Current ir.default values (JSON-encoded) |

## Step 3: Get View Details (when modifying views)

```
odoo_get_view(model="sale.order", view_type="form")
```

Returns the **merged** XML after all inheritance. Use this to:
- Find the field names used in the view
- Identify where to insert new fields (XPath targets)
- See what's already visible/hidden

## Step 4: Get Field Details (when adding/querying fields)

```
odoo_get_fields(model="sale.order")
```

Returns all field definitions with type, required, readonly, help text. Use this to:
- Find the exact field name (not the label)
- Check field type before setting a default
- Identify relational field targets

## Odoo 18 / 19 Notes for Discovery

| Item | Odoo 18 | Odoo 19 |
|---|---|---|
| `ir.model.order` (stored mirror of `_order`) | Available — safe to read | Available — safe to read |
| `ir.model.rec_name` | **Does not exist** — never request it | **Does not exist** — never request it |
| `ir.model.abstract` | Does not exist — querying it errors | Available |
| `ir.model.fold_name` | Does not exist — querying it errors | Available |

**Safe `ir.model` query that works on BOTH versions:**

```python
odoo_search_read(
    model="ir.model",
    domain=[["model", "=", target_model]],
    fields=["name", "model", "order", "state", "transient"],
    limit=1,
)
```

**To get `_rec_name`** (since it isn't stored anywhere queryable), inspect `ir.model.fields` for the model and pick `"name"` if it exists, otherwise `"x_name"`, otherwise `"id"`. This is exactly what Odoo itself does.
