#!/usr/bin/env python3
"""
Skill Retriever for OdooCLI SkillRL.

Retrieves relevant skills from the skill bank based on the current task.
Supports two modes:
  - template: keyword matching (fast, no extra model)
  - embedding: semantic similarity via embedding model (better quality)

Usage:
    from skill_retriever import SkillRetriever

    retriever = SkillRetriever("skill_rl/skill_bank/odoo_skills.json")
    skills = retriever.retrieve("Check for negative stock quantities", top_k=6)
"""

import json
import re
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class RetrievedSkills:
    """Container for retrieved skills to inject into agent prompt."""
    general: list[dict] = field(default_factory=list)
    task_specific: list[dict] = field(default_factory=list)
    mistakes: list[dict] = field(default_factory=list)

    def to_prompt_section(self) -> str:
        """Format retrieved skills as a prompt injection block."""
        parts = []

        if self.general:
            parts.append("## Relevant Skills\n")
            for s in self.general:
                parts.append(f"**{s['title']}**: {s.get('principle', s.get('heuristic', ''))}")
                parts.append(f"  Application: {s.get('application', '')}\n")

        if self.task_specific:
            parts.append("## Task-Specific Guidance\n")
            for s in self.task_specific:
                parts.append(f"**{s['title']}**: {s.get('heuristic', s.get('principle', ''))}")
                parts.append(f"  Application: {s.get('application', '')}\n")

        if self.mistakes:
            parts.append("## Common Mistakes to Avoid\n")
            for m in self.mistakes:
                parts.append(f"**{m['title']}**: {m['description']}")
                parts.append(f"  Avoidance: {m['avoidance']}\n")

        return "\n".join(parts)

    @property
    def total_count(self) -> int:
        return len(self.general) + len(self.task_specific) + len(self.mistakes)


