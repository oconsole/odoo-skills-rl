#!/usr/bin/env python3
"""
SkillRL for OdooCLI — long-running skill evolution loop.

Runs the agent on Odoo tasks, scores outcomes, and evolves the skill bank.
No GPUs, no fine-tuning. The skill bank IS the policy being optimized.

Usage:
    # Basic run (uses OPENROUTER_API_KEY from env or .env)
    python skill_rl/run.py

    # Custom settings
    python skill_rl/run.py --episodes 500 --evolve-every 10 --model claude-sonnet-4-20250514

    # Resume from existing skill bank
    python skill_rl/run.py --skill-bank skill_rl/skill_bank/odoo_skills.json

    # Run in background
    nohup python skill_rl/run.py > skill_rl/runs/rl.log 2>&1 &
"""

import json
import os
import re
import sys
import time
import copy
import random
import logging
import argparse
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
# Load .env from multiple locations
load_dotenv(Path.home() / ".hermes" / ".env")
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("skill_rl")

# ---------------------------------------------------------------------------
# Odoo Client (self-contained, copied from odoo_mcp_server.py)
# ---------------------------------------------------------------------------

import httpx


class OdooClient:
    """Lightweight Odoo JSON-RPC client."""

    def __init__(self, url: str, database: str, username: str,
                 password: str = None, api_key: str = None):
        self.url = url.rstrip("/")
        self.database = database
        self.username = username
        self.password = password
        self.api_key = api_key
        self.uid: Optional[int] = None
        self.version: Optional[str] = None
        self._http = httpx.Client(timeout=30.0)

    def authenticate(self) -> int:
        self.version = self._detect_version()
        if self.api_key and self._is_v19_plus():
            self.uid = self._auth_json2()
        else:
            self.uid = self._auth_jsonrpc()
        if not self.uid:
            raise ConnectionError(f"Auth failed: {self.username}@{self.url}/{self.database}")
        return self.uid

    def _detect_version(self) -> str:
        try:
            resp = self._jsonrpc(f"{self.url}/jsonrpc", "call",
                                 service="common", method="version", args=[])
            return resp.get("server_version", "unknown")
        except Exception:
            return "unknown"

    def _is_v19_plus(self) -> bool:
        try:
            return int((self.version or "0").split(".")[0]) >= 19
        except (ValueError, IndexError):
            return False

    def _auth_jsonrpc(self) -> Optional[int]:
        try:
            result = self._jsonrpc(f"{self.url}/jsonrpc", "call",
                                   service="common", method="authenticate",
                                   args=[self.database, self.username, self.password or "", {}])
            return result if isinstance(result, int) else None
        except Exception:
            return None

    def _auth_json2(self) -> Optional[int]:
        try:
            resp = self._http.get(f"{self.url}/api/res.users/whoami",
                                  headers={"Authorization": f"Bearer {self.api_key}"})
            resp.raise_for_status()
            data = resp.json()
            return data.get("uid") or data.get("id")
        except Exception:
            return None

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.uid is None:
            raise ConnectionError("Not authenticated.")
        if self._is_v19_plus() and self.api_key:
            return self._exec_json2(model, method, *args, **kwargs)
        return self._exec_jsonrpc(model, method, *args, **kwargs)

    def _exec_jsonrpc(self, model: str, method: str, *args, **kwargs) -> Any:
        return self._jsonrpc(f"{self.url}/jsonrpc", "call",
                             service="object", method="execute_kw",
                             args=[self.database, self.uid, self.password or "",
                                   model, method, list(args), kwargs])

    def _exec_json2(self, model: str, method: str, *args, **kwargs) -> Any:
        payload: dict[str, Any] = {}
        if args:
            payload["args"] = list(args)
        if kwargs:
            payload.update(kwargs)
        resp = self._http.post(f"{self.url}/api/{model}/{method}",
                               json=payload,
                               headers={"Authorization": f"Bearer {self.api_key}"})
        resp.raise_for_status()
        return resp.json()

    def search_read(self, model, domain=None, fields=None, limit=100, offset=0, order=None):
        kw = {"limit": limit, "offset": offset}
        if fields:
            kw["fields"] = fields
        if order:
            kw["order"] = order
        return self.execute(model, "search_read", domain or [], **kw)

    def search_count(self, model, domain=None):
        return self.execute(model, "search_count", domain or [])

    def _jsonrpc(self, url, rpc_method, **params):
        payload = {"jsonrpc": "2.0", "method": rpc_method, "params": params, "id": 1}
        resp = self._http.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            err = body["error"]
            msg = err.get("data", {}).get("message", err.get("message", str(err)))
            raise ConnectionError(f"Odoo error: {msg}")
        return body.get("result")

    def close(self):
        self._http.close()


# ---------------------------------------------------------------------------
# Tool definitions (what the LLM can call)
# ---------------------------------------------------------------------------

