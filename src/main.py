"""CLI entry point for the IT Access Requests intake application."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .extractor import Extractor
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .queue import save_queue, display_queue, build_queue
from .state import StateManager
from .utils import compute_sha256, read_json_file, setup_logging, write_json_file
from .validator import load_schema, validate_extraction


def get_request_files(requests_dir: str) -> list[Path]:
    """Return sorted list of .json files in the requests directory."""
    dir_path = Path(requests_dir)
    if not dir_path.exists():
        raise FileNotFoundError(f"Requests directory not found: {dir_path}")
    return sorted(dir_path.glob("*.json"))


def cmd_intake(args: argparse.Namespace) -> None:
    """Process request files through extraction and policy routing."""
    config = load_config(args.config)
    logger = setup_logging(args.log_level)

    schema = load_schema(config["extraction"]["schema_path"])
    use_mock = config.get("response_source", "mock") == "mock"
    processing_settings = {
        "model_id": config["model"]["model_id"],
        "response_source": "mock" if use_mock else "provider",
        "temperature": config["model"].get("temperature"),
        "max_tokens": config["model"].get("max_tokens"),
    }

    state = StateManager(
        deliverables_dir=config["deliverables_dir"],
        processing_settings=processing_settings,
    )
    extractor = Extractor(config, schema, use_mock=use_mock)

    request_files = get_request_files(config["requests_dir"])
    all_doc_ids = [f.name for f in request_files]

    # Filter to specific file if provided
    if args.document_id:
        request_files = [f for f in request_files if f.name == args.document_id]
        if not request_files:
            logger.error("Document not found: %s", args.document_id)
            sys.exit(1)

    processed = 0
    skipped = 0
    errors = 0

    for file_path in request_files:
        doc_id = file_path.name
        sha256 = compute_sha256(file_path)

        # Check if unchanged
        unchanged, existing = state.is_unchanged(doc_id, sha256)
        if unchanged and existing:
            logger.info("Skipping %s (unchanged, reusing prior result)", doc_id)
            skipped += 1
            continue

        # Read message
        data = read_json_file(file_path)
        message = data.get("message", "")

        # Extract
        logger.info("Processing %s...", doc_id)
        record = extractor.extract_with_metadata(doc_id, message, sha256)
        state.save_record(record)

        if record["status"] == "error":
            errors += 1
            logger.error("Failed: %s — %s", doc_id, record["error"])
            logger.info("Retry with: %s", state.get_retry_command(doc_id))
        else:
            processed += 1
            logger.info("Done: %s → %s / %s / %s", doc_id, record["status"], record["owner"], record["next_action"])

    # Build and save queue
    save_queue(state, all_doc_ids, str(Path(config["deliverables_dir"]) / "queue.json"))

    logger.info("\nIntake complete: %d processed, %d skipped (unchanged), %d errors", processed, skipped, errors)


def cmd_queue(args: argparse.Namespace) -> None:
    """Display the current review queue."""
    config = load_config(args.config)
    state = StateManager(deliverables_dir=config["deliverables_dir"])
    request_files = get_request_files(config["requests_dir"])
    all_doc_ids = [f.name for f in request_files]

    queue = build_queue(state, all_doc_ids)
    print(display_queue(queue))


def cmd_status(args: argparse.Namespace) -> None:
    """Show processing status."""
    config = load_config(args.config)
    logger = setup_logging(args.log_level)
    state = StateManager(deliverables_dir=config["deliverables_dir"])
    request_files = get_request_files(config["requests_dir"])
    all_doc_ids = [f.name for f in request_files]

    summary = state.get_status_summary(all_doc_ids)
    print(json.dumps(summary, indent=2))

    # Show error details
    current = state.get_current_records()
    errors = [(doc_id, rec) for doc_id, rec in current.items() if rec.get("status") == "error"]
    if errors:
        print("\nErrors:")
        for doc_id, rec in errors:
            print(f"  {doc_id}: {rec.get('error')}")
            print(f"    Retry: {state.get_retry_command(doc_id)}")


def cmd_retry(args: argparse.Namespace) -> None:
    """Retry a failed extraction."""
    config = load_config(args.config)
    logger = setup_logging(args.log_level)
    schema = load_schema(config["extraction"]["schema_path"])
    use_mock = config.get("response_source", "mock") == "mock"

    state = StateManager(deliverables_dir=config["deliverables_dir"])
    extractor = Extractor(config, schema, use_mock=use_mock)

    file_path = Path(config["requests_dir"]) / args.document_id
    if not file_path.exists():
        logger.error("Document not found: %s", args.document_id)
        sys.exit(1)

    sha256 = compute_sha256(file_path)
    data = read_json_file(file_path)
    message = data.get("message", "")

    logger.info("Retrying %s...", args.document_id)
    record = extractor.extract_with_metadata(args.document_id, message, sha256)
    state.save_record(record)

    if record["status"] == "error":
        logger.error("Retry failed: %s", record["error"])
        sys.exit(1)
    else:
        logger.info("Retry succeeded: %s → %s / %s / %s", args.document_id, record["status"], record["owner"], record["next_action"])

    # Rebuild queue
    request_files = get_request_files(config["requests_dir"])
    all_doc_ids = [f.name for f in request_files]
    save_queue(state, all_doc_ids, str(Path(config["deliverables_dir"]) / "queue.json"))


def cmd_test(args: argparse.Namespace) -> None:
    """Run the test suite."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v"],
        cwd=str(Path(__file__).parent.parent),
    )
    sys.exit(result.returncode)


