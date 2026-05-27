from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tarfile
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..infra import db as sqlite3
from ..config.settings import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, TRAINING_FORMAT, TRAINING_SCHEMA_VERSION, TrainingHubSettings

from .common import _now_utc_iso


_ADMIN_CASE_DISPLAY_RE = re.compile(
    r"^(?:case[._-])?(?P<prefix>[0-9a-fA-F]{8})[0-9a-fA-F-]*?(?:[._-]review-(?P<review>\d+))$"
)
_SCAMSCREENER_USER_AGENT_RE = re.compile(r"^ScamScreener/(?P<mod>[^+\s]+)\+(?P<mc>[^\s]+)$", re.IGNORECASE)
_ADMIN_CASE_FILTER_TOKEN_RE = re.compile(r"(?i)\b(?P<key>status|label):(?P<value>[^\s]+)")
_ADMIN_CASE_SORT_COLUMNS = {
    "id": "c.id",
    "case_id": "LOWER(c.case_id)",
    "status": "LOWER(c.status)",
    "label": "LOWER(COALESCE(c.label, ''))",
    "outcome": "LOWER(COALESCE(c.outcome, ''))",
    "updated": "c.updated_at",
    "mod_version": "LOWER(COALESCE(up.user_agent, ''))",
}
_ADMIN_CASE_SORT_DEFAULT_BY = "updated"
_ADMIN_CASE_SORT_DEFAULT_DIR = "desc"


def _short_admin_case_id(case_id: str) -> str:
    candidate = str(case_id or "").strip()
    match = _ADMIN_CASE_DISPLAY_RE.match(candidate)
    if match is None:
        return candidate
    return f"{match.group('prefix')}-{match.group('review')}"


def _admin_case_mod_version(user_agent: str) -> str:
    normalized = str(user_agent or "").strip()
    match = _SCAMSCREENER_USER_AGENT_RE.match(normalized)
    if match is None:
        return "-"
    return f"{match.group('mod')} {match.group('mc')}"


def _parse_admin_case_filter(raw_filter: str) -> dict[str, str]:
    normalized_filter = str(raw_filter or "").strip()
    result = {
        "raw": normalized_filter,
        "status": "",
        "label": "",
        "text": "",
    }
    if not normalized_filter:
        return result

    extracted_keys: dict[str, str] = {}

    def _strip_token(match: re.Match[str]) -> str:
        extracted_keys[str(match.group("key") or "").strip().lower()] = str(match.group("value") or "").strip().lower()
        return " "

    remaining = _ADMIN_CASE_FILTER_TOKEN_RE.sub(_strip_token, normalized_filter)
    result["status"] = extracted_keys.get("status", "")
    result["label"] = extracted_keys.get("label", "")
    result["text"] = " ".join(remaining.split())
    return result


def _normalize_admin_case_sort(sort_by: str, sort_dir: str) -> tuple[str, str]:
    normalized_sort_by = str(sort_by or "").strip().lower()
    if normalized_sort_by not in _ADMIN_CASE_SORT_COLUMNS:
        normalized_sort_by = _ADMIN_CASE_SORT_DEFAULT_BY

    normalized_sort_dir = str(sort_dir or "").strip().lower()
    if normalized_sort_dir not in {"asc", "desc"}:
        normalized_sort_dir = _ADMIN_CASE_SORT_DEFAULT_DIR
    return normalized_sort_by, normalized_sort_dir