ODOO_TOOLS = [
    {
        "name": "odoo_search_read",
        "description": "Search and read records from any Odoo model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Odoo model name (e.g. 'res.partner')"},
                "domain": {"type": "array", "description": "Search domain filter, e.g. [[\"state\",\"=\",\"sale\"]]"},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Fields to return"},
                "limit": {"type": "integer", "description": "Max records (default 20)"},
                "order": {"type": "string", "description": "Sort order, e.g. 'create_date desc'"},
            },
            "required": ["model"],
        },
    },
    {
        "name": "odoo_search_count",
        "description": "Count records matching a domain without fetching data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "domain": {"type": "array"},
            },
            "required": ["model"],
        },
    },
    {
        "name": "odoo_get_fields",
        "description": "Get field definitions for an Odoo model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
            },
            "required": ["model"],
        },
    },
    {
        "name": "odoo_list_models",
        "description": "List available Odoo models, optionally filtered by keyword.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Filter models by keyword"},
            },
        },
    },
    {
        "name": "odoo_doctor",
        "description": "Run health diagnostics on the Odoo instance.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "odoo_execute",
        "description": "Execute any method on an Odoo model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "method": {"type": "string"},
                "args": {"type": "array", "description": "Positional arguments"},
                "kwargs": {"type": "object", "description": "Keyword arguments"},
            },
            "required": ["model", "method"],
        },
    },
    {
        "name": "odoo_model_info",
        "description": "Get comprehensive model metadata in one call: fields (grouped by type), views, window actions, default values, sort order, custom fields, relational fields, and required fields. Use this instead of multiple exploratory queries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Odoo model name (e.g. 'res.partner', 'sale.order')"},
            },
            "required": ["model"],
        },
    },
    {
        "name": "odoo_set_default",
        "description": "Set, update, or clear a field's default value via ir.default. Handles JSON encoding automatically. Pass value=null to remove a default.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Odoo model name (e.g. 'product.template')"},
                "field_name": {"type": "string", "description": "Field name (e.g. 'invoice_policy')"},
                "value": {"description": "Default value to set. Raw value — JSON-encoded automatically. null removes the default."},
                "user_id": {"type": "integer", "description": "User ID for user-specific default. Omit for global."},
                "company_id": {"type": "integer", "description": "Company ID for company-specific default. Omit for all."},
            },
            "required": ["model", "field_name"],
        },
    },
    {
        "name": "odoo_get_view",
        "description": "Get the fully rendered (merged) view XML for a model after all inheritance is applied. Returns the actual view the user sees, not raw ir.ui.view fragments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Odoo model name (e.g. 'sale.order')"},
                "view_type": {"type": "string", "description": "View type: form, tree, search, kanban, pivot, graph, calendar. Default: form."},
            },
            "required": ["model"],
        },
    },
    {
        "name": "odoo_modify_action",
        "description": "Read or modify a window action (ir.actions.act_window) that controls how a model appears in the UI. Change default domain filters, sort order, grouping, view modes, or page limit. Provide model to list actions, or action_id + changes to modify.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "integer", "description": "ID of action to modify. Omit to list actions for the model."},
                "model": {"type": "string", "description": "Model name to find actions for (when action_id is omitted)."},
                "domain": {"type": "string", "description": "New domain filter, e.g. \"[['state','=','sale']]\""},
                "context": {"type": "string", "description": "New context, e.g. \"{'group_by': 'partner_id', 'search_default_posted': 1}\""},
                "order": {"type": "string", "description": "Default sort order, e.g. 'date_order desc'"},
                "limit": {"type": "integer", "description": "Records per page (e.g. 40, 80, 200)"},
                "view_mode": {"type": "string", "description": "View modes, e.g. 'tree,form,kanban'"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool executor — runs tool calls against live Odoo
# ---------------------------------------------------------------------------

def execute_tool(odoo: OdooClient, name: str, args: dict) -> str:
    """Execute a tool call and return the JSON result string."""
    try:
        if name == "odoo_search_read":
            records = odoo.search_read(
                args["model"],
                domain=args.get("domain"),
                fields=args.get("fields"),
                limit=args.get("limit", 20),
                order=args.get("order"),
            )
            return json.dumps({"model": args["model"], "count": len(records), "records": records}, default=str)

        elif name == "odoo_search_count":
            count = odoo.search_count(args["model"], domain=args.get("domain"))
            return json.dumps({"model": args["model"], "count": count})

        elif name == "odoo_get_fields":
            fields = odoo.execute(args["model"], "fields_get", [], {"attributes": ["string", "type", "required", "readonly"]})
            return json.dumps({"model": args["model"], "fields": fields}, default=str)

        elif name == "odoo_list_models":
            keyword = args.get("keyword", "")
            domain = [["model", "ilike", keyword]] if keyword else []
            models = odoo.search_read("ir.model", domain=domain, fields=["model", "name"], limit=200)
            return json.dumps({"count": len(models), "models": models}, default=str)

        elif name == "odoo_doctor":
            checks = []
            # Version
            checks.append({"check": "server_version", "status": "ok", "value": odoo.version})
            # Modules
            mods = odoo.search_count("ir.module.module", [["state", "=", "installed"]])
            checks.append({"check": "installed_modules", "status": "ok", "value": mods})
            # Users
            users = odoo.search_count("res.users", [["active", "=", True]])
            checks.append({"check": "active_users", "status": "ok", "value": users})
            # Cron
            crons = odoo.search_count("ir.cron", [["active", "=", True]])
            checks.append({"check": "active_cron_jobs", "status": "ok", "value": crons})
            return json.dumps({"summary": f"{len(checks)}/4 checks passed", "checks": checks}, default=str)

        elif name == "odoo_execute":
            result = odoo.execute(args["model"], args["method"],
                                  *(args.get("args") or []), **(args.get("kwargs") or {}))
            return json.dumps({"result": result}, default=str)

        elif name == "odoo_model_info":
            model = args["model"]
            info: dict[str, Any] = {"model": model}

            # Model metadata. ir.model exposes 'order' as a stored Char in
            # both Odoo 18 and 19 (it mirrors _order). However 'rec_name' is
            # NOT a stored field — _rec_name is a class attr. Infer it from
            # fields_get below.
            ir_models = odoo.search_read("ir.model", [["model", "=", model]],
                                         ["name", "model", "order", "state", "transient"], 1)
            if not ir_models:
                return json.dumps({"error": f"Model '{model}' not found."})
            ir_m = ir_models[0]
            info["name"] = ir_m.get("name", "")
            info["default_order"] = ir_m.get("order", "id")
            info["state"] = ir_m.get("state", "")
            info["transient"] = ir_m.get("transient", False)
            model_id = ir_m["id"]

            # Fields
            fields = odoo.search_read("ir.model.fields", [["model_id", "=", model_id]],
                                       ["name", "field_description", "ttype", "required",
                                        "readonly", "store", "state", "relation"], 500)
            field_names = {f["name"] for f in fields}
            # _rec_name is not queryable via RPC. Default to "name" if it exists,
            # else "x_name", else "id". Matches Odoo's own fallback logic.
            info["rec_name"] = "name" if "name" in field_names else ("x_name" if "x_name" in field_names else "id")
            info["field_count"] = len(fields)
            by_type: dict[str, int] = {}
            for f in fields:
                by_type[f.get("ttype", "?")] = by_type.get(f.get("ttype", "?"), 0) + 1
            info["fields_by_type"] = by_type
            info["custom_fields"] = [{"name": f["name"], "type": f["ttype"], "label": f.get("field_description", "")}
                                      for f in fields if f.get("state") == "manual" or f["name"].startswith("x_")]
            info["relational_fields"] = [{"name": f["name"], "type": f["ttype"], "target": f.get("relation", ""),
                                           "label": f.get("field_description", "")}
                                          for f in fields if f.get("ttype") in ("many2one", "one2many", "many2many")]
            info["required_fields"] = [{"name": f["name"], "type": f["ttype"], "label": f.get("field_description", "")}
                                        for f in fields if f.get("required")]

            # Views
            views = odoo.search_read("ir.ui.view", [["model", "=", model], ["inherit_id", "=", False]],
                                      ["name", "type", "priority"], 20, order="type, priority")
            info["views"] = [{"id": v["id"], "name": v.get("name", ""), "type": v.get("type", ""),
                              "priority": v.get("priority", 16)} for v in views]

            # Actions
            actions = odoo.search_read("ir.actions.act_window", [["res_model", "=", model]],
                                        ["name", "domain", "context", "view_mode", "limit"], 20)
            info["actions"] = [{"id": a["id"], "name": a.get("name", ""), "domain": a.get("domain", ""),
                                "context": a.get("context", ""), "view_mode": a.get("view_mode", ""),
                                "limit": a.get("limit", 80)} for a in actions]

            # Defaults
            field_ids = [f["id"] for f in fields]
            if field_ids:
                defaults = odoo.search_read("ir.default", [["field_id", "in", field_ids]],
                                             ["field_id", "json_value", "user_id", "company_id"], 50)
                info["defaults"] = [{"field": d.get("field_id", [None, ""])[1] if isinstance(d.get("field_id"), list) else str(d.get("field_id", "")),
                                     "value": d.get("json_value", "")} for d in defaults]
            else:
                info["defaults"] = []

            return json.dumps(info, default=str)

        elif name == "odoo_set_default":
            model = args["model"]
            field_name = args["field_name"]
            value = args.get("value")
            user_id = args.get("user_id")
            company_id = args.get("company_id")

            # Find field
            fr = odoo.search_read("ir.model.fields", [["model", "=", model], ["name", "=", field_name]],
                                   ["id", "name", "ttype", "field_description"], 1)
            if not fr:
                return json.dumps({"error": f"Field '{field_name}' not found on '{model}'."})
            field_id = fr[0]["id"]

            # Find existing default
            sd: list = [["field_id", "=", field_id],
                        ["user_id", "=", user_id or False],
                        ["company_id", "=", company_id or False]]
            existing = odoo.search_read("ir.default", sd, ["id", "json_value"], 1)

            if value is None:
                if existing:
                    odoo.execute("ir.default", "unlink", [existing[0]["id"]])
                    return json.dumps({"operation": "removed", "field": field_name, "model": model})
                return json.dumps({"operation": "no_default_found", "field": field_name, "model": model})

            json_value = json.dumps(value)
            if existing:
                old = existing[0].get("json_value")
                odoo.execute("ir.default", "write", [existing[0]["id"]], {"json_value": json_value})
                return json.dumps({"operation": "updated", "field": field_name, "model": model,
                                   "previous": old, "new_value": json_value})
            else:
                vals: dict[str, Any] = {"field_id": field_id, "json_value": json_value}
                if user_id:
                    vals["user_id"] = user_id
                if company_id:
                    vals["company_id"] = company_id
                new_id = odoo.execute("ir.default", "create", vals)
                return json.dumps({"operation": "created", "field": field_name, "model": model,
                                   "value": json_value, "id": new_id})

        elif name == "odoo_get_view":
            model = args["model"]
            view_type = args.get("view_type", "form")
            try:
                vdata = odoo.execute(model, "get_views", [[False, view_type]])
            except Exception:
                try:
                    vdata = odoo.execute(model, "fields_view_get", view_type=view_type)
                except Exception as exc:
                    return json.dumps({"error": f"Failed to get {view_type} view: {exc}"})

            result: dict[str, Any] = {"model": model, "view_type": view_type}
            if isinstance(vdata, dict) and "views" in vdata:
                vd = vdata["views"].get(view_type, {})
                result["view_id"] = vd.get("id")
                arch = vd.get("arch", "")
                result["fields_in_view"] = list(vd.get("fields", {}).keys())
            elif isinstance(vdata, dict):
                result["view_id"] = vdata.get("view_id")
                arch = vdata.get("arch", "")
                result["fields_in_view"] = list(vdata.get("fields", {}).keys())
            else:
                arch = str(vdata)

            if len(arch) > 15000:
                arch = arch[:15000] + f"\n<!-- truncated ({len(arch)} chars) -->"
            result["arch"] = arch
            return json.dumps(result, default=str)

        elif name == "odoo_modify_action":
            action_id = args.get("action_id")
            model = args.get("model")
            if not action_id and not model:
                return json.dumps({"error": "Provide action_id or model."})

            if action_id:
                actions = odoo.search_read("ir.actions.act_window", [["id", "=", action_id]],
                                            ["name", "res_model", "domain", "context", "view_mode", "limit"], 1)
            else:
                actions = odoo.search_read("ir.actions.act_window", [["res_model", "=", model]],
                                            ["name", "res_model", "domain", "context", "view_mode", "limit"], 10)

            if not actions:
                return json.dumps({"error": "No window actions found."})

            no_changes = all(args.get(k) is None for k in ["domain", "context", "order", "limit", "view_mode"])
            if no_changes:
                return json.dumps({"actions": [{"id": a["id"], "name": a.get("name", ""),
                                                "domain": a.get("domain", ""), "context": a.get("context", ""),
                                                "view_mode": a.get("view_mode", ""), "limit": a.get("limit", 80)}
                                               for a in actions]})

            action = actions[0]
            aid = action["id"]
            before = {k: action.get(k) for k in ["domain", "context", "view_mode", "limit"]}
            update_vals: dict[str, Any] = {}
            if args.get("domain") is not None:
                update_vals["domain"] = args["domain"]
            if args.get("context") is not None:
                update_vals["context"] = args["context"]
            if args.get("view_mode") is not None:
                update_vals["view_mode"] = args["view_mode"]
            if args.get("limit") is not None:
                update_vals["limit"] = args["limit"]
            if args.get("order") is not None:
                existing_ctx = action.get("context", "{}")
                try:
                    ctx_dict = eval(existing_ctx) if existing_ctx else {}
                except Exception:
                    ctx_dict = {}
                ctx_dict["default_order"] = args["order"]
                update_vals["context"] = repr(ctx_dict)

            odoo.execute("ir.actions.act_window", "write", [aid], update_vals)
            updated = odoo.search_read("ir.actions.act_window", [["id", "=", aid]],
                                        ["domain", "context", "view_mode", "limit"], 1)
            after = {k: updated[0].get(k) for k in ["domain", "context", "view_mode", "limit"]} if updated else {}
            return json.dumps({"action_id": aid, "name": action.get("name", ""),
                               "operation": "updated", "before": before, "after": after}, default=str)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Detailed skill loader — reads SKILL.md + references/
# ---------------------------------------------------------------------------

SKILLS_DIR = Path(__file__).parent / "skills"

# Map task categories to skill folders and which references to load per task
_SKILL_REFS = {
    "model-customize": {
        "folder": "write/odoo-model-customize",
        "always": ["safety-boundary.md", "discovery.md"],
        "keyword_refs": {
            "default": ["defaults.md"],
            "sort": ["sort-order.md"],
            "order": ["sort-order.md"],
            "filter": ["filters.md", "saved-filters.md"],
            "groupby": ["filters.md"],
            "field": ["custom-fields.md"],
            "x_": ["custom-fields.md"],
            "view": ["view-inheritance.md"],
            "form": ["view-inheritance.md"],
            "tree": ["view-inheritance.md"],
            "automat": ["automation.md"],
            "trigger": ["automation.md"],
        },
    },
    "field-management": {
        "folder": "write/odoo-model-customize",
        "always": ["safety-boundary.md", "discovery.md", "custom-fields.md"],
        "keyword_refs": {
            "view": ["view-inheritance.md"],
            "track": ["discovery.md"],
        },
    },
    "view-customize": {
        "folder": "write/odoo-model-customize",
        "always": ["safety-boundary.md", "discovery.md"],
        "keyword_refs": {
            "inherit": ["view-inheritance.md"],
            "xpath": ["view-inheritance.md"],
            "form": ["view-inheritance.md"],
            "tree": ["view-inheritance.md"],
            "filter": ["filters.md", "saved-filters.md"],
            "action": ["filters.md"],
            "automat": ["automation.md"],
        },
    },
}


def _load_detailed_skills(category: str, task: str) -> Optional[str]:
    """Load SKILL.md + relevant references for a category. Returns markdown or None."""
    cfg = _SKILL_REFS.get(category)
    if not cfg:
        return None

    skill_dir = SKILLS_DIR / cfg["folder"]
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    parts = []

    # Load SKILL.md (main index)
    parts.append(skill_md.read_text(encoding="utf-8"))

    # Load always-included references
    refs_dir = skill_dir / "references"
    loaded = set()
    for ref in cfg.get("always", []):
        ref_path = refs_dir / ref
        if ref_path.exists() and ref not in loaded:
            parts.append(f"\n---\n# Reference: {ref}\n")
            parts.append(ref_path.read_text(encoding="utf-8"))
            loaded.add(ref)

    # Load keyword-matched references
    task_lower = task.lower()
    for keyword, refs in cfg.get("keyword_refs", {}).items():
        if keyword in task_lower:
            for ref in refs:
                ref_path = refs_dir / ref
                if ref_path.exists() and ref not in loaded:
                    parts.append(f"\n---\n# Reference: {ref}\n")
                    parts.append(ref_path.read_text(encoding="utf-8"))
                    loaded.add(ref)

    content = "\n".join(parts)
    # Truncate if too large for context
    if len(content) > 12000:
        content = content[:12000] + "\n\n<!-- ... truncated for context -->"
    return content


# ---------------------------------------------------------------------------
# Skill retriever
# ---------------------------------------------------------------------------

def retrieve_skills(skill_bank: dict, task: str, top_k: int = 5, category: str = None) -> str:
    """Retrieve relevant skills. Uses detailed SKILL.md when available, falls back to JSON bank."""
    task_lower = task.lower()

    # Detect category if not provided
    cat_keywords = {
        "health-check": ["health", "diagnos", "doctor", "status", "cron", "error", "log"],
        "deploy-module": ["deploy", "install", "upgrade", "module", "depend"],
        "inventory-audit": ["inventory", "stock", "quant", "warehouse", "transfer", "picking", "negative"],
        "invoice-posting": ["invoice", "post", "draft", "bill", "payment", "overdue"],
        "backup-restore": ["backup", "restore", "database", "dump", "recovery"],
        "model-customize": ["sort", "order", "default", "dropdown", "selection", "customize", "reorder", "_order", "many2one", "ir.default"],
        "field-management": ["field", "custom", "x_", "manual", "computed", "relational", "many2many", "one2many", "tracking", "ir.model.fields"],
        "view-customize": ["view", "form", "tree", "list", "search", "filter", "action", "window", "ir.ui.view", "act_window", "ir.filters", "groupby"],
    }
    if category:
        best_cat = category
    else:
        best_cat, best_score = None, 0
        for cat, kws in cat_keywords.items():
            score = sum(1 for kw in kws if kw in task_lower)
            if score > best_score:
                best_cat, best_score = cat, score

    # Try detailed markdown skills first (model-customize, field-management, view-customize)
    if best_cat:
        detailed = _load_detailed_skills(best_cat, task)
        if detailed:
            return detailed

    # Fall back to JSON skill bank for other categories
    parts = []

    def keyword_score(item: dict) -> float:
        text = " ".join(str(v) for v in item.values()).lower()
        query_words = set(re.findall(r"\w+", task_lower))
        item_words = set(re.findall(r"\w+", text))
        if not query_words:
            return 0.0
        return len(query_words & item_words) / len(query_words)

    # General skills
    gs = skill_bank.get("general_skills", [])
    gs_ranked = sorted(gs, key=keyword_score, reverse=True)[:top_k]
    if gs_ranked:
        parts.append("## Relevant Skills")
        for s in gs_ranked:
            parts.append(f"- **{s['title']}**: {s.get('principle', '')} → {s.get('application', '')}")

    # Task-specific
    if best_cat:
        ts_data = skill_bank.get("task_specific_skills", {})
        if isinstance(ts_data, dict):
            ts = ts_data.get(best_cat, [])
        elif isinstance(ts_data, list):
            ts = [s for s in ts_data if s.get("category") == best_cat]
        else:
            ts = []
        if ts:
            parts.append(f"\n## Task-Specific ({best_cat})")
            for s in ts:
                parts.append(f"- **{s['title']}**: {s.get('heuristic', '')} → {s.get('application', '')}")

    # Mistakes
    cms = skill_bank.get("common_mistakes", [])
    cms_ranked = sorted(cms, key=keyword_score, reverse=True)[:3]
    if cms_ranked:
        parts.append("\n## Mistakes to Avoid")
        for m in cms_ranked:
            title = m.get('title', m.get('mistake', m.get('id', '?')))
            desc = m.get('description', m.get('consequence', ''))
            fix = m.get('avoidance', m.get('prevention', ''))
            parts.append(f"- **{title}**: {desc} → {fix}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM client — uses Claude Code's internal OAuth token
# ---------------------------------------------------------------------------

def _load_claude_code_token() -> Optional[str]:
    """Read the OAuth access token from Claude Code's credentials file."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return None
    try:
        with open(creds_path) as f:
            creds = json.load(f)
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except Exception as e:
        log.warning("Could not read Claude Code credentials: %s", e)
        return None


def create_llm_client():
    """Create Anthropic client using Claude Code's internal auth."""
    import anthropic

    # Priority 1: Claude Code internal OAuth token
    token = _load_claude_code_token()
    if token:
        log.info("Using Claude Code internal auth token")
        return anthropic.Anthropic(api_key=token)

    # Priority 2: ANTHROPIC_API_KEY env var
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        log.info("Using ANTHROPIC_API_KEY")
        return anthropic.Anthropic(api_key=api_key)

    # Priority 3: OPENROUTER_API_KEY via OpenAI compat (legacy fallback)
    or_key = os.environ.get("OPENROUTER_API_KEY")
    if or_key:
        log.info("Using OPENROUTER_API_KEY (legacy fallback)")
        return anthropic.Anthropic(
            api_key=or_key,
            base_url="https://openrouter.ai/api/v1",
        )

    log.error("No API key found. Need Claude Code credentials, ANTHROPIC_API_KEY, or OPENROUTER_API_KEY")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Task bank
# ---------------------------------------------------------------------------

# Each task category has a tier — read tasks must use only read tools.
# The reward function uses this to bonus/penalize tier discipline.
CATEGORY_TIER = {
    "health-check": "read",
    "deploy-module": "read",
    "inventory-audit": "read",
    "invoice-posting": "read",
    "backup-restore": "read",
    "model-customize": "write",
    "field-management": "write",
    "view-customize": "write",
    # Grey-area judgment tasks: span multiple modules, require interpretation,
    # mix discovery with mutation. Tier=write because the dominant intent is
    # to produce MOs / records, but the agent is expected to discover first.
    "cross-module-judgment": "write",
}

# Tools that mutate Odoo state. The reward function penalizes any use of
# these during a read-tier task and bonuses zero use.
WRITE_TOOLS = {"odoo_set_default", "odoo_create", "odoo_delete", "odoo_update"}

# Methods that mutate state when called via odoo_execute.
WRITE_METHODS = {"write", "create", "unlink", "copy", "name_create"}


def is_write_call(tc: dict) -> bool:
    """Return True if this tool call mutates Odoo state."""
    name = tc.get("tool", "")
    if name in WRITE_TOOLS:
        return True
    args = tc.get("args", {}) or {}
    if name == "odoo_modify_action":
        # Read when called with just `model` to list actions; write when an
        # action_id is supplied alongside any change params.
        if "action_id" in args and any(k in args for k in ("domain", "context", "order", "limit", "view_mode")):
            return True
    if name == "odoo_execute":
        method = args.get("method", "")
        if method in WRITE_METHODS or method.startswith(("action_", "button_")):
            return True
    return False


TASKS = {
    "health-check": [
        "Run a full health check on the Odoo instance and report any issues found.",
        "Check if there are any stuck or overdue scheduled actions in ir.cron.",
        "List all installed modules and flag any in 'to upgrade' state.",
        "Check the Odoo server version and count active users.",
        "Look for recent error logs in ir.logging from the last 24 hours.",
        "Verify the database connection and report basic instance info.",
    ],
    "deploy-module": [
        "Check if the 'sale' module is installed and list its dependencies.",
        "Verify the state of the 'stock' module and its dependents.",
        "Check if there are any modules in 'to install' or 'to upgrade' state.",
        "Show me the dependency tree for the 'account' module.",
        "List all uninstalled modules available on this instance.",
        "Check if 'website_sale' is installed and what modules it depends on.",
    ],
    "inventory-audit": [
        "Check for any products with negative stock quantities.",
        "Find all stock transfers that are overdue past their scheduled date.",
        "Audit current inventory levels for the top 20 products by quantity.",
        "Check for stock quants with zero quantity but non-zero reserved quantity.",
        "List all warehouse locations and count stock quants per location.",
        "Find products marked as storable with zero available quantity.",
    ],
    "invoice-posting": [
        "Find all draft customer invoices and summarize by customer.",
        "Check for overdue unpaid invoices and list them by age.",
        "Show the total amount of draft invoices waiting to be posted.",
        "Find invoices posted in the last 7 days and their payment status.",
        "List all vendor bills in draft state with their due dates.",
        "Count invoices by state (draft, posted, cancelled) and move type.",
    ],
    "backup-restore": [
        "What is the current database name and Odoo version for backup docs?",
        "Check key record counts: partners, invoices, sale orders, stock pickings.",
        "Count ir.attachment records to estimate filestore size.",
        "List scheduled actions that might affect a restore procedure.",
        "Check for any pending database operations (modules to install/upgrade).",
        "Verify data integrity by counting records in core models.",
    ],
    "model-customize": [
        # Runtime-safe: ir.default
        "Set the default value of 'invoice_policy' on product.template to 'delivery' using ir.default.",
        "Check what default values are currently set for sale.order fields via ir.default, and show them.",
        "Set a global default for the 'type' field on res.partner to 'contact' using ir.default.",
        # Runtime-safe: window actions
        "Change the sale order list view to sort by date_order descending by modifying the window action.",
        "Modify the account.move window action to default-filter only posted invoices.",
        "Add a default groupby of 'partner_id' to the sale.order window action context.",
        # Discovery / audit
        "What fields are available on sale.order? Show which ones are required vs optional.",
        "Show the current _order attribute for sale.order. Explain whether it can be changed at runtime or needs a custom module.",
        "Find all Many2one fields on account.move that reference res.partner.",
        "List all selection fields on crm.lead and list their possible values.",
    ],
    "field-management": [
        # Runtime-safe: custom x_ fields
        "Add a custom text field 'x_internal_notes' to res.partner for internal team comments.",
        "List all custom fields (x_ prefix) on res.partner and show their types.",
        "Add a custom selection field 'x_priority_level' on sale.order with values: low, medium, high.",
        # Discovery / audit
        "Check if there are any user-created fields (state='manual') across all models.",
        "List all relational fields (Many2one, One2many, Many2many) on sale.order.line and their target models.",
        "Show the field properties (type, required, readonly, help text) for all fields on product.template.",
        "Check which fields on res.partner have tracking enabled for the chatter log.",
    ],
    "view-customize": [
        # Runtime-safe: inherited views via XPath
        "Create an inherited view to add the 'x_internal_notes' field to the res.partner form view using XPath.",
        "Create an inherited tree view for sale.order that adds the 'amount_untaxed' column after the 'name' column.",
        # Runtime-safe: ir.filters
        "Create a shared saved filter on sale.order that shows only confirmed orders from this month.",
        "List all ir.filters (saved filters) for stock.picking and show their domains.",
        # Runtime-safe: window actions
        "Find all window actions for sale.order and show their default domain, context, and view modes.",
        "Modify the stock.picking window action to show 200 records per page instead of the default.",
        # Runtime-safe: automated actions
        "Create an automated action on res.partner that logs a note when the email field is changed.",
        # Discovery
        "Show the rendered form view for res.partner and list all fields visible in it.",
        "List all inherited views for the sale.order form view, showing their priority and module origin.",
    ],
    "cross-module-judgment": [
        # Ambiguous prompts that require the agent to interpret intent,
        # discover what's available, and make judgment calls. Span multiple
        # Odoo modules. Some tasks are designed to fail gracefully when
        # required modules are missing — that's part of the learning.
        #
        # CRM-style fallback (crm module not installed on this instance —
        # the agent must detect that and either fall back to sale.order
        # pipeline data or explain the limitation).
        "Give me our most likely leads to convert into sales this quarter. Use whatever data you have available — if a CRM module isn't installed, fall back to sale order pipeline signals.",
        "Find our top 5 prospects by likely deal value. Explain what 'likely' means based on the data available on this instance.",
        # Manufacturing + project + product knowledge
        "Create a manufacturing order that transforms products related to project X into a single combined product. Pick a project that actually has linked products. Choose a sensible target product. Explain your reasoning.",
        "Make three new manufacturing orders that ensure our recently produced items are sufficiently polished and burred for sale. Find the recent MOs first, then plan the finishing work.",
        "I need to fulfill all draft sale orders for the next 7 days. Build a manufacturing plan: which MOs are needed, in what quantity, and flag any component shortages.",
        # Inventory + sales judgment
        "Identify our slowest-moving products and propose which ones to mark down. Use stock movement data, not just current quantities. Justify each pick.",
        "Which sale orders are at risk of late delivery based on current stock levels? Cross-reference sale.order.line with stock availability.",
        # Multi-step finishing pipeline
        "We just finished a batch of raw furniture parts. Create the finishing MOs (sand, stain, polish) so they're ready to ship. Find the recent finished components first, then plan the right number of follow-up MOs based on what you find.",
        # Project + procurement
        "Project X needs materials we don't have in stock. Find what's missing across all open project tasks, then create the right purchase orders or manufacturing orders to fill the gap.",
        # Judgment with no clear right answer
        "Suggest which open quotations are most likely to close this month. Rank them and explain your scoring method.",
    ],
}


# ---------------------------------------------------------------------------
# Scoring — reward function
# ---------------------------------------------------------------------------

@dataclass
class EpisodeResult:
    task: str
    category: str
    messages: list
    tool_calls: list
    turns: int
    completed: bool
    error_count: int
    odoo_errors: int
    final_response: str
    duration_s: float
    skills_used: str  # the skills prompt injected

    @property
    def reward(self) -> float:
        """Compute reward for this episode. Range: -1.0 to 1.0"""
        r = 0.0

        # Task completion (did the agent produce a substantive answer?)
        if self.completed and len(self.final_response) > 50:
            r += 0.4
        elif self.completed:
            r += 0.15

        # Tool usage (did it actually query Odoo?)
        if self.tool_calls:
            r += 0.15
        # Bonus for using multiple relevant tools
        if len(self.tool_calls) >= 2:
            r += 0.1

        # Efficiency (fewer turns = better, but need at least 1 tool call)
        if 1 <= self.turns <= 5:
            r += 0.1
        elif self.turns > 10:
            r -= 0.1

        # --- Safety-aware customization rewards ---
        tools_used = {tc["tool"] for tc in self.tool_calls}
        response_lower = self.final_response.lower()

        # Reward using the safe, specialized customization tools
        safe_tools = {"odoo_model_info", "odoo_set_default", "odoo_get_view", "odoo_modify_action"}
        if tools_used & safe_tools:
            r += 0.15  # used the right tools for the job

        # Reward when agent correctly identifies runtime-safe vs module-required
        safe_patterns = [
            "ir.default", "ir.filters", "ir.actions.act_window",
            "inherited view", "inherit_id", "xpath",
            "x_", "custom field", "state='manual'", "base.automation",
            "runtime", "no module needed", "without a custom module",
        ]
        if any(p in response_lower for p in safe_patterns):
            r += 0.1  # demonstrates knowledge of safe modification paths

        # Reward when agent correctly flags that something needs a custom module
        module_awareness = [
            "requires a custom module", "needs a module", "cannot be changed at runtime",
            "_order", "python inheritance", "class attribute",
            "override", "stored compute", "@api.constrains",
        ]
        if any(p in response_lower for p in module_awareness):
            r += 0.1  # correctly identifies module-required operations

        # --- Tier discipline rewards ---
        #
        # Read-tier tasks should never call a mutating tool. Bonus for clean
        # read episodes; significant penalty for write calls during read tasks.
        tier = CATEGORY_TIER.get(self.category, "write")
        write_calls = [tc for tc in self.tool_calls if is_write_call(tc)]
        if tier == "read":
            if not write_calls:
                r += 0.2   # bonus for staying read-only
            else:
                r -= 0.4   # hard penalty per offending episode

        # --- Safety penalties ---

        # Penalize attempting to directly write _order or core model attributes
        unsafe_writes = False
        for tc in self.tool_calls:
            args = tc.get("args", {})
            # Tried to write to ir.model.order directly
            if tc["tool"] in ("odoo_update", "odoo_execute"):
                model = args.get("model", "")
                if model == "ir.model" and "order" in json.dumps(args.get("values", args.get("kwargs", {}))):
                    unsafe_writes = True
                # Tried to write state fields directly on workflow models
                vals = json.dumps(args.get("values", args.get("kwargs", {})))
                if "'state'" in vals or '"state"' in vals:
                    if model in ("sale.order", "account.move", "stock.picking", "purchase.order"):
                        unsafe_writes = True
        if unsafe_writes:
            r -= 0.3  # significant penalty for unsafe direct writes

        # Penalize Odoo errors and other errors
        r -= self.odoo_errors * 0.12
        r -= self.error_count * 0.08

        # Clamp
        return max(-1.0, min(1.0, r))


# ---------------------------------------------------------------------------
# Agent loop — run one episode
# ---------------------------------------------------------------------------

def run_episode(
    llm_client,
    odoo: OdooClient,
    model: str,
    task: str,
    category: str,
    skill_bank: dict,
    max_turns: int = 15,
    replay_examples: str = "",
) -> EpisodeResult:
    """Run one agent episode: task → tool calls → final answer."""

    skills_prompt = retrieve_skills(skill_bank, task, category=category)

    system = (
        "You are O-CLI, an Odoo operations agent. You have tools to query and manage "
        "a live Odoo instance. Be direct and efficient.\n\n"
        "ALWAYS use the odoo tools to answer questions. Do not guess or make up data.\n"
        "Verify before mutating. Confirm destructive operations.\n"
    )
    if skills_prompt:
        system += f"\n# Learned Skills\n{skills_prompt}\n"
    if replay_examples:
        system += f"\n{replay_examples}\n"

    messages = [{"role": "user", "content": task}]

    tool_calls_log = []
    error_count = 0
    odoo_errors = 0
    final_response = ""
    t0 = time.time()

    for turn in range(max_turns):
        try:
            # Retry with backoff on rate limits
            response = None
            for attempt in range(5):
                try:
                    response = llm_client.messages.create(
                        model=model,
                        system=system,
                        messages=messages,
                        tools=ODOO_TOOLS,
                        temperature=0.3,
                        max_tokens=2048,
                    )
                    break
                except Exception as api_err:
                    if "429" in str(api_err) and attempt < 4:
                        wait = (2 ** attempt) * 5  # 5, 10, 20, 40s
                        log.info("  Rate limited, waiting %ds...", wait)
                        time.sleep(wait)
                    else:
                        raise
            if response is None:
                raise RuntimeError("Failed after retries")
        except Exception as e:
            log.warning("  LLM API error on turn %d: %s", turn + 1, e)
            error_count += 1
            break

        # Check stop reason
        stop_reason = response.stop_reason

        # Extract text and tool_use blocks from content
        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        # Append assistant message (preserving full content for Anthropic format)
        messages.append({"role": "assistant", "content": response.content})

        # No tool calls — agent is done
        if not tool_uses:
            final_response = "\n".join(text_parts)
            break

        # Process tool calls
        tool_results = []
        for tu in tool_uses:
            fn_name = tu.name
            fn_args = tu.input or {}

            result = execute_tool(odoo, fn_name, fn_args)
            tool_calls_log.append({"tool": fn_name, "args": fn_args, "result_preview": result[:200]})

            if '"error"' in result:
                odoo_errors += 1

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

        # Capture any text alongside tool calls
        if text_parts:
            final_response = "\n".join(text_parts)

    duration = time.time() - t0

    # If we exhausted turns without a final text response, find the last one
    if not final_response:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                content = m.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if hasattr(block, "type") and block.type == "text":
                            final_response = block.text
                            break
                if final_response:
                    break

    return EpisodeResult(
        task=task,
        category=category,
        messages=[],  # don't store raw Anthropic objects
        tool_calls=tool_calls_log,
        turns=len([m for m in messages if m.get("role") == "assistant"]),
        completed=bool(final_response),
        error_count=error_count,
        odoo_errors=odoo_errors,
        final_response=final_response,
        duration_s=duration,
        skills_used=skills_prompt,
    )


# ---------------------------------------------------------------------------
# Skill evolution — distill new skills from trajectories
# ---------------------------------------------------------------------------

def evolve_skills(
    llm_client,
    model: str,
    skill_bank: dict,
    recent_episodes: list[EpisodeResult],
    update_threshold: float = 0.4,
) -> dict:
    """Evolve the skill bank based on recent episode results."""

    # Split into successes and failures
    successes = [e for e in recent_episodes if e.reward >= update_threshold]
    failures = [e for e in recent_episodes if e.reward < 0]

    if not successes and not failures:
        log.info("  No strong signals for skill evolution, skipping")
        return skill_bank

    # Build evolution prompt
    success_summaries = []
    for e in successes[:10]:
        tools_used = [tc["tool"] for tc in e.tool_calls]
        success_summaries.append(
            f"- [{e.category}] \"{e.task}\" → reward={e.reward:.2f}, "
            f"tools={tools_used}, turns={e.turns}"
        )

    failure_summaries = []
    for e in failures[:10]:
        tools_used = [tc["tool"] for tc in e.tool_calls]
        failure_summaries.append(
            f"- [{e.category}] \"{e.task}\" → reward={e.reward:.2f}, "
            f"errors={e.odoo_errors}, tools={tools_used}"
        )

    prompt = f"""You are evolving a skill bank for an Odoo ERP agent. Based on recent episode results, suggest updates.

CURRENT SKILL BANK:
{json.dumps(skill_bank, indent=2)[:8000]}

RECENT SUCCESSES (reward >= {update_threshold}):
{chr(10).join(success_summaries) if success_summaries else "None"}

RECENT FAILURES (reward < 0):
{chr(10).join(failure_summaries) if failure_summaries else "None"}

Based on these results, return an updated skill bank JSON with:
1. Keep all existing skills that still seem valid
2. Add new general_skills if you see patterns across successes
3. Add new task_specific_skills if category-specific patterns emerge
4. Add new common_mistakes if failures reveal avoidable patterns
5. Remove or update skills that seem counterproductive

Return ONLY valid JSON with keys: general_skills, task_specific_skills, common_mistakes
Use the same ID format: GS-XXX, TS-XX-XXX, CM-XXX (increment from existing max).
"""

    try:
        response = llm_client.messages.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=8000,
        )
        content = response.content[0].text if response.content else ""

        # Parse JSON from response
        try:
            new_bank = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            if match:
                new_bank = json.loads(match.group(1))
            else:
                log.warning("  Could not parse evolution response, keeping current bank")
                return skill_bank

        # Validate structure
        if "general_skills" in new_bank and "common_mistakes" in new_bank:
            return new_bank
        else:
            log.warning("  Evolution response missing required keys, keeping current bank")
            return skill_bank

    except Exception as e:
        log.warning("  Skill evolution failed: %s", e)
        return skill_bank


# ---------------------------------------------------------------------------
# 1. Experience Replay — store top trajectories, inject as few-shot examples
#    (Sarukkai et al. NeurIPS 2025: 73% → 93% on ALFWorld)
# ---------------------------------------------------------------------------

class ExperienceReplay:
    """Stores top-scoring full trajectories for few-shot injection."""

    def __init__(self, max_size: int = 50):
        self.max_size = max_size
        self.buffer: list[dict] = []  # sorted by reward desc

    def add(self, episode: 'EpisodeResult'):
        """Add a successful episode to the replay buffer."""
        if episode.reward < 0.5 or not episode.completed:
            return  # only keep good episodes
        entry = {
            "task": episode.task,
            "category": episode.category,
            "reward": episode.reward,
            "tool_calls": episode.tool_calls,
            "final_response": episode.final_response[:1000],
        }
        self.buffer.append(entry)
        self.buffer.sort(key=lambda x: x["reward"], reverse=True)
        if len(self.buffer) > self.max_size:
            self.buffer = self.buffer[:self.max_size]

    def get_examples(self, category: str, n: int = 2) -> str:
        """Get top n examples for a category as a few-shot prompt section."""
        matching = [e for e in self.buffer if e["category"] == category]
        if not matching:
            # Fall back to any top examples
            matching = self.buffer[:n]
        else:
            matching = matching[:n]

        if not matching:
            return ""

        parts = ["# Successful Past Examples (use as reference)\n"]
        for i, ex in enumerate(matching, 1):
            tools_used = [tc["tool"] for tc in ex["tool_calls"]]
            parts.append(f"## Example {i}: {ex['task'][:80]}")
            parts.append(f"Tools used: {', '.join(tools_used)}")
            parts.append(f"Reward: {ex['reward']:.2f}")
            parts.append(f"Response: {ex['final_response'][:400]}\n")
        return "\n".join(parts)

    def save(self, path: Path):
        with open(path, "w") as f:
            json.dump(self.buffer, f, indent=2, default=str)

    def load(self, path: Path):
        if path.exists():
            with open(path) as f:
                self.buffer = json.load(f)


# ---------------------------------------------------------------------------
# 2. Trajectory Repair — fix failed episodes and add as positive examples
#    (SiriuS, Zhao et al. NeurIPS 2025: 2-22% gains)
# ---------------------------------------------------------------------------

def repair_trajectory(
    llm_client,
    model: str,
    episode: 'EpisodeResult',
) -> Optional[dict]:
    """Take a failed episode and have the LLM generate a corrected approach."""
    if episode.reward >= 0.3:
        return None  # only repair clear failures

    tools_used = [tc["tool"] for tc in episode.tool_calls]
    errors = [tc for tc in episode.tool_calls if '"error"' in tc.get("result_preview", "")]
    error_details = "\n".join(f"  - {e['tool']}({e['args']}): {e['result_preview'][:150]}" for e in errors[:5])

    prompt = f"""An Odoo agent attempted this task and failed. Analyze what went wrong and write the CORRECT approach.

TASK: {episode.task}
CATEGORY: {episode.category}
REWARD: {episode.reward:.2f}
TOOLS USED: {tools_used}
ERRORS:
{error_details}

AGENT'S RESPONSE (incomplete/wrong):
{episode.final_response[:500]}

Write the CORRECT approach as a brief step-by-step with the right tool calls and expected behavior.
Focus on: what tools should be called, in what order, with what arguments, and what the correct response should include.
Keep it under 300 words.
"""

    try:
        response = llm_client.messages.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1000,
        )
        corrected = response.content[0].text if response.content else ""
        if corrected:
            return {
                "task": episode.task,
                "category": episode.category,
                "original_reward": episode.reward,
                "corrected_approach": corrected,
                "errors_fixed": [e["tool"] for e in errors],
            }
    except Exception as e:
        log.warning("  Trajectory repair failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# 3. Self-Editing Skills — agent proposes patches to SKILL.md references
#    (SICA, Robeyns et al. 2025: 17-53% improvement)
# ---------------------------------------------------------------------------

def self_edit_skills(
    llm_client,
    model: str,
    recent_episodes: list['EpisodeResult'],
    repaired: list[dict],
    skills_dir: Path,
) -> list[str]:
    """Let the agent propose patches to SKILL.md reference files based on experience."""
    # Only edit if we have enough data
    if len(recent_episodes) < 5:
        return []

    # Gather signal from episodes
    successes = [e for e in recent_episodes if e.reward >= 0.7]
    failures = [e for e in recent_episodes if e.reward < 0.3]
    customize_cats = {"model-customize", "field-management", "view-customize"}
    relevant = [e for e in recent_episodes if e.category in customize_cats]

    if not relevant:
        return []

    # Read current skill files (write tier — only write skill self-edits from RL experience)
    skill_folder = skills_dir / "write" / "odoo-model-customize"
    refs_dir = skill_folder / "references"
    if not refs_dir.exists():
        return []

    current_refs = {}
    for ref_file in refs_dir.glob("*.md"):
        current_refs[ref_file.name] = ref_file.read_text(encoding="utf-8")[:3000]

    # Build the prompt
    success_patterns = []
    for e in successes[:5]:
        if e.category in customize_cats:
            tools = [tc["tool"] for tc in e.tool_calls]
            success_patterns.append(f"- [{e.category}] r={e.reward:.2f}: {e.task[:60]} → tools: {tools}")

    failure_patterns = []
    for e in failures[:5]:
        if e.category in customize_cats:
            errs = [tc for tc in e.tool_calls if '"error"' in tc.get("result_preview", "")]
            failure_patterns.append(f"- [{e.category}] r={e.reward:.2f}: {e.task[:60]} → errors: {[e['tool'] for e in errs]}")

    repair_insights = []
    for r in repaired[:3]:
        if r["category"] in customize_cats:
            repair_insights.append(f"- {r['task'][:60]}: {r['corrected_approach'][:200]}")

    if not success_patterns and not failure_patterns:
        return []

    prompt = f"""You are improving the reference documentation for an Odoo model customization skill.
Based on real agent experience, suggest specific PATCHES to the reference files.

CURRENT REFERENCE FILES:
{json.dumps({k: v[:1500] for k, v in current_refs.items()}, indent=2)[:6000]}

WHAT WORKED WELL:
{chr(10).join(success_patterns) if success_patterns else "No strong successes yet"}

WHAT FAILED:
{chr(10).join(failure_patterns) if failure_patterns else "No clear failures"}

REPAIRED TRAJECTORIES:
{chr(10).join(repair_insights) if repair_insights else "None"}

For each file that needs updating, output:
FILE: filename.md
PATCH: what to add or change (be specific — include the actual text to add)

Only suggest patches that address real problems observed in the episodes.
Do not rewrite entire files — suggest targeted additions or corrections.
"""

    try:
        response = llm_client.messages.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=3000,
        )
        content = response.content[0].text if response.content else ""
    except Exception as e:
        log.warning("  Self-edit skills failed: %s", e)
        return []

    # Parse and apply patches
    patches_applied = []
    blocks = re.split(r"FILE:\s*", content)
    for block in blocks[1:]:  # skip preamble
        lines = block.strip().split("\n", 1)
        if len(lines) < 2:
            continue
        filename = lines[0].strip()
        patch_text = lines[1].strip()
        if patch_text.startswith("PATCH:"):
            patch_text = patch_text[6:].strip()

        ref_path = refs_dir / filename
        if ref_path.exists() and patch_text:
            # Append patch as a new section
            current = ref_path.read_text(encoding="utf-8")
            addition = f"\n\n## Learned from Experience\n\n{patch_text}\n"
            # Only add if not already present (idempotent)
            if patch_text[:100] not in current:
                ref_path.write_text(current + addition, encoding="utf-8")
                patches_applied.append(filename)
                log.info("  Patched: %s", filename)

    return patches_applied


# ---------------------------------------------------------------------------
# Logging & persistence
# ---------------------------------------------------------------------------

def save_episode(episode: EpisodeResult, output_dir: Path):
    """Append episode to trajectory log."""
    traj_file = output_dir / "trajectories.jsonl"
    entry = {
        "task": episode.task,
        "category": episode.category,
        "reward": episode.reward,
        "turns": episode.turns,
        "tool_calls": episode.tool_calls,
        "completed": episode.completed,
        "error_count": episode.error_count,
        "odoo_errors": episode.odoo_errors,
        "duration_s": round(episode.duration_s, 2),
        "final_response_preview": episode.final_response[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(traj_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def save_skill_bank(skill_bank: dict, output_dir: Path):
    """Save current skill bank with backup."""
    bank_file = output_dir / "odoo_skills.json"
    # Backup previous version
    if bank_file.exists():
        backup = output_dir / f"odoo_skills.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak.json"
        bank_file.rename(backup)
    with open(bank_file, "w") as f:
        json.dump(skill_bank, f, indent=2)
    # Also update the canonical location
    canonical = Path(__file__).parent / "skill_bank" / "odoo_skills.json"
    with open(canonical, "w") as f:
        json.dump(skill_bank, f, indent=2)


def print_stats(episodes: list[EpisodeResult], generation: int):
    """Print generation stats."""
    if not episodes:
        return
    rewards = [e.reward for e in episodes]
    avg_r = sum(rewards) / len(rewards)
    max_r = max(rewards)
    min_r = min(rewards)
    completions = sum(1 for e in episodes if e.completed) / len(episodes) * 100
    avg_turns = sum(e.turns for e in episodes) / len(episodes)
    total_errors = sum(e.odoo_errors for e in episodes)

    log.info("=" * 60)
    log.info("  Generation %d — %d episodes", generation, len(episodes))
    log.info("  Reward:     avg=%.3f  min=%.3f  max=%.3f", avg_r, min_r, max_r)
    log.info("  Completion: %.0f%%", completions)
    log.info("  Avg turns:  %.1f", avg_turns)
    log.info("  Odoo errors: %d total", total_errors)
    log.info("=" * 60)


def sync_to_registry(odoo_skills_repo: str) -> None:
    """Run odoo-skills/tools/sync-from-rl.py --apply to copy graduated
    skills into the published registry. Logs success/failure but never
    raises — sync issues should not crash the RL loop."""
    import subprocess
    sync_script = Path(odoo_skills_repo) / "tools" / "sync-from-rl.py"
    if not sync_script.exists():
        log.warning("  Sync skipped: %s not found", sync_script)
        return
    try:
        result = subprocess.run(
            ["python3", str(sync_script), "--apply"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            # Surface "Files added/updated" lines so we know what flowed.
            for line in result.stdout.splitlines():
                if line.startswith(("Files added", "Files updated", "PASS", "FAIL", "Results:")):
                    log.info("  [sync] %s", line)
            log.info("  Sync to %s: OK", odoo_skills_repo)
        else:
            log.warning("  Sync FAILED (rc=%d):\n%s", result.returncode, result.stderr.strip())
    except Exception as exc:
        log.warning("  Sync error: %s", exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SkillRL — long-running Odoo skill evolution")
    parser.add_argument("--episodes", type=int, default=0, help="Total episodes (0 = run forever)")
    parser.add_argument("--evolve-every", type=int, default=10, help="Evolve skill bank every N episodes")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001", help="LLM model")
    parser.add_argument("--skill-bank", default=None, help="Path to initial skill bank JSON")
    parser.add_argument("--max-turns", type=int, default=12, help="Max turns per episode")
    parser.add_argument("--cooldown", type=float, default=10.0, help="Seconds between episodes")
    parser.add_argument("--categories", type=str, default=None, help="Comma-separated categories to run (e.g. model-customize,view-customize)")
    parser.add_argument("--odoo-url", default=None, help="Override ODOO_URL")
    parser.add_argument("--odoo-db", default=None, help="Override ODOO_DB")
    parser.add_argument("--sync-after-evolve", action="store_true",
                        help="After each generation, run odoo-skills/tools/sync-from-rl.py --apply to copy graduated skills into the published registry")
    parser.add_argument("--odoo-skills-repo", default="/home/ec2-user/odoo-skills",
                        help="Path to the odoo-skills registry repo (used by --sync-after-evolve)")
    args = parser.parse_args()

    # --- Output dir ---
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(__file__).parent / "runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Load skill bank ---
    bank_path = args.skill_bank or str(Path(__file__).parent / "skill_bank" / "odoo_skills.json")
    with open(bank_path) as f:
        skill_bank = json.load(f)
    gs = len(skill_bank.get("general_skills", []))
    ts_data = skill_bank.get("task_specific_skills", {})
    ts = sum(len(v) for v in ts_data.values()) if isinstance(ts_data, dict) else len(ts_data)
    cm = len(skill_bank.get("common_mistakes", []))
    log.info("Loaded skill bank: %d general, %d task-specific, %d mistakes", gs, ts, cm)

    # --- Connect to Odoo ---
    odoo_url = args.odoo_url or os.environ.get("ODOO_URL", "http://13.210.13.41:8069")
    odoo_db = args.odoo_db or os.environ.get("ODOO_DB", "odoo-clawd-19")
    odoo_user = os.environ.get("ODOO_USER", "admin")
    odoo_pass = os.environ.get("ODOO_PASSWORD", "admin")
    odoo_key = os.environ.get("ODOO_API_KEY")

    odoo = OdooClient(odoo_url, odoo_db, odoo_user, password=odoo_pass, api_key=odoo_key)
    odoo.authenticate()
    log.info("Connected to Odoo %s @ %s/%s (uid=%d)", odoo.version, odoo_url, odoo_db, odoo.uid)

    # --- LLM client ---
    llm = create_llm_client()
    log.info("LLM model: %s", args.model)

    # --- Build flat task list ---
    if args.categories:
        allowed_cats = set(c.strip() for c in args.categories.split(","))
        all_tasks = [(cat, task) for cat, tasks in TASKS.items() for task in tasks if cat in allowed_cats]
    else:
        all_tasks = [(cat, task) for cat, tasks in TASKS.items() for task in tasks]

    # --- Main RL loop ---
    # --- Experience replay buffer ---
    replay = ExperienceReplay(max_size=50)
    replay_path = Path(__file__).parent / "skill_bank" / "replay_buffer.json"
    replay.load(replay_path)
    log.info("Experience replay: %d stored trajectories", len(replay.buffer))

    log.info("Starting SkillRL loop — evolve every %d episodes, output: %s",
             args.evolve_every, output_dir)
    log.info("")

    episode_num = 0
    generation = 0
    generation_episodes: list[EpisodeResult] = []
    generation_repairs: list[dict] = []

    try:
        while True:
            # Pick a task (shuffle through all, then repeat)
            cat, task = all_tasks[episode_num % len(all_tasks)]

            # Add variety — occasionally rephrase
            if random.random() < 0.3:
                task = task.rstrip(".") + ". Be thorough and check related data."

            episode_num += 1
            log.info("[Episode %d] [%s] %s", episode_num, cat, task[:80])

            # [Feature 1] Inject experience replay examples into system prompt
            replay_examples = replay.get_examples(cat, n=2)

            # Run episode
            result = run_episode(llm, odoo, args.model, task, cat, skill_bank, args.max_turns,
                                 replay_examples=replay_examples)

            log.info("  → reward=%.2f  turns=%d  tools=%d  errors=%d  %.1fs",
                     result.reward, result.turns, len(result.tool_calls),
                     result.odoo_errors, result.duration_s)

            # Save episode
            save_episode(result, output_dir)
            generation_episodes.append(result)

            # [Feature 1] Add successful episodes to replay buffer
            replay.add(result)

            # [Feature 2] Repair failed trajectories
            if result.reward < 0.3 and result.odoo_errors > 0:
                log.info("  Repairing failed trajectory...")
                repair = repair_trajectory(llm, args.model, result)
                if repair:
                    generation_repairs.append(repair)
                    log.info("  Repair generated for: %s", result.task[:50])

            # Evolve skill bank
            if len(generation_episodes) >= args.evolve_every:
                generation += 1
                print_stats(generation_episodes, generation)

                log.info("Evolving skill bank (generation %d)...", generation)
                skill_bank = evolve_skills(
                    llm, args.model, skill_bank, generation_episodes
                )
                save_skill_bank(skill_bank, output_dir)

                gs = len(skill_bank.get("general_skills", []))
                ts_data = skill_bank.get("task_specific_skills", {})
                ts = sum(len(v) for v in ts_data.values()) if isinstance(ts_data, dict) else len(ts_data)
                cm = len(skill_bank.get("common_mistakes", []))
                log.info("  Skill bank updated: %d general, %d task-specific, %d mistakes", gs, ts, cm)

                # [Feature 3] Self-edit SKILL.md references based on experience
                log.info("  Self-editing skill references...")
                patches = self_edit_skills(
                    llm, args.model, generation_episodes, generation_repairs, SKILLS_DIR
                )
                if patches:
                    log.info("  Patched %d reference files: %s", len(patches), patches)

                # Optional: sync graduated skills into the published registry
                if args.sync_after_evolve:
                    sync_to_registry(args.odoo_skills_repo)

                # Save replay buffer
                replay.save(replay_path)
                log.info("  Replay buffer: %d trajectories saved", len(replay.buffer))

                # Save repairs log
                if generation_repairs:
                    repairs_file = output_dir / "repairs.jsonl"
                    with open(repairs_file, "a") as f:
                        for r in generation_repairs:
                            f.write(json.dumps(r) + "\n")

                generation_episodes = []
                generation_repairs = []

            # Check exit condition
            if args.episodes > 0 and episode_num >= args.episodes:
                log.info("Reached %d episodes, stopping.", args.episodes)
                break

            # Cooldown between episodes
            time.sleep(args.cooldown)

    except KeyboardInterrupt:
        log.info("\nInterrupted. Saving final state...")
    finally:
        # Save whatever we have
        if generation_episodes:
            generation += 1
            print_stats(generation_episodes, generation)
            log.info("Final evolution pass...")
            skill_bank = evolve_skills(llm, args.model, skill_bank, generation_episodes)
            save_skill_bank(skill_bank, output_dir)

            # Final self-edit
            patches = self_edit_skills(
                llm, args.model, generation_episodes, generation_repairs, SKILLS_DIR
            )
            if patches:
                log.info("  Final patches: %s", patches)

        replay.save(replay_path)
        odoo.close()
        log.info("Done. Output: %s", output_dir)
        log.info("Final skill bank: %s", output_dir / "odoo_skills.json")
        log.info("Replay buffer: %d trajectories", len(replay.buffer))


if __name__ == "__main__":
    main()
