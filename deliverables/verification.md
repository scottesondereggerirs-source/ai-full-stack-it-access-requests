# Verification, observations and limits

All committed successful extraction records and accuracy observations use mock responses. The OpenAI request adapter is tested using intercepted HTTP and synthetic credentials. A subsequent operator-initiated live request returned HTTP 429, as recorded below; no successful live extraction, live throughput or live-model accuracy was verified.

## Subsequent operator review and runtime checks

During the captured review, the user cited the policy's boundary that all successful intake outputs await human review. On checking IT-001 against `materials/office-policy.md` rule 5, the assistant clarified that the complete staging/read request belongs to System Owner with status `review` and next action `review-request`, rather than IT Service Desk. The committed output matches that rule. This records the discussion and correction; it does not establish that every source fact or routing case was independently reviewed by the user.

The user directed a refresh and retry to address historical prompts. In the existing local demonstration database, all five current records already used the repaired prompt. Mock refresh selected zero requests and created no duplicate attempts. Retrying IT-001 created attempt 13 from attempt 8. Assertions verified the exact current prompt and its SHA-256, unchanged settings/source revision/source hash/facts/quotes/issues/route, and preservation of attempt 8 unchanged in history. All five current records used the repaired prompt after the check. No network or API key was used for this refresh/retry verification. These observations concern the local ignored demonstration database; the committed 20-message mock publication remains separate.

After the user supplied a credential through a private temporary file, the assistant loaded it into the server's API_KEY environment and deleted the temporary file. The user then attempted live extraction of IT-001 using configured provider `openai`, model `gpt-4o-2024-08-06`, and endpoint `https://api.openai.com/v1`. The recorded result is `error / IT Service Desk / retry-extraction`, null extraction, category `rate_limit`, and message `Provider HTTP 429; check service access or retry later.` No Retry-After value was recorded. The current error handler does not retain the provider error body, so this evidence cannot distinguish transient rate limiting from quota or spending restrictions, establish credential permissions, or confirm a completed model generation. Credentials are excluded from these records. No successful live extraction or live accuracy comparison is claimed.

The capture audit for the earlier submitted commit 7389a67 found all 25 local checkpoint refs on GitHub with matching full object IDs. Its linked capture includes the original assignment/MVP/test/build discussion as well as web troubleshooting. This verifies capture availability, not which transcripts the grader ingested. Subsequent repairs and this verification update have their own commit-linked checkpoints. These observations do not change the historical acceptance verdicts in planning/validation.md or establish newly completed model-selection, latency-comparison, or source-uncertainty decisions.

## Post-feedback prompt repair

The submitted implementation duplicated the schema in the system prompt and the native structured-output setting. `build_messages()` now reads system instructions exclusively from `src/it_access/extraction_prompt.txt`; it appends neither `snapshot.schema.json` nor `materials/office-policy.md`. The schema remains in the provider's native `text.format` configuration with `strict: true`. Python still validates responses and applies the policy for human review.

T-14 checks the exact prompt-file/source-message separation. T-34 checks both direct and captured-settings request construction, including the separate strict schema. T-44 checks that the committed credential-redacted request evidence agrees with the implementation. [Failing checks](../planning/prompt-review-red.txt) were committed before the repair; [targeted passing checks](../planning/prompt-review-targeted-green.txt) and [full regression output](../planning/prompt-review-green.txt) record the repaired behavior.

The post-feedback full regression passed all 63 tests in 36.519 seconds with API_KEY unset. No live model was used.

The 20-message extraction/queue publication, separate 21-message accuracy input, accuracy report, and request evidence were regenerated using the repaired configuration. [Artifact comparison](../planning/prompt-review-artifacts.txt) verifies unchanged source hashes, extracted facts, quotes, issues, routes and mock provenance against the prior deliverables. Publication IDs, timestamps and prompt settings change; scalar exact match remains 252/252 and routing remains 21/21. These are mock observations, not evidence of live-model accuracy or a new human acceptance decision.

Existing database attempts retain their recorded historical prompts. A new intake with the changed prompt gets a different settings fingerprint; operators can explicitly refresh affected documents as documented in README.md. Resuming/retrying an older attempt preserves its recorded settings for reproducibility.

## Reproducible commands and assertions

