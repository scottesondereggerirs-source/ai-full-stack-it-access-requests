# IT Access Requests — Intake and Review Queue

A command-line application for Northstar Labs regional IT teams. It reads exported access-request messages, uses a language model to extract structured records that conform to `snapshot.schema.json`, applies the office review policy (`materials/office-policy.md`), and writes a review queue for human reviewers. The application never approves access. Every successful output waits for a named human reviewer.

The task brief is in `CHALLENGE.md`. Operating requirements are in `docs/requirements.md`. Open questions and the defaults chosen for them are in `docs/open-questions.md`.

## Supported runtime and provider

| Item | Supported configuration |
|---|---|
| Python | 3.12 (tested on 3.12.3). Uses `str \| None` type syntax, so 3.10 is the minimum. |
| OS | Linux or macOS shell. Tested on Databricks serverless compute. |
| Default response source | `mock`: hand-written responses in `src/mock_provider.py` for the 20 supplied messages. No network or key needed. |
| Live provider | Any OpenAI-compatible Chat Completions endpoint. The documented configuration is the Databricks AI Gateway MLflow Chat Completions API, model service `system.ai.gpt-6-luna`, provider-default temperature (this reasoning model rejects any other value), `max_completion_tokens` 4096, 120 s timeout. Verified with a live run on 2026-11-06. |

## Setup

Run every command from the repository root, because `config.json` and the data paths are resolved relative to the current directory.

```sh
git clone https://github.com/<your-account>/<repository>.git
cd <repository>
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins direct dependencies: `jsonschema==4.23.0`, `requests==2.32.3`, `pytest==8.3.3`.

On Databricks workspace files (`/Workspace/...`), Python cannot write `__pycache__`. Set these first:

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX=/tmp/pycache
```

## Configuration

Settings live in `config.json`:

| Key | Meaning | Default |
|---|---|---|
| `model.provider` | Label only | `databricks` |
| `model.model_id` | Model sent in the request; recorded in `response_source`. On Databricks this must be the full Unity Catalog name. | `system.ai.gpt-6-luna` |
| `model.base_url` | Full Chat Completions URL. `${VAR}` references are expanded from the environment. | `${DATABRICKS_HOST}/ai-gateway/mlflow/v1/chat/completions` |
| `model.temperature` | Sampling temperature; `null` omits it from the request | `null` |
| `model.max_tokens` | Response token limit (includes reasoning tokens) | `4096` |
| `model.max_tokens_param` | Request field name for the token limit (`max_completion_tokens` or `max_tokens`) | `max_completion_tokens` |
| `model.timeout_seconds` | HTTP timeout per call | `120` |
| `extraction.schema_path` | Record schema | `snapshot.schema.json` |
| `requests_dir` | Input exports | `materials/requests` |
| `deliverables_dir` | Output and state directory | `deliverables` |
| `data_dir` | Paraphrase test data | `data` |
| `response_source` | `mock` uses canned responses; any other value calls the live provider | `mock` |

### Environment variables

Environment variables override `config.json`. Never put credentials in `config.json` or commit them.

| Variable | Purpose |
|---|---|
| `API_KEY` | Provider API key, supplied by the facilitator. Required only for live runs. Sent as a Bearer token. |
| `RESPONSE_SOURCE` | Set to `provider` to call the live model; `mock` (default) for canned responses |
| `MODEL_ID` | Overrides `model.model_id` |
| `BASE_URL` | Overrides `model.base_url` |

Example live run on Databricks (`DATABRICKS_HOST` is already set on Databricks compute):

```sh
export API_KEY=<facilitator-supplied key>
export RESPONSE_SOURCE=provider
python -m src.main intake
```

The 2026-11-06 live run used the workspace's own token as `API_KEY` (`API_KEY="$DATABRICKS_TOKEN"`) because the facilitator key was not yet available.

## Commands

Global options (`--config`, `--log-level`) must come before the subcommand, for example `python -m src.main --log-level DEBUG intake`.

| Task | Command |
|---|---|
| Run intake on all exports | `python -m src.main intake` |
| Run intake on one document | `python -m src.main intake --document-id IT-006.json` |
| Inspect the queue | `python -m src.main queue` |
| Show progress, errors and retry commands | `python -m src.main status` |
| Retry one document | `python -m src.main retry IT-006.json` |
| Run the test suite | `python -m pytest tests/ -v` (or `python -m src.main test`) |
| Accuracy check | `python -m src.main accuracy` |

### Starting intake

`intake` processes each file in `materials/requests/` independently and one at a time. For each file it:

1. Computes the SHA-256 of the original file bytes. `document_id` is the filename.
2. Skips the file and reuses the prior result when both the hash and the processing settings (`model_id`, `response_source`, `temperature`) match the current record.
3. Otherwise extracts, validates the result against the schema, applies the policy, and saves the record immediately.
4. Rebuilds `deliverables/queue.json` at the end.

A failure on one message does not stop the others. A failed message gets `status: error`, `owner: IT Service Desk`, `next_action: retry-extraction`, a null extraction, and an error message. The log prints the retry command.

### Inspecting the queue

