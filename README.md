# Northstar IT access-request intake

Turn exported messages into source-supported extractions and a queue for human review. This application prepares requests; it never approves or grants access. The company and messages are fictional.

Supported installation: Linux x86_64, CPython 3.12, host pip 24+. Source requires Python 3.10+, but the committed wheel hashes and clean-install verification target Python 3.12/Linux. Other platforms need their own verified lock hashes. The live adapter supports the OpenAI Responses API with `gpt-4o-2024-08-06`, strict JSON schema, temperature 0, 3,000 output tokens, and storage disabled. Facilitator access to that provider/model is still to be confirmed; no live accuracy or availability is claimed.

## Setup

From the repository root, install the hash-pinned runtime and build dependencies before installing this source. Using host pip avoids requiring ensurepip inside the virtual environment:

```bash
python3 -m venv --without-pip .venv
python3 -m pip --python .venv/bin/python install --require-hashes -r requirements.lock
python3 -m pip --python .venv/bin/python install --no-build-isolation --no-deps -e .
source .venv/bin/activate
```

For an offline installation, download wheels ahead of time using `python3 -m pip download --require-hashes -r requirements.lock --dest /tmp/it-access-wheels`; add `--no-index --find-links /tmp/it-access-wheels` to dependency installation and `--no-index` to source installation. Tests use that local cache for their isolated installation check; its absence produces an explicit skip, not a successful install claim. The repository and its materials/schema must remain available because the supported install is editable.

## Intake and review

```bash
it-access --db state/intake.sqlite3 intake materials/requests --provider mock --min-interval 0
it-access --db state/intake.sqlite3 queue
it-access --db state/intake.sqlite3 queue --status needs-information --json
it-access --db state/intake.sqlite3 show IT-016.json --history --json
it-access --db state/intake.sqlite3 resume
it-access --db state/intake.sqlite3 retry IT-001.json
it-access --db state/intake.sqlite3 refresh --provider mock --min-interval 0
```

Intake reads top-level *.json files; document identity is the filename, distinct from request_id. New bytes or relevant settings create a revision. An unchanged success is reused; an unchanged failure remains visible until explicit retry or changed settings/source. Missing documents in a later batch remain current. Retry deliberately creates a new attempt even after success. Refresh reprocesses affected stored sources, including documents omitted from later batches; supply the desired model/provider options explicitly. A named manager is a contact, not approval.

Mock replies are selected by filename, original byte SHA-256 and decoded-message hash from tests/mock_responses/manifest.json. An unfamiliar source without a saved reply fails explicitly; author a new fixture or use the live adapter. Source revisions and all earlier attempts remain in SQLite. Mock fixture files are captured once per processing command; a compact bundle digest binds their exact contents and manifest to each attempt.

## Live configuration

The facilitator supplies `API_KEY` in the environment. Only live mode reads it; do not place it in a source file, database option or URL. No other environment variable is required. Model/provider settings are command options:

```bash
it-access --db state/live.sqlite3 intake materials/requests --provider openai --model gpt-4o-2024-08-06 --endpoint https://api.openai.com/v1 --workers 2 --min-interval 1 --timeout 60
```

The endpoint must support POST /responses and the configured model/schema. The adapter makes one request per attempt, does not follow redirects, and does not automatically retry. Numeric Retry-After is shown as guidance. Refusal, incomplete output, malformed JSON/schema, invalid dates/quotes and provider failures produce explicit error records with null extraction and a database-specific retry command. Credentials and arbitrary provider error bodies are excluded from errors and request evidence.

## Shared operation and recovery

Run on **one host**, using a local SQLite database (WAL enabled) shared by operators. A nonblocking OS lock admits one mutating command; overlapping writers exit 3 with a busy message. Read-only queue/history commands remain available while calls are pending. Hard links, multiple application hosts and network filesystems are outside the supported locking model; symlink paths resolve to the same lock. Keep the lock file; releasing ownership does not delete it.

At most two provider calls run concurrently, with configurable spacing between starts within each processing command. Pace separate manual retry commands to respect shared quotas. Results commit individually as they finish. All input bytes are stored before dispatch, so resume works without the original input directory. On the next admitted processing command, abandoned running attempts become interrupted history. Resume retries interrupted/pending work, skips durable successes and leaves terminal errors for targeted retry. A crash after remote completion but before local commit may repeat a paid request: execution is at least once. Preserve the database and the recorded mock fixture directory for recovery. If validation policy/schema changed since an unfinished attempt, refresh it explicitly.

Defaults are two workers, one second between starts, and a 60-second socket timeout; a socket timeout is not a total wall-clock deadline. Model, endpoint, prompt/schema/policy hashes, generation settings and routing version affect reuse; pacing and worker count do not. Policy changes require corresponding routing-code changes and a routing version bump. No automatic approval, adaptive quota scheduler or distributed service is implemented.

Exit codes: 0 successful selected work, 1 technical extraction failures or failed export/accuracy checks, 2 invalid usage/configuration/access, 3 busy writer, 130 keyboard interruption. Clarification requests are successful intake outputs, not technical errors or denials.

## Tests and submission artifacts

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
it-access --db state/intake.sqlite3 export --output deliverables
it-access verify-export --output deliverables
it-access accuracy --expected tests/expected/facts.json --actual deliverables/accuracy-input.jsonl --output deliverables/accuracy.md
```

Tests use saved mock replies, synthetic credentials and blocked HTTP transports, with process-level crash/overlap checks and temporary 400/2,000-document mock batches. Dependency installation tests use only cached wheels and no index. No test uses the actual API_KEY or a real model. Do not recursively run the suite from its own README check.

Committed extractions.jsonl and queue.json cover the 20 supplied messages. manifest.json publishes their shared generation and byte hashes; verify it before consuming the pair. A crash between file replacements invalidates the publication; rerun export repairs it. Pending/running records are omitted explicitly in the manifest. accuracy-input.jsonl is the separate 21-message mock observation (the supplied 20 plus tests/inputs/IT-101.json). Expected facts and source evidence live independently in tests/expected/facts.json. Accuracy counts 12 scalar fields × 21 messages = 252, including nulls; failures/missing/invalid/duplicate records get zero of 12. Routing and evidence/issues are checked separately. Faithful alternate purpose wording still loses exact-match credit and needs human factual classification.

To reproduce the 21-message accuracy input from scratch, copy materials/requests into a temporary directory, add tests/inputs/IT-101.json, run mock intake into a separate database, export to a separate directory, and use its extractions.jsonl as --actual. Do not replace the 20-message submission queue with that supporting run. Mock accuracy validates the pipeline, not live extraction quality or immunity to hallucination. Source substring checks alone cannot prove semantic correctness; injection detection has documented phrase/issue coverage and can miss novel wording.

Read deliverables/verification.md for observed checks and limits. planning/test-run.txt is the full run; planning/validation.md has a row per requirement for human inspection. Its unreviewed rows must not be treated as accepted merely because tests pass. Follow docs/03-implement-and-validate.md, verify the session capture paired with the final commit, then push the reviewed commits and artifacts to the default branch and submit only the repository root link. No automatic push or Classroom submission occurs.