def _admin_users(database_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT
                u.id,
                u.username,
                u.email,
                u.is_admin,
                u.last_login_at,
                COUNT(up.id) AS upload_count,
                COALESCE(SUM(up.case_count), 0) AS case_count
            FROM users u
            LEFT JOIN uploads up ON (
                up.user_id = u.id
                OR EXISTS (
                    SELECT 1
                    FROM client_identities ci
                    WHERE ci.id = up.client_identity_id AND ci.linked_user_id = u.id
                )
            )
            GROUP BY u.id
            ORDER BY u.created_at ASC
            """
        ).fetchall()


def _admin_user_count(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()
    return int(count[0]) if count is not None else 0


def _admin_cases(
    database_path: Path,
    filter_query: str = "",
    sort_by: str = _ADMIN_CASE_SORT_DEFAULT_BY,
    sort_dir: str = _ADMIN_CASE_SORT_DEFAULT_DIR,
) -> list[dict[str, Any]]:
    parsed_filter = _parse_admin_case_filter(filter_query)
    normalized_sort_by, normalized_sort_dir = _normalize_admin_case_sort(sort_by, sort_dir)
    conditions = ["c.content_deleted_at IS NULL"]
    params: list[Any] = []
    if parsed_filter["status"]:
        conditions.append("LOWER(c.status) = ?")
        params.append(parsed_filter["status"])
    if parsed_filter["label"]:
        conditions.append("LOWER(c.label) = ?")
        params.append(parsed_filter["label"])
    if parsed_filter["text"]:
        conditions.append("LOWER(c.payload_json) LIKE ?")
        params.append(f"%{parsed_filter['text'].lower()}%")

    where_clause = " AND ".join(conditions)
    order_clause = f"{_ADMIN_CASE_SORT_COLUMNS[normalized_sort_by]} {normalized_sort_dir.upper()}, c.id {normalized_sort_dir.upper()}"
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT
                c.id,
                c.case_id,
                c.updated_at,
                c.status,
                c.label,
                c.outcome,
                up.user_agent AS source_user_agent
            FROM training_cases c
            LEFT JOIN uploads up ON up.id = c.source_upload_id
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT 200
            """,
            tuple(params),
        ).fetchall()
    cases: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["display_case_id"] = _short_admin_case_id(str(row["case_id"] or ""))
        item["mod_version"] = _admin_case_mod_version(str(row["source_user_agent"] or ""))
        cases.append(item)
    return cases


