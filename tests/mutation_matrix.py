"""Independent negative controls for the canonical capture packet.

The matrix deliberately imports only ``independent_oracle``.  It replays the
predecessor's 42 declared controls and adds the schema, timestamp, typed
provenance, attachment-identity, and lineage/derivation regressions found by
the two correction reviews.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Callable

from independent_oracle import OracleError, read_jsonl, validate_packet


Packet = dict[str, Any]
Mutation = Callable[[Packet], None]


def _load_packet(fixtures: Path) -> Packet:
    events = read_jsonl(fixtures / "expected-events-v2.jsonl")
    receipts = read_jsonl(fixtures / "expected-receipts-v2.jsonl")
    counters = json.loads((fixtures / "expected-counters-v2.json").read_text(
        encoding="utf-8"))
    return {
        "occurrences": read_jsonl(fixtures / "occurrences-v2.jsonl"),
        "expected_events": events,
        "expected_receipts": receipts,
        "expected_counters": counters,
        "observed_events": copy.deepcopy(events),
        "observed_receipts": copy.deepcopy(receipts),
        "observed_counters": copy.deepcopy(counters),
    }


def _validate(packet: Packet) -> tuple[str, str]:
    try:
        validate_packet(
            packet["occurrences"], packet["observed_events"],
            packet["observed_receipts"], packet["observed_counters"],
            expected_events=packet["expected_events"],
            expected_receipts=packet["expected_receipts"],
            expected_counters=packet["expected_counters"],
        )
    except OracleError as error:
        return "FAIL", str(error)
    return "PASS", ""


def _event(packet: Packet, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return next(row for row in packet["observed_events"] if predicate(row))


def _receipt(packet: Packet, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return next(row for row in packet["observed_receipts"] if predicate(row))


def run_matrix(fixtures: Path) -> dict[str, Any]:
    base = _load_packet(fixtures)
    control_result, control_errors = _validate(copy.deepcopy(base))
    if control_result != "PASS":
        raise OracleError("mutation-control:" + control_errors)

    cases: list[tuple[str, str, str, Mutation]] = []

    def add(name: str, expected: str, token: str, mutation: Mutation) -> None:
        cases.append((name, expected, token, mutation))

    first = lambda p: p["observed_events"][0]
    first_receipt = lambda p: p["observed_receipts"][0]
    attachment_event = lambda p: _event(p, lambda row: bool(row["attachments"]))
    resumed_event = lambda p: _event(p, lambda row: row["event_class"] == "resumed_session")
    queue_event = lambda p: _event(p, lambda row: row["event_class"] == "queue_operation_record")
    multi_event = lambda p: _event(p, lambda row: len(row["source_occurrence_ids"]) > 1)
    contributed_value_event = lambda p: _event(
        p, lambda row: len(row["source_occurrence_ids"]) > 1 and bool(row["attachments"]))
    dropped_receipt = lambda p: _receipt(p, lambda row: row["disposition"] == "DROPPED")

    # The predecessor's 18 event controls.
    add("event-extra-property", "FAIL", ":keys", lambda p: first(p).__setitem__("fixture_id", "forbidden"))
    add("event-missing-field", "FAIL", ":keys", lambda p: first(p).pop("actor"))
    add("event-blank-id", "FAIL", ":event_id", lambda p: first(p).__setitem__("event_id", ""))
    add("event-boolean-logical-seq", "FAIL", ":logical-seq", lambda p: first(p).__setitem__("logical_seq", True))
    add("event-wrong-value", "FAIL", "expected:events", lambda p: first(p)["text_blocks"].__setitem__(0, "wrong"))
    add("event-unknown-id", "FAIL", "expected:events", lambda p: first(p).__setitem__("event_id", "UNKNOWN"))
    add("event-missing-row", "FAIL", "expected:events", lambda p: p["observed_events"].pop())
    add("event-duplicate-row", "FAIL", "join:duplicate-event", lambda p: p["observed_events"].append(copy.deepcopy(first(p))))
    add("event-order", "FAIL", "expected:events", lambda p: p["observed_events"].__setitem__(slice(0, 2), list(reversed(p["observed_events"][:2]))))
    add("event-fixture-only-class", "FAIL", ":event-class", lambda p: first(p).__setitem__("event_class", "duplicated_record"))
    add("event-extra-provenance-key", "FAIL", ":provenance", lambda p: first(p)["provenance"].__setitem__("generator", "fixture"))
    add("event-unknown-occurrence", "FAIL", "join:event-record-lineage", lambda p: first(p)["source_occurrence_ids"].__setitem__(0, "UNKNOWN-OCCURRENCE"))
    add("event-duplicate-occurrence", "FAIL", ":source_occurrence_ids", lambda p: multi_event(p)["source_occurrence_ids"].append(multi_event(p)["source_occurrence_ids"][0]))
    add("event-provenance-locator", "FAIL", "join:event-locator-lineage", lambda p: first(p)["provenance"]["source_locators"].__setitem__(0, "wrong#L1"))
    add("event-attachment-bytecount-boolean", "FAIL", ":byte-count", lambda p: attachment_event(p)["attachments"][0].__setitem__("byte_count", True))
    add("event-attachment-only-empty", "FAIL", ":attachment-only-shape", lambda p: _event(p, lambda row: row["event_class"] == "attachment_only_message").__setitem__("attachments", []))
    add("event-resume-null", "FAIL", ":resume-shape", lambda p: resumed_event(p).__setitem__("resume_of", None))
    add("event-queue-boundary-null", "FAIL", ":boundary-shape", lambda p: queue_event(p).__setitem__("boundary", None))

    # The predecessor's 16 receipt controls, including its order-insensitive pass.
    add("receipt-extra-property", "FAIL", ":keys", lambda p: first_receipt(p).__setitem__("fixture_id", "forbidden"))
    add("receipt-blank-id", "FAIL", ":receipt_id", lambda p: first_receipt(p).__setitem__("receipt_id", ""))
    add("receipt-numeric-id", "FAIL", ":receipt_id", lambda p: first_receipt(p).__setitem__("receipt_id", 7))
    add("receipt-numeric-locator", "FAIL", ":source_locator", lambda p: first_receipt(p).__setitem__("source_locator", 7))
    add("receipt-false-locator", "FAIL", "join:receipt-locator", lambda p: first_receipt(p).__setitem__("source_locator", "false#L1"))
    add("receipt-missing-row", "FAIL", "join:receipt-coverage", lambda p: p["observed_receipts"].pop())
    add("receipt-duplicate-occurrence", "FAIL", "join:duplicate-receipt-occurrence", lambda p: p["observed_receipts"].append(copy.deepcopy(first_receipt(p))))
    add("receipt-duplicate-id", "FAIL", ":receipt-id-binding", lambda p: p["observed_receipts"][1].__setitem__("receipt_id", first_receipt(p)["receipt_id"]))
    add("receipt-unknown-occurrence", "FAIL", "join:receipt-coverage", lambda p: first_receipt(p).__setitem__("occurrence_id", "UNKNOWN-OCCURRENCE"))
    add("receipt-arbitrary-reason", "FAIL", ":reason", lambda p: first_receipt(p).__setitem__("reason_code", "ARBITRARY"))
    add("receipt-wrong-valid-reason", "FAIL", ":emitted-shape", lambda p: first_receipt(p).__setitem__("reason_code", "SEMANTIC_EVENT_ASSEMBLY"))
    add("receipt-null-valid-source-id", "FAIL", ":emitted-shape", lambda p: first_receipt(p).__setitem__("source_record_id", None))
    add("receipt-fixture-only-class", "FAIL", ":event-class", lambda p: first_receipt(p).__setitem__("event_class", "duplicated_record"))
    add("receipt-physical-duplicate-missing", "FAIL", "join:receipt-coverage", lambda p: p["observed_receipts"].remove(dropped_receipt(p)))

    def contribute_duplicate(packet: Packet) -> None:
        row = dropped_receipt(packet)
        row["disposition"] = "CONTRIBUTED"
        row["reason_code"] = "SEMANTIC_EVENT_ASSEMBLY"

    add("receipt-physical-duplicate-contributed", "FAIL", "join:unlinked-contributor", contribute_duplicate)
    add("receipt-order-permutation", "PASS", "", lambda p: p["observed_receipts"].reverse())

    # The predecessor's five counter and three occurrence controls.
    add("counter-wrong-total", "FAIL", "counters:recompute", lambda p: p["observed_counters"].__setitem__("total_source_occurrences", -1))
    add("counter-extra-property", "FAIL", "counters:keys", lambda p: p["observed_counters"].__setitem__("fixture_count", 47))
    add("counter-boolean-integer", "FAIL", "counters:total_source_occurrences", lambda p: p["observed_counters"].__setitem__("total_source_occurrences", True))
    add("counter-missing-class", "FAIL", "counters:classes", lambda p: p["observed_counters"]["by_event_class"].pop("ordinary_user_turn"))
    add("counter-wrong-reason", "FAIL", "counters:recompute", lambda p: p["observed_counters"]["by_reason_code"].__setitem__("OWNER_EVENT_EMITTED", 999))
    add("input-extra-property", "FAIL", ":keys", lambda p: p["occurrences"][0].__setitem__("expected_action", "CAPTURE"))
    add("input-boolean-line-number", "FAIL", ":line-number", lambda p: p["occurrences"][0].__setitem__("line_number", True))
    add("input-false-locator", "FAIL", "join:receipt-locator", lambda p: p["occurrences"][0].__setitem__("source_locator", "changed#L1"))

    # Six additional controls close the final review's false-pass family.
    for suffix, invalid in (
        ("week-date", "2026-W01-1T00:00:00Z"),
        ("space-separator", "2026-01-01 00:00:00Z"),
        ("short-offset", "2026-01-01T00:00:00+01"),
        ("offset-seconds", "2026-01-01T00:00:00+01:00:30"),
    ):
        def paired_timestamp(packet: Packet, value: str = invalid) -> None:
            packet["observed_events"][0]["timestamp"] = value
            packet["expected_events"][0]["timestamp"] = value

        add("paired-invalid-timestamp-" + suffix, "FAIL", ":timestamp:syntax", paired_timestamp)
    add("input-raw-hash-mismatch", "FAIL", ":raw-hash-value", lambda p: p["occurrences"][0].__setitem__("raw_line_sha256", "0" * 64))
    add("event-drop-later-contributor-value", "FAIL", ":mixed-shape", lambda p: contributed_value_event(p).__setitem__("attachments", []))

    # Four successor controls cover accepted packet-level findings.
    add("input-recursive-boolean-number-mismatch", "FAIL", ":object-mismatch",
        lambda p: p["occurrences"][0]["record"].__setitem__("logical_seq", True))

    def duplicate_attachment_id(packet: Packet) -> None:
        row = attachment_event(packet)
        duplicate = copy.deepcopy(row["attachments"][0])
        duplicate["name"] = "distinct-name-with-same-id.bin"
        row["attachments"].append(duplicate)

    add("event-duplicate-attachment-id", "FAIL", ":duplicate-id", duplicate_attachment_id)
    add("event-lineage-cardinality", "FAIL", ":record-lineage-cardinality",
        lambda p: first(p)["source_record_ids"].append("EXTRA-SOURCE-RECORD"))
    add("event-derivation-cardinality", "FAIL", ":derivation-cardinality",
        lambda p: first(p)["provenance"].__setitem__(
            "derivation", "MULTI_RECORD_ASSEMBLY"))

    results = []
    for name, expected_result, token, mutation in cases:
        packet = copy.deepcopy(base)
        mutation(packet)
        actual_result, errors = _validate(packet)
        token_seen = not token or token in errors
        passed = actual_result == expected_result and token_seen
        results.append({
            "name": name,
            "expected_result": expected_result,
            "actual_result": actual_result,
            "expected_error_token": token,
            "token_seen": token_seen,
            "errors": errors.split(";") if errors else [],
            "result": "PASS" if passed else "FAIL",
        })
    return {
        "version": "SESSION-BACKBONE-INDEPENDENT-MUTATIONS-v1",
        "result": "PASS" if all(row["result"] == "PASS" for row in results) else "FAIL",
        "control_result": control_result,
        "cases": len(results),
        "passed": sum(row["result"] == "PASS" for row in results),
        "failed": sum(row["result"] != "PASS" for row in results),
        "predecessor_cases_replayed": 42,
        "additional_cases": len(results) - 42,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--result-json", type=Path)
    args = parser.parse_args()
    result = run_matrix(args.fixtures.resolve())
    if args.result_json:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
    print(json.dumps({key: result[key] for key in (
        "version", "result", "control_result", "cases", "passed", "failed",
    )}, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
