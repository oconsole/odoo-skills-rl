#!/usr/bin/env bash
# SkillRL Training Script for OdooCLI
#
# Usage:
#   export MODEL_PATH=/path/to/your/sft/checkpoint
#   bash skill_rl/scripts/run_training.sh
#
# Prerequisites:
#   - pip install vllm==0.11.0 flash-attn==2.7.4.post1
#   - pip install verl (or clone SkillRL repo)
#   - SFT checkpoint trained on Odoo task data

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- Config ---
MODEL_PATH="${MODEL_PATH:?Set MODEL_PATH to your SFT checkpoint}"
SKILLS_JSON="${SKILLS_JSON:-$PROJECT_ROOT/skill_rl/skill_bank/odoo_skills.json}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_ROOT/skill_rl/runs/$(date +%Y%m%d_%H%M%S)}"
RETRIEVAL_MODE="${RETRIEVAL_MODE:-template}"  # "template" or "embedding"

echo "========================================="
echo "  SkillRL Training — OdooCLI Skills"
echo "========================================="
echo "Model:          $MODEL_PATH"
echo "Skills JSON:    $SKILLS_JSON"
echo "Retrieval mode: $RETRIEVAL_MODE"
echo "Output:         $OUTPUT_DIR"
echo ""

mkdir -p "$OUTPUT_DIR"

# --- Launch training ---
python3 -m verl.trainer.main_ppo \
    data.train_files="$PROJECT_ROOT/skill_rl/memory_data/trajectories.jsonl" \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
    critic.optim.lr=1e-6 \
    trainer.total_epochs=3 \
    trainer.save_freq=50 \
    trainer.project_name="odoocli-skillrl" \
    trainer.experiment_name="odoo-skills-$(date +%Y%m%d)" \
    trainer.logger="['console','wandb']" \
    trainer.default_local_dir="$OUTPUT_DIR" \
    +env.use_skills_only_memory=True \
    +env.skills_only_memory.skills_json_path="$SKILLS_JSON" \
    +env.skills_only_memory.retrieval_mode="$RETRIEVAL_MODE" \
    +env.skills_only_memory.top_k=6 \
    +env.skills_only_memory.task_specific_top_k=5 \
    +env.skills_only_memory.enable_dynamic_update=True \
    +env.skills_only_memory.update_threshold=0.4 \
    +env.skills_only_memory.max_new_skills=3 \
    "$@"

echo ""
echo "Training complete. Output saved to: $OUTPUT_DIR"
