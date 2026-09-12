#!/usr/bin/env python3
"""Apply one API-generated PostgreSQL configuration temporarily.

This module has no tuning values of its own.  It accepts the exact mapping
parsed from an API response, looks up each GUC in the live ``pg_settings``
view, applies every supported field according to its PostgreSQL context, and
restores the previous ALTER SYSTEM state when the context exits.
"""

from __future__ import annotations

import json
import copy
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
# ``*`` is a valid PostgreSQL value for settings such as listen_addresses;
# it is still passed as a SQL literal, never interpreted by a shell.
# Empty is a valid value for string GUCs such as bonjour_name.  Keep the
# whitelist (the value is still emitted as one SQL literal) while allowing
# that PostgreSQL-supported case.
_VALUE = re.compile(r"^[A-Za-z0-9_./:+%\-* ]*$")
_RESTART_TIMEOUT_SECONDS = 300


def _literal(value: Any) -> str:
    text = str(value)
    if not _VALUE.fullmatch(text):
        raise ValueError("unsafe PostgreSQL setting value: {!r}".format(text))
    return "'{}'".format(text.replace("'", "''"))


def _ident(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("unsafe PostgreSQL setting name: {!r}".format(value))
    return '"{}"'.format(value)


def _command(db_args: Any, sql: str, application_name: str) -> List[str]:
    command = [
        "runuser",
        "-u",
        str(db_args.run_as),
        "--",
        "env",
        "PGAPPNAME={}".format(application_name),
        "psql",
        "-X",
        "-A",
        "-t",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        str(db_args.host),
        "-U",
        str(db_args.db_user),
        "-d",
        str(db_args.db),
    ]
    port = getattr(db_args, "port", None)
    if port is not None:
        command.extend(["-p", str(port)])
    command.extend(["-c", sql])
    return command


def _psql(db_args: Any, sql: str, label: str, timeout: float = 60.0) -> str:
    result = subprocess.run(
        _command(db_args, sql, "perf-anomaly-demo-config-{}".format(label)),
        cwd="/",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )
    return result.stdout.strip()


def _json_rows(db_args: Any, sql: str, label: str) -> List[Dict[str, Any]]:
    raw = _psql(db_args, sql, label)
    rows: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def settings_details(db_args: Any, names: Iterable[str], label: str) -> Dict[str, Dict[str, Any]]:
    names = sorted(set(str(name) for name in names))
    if not names:
        return {}
    in_list = ", ".join(_literal(name) for name in names)
    sql = (
        "SELECT json_build_object("
        "'name', name, 'setting', setting, 'unit', unit, 'context', context, 'vartype', vartype, "
        "'sourcefile', sourcefile, 'pending_restart', pending_restart, "
        "'boot_val', boot_val, 'reset_val', reset_val, 'enumvals', enumvals"
        ")::text FROM pg_settings WHERE name IN ({}) ORDER BY name"
    ).format(in_list)
    return {str(row["name"]): row for row in _json_rows(db_args, sql, label)}


def _file_settings_snapshot(db_args: Any, names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    names = sorted(set(str(name) for name in names))
    if not names:
        return {}
    in_list = ", ".join(_literal(name) for name in names)
    sql = (
        "SELECT json_build_object('name', name, 'setting', setting, 'applied', applied, "
        "'error', error, 'sourcefile', sourcefile, 'sourceline', sourceline, 'seqno', seqno)::text "
        "FROM pg_file_settings WHERE name IN ({}) ORDER BY seqno"
    ).format(in_list)
    result: Dict[str, Dict[str, Any]] = {}
    for row in _json_rows(db_args, sql, "file-settings-before"):
        name = str(row.get("name"))
        # PostgreSQL reads the last applicable entry for a GUC.  Retain the
        # last row, while also recording all rows in the audit object below.
        result[name] = row
    return result


def _reload(db_args: Any) -> str:
    return _psql(db_args, "SELECT pg_reload_conf()", "reload")


def connection_args_for_configuration(db_args: Any, normalized: Dict[str, Any]) -> Any:
    """Clone DB connection arguments for the endpoint in an applied GUC set."""

    connection_args = copy.copy(db_args)
    if "port" in normalized:
        connection_args.port = int(normalized["port"])
    if "unix_socket_directories" in normalized:
        sockets = str(normalized["unix_socket_directories"]).strip()
        if sockets:
            # PostgreSQL accepts a comma-separated list.  The validator uses
            # the first local socket, which is deterministic and sufficient
            # for this single-instance demo.
            connection_args.host = sockets.split(",", 1)[0].strip()
    return connection_args


def _service_name(version: str, cluster: str) -> str:
    return "postgresql@{}-{}.service".format(version, cluster)


def _stop_cluster(db_args: Any) -> Dict[str, Any]:
    """Stop both the systemd view and any directly started cluster process."""

    version = str(getattr(db_args, "pg_version", "12"))
    cluster = str(getattr(db_args, "pg_cluster", "main"))
    unit = _service_name(version, cluster)
    managed = subprocess.run(
        ["systemctl", "stop", unit],
        cwd="/",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=_RESTART_TIMEOUT_SECONDS,
        check=False,
    )
    direct = subprocess.run(
        ["pg_ctlcluster", "--skip-systemctl-redirect", version, cluster, "stop", "-m", "fast"],
        cwd="/",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=_RESTART_TIMEOUT_SECONDS,
        check=False,
    )
    # pg_ctlcluster returns 2 when systemd already stopped the cluster.  That
    # is a successful outcome for this cleanup helper.
    if direct.returncode not in (0, 2):
        raise RuntimeError(
            "pg_ctlcluster stop failed ({}): {}".format(
                direct.returncode, direct.stdout[-4000:]
            )
        )
    return {
        "systemctl_returncode": managed.returncode,
        "systemctl_output": managed.stdout[-2000:],
        "pg_ctlcluster_returncode": direct.returncode,
        "pg_ctlcluster_output": direct.stdout[-2000:],
    }


def _restart(db_args: Any, direct_start: bool = False) -> Dict[str, Any]:
    version = str(getattr(db_args, "pg_version", "12"))
    cluster = str(getattr(db_args, "pg_cluster", "main"))
    unit = _service_name(version, cluster)
    stopped = _stop_cluster(db_args)
    if direct_start:
        # The API may change external_pid_file.  Starting directly while the
        # unit is stopped avoids systemd waiting for its original PID path.
        command = ["pg_ctlcluster", "--skip-systemctl-redirect", version, cluster, "start"]
    else:
        # After restoring the original file-backed settings, hand ownership
        # back to the normal systemd unit.
        command = ["systemctl", "start", unit]
    completed = subprocess.run(
        command,
        cwd="/",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=_RESTART_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "PostgreSQL restart failed ({}): {}".format(
                completed.returncode, completed.stdout[-4000:]
            )
        )
    return {
        "stop": stopped,
        "start_command": command,
        "returncode": completed.returncode,
        "output": completed.stdout[-4000:],
    }


def _alter_system_set(db_args: Any, name: str, value: Any) -> None:
    _psql(
        db_args,
        "ALTER SYSTEM SET {} = {}".format(_ident(name), _literal(value)),
        "set-{}".format(name),
    )


def _alter_system_reset(db_args: Any, name: str) -> None:
    _psql(db_args, "ALTER SYSTEM RESET {}".format(_ident(name)), "reset-{}".format(name))


def _restore_global_state(
    db_args: Any,
    global_names: List[str],
    file_snapshot: Dict[str, Dict[str, Any]],
    restart_required: bool,
) -> Dict[str, Any]:
    restored: Dict[str, Any] = {"entries": {}, "restart": None, "errors": []}
    for name in global_names:
        previous = file_snapshot.get(name)
        try:
            # The snapshot is the final pg_file_settings row.  Restore an
            # ALTER SYSTEM value only when the previous effective entry came
            # from postgresql.auto.conf; otherwise reset the temporary entry
            # and let postgresql.conf/defaults take effect again.
            sourcefile = str(previous.get("sourcefile")) if previous else ""
            if previous and previous.get("setting") is not None and sourcefile.endswith("postgresql.auto.conf"):
                _alter_system_set(db_args, name, previous["setting"])
                restored["entries"][name] = {
                    "action": "restore_alter_system_value",
                    "value": previous["setting"],
                    "sourcefile": sourcefile,
                }
            else:
                _alter_system_reset(db_args, name)
                restored["entries"][name] = {"action": "reset_temporary_alter_system"}
        except Exception as exc:  # restore all remaining names before failing
            restored["errors"].append(
                {"name": name, "error": "{}: {}".format(type(exc).__name__, exc)}
            )
    if restart_required:
        try:
            restored["restart"] = _restart(db_args, direct_start=False)
        except Exception as exc:
            restored["errors"].append(
                {"phase": "restart_after_restore", "error": "{}: {}".format(type(exc).__name__, exc)}
            )
    else:
        try:
            restored["reload"] = _reload(db_args)
        except Exception as exc:
            restored["errors"].append(
                {"phase": "reload_after_restore", "error": "{}: {}".format(type(exc).__name__, exc)}
            )
    return restored


class TemporaryPostgresConfiguration:
    """Context manager for an exact API response configuration."""

    def __init__(self, db_args: Any, config: Dict[str, Any], label: str = "api") -> None:
        self.db_args = db_args
        self.config = {str(key): value for key, value in config.items()}
        self.label = label
        self.state: Dict[str, Any] = {
            "requested_configuration": self.config,
            "status": "not_started",
            "session_configuration": {},
            "connection_configuration": {},
            "global_configuration": {},
            "unsupported": {},
            "pre_settings": {},
            "post_settings": {},
            "restore": None,
        }
        self._global_names: List[str] = []
        self._file_snapshot: Dict[str, Dict[str, Any]] = {}
        self._restart_required = False
        self._connection_args = copy.copy(db_args)

    @staticmethod
    def _normalise_value(name: str, value: Any, detail: Dict[str, Any]) -> Any:
        """Convert only parser representation, never change the numeric value."""

        vartype = str(detail.get("vartype") or "")
        if (
            vartype in {"string", "enum"}
            and isinstance(value, str)
            and len(value) >= 2
            and value[0] == "'"
            and value[-1] == "'"
        ):
            # Some model responses serialize a PostgreSQL string literal in
            # the configuration field (for example, '\'stderr\'').  Strip
            # only this single outer pair before passing the actual value to
            # ALTER SYSTEM; the raw API value remains preserved in the
            # artifact for audit.
            value = value[1:-1]
        if vartype in {"integer", "bigint", "oid"}:
            if isinstance(value, bool):
                raise ValueError("boolean is not a valid integer value for {}".format(name))
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("{} is not numeric for integer GUC {}".format(value, name)) from exc
            if not number.is_integer():
                raise ValueError("{} is not an integer value for {}".format(value, name))
            return int(number)
        if vartype in {"real", "double"}:
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("{} is not numeric for real GUC {}".format(value, name)) from exc
        if vartype == "bool":
            text = str(value).strip().lower()
            if text in {"on", "true", "1"}:
                return "on"
            if text in {"off", "false", "0"}:
                return "off"
            raise ValueError("{} is not a boolean value for {}".format(value, name))
        return value

    def __enter__(self) -> Dict[str, Any]:
        if not self.config:
            raise ValueError("API configuration is empty; no preset fallback is allowed")
        details = settings_details(self.db_args, self.config, self.label + "-details")
        self.state["pre_settings"] = details
        missing = sorted(set(self.config) - set(details))
        if missing:
            raise ValueError("API configuration contains unknown live GUCs: {}".format(missing))

        normalized = {
            name: self._normalise_value(name, value, details[name])
            for name, value in self.config.items()
        }
        self.state["normalized_configuration"] = normalized
        self._connection_args = connection_args_for_configuration(self.db_args, normalized)
        self.state["connection_endpoint"] = {
            "host": getattr(self._connection_args, "host", None),
            "port": getattr(self._connection_args, "port", None),
        }

        for name, value in normalized.items():
            context = str(details[name].get("context") or "")
            if context in {"user", "superuser"}:
                self.state["session_configuration"][name] = value
            elif context in {"superuser-backend", "backend"}:
                self.state["connection_configuration"][name] = value
            elif context in {"sighup", "postmaster"}:
                self.state["global_configuration"][name] = {
                    "value": value,
                    "context": context,
                }
                self._global_names.append(name)
                if context == "postmaster":
                    self._restart_required = True
            else:
                self.state["unsupported"][name] = {
                    "value": value,
                    "context": context,
                }
        if self.state["unsupported"]:
            raise ValueError(
                "API returned GUCs that this controlled applier cannot apply: {}".format(
                    sorted(self.state["unsupported"])
                )
            )

        self._file_snapshot = _file_settings_snapshot(self.db_args, self._global_names)
        try:
            for name, item in self.state["global_configuration"].items():
                _alter_system_set(self.db_args, name, item["value"])
            if self._restart_required:
                self.state["activation"] = _restart(self.db_args, direct_start=True)
            elif self._global_names:
                self.state["activation"] = _reload(self.db_args)
            self.state["post_settings"] = settings_details(
                self._connection_args, self.config, self.label + "-applied-details"
            )
            self.state["status"] = "applied"
            return self.state
        except Exception:
            # A failed startup/reload must not leave a partial API config
            # behind.  Best-effort restoration is included in the raised error
            # object's state by the caller's surrounding artifact.
            self.state["restore"] = _restore_global_state(
                self._connection_args, self._global_names, self._file_snapshot, self._restart_required
            )
            self.state["status"] = "apply_failed"
            raise

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if self._global_names:
            self.state["restore"] = _restore_global_state(
                self._connection_args, self._global_names, self._file_snapshot, self._restart_required
            )
            if self.state["restore"].get("errors"):
                self.state["status"] = "restore_failed"
                if exc_value is None:
                    raise RuntimeError(
                        "temporary API configuration restore failed: {}".format(
                            self.state["restore"]["errors"]
                        )
                    )
            else:
                self.state["status"] = "applied_and_restored"
        else:
            self.state["status"] = "session_only_applied_and_ended"
        return False
