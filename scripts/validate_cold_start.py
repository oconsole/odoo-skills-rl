#!/usr/bin/env python3
"""validate_cold_start.py — does the auto-curated SKILL.md actually help a fresh agent?

The RL pipeline curates a "Common Pitfalls" section in each tier's SKILL.md.
We've been measuring success via the proxy reward in run.py, but the real
question is: if a fresh agent loads only the SKILL.md (no skill bank, no
RL training), does it make fewer Odoo errors than a baseline agent without it?

This harness answers that. For each hold-out task it runs the same task
twice — once with NO skill content injected (baseline), once with the
auto-curated SKILL.md loaded — and reports the per-task and aggregate
deltas.

Key metrics:
  - odoo_errors:    number of tool calls that returned "error"
  - tool_calls:     total tool calls (lower = more efficient)
  - completed:      did the agent produce a substantive answer
  - duration_s:     wall clock
  - bullet_hits:    did the agent's tool args reference any of the
                    wrong-field tokens documented in the bullets?

Run:
    python scripts/validate_cold_start.py                    # both tiers
    python scripts/validate_cold_start.py --tier read        # read only
    python scripts/validate_cold_start.py --tier write       # write only
    python scripts/validate_cold_start.py --jsonl out.jsonl  # raw results
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Reuse the production tooling so the agent under test sees exactly the
# same Odoo surface area as RL episodes.
from run import (  # type: ignore
    OdooClient,
    ODOO_TOOLS,
    execute_tool,
    create_llm_client,
    log,
)

import os
from dotenv import load_dotenv
load_dotenv(Path.home() / ".hermes" / ".env")
load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Hold-out task set
# ---------------------------------------------------------------------------
#
# Each task is a probe for one or more bullets in the auto-curated section.
# `wrong_tokens` lists the field/method names that the bullet TELLS the agent
# to avoid; if any tool call uses one of these, count it as a bullet violation.
# Tasks are NOT in TASKS in run.py, so neither agent has seen them in training.

@dataclass
class HoldoutTask:
    id: str
    tier: str            # read | write
    task: str            # the user prompt
    wrong_tokens: list[str] = field(default_factory=list)
    notes: str = ""


HOLDOUT_TASKS: list[HoldoutTask] = [
    # ─── READ TIER probes ───────────────────────────────────────────────────
    HoldoutTask(
        id="r-cron-stuck",
        tier="read",
        task="Find any scheduled cron jobs that look stuck (haven't run recently). Report them with the next-call time.",
        wrong_tokens=["numbercall", "last_call"],
        notes="bullet: NEVER use numbercall / last_call on ir.cron",
    ),
    HoldoutTask(
        id="r-module-versions",
        tier="read",
        task="Show me the version number for every installed Odoo module on this instance.",
        wrong_tokens=["version"],
        notes="bullet: use installed_version not version on ir.module.module",
    ),
    HoldoutTask(
        id="r-broken-deps",
        tier="read",
        task="Check whether any installed modules have broken dependencies (deps that themselves are not installed).",
        wrong_tokens=["dependency.state"],  # filter on this is not stored
        notes="bullet: never filter ir.module.module.dependency.state",
    ),
    HoldoutTask(
        id="r-recent-errors",
        tier="read",
        task="Find ir.logging entries from the last hour that have level=ERROR.",
        wrong_tokens=["now()", "%(today", "%(date_"],
        notes="bullet: never use now() / %(today)s in domain filters",
    ),
    HoldoutTask(
        id="r-pending-moves",
        tier="read",
        task="List all stock moves that are still in 'assigned' state — show the product, source location, and current quantity.",
        wrong_tokens=["quantity_done", "qty_done"],
        notes="bullet: stock.move uses quantity not quantity_done in Odoo 17+",
    ),
    HoldoutTask(
        id="r-customer-invoices",
        tier="read",
        task="List all customer invoices posted in the last 30 days. Group by partner and sum the amount.",
        wrong_tokens=[],  # account.move.move_type — agent should know
        notes="probes account.move.move_type bullet (positive)",
    ),
    HoldoutTask(
        id="r-leads",
        tier="read",
        task="Give me our top 5 sales prospects by likely deal value.",
        wrong_tokens=["crm.lead", "probability"],
        notes="bullet: crm.lead may not exist; probability not on sale.order",
    ),
    HoldoutTask(
        id="r-low-stock",
        tier="read",
        task="Find products that have qty_available below 10 right now.",
        wrong_tokens=["qty_available"],  # in domain filter — it's computed
        notes="bullet: qty_available is computed; fetch IDs first",
    ),
    HoldoutTask(
        id="r-mrp-running",
        tier="read",
        task="Show me all manufacturing orders currently in progress.",
        wrong_tokens=[],
        notes="probes mrp install check + date_start naming",
    ),
    HoldoutTask(
        id="r-default-values",
        tier="read",
        task="What default values are currently set for fields on sale.order? Use ir.default.",
        wrong_tokens=["model_id.model"],  # ir.default has no model_id; use field_id.model_id.model
        notes="probes ir.default schema knowledge",
    ),
    HoldoutTask(
        id="r-control-1",
        tier="read",
        task="How many res.partner records exist on this instance?",
        wrong_tokens=[],
        notes="control: pure read with no bullet relevance",
    ),
    HoldoutTask(
        id="r-control-2",
        tier="read",
        task="What is the current Odoo server version?",
        wrong_tokens=[],
        notes="control: simple version probe",
    ),

    # ─── WRITE TIER probes ──────────────────────────────────────────────────
    HoldoutTask(
        id="w-set-default",
        tier="write",
        task="Set the default value of 'invoice_policy' on product.template to 'order' globally.",
        wrong_tokens=["default_value"],  # bullet says ir.default has no default_value
        notes="bullet: ir.default uses json_value not default_value",
    ),
    HoldoutTask(
        id="w-mrp-create",
        tier="write",
        task="Create one new manufacturing order for the 'Drawer' product (FURN_8855), quantity 5.",
        wrong_tokens=["scheduled_start_date", "date_planned_start"],
        notes="bullet: mrp.production uses date_start in Odoo 17+; do not pass name to create()",
    ),
    HoldoutTask(
        id="w-bom-lookup",
        tier="write",
        task="Find the bill of materials for the Drawer product and show its components.",
        wrong_tokens=[],
        notes="probes mrp.bom field knowledge",
    ),
    HoldoutTask(
        id="w-project-state",
        tier="write",
        task="Mark project 'Office Design' as completed. Whatever 'completed' means on this instance — figure it out.",
        wrong_tokens=[],
        notes="probes project.project.state bullet (no such field)",
    ),
    HoldoutTask(
        id="w-control-1",
        tier="write",
        task="Create a new ir.filters saved filter named 'Active partners' on res.partner that filters active=True.",
        wrong_tokens=[],
        notes="control: clean ir.filters create",
    ),
    HoldoutTask(
        id="w-control-2",
        tier="write",
        task="Add a custom x_internal_notes text field to res.partner.",
        wrong_tokens=[],
        notes="control: clean ir.model.fields create",
    ),

    # ─── HARD: real operator/manager workflows ──────────────────────────────
    # These come from actual Odoo MRP / planner / supervisor questions surfaced
    # by research (Odoo docs, frePPLe, Pragtech, Bista, Odoo Experience 2023).
    # Each task requires 3-5 MCP calls minimum and crosses at least 2 modules.

    # READ — feasibility / "can we ship?"
    HoldoutTask(
        id="r-can-we-ship",
        tier="read",
        task="We need to ship 25 Customizable Desks by Friday. Walk the BoM, check current stock for every component, and tell me yes or no with the gating constraint.",
        wrong_tokens=["date_planned_start", "scheduled_start_date"],
        notes="cross-module SO+MRP+stock; tests BoM traversal + reservation logic",
    ),

    # READ — root cause on a specific late MO
    HoldoutTask(
        id="r-why-mo-late",
        tier="read",
        task="Why is manufacturing order WH/MO/00010 not done yet? Check its components, reservation status, and any blockers.",
        wrong_tokens=["date_planned_start", "scheduled_start_date", "quantity_done"],
        notes="probes mrp.production state machine + stock.move reservation",
    ),

    # READ — stuck MOs (operator pain point)
    HoldoutTask(
        id="r-stuck-mos",
        tier="read",
        task="Find every manufacturing order in 'Confirmed' state for more than 14 days that has zero reserved components. These are stuck — list them with their products and dates.",
        wrong_tokens=["now()", "%(today", "date_planned_start", "scheduled_start_date"],
        notes="date arithmetic in domain + mrp state + reservation check",
    ),

    # READ — multi-level BoM where-used
    HoldoutTask(
        id="r-where-used",
        tier="read",
        task="Where is the Bolt component (CONS_89957) used? List every bill of materials that includes it at any level, and the total quantity consumed across confirmed MOs in the last 12 months.",
        wrong_tokens=[],
        notes="mrp.bom.line traversal + multi-level resolution + historical aggregation",
    ),

    # READ — stock reconciliation
    HoldoutTask(
        id="r-stock-reconcile",
        tier="read",
        task="stock.quant says we have 40 of CONS_89957 but I think we've consumed more — show me recent stock moves for this product so I can reconcile.",
        wrong_tokens=["quantity_done"],
        notes="probes stock.move quantity field rename in Odoo 17+",
    ),

    # READ — KPI: on-time delivery rate
    HoldoutTask(
        id="r-otd-rate",
        tier="read",
        task="What's our on-time delivery rate for finished manufacturing orders in the last 30 days? Break it down by product category.",
        wrong_tokens=["date_planned_start", "scheduled_start_date", "now()", "%(today"],
        notes="KPI rollup; needs date_start vs date_finished comparison + read_group",
    ),

    # READ — WIP aging
    HoldoutTask(
        id="r-wip-aging",
        tier="read",
        task="WIP aging report: which MOs have been 'In Progress' for more than 7 days? Sort oldest first and tell me roughly how much value is sitting on the floor.",
        wrong_tokens=["now()", "%(today", "%(date_"],
        notes="state filter + date arithmetic + cost rollup",
    ),

    # READ — procurement / orderpoint diagnostic
    HoldoutTask(
        id="r-orderpoint-trace",
        tier="read",
        task="The reordering rule for Whiteboard Pen isn't firing. Show me the rule (min/max), current on-hand, any incoming purchase orders, and the last 5 procurement events.",
        wrong_tokens=["qty_min", "qty_max"],  # correct names are product_min_qty / product_max_qty
        notes="orderpoint field-naming pitfall + cross-model trace (orderpoint+stock+po)",
    ),

    # READ — unpaid invoices (operator language)
    HoldoutTask(
        id="r-unpaid-invoices",
        tier="read",
        task="List all customer invoices posted in the last 7 days that are still unpaid. Show partner, amount, and days overdue.",
        wrong_tokens=["type", "post_date", "posted_date", "now()", "%(today"],
        notes="probes account.move.move_type and date computation",
    ),

    # READ — project + MRP intersection
    HoldoutTask(
        id="r-project-mos",
        tier="read",
        task="Project 'Office Design' has work happening. Find any manufacturing orders related to its tasks and report their state and current cost.",
        wrong_tokens=["product_id", "state"],  # project.project.product_id and .state don't exist
        notes="project↔mrp link; probes project.project field hallucinations",
    ),

    # READ — login activity (probes the new login_date / Many2one bullet)
    HoldoutTask(
        id="r-inactive-users",
        tier="read",
        task="Which users haven't logged in within the last 7 days? Sort oldest first.",
        wrong_tokens=["last_login"],
        notes="probes res.users login_date related-field bullet",
    ),

    # WRITE — multi-step MO creation with feasibility check
    HoldoutTask(
        id="w-mo-chain",
        tier="write",
        task="Start a manufacturing run for 10 Drawer (FURN_8855) units. First check the BoM and verify all components are in stock. If everything's available, create the MO. If something's missing, tell me what to order and don't create the MO.",
        wrong_tokens=["scheduled_start_date", "date_planned_start"],
        notes="multi-step write conditional on read; tests do-the-right-thing-don't-just-act",
    ),

    # WRITE — procurement cascade from a shortage
    HoldoutTask(
        id="w-shortage-fix",
        tier="write",
        task="Component CONS_89957 (Bolt) is short. Find every confirmed manufacturing order that needs it, calculate the total deficit, and create a draft purchase order for the right quantity from the cheapest current supplier.",
        wrong_tokens=[],
        notes="cross-module write: BoM trace → MO aggregation → PO create",
    ),

    # ─── HARD: tasks that exercise the seed_demo_data.py fixture ───────────
    # These rely on the [DEMO]-tagged demo data (multi-level BoMs, stuck MOs,
    # WIP MOs, finished MOs with mixed late/on-time dates). Run
    # `python scripts/seed_demo_data.py --apply` first.

    # READ — multi-level BoM resolution
    HoldoutTask(
        id="r-multilevel-bom",
        tier="read",
        task="Walk the bill of materials for the [DEMO] Premium Cabinet product (default code x_demo_premium_cabinet) and show every component including sub-components. For 5 cabinets, give the total quantity of each raw component (no sub-assemblies — only leaf items like Bolt and Screw).",
        wrong_tokens=[],
        notes="multi-level BoM recursion; needs to expand sub-assembly BoMs",
    ),

    # READ — sub-product where-used (recursive)
    HoldoutTask(
        id="r-frame-where-used",
        tier="read",
        task="Find every bill of materials that uses [DEMO] Cabinet Frame (default code x_demo_cabinet_frame) anywhere — directly or via a parent BoM.",
        wrong_tokens=[],
        notes="probes mrp.bom.line traversal across BoMs",
    ),

    # READ — production status snapshot for a finished good
    HoldoutTask(
        id="r-cabinet-status",
        tier="read",
        task="Give me a status snapshot of every manufacturing order for [DEMO] Premium Cabinet, grouped by state (draft / confirmed / progress / done / cancel) with counts.",
        wrong_tokens=[],
        notes="state-grouping on a single product",
    ),

    # READ — throughput in a time window
    HoldoutTask(
        id="r-demo-throughput",
        tier="read",
        task="How many [DEMO] units have we finished producing in the last 30 days, broken down by product?",
        wrong_tokens=["now()", "%(today", "date_planned_start"],
        notes="throughput KPI; date_finished filtering + group-by",
    ),

    # READ — fixture-specific stuck MOs (the seeder created exactly 4)
    HoldoutTask(
        id="r-demo-stuck-mos",
        tier="read",
        task="Find all confirmed manufacturing orders with origin starting with [DEMO]-FIXTURE-stuck. Report their product, quantity, and how long they've been confirmed.",
        wrong_tokens=["date_planned_start", "scheduled_start_date", "now()"],
        notes="origin field filter + state filter + date arithmetic on date_start",
    ),

    # WRITE — create an MO using the multi-level BoM
    HoldoutTask(
        id="w-cabinet-mo",
        tier="write",
        task="Create a manufacturing order for 3 [DEMO] Premium Cabinet units (default code x_demo_premium_cabinet). Use the existing BoM. Set the origin to '[DEMO]-validation-test' so we can find it later.",
        wrong_tokens=["scheduled_start_date", "date_planned_start", "name"],
        notes="probes mrp.production create payload + bom_id resolution",
    ),

    # READ — capacity calculation
    HoldoutTask(
        id="r-cabinet-feasibility",
        tier="read",
        task="If I want to start 5 new [DEMO] Premium Cabinet manufacturing orders right now, walk the BoM and check whether components are available in stock. Tell me how many cabinets we could actually start without running out of any component.",
        wrong_tokens=[],
        notes="feasibility analysis; multi-component constraint",
    ),
]


# ---------------------------------------------------------------------------
# Episode runner
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    task_id: str
    tier: str
    mode: str            # baseline | with-skill
    task: str
    completed: bool
    odoo_errors: int
    tool_calls: int
    bullet_hits: int
    duration_s: float
    final_response: str
    used_tools: list[str]


BASE_SYSTEM = (
    "You are O-CLI, an Odoo operations agent. You have tools to query and "
    "manage a live Odoo instance. Be direct and efficient.\n\n"
    "ALWAYS use the odoo tools to answer questions. Do not guess or make up "
    "data. Verify before mutating. Confirm destructive operations.\n"
)


def _read_skill_md(tier: str) -> str:
    """Snapshot the tier's SKILL.md so concurrent RL writes don't perturb us."""
    name_by_tier = {
        "read": "odoo-model-inspect",
        "write": "odoo-model-customize",
    }
    p = PROJECT_ROOT / "skills" / tier / name_by_tier[tier] / "SKILL.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def _count_bullet_hits(tool_calls: list[dict], wrong_tokens: list[str]) -> int:
    """Count tool calls whose args USE a wrong_token in a way that matters.

    A wrong_token is a real bullet violation when:
      - It appears as a domain leaf (the agent is FILTERING on it), or
      - It appears as a values dict KEY in a create/write call, or
      - It appears as a method argument to odoo_execute on a write-class method.

    A wrong_token in `fields=["x", ...]` is NOT a violation — the agent is
    just asking for the column to be returned, not asserting it exists in
    a query the way a domain filter would.
    """
    if not wrong_tokens:
        return 0

    wrong_lower = [t.lower() for t in wrong_tokens]
    hits = 0
    for tc in tool_calls:
        args = tc.get("args", {}) or {}
        if not isinstance(args, dict):
            continue
        violated = False

        # Check domain — list of [field, op, value] triples
        domain = args.get("domain", [])
        if isinstance(domain, list):
            domain_str = json.dumps(domain, default=str).lower()
            # Strip the field projection (which appears later in args)
            for tok in wrong_lower:
                if tok in domain_str:
                    violated = True
                    break

        # Check write / create values dict KEYS only
        if not violated:
            values = args.get("values", args.get("vals", {}))
            if isinstance(values, dict):
                value_keys = [k.lower() for k in values.keys()]
                for tok in wrong_lower:
                    if tok in value_keys:
                        violated = True
                        break

        # Check odoo_execute method args (kwargs and positional)
        if not violated and tc.get("tool") == "odoo_execute":
            method = (args.get("method") or "").lower()
            if method in {"write", "create", "unlink"}:
                method_blob = json.dumps(
                    {"args": args.get("args"), "kwargs": args.get("kwargs")},
                    default=str,
                ).lower()
                for tok in wrong_lower:
                    if tok in method_blob:
                        violated = True
                        break

        if violated:
            hits += 1
    return hits


def run_one(
    llm_client,
    odoo: OdooClient,
    model: str,
    task: HoldoutTask,
    skill_md_text: Optional[str],
    max_turns: int = 12,
) -> TaskResult:
    """Run a single hold-out task with optional SKILL.md injection.

    Mirrors run.run_episode's loop but exposes the skill content as a
    direct parameter so we can toggle it on/off cleanly.
    """
    system = BASE_SYSTEM
    if skill_md_text:
        system += f"\n# Skill: cold-start guidance\n{skill_md_text}\n"

    messages = [{"role": "user", "content": task.task}]

    tool_calls_log: list[dict] = []
    odoo_errors = 0
    final_response = ""
    t0 = time.time()

    for _turn in range(max_turns):
        try:
            response = llm_client.messages.create(
                model=model,
                system=system,
                messages=messages,
                tools=ODOO_TOOLS,
                temperature=0.3,
                max_tokens=2048,
            )
        except Exception as exc:
            log.warning("  [%s] LLM error: %s", task.id, exc)
            break

        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        messages.append({"role": "assistant", "content": response.content})

        if not tool_uses:
            final_response = "\n".join(text_parts)
            break

        tool_results = []
        for tu in tool_uses:
            result = execute_tool(odoo, tu.name, tu.input or {})
            tool_calls_log.append({
                "tool": tu.name,
                "args": tu.input or {},
                "result_preview": result[:200],
            })
            if '"error"' in result:
                odoo_errors += 1
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

        if text_parts:
            final_response = "\n".join(text_parts)

    duration = time.time() - t0

    return TaskResult(
        task_id=task.id,
        tier=task.tier,
        mode="with-skill" if skill_md_text else "baseline",
        task=task.task,
        completed=bool(final_response and len(final_response) > 30),
        odoo_errors=odoo_errors,
        tool_calls=len(tool_calls_log),
        bullet_hits=_count_bullet_hits(tool_calls_log, task.wrong_tokens),
        duration_s=duration,
        final_response=final_response[:500],
        used_tools=[tc["tool"] for tc in tool_calls_log],
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _aggregate(results: list[TaskResult]) -> dict:
    if not results:
        return {}
    n = len(results)
    return {
        "n": n,
        "completion_rate": sum(1 for r in results if r.completed) / n,
        "mean_errors": sum(r.odoo_errors for r in results) / n,
        "mean_tool_calls": sum(r.tool_calls for r in results) / n,
        "total_bullet_hits": sum(r.bullet_hits for r in results),
        "mean_duration_s": sum(r.duration_s for r in results) / n,
    }


def print_report(baseline: list[TaskResult], with_skill: list[TaskResult]) -> None:
    base = _aggregate(baseline)
    skill = _aggregate(with_skill)

    def fmt_delta(a, b, lower_is_better=True):
        if a == 0 and b == 0:
            return "—"
        delta = b - a
        if a == 0:
            return f"{b:+.2f}"
        pct = (delta / a) * 100
        arrow = "▼" if (delta < 0) == lower_is_better else "▲"
        return f"{delta:+.2f} ({pct:+.0f}%) {arrow}"

    print()
    print("=" * 76)
    print(" COLD-START VALIDATION — does the auto-curated SKILL.md help?")
    print("=" * 76)
    print()
    print(f"  hold-out tasks:      {base.get('n', 0)}")
    print(f"  model:               {os.environ.get('LLM_MODEL', 'claude-haiku-4-5-20251001')}")
    print()
    print(f"  {'metric':<22}{'baseline':>14}{'with-skill':>14}{'delta':>26}")
    print(f"  {'-' * 22}{'-' * 14}{'-' * 14}{'-' * 26}")
    print(f"  {'completion rate':<22}{base.get('completion_rate', 0):>13.0%}"
          f"{skill.get('completion_rate', 0):>14.0%}"
          f"{fmt_delta(base.get('completion_rate', 0), skill.get('completion_rate', 0), lower_is_better=False):>26}")
    print(f"  {'mean odoo errors':<22}{base.get('mean_errors', 0):>14.2f}"
          f"{skill.get('mean_errors', 0):>14.2f}"
          f"{fmt_delta(base.get('mean_errors', 0), skill.get('mean_errors', 0)):>26}")
    print(f"  {'mean tool calls':<22}{base.get('mean_tool_calls', 0):>14.2f}"
          f"{skill.get('mean_tool_calls', 0):>14.2f}"
          f"{fmt_delta(base.get('mean_tool_calls', 0), skill.get('mean_tool_calls', 0)):>26}")
    print(f"  {'bullet violations':<22}{base.get('total_bullet_hits', 0):>14}"
          f"{skill.get('total_bullet_hits', 0):>14}"
          f"{fmt_delta(base.get('total_bullet_hits', 0), skill.get('total_bullet_hits', 0)):>26}")
    print(f"  {'mean duration (s)':<22}{base.get('mean_duration_s', 0):>14.1f}"
          f"{skill.get('mean_duration_s', 0):>14.1f}"
          f"{fmt_delta(base.get('mean_duration_s', 0), skill.get('mean_duration_s', 0)):>26}")
    print()
    print("=" * 76)
    print(" PER-TASK BREAKDOWN")
    print("=" * 76)
    by_id_base = {r.task_id: r for r in baseline}
    by_id_skill = {r.task_id: r for r in with_skill}
    print()
    print(f"  {'task':<22}{'baseline':>22}{'with-skill':>22}")
    print(f"  {'-' * 22}{'-' * 22}{'-' * 22}")
    for tid in sorted(by_id_base.keys()):
        b = by_id_base[tid]
        s = by_id_skill.get(tid)
        if s is None:
            continue
        b_str = f"err={b.odoo_errors} hits={b.bullet_hits} tools={b.tool_calls}"
        s_str = f"err={s.odoo_errors} hits={s.bullet_hits} tools={s.tool_calls}"
        print(f"  {tid:<22}{b_str:>22}{s_str:>22}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tier", choices=["read", "write", "both"], default="both")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--jsonl", help="Write per-task results to this JSONL file")
    parser.add_argument("--odoo-url", default=os.environ.get("ODOO_URL", "http://13.210.13.41:8069"))
    parser.add_argument("--odoo-db", default=os.environ.get("ODOO_DB", "odoo-clawd-19"))
    parser.add_argument("--limit", type=int, help="Run only the first N hold-out tasks")
    args = parser.parse_args()

    # Filter tasks by tier
    if args.tier == "both":
        tasks = HOLDOUT_TASKS
    else:
        tasks = [t for t in HOLDOUT_TASKS if t.tier == args.tier]
    if args.limit:
        tasks = tasks[: args.limit]

    log.info("Cold-start validation: %d tasks (tier=%s)", len(tasks), args.tier)

    # Snapshot the SKILL.md content per tier so concurrent RL writes don't
    # perturb the comparison.
    skill_md_by_tier = {
        "read": _read_skill_md("read"),
        "write": _read_skill_md("write"),
    }
    for t, txt in skill_md_by_tier.items():
        log.info("  snapshot %s SKILL.md: %d chars", t, len(txt))

    # Connect to Odoo (separate client from any running RL loop)
    odoo = OdooClient(args.odoo_url, args.odoo_db, "admin", password="admin")
    odoo.authenticate()
    log.info("Connected to Odoo %s", odoo.version)

    # LLM client
    llm = create_llm_client()

    baseline_results: list[TaskResult] = []
    with_skill_results: list[TaskResult] = []

    # Open the JSONL incrementally so a mid-run death doesn't lose anything.
    jsonl_fh = open(args.jsonl, "w") if args.jsonl else None

    def _persist(r: TaskResult) -> None:
        if jsonl_fh:
            jsonl_fh.write(json.dumps(r.__dict__, default=str) + "\n")
            jsonl_fh.flush()

    try:
        for i, task in enumerate(tasks, 1):
            log.info("[%d/%d] %s — %s", i, len(tasks), task.id, task.task[:60])
            skill_text = skill_md_by_tier.get(task.tier, "")

            # Baseline first (no skill content)
            log.info("  baseline...")
            b = run_one(llm, odoo, args.model, task, skill_md_text=None, max_turns=args.max_turns)
            baseline_results.append(b)
            _persist(b)
            log.info("    err=%d hits=%d tools=%d %.1fs", b.odoo_errors, b.bullet_hits, b.tool_calls, b.duration_s)

            # Then with-skill
            log.info("  with-skill...")
            s = run_one(llm, odoo, args.model, task, skill_md_text=skill_text, max_turns=args.max_turns)
            with_skill_results.append(s)
            _persist(s)
            log.info("    err=%d hits=%d tools=%d %.1fs", s.odoo_errors, s.bullet_hits, s.tool_calls, s.duration_s)
    finally:
        if jsonl_fh:
            jsonl_fh.close()

    print_report(baseline_results, with_skill_results)
    if args.jsonl:
        log.info("Wrote %s", args.jsonl)

    odoo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
