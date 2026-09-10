#!/usr/bin/env python3
"""Database/version profile resolution for the SysInsight demo.

The profile only selects data files and descriptive metadata.  The tuning
algorithm, prompt assembly, LLM request, response parser and candidate
filter remain the original SysInsight source methods.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parent
PROFILE_ROOT = ROOT / "db_profiles"


def _normalise_dbms(value: str) -> str:
    value = value.strip().lower().replace("-", "_")
    aliases = {
        "mysql": "mysql",
        "mariadb": "mysql",
        "postgres": "postgresql",
        "postgresql": "postgresql",
        "pgsql": "postgresql",
    }
    try:
        return aliases[value]
    except KeyError as exc:
        raise ValueError("unsupported database: {} (use mysql or postgresql)".format(value)) from exc


def _normalise_version(dbms: str, version: Optional[str]) -> str:
    if version is None or not str(version).strip():
        return "8.0.36" if dbms == "mysql" else "12"
    value = str(version).strip().lower()
    if dbms == "mysql" and value in {"8", "8.0", "8.0.x"}:
        return "8.0.36"
    if dbms == "postgresql":
        if value.startswith("postgresql-"):
            value = value[len("postgresql-"):]
        if value.startswith("postgres-"):
            value = value[len("postgres-"):]
        if value in {"12.x", "12.0"}:
            return "12"
        if value in {"13.x", "13.0"}:
            return "13"
        if value in {"14.x", "14.0"}:
            return "14"
    return value


@dataclass(frozen=True)
class DatabaseProfile:
    """A resolved, immutable database/version profile."""

    dbms: str
    version: str
    root: Path
    raw: Dict[str, Any]

    @property
    def name(self) -> str:
        return "{}/{}".format(self.dbms, self.version)

    @property
    def display_name(self) -> str:
        return str(self.raw.get("display_name", self.dbms))

    @property
    def kernel_info(self) -> str:
        return str(self.raw.get("kernel_info", "{} {}".format(self.display_name, self.version)))

    @property
    def hardware_info(self) -> str:
        return str(self.raw.get("hardware_info", "Runtime host resources are supplied by the collector."))

    @property
    def unit_note(self) -> str:
        return str(self.raw.get("unit_note", "Use the database's native configuration units."))

    @property
    def knowledge_source(self) -> Dict[str, Any]:
        value = self.raw.get("knowledge_source", {})
        return value if isinstance(value, dict) else {}

    def path(self, key: str, required: bool = True) -> Optional[Path]:
        files = self.raw.get("files", {})
        value = files.get(key) if isinstance(files, dict) else None
        if value in (None, ""):
            if required:
                raise FileNotFoundError("profile {} has no file entry '{}'".format(self.name, key))
            return None
        path = Path(value)
        if not path.is_absolute():
            path = (self.root / path).resolve()
        if required and not path.exists():
            raise FileNotFoundError("profile {} file '{}' does not exist: {}".format(self.name, key, path))
        return path

    def _read_json(self, key: str) -> Any:
        path = self.path(key)
        assert path is not None
        return json.loads(path.read_text(encoding="utf-8"))

    def constraints(self) -> Dict[str, Any]:
        value = self._read_json("constraints")
        source_key = self.raw.get("constraints_key")
        if source_key:
            value = value[source_key]
        if not isinstance(value, dict):
            raise ValueError("profile {} constraints must be an object".format(self.name))
        return value

    def defaults(self) -> Dict[str, Any]:
        value = self._read_json("defaults")
        if isinstance(value, list):
            index = int(self.raw.get("defaults_index", 0))
            value = value[index]
        if not isinstance(value, dict):
            raise ValueError("profile {} defaults must be an object".format(self.name))
        return value

    def initial_config(self) -> Dict[str, Any]:
        value = self._read_json("initial_config")
        if isinstance(value, list):
            index = int(self.raw.get("initial_config_index", 0))
            value = value[index]
        if not isinstance(value, dict):
            raise ValueError("profile {} initial config must be an object".format(self.name))
        return value

    def prompt_template(self) -> Optional[str]:
        path = self.path("prompt_template", required=False)
        if path is None:
            return None
        return path.read_text(encoding="utf-8")

    def task_fragment(self) -> Dict[str, Any]:
        """Return the path-bearing fragment consumed by original SysInsight code."""

        files = self.raw.get("files", {})
        result: Dict[str, Any] = {
            "dbms": self.dbms,
            "db_version": self.version,
            "display_name": self.display_name,
            "dbms_info": self.kernel_info,
            "hardware_info": self.hardware_info,
            "unit_note": self.unit_note,
            "profile_name": self.name,
            "profile_root": str(self.root),
            "profile_files": {},
        }
        for key in files:
            path = self.path(key, required=False)
            if path is not None:
                result["profile_files"][key] = str(path)

        # These names are deliberately explicit: they are the optional
        # profile hooks read by the parameter library's path-aware adapter.
        aliases = {
            "association": "association_file",
            "manual": "manual_file",
            "rule": "rule_file",
            "target_knobs": "target_knob_file",
            "code_folder": "code_folder",
        }
        for source_key, task_key in aliases.items():
            path = self.path(source_key, required=False)
            if path is not None:
                result[task_key] = str(path)
        return result


def _profile_path(dbms: str, version: str) -> Path:
    return PROFILE_ROOT / dbms / version / "profile.json"


def available_profiles() -> List[str]:
    result: List[str] = []
    for path in sorted(PROFILE_ROOT.glob("*/**/profile.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result.append("{}/{}".format(data["dbms"], data["version"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
    return result


def resolve_profile(dbms: str = "postgresql", version: Optional[str] = None) -> DatabaseProfile:
    normalised_dbms = _normalise_dbms(dbms)
    normalised_version = _normalise_version(normalised_dbms, version)
    path = _profile_path(normalised_dbms, normalised_version)
    if not path.exists():
        raise FileNotFoundError(
            "profile {}/{} not found; available profiles: {}".format(
                normalised_dbms, normalised_version, ", ".join(available_profiles())
            )
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    return DatabaseProfile(normalised_dbms, normalised_version, path.parent, raw)


def profile_summary(profile: DatabaseProfile) -> Dict[str, Any]:
    return {
        "name": profile.name,
        "display_name": profile.display_name,
        "kernel_info": profile.kernel_info,
        "hardware_info": profile.hardware_info,
        "unit_note": profile.unit_note,
        "files": {
            key: str(profile.path(key, required=False))
            for key in (profile.raw.get("files", {}) or {})
        },
        "knowledge_source": profile.knowledge_source,
    }
