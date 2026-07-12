from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..infra import db as sqlite3
from .common import _now_utc_iso

CONTENT_SCRUB_MATCH_MODES = ("exact", "starts_with", "contains", "ends_with")
CONTENT_SCRUB_MATCH_MODE_LABELS = {
    "exact": "Exact",
    "starts_with": "Starts With",
    "contains": "Contains",
    "ends_with": "Ends With",
}
CONTENT_SCRUB_PATTERN_MAX_LENGTH = 256
CONTENT_SCRUB_MAX_RULES = 200
_CONTENT_SCRUB_PROTECTED_TOP_LEVEL_FIELDS = {"format", "schemaVersion", "caseId"}


@dataclass(frozen=True)
class _CompiledContentScrubRule:
    id: int
    match_mode: str
    pattern_text: str
    use_regex: bool
    regex: re.Pattern[str] | None = None

    def scrub(self, value: str) -> tuple[str, int]:
        if not value:
            return value, 0

        if not self.use_regex:
            return _scrub_plain_text_value(value, self.pattern_text, self.match_mode)

        if self.regex is None:
            raise ValueError("Regex rule was not compiled.")
        return _scrub_regex_value(value, self.regex, self.match_mode)


def _normalize_content_scrub_rule_input(
    *,
    pattern_text: str,
    match_mode: str,
    use_regex: bool,
) -> tuple[str, str, bool]:
    normalized_pattern = str(pattern_text or "")
    if not normalized_pattern.strip():
        raise ValueError("Pattern is required.")
    if len(normalized_pattern) > CONTENT_SCRUB_PATTERN_MAX_LENGTH:
        raise ValueError(f"Pattern must be <= {CONTENT_SCRUB_PATTERN_MAX_LENGTH} characters.")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in normalized_pattern):
        raise ValueError("Pattern contains unsupported control characters.")

    normalized_match_mode = str(match_mode or "").strip().lower()
    if normalized_match_mode not in CONTENT_SCRUB_MATCH_MODES:
        raise ValueError("Unsupported match mode.")

    normalized_use_regex = bool(use_regex)
    if normalized_use_regex:
        try:
            compiled = re.compile(normalized_pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc
        if compiled.search("") is not None:
            raise ValueError("Regex patterns must not match empty strings.")

    return normalized_pattern, normalized_match_mode, normalized_use_regex


def _scrub_plain_text_value(value: str, pattern_text: str, match_mode: str) -> tuple[str, int]:
    if match_mode == "exact":
        return ("", 1) if value == pattern_text else (value, 0)

    if match_mode == "starts_with":
        return (value[len(pattern_text) :], 1) if value.startswith(pattern_text) else (value, 0)

    if match_mode == "ends_with":
        return (value[: -len(pattern_text)], 1) if value.endswith(pattern_text) else (value, 0)

    occurrences = value.count(pattern_text)
    if occurrences == 0:
        return value, 0
    return value.replace(pattern_text, ""), occurrences


def _scrub_regex_value(value: str, regex: re.Pattern[str], match_mode: str) -> tuple[str, int]:
    if match_mode == "exact":
        return ("", 1) if regex.fullmatch(value) is not None else (value, 0)

    if match_mode == "starts_with":
        match = regex.match(value)
        if match is None:
            return value, 0
        start, end = match.span()
        if start != 0 or end <= start:
            return value, 0
        return value[end:], 1

    if match_mode == "ends_with":
        last_match: re.Match[str] | None = None
        for candidate in regex.finditer(value):
            last_match = candidate
        if last_match is None:
            return value, 0
        start, end = last_match.span()
        if end != len(value) or end <= start:
            return value, 0
        return value[:start], 1

    scrubbed_value, replacements = regex.subn("", value)
    return scrubbed_value, replacements


def _admin_content_scrub_rules(database_path: Path | str) -> list[dict[str, Any]]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                id,
                created_at,
                updated_at,
                created_by_user_id,
                match_mode,
                pattern_text,
                use_regex,
                is_enabled,
                match_count
            FROM content_scrub_rules
            ORDER BY id ASC
            """
        ).fetchall()

    rules: list[dict[str, Any]] = []
    for row in rows:
        match_mode = str(row["match_mode"] or "").strip().lower()
        rules.append(
            {
                "id": int(row["id"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "created_by_user_id": int(row["created_by_user_id"]) if row["created_by_user_id"] is not None else None,
                "match_mode": match_mode,
                "match_mode_label": CONTENT_SCRUB_MATCH_MODE_LABELS.get(match_mode, match_mode),
                "pattern_text": str(row["pattern_text"] or ""),
                "use_regex": int(row["use_regex"] or 0) == 1,
                "is_enabled": int(row["is_enabled"] or 0) == 1,
                "match_count": int(row["match_count"] or 0),
            }
        )
    return rules


def _create_content_scrub_rule(
    database_path: Path | str,
    *,
    actor_user_id: int,
    pattern_text: str,
    match_mode: str,
    use_regex: bool,
) -> dict[str, Any]:
    normalized_pattern, normalized_match_mode, normalized_use_regex = _normalize_content_scrub_rule_input(
        pattern_text=pattern_text,
        match_mode=match_mode,
        use_regex=use_regex,
    )
    now = _now_utc_iso()
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        count_row = connection.execute("SELECT COUNT(*) FROM content_scrub_rules").fetchone()
        existing_count = int(count_row[0] if count_row is not None else 0)
        if existing_count >= CONTENT_SCRUB_MAX_RULES:
            raise ValueError(f"A maximum of {CONTENT_SCRUB_MAX_RULES} content scrub rules is allowed.")

        existing_rule = connection.execute(
            """
            SELECT id
            FROM content_scrub_rules
            WHERE match_mode = ? AND pattern_text = ? AND use_regex = ? AND is_enabled = 1
            """,
            (normalized_match_mode, normalized_pattern, 1 if normalized_use_regex else 0),
        ).fetchone()
        if existing_rule is not None:
            raise ValueError(f"An enabled rule with the same pattern already exists (#{int(existing_rule['id'])}).")

        cursor = connection.execute(
            """
            INSERT INTO content_scrub_rules (
                created_at,
                updated_at,
                created_by_user_id,
                match_mode,
                pattern_text,
                use_regex,
                match_count,
                is_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 1)
            """,
            (
                now,
                now,
                int(actor_user_id),
                normalized_match_mode,
                normalized_pattern,
                1 if normalized_use_regex else 0,
            ),
        )
        connection.commit()
        rule_id = int(cursor.lastrowid)

    return {
        "id": rule_id,
        "match_mode": normalized_match_mode,
        "pattern_text": normalized_pattern,
        "use_regex": normalized_use_regex,
    }


def _delete_content_scrub_rule(database_path: Path | str, rule_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, match_mode, pattern_text, use_regex
            FROM content_scrub_rules
            WHERE id = ?
            """,
            (int(rule_id),),
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM content_scrub_rules WHERE id = ?", (int(rule_id),))
        connection.commit()

    return {
        "id": int(row["id"]),
        "match_mode": str(row["match_mode"] or ""),
        "pattern_text": str(row["pattern_text"] or ""),
        "use_regex": int(row["use_regex"] or 0) == 1,
    }


def _load_active_content_scrub_rules(database_path: Path | str) -> list[_CompiledContentScrubRule]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, match_mode, pattern_text, use_regex
            FROM content_scrub_rules
            WHERE is_enabled = 1
            ORDER BY id ASC
            LIMIT ?
            """,
            (CONTENT_SCRUB_MAX_RULES,),
        ).fetchall()

    rules: list[_CompiledContentScrubRule] = []
    for row in rows:
        pattern_text, match_mode, use_regex = _normalize_content_scrub_rule_input(
            pattern_text=str(row["pattern_text"] or ""),
            match_mode=str(row["match_mode"] or ""),
            use_regex=int(row["use_regex"] or 0) == 1,
        )
        compiled_regex = re.compile(pattern_text) if use_regex else None
        rules.append(
            _CompiledContentScrubRule(
                id=int(row["id"]),
                match_mode=match_mode,
                pattern_text=pattern_text,
                use_regex=use_regex,
                regex=compiled_regex,
            )
        )
    return rules


def _apply_content_scrub_rules_to_cases(
    database_path: Path | str,
    parsed_cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    rules = _load_active_content_scrub_rules(database_path)
    if not rules:
        return parsed_cases, [], {
            "rule_count": 0,
            "fields_scrubbed": 0,
            "replacements_removed": 0,
            "cases_scrubbed": 0,
            "accepted_cases": len(parsed_cases),
            "quarantined_cases": 0,
        }

    accepted_cases: list[dict[str, Any]] = []
    quarantined_cases: list[dict[str, Any]] = []
    fields_scrubbed = 0
    replacements_removed = 0
    cases_scrubbed = 0
    per_rule_matches: dict[int, int] = {}

    for payload in parsed_cases:
        payload_fields_scrubbed, payload_replacements_removed = _count_content_scrub_matches(
            payload,
            rules,
            (),
            per_rule_matches,
        )
        fields_scrubbed += payload_fields_scrubbed
        replacements_removed += payload_replacements_removed
        if payload_fields_scrubbed > 0:
            cases_scrubbed += 1
            quarantined_cases.append(payload)
            continue
        accepted_cases.append(payload)

    if per_rule_matches:
        with sqlite3.connect(database_path) as connection:
            for rule_id, match_count in per_rule_matches.items():
                connection.execute(
                    "UPDATE content_scrub_rules SET match_count = match_count + ? WHERE id = ?",
                    (int(match_count), int(rule_id)),
                )
            connection.commit()

    return accepted_cases, quarantined_cases, {
        "rule_count": len(rules),
        "fields_scrubbed": fields_scrubbed,
        "replacements_removed": replacements_removed,
        "cases_scrubbed": cases_scrubbed,
        "accepted_cases": len(accepted_cases),
        "quarantined_cases": len(quarantined_cases),
    }


def _count_content_scrub_matches(
    value: Any,
    rules: list[_CompiledContentScrubRule],
    path: tuple[str, ...],
    per_rule_matches: dict[int, int],
) -> tuple[int, int]:
    if isinstance(value, str):
        if len(path) == 1 and path[0] in _CONTENT_SCRUB_PROTECTED_TOP_LEVEL_FIELDS:
            return 0, 0

        replacements_removed = 0
        for rule in rules:
            _, removed_for_rule = rule.scrub(value)
            replacements_removed += removed_for_rule
            if removed_for_rule > 0:
                per_rule_matches[rule.id] = int(per_rule_matches.get(rule.id, 0)) + int(removed_for_rule)
        fields_scrubbed = 1 if replacements_removed > 0 else 0
        return fields_scrubbed, replacements_removed

    if isinstance(value, list):
        fields_scrubbed = 0
        replacements_removed = 0
        for item in value:
            item_fields_scrubbed, item_replacements_removed = _count_content_scrub_matches(
                item,
                rules,
                path,
                per_rule_matches,
            )
            fields_scrubbed += item_fields_scrubbed
            replacements_removed += item_replacements_removed
        return fields_scrubbed, replacements_removed

    if isinstance(value, dict):
        fields_scrubbed = 0
        replacements_removed = 0
        for key, item in value.items():
            normalized_key = str(key)
            item_fields_scrubbed, item_replacements_removed = _count_content_scrub_matches(
                item,
                rules,
                path + (normalized_key,),
                per_rule_matches,
            )
            fields_scrubbed += item_fields_scrubbed
            replacements_removed += item_replacements_removed
        return fields_scrubbed, replacements_removed

    return 0, 0


def _quarantine_storage_summary(quarantine_dir: Path | str) -> dict[str, int]:
    directory = Path(quarantine_dir)
    if not directory.exists() or not directory.is_dir():
        return {"file_count": 0, "case_count": 0, "size_bytes": 0}

    file_count = 0
    case_count = 0
    size_bytes = 0
    for file_path in sorted(directory.glob("*.jsonl")):
        if not file_path.is_file():
            continue
        file_count += 1
        try:
            size_bytes += int(file_path.stat().st_size)
        except OSError:
            continue
        try:
            with file_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        case_count += 1
        except OSError:
            continue

    return {"file_count": file_count, "case_count": case_count, "size_bytes": size_bytes}
