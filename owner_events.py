#!/usr/bin/env python3
"""Lossless SourceOccurrence wrapping and canonical owner-event normalization.

This module is intentionally standard-library only. It treats physical source
occurrences, semantic owner events, and lossy Markdown projections as different
products. It never invents semantic fields that are absent from a source row.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = "session-backbone.capture-integrity.v1"
PRODUCTION_CLASSES = (
    "ordinary_user_turn",
    "queued_mid_turn_user_message",
    "queue_operation_record",
    "attachment_only_message",
    "message_with_attachment_and_text",
    "steering_interruption",
    "resumed_session",
    "close_handoff_event",
)
MESSAGE_CLASSES = {
    "ordinary_user_turn", "attachment_only_message", "message_with_attachment_and_text",
}
DISPOSITIONS = ("EMITTED", "CONTRIBUTED", "SUPPRESSED", "DROPPED", "REJECTED")
REASON_CODES = (
    "OWNER_EVENT_EMITTED", "SEMANTIC_EVENT_ASSEMBLY", "DUPLICATE_DURABLE_ID",
    "NON_OWNER_ACTOR", "TOOL_RECORD", "SYSTEM_BOUNDARY", "CARRIED_HISTORY_ECHO",
    "MALFORMED_JSON", "NON_OBJECT_JSON", "MALFORMED_RECORD", "MISSING_ACTOR",
    "INVALID_ATTACHMENT", "MISSING_REQUIRED_FIELD", "INVALID_TIMESTAMP",
)
ATTACHMENT_KEYS = ("id", "name", "mime_type", "byte_count", "sha256")
SAFE_STREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RFC3339 = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"[Tt](?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-5][0-9])"
    r"(?P<fraction>\.[0-9]+)?(?P<zone>[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)
CANONICAL_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-5][0-9]"
    r"(?:\.[0-9]*[1-9])?Z$"
)


class ContractError(ValueError):
    """Raised when a declared lossless contract cannot be satisfied safely."""


class StrictJSONError(ValueError):
    """Raised for non-standard constants or duplicate JSON object names."""


@dataclass(frozen=True)
class ParsedTimestamp:
    canonical: str
    utc_second: datetime
    fraction: Decimal

    @property
    def sort_key(self) -> tuple[datetime, Decimal]:
        return self.utc_second, self.fraction


@dataclass(frozen=True)
class CapturedInput:
    """One immutable source read used for both parsing and provenance."""

    path: Path
    data: bytes

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "bytes": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


@dataclass(frozen=True)
class Classification:
    action: str
    reason: str
    event_class: str | None
    text_blocks: tuple[str, ...]
    attachments: tuple[dict[str, Any], ...]
    timestamp: ParsedTimestamp | None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(label + ":nonempty-string-required")
    return value


def _json_fingerprint(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def json_type_exact_equal(left: Any, right: Any) -> bool:
    """Compare parsed JSON trees without Python's bool/number coercion.

    JSON provenance binds the envelope value to the parsed source value at
    every nesting level.  Python's ordinary equality is unsuitable because
    ``True == 1`` and ``False == 0``.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (left.keys() == right.keys()
                and all(json_type_exact_equal(left[key], right[key]) for key in left))
    if isinstance(left, list):
        return (len(left) == len(right)
                and all(json_type_exact_equal(a, b) for a, b in zip(left, right)))
    return left == right


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError("json:duplicate-object-name:" + key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise StrictJSONError("json:non-standard-number:" + value)


def _parse_finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise StrictJSONError("json:number-out-of-range:" + value)
    return result


def strict_json_loads(value: str) -> Any:
    return json.loads(value, object_pairs_hook=_strict_object,
                      parse_constant=_reject_json_constant,
                      parse_float=_parse_finite_json_float)


def file_identity(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def capture_input(path: Path) -> CapturedInput:
    """Read a source once so the parsed bytes and published identity cannot diverge."""
    source = Path(path).resolve()
    return CapturedInput(path=source, data=source.read_bytes())


def verify_input_unchanged(captured: CapturedInput) -> None:
    """Fail closed if the source path no longer contains the captured bytes."""
    try:
        current = captured.path.read_bytes()
    except OSError as error:
        raise ContractError("canonical-input:unavailable-before-publication") from error
    if current != captured.data:
        raise ContractError("canonical-input:drift-before-publication")


def parse_timestamp(value: Any) -> ParsedTimestamp:
    """Validate the declared RFC-3339 profile and normalize it to UTC ``Z``.

    The profile accepts a full Gregorian date, ``T``/``t``, seconds 00-59,
    arbitrary decimal precision, and ``Z``/``z`` or a numeric minute offset.
    Leap seconds are deliberately outside this declared profile so distinct
    instants are never collapsed by Python's non-leap-second datetime model.
    """
    if not isinstance(value, str):
        raise ContractError("timestamp:string-required")
    match = RFC3339.fullmatch(value)
    if not match:
        raise ContractError("timestamp:declared-rfc3339-profile")
    parts = {name: int(match.group(name)) for name in (
        "year", "month", "day", "hour", "minute", "second",
    )}
    if parts["hour"] > 23 or parts["minute"] > 59:
        raise ContractError("timestamp:time-range")
    try:
        local_second = datetime(
            parts["year"], parts["month"], parts["day"],
            parts["hour"], parts["minute"], parts["second"],
        )
    except ValueError as error:
        raise ContractError("timestamp:calendar-range") from error
    zone = match.group("zone")
    if zone.lower() == "z":
        offset = timedelta(0)
    else:
        offset_hour = int(zone[1:3])
        offset_minute = int(zone[4:6])
        if offset_hour > 23 or offset_minute > 59:
            raise ContractError("timestamp:offset-range")
        direction = 1 if zone[0] == "+" else -1
        offset = direction * timedelta(hours=offset_hour, minutes=offset_minute)
    try:
        utc_second = local_second - offset
    except OverflowError as error:
        raise ContractError("timestamp:utc-range") from error
    raw_fraction = (match.group("fraction") or "")[1:]
    fraction = Decimal("0." + raw_fraction) if raw_fraction else Decimal(0)
    normalized_fraction = raw_fraction.rstrip("0")
    canonical = utc_second.strftime("%Y-%m-%dT%H:%M:%S")
    if normalized_fraction:
        canonical += "." + normalized_fraction
    canonical += "Z"
    return ParsedTimestamp(canonical, utc_second, fraction)


def validate_source_label(value: str) -> str:
    value = _require_nonempty_string(value, "source-label").replace("\\", "/")
    path = PurePosixPath(value)
    if value.startswith("/") or ":" in value or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError("source-label:relative-logical-path-required")
    return value


def validate_stream_id(value: str) -> str:
    value = _require_nonempty_string(value, "stream-id")
    if not SAFE_STREAM.fullmatch(value):
        raise ContractError("stream-id:unsafe")
    return value


def validate_source_locator(value: Any, line_number: int | None = None) -> str:
    original = _require_nonempty_string(value, "source-locator")
    value = original.replace("\\", "/")
    if value != original:
        raise ContractError("source-locator:forward-slashes-required")
    try:
        source_label, locator_line = value.rsplit("#L", 1)
    except ValueError as error:
        raise ContractError("source-locator:line-suffix-required") from error
    if not locator_line.isdigit() or locator_line.startswith("0"):
        raise ContractError("source-locator:positive-line-required")
    if line_number is not None and int(locator_line) != line_number:
        raise ContractError("source-locator:line-mismatch")
    validate_source_label(source_label)
    return value


def stream_key(occurrence: dict[str, Any]) -> str:
    value = occurrence.get("stream_id", occurrence.get("fixture_id"))
    return _require_nonempty_string(value, "occurrence-stream")


def validate_occurrence(occurrence: Any) -> dict[str, Any]:
    if not isinstance(occurrence, dict):
        raise ContractError("occurrence:object-required")
    base = {
        "occurrence_id", "source_locator", "line_number", "raw_line_sha256",
        "raw_text", "parse_status", "record",
    }
    aliases = {name for name in ("stream_id", "fixture_id") if name in occurrence}
    if len(aliases) != 1:
        raise ContractError("occurrence:exactly-one-stream-id")
    if set(occurrence) != base | aliases:
        raise ContractError("occurrence:exact-key-set")
    _require_nonempty_string(occurrence["occurrence_id"], "occurrence-id")
    if not _is_int(occurrence["line_number"]) or occurrence["line_number"] < 1:
        raise ContractError("occurrence:line-number")
    validate_source_locator(occurrence["source_locator"], occurrence["line_number"])
    validate_stream_id(stream_key(occurrence))
    if not isinstance(occurrence["raw_text"], str):
        raise ContractError("occurrence:raw-text")
    raw_hash = occurrence["raw_line_sha256"]
    if not isinstance(raw_hash, str) or not HEX64.fullmatch(raw_hash):
        raise ContractError("occurrence:raw-hash-shape")
    expected_hash = hashlib.sha256(occurrence["raw_text"].encode("utf-8")).hexdigest()
    if raw_hash != expected_hash:
        raise ContractError("occurrence:raw-hash-mismatch")
    status = occurrence["parse_status"]
    if status not in {"OBJECT", "NON_OBJECT_JSON", "MALFORMED_JSON"}:
        raise ContractError("occurrence:parse-status")
    try:
        parsed = strict_json_loads(occurrence["raw_text"])
        parsed_ok = True
    except (json.JSONDecodeError, StrictJSONError):
        parsed, parsed_ok = None, False
    record = occurrence["record"]
    if status == "OBJECT":
        if (not parsed_ok or not isinstance(parsed, dict)
                or not json_type_exact_equal(record, parsed)):
            raise ContractError("occurrence:object-record-mismatch")
    elif status == "NON_OBJECT_JSON":
        if not parsed_ok or isinstance(parsed, dict) or record is not None:
            raise ContractError("occurrence:non-object-record-mismatch")
    elif parsed_ok or record is not None:
        raise ContractError("occurrence:malformed-record-mismatch")
    return occurrence


def split_jsonl_records(text: str) -> list[str]:
    """Split only on the JSONL contract's CR/LF record separators."""
    if text == "":
        return []
    lines = re.split(r"\r\n|\n|\r", text)
    if lines and lines[-1] == "" and re.search(r"(?:\r\n|\n|\r)$", text):
        lines.pop()
    return lines


def occurrences_from_raw_bytes(data: bytes, stream_id: str,
                               source_label: str) -> list[dict[str, Any]]:
    stream_id = validate_stream_id(stream_id)
    source_label = validate_source_label(source_label)
    if data.startswith(b"\xef\xbb\xbf"):
        raise ContractError("raw-jsonl:utf8-bom-not-supported")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("raw-jsonl:utf8-required") from error
    result = []
    for line_number, raw_text in enumerate(split_jsonl_records(text), 1):
        try:
            parsed = strict_json_loads(raw_text)
            if isinstance(parsed, dict):
                status, record = "OBJECT", parsed
            else:
                status, record = "NON_OBJECT_JSON", None
        except (json.JSONDecodeError, StrictJSONError):
            status, record = "MALFORMED_JSON", None
        occurrence = {
            "occurrence_id": f"{stream_id}-O{line_number:06d}",
            "stream_id": stream_id,
            "source_locator": f"{source_label}#L{line_number}",
            "line_number": line_number,
            "raw_line_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            "raw_text": raw_text,
            "parse_status": status,
            "record": record,
        }
        result.append(validate_occurrence(occurrence))
    return result


def occurrences_from_raw(path: Path, stream_id: str,
                         source_label: str) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that do not need publication custody."""
    return occurrences_from_raw_bytes(capture_input(path).data, stream_id, source_label)


def read_occurrences_bytes(data: bytes) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("occurrence-jsonl:utf8-required") from error
    rows = []
    for line_number, line in enumerate(split_jsonl_records(text), 1):
        if not line:
            raise ContractError(f"occurrence-jsonl:blank-envelope:{line_number}")
        try:
            value = strict_json_loads(line)
        except (json.JSONDecodeError, StrictJSONError) as error:
            raise ContractError(f"occurrence-jsonl:malformed-envelope:{line_number}") from error
        rows.append(validate_occurrence(value))
    return rows


def read_occurrences(path: Path) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that do not need publication custody."""
    return read_occurrences_bytes(capture_input(path).data)


def _text_and_attachments(record: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], bool]:
    text_blocks = []
    attachments = []
    invalid = False
    value = record.get("text")
    if isinstance(value, str) and value:
        text_blocks.append(value)
    content = record.get("content")
    if content is not None and not isinstance(content, list):
        return text_blocks, attachments, True
    for item in content or []:
        if not isinstance(item, dict):
            invalid = True
            continue
        if item.get("type") == "text":
            if isinstance(item.get("text"), str) and item["text"]:
                text_blocks.append(item["text"])
            elif not isinstance(item.get("text"), str):
                invalid = True
        elif item.get("type") == "attachment":
            if set(item) != set(ATTACHMENT_KEYS) | {"type"}:
                invalid = True
                continue
            if not all(isinstance(item.get(key), str) and item[key]
                       for key in ("id", "name", "mime_type", "sha256")):
                invalid = True
                continue
            if (not _is_int(item.get("byte_count")) or item["byte_count"] < 0
                    or not HEX64.fullmatch(item["sha256"])):
                invalid = True
                continue
            attachments.append({key: item[key] for key in ATTACHMENT_KEYS})
        else:
            invalid = True
    return text_blocks, attachments, invalid


def classify(occurrence: dict[str, Any]) -> Classification:
    status = occurrence["parse_status"]
    if status == "MALFORMED_JSON":
        return Classification("REJECTED", "MALFORMED_JSON", None, (), (), None)
    if status == "NON_OBJECT_JSON":
        return Classification("REJECTED", "NON_OBJECT_JSON", None, (), (), None)
    record = occurrence["record"]
    assert isinstance(record, dict)
    sender_value = record.get("sender")
    if not isinstance(sender_value, str) or not sender_value.strip():
        return Classification("REJECTED", "MISSING_ACTOR", None, (), (), None)
    sender = sender_value.strip().lower()
    if record.get("record_type") == "malformed":
        return Classification("REJECTED", "MALFORMED_RECORD", None, (), (), None)
    text_blocks, attachments, invalid_attachment = _text_and_attachments(record)
    if invalid_attachment:
        return Classification("REJECTED", "INVALID_ATTACHMENT", None,
                              tuple(text_blocks), tuple(attachments), None)
    record_type_value = record.get("record_type")
    record_type = record_type_value.strip().lower() if isinstance(record_type_value, str) else ""
    if record.get("boundary") == "carried_history_echo":
        return Classification("SUPPRESSED", "CARRIED_HISTORY_ECHO", None,
                              tuple(text_blocks), tuple(attachments), None)
    if record_type in {"tool_pair", "tool_call", "tool_result"} or sender == "tool":
        return Classification("SUPPRESSED", "TOOL_RECORD", None,
                              tuple(text_blocks), tuple(attachments), None)
    if record_type == "compaction" or sender == "system":
        return Classification("SUPPRESSED", "SYSTEM_BOUNDARY", None,
                              tuple(text_blocks), tuple(attachments), None)
    if sender not in {"human", "owner"}:
        return Classification("SUPPRESSED", "NON_OWNER_ACTOR", None,
                              tuple(text_blocks), tuple(attachments), None)
    required_strings = ("event_id", "record_id", "session_id", "record_type")
    if (not all(isinstance(record.get(key), str) and record[key] for key in required_strings)
            or "timestamp" not in record
            or not _is_int(record.get("logical_seq")) or record["logical_seq"] < 1):
        return Classification("REJECTED", "MISSING_REQUIRED_FIELD", None,
                              tuple(text_blocks), tuple(attachments), None)
    for optional in ("resume_of", "tool_call_id", "boundary"):
        if record.get(optional) is not None and (
                not isinstance(record[optional], str) or not record[optional]):
            return Classification("REJECTED", "MISSING_REQUIRED_FIELD", None,
                                  tuple(text_blocks), tuple(attachments), None)
    try:
        timestamp = parse_timestamp(record["timestamp"])
    except ContractError:
        return Classification("REJECTED", "INVALID_TIMESTAMP", None,
                              tuple(text_blocks), tuple(attachments), None)
    boundary = record.get("boundary")
    if record.get("resume_of") is not None:
        event_class = "resumed_session"
    elif record_type == "queue_operation":
        event_class = "queue_operation_record"
    elif record_type == "steering":
        event_class = "steering_interruption"
    elif record_type == "close":
        event_class = "close_handoff_event"
    elif record_type == "message" and attachments and text_blocks:
        event_class = "message_with_attachment_and_text"
    elif record_type == "message" and attachments:
        event_class = "attachment_only_message"
    elif (record_type == "message" and isinstance(boundary, str)
          and "queue" in boundary.lower() and text_blocks):
        event_class = "queued_mid_turn_user_message"
    elif record_type == "message" and text_blocks:
        event_class = "ordinary_user_turn"
    else:
        return Classification("REJECTED", "MISSING_REQUIRED_FIELD", None,
                              tuple(text_blocks), tuple(attachments), None)
    if event_class in {
        "queue_operation_record", "steering_interruption", "resumed_session", "close_handoff_event",
    } and not text_blocks:
        return Classification("REJECTED", "MISSING_REQUIRED_FIELD", None,
                              tuple(text_blocks), tuple(attachments), None)
    return Classification("CAPTURE", "", event_class,
                          tuple(text_blocks), tuple(attachments), timestamp)


def _ordered_unique(values: Iterable[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        fingerprint = _json_fingerprint(value)
        if fingerprint not in seen:
            seen.add(fingerprint)
            result.append(value)
    return result


def validate_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ContractError("event:object-required")
    keys = {
        "event_id", "source_record_ids", "source_occurrence_ids", "session_id",
        "resume_of", "logical_seq", "timestamp", "actor", "event_class",
        "text_blocks", "attachments", "tool_call_id", "boundary", "provenance",
    }
    if set(event) != keys:
        raise ContractError("event:exact-key-set")
    for key in ("event_id", "session_id"):
        _require_nonempty_string(event[key], "event:" + key)
    if event["actor"] != "owner" or event["event_class"] not in PRODUCTION_CLASSES:
        raise ContractError("event:actor-or-class")
    if not _is_int(event["logical_seq"]) or event["logical_seq"] < 1:
        raise ContractError("event:logical-seq")
    parsed = parse_timestamp(event["timestamp"])
    if event["timestamp"] != parsed.canonical or not CANONICAL_TIMESTAMP.fullmatch(event["timestamp"]):
        raise ContractError("event:timestamp-not-canonical")
    for key in ("resume_of", "tool_call_id", "boundary"):
        if event[key] is not None and (not isinstance(event[key], str) or not event[key]):
            raise ContractError("event:" + key)
    for key in ("source_record_ids", "source_occurrence_ids"):
        values = event[key]
        if (not isinstance(values, list) or not values
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))):
            raise ContractError("event:" + key)
    if not isinstance(event["text_blocks"], list) or any(
            not isinstance(value, str) for value in event["text_blocks"]):
        raise ContractError("event:text-blocks")
    if not isinstance(event["attachments"], list):
        raise ContractError("event:attachments")
    attachment_fingerprints = set()
    for item in event["attachments"]:
        if not isinstance(item, dict) or set(item) != set(ATTACHMENT_KEYS):
            raise ContractError("event:attachment-key-set")
        if not all(isinstance(item.get(key), str) and item[key]
                   for key in ("id", "name", "mime_type", "sha256")):
            raise ContractError("event:attachment-string")
        if (not _is_int(item.get("byte_count")) or item["byte_count"] < 0
                or not HEX64.fullmatch(item["sha256"])):
            raise ContractError("event:attachment-value")
        attachment_id = item["id"]
        if attachment_id in attachment_fingerprints:
            raise ContractError("event:duplicate-attachment-id")
        attachment_fingerprints.add(attachment_id)
    provenance = event["provenance"]
    if (not isinstance(provenance, dict)
            or set(provenance) != {"source_locators", "derivation"}
            or provenance.get("derivation") not in {"DIRECT_SOURCE_FIELDS", "MULTI_RECORD_ASSEMBLY"}):
        raise ContractError("event:provenance")
    locators = provenance.get("source_locators")
    if (not isinstance(locators, list) or not locators
            or any(not isinstance(value, str) or not value for value in locators)
            or len(locators) != len(set(locators))):
        raise ContractError("event:source-locators")
    for locator in locators:
        validate_source_locator(locator)
    if len(event["source_record_ids"]) != len(event["source_occurrence_ids"]):
        raise ContractError("event:source-lineage-cardinality")
    if len(locators) != len(event["source_occurrence_ids"]):
        raise ContractError("event:locator-lineage-cardinality")
    expected_derivation = (
        "MULTI_RECORD_ASSEMBLY" if len(event["source_occurrence_ids"]) > 1
        else "DIRECT_SOURCE_FIELDS"
    )
    if provenance["derivation"] != expected_derivation:
        raise ContractError("event:derivation-cardinality")
    event_class = event["event_class"]
    has_text, has_attachments = bool(event["text_blocks"]), bool(event["attachments"])
    if event_class == "ordinary_user_turn" and (not has_text or has_attachments or event["resume_of"] is not None):
        raise ContractError("event:ordinary-shape")
    if event_class == "attachment_only_message" and (has_text or not has_attachments):
        raise ContractError("event:attachment-only-shape")
    if event_class == "message_with_attachment_and_text" and (not has_text or not has_attachments):
        raise ContractError("event:mixed-shape")
    if event_class in {"queued_mid_turn_user_message", "queue_operation_record", "steering_interruption", "close_handoff_event"}:
        if not has_text or not isinstance(event["boundary"], str) or not event["boundary"]:
            raise ContractError("event:special-boundary-shape")
    if event_class == "resumed_session" and (not has_text or not isinstance(event["resume_of"], str)):
        raise ContractError("event:resume-shape")
    return event


def validate_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ContractError("receipt:object-required")
    keys = {
        "receipt_id", "occurrence_id", "source_locator", "source_record_id",
        "event_id", "event_class", "disposition", "reason_code",
    }
    if set(receipt) != keys:
        raise ContractError("receipt:exact-key-set")
    for key in ("receipt_id", "occurrence_id", "source_locator"):
        _require_nonempty_string(receipt[key], "receipt:" + key)
    validate_source_locator(receipt["source_locator"])
    if receipt["receipt_id"] != "RCPT-" + receipt["occurrence_id"]:
        raise ContractError("receipt:id-binding")
    for key in ("source_record_id", "event_id"):
        if receipt[key] is not None and (not isinstance(receipt[key], str) or not receipt[key]):
            raise ContractError("receipt:" + key)
    if receipt["event_class"] is not None and receipt["event_class"] not in PRODUCTION_CLASSES:
        raise ContractError("receipt:event-class")
    if receipt["disposition"] not in DISPOSITIONS or receipt["reason_code"] not in REASON_CODES:
        raise ContractError("receipt:disposition-or-reason")
    disposition, reason, event_class = (
        receipt["disposition"], receipt["reason_code"], receipt["event_class"],
    )
    if disposition == "EMITTED" and (
            reason != "OWNER_EVENT_EMITTED" or event_class is None
            or not isinstance(receipt["source_record_id"], str)
            or not isinstance(receipt["event_id"], str)):
        raise ContractError("receipt:emitted-shape")
    if disposition == "CONTRIBUTED" and (
            reason != "SEMANTIC_EVENT_ASSEMBLY" or event_class is None
            or not isinstance(receipt["source_record_id"], str)
            or not isinstance(receipt["event_id"], str)):
        raise ContractError("receipt:contributed-shape")
    if disposition == "DROPPED" and (
            reason != "DUPLICATE_DURABLE_ID" or event_class is None
            or not isinstance(receipt["source_record_id"], str)
            or not isinstance(receipt["event_id"], str)):
        raise ContractError("receipt:dropped-shape")
    if disposition == "SUPPRESSED" and (event_class is not None or reason not in {
        "NON_OWNER_ACTOR", "TOOL_RECORD", "SYSTEM_BOUNDARY", "CARRIED_HISTORY_ECHO",
    }):
        raise ContractError("receipt:suppressed-shape")
    if disposition == "REJECTED" and (event_class is not None or reason not in {
        "MALFORMED_JSON", "NON_OBJECT_JSON", "MALFORMED_RECORD", "MISSING_ACTOR",
        "INVALID_ATTACHMENT", "MISSING_REQUIRED_FIELD", "INVALID_TIMESTAMP",
    }):
        raise ContractError("receipt:rejected-shape")
    return receipt


def _make_receipt(occurrence: dict[str, Any], disposition: str, reason: str,
                  event_class: str | None) -> dict[str, Any]:
    record = occurrence["record"]
    source_record_id = record.get("record_id") if isinstance(record, dict) else None
    event_id = record.get("event_id") if isinstance(record, dict) else None
    return validate_receipt({
        "receipt_id": "RCPT-" + occurrence["occurrence_id"],
        "occurrence_id": occurrence["occurrence_id"],
        "source_locator": occurrence["source_locator"],
        "source_record_id": source_record_id if isinstance(source_record_id, str) and source_record_id else None,
        "event_id": event_id if isinstance(event_id, str) and event_id else None,
        "event_class": event_class,
        "disposition": disposition,
        "reason_code": reason,
    })


def build_counters(events: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    by_disposition = {name: 0 for name in DISPOSITIONS}
    by_class = {
        name: {"semantic_events": 0, "emitted_events": 0,
               "contributing_occurrences": 0, "dropped_occurrences": 0}
        for name in PRODUCTION_CLASSES
    }
    by_reason: Counter[str] = Counter()
    for event in events:
        by_class[event["event_class"]]["semantic_events"] += 1
        by_class[event["event_class"]]["emitted_events"] += 1
    for receipt in receipts:
        disposition = receipt["disposition"]
        by_disposition[disposition] += 1
        by_reason[receipt["reason_code"]] += 1
        event_class = receipt["event_class"]
        if event_class is not None:
            if disposition in {"EMITTED", "CONTRIBUTED"}:
                by_class[event_class]["contributing_occurrences"] += 1
            elif disposition == "DROPPED":
                by_class[event_class]["dropped_occurrences"] += 1
    return {
        "total_source_occurrences": len(receipts),
        "total_dispositions": len(receipts),
        "total_semantic_owner_events": len(events),
        "total_emitted_events": len(events),
        "by_disposition": by_disposition,
        "by_event_class": by_class,
        "by_reason_code": dict(sorted(by_reason.items())),
    }


def validate_counters(counters: Any) -> dict[str, Any]:
    if not isinstance(counters, dict) or set(counters) != {
        "total_source_occurrences", "total_dispositions", "total_semantic_owner_events",
        "total_emitted_events", "by_disposition", "by_event_class", "by_reason_code",
    }:
        raise ContractError("counters:exact-key-set")
    for key in ("total_source_occurrences", "total_dispositions",
                "total_semantic_owner_events", "total_emitted_events"):
        if not _is_int(counters[key]) or counters[key] < 0:
            raise ContractError("counters:" + key)
    if set(counters["by_disposition"]) != set(DISPOSITIONS):
        raise ContractError("counters:dispositions")
    if set(counters["by_event_class"]) != set(PRODUCTION_CLASSES):
        raise ContractError("counters:event-classes")
    for value in counters["by_disposition"].values():
        if not _is_int(value) or value < 0:
            raise ContractError("counters:disposition-value")
    class_keys = {"semantic_events", "emitted_events", "contributing_occurrences", "dropped_occurrences"}
    for value in counters["by_event_class"].values():
        if not isinstance(value, dict) or set(value) != class_keys:
            raise ContractError("counters:class-shape")
        if any(not _is_int(item) or item < 0 for item in value.values()):
            raise ContractError("counters:class-value")
    if (not isinstance(counters["by_reason_code"], dict)
            or any(reason not in REASON_CODES or not _is_int(count) or count < 0
                   for reason, count in counters["by_reason_code"].items())):
        raise ContractError("counters:reason-code")
    return counters


def normalize_occurrences(occurrences: list[dict[str, Any]]) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    validated = [validate_occurrence(row) for row in occurrences]
    occurrence_ids = [row["occurrence_id"] for row in validated]
    locators = [row["source_locator"] for row in validated]
    if len(occurrence_ids) != len(set(occurrence_ids)):
        raise ContractError("occurrence:duplicate-id")
    if len(locators) != len(set(locators)):
        raise ContractError("occurrence:duplicate-source-locator")

    groups: dict[str, dict[str, Any]] = {}
    receipt_specs = []
    for occurrence in validated:
        classification = classify(occurrence)
        if classification.action != "CAPTURE":
            receipt_specs.append((occurrence, classification.action,
                                  classification.reason, None))
            continue
        record = occurrence["record"]
        assert isinstance(record, dict)
        event_id = record["event_id"]
        durable_id = record["record_id"]
        group = groups.setdefault(event_id, {
            "stream": stream_key(occurrence), "accepted": [], "durable_records": {},
        })
        if group["stream"] != stream_key(occurrence):
            raise ContractError("event-group:cross-stream:" + event_id)
        record_fingerprint = _json_fingerprint(record)
        if durable_id in group["durable_records"]:
            if group["durable_records"][durable_id] != record_fingerprint:
                raise ContractError("event-group:conflicting-durable-id:" + event_id)
            disposition, reason = "DROPPED", "DUPLICATE_DURABLE_ID"
        elif not group["accepted"]:
            disposition, reason = "EMITTED", "OWNER_EVENT_EMITTED"
            group["durable_records"][durable_id] = record_fingerprint
            group["accepted"].append((occurrence, classification))
        else:
            disposition, reason = "CONTRIBUTED", "SEMANTIC_EVENT_ASSEMBLY"
            group["durable_records"][durable_id] = record_fingerprint
            group["accepted"].append((occurrence, classification))
        receipt_specs.append((occurrence, disposition, reason, event_id))

    events_with_sort = []
    final_classes: dict[str, str] = {}
    scalar_fields = ("session_id", "resume_of", "logical_seq", "tool_call_id", "boundary")
    for event_id, group in groups.items():
        accepted = group["accepted"]
        first_occurrence, first_classification = accepted[0]
        first_record = first_occurrence["record"]
        assert isinstance(first_record, dict) and first_classification.timestamp is not None
        scalar = {field: first_record.get(field) for field in scalar_fields}
        instant = first_classification.timestamp
        preliminary_classes = set()
        all_text = []
        all_attachments = []
        for occurrence, classification in accepted:
            record = occurrence["record"]
            assert isinstance(record, dict) and classification.timestamp is not None
            for field in scalar_fields:
                if record.get(field) != scalar[field]:
                    raise ContractError(f"event-group:conflicting-{field}:{event_id}")
            if classification.timestamp.sort_key != instant.sort_key:
                raise ContractError("event-group:conflicting-timestamp:" + event_id)
            preliminary_classes.add(classification.event_class)
            all_text.extend(classification.text_blocks)
            all_attachments.extend(classification.attachments)
        text_blocks = [str(value) for value in _ordered_unique(all_text)]
        attachments_by_id = {}
        for attachment in all_attachments:
            attachment_id = attachment["id"]
            fingerprint = _json_fingerprint(attachment)
            if (attachment_id in attachments_by_id
                    and attachments_by_id[attachment_id][0] != fingerprint):
                raise ContractError("event-group:conflicting-attachment-id:" + event_id)
            attachments_by_id.setdefault(attachment_id, (fingerprint, dict(attachment)))
        attachments = [item[1] for item in attachments_by_id.values()]
        special = preliminary_classes - MESSAGE_CLASSES
        if len(special) > 1 or (special and preliminary_classes & MESSAGE_CLASSES):
            raise ContractError("event-group:incompatible-classes:" + event_id)
        if special:
            event_class = next(iter(special))
        elif text_blocks and attachments:
            event_class = "message_with_attachment_and_text"
        elif attachments:
            event_class = "attachment_only_message"
        elif text_blocks:
            event_class = "ordinary_user_turn"
        else:
            raise ContractError("event-group:empty-payload:" + event_id)
        final_classes[event_id] = event_class
        source_record_ids = _ordered_unique(
            occurrence["record"]["record_id"] for occurrence, _ in accepted
        )
        event = validate_event({
            "event_id": event_id,
            "source_record_ids": source_record_ids,
            "source_occurrence_ids": [occurrence["occurrence_id"] for occurrence, _ in accepted],
            "session_id": scalar["session_id"],
            "resume_of": scalar["resume_of"],
            "logical_seq": scalar["logical_seq"],
            "timestamp": instant.canonical,
            "actor": "owner",
            "event_class": event_class,
            "text_blocks": text_blocks,
            "attachments": attachments,
            "tool_call_id": scalar["tool_call_id"],
            "boundary": scalar["boundary"],
            "provenance": {
                "source_locators": [occurrence["source_locator"] for occurrence, _ in accepted],
                "derivation": "MULTI_RECORD_ASSEMBLY" if len(accepted) > 1 else "DIRECT_SOURCE_FIELDS",
            },
        })
        first_line = min(occurrence["line_number"] for occurrence, _ in accepted)
        events_with_sort.append((
            group["stream"], event["logical_seq"], instant.utc_second,
            instant.fraction, first_line, event_id, event,
        ))
    events = [item[-1] for item in sorted(events_with_sort, key=lambda item: item[:-1])]

    receipts = []
    for occurrence, disposition, reason, event_id in receipt_specs:
        event_class = final_classes[event_id] if event_id is not None else None
        receipts.append(_make_receipt(occurrence, disposition, reason, event_class))
    counters = validate_counters(build_counters(events, receipts))
    return events, receipts, counters


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(_json_fingerprint(row) + "\n" for row in rows),
        encoding="utf-8", newline="\n",
    )


def publish_bundle(occurrences: list[dict[str, Any]], output_dir: Path, *,
                   input_mode: str, captured_input: CapturedInput) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ContractError("canonical-output:already-exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    events, receipts, counters = normalize_occurrences(occurrences)
    temporary = Path(tempfile.mkdtemp(prefix=output_dir.name + ".tmp-", dir=output_dir.parent))
    try:
        occurrences_path = temporary / "occurrences.jsonl"
        events_path = temporary / "events.jsonl"
        receipts_path = temporary / "receipts.jsonl"
        counters_path = temporary / "counters.json"
        _write_jsonl(occurrences_path, occurrences)
        _write_jsonl(events_path, events)
        _write_jsonl(receipts_path, receipts)
        counters_path.write_text(
            json.dumps(counters, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
        outputs = {
            name: file_identity(temporary / name) for name in (
                "occurrences.jsonl", "events.jsonl", "receipts.jsonl", "counters.json",
            )
        }
        summary = {
            "version": "CI-PRODUCTION-STAGING-v1",
            "schema_version": SCHEMA_VERSION,
            "result": "PASS",
            "input_mode": input_mode,
            "input": {"path_label": captured_input.path.name, **captured_input.identity},
            "counts": {
                "source_occurrences": len(occurrences),
                "semantic_owner_events": len(events),
                "disposition_receipts": len(receipts),
            },
            "outputs": outputs,
        }
        (temporary / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
        verify_input_unchanged(captured_input)
        temporary.replace(output_dir)
        return summary
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
