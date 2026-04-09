#!/usr/bin/env python3
"""seed_demo_data.py — populate the test Odoo with rich, tagged demo data
so the cold-start validator can probe realistic operator workflows.

The default test instance is too sparse for hard tasks: there are no stuck
MOs, no aged WIP, no multi-level BoMs, no missing-component shortages,
no orderpoint records on common products. The validator's harder tasks
end up trivially failing on both baseline and with-skill agents because
the data the question references doesn't exist.

This script creates that data using the same conventions the demo-tier
skill teaches an agent to use:

  - Names prefixed with "[DEMO]"
  - Origin field on MOs prefixed with "[DEMO]-FIXTURE-..."
  - Custom finished products use "[DEMO] " name + "x_demo_..." default code

So everything is trivially findable for cleanup. Direct state writes are
used (the seeder is a fixture builder, not an agent — the SKILL.md
boundary about not writing state directly applies to agents).

Modes:
    python scripts/seed_demo_data.py --status      # what's already seeded
    python scripts/seed_demo_data.py --apply       # create everything
    python scripts/seed_demo_data.py --cleanup     # remove everything

The script is idempotent: --apply twice does not create duplicates.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from run import OdooClient, log  # type: ignore

import os
from dotenv import load_dotenv
load_dotenv(Path.home() / ".hermes" / ".env")
load_dotenv(PROJECT_ROOT / ".env")


# ──────────────────────────────────────────────────────────────────────────
# Tagging conventions (must match demo skill SKILL.md)
# ──────────────────────────────────────────────────────────────────────────

DEMO_NAME_PREFIX = "[DEMO]"
DEMO_ORIGIN_PREFIX = "[DEMO]-FIXTURE"
DEMO_CODE_PREFIX = "x_demo_"


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def search_one(odoo: OdooClient, model: str, domain: list, fields=None):
    """Return the first matching record or None."""
    fields = fields or ["id"]
    r = odoo.search_read(model, domain, fields, limit=1)
    if isinstance(r, dict):
        r = r.get("records", [])
    return r[0] if r else None


def search_many(odoo: OdooClient, model: str, domain: list, fields=None, limit=200):
    fields = fields or ["id"]
    r = odoo.search_read(model, domain, fields, limit)
    if isinstance(r, dict):
        r = r.get("records", [])
    return r or []


def create(odoo: OdooClient, model: str, values: dict) -> int:
    """Create a record and return its id.

    Odoo's create() is decorated @api.model_create_multi, so it always
    receives a list of dicts and returns a list of ids — even when we
    only want one. Unwrap to a single int here.
    """
    result = odoo.execute(model, "create", [values])
    if isinstance(result, list):
        return result[0]
    return result


def write(odoo: OdooClient, model: str, ids: list[int], values: dict) -> bool:
    # OdooClient.execute spreads *args into the JSON-RPC positional list,
    # so the right call shape is execute(model, method, ids, vals).
    return odoo.execute(model, "write", ids, values)


def unlink(odoo: OdooClient, model: str, ids: list[int]) -> bool:
    return odoo.execute(model, "unlink", ids)


def find_or_create_product(odoo: OdooClient, name: str, code: str, list_price: float = 100.0) -> dict:
    """Idempotent product.template create. Returns {'tmpl_id', 'product_id'}."""
    existing = search_one(odoo, "product.template", [["default_code", "=", code]],
                          ["id", "name"])
    if existing:
        # also fetch the product.product variant id
        prod = search_one(odoo, "product.product", [["default_code", "=", code]], ["id"])
        return {"tmpl_id": existing["id"], "product_id": prod["id"] if prod else None}

    tmpl_id = create(odoo, "product.template", {
        "name": name,
        "default_code": code,
        "type": "consu",         # consumable — works on all instances regardless of stock module
        "list_price": list_price,
        "sale_ok": False,
        "purchase_ok": False,
    })
    # Find the auto-created variant
    prod = search_one(odoo, "product.product", [["product_tmpl_id", "=", tmpl_id]], ["id"])
    log.info("  created product %s (tmpl=%d, variant=%s)", name, tmpl_id, prod["id"] if prod else "?")
    return {"tmpl_id": tmpl_id, "product_id": prod["id"] if prod else None}


def find_or_create_bom(odoo: OdooClient, product_tmpl_id: int, code: str,
                       components: list[tuple[int, float]],
                       qty: float = 1.0) -> int:
    """Idempotent mrp.bom create. components = [(product_id, qty), ...]."""
    existing = search_one(odoo, "mrp.bom", [["code", "=", code]], ["id"])
    if existing:
        return existing["id"]

    bom_id = create(odoo, "mrp.bom", {
        "product_tmpl_id": product_tmpl_id,
        "product_qty": qty,
        "type": "normal",
        "code": code,
        "bom_line_ids": [
            (0, 0, {"product_id": pid, "product_qty": q})
            for pid, q in components
        ],
    })
    log.info("  created BoM %s (id=%d)", code, bom_id)
    return bom_id


# ──────────────────────────────────────────────────────────────────────────
# Apply
# ──────────────────────────────────────────────────────────────────────────

def apply_seed(odoo: OdooClient) -> dict:
    """Create all the demo data. Idempotent."""
    summary = {"products": [], "boms": [], "mos": [], "orderpoints": [], "invoices": []}

    # ── Step 1: find real components we can reference ────────────────────
    # We need a couple of existing products to use as BoM components.
    bolt = search_one(odoo, "product.product", [["default_code", "=", "CONS_89957"]],
                      ["id", "name", "default_code"])
    screw = search_one(odoo, "product.product", [["default_code", "=", "CONS_25630"]],
                       ["id", "name", "default_code"])
    pen = search_one(odoo, "product.product", [["default_code", "=", "CONS_0001"]],
                     ["id", "name", "default_code"])
    if not bolt or not screw or not pen:
        sys.exit("ERROR: could not find seed-reference products (bolt/screw/pen). "
                 "Is this the expected demo Odoo instance?")

    # ── Step 2: multi-level BoM hierarchy ─────────────────────────────────
    #   [DEMO] Premium Cabinet  (top-level finished good)
    #     ├── [DEMO] Cabinet Frame  (sub-assembly with its own BoM)
    #     │     ├── 4× Bolt
    #     │     └── 8× Screw
    #     ├── [DEMO] Cabinet Door  (sub-assembly with its own BoM)
    #     │     ├── 2× Bolt
    #     │     └── 6× Screw
    #     └── 6× Bolt (direct component)
    log.info("Step 2: creating multi-level BoM hierarchy")

    frame = find_or_create_product(odoo, f"{DEMO_NAME_PREFIX} Cabinet Frame",
                                   f"{DEMO_CODE_PREFIX}cabinet_frame", list_price=80.0)
    door = find_or_create_product(odoo, f"{DEMO_NAME_PREFIX} Cabinet Door",
                                  f"{DEMO_CODE_PREFIX}cabinet_door", list_price=60.0)
    cabinet = find_or_create_product(odoo, f"{DEMO_NAME_PREFIX} Premium Cabinet",
                                     f"{DEMO_CODE_PREFIX}premium_cabinet", list_price=350.0)
    summary["products"] = [frame, door, cabinet]

    # Sub-assembly BoMs
    frame_bom = find_or_create_bom(odoo, frame["tmpl_id"],
                                   f"{DEMO_NAME_PREFIX}-BOM-frame",
                                   components=[(bolt["id"], 4), (screw["id"], 8)])
    door_bom = find_or_create_bom(odoo, door["tmpl_id"],
                                  f"{DEMO_NAME_PREFIX}-BOM-door",
                                  components=[(bolt["id"], 2), (screw["id"], 6)])
    # Top-level BoM uses the sub-assembly products as components
    cabinet_bom = find_or_create_bom(odoo, cabinet["tmpl_id"],
                                     f"{DEMO_NAME_PREFIX}-BOM-cabinet",
                                     components=[
                                         (frame["product_id"], 1),
                                         (door["product_id"], 2),  # 2 doors per cabinet
                                         (bolt["id"], 6),           # plus 6 direct bolts
                                     ])
    summary["boms"] = [frame_bom, door_bom, cabinet_bom]

    # ── Step 3: stuck MOs (confirmed, >14 days old, no reservations) ─────
    log.info("Step 3: creating stuck MOs (confirmed, >14d old)")
    twenty_days_ago = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")

    existing_stuck = search_many(odoo, "mrp.production",
                                 [["origin", "=", f"{DEMO_ORIGIN_PREFIX}-stuck"]],
                                 ["id"])
    if not existing_stuck:
        for i in range(4):
            mo_id = create(odoo, "mrp.production", {
                "product_id": cabinet["product_id"],
                "product_qty": 5 + i,
                "bom_id": cabinet_bom,
                "origin": f"{DEMO_ORIGIN_PREFIX}-stuck",
                "date_start": twenty_days_ago,
            })
            # Force into 'confirmed' state without doing reservation
            try:
                write(odoo, "mrp.production", [mo_id], {"state": "confirmed"})
            except Exception as e:
                log.warning("    could not force confirmed state: %s", e)
            summary["mos"].append({"id": mo_id, "kind": "stuck"})
            log.info("  created stuck MO id=%d", mo_id)
    else:
        log.info("  %d stuck MOs already exist, skipping", len(existing_stuck))
        summary["mos"].extend([{"id": m["id"], "kind": "stuck"} for m in existing_stuck])

    # ── Step 4: WIP MOs (in progress, >7 days old) ───────────────────────
    log.info("Step 4: creating WIP MOs (progress, >7d old)")
    ten_days_ago = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

    existing_wip = search_many(odoo, "mrp.production",
                               [["origin", "=", f"{DEMO_ORIGIN_PREFIX}-wip"]],
                               ["id"])
    if not existing_wip:
        for i in range(3):
            mo_id = create(odoo, "mrp.production", {
                "product_id": door["product_id"],
                "product_qty": 10 + i * 5,
                "bom_id": door_bom,
                "origin": f"{DEMO_ORIGIN_PREFIX}-wip",
                "date_start": ten_days_ago,
            })
            try:
                write(odoo, "mrp.production", [mo_id], {"state": "progress"})
            except Exception as e:
                log.warning("    could not force progress state: %s", e)
            summary["mos"].append({"id": mo_id, "kind": "wip"})
            log.info("  created WIP MO id=%d", mo_id)
    else:
        log.info("  %d WIP MOs already exist, skipping", len(existing_wip))
        summary["mos"].extend([{"id": m["id"], "kind": "wip"} for m in existing_wip])

    # ── Step 5: finished MOs with mixed on-time / late dates ─────────────
    log.info("Step 5: creating finished MOs (mix of on-time and late)")
    existing_done = search_many(odoo, "mrp.production",
                                [["origin", "=", f"{DEMO_ORIGIN_PREFIX}-done"]],
                                ["id"])
    if not existing_done:
        # 5 finished MOs: 3 on-time, 2 late
        scenarios = [
            (frame, frame_bom, 25, -3, True),    # planned 25d ago, finished on time
            (door, door_bom, 20, -2, True),      # planned 20d ago, on time
            (cabinet, cabinet_bom, 15, -1, True),# planned 15d ago, on time
            (frame, frame_bom, 18, +5, False),   # late by 5 days
            (door, door_bom, 12, +3, False),     # late by 3 days
        ]
        for prod, bom, days_ago, slip_days, on_time in scenarios:
            planned_start = datetime.now() - timedelta(days=days_ago)
            actual_finish = planned_start + timedelta(days=2 + slip_days)
            mo_id = create(odoo, "mrp.production", {
                "product_id": prod["product_id"],
                "product_qty": 5,
                "bom_id": bom,
                "origin": f"{DEMO_ORIGIN_PREFIX}-done",
                "date_start": planned_start.strftime("%Y-%m-%d %H:%M:%S"),
                "date_finished": actual_finish.strftime("%Y-%m-%d %H:%M:%S"),
            })
            try:
                write(odoo, "mrp.production", [mo_id],
                      {"state": "done"})
            except Exception as e:
                log.warning("    could not force done state: %s", e)
            summary["mos"].append({"id": mo_id, "kind": "done", "on_time": on_time})
            log.info("  created done MO id=%d (on_time=%s)", mo_id, on_time)
    else:
        log.info("  %d done MOs already exist, skipping", len(existing_done))

    # ── Step 6: orderpoint on Whiteboard Pen ─────────────────────────────
    log.info("Step 6: creating orderpoint on Whiteboard Pen")
    existing_op = search_one(odoo, "stock.warehouse.orderpoint",
                             [["name", "=like", f"{DEMO_NAME_PREFIX}%"],
                              ["product_id", "=", pen["id"]]],
                             ["id"])
    if not existing_op:
        warehouse = search_one(odoo, "stock.warehouse", [], ["id", "lot_stock_id"])
        if warehouse:
            try:
                op_id = create(odoo, "stock.warehouse.orderpoint", {
                    "name": f"{DEMO_NAME_PREFIX} Pen reorder",
                    "product_id": pen["id"],
                    "product_min_qty": 50.0,
                    "product_max_qty": 200.0,
                    "warehouse_id": warehouse["id"],
                    "location_id": warehouse["lot_stock_id"][0],
                })
                summary["orderpoints"].append(op_id)
                log.info("  created orderpoint id=%d", op_id)
            except Exception as e:
                log.warning("  orderpoint create failed: %s", e)
    else:
        log.info("  orderpoint already exists, skipping")
        summary["orderpoints"].append(existing_op["id"])

    # ── Step 7: unpaid invoice posted in last 7 days ─────────────────────
    log.info("Step 7: creating recent unpaid invoice")
    existing_inv = search_one(odoo, "account.move",
                              [["ref", "=", f"{DEMO_NAME_PREFIX}-INV-fixture"]],
                              ["id"])
    if not existing_inv:
        partner = search_one(odoo, "res.partner",
                             [["customer_rank", ">", 0]], ["id", "name"])
        if partner:
            try:
                inv_id = create(odoo, "account.move", {
                    "move_type": "out_invoice",
                    "partner_id": partner["id"],
                    "ref": f"{DEMO_NAME_PREFIX}-INV-fixture",
                    "invoice_date": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"),
                    "invoice_line_ids": [
                        (0, 0, {
                            "name": f"{DEMO_NAME_PREFIX} Premium Cabinet",
                            "quantity": 2,
                            "price_unit": 350.0,
                        }),
                    ],
                })
                summary["invoices"].append(inv_id)
                log.info("  created invoice id=%d (draft, unpaid)", inv_id)
            except Exception as e:
                log.warning("  invoice create failed: %s", e)
    else:
        log.info("  invoice already exists, skipping")
        summary["invoices"].append(existing_inv["id"])

    return summary


# ──────────────────────────────────────────────────────────────────────────
# Cleanup
# ──────────────────────────────────────────────────────────────────────────

def cleanup_seed(odoo: OdooClient) -> dict:
    """Find and unlink everything tagged as demo fixture data."""
    removed = {"mos": 0, "boms": 0, "products": 0, "orderpoints": 0, "invoices": 0}

    # 1. MOs (children first to avoid FK issues)
    mo_ids = [r["id"] for r in search_many(odoo, "mrp.production",
                                           [["origin", "=like", f"{DEMO_ORIGIN_PREFIX}%"]],
                                           ["id"], limit=500)]
    if mo_ids:
        try:
            # Cancel before unlink so state machine doesn't block
            write(odoo, "mrp.production", mo_ids, {"state": "cancel"})
        except Exception:
            pass
        try:
            unlink(odoo, "mrp.production", mo_ids)
            removed["mos"] = len(mo_ids)
        except Exception as e:
            log.warning("  unlink MOs failed: %s", e)

    # 2. Invoices
    inv_ids = [r["id"] for r in search_many(odoo, "account.move",
                                            [["ref", "=", f"{DEMO_NAME_PREFIX}-INV-fixture"]],
                                            ["id"])]
    if inv_ids:
        try:
            write(odoo, "account.move", inv_ids, {"state": "draft"})
        except Exception:
            pass
        try:
            unlink(odoo, "account.move", inv_ids)
            removed["invoices"] = len(inv_ids)
        except Exception as e:
            log.warning("  unlink invoices failed: %s", e)

    # 3. Orderpoints
    op_ids = [r["id"] for r in search_many(odoo, "stock.warehouse.orderpoint",
                                           [["name", "=like", f"{DEMO_NAME_PREFIX}%"]],
                                           ["id"])]
    if op_ids:
        try:
            unlink(odoo, "stock.warehouse.orderpoint", op_ids)
            removed["orderpoints"] = len(op_ids)
        except Exception as e:
            log.warning("  unlink orderpoints failed: %s", e)

    # 4. BoMs
    bom_ids = [r["id"] for r in search_many(odoo, "mrp.bom",
                                            [["code", "=like", f"{DEMO_NAME_PREFIX}%"]],
                                            ["id"])]
    if bom_ids:
        try:
            unlink(odoo, "mrp.bom", bom_ids)
            removed["boms"] = len(bom_ids)
        except Exception as e:
            log.warning("  unlink BoMs failed: %s", e)

    # 5. Products
    prod_tmpl_ids = [r["id"] for r in search_many(odoo, "product.template",
                                                  [["default_code", "=like", f"{DEMO_CODE_PREFIX}%"]],
                                                  ["id"])]
    if prod_tmpl_ids:
        try:
            unlink(odoo, "product.template", prod_tmpl_ids)
            removed["products"] = len(prod_tmpl_ids)
        except Exception as e:
            log.warning("  unlink products failed: %s", e)

    return removed


# ──────────────────────────────────────────────────────────────────────────
# Status
# ──────────────────────────────────────────────────────────────────────────

def status_seed(odoo: OdooClient) -> None:
    """Report current demo data state."""
    print()
    print("=" * 60)
    print(" DEMO DATA STATUS")
    print("=" * 60)

    mo_kinds = [
        ("stuck", f"{DEMO_ORIGIN_PREFIX}-stuck"),
        ("wip",   f"{DEMO_ORIGIN_PREFIX}-wip"),
        ("done",  f"{DEMO_ORIGIN_PREFIX}-done"),
    ]
    for kind, origin in mo_kinds:
        n = len(search_many(odoo, "mrp.production",
                            [["origin", "=", origin]], ["id"]))
        print(f"  MOs ({kind:8s})    {n:>5}")

    boms = len(search_many(odoo, "mrp.bom",
                           [["code", "=like", f"{DEMO_NAME_PREFIX}%"]], ["id"]))
    print(f"  BoMs               {boms:>5}")

    prods = len(search_many(odoo, "product.template",
                            [["default_code", "=like", f"{DEMO_CODE_PREFIX}%"]], ["id"]))
    print(f"  Products           {prods:>5}")

    ops = len(search_many(odoo, "stock.warehouse.orderpoint",
                          [["name", "=like", f"{DEMO_NAME_PREFIX}%"]], ["id"]))
    print(f"  Orderpoints        {ops:>5}")

    invs = len(search_many(odoo, "account.move",
                           [["ref", "=", f"{DEMO_NAME_PREFIX}-INV-fixture"]], ["id"]))
    print(f"  Invoices           {invs:>5}")
    print()


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Create demo data")
    parser.add_argument("--cleanup", action="store_true", help="Remove all demo data")
    parser.add_argument("--status", action="store_true", help="Show what's currently seeded")
    parser.add_argument("--odoo-url", default=os.environ.get("ODOO_URL", "http://13.210.13.41:8069"))
    parser.add_argument("--odoo-db", default=os.environ.get("ODOO_DB", "odoo-clawd-19"))
    args = parser.parse_args()

    if not (args.apply or args.cleanup or args.status):
        args.status = True  # default: just show status

    odoo = OdooClient(args.odoo_url, args.odoo_db, "admin", password="admin")
    odoo.authenticate()
    log.info("Connected to Odoo %s", odoo.version)

    if args.cleanup:
        log.info("Cleaning up demo data...")
        removed = cleanup_seed(odoo)
        print()
        print("Removed:")
        for k, v in removed.items():
            print(f"  {k:15s} {v}")

    if args.apply:
        log.info("Applying demo data seed...")
        summary = apply_seed(odoo)
        print()
        print("Created / found:")
        print(f"  products    {len(summary['products'])}")
        print(f"  BoMs        {len(summary['boms'])}")
        print(f"  MOs         {len(summary['mos'])}")
        print(f"  orderpoints {len(summary['orderpoints'])}")
        print(f"  invoices    {len(summary['invoices'])}")

    if args.status:
        status_seed(odoo)

    odoo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