def cmd_accuracy(args: argparse.Namespace) -> None:
    """Run accuracy check against expected fields."""
    config = load_config(args.config)
    logger = setup_logging(args.log_level)
    schema = load_schema(config["extraction"]["schema_path"])
    use_mock = config.get("response_source", "mock") == "mock"

    state = StateManager(deliverables_dir=config["deliverables_dir"])
    extractor = Extractor(config, schema, use_mock=use_mock)

    request_files = get_request_files(config["requests_dir"])
    all_doc_ids = [f.name for f in request_files]

    # Also process the paraphrase
    paraphrase_path = Path(config.get("data_dir", "data")) / "paraphrase.json"
    if paraphrase_path.exists():
        all_doc_ids.append(paraphrase_path.name)
        request_files.append(paraphrase_path)

    print("Accuracy Report")
    print("=" * 80)

    matches = 0
    mismatches = 0
    for file_path in request_files:
        doc_id = file_path.name
        sha256 = compute_sha256(file_path)
        data = read_json_file(file_path)
        message = data.get("message", "")

        record = extractor.extract_with_metadata(doc_id, message, sha256)
        extraction = record.get("extraction")

        if extraction:
            validation = validate_extraction(extraction, schema)
            if validation["valid"]:
                print(f"  {doc_id}: PASS (status={record['status']}, owner={record['owner']})")
                matches += 1
            else:
                print(f"  {doc_id}: FAIL — {validation['schema_errors']}")
                mismatches += 1
        else:
            print(f"  {doc_id}: ERROR — {record.get('error')}")
            mismatches += 1

    print(f"\nTotal: {matches} passed, {mismatches} failed")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="it-access-requests",
        description="IT Access Requests intake and review queue management.",
    )
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # intake
    p_intake = subparsers.add_parser("intake", help="Process request files through extraction")
    p_intake.add_argument("--document-id", default=None, help="Process only this document (filename)")

    # queue
    p_queue = subparsers.add_parser("queue", help="Display the current review queue")

    # status
    p_status = subparsers.add_parser("status", help="Show processing status")

    # retry
    p_retry = subparsers.add_parser("retry", help="Retry a failed extraction")
    p_retry.add_argument("document_id", help="Document ID (filename) to retry")

    # test
    p_test = subparsers.add_parser("test", help="Run the test suite")

    # accuracy
    p_accuracy = subparsers.add_parser("accuracy", help="Run accuracy check")

    args = parser.parse_args()

    if args.command == "intake":
        cmd_intake(args)
    elif args.command == "queue":
        cmd_queue(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "retry":
        cmd_retry(args)
    elif args.command == "test":
        cmd_test(args)
    elif args.command == "accuracy":
        cmd_accuracy(args)


if __name__ == "__main__":
    main()
