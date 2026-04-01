# SkillRL for OdooCLI

Reinforcement learning framework for evolving OdooCLI agent skills. Inspired by [SkillRL](https://github.com/aiming-lab/SkillRL), this module distills agent trajectories into a hierarchical skill library and co-evolves it during RL training.

## Overview

The pipeline:

1. **Collect trajectories** — run the OdooCLI agent on Odoo tasks and log conversations
2. **Distill skill bank** — compress trajectories into general skills, task-specific skills, and common mistakes
3. **RL training** — inject retrieved skills into the agent prompt during GRPO/PPO training
4. **Skill evolution** — update the skill bank based on validation performance

## Directory Structure

```
skill_rl/
├── README.md
├── config/
│   └── default.yaml          # Hydra-style training config
├── skill_bank/
│   └── odoo_skills.json      # Distilled skill library (SkillRL format)
├── memory_data/
│   └── prompt.txt            # Prompt template for trajectory generation
├── scripts/
│   ├── generate_trajectories.py   # Step 1: collect agent trajectories
│   ├── distill_skills.py          # Step 2: trajectory → skill bank
│   └── run_training.sh            # Step 3: launch RL training
└── skill_retriever.py             # Skill retrieval (template + embedding modes)
```

## Quick Start

### 1. Generate trajectory data

```bash
python skill_rl/scripts/generate_trajectories.py \
    --config skill_rl/config/default.yaml \
    --output skill_rl/memory_data/trajectories.jsonl
```

### 2. Distill into skill bank

```bash
python skill_rl/scripts/distill_skills.py \
    --memory_path skill_rl/memory_data/trajectories.jsonl \
    --output_path skill_rl/skill_bank/odoo_skills.json
```

### 3. Run RL training with skill injection

```bash
export MODEL_PATH=YOUR_SFT_CHECKPOINT
bash skill_rl/scripts/run_training.sh
```

## Skill Bank Format

The skill bank (`skill_bank/odoo_skills.json`) follows the SkillRL hierarchical format:

- **general_skills** — universal Odoo agent strategies (e.g., always verify before mutating)
- **task_specific_skills** — category-level heuristics per Odoo domain (inventory, accounting, etc.)
- **common_mistakes** — failure patterns observed in trajectories

## Skill Retrieval Modes

| Mode | How it works | When to use |
|------|-------------|-------------|
| `template` | Keyword matching on task description | Fast, no extra model needed |
| `embedding` | Semantic similarity via embedding model | Better retrieval quality |

Configure via `config/default.yaml`:
```yaml
skills:
  retrieval_mode: template  # or "embedding"
  top_k: 6
  enable_dynamic_update: true
```