def _admin_case_detail(database_path: Path, case_db_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                c.id,
                c.case_id,
                c.created_at,
                c.updated_at,
                c.status,
                c.label,
                c.outcome,
                c.tag_ids_json,
                c.payload_json,
                c.content_deleted_at,
                COALESCE(u.username, ci.normalized_client_id, 'unknown') AS created_by,
                up.original_file_name AS source_file_name
            FROM training_cases c
            LEFT JOIN users u ON u.id = c.created_by_user_id
            LEFT JOIN client_identities ci ON ci.id = c.created_by_client_identity_id
            LEFT JOIN uploads up ON up.id = c.source_upload_id
            WHERE c.id = ?
            """,
            (case_db_id,),
        ).fetchone()

    if row is None or str(row["content_deleted_at"] or "").strip():
        return None

    payload_text = str(row["payload_json"] or "{}")
    try:
        payload_obj = json.loads(payload_text)
    except json.JSONDecodeError:
        payload_obj = {}
    if not isinstance(payload_obj, dict):
        payload_obj = {}

    case_data = payload_obj.get("caseData", {})
    if not isinstance(case_data, dict):
        case_data = {}
    observed = payload_obj.get("observedPipeline", {})
    if not isinstance(observed, dict):
        observed = {}
    supervision = payload_obj.get("supervision", {})
    if not isinstance(supervision, dict):
        supervision = {}
    context_stage = supervision.get("contextStage", {})
    if not isinstance(context_stage, dict):
        context_stage = {}

    signal_message_indices = _normalize_int_list(context_stage.get("signalMessageIndices", []))
    context_message_indices = _normalize_int_list(context_stage.get("contextMessageIndices", []))
    excluded_message_indices = _normalize_int_list(context_stage.get("excludedMessageIndices", []))

    messages = _normalize_case_messages(
        case_data.get("messages", []),
        signal_message_indices=signal_message_indices,
        context_message_indices=context_message_indices,
        excluded_message_indices=excluded_message_indices,
    )
    stage_results = _normalize_stage_results(observed.get("stageResults", []))
    signal_tags = _normalize_str_list(case_data.get("caseSignalTagIds", []))

    try:
        db_tags = json.loads(str(row["tag_ids_json"] or "[]"))
    except json.JSONDecodeError:
        db_tags = []
    if isinstance(db_tags, list):
        for value in db_tags:
            normalized = str(value).strip()
            if normalized and normalized not in signal_tags:
                signal_tags.append(normalized)

    return {
        "id": int(row["id"]),
        "case_id": str(row["case_id"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "status": str(row["status"]),
        "label": str(row["label"] or ""),
        "outcome": str(row["outcome"] or ""),
        "created_by": str(row["created_by"]),
        "source_file_name": str(row["source_file_name"] or "-"),
        "format": str(payload_obj.get("format", TRAINING_FORMAT)),
        "schema_version": str(payload_obj.get("schemaVersion", TRAINING_SCHEMA_VERSION)),
        "observed": {
            "outcome_at_capture": str(observed.get("outcomeAtCapture", "")),
            "score_at_capture": str(observed.get("scoreAtCapture", "")),
            "decided_by_stage_id": str(observed.get("decidedByStageId", "")),
        },
        "messages": messages,
        "stage_results": stage_results,
        "signal_tags": signal_tags,
        "context_stage": {
            "target_label": str(context_stage.get("targetLabel", "")),
            "signal_message_indices": signal_message_indices,
            "context_message_indices": context_message_indices,
            "excluded_message_indices": excluded_message_indices,
            "target_signal_tag_ids": _normalize_str_list(context_stage.get("targetSignalTagIds", [])),
        },
    }


def _normalize_case_messages(
    raw_messages: Any,
    *,
    signal_message_indices: list[int] | None = None,
    context_message_indices: list[int] | None = None,
    excluded_message_indices: list[int] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_messages, list):
        return []

    signal_index_set = set(signal_message_indices or [])
    context_index_set = set(context_message_indices or [])
    excluded_index_set = set(excluded_message_indices or [])
    messages: list[dict[str, Any]] = []
    for index, item in enumerate(raw_messages):
        if isinstance(item, dict):
            index_value = item.get("index", item.get("messageIndex", index))
            role = (
                item.get("role")
                or item.get("sender")
                or item.get("author")
                or item.get("username")
                or item.get("source")
                or "message"
            )
            text_value = (
                item.get("text")
                or item.get("content")
                or item.get("message")
                or item.get("raw")
                or item.get("body")
                or ""
            )
        else:
            index_value = index
            role = "message"
            text_value = str(item)

        normalized_index = _coerce_int(index_value)
        messages.append(
            {
                "index": str(index_value),
                "role": str(role).strip() or "message",
                "text": str(text_value).strip(),
                "classifications": _message_classifications(
                    normalized_index,
                    signal_message_indices=signal_index_set,
                    context_message_indices=context_index_set,
                    excluded_message_indices=excluded_index_set,
                ),
            }
        )
    return messages


def _normalize_stage_results(raw_results: Any) -> list[dict[str, str]]:
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in raw_results:
        if isinstance(item, dict):
            stage_id = item.get("stageId", item.get("id", ""))
            outcome = item.get("outcome", item.get("decision", ""))
            score = item.get("score", item.get("scoreAtStage", ""))
            reason = item.get("reason", item.get("note", ""))
            normalized.append(
                {
                    "stage_id": str(stage_id or ""),
                    "outcome": str(outcome or ""),
                    "score": str(score or ""),
                    "reason": str(reason or ""),
                }
            )
        else:
            normalized.append({"stage_id": "", "outcome": "", "score": "", "reason": str(item)})
    return normalized


def _normalize_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _normalize_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        normalized = str(item).strip()
        if normalized:
            out.append(normalized)
    return out


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_classifications(
    message_index: int | None,
    *,
    signal_message_indices: set[int],
    context_message_indices: set[int],
    excluded_message_indices: set[int],
) -> list[dict[str, str]]:
    if message_index is None:
        return []

    classifications: list[dict[str, str]] = []
    if message_index in excluded_message_indices:
        classifications.append({"label": "Excluded", "tone": "excluded"})
    if message_index in context_message_indices:
        classifications.append({"label": "Context", "tone": "context"})
    if message_index in signal_message_indices:
        classifications.append({"label": "Signal", "tone": "signal"})
    return classifications


def _admin_runs(database_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT
                tr.id,
                tr.created_at,
                tr.status,
                tr.upload_count,
                tr.case_count,
                tr.output_log,
                COALESCE(u.username, 'unknown') AS started_by
            FROM training_runs tr
            LEFT JOIN users u ON u.id = tr.started_by_user_id
            ORDER BY tr.created_at DESC
            LIMIT 50
            """
        ).fetchall()


def _delete_training_case(database_path: Path, case_db_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, case_id FROM training_cases WHERE id = ?",
            (case_db_id,),
        ).fetchone()
        if row is None:
            return None

        connection.execute("DELETE FROM training_cases WHERE id = ?", (case_db_id,))
        connection.commit()
        return {"id": int(row["id"]), "case_id": str(row["case_id"])}


