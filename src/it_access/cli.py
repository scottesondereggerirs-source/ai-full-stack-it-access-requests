"""Operator commands for intake, inspection and restart recovery."""

import argparse
import json
import math
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from .providers import ProviderConfig, ProviderError
from .runner import BusyError, WriterLock, capture_settings, run_jobs
from .state import StateError, StateStore


def _parser():
    parser = argparse.ArgumentParser(prog="it-access", description="Prepare Northstar access requests for human review.")
    parser.add_argument("--db", type=Path, default=Path("state/intake.sqlite3"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("intake", "queue", "show", "resume", "retry", "refresh", "export", "verify-export", "accuracy", "serve"):
        command = subparsers.add_parser(name)
        command.add_argument("--db", type=Path, default=argparse.SUPPRESS)
        if name in ("export", "verify-export"):
            command.add_argument("--output", type=Path, default=Path("deliverables"))
        if name == "accuracy":
            command.add_argument("--expected", type=Path, required=True)
            command.add_argument("--actual", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
        if name in ("intake", "refresh", "serve"):
            command.add_argument("--provider", choices=("mock", "openai"), default="mock")
            command.add_argument("--model", default="gpt-4o-2024-08-06")
            command.add_argument("--endpoint", default="https://api.openai.com/v1")
            command.add_argument("--timeout", type=float, default=60.0)
            command.add_argument("--workers", type=int, choices=(1, 2), default=2)
            command.add_argument("--min-interval", type=float, default=1.0)
            command.add_argument("--mock-dir", type=Path, default=Path("tests/mock_responses"))
        if name == "serve":
            command.add_argument("--port", type=int, default=8765)
            command.add_argument("--input-dir", type=Path, default=Path("materials/requests"))
        if name == "intake":
            command.add_argument("input_directory", type=Path)
        if name == "refresh":
            command.add_argument("--document")
        if name in ("show", "retry"):
            command.add_argument("document_id")
        if name == "show":
            command.add_argument("--history", action="store_true")
        if name in ("show", "queue"):
            command.add_argument("--json", action="store_true")
        if name == "queue":
            command.add_argument("--status", choices=("review", "needs-information", "error", "pending", "processing"))
    return parser


def _validate_processing(args):
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise StateError("Timeout must be a positive finite number.")
    if not math.isfinite(args.min_interval) or args.min_interval < 0:
        raise StateError("Start interval must be a finite nonnegative number.")
    try:
        endpoint = urlsplit(args.endpoint)
        if endpoint.scheme != "https" or not endpoint.netloc or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise StateError("Endpoint must be an HTTPS URL without credentials, query values or a fragment.")
        endpoint.port
    except ValueError as error:
        raise StateError("Endpoint is not a valid HTTPS service address.") from error
    if not args.model.strip():
        raise StateError("Model must be a nonempty identifier.")
    return ProviderConfig(provider=args.provider, model=args.model, endpoint=args.endpoint.rstrip("/"), timeout_seconds=args.timeout)


def _show_rows(rows):
    print("document_id\tstatus\towner\tnext_action\tattempt_id\tsource_sha256")
    for row in rows:
        print(f"{row['document_id']}\t{row['status']}\t{row['owner']}\t{row['next_action']}\t{row['attempt_id']}\t{row['source_sha256']}")
        if row["error"] is not None:
            print(row["error"]["message"] + "\n" + row["error"]["retry_command"])


def _read(args):
    with StateStore(args.db, read_only=True) as store, store.snapshot():
        if args.command == "queue":
            rows = [row for row in store.queue() if args.status is None or row["status"] == args.status]
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                _show_rows(rows)
        else:
            current = store.current(args.document_id)
            result = {"current": current}
            if args.history:
                result["history"] = store.history(args.document_id)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                _show_rows([current])
                if args.history:
                    print("History:")
                    _show_rows(result["history"])
    return 0


def _process(args):
    files = None
    settings = provider = None
    if args.command in ("intake", "refresh"):
        config = _validate_processing(args)
        if args.command == "intake":
            if not args.input_directory.is_dir():
                raise StateError("Input directory does not exist or is not a directory.")
            files = sorted(path for path in args.input_directory.glob("*.json") if path.is_file())
            if not files:
                raise StateError("Input directory contains no top-level JSON exports.")
        settings, provider = capture_settings(config, args.mock_dir, args.workers, args.min_interval)
    database = args.db.resolve()
    if args.command != "intake" and not database.is_file():
        raise StateError("Intake database does not exist; run intake first.")
    with WriterLock(database):
        # Capture all input bytes before dispatch so an interrupted run can
        # resume even if the operator removes or changes the input directory.
        sources = [(path.name, path.read_bytes()) for path in files] if files is not None else []
        with StateStore(database) as store:
            store.recover_running()
            if args.command == "intake":
                jobs = [store.enqueue(document_id, raw, settings) for document_id, raw in sources]
            elif args.command == "refresh":
                jobs = store.refresh(settings, args.document)
            elif args.command == "retry":
                jobs = [store.retry(args.document_id)]
            else:
                jobs = store.resume_jobs()
            providers = {}
            if provider is not None:
                key = (settings["provider"], settings.get("mock_dir"), settings["endpoint"])
                providers[key] = provider
            run_jobs(store, jobs, providers=providers)
            failed = [store.current(job.document_id) for job in jobs if store.current(job.document_id)["status"] == "error"]
            for row in failed:
                print(f"{row['document_id']}: {row['error']['message']}\n{row['error']['retry_command']}")
            print(f"Selected {len(jobs)} request(s); {len(failed)} error(s).", flush=True)
            return 1 if failed else 0


def main(argv=None):
    args = _parser().parse_args(argv)
    from .artifacts import ArtifactError, export_snapshot, verify_export, write_accuracy
    try:
        if args.command == "serve":
            from .web import serve
            serve(args.input_dir,args.db,args.mock_dir,config=_validate_processing(args),port=args.port)
            return 0
        if args.command == "export":
            export_snapshot(args.db, args.output);print("Snapshot exported; run verify-export before use.");return 0
        if args.command == "verify-export":
            manifest = verify_export(args.output);print("Verified generation " + manifest["generation_id"]);return 0
        if args.command == "accuracy":
            result = write_accuracy(args.expected, args.actual, args.output)
            print(f"Scalar exact match: {result['correct']}/{result['total']}; routing: {result['routing_correct']}/{result['routing_total']}.")
            return int(bool(result["errors"] or result["correct"] != result["total"] or result["routing_correct"] != result["routing_total"] or result["evidence_correct"] != result["routing_total"] or result["issues_correct"] != result["routing_total"]))
        return _read(args) if args.command in ("queue", "show") else _process(args)
    except ArtifactError as error:
        print(str(error), file=sys.stderr);return 1
    except BusyError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (StateError, ProviderError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("Cannot access intake inputs or database; check paths, permissions and availability.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Intake interrupted; use resume to continue recorded unfinished work.", file=sys.stderr)
        return 130
