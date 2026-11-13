"""Published snapshots and independent source-grounded accuracy accounting."""
import hashlib
import json
import os
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator, ValidationError

from .extraction import ROOT, SCHEMA_PATH
from .runner import WriterLock
from .state import StateStore


class ArtifactError(ValueError):
    pass


def _publish(path, raw):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary, path)


def export_snapshot(database, output):
    output = Path(output)
    with WriterLock(Path(database)), StateStore(Path(database), read_only=True) as store, store.snapshot():
        rows = store.queue()
        generation = uuid.uuid4().hex
        pending = [row["document_id"] for row in rows if row["state"] in ("pending", "running")]
        records = [{**row, "generation_id": generation} for row in rows if row["state"] not in ("pending", "running")]
        keys = ("document_id", "source_sha256", "attempt_id", "generation_id", "status", "owner", "next_action", "error", "response_source")
        queue = [{key: record[key] for key in keys} for record in records]
        files = {
            "extractions.jsonl": "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode("utf-8"),
            "queue.json": (json.dumps(queue, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        }
        manifest = {"generation_id": generation, "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
                    "pending_documents": pending, "response_sources": sorted({record["response_source"] for record in records})}
        output.mkdir(parents=True, exist_ok=True)
        for name, raw in files.items():_publish(output / name, raw)
        _publish(output / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        directory = os.open(output, os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    return manifest


def verify_export(output):
    output = Path(output)
    try:
        manifest_bytes = (output / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        raw_files = {name: (output / name).read_bytes() for name in ("extractions.jsonl", "queue.json")}
        if (output / "manifest.json").read_bytes() != manifest_bytes:raise ArtifactError("Publication changed during verification; retry verification.")
        for name, raw in raw_files.items():
            if hashlib.sha256(raw).hexdigest() != manifest["files"][name]:raise ArtifactError("Snapshot file hash mismatch; rerun export.")
        records = [json.loads(line) for line in raw_files["extractions.jsonl"].decode().splitlines() if line.strip()]
        queue = json.loads(raw_files["queue.json"])
        by_id = {r["document_id"]: r for r in records}
        queue_ids = [r["document_id"] for r in queue]
        if len(by_id) != len(records) or len(set(queue_ids)) != len(queue_ids) or set(queue_ids) != set(by_id):
            raise ArtifactError("Duplicate or missing document in snapshot pair.")
        validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))
        for row in queue:
            record = by_id[row["document_id"]]
            for key in ("document_id", "source_sha256", "attempt_id", "generation_id", "status", "owner", "next_action", "error", "response_source"):
                if row[key] != record[key]:raise ArtifactError("Queue/extraction source, attempt or routing mismatch.")
            if record["generation_id"] != manifest["generation_id"]:raise ArtifactError("Snapshot generation mismatch.")
            if record["status"] == "error":
                if record["extraction"] is not None or not isinstance(record["error"], dict):raise ArtifactError("Invalid error record.")
            else:
                if record["status"] not in ("review", "needs-information") or record["error"] is not None:raise ArtifactError("Invalid human review record.")
                validator.validate(record["extraction"])
        if set(manifest["pending_documents"]) & set(by_id):raise ArtifactError("Pending/current snapshot membership disagrees.")
        return manifest
    except ArtifactError:
        raise
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as error:
        raise ArtifactError("Export is incomplete or invalid; rerun export and verification.") from error


def score(expected_path, actual_path):
    expected = json.loads(Path(expected_path).read_text(encoding="utf-8"))
    documents = expected["documents"]
    fields = expected["scalar_fields"]
    schema = json.loads(SCHEMA_PATH.read_text())
    scalars = {name for name, definition in schema["properties"].items() if definition["type"] != "array"}
    ids = {doc["document_id"] for doc in documents}
    if len(ids) != len(documents) or set(fields) != scalars or len(fields) != len(scalars):raise ArtifactError("Expected data has duplicate documents or incompatible scalar fields.")
    validator = Draft202012Validator(schema)
    actual = {};duplicates = set();errors = [];mismatches = [];routing_mismatches = []
    for line_number, line in enumerate(Path(actual_path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():continue
        try:
            record = json.loads(line)
            identity = record["document_id"]
            if not isinstance(identity, str):raise ValueError("invalid identity")
            if identity in actual:duplicates.add(identity)
            actual[identity] = record
        except (ValueError, KeyError, TypeError):errors.append(f"Invalid actual record at line {line_number}.")
    for identity in sorted(duplicates):errors.append(f"Duplicate actual document: {identity}")
    for identity in sorted(set(actual) - ids):errors.append(f"Unexpected actual document: {identity}")
    correct = routing_correct = evidence_correct = issues_correct = 0
    configurations = {}
    for doc in documents:
        identity = doc["document_id"];record = actual.get(identity)
        valid = record is not None and identity not in duplicates
        if valid:
            valid = record.get("status") in ("review", "needs-information") and record.get("error") is None and record.get("source_sha256") == doc["source_sha256"]
            if valid:
                try:validator.validate(record.get("extraction"))
                except ValidationError:valid = False
        extracted = record.get("extraction") if valid else None
        if not valid:errors.append(f"Missing, failed, duplicate, invalid or wrong-source extraction: {identity}")
        for field in fields:
            expected_value = doc["fields"][field]
            if valid and extracted[field] == expected_value:correct += 1
            else:
                mismatches.append({"document_id":identity,"field":field,"expected":expected_value,
                                   "actual":extracted.get(field) if valid else "<invalid or missing extraction>",
                                   "evidence":doc.get("supporting_evidence",{}).get(field,[]),"classification":"unreviewed"})
        if valid and all(record.get(key) == doc[key] for key in ("status", "owner", "next_action")):
            routing_correct += 1
        else:routing_mismatches.append(identity)
        if valid:
            message = json.loads((ROOT / doc["source_path"]).read_bytes())["message"]
            quotes = extracted["supporting_quotes"]
            evidence_ok = bool(quotes) and all(quote and quote in message for quote in quotes)
            evidence_ok = evidence_ok and all(any(required in quote for quote in quotes) for values in doc.get("supporting_evidence",{}).values() for required in values)
            evidence_correct += int(evidence_ok)
            issues_correct += int(all(any(issue.startswith(code+":") and issue[len(code)+1:].strip() for issue in extracted["issues"]) for code in doc["required_issue_codes"]))
        if record is not None:
            settings = record.get("settings",{})
            configuration = {key:settings[key] for key in ("provider","endpoint","model","prompt_sha256","schema_sha256","policy_sha256","routing_version","generation") if key in settings}
            configuration["response_source"] = record.get("response_source", "unspecified")
            configurations[json.dumps(configuration,sort_keys=True)] = configuration
    return {"correct":correct,"total":len(documents)*len(fields),"routing_correct":routing_correct,"routing_total":len(documents),
            "evidence_correct":evidence_correct,"issues_correct":issues_correct,"errors":errors,"mismatches":mismatches,
            "routing_mismatches":routing_mismatches,"configurations":list(configurations.values())}


def write_accuracy(expected_path, actual_path, output):
    result = score(expected_path, actual_path)
    expected = json.loads(Path(expected_path).read_text())
    lines = ["# Accuracy and source-grounded expected facts", "",
             "Observation: " + ("mock responses only; no live model accuracy measured." if all(c["response_source"] == "mock" for c in result["configurations"]) else "provider responses; inspect per-record provenance and configurations below."), "",
             f"Scalar exact match: {result['correct']}/{result['total']}. Routing tuples: {result['routing_correct']}/{result['routing_total']}.",
             f"Source evidence coverage: {result['evidence_correct']}/{result['routing_total']}; required issue coverage: {result['issues_correct']}/{result['routing_total']}.", "",
             "Compare the 12 decoded scalar fields, including explicit null, by exact equality with no case folding, trimming or synonyms. Each missing/failed/invalid/duplicate/wrong-source document scores zero of 12; invalid or absent keys never equal null. Extra IDs and malformed lines are errors and do not increase the denominator. Routing compares status, owner and next_action as one tuple per document. Quotes and issue coverage are separate document-level checks. Expected values and evidence were selected from source messages and office policy before application outputs. Mock success measures pipeline behavior, not live model accuracy.", "",
             f"Inputs: expected={expected_path}; actual={actual_path}.", "", "## Configurations", "", "```json",json.dumps(result["configurations"],indent=2),"```", "", "## Expected fields and source evidence", "",
             "Missing/conflicting values follow office-policy Extracted facts; routing follows Review queue rules 1–5 in order. Relative dates stay null; explicit corrections replace earlier values; managers are contacts, not approval.", ""]
    for doc in expected["documents"]:
        lines.extend(["### " + doc["document_id"], "", "Source: " + doc["source_path"], "", "```json",json.dumps({"fields":doc["fields"],"evidence":doc["supporting_evidence"],"required_issue_codes":doc["required_issue_codes"],"status":doc["status"],"owner":doc["owner"],"next_action":doc["next_action"]},indent=2,ensure_ascii=False),"```", ""])
    lines.extend(["## Factual-error review", "", "Mismatches below require human classification as invented facts, missed facts, wrong corrections/alternatives, or faithful alternative wording. Unreviewed is not a claim of factual correctness; exact-match mismatches remain mismatches. No automated judge assigns factual truth.", "", "```json",json.dumps({"errors":result["errors"],"mismatches":result["mismatches"],"routing_mismatches":result["routing_mismatches"]},indent=2,ensure_ascii=False),"```", ""])
    output = Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    _publish(output, "\n".join(lines).encode("utf-8"))
    return result
