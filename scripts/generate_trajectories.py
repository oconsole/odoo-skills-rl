#!/usr/bin/env python3
"""
Generate training trajectories by running the OdooCLI agent on Odoo tasks.

Collects agent conversations as JSONL trajectories that can be distilled
into the skill bank or used for SFT/RL training.

Usage:
    python skill_rl/scripts/generate_trajectories.py \
        --config skill_rl/config/default.yaml \
        --output skill_rl/memory_data/trajectories.jsonl
"""

import json
import sys
import yaml
import argparse
from pathlib import Path
from datetime import datetime


# --- Task definitions for each Odoo skill category ---

ODOO_TASKS = {
    "health-check": [
        "Run a full health check on the Odoo instance and report any issues.",
        "Check if there are any stuck scheduled actions (ir.cron) and report their status.",
        "List all installed modules and check if any are in 'to upgrade' state.",
        "Check the Odoo server version and count active users.",
        "Look for recent error logs in ir.logging from the last 24 hours.",
    ],
    "deploy-module": [
        "Check if the 'sale' module is installed. If not, show me its dependencies.",
        "Verify the state of the 'stock' module and list its dependent modules.",
        "Check if there are any modules in 'to install' or 'to upgrade' state.",
        "Show me the dependency tree for the 'account' module.",
        "List all uninstalled modules that are available on this instance.",
    ],
    "inventory-audit": [
        "Check for any products with negative stock quantities.",
        "Find all stock transfers that are overdue (past scheduled date).",
        "Audit the current inventory levels for the top 20 products by value.",
        "Check for stock quants with zero quantity but non-zero reserved quantity.",
        "List all warehouse locations and their current stock levels.",
    ],
    "invoice-posting": [
        "Find all draft customer invoices and show me a summary by customer.",
        "Check for overdue unpaid invoices and list them by age.",
        "Show me the total amount of draft invoices waiting to be posted.",
        "Find invoices that were posted in the last 7 days and their payment status.",
        "List all vendor bills in draft state with their due dates.",
    ],
    "backup-restore": [
        "What is the current database name and Odoo version? I need this for backup documentation.",
        "Check the database for key record counts (partners, invoices, orders) to establish a baseline.",
        "List all ir.attachment records to estimate filestore size.",
        "Verify data integrity by checking for orphaned records in common models.",
        "What scheduled actions exist that might affect a restore procedure?",
    ],
}


def load_config(config_path: str) -> dict:
    """Load YAML configuration."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def generate_task_list(config: dict) -> list[dict]:
    """Build the full task list from config categories."""
    categories = config.get("environment", {}).get("task_categories", list(ODOO_TASKS.keys()))
    tasks = []
    for category in categories:
        if category in ODOO_TASKS:
            for task_desc in ODOO_TASKS[category]:
                tasks.append({
                    "category": category,
                    "task": task_desc,
                })
    return tasks


def main():
    parser = argparse.ArgumentParser(description="Generate OdooCLI training trajectories")
    parser.add_argument("--config", default="skill_rl/config/default.yaml", help="Config YAML path")
    parser.add_argument("--output", default="skill_rl/memory_data/trajectories.jsonl", help="Output JSONL path")
    parser.add_argument("--dry-run", action="store_true", help="Print tasks without running agent")
    args = parser.parse_args()

    config = load_config(args.config)
    tasks = generate_task_list(config)

    if args.dry_run:
        print(f"Would generate {len(tasks)} trajectories:")
        for i, t in enumerate(tasks, 1):
            print(f"  {i}. [{t['category']}] {t['task']}")
        return

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Generating trajectories for {len(tasks)} tasks...")
    print(f"Output: {output_path}")
    print()
    print("NOTE: This script generates the task list. To collect actual trajectories,")
    print("integrate with the OdooCLI agent loop (run_agent.py) or use the trajectory")
    print("logging in agent/trajectory.py.")
    print()

    # Write task manifest for downstream consumption
    manifest_path = output_path.with_suffix(".tasks.jsonl")
    with open(manifest_path, "w") as f:
        for task in tasks:
            task["generated_at"] = datetime.utcnow().isoformat()
            f.write(json.dumps(task) + "\n")

    print(f"Task manifest written to {manifest_path} ({len(tasks)} tasks)")
    print()
    print("Next steps:")
    print("  1. Run each task through the OdooCLI agent to collect trajectories")
    print("  2. Distill trajectories into the skill bank:")
    print(f"     python skill_rl/scripts/distill_skills.py \\")
    print(f"         --memory_path {args.output} \\")
    print(f"         --output_path skill_rl/skill_bank/odoo_skills.json")


if __name__ == "__main__":
    main()
