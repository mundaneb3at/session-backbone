from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import owner_events
from independent_oracle import (
    OracleError, exact_precedence_key as oracle_exact_precedence_key,
    read_jsonl, split_jsonl_records as oracle_split_jsonl_records,
    validate_counters, validate_event, validate_occurrence as oracle_validate_occurrence,
    validate_packet, validate_receipt,
)
from mutation_matrix import run_matrix
from strict_schema import validate as validate_schema


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "capture_integrity"
MAP_FIXTURES = ROOT / "fixtures" / "map-history"
EXPORT = ROOT / "export-convert.py"
MAP_HISTORY = ROOT / "map-history.py"


def json_wire(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def write_jsonl(path: Path, rows):
    path.write_text("".join(json_wire(row) + "\n" for row in rows),
                    encoding="utf-8", newline="\n")


def make_record(event_id, record_id, session_id, logical_seq, timestamp, *,
                sender="human", record_type="message", text=None, content=None,
                resume_of=None, boundary=None, tool_call_id=None):
    row = {
        "event_id": event_id, "record_id": record_id, "session_id": session_id,
        "logical_seq": logical_seq, "timestamp": timestamp, "sender": sender,
        "record_type": record_type,
    }
    if text is not None:
        row["text"] = text
    if content is not None:
        row["content"] = content
    if resume_of is not None:
        row["resume_of"] = resume_of
    if boundary is not None:
        row["boundary"] = boundary
    if tool_call_id is not None:
        row["tool_call_id"] = tool_call_id
    return row


def make_occurrence(record, number, *, stream="SYNTH"):
    raw = json_wire(record)
    return {
        "occurrence_id": f"{stream}-O{number:06d}",
        "stream_id": stream,
        "source_locator": f"fixtures/{stream}.jsonl#L{number}",
        "line_number": number,
        "raw_line_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "raw_text": raw,
        "parse_status": "OBJECT",
        "record": record,
    }


def make_event(event_id, session_id, timestamp, text_blocks, *,
               event_class="ordinary_user_turn", attachments=None,
               logical_seq=1, resume_of=None, boundary=None):
    return {
        "event_id": event_id,
        "source_record_ids": [event_id + "-R1"],
        "source_occurrence_ids": [event_id + "-O1"],
        "session_id": session_id,
        "resume_of": resume_of,
        "logical_seq": logical_seq,
        "timestamp": timestamp,
        "actor": "owner",
        "event_class": event_class,
        "text_blocks": text_blocks,
        "attachments": attachments or [],
        "tool_call_id": None,
        "boundary": boundary,
        "provenance": {
            "source_locators": ["fixtures/canonical.jsonl#L" + str(logical_seq)],
            "derivation": "DIRECT_SOURCE_FIELDS",
        },
    }


def run_command(arguments, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-B", *map(str, arguments)], cwd=cwd,
        text=True, encoding="utf-8", capture_output=True, check=False,
    )


def read_summary(stdout):
    rows = [line[len("SUMMARY: "):] for line in stdout.splitlines()
            if line.startswith("SUMMARY: ")]
    if not rows:
        raise AssertionError("missing SUMMARY line:\n" + stdout)
    return json.loads(rows[-1])


class CaptureIntegrityTests(unittest.TestCase):
    def test_authority_hashes_and_exact_frozen_packet(self):
        authority = json.loads((FIXTURES / "AUTHORITY.json").read_text(encoding="utf-8"))
        for name, identity in authority["files"].items():
            data = (FIXTURES / name).read_bytes()
            self.assertEqual(len(data), identity["bytes"], name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), identity["sha256"], name)

        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "run1"
            second = Path(temporary) / "run2"
            command = [EXPORT, "--occurrences-jsonl", FIXTURES / "occurrences-v2.jsonl",
                       "--canonical-out"]
            result1 = run_command([*command, first])
            result2 = run_command([*command, second])
            self.assertEqual(result1.returncode, 0, result1.stderr + result1.stdout)
            self.assertEqual(result2.returncode, 0, result2.stderr + result2.stdout)

            occurrences = read_jsonl(first / "occurrences.jsonl")
            events = read_jsonl(first / "events.jsonl")
            receipts = read_jsonl(first / "receipts.jsonl")
            counters = json.loads((first / "counters.json").read_text(encoding="utf-8"))
            validate_packet(
                occurrences, events, receipts, counters,
                expected_events=read_jsonl(FIXTURES / "expected-events-v2.jsonl"),
                expected_receipts=read_jsonl(FIXTURES / "expected-receipts-v2.jsonl"),
                expected_counters=json.loads(
                    (FIXTURES / "expected-counters-v2.json").read_text(encoding="utf-8")),
            )
            self.assertEqual((len(occurrences), len(events), len(receipts)), (169, 104, 169))
            sentinel = [row for row in events if row["event_id"].startswith("CI-B02-V04-")]
            self.assertEqual(len(sentinel), 6)
            self.assertEqual([row["event_class"] for row in sentinel], [
                "ordinary_user_turn", "ordinary_user_turn", "ordinary_user_turn",
                "queue_operation_record", "queue_operation_record", "attachment_only_message",
            ])
            self.assertEqual(
                hashlib.sha256((first / "receipts.jsonl").read_bytes()).hexdigest(),
                "e8cabdad4116e1039ac7b2d91fb039bc1b10b25c72c3a94fee23c6fab384ccf6",
            )
            for name in ("occurrences.jsonl", "events.jsonl", "receipts.jsonl",
                         "counters.json", "summary.json"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_legacy_markdown_and_self_test_remain_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "legacy"
            result = run_command([
                EXPORT, "--export", ROOT / "fixtures" / "conversations.json",
                "--out", out, "--min-date", "2026-07-01",
            ])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            expected = ROOT / "fixtures" / "_selftest" / "export" / (
                "11111111-1111-1111-1111-111111111111-prompts.md")
            self.assertEqual((out / expected.name).read_bytes(), expected.read_bytes())
        self_test = run_command([EXPORT, "--self-test"])
        self.assertEqual(self_test.returncode, 0, self_test.stderr + self_test.stdout)
        self.assertIn("SELF-TEST PASS: 15 assertions", self_test.stdout)

    def test_raw_adapter_accounts_for_every_physical_line(self):
        valid = make_record("RAW-E1", "RAW-R1", "RAW-SESSION", 1,
                            "2026-08-08T01:00:00Z", text="real owner message")
        assistant = make_record("RAW-E2", "RAW-R2", "RAW-SESSION", 2,
                                "2026-08-08T01:00:01Z", sender="assistant",
                                text="assistant text")
        invalid_time = make_record("RAW-E3", "RAW-R3", "RAW-SESSION", 3,
                                   "2026-W01-1T00:00:00Z", text="invalid timestamp")
        lines = [
            json_wire(valid), "", '"non-object"', "{broken", json_wire(assistant),
            json_wire(invalid_time), '{"value":NaN}',
            '{"event_id":"FIRST","event_id":"SECOND"}',
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "records.jsonl"
            raw.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
            out = root / "bundle"
            result = run_command([
                EXPORT, "--raw-jsonl", raw, "--stream-id", "RAW",
                "--source-label", "fixtures/records.jsonl", "--canonical-out", out,
            ])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            occurrences = read_jsonl(out / "occurrences.jsonl")
            events = read_jsonl(out / "events.jsonl")
            receipts = read_jsonl(out / "receipts.jsonl")
            counters = json.loads((out / "counters.json").read_text(encoding="utf-8"))
            validate_packet(occurrences, events, receipts, counters)
            self.assertEqual(len(occurrences), 8)
            self.assertEqual([row["parse_status"] for row in occurrences], [
                "OBJECT", "MALFORMED_JSON", "NON_OBJECT_JSON", "MALFORMED_JSON",
                "OBJECT", "OBJECT", "MALFORMED_JSON", "MALFORMED_JSON",
            ])
            self.assertEqual([row["disposition"] for row in receipts], [
                "EMITTED", "REJECTED", "REJECTED", "REJECTED", "SUPPRESSED", "REJECTED",
                "REJECTED", "REJECTED",
            ])
            self.assertEqual(receipts[5]["reason_code"], "INVALID_TIMESTAMP")
            self.assertEqual([row["reason_code"] for row in receipts[-2:]],
                             ["MALFORMED_JSON", "MALFORMED_JSON"])
            self.assertTrue(all(row["source_locator"].startswith("fixtures/records.jsonl#L")
                                for row in occurrences))
            self.assertFalse(any(str(root) in row["source_locator"] for row in occurrences))
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["input"]["path_label"], raw.name)
            self.assertEqual(summary["input"]["bytes"], len(raw.read_bytes()))
            self.assertEqual(summary["input"]["sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())
            for name, identity in summary["outputs"].items():
                data = (out / name).read_bytes()
                self.assertEqual(identity, {
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                })

            invalid_utf8 = root / "invalid-utf8.jsonl"
            invalid_utf8.write_bytes(b"\xff\n")
            invalid_out = root / "invalid-output"
            failed = run_command([
                EXPORT, "--raw-jsonl", invalid_utf8, "--stream-id", "BAD",
                "--source-label", "fixtures/bad.jsonl", "--canonical-out", invalid_out,
            ])
            self.assertEqual(failed.returncode, 2)
            self.assertFalse(invalid_out.exists())
            bom = root / "bom.jsonl"
            bom.write_bytes(b"\xef\xbb\xbf{}\n")
            bom_out = root / "bom-output"
            failed_bom = run_command([
                EXPORT, "--raw-jsonl", bom, "--stream-id", "BOM",
                "--source-label", "fixtures/bom.jsonl", "--canonical-out", bom_out,
            ])
            self.assertEqual(failed_bom.returncode, 2)
            self.assertIn("utf8-bom-not-supported", failed_bom.stdout)
            self.assertFalse(bom_out.exists())
            unsafe_out = root / "unsafe-output"
            unsafe = run_command([
                EXPORT, "--raw-jsonl", raw, "--stream-id", "RAW",
                "--source-label", "C:/private/records.jsonl", "--canonical-out", unsafe_out,
            ])
            self.assertEqual(unsafe.returncode, 2)
            self.assertFalse(unsafe_out.exists())
            existing_out = root / "existing-output"
            existing_out.mkdir()
            existing = run_command([
                EXPORT, "--raw-jsonl", raw, "--stream-id", "RAW",
                "--source-label", "fixtures/records.jsonl", "--canonical-out", existing_out,
            ])
            self.assertEqual(existing.returncode, 2)
            self.assertIn("already-exists", existing.stdout)

            empty = root / "empty.jsonl"
            empty.write_bytes(b"")
            empty_out = root / "empty-output"
            empty_result = run_command([
                EXPORT, "--raw-jsonl", empty, "--stream-id", "EMPTY",
                "--source-label", "fixtures/empty.jsonl", "--canonical-out", empty_out,
            ])
            self.assertEqual(empty_result.returncode, 0, empty_result.stdout)
            self.assertEqual(read_jsonl(empty_out / "occurrences.jsonl"), [])
            self.assertEqual(read_jsonl(empty_out / "events.jsonl"), [])
            self.assertEqual(read_jsonl(empty_out / "receipts.jsonl"), [])

            duplicate_envelope = root / "duplicate-envelope.jsonl"
            envelope = json_wire(make_occurrence(valid, 1, stream="ENVELOPE"))
            envelope = envelope.replace('"line_number":1',
                                        '"line_number":1,"line_number":1')
            duplicate_envelope.write_text(envelope + "\n", encoding="utf-8", newline="\n")
            duplicate_envelope_out = root / "duplicate-envelope-output"
            envelope_result = run_command([
                EXPORT, "--occurrences-jsonl", duplicate_envelope,
                "--canonical-out", duplicate_envelope_out,
            ])
            self.assertEqual(envelope_result.returncode, 2)
            self.assertIn("malformed-envelope", envelope_result.stdout)
            self.assertFalse(duplicate_envelope_out.exists())

            captured = owner_events.capture_input(raw)
            captured_occurrences = owner_events.occurrences_from_raw_bytes(
                captured.data, "DRIFT", "fixtures/drift.jsonl")
            raw.write_text(json_wire(make_record(
                "DRIFT-E2", "DRIFT-R2", "DRIFT-SESSION", 1,
                "2026-08-08T02:00:00Z", text="changed after capture",
            )) + "\n", encoding="utf-8", newline="\n")
            drift_out = root / "drift-output"
            with self.assertRaisesRegex(
                    owner_events.ContractError, "drift-before-publication"):
                owner_events.publish_bundle(
                    captured_occurrences, drift_out, input_mode="raw-jsonl",
                    captured_input=captured,
                )
            self.assertFalse(drift_out.exists())

            raw.write_bytes(captured.data)
            bound_out = root / "bound-output"
            bound_summary = owner_events.publish_bundle(
                captured_occurrences, bound_out, input_mode="raw-jsonl",
                captured_input=captured,
            )
            self.assertEqual(bound_summary["input"], {
                "path_label": raw.name,
                "bytes": len(captured.data),
                "sha256": hashlib.sha256(captured.data).hexdigest(),
            })

    def test_timestamp_normalization_order_and_invalid_matrix(self):
        rows = [
            make_occurrence(make_record(
                "ORDER-LATE", "ORDER-R2", "ORDER-SESSION", 7,
                "2026-02-06T00:30:00Z", text="later instant physically first"), 1, stream="ORDER"),
            make_occurrence(make_record(
                "ORDER-EARLY", "ORDER-R1", "ORDER-SESSION", 7,
                "2026-02-06T01:00:00+01:00", text="earlier instant physically second"), 2,
                stream="ORDER"),
            make_occurrence(make_record(
                "FRACTION-LATE", "FRACTION-R2", "FRACTION-SESSION", 8,
                "2026-02-06T00:00:00.1000Z", text="fraction one tenth"), 1,
                stream="FRACTION"),
            make_occurrence(make_record(
                "FRACTION-EARLY", "FRACTION-R1", "FRACTION-SESSION", 8,
                "2026-02-06T00:00:00.01Z", text="fraction one hundredth"), 2,
                stream="FRACTION"),
            make_occurrence(make_record(
                "EQUIV", "EQUIV-R1", "EQUIV-SESSION", 1,
                "2026-02-06T00:00:00Z", text="split text"), 1, stream="EQUIV"),
            make_occurrence(make_record(
                "EQUIV", "EQUIV-R2", "EQUIV-SESSION", 1,
                "2026-02-06T01:00:00+01:00", content=[{
                    "type": "attachment", "id": "EQUIV-A1", "name": "split.bin",
                    "mime_type": "application/octet-stream", "byte_count": 4,
                    "sha256": "a" * 64,
                }]), 2, stream="EQUIV"),
        ]
        invalid_values = [
            "2026-W01-1T00:00:00Z", "2026-01-01 00:00:00Z",
            "2026-01-01T00:00:00+01", "2026-01-01T00:00:00+01:00:30",
            "2026-01-01T00:00:00", "2026-02-30T00:00:00Z",
            "2026-01-01T24:00:00Z", "2026-06-30T23:59:60Z",
        ]
        for index, value in enumerate(invalid_values, 1):
            rows.append(make_occurrence(make_record(
                f"INVALID-{index}", f"INVALID-R{index}", "INVALID-SESSION", index,
                value, text="invalid timestamp control"), index, stream=f"INVALID-{index}"))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "occurrences.jsonl"
            out = Path(temporary) / "bundle"
            write_jsonl(source, rows)
            result = run_command([
                EXPORT, "--occurrences-jsonl", source, "--canonical-out", out,
            ])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            occurrences = read_jsonl(out / "occurrences.jsonl")
            events = read_jsonl(out / "events.jsonl")
            receipts = read_jsonl(out / "receipts.jsonl")
            counters = json.loads((out / "counters.json").read_text(encoding="utf-8"))
            validate_packet(occurrences, events, receipts, counters)
            order_ids = [row["event_id"] for row in events if row["event_id"].startswith("ORDER-")]
            self.assertEqual(order_ids, ["ORDER-EARLY", "ORDER-LATE"])
            fraction_ids = [row["event_id"] for row in events if row["event_id"].startswith("FRACTION-")]
            self.assertEqual(fraction_ids, ["FRACTION-EARLY", "FRACTION-LATE"])
            fraction_times = {row["event_id"]: row["timestamp"] for row in events
                              if row["event_id"].startswith("FRACTION-")}
            self.assertEqual(fraction_times["FRACTION-LATE"], "2026-02-06T00:00:00.1Z")
            equivalent = next(row for row in events if row["event_id"] == "EQUIV")
            self.assertEqual(equivalent["timestamp"], "2026-02-06T00:00:00Z")
            self.assertEqual(equivalent["event_class"], "message_with_attachment_and_text")
            invalid_receipts = [row for row in receipts if str(row["event_id"]).startswith("INVALID-")]
            self.assertEqual(len(invalid_receipts), len(invalid_values))
            self.assertTrue(all(row["reason_code"] == "INVALID_TIMESTAMP"
                                for row in invalid_receipts))

    def test_contributor_conflict_fails_without_partial_output(self):
        rows = [
            make_occurrence(make_record(
                "CONFLICT", "CONFLICT-R1", "SESSION-A", 1,
                "2026-08-08T01:00:00Z", text="first contributor"), 1),
            make_occurrence(make_record(
                "CONFLICT", "CONFLICT-R2", "SESSION-B", 1,
                "2026-08-08T01:00:00Z", text="second contributor"), 2),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "occurrences.jsonl"
            out = Path(temporary) / "bundle"
            write_jsonl(source, rows)
            result = run_command([
                EXPORT, "--occurrences-jsonl", source, "--canonical-out", out,
            ])
            self.assertEqual(result.returncode, 2)
            self.assertIn("conflicting-session_id", result.stdout)
            self.assertFalse(out.exists())

            incompatible = [
                make_occurrence(make_record(
                    "CONFLICT", "CONFLICT-R1", "SESSION-A", 1,
                    "2026-08-08T01:00:00Z", text="first contributor",
                    boundary="shared"), 1),
                make_occurrence(make_record(
                    "CONFLICT", "CONFLICT-R2", "SESSION-A", 1,
                    "2026-08-08T01:00:00Z", text="second contributor",
                    record_type="queue_operation", boundary="shared"), 2),
            ]
            incompatible_source = Path(temporary) / "incompatible.jsonl"
            incompatible_out = Path(temporary) / "incompatible-output"
            write_jsonl(incompatible_source, incompatible)
            incompatible_result = run_command([
                EXPORT, "--occurrences-jsonl", incompatible_source,
                "--canonical-out", incompatible_out,
            ])
            self.assertEqual(incompatible_result.returncode, 2)
            self.assertIn("incompatible-classes", incompatible_result.stdout)
            self.assertFalse(incompatible_out.exists())

            duplicate_record = make_record(
                "DUPLICATE", "DUPLICATE-R1", "DUPLICATE-SESSION", 1,
                "2026-08-08T01:00:00Z", text="same durable record")
            duplicates = [
                make_occurrence(duplicate_record, 1, stream="DUPLICATE"),
                make_occurrence(copy.deepcopy(duplicate_record), 2, stream="DUPLICATE"),
            ]
            duplicate_source = Path(temporary) / "duplicates.jsonl"
            duplicate_out = Path(temporary) / "duplicate-output"
            write_jsonl(duplicate_source, duplicates)
            duplicate_result = run_command([
                EXPORT, "--occurrences-jsonl", duplicate_source,
                "--canonical-out", duplicate_out,
            ])
            self.assertEqual(duplicate_result.returncode, 0, duplicate_result.stdout)
            duplicate_receipts = read_jsonl(duplicate_out / "receipts.jsonl")
            self.assertEqual([row["disposition"] for row in duplicate_receipts],
                             ["EMITTED", "DROPPED"])
            self.assertEqual(len(read_jsonl(duplicate_out / "events.jsonl")), 1)

            conflicting_duplicate = copy.deepcopy(duplicates)
            conflicting_record = copy.deepcopy(conflicting_duplicate[1]["record"])
            conflicting_record["text"] = "same durable id, conflicting value"
            conflicting_duplicate[1]["record"] = conflicting_record
            conflicting_duplicate[1]["raw_text"] = json_wire(conflicting_record)
            conflicting_duplicate[1]["raw_line_sha256"] = hashlib.sha256(
                conflicting_duplicate[1]["raw_text"].encode("utf-8")).hexdigest()
            conflicting_source = Path(temporary) / "conflicting-duplicate.jsonl"
            conflicting_out = Path(temporary) / "conflicting-duplicate-output"
            write_jsonl(conflicting_source, conflicting_duplicate)
            conflicting_result = run_command([
                EXPORT, "--occurrences-jsonl", conflicting_source,
                "--canonical-out", conflicting_out,
            ])
            self.assertEqual(conflicting_result.returncode, 2)
            self.assertIn("conflicting-durable-id", conflicting_result.stdout)
            self.assertFalse(conflicting_out.exists())

            attachment_one = {
                "type": "attachment", "id": "SHARED-A1", "name": "one.bin",
                "mime_type": "application/octet-stream", "byte_count": 1,
                "sha256": "a" * 64,
            }
            attachment_two = dict(attachment_one, name="two.bin", sha256="b" * 64)
            attachment_rows = [
                make_occurrence(make_record(
                    "ATTACH-CONFLICT", "ATTACH-R1", "ATTACH-SESSION", 1,
                    "2026-08-08T01:00:00Z", text="attachment text",
                    content=[attachment_one]), 1, stream="ATTACH"),
                make_occurrence(make_record(
                    "ATTACH-CONFLICT", "ATTACH-R2", "ATTACH-SESSION", 1,
                    "2026-08-08T01:00:00Z", text="attachment text",
                    content=[attachment_two]), 2, stream="ATTACH"),
            ]
            attachment_source = Path(temporary) / "attachment-conflict.jsonl"
            attachment_out = Path(temporary) / "attachment-conflict-output"
            write_jsonl(attachment_source, attachment_rows)
            attachment_result = run_command([
                EXPORT, "--occurrences-jsonl", attachment_source,
                "--canonical-out", attachment_out,
            ])
            self.assertEqual(attachment_result.returncode, 2)
            self.assertIn("conflicting-attachment-id", attachment_result.stdout)
            self.assertFalse(attachment_out.exists())

    def test_independent_oracle_rejects_schema_and_paired_timestamp_mutations(self):
        events = read_jsonl(FIXTURES / "expected-events-v2.jsonl")
        receipts = read_jsonl(FIXTURES / "expected-receipts-v2.jsonl")
        occurrences = read_jsonl(FIXTURES / "occurrences-v2.jsonl")
        counters = json.loads((FIXTURES / "expected-counters-v2.json").read_text(encoding="utf-8"))

        invalid_event = copy.deepcopy(events[0])
        invalid_event["extra"] = "not allowed"
        self.assertTrue(validate_event(invalid_event))
        invalid_event = copy.deepcopy(events[0])
        invalid_event["logical_seq"] = True
        self.assertTrue(validate_event(invalid_event))
        invalid_event = copy.deepcopy(events[0])
        invalid_event["provenance"]["source_locators"][0] = "C:/private/source.jsonl#L1"
        self.assertTrue(validate_event(invalid_event))
        invalid_receipt = copy.deepcopy(receipts[0])
        invalid_receipt["receipt_id"] = 7
        self.assertTrue(validate_receipt(invalid_receipt))
        invalid_receipt = copy.deepcopy(receipts[0])
        invalid_receipt["source_locator"] = 9
        self.assertTrue(validate_receipt(invalid_receipt))
        invalid_counters = copy.deepcopy(counters)
        invalid_counters["extra"] = 1
        self.assertTrue(validate_counters(invalid_counters))
        invalid_counters = copy.deepcopy(counters)
        invalid_counters["by_reason_code"]["UNKNOWN_REASON"] = 1
        self.assertTrue(validate_counters(invalid_counters))

        for value in (
            "2026-W01-1T00:00:00Z", "2026-01-01 00:00:00Z",
            "2026-01-01T00:00:00+01", "2026-01-01T00:00:00+01:00:30",
        ):
            observed = copy.deepcopy(events)
            expected = copy.deepcopy(events)
            observed[0]["timestamp"] = value
            expected[0]["timestamp"] = value
            with self.assertRaises(OracleError):
                validate_packet(
                    occurrences, observed, receipts, counters,
                    expected_events=expected, expected_receipts=receipts,
                    expected_counters=counters,
                )

    def test_prebuilt_raw_record_binding_is_recursively_json_type_exact(self):
        raw_record = {
            "outer": {"flag": True, "items": [7, {"flag": False}]},
        }
        occurrence = make_occurrence(raw_record, 1, stream="TYPE-EXACT")
        contradictory = copy.deepcopy(occurrence)
        contradictory["record"]["outer"]["flag"] = 1
        contradictory["record"]["outer"]["items"][1]["flag"] = 0

        self.assertEqual(owner_events.validate_occurrence(copy.deepcopy(occurrence)), occurrence)
        self.assertEqual(oracle_validate_occurrence(copy.deepcopy(occurrence)), [])
        with self.assertRaisesRegex(owner_events.ContractError,
                                    "occurrence:object-record-mismatch"):
            owner_events.validate_occurrence(contradictory)
        self.assertIn("occurrence:object-mismatch",
                      oracle_validate_occurrence(contradictory))

    def test_jsonl_uses_only_cr_lf_record_separators(self):
        literal = "first\u0085second\u2028third\u2029fourth"
        record = make_record("UNICODE-E1", "UNICODE-R1", "UNICODE-S1", 1,
                             "2026-08-08T01:00:00Z", text=literal)
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        occurrence = make_occurrence(record, 1, stream="UNICODE")
        occurrence["raw_text"] = raw
        occurrence["raw_line_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        envelope = json.dumps(
            occurrence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        self.assertEqual(owner_events.split_jsonl_records(envelope + "\r\n"), [envelope])
        self.assertEqual(oracle_split_jsonl_records(envelope + "\r\n"), [envelope])
        loaded_occurrences = owner_events.read_occurrences_bytes(
            (envelope + "\r\n").encode("utf-8"))
        self.assertEqual(len(loaded_occurrences), 1)
        self.assertEqual(loaded_occurrences[0]["record"]["text"], literal)

        event = make_event("UNICODE-E1", "UNICODE-S1", "2026-08-08T01:00:00Z",
                           [literal])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unicode-events.jsonl"
            path.write_text(
                json.dumps(event, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n",
                encoding="utf-8", newline="",
            )
            self.assertEqual(read_jsonl(path), [event])
            spec = importlib.util.spec_from_file_location("map_history_unicode", MAP_HISTORY)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            items = module.load_canonical_events([path], Counter())
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["text"], literal)

    def test_close_precedence_is_byte_exact_in_candidate_and_oracle(self):
        spec = importlib.util.spec_from_file_location("map_history_precedence", MAP_HISTORY)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        close = {"session_id": "S1", "text": "keep exact bytes"}
        exact = {"session_id": "S1", "text": "keep exact bytes"}
        distinct = {"session_id": "S1", "text": "  Keep  EXACT   bytes  "}

        self.assertEqual(module._exact_item_key(close), module._exact_item_key(exact))
        self.assertNotEqual(module._exact_item_key(close), module._exact_item_key(distinct))
        self.assertEqual(oracle_exact_precedence_key("S1", close["text"]),
                         oracle_exact_precedence_key("S1", exact["text"]))
        self.assertNotEqual(oracle_exact_precedence_key("S1", close["text"]),
                            oracle_exact_precedence_key("S1", distinct["text"]))
        close_keys = {module._exact_item_key(close)}
        kept = [item for item in (exact, distinct)
                if module._exact_item_key(item) not in close_keys]
        self.assertEqual(kept, [distinct])

    def test_declared_event_admissibility_matches_runtime_semantics(self):
        schema = json.loads((ROOT / "schemas" / "production-event.schema.json").read_text(
            encoding="utf-8"))
        attachment_a = {
            "id": "A1", "name": "first.bin", "mime_type": "application/octet-stream",
            "byte_count": 1, "sha256": "a" * 64,
        }
        attachment_b = {
            "id": "A1", "name": "second.bin", "mime_type": "application/octet-stream",
            "byte_count": 2, "sha256": "b" * 64,
        }
        duplicate_attachment = make_event(
            "SCHEMA-A", "SCHEMA-S", "2026-08-08T01:00:00Z", ["text"],
            event_class="message_with_attachment_and_text",
            attachments=[attachment_a, attachment_b],
        )
        lineage = make_event("SCHEMA-L", "SCHEMA-S", "2026-08-08T01:00:01Z", ["text"])
        lineage["source_record_ids"].append("SCHEMA-L-R2")
        derivation = make_event("SCHEMA-D", "SCHEMA-S", "2026-08-08T01:00:02Z", ["text"])
        derivation["provenance"]["derivation"] = "MULTI_RECORD_ASSEMBLY"

        cases = (
            (duplicate_attachment, "event:duplicate-attachment-id",
             "attachment:1:duplicate-id", "x-uniqueBy:id"),
            (lineage, "event:source-lineage-cardinality",
             "record-lineage-cardinality", "x-equalCardinality"),
            (derivation, "event:derivation-cardinality",
             "derivation-cardinality", "x-derivationCardinality"),
        )
        for event, runtime_error, oracle_token, schema_token in cases:
            with self.subTest(runtime_error=runtime_error):
                with self.assertRaisesRegex(owner_events.ContractError, runtime_error):
                    owner_events.validate_event(event)
                self.assertTrue(oracle_errors := validate_event(event))
                self.assertTrue(any(oracle_token in error for error in oracle_errors))
                self.assertTrue(any(schema_token in error
                                    for error in validate_schema(event, schema)))

    def test_exponent_overflow_is_accounted_or_structured(self):
        first = make_record("OVERFLOW-E1", "OVERFLOW-R1", "OVERFLOW-S", 1,
                            "2026-08-08T01:00:00Z", text="before")
        third = make_record("OVERFLOW-E3", "OVERFLOW-R3", "OVERFLOW-S", 3,
                            "2026-08-08T01:00:02Z", text="after")
        overflow = json_wire(make_record(
            "OVERFLOW-E2", "OVERFLOW-R2", "OVERFLOW-S", 2,
            "2026-08-08T01:00:01Z", text="overflow"))[:-1] + ',"extra":1e9999}'
        raw_bytes = (json_wire(first) + "\n" + overflow + "\n" + json_wire(third) + "\n").encode(
            "utf-8")
        occurrences = owner_events.occurrences_from_raw_bytes(
            raw_bytes, "OVERFLOW", "fixtures/overflow.jsonl")
        self.assertEqual([row["parse_status"] for row in occurrences],
                         ["OBJECT", "MALFORMED_JSON", "OBJECT"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_path = root / "raw.jsonl"
            raw_path.write_bytes(raw_bytes)
            out = root / "raw-bundle"
            result = run_command([
                EXPORT, "--raw-jsonl", raw_path, "--stream-id", "OVERFLOW",
                "--source-label", "fixtures/overflow.jsonl", "--canonical-out", out,
            ])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(len(read_jsonl(out / "occurrences.jsonl")), 3)
            receipts = read_jsonl(out / "receipts.jsonl")
            self.assertEqual(receipts[1]["reason_code"], "MALFORMED_JSON")

            oracle_path = root / "oracle-overflow.jsonl"
            oracle_path.write_text(overflow + "\n", encoding="utf-8", newline="")
            with self.assertRaisesRegex(OracleError, "malformed:1"):
                read_jsonl(oracle_path)

            valid_occurrence = make_occurrence(first, 1, stream="PREBUILT-OVERFLOW")
            record_wire = json_wire(first)
            invalid_record_wire = record_wire[:-1] + ',"extra":1e9999}'
            envelope = json_wire(valid_occurrence).replace(
                '"record":' + record_wire, '"record":' + invalid_record_wire, 1)
            self.assertIn("1e9999", envelope)
            prebuilt = root / "prebuilt.jsonl"
            prebuilt.write_text(envelope + "\n", encoding="utf-8", newline="")
            prebuilt_out = root / "prebuilt-bundle"
            failed = run_command([
                EXPORT, "--occurrences-jsonl", prebuilt,
                "--canonical-out", prebuilt_out,
            ])
            self.assertEqual(failed.returncode, 2, failed.stderr + failed.stdout)
            self.assertEqual(read_summary(failed.stdout)["exit_code"], 2)
            self.assertFalse(prebuilt_out.exists())

    def test_complete_independent_mutation_matrix(self):
        result = run_matrix(FIXTURES)
        self.assertEqual(result["control_result"], "PASS")
        self.assertEqual(result["cases"], 52)
        self.assertEqual(result["predecessor_cases_replayed"], 42)
        self.assertEqual(result["additional_cases"], 10)
        self.assertEqual(result["failed"], 0, json.dumps(result, indent=2))
        self.assertEqual(result["result"], "PASS")

    def test_declared_schemas_are_structurally_strict(self):
        schemas = ROOT / "schemas"
        source = json.loads((schemas / "source-occurrence.schema.json").read_text(encoding="utf-8"))
        event = json.loads((schemas / "production-event.schema.json").read_text(encoding="utf-8"))
        receipt = json.loads((schemas / "disposition-receipt.schema.json").read_text(encoding="utf-8"))
        counters = json.loads((schemas / "run-counters.schema.json").read_text(encoding="utf-8"))
        self.assertFalse(source["additionalProperties"])
        self.assertEqual(len(source["oneOf"]), 2)
        self.assertFalse(event["additionalProperties"])
        self.assertIn("pattern", event["properties"]["timestamp"])
        self.assertEqual(len(event["required"]), 14)
        self.assertEqual(len(event["allOf"]), 8)
        self.assertFalse(receipt["additionalProperties"])
        self.assertEqual(receipt["properties"]["receipt_id"]["minLength"], 1)
        self.assertEqual(len(receipt["allOf"]), 5)
        self.assertFalse(counters["additionalProperties"])
        self.assertIn("propertyNames", counters["properties"]["by_reason_code"])

    def test_map_history_canonical_precedence_and_strict_failure(self):
        catalog_rows = [json.loads(line) for line in
                        (MAP_FIXTURES / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
                        if line]
        close_row = next(row for row in catalog_rows if row.get("entry_source") == "close")
        close_goal = next(goal for goal in close_row["goals"] if isinstance(goal, dict))
        close_text = close_goal.get("goal") or close_goal.get("text")
        normalized_collision = "  " + close_text.swapcase().replace(" ", "  ") + "  "
        attachment = {
            "id": "MAP-A1", "name": "map.bin", "mime_type": "application/octet-stream",
            "byte_count": 3, "sha256": "b" * 64,
        }
        canonical = [
            make_event("MAP-C1", "P1", "2026-06-11T10:00:00Z",
                       ["add another chest pin for the topic classifier instead of fixing the keyword"]),
            make_event("MAP-C2", "CANON-A", "2026-06-12T10:00:00Z",
                       ["canonical recurrence preserves genuine repeated owner request"]),
            make_event("MAP-C3", "CANON-B", "2026-06-13T10:00:00Z",
                       ["canonical recurrence preserves genuine repeated owner request"]),
            make_event("MAP-C4", "CANON-POST", "2026-07-13T10:00:00Z",
                       ["post cutover canonical owner request remains available for clustering"]),
            make_event("MAP-C5", "CANON-ATT", "2026-06-14T10:00:00Z", [],
                       event_class="attachment_only_message", attachments=[attachment]),
            make_event("MAP-C6", close_row["session_id"],
                       str(close_row["date"])[:10] + "T12:00:00Z", [close_text]),
            make_event("MAP-C7", "CANON-NOISE-A", "2026-06-15T10:00:00Z",
                       ["Please fix the Point-of-Performance dashboard recurrence"]),
            make_event("MAP-C8", "CANON-NOISE-B", "2026-06-16T10:00:00Z",
                       ["Please fix the Point-of-Performance dashboard recurrence"]),
            make_event("MAP-C9", "CANON-EXACT", "2026-06-17T10:00:00Z",
                       ["  leading  spaces", "second\nline  "]),
            make_event("MAP-C10", close_row["session_id"],
                       "2026-06-30T12:00:01Z",
                       [normalized_collision]),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events_path = root / "canonical.jsonl"
            out = root / "MAP-HISTORY.md"
            write_jsonl(events_path, canonical)
            command = [
                MAP_HISTORY, "--catalog", MAP_FIXTURES / "catalog.jsonl",
                "--prompts", MAP_FIXTURES / "prompts.jsonl",
                "--quotes", MAP_FIXTURES / "quotes.jsonl",
                "--names", MAP_FIXTURES / "names.json",
                "--seeds", MAP_FIXTURES / "seeds.json",
                "--memory-dir", MAP_FIXTURES / "memory",
                "--legacy-close", MAP_FIXTURES / "legacy-close",
                "--session-notes", MAP_FIXTURES / "session-notes",
                "--canonical-events", events_path,
                "--out", out, "--min-sessions", "2", "--top", "20",
                "--now", "2026-08-08", "--require-pre-july",
            ]
            result = run_command(command)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            summary = read_summary(result.stdout)
            self.assertEqual(summary["canonical_events"], 10)
            self.assertEqual(summary["canonical_text_events"], 9)
            self.assertEqual(summary["canonical_attachment_only"], 1)
            self.assertEqual(summary["canonical_post_cutover"], 1)
            self.assertEqual(summary["canonical_superseded_by_close"], 1)
            self.assertEqual(summary["legacy_superseded_by_canonical"], 1)
            output_text = out.read_text(encoding="utf-8").lower()
            self.assertIn("canonical recurrence preserves genuine repeated owner request",
                          output_text)
            self.assertIn("point-of-performance dashboard recurrence", output_text)

            spec = importlib.util.spec_from_file_location("map_history_under_test", MAP_HISTORY)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            map_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(map_module)
            loaded = map_module.load_canonical_events([events_path], Counter())
            exact_item = next(item for item in loaded if item["ref"] == "MAP-C9")
            self.assertEqual(exact_item["text"], "  leading  spaces\nsecond\nline  ")

            invalid = copy.deepcopy(canonical[0])
            invalid["extra"] = "schema violation"
            invalid_path = root / "invalid.jsonl"
            invalid_out = root / "INVALID.md"
            write_jsonl(invalid_path, [invalid])
            invalid_command = list(command)
            invalid_command[invalid_command.index(events_path)] = invalid_path
            invalid_command[invalid_command.index(out)] = invalid_out
            failed = run_command(invalid_command)
            self.assertEqual(failed.returncode, 2)
            self.assertFalse(invalid_out.exists())

            unsafe = copy.deepcopy(canonical[0])
            unsafe["provenance"]["source_locators"][0] = "C:/private/source.jsonl#L1"
            unsafe_path = root / "unsafe.jsonl"
            unsafe_out = root / "UNSAFE.md"
            write_jsonl(unsafe_path, [unsafe])
            unsafe_command = list(command)
            unsafe_command[unsafe_command.index(events_path)] = unsafe_path
            unsafe_command[unsafe_command.index(out)] = unsafe_out
            unsafe_result = run_command(unsafe_command)
            self.assertEqual(unsafe_result.returncode, 2)
            self.assertFalse(unsafe_out.exists())

            duplicate_key_path = root / "duplicate-key.jsonl"
            duplicate_key_wire = json_wire(canonical[0]).replace(
                '"event_id":"MAP-C1"', '"event_id":"MAP-C1","event_id":"MAP-C1"')
            duplicate_key_path.write_text(duplicate_key_wire + "\n",
                                          encoding="utf-8", newline="\n")
            duplicate_key_out = root / "DUPLICATE-KEY.md"
            duplicate_key_command = list(command)
            duplicate_key_command[duplicate_key_command.index(events_path)] = duplicate_key_path
            duplicate_key_command[duplicate_key_command.index(out)] = duplicate_key_out
            duplicate_key_result = run_command(duplicate_key_command)
            self.assertEqual(duplicate_key_result.returncode, 2)
            self.assertFalse(duplicate_key_out.exists())


if __name__ == "__main__":
    unittest.main()