`python -m src.main queue` prints the current queue grouped by owner. Each row shows document, status, next action, stated request ID, kind, system, environment and permission. Error rows also show the error text. Records that have not been processed yet appear as `(unprocessed)` with status `pending`, so reviewers can start on completed records while the rest are still pending.

The machine-readable queue is `deliverables/queue.json`. Each entry links to its extraction record by `document_id` and `source_sha256`.

### Resuming after an interruption

Each record is written to disk as soon as its message finishes. After a stop or outage, run `python -m src.main status` to see processed, pending and error counts, then run `python -m src.main intake` again. Finished, unchanged documents are skipped. Documents that are missing or changed are processed. To re-attempt a failed document, use the `retry` command that `status` prints.

## Review routing

The rules from `materials/office-policy.md` are applied in this order, in `src/policy.py`:

| # | Condition | Owner | Status | Next action |
|---|---|---|---|---|
| 1 | Instruction-override attempt in the message | IT Service Desk | review | inspect-message |
| 2 | Withdrawal with request_id and request_date | IT Service Desk | review | confirm-withdrawal |
| 3 | kind other/null, conflict, identifier mismatch, missing required fact, or start after end | IT Service Desk | needs-information | clarify |
| 4 | Complete new/change involving production, write or admin | Security Review | review | review-risk-then-owner |
| 5 | Complete staging/read | System Owner | review | review-request |
| - | Extraction failure | IT Service Desk | error | retry-extraction |

Current committed result (live `system.ai.gpt-6-luna`, 20 messages, 2026-11-06): IT Service Desk 10, Security Review 5, System Owner 5, errors 0. The earlier mock results are kept in `deliverables/history/`. Two live routings are wrong; see Known limitations.

## Outputs and state

| Path | Content |
|---|---|
| `deliverables/extractions.jsonl` | One current extraction record per document: document_id, source_sha256, extraction, status, owner, next_action, error, response_source, timestamp, processing_settings |
| `deliverables/queue.json` | Review queue, one entry per input document |
| `deliverables/history/<document_id>.jsonl` | Earlier records for a document, kept when a newer attempt replaces them (including a failed retry after a success) |
| `deliverables/accuracy.md`, `request-evidence.json`, `verification.md` | Supporting documentation (in progress) |

`response_source` is `mock` for canned responses or `provider:<model_id>` for live responses.

## Project layout

```
src/main.py           CLI commands
src/config.py         config.json loading and env overrides
src/prompt.py         system prompt and user prompt template
src/extractor.py      prompt -> provider -> parse -> validate -> route
src/llm_provider.py   OpenAI-compatible HTTP client; refusal, empty, malformed, timeout handling
src/mock_provider.py  canned responses for the 20 supplied messages
src/validator.py      schema validation and completeness checks
src/policy.py         review routing rules
src/state.py          records, history, unchanged detection, retry commands
src/queue.py          queue building and display
tests/                pytest suite (65 tests)
data/paraphrase.json  synthetic reworded version of IT-001 with expected facts
```

## Operating assumptions

* Workload: designed for the sample set and an ordinary day (200–400 messages). Processing is sequential, one model call at a time. A 2,000-message onboarding morning will run but will take roughly 2,000 times the per-call latency. There is no parallelism or rate-limit backoff yet.
* A single operator runs intake at a time. There is no file locking, so two operators starting intake at the same moment against the same `deliverables/` directory can overwrite each other's writes.
* Source messages are treated as data. Embedded instructions (for example the override text in IT-006) are quoted as evidence and routed to inspection, never followed.
* Absence from a later export does not withdraw a request. Only a withdrawal message does.
* Credentials come only from `API_KEY`. Error text from the provider is credential-redacted before it is logged.

## Known limitations

These are known gaps in the current build that the next operator should know about:

* Live run misroutes 2 of 20 because routing rules 1 and 3 rely on keywords in the extraction's `issues` text (see below). IT-006: the model treated the injected "grant admin access" text as a permission conflict and did not flag an override, so it went to `clarify` instead of `inspect-message`. IT-016: the model noted that IT-061 "differs from the document filename" but did not use the word "mismatch", so it went to System Owner instead of `clarify`.
* `retry` does not record processing settings. Because of that, the next `intake` sees a settings mismatch and processes that document again. This is harmless but redundant.
* `accuracy` currently checks schema validity and routing only. It does not compare fields against expected values. It reports `paraphrase.json` as an error in mock mode because no mock response exists for it. A field-by-field comparison against source-derived expected values is still to be built for `deliverables/accuracy.md`.
* Routing rules 1 and 3 detect overrides, conflicts and identifier mismatches by keywords in the extraction's `issues` text (`override`, `conflict`, `mismatch`). This is reliable for the mock responses, but a live model may word issues differently. Deterministic checks (for example comparing the stated request ID with the filename) would make this more robust.
* Supporting quotes are not yet checked automatically to be exact substrings of the message. A manual check of the live run found all quotes exact. One mock quote for IT-003 (now in history) is abridged with `...`.
* Changing the review policy text does not trigger re-processing, because policy version is not part of the processing settings yet.