def _update_training_case_status(
    database_path: Path,
    case_db_id: int,
    *,
    status: str,
) -> dict[str, Any] | None:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"approved", "rejected"}:
        raise ValueError(f"Unsupported case status: {status}")

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, case_id, status FROM training_cases WHERE id = ?",
            (case_db_id,),
        ).fetchone()
        if row is None:
            return None

        current_status = str(row["status"] or "").strip().lower()
        case_id = str(row["case_id"])
        if current_status == normalized_status:
            return {
                "id": int(row["id"]),
                "case_id": case_id,
                "status": current_status,
                "changed": False,
                "notice": f"Case {case_id} is already {normalized_status}.",
            }

        if current_status != "submitted":
            return {
                "id": int(row["id"]),
                "case_id": case_id,
                "status": current_status,
                "changed": False,
                "error": f"Only submitted cases can be marked as {normalized_status}.",
                "status_code": 409,
            }

        connection.execute(
            "UPDATE training_cases SET status = ?, updated_at = ? WHERE id = ?",
            (normalized_status, _now_utc_iso(), case_db_id),
        )
        connection.commit()
        return {
            "id": int(row["id"]),
            "case_id": case_id,
            "status": normalized_status,
            "changed": True,
            "notice": f"{normalized_status.capitalize()} case {case_id}.",
        }


def _update_training_case_label(
    database_path: Path,
    case_db_id: int,
    *,
    label: str,
) -> dict[str, Any] | None:
    normalized_label = str(label or "").strip().lower()
    if normalized_label not in {"safe", "risk"}:
        raise ValueError(f"Unsupported case label: {label}")

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, case_id, label FROM training_cases WHERE id = ?",
            (case_db_id,),
        ).fetchone()
        if row is None:
            return None

        current_label = str(row["label"] or "").strip().lower()
        case_id = str(row["case_id"])
        if current_label == normalized_label:
            return {
                "id": int(row["id"]),
                "case_id": case_id,
                "label": current_label,
                "changed": False,
                "notice": f"Case {case_id} is already marked {normalized_label}.",
            }

        connection.execute(
            "UPDATE training_cases SET label = ?, updated_at = ? WHERE id = ?",
            (normalized_label, _now_utc_iso(), case_db_id),
        )
        connection.commit()
        return {
            "id": int(row["id"]),
            "case_id": case_id,
            "label": normalized_label,
            "changed": True,
            "notice": f"Marked case {case_id} as {normalized_label}.",
        }


def _delete_rejected_training_case_content(database_path: Path) -> dict[str, Any]:
    deleted_at = _now_utc_iso()
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, case_id
            FROM training_cases
            WHERE status = 'rejected' AND content_deleted_at IS NULL
            ORDER BY updated_at ASC, id ASC
            """
        ).fetchall()
        if not rows:
            return {"deleted_cases": 0, "case_ids": []}

        case_ids = [str(row["case_id"]) for row in rows if str(row["case_id"] or "").strip()]
        case_db_ids = [int(row["id"]) for row in rows]
        for case_db_id in case_db_ids:
            connection.execute(
                """
                UPDATE training_cases
                SET
                    updated_at = ?,
                    created_by_user_id = NULL,
                    created_by_client_identity_id = NULL,
                    source_upload_id = NULL,
                    label = '',
                    outcome = '',
                    tag_ids_json = '[]',
                    payload_json = '{}',
                    content_deleted_at = ?
                WHERE id = ?
                """,
                (deleted_at, deleted_at, case_db_id),
            )
        if case_ids:
            placeholders = ",".join("?" for _ in case_ids)
            connection.execute(f"DELETE FROM upload_cases WHERE case_id IN ({placeholders})", tuple(case_ids))
        connection.commit()
        return {"deleted_cases": len(case_db_ids), "case_ids": case_ids}


def _create_audit_log(
    database_path: Path,
    actor_user_id: int | None,
    action: str,
    target_type: str = "",
    target_id: int | None = None,
    details: str = "",
    source_ip: str = "",
    user_agent: str = "",
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO audit_logs (created_at, actor_user_id, action, target_type, target_id, details, source_ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_iso(),
                int(actor_user_id) if actor_user_id is not None else None,
                (action or "").strip() or "unknown.action",
                (target_type or "").strip(),
                target_id,
                (details or "").strip(),
                (source_ip or "").strip()[:80],
                (user_agent or "").strip()[:300],
            ),
        )
        connection.commit()


def _admin_audit_logs(database_path: Path, limit: int = 200) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT
                a.id,
                a.created_at,
                a.action,
                a.target_type,
                a.target_id,
                a.details,
                a.source_ip,
                a.user_agent,
                COALESCE(u.username, 'unknown') AS actor_username
            FROM audit_logs a
            LEFT JOIN users u ON u.id = a.actor_user_id
            ORDER BY a.created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

