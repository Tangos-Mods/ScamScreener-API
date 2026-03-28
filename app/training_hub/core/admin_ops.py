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
            LEFT JOIN uploads up ON up.user_id = u.id
            GROUP BY u.id
            ORDER BY u.created_at ASC
            """
        ).fetchall()


def _admin_user_count(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()
    return int(count[0]) if count is not None else 0


def _admin_cases(database_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT
                c.id,
                c.case_id,
                c.updated_at,
                c.status,
                c.label,
                c.outcome,
                u.username AS created_by
            FROM training_cases c
            JOIN users u ON u.id = c.created_by_user_id
            ORDER BY c.updated_at DESC
            LIMIT 200
            """
        ).fetchall()


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
                u.username AS created_by,
                up.original_file_name AS source_file_name
            FROM training_cases c
            JOIN users u ON u.id = c.created_by_user_id
            LEFT JOIN uploads up ON up.id = c.source_upload_id
            WHERE c.id = ?
            """,
            (case_db_id,),
        ).fetchone()

    if row is None:
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

    messages = _normalize_case_messages(case_data.get("messages", []))
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
            "signal_message_indices": _normalize_int_list(context_stage.get("signalMessageIndices", [])),
            "context_message_indices": _normalize_int_list(context_stage.get("contextMessageIndices", [])),
            "excluded_message_indices": _normalize_int_list(context_stage.get("excludedMessageIndices", [])),
            "target_signal_tag_ids": _normalize_str_list(context_stage.get("targetSignalTagIds", [])),
        },
    }


def _normalize_case_messages(raw_messages: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_messages, list):
        return []

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

        messages.append(
            {
                "index": str(index_value),
                "role": str(role).strip() or "message",
                "text": str(text_value).strip(),
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


def _create_audit_log(
    database_path: Path,
    actor_user_id: int,
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
                actor_user_id,
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