class SkillRetriever:
    """Retrieve skills from the skill bank for prompt injection."""

    def __init__(
        self,
        skills_json_path: str,
        retrieval_mode: str = "template",
        embedding_model_path: str = "Qwen/Qwen3-Embedding-0.6B",
    ):
        self.skills_json_path = skills_json_path
        self.retrieval_mode = retrieval_mode
        self.embedding_model_path = embedding_model_path

        with open(skills_json_path) as f:
            self.skill_bank = json.load(f)

        self._embedder = None

    def retrieve(
        self,
        task_description: str,
        top_k: int = 6,
        task_specific_top_k: int | None = None,
    ) -> RetrievedSkills:
        """Retrieve relevant skills for a task description."""
        if self.retrieval_mode == "embedding":
            return self._retrieve_embedding(task_description, top_k, task_specific_top_k)
        return self._retrieve_template(task_description, top_k, task_specific_top_k)

    # --- Template mode (keyword matching) ---

    def _retrieve_template(
        self,
        task_description: str,
        top_k: int,
        task_specific_top_k: int | None,
    ) -> RetrievedSkills:
        """Keyword-based skill retrieval."""
        task_lower = task_description.lower()
        result = RetrievedSkills()

        # Score general skills by keyword overlap
        general_scored = []
        for skill in self.skill_bank.get("general_skills", []):
            score = self._keyword_score(task_lower, skill)
            general_scored.append((score, skill))
        general_scored.sort(key=lambda x: x[0], reverse=True)
        result.general = [s for _, s in general_scored[:top_k]]

        # Detect task category from keywords
        category = self._detect_category(task_lower)
        ts_limit = task_specific_top_k or top_k

        if category:
            ts_skills = self.skill_bank.get("task_specific_skills", {}).get(category, [])
            ts_scored = [(self._keyword_score(task_lower, s), s) for s in ts_skills]
            ts_scored.sort(key=lambda x: x[0], reverse=True)
            result.task_specific = [s for _, s in ts_scored[:ts_limit]]

        # Score common mistakes
        cm_scored = []
        for mistake in self.skill_bank.get("common_mistakes", []):
            score = self._keyword_score(task_lower, mistake)
            cm_scored.append((score, mistake))
        cm_scored.sort(key=lambda x: x[0], reverse=True)
        result.mistakes = [m for _, m in cm_scored[:3]]

        return result

    def _keyword_score(self, query: str, skill: dict) -> float:
        """Score a skill by keyword overlap with the query."""
        skill_text = " ".join(str(v) for v in skill.values()).lower()
        query_words = set(re.findall(r"\w+", query))
        skill_words = set(re.findall(r"\w+", skill_text))
        if not query_words:
            return 0.0
        overlap = query_words & skill_words
        return len(overlap) / len(query_words)

    def _detect_category(self, task_lower: str) -> str | None:
        """Detect Odoo task category from keywords."""
        category_keywords = {
            "health-check": ["health", "diagnos", "doctor", "status", "check", "cron", "error", "log"],
            "deploy-module": ["deploy", "install", "upgrade", "module", "depend"],
            "inventory-audit": ["inventory", "stock", "quant", "warehouse", "transfer", "picking", "negative"],
            "invoice-posting": ["invoice", "post", "draft", "bill", "payment", "account.move", "overdue"],
            "backup-restore": ["backup", "restore", "database", "dump", "recovery", "filestore"],
        }
        best_category = None
        best_score = 0
        for category, keywords in category_keywords.items():
            score = sum(1 for kw in keywords if kw in task_lower)
            if score > best_score:
                best_score = score
                best_category = category
        return best_category if best_score > 0 else None

    # --- Embedding mode (semantic similarity) ---

    def _retrieve_embedding(
        self,
        task_description: str,
        top_k: int,
        task_specific_top_k: int | None,
    ) -> RetrievedSkills:
        """Semantic similarity skill retrieval using embedding model."""
        if self._embedder is None:
            self._init_embedder()

        result = RetrievedSkills()

        # Embed query
        query_emb = self._embed([task_description])[0]

        # Score general skills
        general_skills = self.skill_bank.get("general_skills", [])
        if general_skills:
            texts = [f"{s['title']} {s.get('principle', '')}" for s in general_skills]
            embeddings = self._embed(texts)
            scores = [self._cosine_sim(query_emb, emb) for emb in embeddings]
            ranked = sorted(zip(scores, general_skills), key=lambda x: x[0], reverse=True)
            result.general = [s for _, s in ranked[:top_k]]

        # Score task-specific skills across all categories
        ts_limit = task_specific_top_k or top_k
        all_ts = []
        for _cat, skills in self.skill_bank.get("task_specific_skills", {}).items():
            all_ts.extend(skills)
        if all_ts:
            texts = [f"{s['title']} {s.get('heuristic', '')}" for s in all_ts]
            embeddings = self._embed(texts)
            scores = [self._cosine_sim(query_emb, emb) for emb in embeddings]
            ranked = sorted(zip(scores, all_ts), key=lambda x: x[0], reverse=True)
            result.task_specific = [s for _, s in ranked[:ts_limit]]

        # Score common mistakes
        mistakes = self.skill_bank.get("common_mistakes", [])
        if mistakes:
            texts = [f"{m['title']} {m['description']}" for m in mistakes]
            embeddings = self._embed(texts)
            scores = [self._cosine_sim(query_emb, emb) for emb in embeddings]
            ranked = sorted(zip(scores, mistakes), key=lambda x: x[0], reverse=True)
            result.mistakes = [m for _, m in ranked[:3]]

        return result

    def _init_embedder(self):
        """Initialize the embedding model."""
        try:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(self.embedding_model_path)
        except ImportError:
            print("ERROR: sentence-transformers required for embedding mode.")
            print("Install with: pip install sentence-transformers")
            raise

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts."""
        return self._embedder.encode(texts, normalize_embeddings=True).tolist()

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python skill_retriever.py 'task description'")
        print("       python skill_retriever.py 'Check for negative stock' --mode=embedding")
        sys.exit(1)

    task = sys.argv[1]
    mode = "template"
    for arg in sys.argv[2:]:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]

    skills_path = str(Path(__file__).parent / "skill_bank" / "odoo_skills.json")
    retriever = SkillRetriever(skills_path, retrieval_mode=mode)
    result = retriever.retrieve(task)

    print(f"Retrieved {result.total_count} skills for: {task!r}\n")
    print(result.to_prompt_section())