Run from the repository root after the locked editable install in README.md:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
it-access --db state/intake.sqlite3 intake materials/requests --provider mock --min-interval 0
it-access --db state/intake.sqlite3 queue --json
it-access --db state/intake.sqlite3 export --output deliverables
it-access verify-export --output deliverables
it-access accuracy --expected tests/expected/facts.json --actual deliverables/accuracy-input.jsonl --output deliverables/accuracy.md
```

- T-01 compares all 21 saved scalar records to independently authored source facts; it checks hashes, exact evidence, no key lookup/network, missing fixture rejection and payload isolation.
- T-02–T-19 compare successful/null/error outputs and all five routing rules to source-grounded expectations, including 24 missing-field variants, conflicts, corrections, withdrawal precedence, injected instructions, malformed JSON/schema, invented quotes, calendar boundaries, refusal and incomplete responses. Factual truth is not implied by schema validity.
- T-20–T-26 check raw-byte SHA-256, durable history after reopen, unchanged reuse, source reversion, stale/mismatched completion rejection and transaction rollback, incremental batches, settings refresh and failed attempts after a success. A missing in-message date stays null despite dated export metadata.
- T-27–T-33 use actual CLI processes with blocked network access. A paused provider leaves completed work readable; a second writer, including a symlink alias, is rejected. Four process-kill checkpoints test interruption/resume: pending before dispatch, inside the provider, after response before commit, and after commit. Resume uses stored bytes after input files are removed; it preserves successes and does not retry terminal failures. Timeout/rate-limit/outage fixtures test isolation and database-specific retry commands.
- T-34–T-36 inspect the exact POST, model, strict supplied schema, generation parameters, timeout, refusal/incomplete handling and explicit HTTP errors. Synthetic secrets must not appear in errors or request evidence. Transport is injected; these are mocked observations.
- T-37–T-40 check shared snapshot generation/source/attempt/routing and manifest hashes, partial-publication rejection, repair by re-export, missing/duplicate/tampered records, and independent scoring. Scoring fixes 12 × 21 = 252 comparisons, keeps failures in the denominator, separates routing/evidence/issues and leaves factual mismatch classification for human review.
- T-41 generates 400 and 2,000 matching-ID mock exports, checks every current identity/hash/routing, at most two in-flight calls, constant-sized fixture provenance and zero extra provider calls on an unchanged rerun. No wall-clock SLA is imposed.
- T-42 uses downloaded hash-checked wheels with --no-index to install into a fresh virtual environment, then invokes the installed console command for intake, queue and export verification. A missing wheel cache is an explicit skip/unverified installation, never a passed install claim.
- T-43 checks the validation index has one row for each of the 95 requirements. It does not certify capture, push, Classroom submission, or human acceptance.

The recorded assignment suite output is planning/test-run.txt. Per-step red/green logs preserve the test-first history. The completed step-7 run and observed workload timings are recorded below.

## Repairs and limitations

The accuracy denominator was corrected from an initial planning count of 273 to 252 because the schema has twelve scalar fields and two arrays. Before SQLite implementation, a fixture incorrectly routed a missing request date to System Owner; policy rule 3 corrected it to needs-information / IT Service Desk / clarify in its own test-first commit. The step-7 red workload check found 180,146 bytes of configuration for 2,000 fixtures, exceeding twice the 400-fixture baseline; the implementation now stores a compact digest of the immutable fixture snapshot rather than repeating the entire hash map in every record.

Operating limits: one host, local SQLite, one admitted writer, two workers, bounded socket timeout and manual retry. No network filesystem/multiple-host guarantee, automatic approval, adaptive rate controller, total request wall-clock deadline, arbitrary prompt-attack detector, or storage-corruption/full-disk recovery claim. Stable recorded fixture paths are needed for mock resume/retry. Invalid input bytes are per-file errors; unreadable input files or unavailable storage require operator repair. Schema/policy changes during deployment require explicit refresh; routing-code changes must bump the routing version. A remote call can repeat after a crash before its result commits.

The supported hash lock is Linux x86_64 / CPython 3.12. Dependency wheels were downloaded during setup; automated installation uses the local wheel cache without network. This environment lacks ensurepip, so setup uses a venv without pip and host pip's --python option. Other interpreter/platform combinations have not been clean-install verified. The build is editable and expects repository materials/schema to remain available.

Committed current outputs cover 20 supplied messages; accuracy-input.jsonl separately retains the 21-message mock comparison including the paraphrase. The mock 252/252 scalar and 21/21 routing results demonstrate reproducible pipeline behavior. They do not measure live extraction performance, establish hallucination immunity, or replace human review. Manifest verification is required before consuming exported file pairs; a pair of file replacements is not one atomic operation.

planning/validation.md records all 95 requirements as met at the user’s direction after validation within the available methods and time. This acceptance decision does not extend the scope of the recorded checks. Before hand-in, commit the remaining reviewed changes, verify Entire’s session pairing with the final commit, push the reviewed default branch and submit only the repository root link. Final session pairing, push and Classroom submission are not confirmed here.

## Observed assignment run

Observed command: `env -u API_KEY python3 -m unittest discover -s tests -v`. All 53 tests passed with no skips; full output is planning/test-run.txt and the same run is retained as planning/step-07-green.txt. Total observed suite time was 38.776 seconds on this host. T-42 verified a fresh isolated offline installation and exercised its installed console script. These are local mock/test observations, not live-provider measurements.

Workload configuration: provider=mock, model label=gpt-4o-2024-08-06, workers=2, min_interval=0, socket timeout=60; generated matching-ID variants of IT-001 with saved replies and no real model. Observations:

```text
WORKLOAD mock count=400 seconds=2.941 calls=400 max_in_flight=1
WORKLOAD mock count=2000 seconds=13.980 calls=2000 max_in_flight=1
```

The fast local fixture reads observed one simultaneous call in these bulk runs, within the two-call limit; the event-gated T-32 separately exercised both one- and two-worker concurrency. Every generated current hash/identity/route matched the source transformation, and unchanged reruns made no further calls. The 21-message supporting comparison scored mock scalar exact match 252/252, routing 21/21, source evidence 21/21 and required issues 21/21. The user has accepted the validation results; final submission and capture pairing require confirmation during hand-in.
