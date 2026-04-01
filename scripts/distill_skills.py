#!/usr/bin/env python3
"""
Distill agent trajectories into the SkillRL skill bank format.

Reads raw trajectory JSONL files and uses an LLM to extract:
- General skills (universal Odoo agent patterns)
- Task-specific skills (per-category heuristics)
- Common mistakes (failure patterns to avoid)

Usage:
    python skill_rl/scripts/distill_skills.py \
        --memory_path skill_rl/memory_data/trajectories.jsonl \
        --output_path skill_rl/skill_bank/odoo_skills.json

    # Merge with existing skill bank (additive)
    python skill_rl/scripts/distill_skills.py \
        --memory_path skill_rl/memory_data/trajectories.jsonl \
        --output_path skill_rl/skill_bank/odoo_skills.json \
        --merge
"""

import json
import os
import re
import sys
import argparse
from pathlib import Path
from typing import Any


DISTILLATION_PROMPT = """\
You are analyzing OdooCLI agent trajectories to extract reusable skills.

Given the following trajectory data from an Odoo ERP agent, extract:

1. **General Skills**: Universal patterns that apply across all Odoo tasks.
   Format: {{"id": "GS-XXX", "title": "...", "principle": "...", "application": "..."}}

2. **Task-Specific Skills**: Heuristics specific to the task category ({category}).
   Format: {{"id": "TS-XX-XXX", "title": "...", "heuristic": "...", "application": "..."}}

3. **Common Mistakes**: Failure patterns observed in the trajectories.
   Format: {{"id": "CM-XXX", "title": "...", "description": "...", "avoidance": "..."}}

Focus on:
- Patterns that led to successful task completion
- Strategies that avoided errors or recovered from them
- Odoo-specific knowledge (model relationships, workflow states, API patterns)
- Safety practices (confirmation before writes, backup reminders)

Trajectory data:
{trajectory_data}

Return valid JSON with keys: general_skills, task_specific_skills, common_mistakes
"""


def load_trajectories(path: str) -> list[dict]:
    """Load trajectories from JSONL file."""
    trajectories = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                trajectories.append(json.loads(line))
    return trajectories


def group_by_category(trajectories: list[dict]) -> dict[str, list[dict]]:
    """Group trajectories by task category."""
    groups: dict[str, list[dict]] = {}
    for traj in trajectories:
        cat = traj.get("category", "unknown")
        groups.setdefault(cat, []).append(traj)
    return groups


def distill_with_llm(trajectories: list[dict], category: str) -> dict[str, Any]:
    """Call LLM to distill trajectories into skills.

    Requires OPENROUTER_API_KEY or ANTHROPIC_API_KEY in environment.
    """
    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: openai package required. Install with: pip install openai")
        sys.exit(1)

    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY")
        sys.exit(1)

    base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.environ.get("DISTILL_MODEL", "anthropic/claude-sonnet-4")

    client = OpenAI(api_key=api_key, base_url=base_url)

    # Truncate trajectory data to fit context
    traj_text = json.dumps(trajectories[:20], indent=2)[:50000]

    prompt = DISTILLATION_PROMPT.format(
        category=category,
        trajectory_data=traj_text,
    )

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=4096,
    )

    content = response.choices[0].message.content

    # Extract JSON from response
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        print(f"WARNING: Could not parse LLM response for category '{category}'")
        return {"general_skills": [], "task_specific_skills": [], "common_mistakes": []}


def merge_skill_banks(existing: dict, new: dict) -> dict:
    """Merge new skills into existing bank, deduplicating by ID."""
    merged = {
        "general_skills": list(existing.get("general_skills", [])),
        "task_specific_skills": dict(existing.get("task_specific_skills", {})),
        "common_mistakes": list(existing.get("common_mistakes", [])),
    }

    existing_gs_ids = {s["id"] for s in merged["general_skills"]}
    for skill in new.get("general_skills", []):
        if skill.get("id") not in existing_gs_ids:
            merged["general_skills"].append(skill)

    for category, skills in new.get("task_specific_skills", {}).items():
        if category not in merged["task_specific_skills"]:
            merged["task_specific_skills"][category] = []
        existing_ts_ids = {s["id"] for s in merged["task_specific_skills"][category]}
        for skill in skills:
            if skill.get("id") not in existing_ts_ids:
                merged["task_specific_skills"][category].append(skill)

    existing_cm_ids = {s["id"] for s in merged["common_mistakes"]}
    for mistake in new.get("common_mistakes", []):
        if mistake.get("id") not in existing_cm_ids:
            merged["common_mistakes"].append(mistake)

    return merged


def main():
    parser = argparse.ArgumentParser(description="Distill trajectories into SkillRL skill bank")
    parser.add_argument("--memory_path", required=True, help="Path to trajectory JSONL")
    parser.add_argument("--output_path", required=True, help="Output skill bank JSON path")
    parser.add_argument("--merge", action="store_true", help="Merge with existing skill bank")
    parser.add_argument("--dry-run", action="store_true", help="Show stats without calling LLM")
    args = parser.parse_args()

    trajectories = load_trajectories(args.memory_path)
    grouped = group_by_category(trajectories)

    print(f"Loaded {len(trajectories)} trajectories across {len(grouped)} categories:")
    for cat, trajs in grouped.items():
        print(f"  {cat}: {len(trajs)} trajectories")

    if args.dry_run:
        return

    all_new_skills = {
        "general_skills": [],
        "task_specific_skills": {},
        "common_mistakes": [],
    }

    for category, trajs in grouped.items():
        print(f"\nDistilling {category} ({len(trajs)} trajectories)...")
        result = distill_with_llm(trajs, category)
        all_new_skills["general_skills"].extend(result.get("general_skills", []))
        if result.get("task_specific_skills"):
            ts = result["task_specific_skills"]
            if isinstance(ts, list):
                all_new_skills["task_specific_skills"][category] = ts
            elif isinstance(ts, dict):
                all_new_skills["task_specific_skills"].update(ts)
        all_new_skills["common_mistakes"].extend(result.get("common_mistakes", []))

    output_path = Path(args.output_path)
    if args.merge and output_path.exists():
        with open(output_path) as f:
            existing = json.load(f)
        final = merge_skill_banks(existing, all_new_skills)
        print(f"\nMerged with existing skill bank at {output_path}")
    else:
        final = all_new_skills

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(final, f, indent=2)

    gs = len(final["general_skills"])
    ts = sum(len(v) for v in final["task_specific_skills"].values())
    cm = len(final["common_mistakes"])
    print(f"\nSkill bank written to {output_path}")
    print(f"  General skills: {gs}")
    print(f"  Task-specific skills: {ts}")
    print(f"  Common mistakes: {cm}")


if __name__ == "__main__":
    main()
