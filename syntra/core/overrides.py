"""User overrides.

You know things the AA catalog doesn't. Examples:
- "DeepSeek V4 via NVIDIA hallucinates a lot. Penalize 0.5."
- "OpenRouter routes for Opus run out of credits often. Soft-demote unless quality_bias > 0.9."
- "Never use route X for executor work."
- "Add specialty 'verified-coding' to model Y."

This file is editable text + a few CLI commands. The router consults it when
scoring picks. Empirical knowledge beats benchmark scores.

File: ~/.config/syntra/overrides.json (or $XDG_CONFIG_HOME/syntra/overrides.json)

Schema:
{
  "blacklists": [
    {"provider": "nvidia", "model_id": "deepseek-ai/deepseek-v4-pro", "reason": "hallucinates"},
    {"model_id": "google/gemini-3-flash-preview", "reason": "rate limit pain"}
  ],
  "penalties": [
    {"provider": "openrouter", "model_id": "anthropic/claude-opus-4.7", "penalty": 0.3, "reason": "credit exhaustion"},
    {"model_id": "deepseek-ai/deepseek-v4-pro", "penalty": 0.4, "reason": "unreliable output"}
  ],
  "extra_specialties": [
    {"model_id": "deepseek-ai/deepseek-v4-pro", "add": ["unreliable"], "remove": []}
  ],
  "role_overrides": [
    {"model_id": "deepseek-ai/deepseek-v4-pro", "remove_roles": ["planner"]}
  ]
}

A blacklist with no `provider` field matches that model on any provider.
A penalty is a score multiplier (1.0 = no change, 0.5 = half score, 1.2 = boost).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Blacklist:
    model_id: str
    provider: str | None = None      # None = matches any provider
    reason: str = ""


@dataclass
class Penalty:
    model_id: str
    provider: str | None = None
    penalty: float = 1.0              # multiplier >=0; 1.0 = none; >1.0 = boost
    reason: str = ""


@dataclass
class SpecialtyEdit:
    model_id: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass
class RoleEdit:
    model_id: str
    add_roles: tuple[str, ...] = ()
    remove_roles: tuple[str, ...] = ()


@dataclass
class RolePin:
    """A hard assignment: use this exact model for this role.

    Set via `/models` (TUI) or `syntra pin <role> <model_id>`. The router
    returns the pinned model directly for that role, bypassing scoring (as
    long as the model still exists in the catalog and resolves to a provider).
    """
    role: str          # "planner" | "executor" | "reviewer"
    model_id: str


@dataclass
class Overrides:
    blacklists: list[Blacklist] = field(default_factory=list)
    penalties: list[Penalty] = field(default_factory=list)
    specialty_edits: list[SpecialtyEdit] = field(default_factory=list)
    role_edits: list[RoleEdit] = field(default_factory=list)
    role_pins: list[RolePin] = field(default_factory=list)
    source_path: str = ""

    # --------------------------------------------------------------- loader

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Overrides":
        resolved = cls._resolve_path(path)
        if not resolved.exists():
            return cls(source_path=str(resolved))
        try:
            raw = json.loads(resolved.read_text())
        except (json.JSONDecodeError, OSError):
            # Overrides are optional — a malformed file degrades to NO overrides
            # rather than crashing the run (routing still works without pins/penalties).
            return cls(source_path=str(resolved))
        # F19: each row's `model_id` is guarded with `row.get("model_id")` skips (matching
        # role_pins below) so a malformed row degrades to "skip that entry" instead of raising
        # an unguarded KeyError — honoring the "malformed file → NO overrides" contract.
        return cls(
            blacklists=[
                Blacklist(
                    model_id=row["model_id"],
                    provider=row.get("provider"),
                    reason=row.get("reason", ""),
                )
                for row in raw.get("blacklists", [])
                if row.get("model_id")
            ],
            penalties=[
                Penalty(
                    model_id=row["model_id"],
                    provider=row.get("provider"),
                    penalty=float(row.get("penalty", 1.0)),
                    reason=row.get("reason", ""),
                )
                for row in raw.get("penalties", [])
                if row.get("model_id")
            ],
            specialty_edits=[
                SpecialtyEdit(
                    model_id=row["model_id"],
                    add=tuple(row.get("add", [])),
                    remove=tuple(row.get("remove", [])),
                )
                for row in raw.get("extra_specialties", [])
                if row.get("model_id")
            ],
            role_edits=[
                RoleEdit(
                    model_id=row["model_id"],
                    add_roles=tuple(row.get("add_roles", [])),
                    remove_roles=tuple(row.get("remove_roles", [])),
                )
                for row in raw.get("role_overrides", [])
                if row.get("model_id")
            ],
            role_pins=[
                RolePin(role=row["role"].lower(), model_id=row["model_id"])
                for row in raw.get("role_pins", [])
                if row.get("role") and row.get("model_id")
            ],
            source_path=str(resolved),
        )

    @staticmethod
    def _resolve_path(explicit: Path | str | None) -> Path:
        if explicit:
            return Path(explicit).expanduser()
        env = os.environ.get("SYNTRA_OVERRIDES_FILE")
        if env:
            return Path(env).expanduser()
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        return base / "syntra" / "overrides.json"

    # --------------------------------------------------------------- queries

    def is_blacklisted(self, model_id: str, provider: str | None = None) -> tuple[bool, str]:
        for b in self.blacklists:
            if b.model_id != model_id:
                continue
            if b.provider is None or b.provider == provider:
                return True, b.reason
        return False, ""

    def penalty_for(self, model_id: str, provider: str | None = None) -> tuple[float, str]:
        """Return (multiplier, reason). 1.0 = no change, <1 = penalty, >1 = boost.

        Provider-specific override beats generic. When multiple generic penalties
        exist, the most impactful wins (lowest for penalties, highest for boosts)."""
        match_any: float | None = None
        match_specific: float | None = None
        reason_any = ""
        reason_specific = ""
        for p in self.penalties:
            if p.model_id != model_id:
                continue
            mult = max(0.0, float(p.penalty))
            if p.provider == provider:
                match_specific = mult
                reason_specific = p.reason or reason_specific
            elif p.provider is None:
                # Most impactful: for penalties use min, for boosts use max.
                # If we have both a penalty and a boost, the penalty wins (safer).
                if match_any is None:
                    match_any = mult
                    reason_any = p.reason or reason_any
                elif mult < 1.0 or match_any < 1.0:
                    match_any = min(match_any, mult)
                    reason_any = p.reason or reason_any
                else:
                    match_any = max(match_any, mult)
                    reason_any = p.reason or reason_any
        if match_specific is not None:
            return match_specific, reason_specific
        if match_any is not None:
            return match_any, reason_any
        return 1.0, ""

    def extra_specialties(self, model_id: str) -> tuple[set[str], set[str]]:
        add: set[str] = set()
        remove: set[str] = set()
        for s in self.specialty_edits:
            if s.model_id == model_id:
                add.update(s.add)
                remove.update(s.remove)
        return add, remove

    def role_edits_for(self, model_id: str) -> tuple[set[str], set[str]]:
        add: set[str] = set()
        remove: set[str] = set()
        for r in self.role_edits:
            if r.model_id == model_id:
                add.update(r.add_roles)
                remove.update(r.remove_roles)
        return add, remove

    def pinned_model_for(self, role: str) -> str | None:
        """The model_id hard-pinned to this role, if any. Last pin wins."""
        role = role.lower()
        chosen = None
        for p in self.role_pins:
            if p.role == role:
                chosen = p.model_id
        return chosen

    # ---------------------------------------------------------------- mutators

    def add_blacklist(self, model_id: str, provider: str | None, reason: str = "") -> None:
        self.blacklists.append(Blacklist(model_id=model_id, provider=provider, reason=reason))

    def add_penalty(self, model_id: str, provider: str | None, penalty: float, reason: str = "") -> None:
        self.penalties.append(Penalty(model_id=model_id, provider=provider, penalty=penalty, reason=reason))

    def pin_role(self, role: str, model_id: str) -> None:
        """Hard-assign `model_id` to `role` (planner/executor/reviewer).

        Replaces any existing pin for that role (one model per role)."""
        role = role.lower()
        self.role_pins = [p for p in self.role_pins if p.role != role]
        self.role_pins.append(RolePin(role=role, model_id=model_id))

    def unpin_role(self, role: str) -> None:
        role = role.lower()
        self.role_pins = [p for p in self.role_pins if p.role != role]

    def save(self) -> None:
        path = Path(self.source_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "blacklists": [b.__dict__ for b in self.blacklists],
            "penalties": [p.__dict__ for p in self.penalties],
            "extra_specialties": [
                {"model_id": s.model_id, "add": list(s.add), "remove": list(s.remove)}
                for s in self.specialty_edits
            ],
            "role_overrides": [
                {"model_id": r.model_id, "add_roles": list(r.add_roles), "remove_roles": list(r.remove_roles)}
                for r in self.role_edits
            ],
            "role_pins": [
                {"role": p.role, "model_id": p.model_id} for p in self.role_pins
            ],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(path)
