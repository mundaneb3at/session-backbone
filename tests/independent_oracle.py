"""Independent black-box oracle for session-backbone capture-integrity outputs.

This file deliberately does not import ``owner_events`` or ``export-convert``.
Its timestamp grammar, structural checks, joins, and counter recomputation are
implemented independently so matching candidate/expected defects cannot pass by
sharing candidate code.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from strict_schema import load_schema, validate as validate_schema


CLASSES = (
    "ordinary_user_turn", "queued_mid_turn_user_message", "queue_operation_record",
    "attachment_only_message", "message_with_attachment_and_text",
    "steering_interruption", "resumed_session", "close_handoff_event",
)
DISPOSITIONS = ("EMITTED", "CONTRIBUTED", "SUPPRESSED", "DROPPED", "REJECTED")
REASONS = (
    "OWNER_EVENT_EMITTED", "SEMANTIC_EVENT_ASSEMBLY", "DUPLICATE_DURABLE_ID",
    "NON_OWNER_ACTOR", "TOOL_RECORD", "SYSTEM_BOUNDARY", "CARRIED_HISTORY_ECHO",
    "MALFORMED_JSON", "NON_OBJECT_JSON", "MALFORMED_RECORD", "MISSING_ACTOR",
    "INVALID_ATTACHMENT", "MISSING_REQUIRED_FIELD", "INVALID_TIMESTAMP",
)
ATTACHMENT_KEYS = {"id", "name", "mime_type", "byte_count", "sha256"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_TIME = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})T"
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-5][0-9])"
    r"(?P<fraction>\.[0-9]*[1-9])?Z$"
)
SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
SCHEMAS = {
    "occurrence": load_schema(SCHEMA_ROOT / "source-occurrence.schema.json"),
    "event": load_schema(SCHEMA_ROOT / "production-event.schema.json"),
    "receipt": load_schema(SCHEMA_ROOT / "disposition-receipt.schema.json"),
    "counters": load_schema(SCHEMA_ROOT / "run-counters.schema.json"),
}


class OracleError(AssertionError):
    pass


class _StrictJSONError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError("duplicate-object-name:" + key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _StrictJSONError("non-standard-number:" + value)


def _parse_finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise _StrictJSONError("number-out-of-range:" + value)
    return result


def _strict_json_loads(value: str) -> Any:
    return json.loads(value, object_pairs_hook=_strict_object,
                      parse_constant=_reject_constant,
                      parse_float=_parse_finite_float)


def json_type_exact_equal(left: Any, right: Any) -> bool:
    """Independent typed-tree equality for parsed JSON values."""
    def typed(value: Any) -> Any:
        if value is None:
            return ("null",)
        if type(value) is bool:
            return ("boolean", value)
        if type(value) is int:
            return ("integer", value)
        if type(value) is float:
            return ("float", value)
        if isinstance(value, str):
            return ("string", value)
        if isinstance(value, list):
            return ("array", tuple(typed(item) for item in value))
        if isinstance(value, dict):
            return ("object", tuple((key, typed(value[key])) for key in sorted(value)))
        return ("non-json", type(value).__name__)

    return typed(left) == typed(right)


def split_jsonl_records(text: str) -> list[str]:
    """Independently delimit JSONL by CR, LF, or CRLF only."""
    records: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\r" or character == "\n":
            records.append(text[start:index])
            if character == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
            start = index + 1
        index += 1
    if start < len(text):
        records.append(text[start:])
    return records


def exact_precedence_key(session_id: Any, text: Any) -> tuple[str, str]:
    """Oracle model for byte-exact same-session close precedence."""
    return str(session_id or ""), str(text or "")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(
            split_jsonl_records(path.read_text(encoding="utf-8")), 1):
        if not line:
            raise OracleError(f"{path.name}:blank-row:{line_number}")
        try:
            row = _strict_json_loads(line)
        except (json.JSONDecodeError, _StrictJSONError) as error:
            raise OracleError(f"{path.name}:malformed:{line_number}") from error
        if not isinstance(row, dict):
            raise OracleError(f"{path.name}:non-object:{line_number}")
        rows.append(row)
    return rows


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _fail_if(condition: bool, label: str, errors: list[str]) -> None:
    if condition:
        errors.append(label)


def _unique_nonempty_strings(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_string(item) for item in value)
        and len(value) == len(set(value))
    )


def _safe_locator(value: Any, line_number: Any = None) -> bool:
    if not _string(value):
        return False
    normalized = value.replace("\\", "/")
    try:
        source_label, locator_line = normalized.rsplit("#L", 1)
    except ValueError:
        return False
    path = PurePosixPath(source_label)
    if (source_label.startswith("/") or ":" in source_label
            or any(part in {"", ".", ".."} for part in path.parts)):
        return False
    if not locator_line.isdigit() or locator_line.startswith("0"):
        return False
    return line_number is None or (_integer(line_number) and int(locator_line) == line_number)


def validate_canonical_timestamp(value: Any, label: str = "timestamp") -> list[str]:
    errors = []
    if not isinstance(value, str):
        return [label + ":type"]
    match = CANONICAL_TIME.fullmatch(value)
    if not match:
        return [label + ":syntax"]
    fields = {name: int(match.group(name)) for name in (
        "year", "month", "day", "hour", "minute", "second",
    )}
    if fields["hour"] > 23 or fields["minute"] > 59:
        errors.append(label + ":time-range")
    else:
        try:
            datetime(fields["year"], fields["month"], fields["day"], fields["hour"],
                     fields["minute"], fields["second"])
        except ValueError:
            errors.append(label + ":calendar-range")
    return errors


def validate_occurrence(row: Any, label: str = "occurrence") -> list[str]:
    errors = ["schema:" + label + ":" + item
              for item in validate_schema(row, SCHEMAS["occurrence"])]
    if not isinstance(row, dict):
        return [label + ":type"]
    base = {
        "occurrence_id", "source_locator", "line_number", "raw_line_sha256",
        "raw_text", "parse_status", "record",
    }
    aliases = {key for key in ("stream_id", "fixture_id") if key in row}
    _fail_if(len(aliases) != 1, label + ":stream-alias", errors)
    _fail_if(set(row) != base | aliases, label + ":keys", errors)
    for key in ("occurrence_id", "source_locator"):
        _fail_if(not _string(row.get(key)), label + ":" + key, errors)
    if aliases:
        alias = next(iter(aliases))
        _fail_if(not _string(row.get(alias)), label + ":" + alias, errors)
    _fail_if(not _integer(row.get("line_number")) or row.get("line_number", 0) < 1,
             label + ":line-number", errors)
    _fail_if(not _safe_locator(row.get("source_locator"), row.get("line_number")),
             label + ":source-locator-shape", errors)
    raw_text = row.get("raw_text")
    _fail_if(not isinstance(raw_text, str), label + ":raw-text", errors)
    raw_hash = row.get("raw_line_sha256")
    _fail_if(not isinstance(raw_hash, str) or not HEX64.fullmatch(raw_hash),
             label + ":raw-hash-shape", errors)
    if isinstance(raw_text, str) and isinstance(raw_hash, str):
        _fail_if(hashlib.sha256(raw_text.encode("utf-8")).hexdigest() != raw_hash,
                 label + ":raw-hash-value", errors)
    status = row.get("parse_status")
    _fail_if(status not in {"OBJECT", "NON_OBJECT_JSON", "MALFORMED_JSON"},
             label + ":parse-status", errors)
    try:
        parsed = _strict_json_loads(raw_text) if isinstance(raw_text, str) else None
        parsed_ok = isinstance(raw_text, str)
    except (json.JSONDecodeError, _StrictJSONError):
        parsed, parsed_ok = None, False
    if status == "OBJECT":
        _fail_if(not parsed_ok or not isinstance(parsed, dict)
                 or not json_type_exact_equal(row.get("record"), parsed),
                 label + ":object-mismatch", errors)
    elif status == "NON_OBJECT_JSON":
        _fail_if(not parsed_ok or isinstance(parsed, dict) or row.get("record") is not None,
                 label + ":non-object-mismatch", errors)
    elif status == "MALFORMED_JSON":
        _fail_if(parsed_ok or row.get("record") is not None,
                 label + ":malformed-mismatch", errors)
    return errors


def validate_event(row: Any, label: str = "event") -> list[str]:
    errors = ["schema:" + label + ":" + item
              for item in validate_schema(row, SCHEMAS["event"])]
    if not isinstance(row, dict):
        return [label + ":type"]
    keys = {
        "event_id", "source_record_ids", "source_occurrence_ids", "session_id",
        "resume_of", "logical_seq", "timestamp", "actor", "event_class",
        "text_blocks", "attachments", "tool_call_id", "boundary", "provenance",
    }
    _fail_if(set(row) != keys, label + ":keys", errors)
    for key in ("event_id", "session_id"):
        _fail_if(not _string(row.get(key)), label + ":" + key, errors)
    _fail_if(row.get("actor") != "owner", label + ":actor", errors)
    event_class = row.get("event_class")
    _fail_if(event_class not in CLASSES, label + ":event-class", errors)
    _fail_if(not _integer(row.get("logical_seq")) or row.get("logical_seq", 0) < 1,
             label + ":logical-seq", errors)
    errors.extend(validate_canonical_timestamp(row.get("timestamp"), label + ":timestamp"))
    for key in ("resume_of", "tool_call_id", "boundary"):
        _fail_if(row.get(key) is not None and not _string(row.get(key)),
                 label + ":" + key, errors)
    for key in ("source_record_ids", "source_occurrence_ids"):
        values = row.get(key)
        _fail_if(not _unique_nonempty_strings(values), label + ":" + key, errors)
    text_blocks = row.get("text_blocks")
    _fail_if(not isinstance(text_blocks, list)
             or any(not isinstance(value, str) for value in text_blocks or []),
             label + ":text-blocks", errors)
    attachments = row.get("attachments")
    if not isinstance(attachments, list):
        errors.append(label + ":attachments-type")
        attachments = []
    attachment_ids = set()
    for index, item in enumerate(attachments):
        item_label = f"{label}:attachment:{index}"
        if not isinstance(item, dict):
            errors.append(item_label + ":type")
            continue
        _fail_if(set(item) != ATTACHMENT_KEYS, item_label + ":keys", errors)
        for key in ("id", "name", "mime_type"):
            _fail_if(not _string(item.get(key)), item_label + ":" + key, errors)
        _fail_if(not _integer(item.get("byte_count")) or item.get("byte_count", -1) < 0,
                 item_label + ":byte-count", errors)
        sha = item.get("sha256")
        _fail_if(not isinstance(sha, str) or not HEX64.fullmatch(sha),
                 item_label + ":sha256", errors)
        attachment_id = item.get("id")
        _fail_if(attachment_id in attachment_ids, item_label + ":duplicate-id", errors)
        attachment_ids.add(attachment_id)
    provenance = row.get("provenance")
    if (not isinstance(provenance, dict)
            or set(provenance) != {"source_locators", "derivation"}):
        errors.append(label + ":provenance")
    else:
        locators = provenance.get("source_locators")
        _fail_if(not _unique_nonempty_strings(locators),
                 label + ":source-locators", errors)
        if isinstance(locators, list):
            _fail_if(any(not _safe_locator(value) for value in locators),
                     label + ":source-locator-shape", errors)
        _fail_if(provenance.get("derivation") not in {
            "DIRECT_SOURCE_FIELDS", "MULTI_RECORD_ASSEMBLY",
        }, label + ":derivation", errors)
        sources = row.get("source_occurrence_ids")
        if isinstance(sources, list):
            expected_derivation = "MULTI_RECORD_ASSEMBLY" if len(sources) > 1 else "DIRECT_SOURCE_FIELDS"
            _fail_if(provenance.get("derivation") != expected_derivation,
                     label + ":derivation-cardinality", errors)
            _fail_if(not isinstance(row.get("source_record_ids"), list)
                     or len(row["source_record_ids"]) != len(sources),
                     label + ":record-lineage-cardinality", errors)
            _fail_if(not isinstance(locators, list) or len(locators) != len(sources),
                     label + ":locator-lineage-cardinality", errors)
    has_text = bool(text_blocks) if isinstance(text_blocks, list) else False
    has_attachments = bool(attachments)
    if event_class == "ordinary_user_turn":
        _fail_if(not has_text or has_attachments or row.get("resume_of") is not None,
                 label + ":ordinary-shape", errors)
    elif event_class == "attachment_only_message":
        _fail_if(has_text or not has_attachments, label + ":attachment-only-shape", errors)
    elif event_class == "message_with_attachment_and_text":
        _fail_if(not has_text or not has_attachments, label + ":mixed-shape", errors)
    elif event_class in {
        "queued_mid_turn_user_message", "queue_operation_record",
        "steering_interruption", "close_handoff_event",
    }:
        _fail_if(not has_text or not _string(row.get("boundary")),
                 label + ":boundary-shape", errors)
    elif event_class == "resumed_session":
        _fail_if(not has_text or not _string(row.get("resume_of")),
                 label + ":resume-shape", errors)
    return errors


def validate_receipt(row: Any, label: str = "receipt") -> list[str]:
    errors = ["schema:" + label + ":" + item
              for item in validate_schema(row, SCHEMAS["receipt"])]
    if not isinstance(row, dict):
        return [label + ":type"]
    keys = {
        "receipt_id", "occurrence_id", "source_locator", "source_record_id",
        "event_id", "event_class", "disposition", "reason_code",
    }
    _fail_if(set(row) != keys, label + ":keys", errors)
    for key in ("receipt_id", "occurrence_id", "source_locator"):
        _fail_if(not _string(row.get(key)), label + ":" + key, errors)
    _fail_if(not _safe_locator(row.get("source_locator")),
             label + ":source-locator-shape", errors)
    if _string(row.get("occurrence_id")):
        _fail_if(row.get("receipt_id") != "RCPT-" + row["occurrence_id"],
                 label + ":receipt-id-binding", errors)
    for key in ("source_record_id", "event_id"):
        _fail_if(row.get(key) is not None and not _string(row.get(key)),
                 label + ":" + key, errors)
    _fail_if(row.get("event_class") is not None and row.get("event_class") not in CLASSES,
             label + ":event-class", errors)
    disposition, reason = row.get("disposition"), row.get("reason_code")
    _fail_if(disposition not in DISPOSITIONS, label + ":disposition", errors)
    _fail_if(reason not in REASONS, label + ":reason", errors)
    if disposition == "EMITTED":
        _fail_if(reason != "OWNER_EVENT_EMITTED"
                 or row.get("event_class") not in CLASSES
                 or not _string(row.get("event_id"))
                 or not _string(row.get("source_record_id")),
                 label + ":emitted-shape", errors)
    elif disposition == "CONTRIBUTED":
        _fail_if(reason != "SEMANTIC_EVENT_ASSEMBLY"
                 or row.get("event_class") not in CLASSES
                 or not _string(row.get("event_id"))
                 or not _string(row.get("source_record_id")),
                 label + ":contributed-shape", errors)
    elif disposition == "DROPPED":
        _fail_if(reason != "DUPLICATE_DURABLE_ID"
                 or row.get("event_class") not in CLASSES
                 or not _string(row.get("event_id"))
                 or not _string(row.get("source_record_id")),
                 label + ":dropped-shape", errors)
    elif disposition == "SUPPRESSED":
        _fail_if(row.get("event_class") is not None or reason not in {
            "NON_OWNER_ACTOR", "TOOL_RECORD", "SYSTEM_BOUNDARY", "CARRIED_HISTORY_ECHO",
        }, label + ":suppressed-shape", errors)
    elif disposition == "REJECTED":
        _fail_if(row.get("event_class") is not None or reason not in {
            "MALFORMED_JSON", "NON_OBJECT_JSON", "MALFORMED_RECORD", "MISSING_ACTOR",
            "INVALID_ATTACHMENT", "MISSING_REQUIRED_FIELD", "INVALID_TIMESTAMP",
        }, label + ":rejected-shape", errors)
    return errors


def recompute_counters(events: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    by_disposition = {name: 0 for name in DISPOSITIONS}
    by_class = {
        name: {"semantic_events": 0, "emitted_events": 0,
               "contributing_occurrences": 0, "dropped_occurrences": 0}
        for name in CLASSES
    }
    reasons: Counter[str] = Counter()
    for event in events:
        event_class = event.get("event_class") if isinstance(event, dict) else None
        if event_class in by_class:
            by_class[event_class]["semantic_events"] += 1
            by_class[event_class]["emitted_events"] += 1
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        disposition = receipt.get("disposition")
        if disposition in by_disposition:
            by_disposition[disposition] += 1
        reason = receipt.get("reason_code")
        if reason in REASONS:
            reasons[reason] += 1
        event_class = receipt.get("event_class")
        if event_class is not None and disposition in {"EMITTED", "CONTRIBUTED"}:
            if event_class in by_class:
                by_class[event_class]["contributing_occurrences"] += 1
        elif event_class is not None and disposition == "DROPPED":
            if event_class in by_class:
                by_class[event_class]["dropped_occurrences"] += 1
    return {
        "total_source_occurrences": len(receipts),
        "total_dispositions": len(receipts),
        "total_semantic_owner_events": len(events),
        "total_emitted_events": len(events),
        "by_disposition": by_disposition,
        "by_event_class": by_class,
        "by_reason_code": dict(sorted(reasons.items())),
    }


def validate_counters(row: Any, label: str = "counters") -> list[str]:
    errors = ["schema:" + label + ":" + item
              for item in validate_schema(row, SCHEMAS["counters"])]
    if not isinstance(row, dict):
        return [label + ":type"]
    keys = {
        "total_source_occurrences", "total_dispositions", "total_semantic_owner_events",
        "total_emitted_events", "by_disposition", "by_event_class", "by_reason_code",
    }
    _fail_if(set(row) != keys, label + ":keys", errors)
    for key in ("total_source_occurrences", "total_dispositions",
                "total_semantic_owner_events", "total_emitted_events"):
        _fail_if(not _integer(row.get(key)) or row.get(key, -1) < 0,
                 label + ":" + key, errors)
    disposition = row.get("by_disposition")
    _fail_if(not isinstance(disposition, dict) or set(disposition) != set(DISPOSITIONS),
             label + ":dispositions", errors)
    if isinstance(disposition, dict):
        _fail_if(any(not _integer(value) or value < 0 for value in disposition.values()),
                 label + ":disposition-values", errors)
    classes = row.get("by_event_class")
    _fail_if(not isinstance(classes, dict) or set(classes) != set(CLASSES),
             label + ":classes", errors)
    class_keys = {"semantic_events", "emitted_events", "contributing_occurrences", "dropped_occurrences"}
    if isinstance(classes, dict):
        for name, value in classes.items():
            _fail_if(not isinstance(value, dict) or set(value) != class_keys,
                     label + ":class-shape:" + name, errors)
            if isinstance(value, dict):
                _fail_if(any(not _integer(item) or item < 0 for item in value.values()),
                         label + ":class-values:" + name, errors)
    reasons = row.get("by_reason_code")
    valid_reasons = isinstance(reasons, dict) and all(
        key in REASONS and _integer(value) and value >= 0
        for key, value in reasons.items()
    )
    _fail_if(not valid_reasons, label + ":reasons", errors)
    return errors


def validate_packet(occurrences: list[dict[str, Any]], events: list[dict[str, Any]],
                    receipts: list[dict[str, Any]], counters: dict[str, Any], *,
                    expected_events: list[dict[str, Any]] | None = None,
                    expected_receipts: list[dict[str, Any]] | None = None,
                    expected_counters: dict[str, Any] | None = None) -> None:
    errors = []
    for index, row in enumerate(occurrences):
        errors.extend(validate_occurrence(row, f"occurrence[{index}]"))
    for index, row in enumerate(events):
        errors.extend(validate_event(row, f"event[{index}]"))
    for index, row in enumerate(receipts):
        errors.extend(validate_receipt(row, f"receipt[{index}]"))
    errors.extend(validate_counters(counters))
    occurrence_ids = [row.get("occurrence_id") for row in occurrences]
    receipt_occurrence_ids = [row.get("occurrence_id") for row in receipts]
    event_ids = [row.get("event_id") for row in events]
    _fail_if(len(occurrence_ids) != len(set(occurrence_ids)), "join:duplicate-occurrence", errors)
    _fail_if(len(event_ids) != len(set(event_ids)), "join:duplicate-event", errors)
    _fail_if(len(receipt_occurrence_ids) != len(set(receipt_occurrence_ids)),
             "join:duplicate-receipt-occurrence", errors)
    _fail_if(set(occurrence_ids) != set(receipt_occurrence_ids), "join:receipt-coverage", errors)
    event_by_id = {row.get("event_id"): row for row in events}
    occurrence_by_id = {row.get("occurrence_id"): row for row in occurrences}
    receipt_by_occurrence = {row.get("occurrence_id"): row for row in receipts}
    for occurrence_id, occurrence in occurrence_by_id.items():
        receipt = receipt_by_occurrence.get(occurrence_id)
        if not receipt:
            continue
        _fail_if(receipt.get("source_locator") != occurrence.get("source_locator"),
                 "join:receipt-locator:" + str(occurrence_id), errors)
        record = occurrence.get("record")
        expected_record_id = record.get("record_id") if isinstance(record, dict) else None
        expected_event_id = record.get("event_id") if isinstance(record, dict) else None
        if not _string(expected_record_id):
            expected_record_id = None
        if not _string(expected_event_id):
            expected_event_id = None
        _fail_if(receipt.get("source_record_id") != expected_record_id,
                 "join:receipt-record:" + str(occurrence_id), errors)
        _fail_if(receipt.get("event_id") != expected_event_id,
                 "join:receipt-event:" + str(occurrence_id), errors)
    contributing = set()
    for event in events:
        source_occurrence_ids = event.get("source_occurrence_ids", [])
        source_record_ids = event.get("source_record_ids", [])
        provenance = event.get("provenance")
        source_locators = provenance.get("source_locators", []) if isinstance(provenance, dict) else []
        derived_record_ids = []
        derived_locators = []
        for occurrence_id in source_occurrence_ids:
            contributing.add(occurrence_id)
            occurrence = occurrence_by_id.get(occurrence_id)
            if occurrence:
                record = occurrence.get("record")
                derived_record_ids.append(record.get("record_id") if isinstance(record, dict) else None)
                derived_locators.append(occurrence.get("source_locator"))
            receipt = receipt_by_occurrence.get(occurrence_id)
            if not receipt:
                errors.append("join:event-source-without-receipt:" + str(occurrence_id))
            elif (receipt.get("disposition") not in {"EMITTED", "CONTRIBUTED"}
                  or receipt.get("event_id") != event.get("event_id")
                  or receipt.get("event_class") != event.get("event_class")):
                errors.append("join:event-source-receipt:" + str(occurrence_id))
        _fail_if(source_record_ids != derived_record_ids,
                 "join:event-record-lineage:" + str(event.get("event_id")), errors)
        _fail_if(source_locators != derived_locators,
                 "join:event-locator-lineage:" + str(event.get("event_id")), errors)
    for receipt in receipts:
        if receipt.get("disposition") in {"EMITTED", "CONTRIBUTED"}:
            _fail_if(receipt.get("occurrence_id") not in contributing,
                     "join:unlinked-contributor:" + str(receipt.get("occurrence_id")), errors)
        if receipt.get("disposition") == "DROPPED":
            _fail_if(receipt.get("event_id") not in event_by_id,
                     "join:dropped-event-missing:" + str(receipt.get("event_id")), errors)
            _fail_if(receipt.get("occurrence_id") in contributing,
                     "join:dropped-occurrence-contributed:" + str(receipt.get("occurrence_id")), errors)
    _fail_if(counters != recompute_counters(events, receipts), "counters:recompute", errors)

    if expected_events is not None:
        for index, row in enumerate(expected_events):
            errors.extend(validate_event(row, f"expected-event[{index}]"))
        _fail_if(events != expected_events, "expected:events", errors)
    if expected_receipts is not None:
        for index, row in enumerate(expected_receipts):
            errors.extend(validate_receipt(row, f"expected-receipt[{index}]"))
        expected_map = {row.get("occurrence_id"): row for row in expected_receipts}
        observed_map = {row.get("occurrence_id"): row for row in receipts}
        _fail_if(len(expected_map) != len(expected_receipts), "expected:duplicate-receipts", errors)
        _fail_if(observed_map != expected_map, "expected:receipts", errors)
    if expected_counters is not None:
        errors.extend(validate_counters(expected_counters, "expected-counters"))
        _fail_if(counters != expected_counters, "expected:counters", errors)
    if errors:
        raise OracleError(";".join(errors))
