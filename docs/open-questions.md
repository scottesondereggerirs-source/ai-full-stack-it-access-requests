# Open Questions — IT Access Requests Challenge

Questions identified during initial review of CHALLENGE.md, docs/requirements.md, and materials/office-policy.md. We are proceeding with reasonable defaults (noted below each question) but will revisit if any of these get clarified.

## 1. Which LLM provider/model should we target?

**Source**: CHALLENGE.md says "The facilitator supplies an API key through the environment variable `API_KEY`" but does not name a provider.

**Default**: Databricks AI Gateway (`system.ai` models via `/ai-gateway/openai/v1/chat/completions`). The API key will be read from `API_KEY`. We will also support an OpenAI-compatible fallback. Provider and model will be configurable via a `config.json` or environment variables.

---

## 2. Coding-session capture format

**Source**: CHALLENGE.md deliverable table lists "Coding-session capture — Session record paired with the submitted commit, collected through the facilitator's capture setup."

**Question**: Is this handled entirely outside our application (by the facilitator's tooling), or do we need to produce something ourselves?

**Default**: Assume it is external to our code. We will not build capture tooling. If the facilitator's setup requires anything from us, we will add it once instructed.

---

## 3. Dependency lockfile format

**Source**: CHALLENGE.md says "Source code and dependency lockfile."

**Default**: `requirements.txt` with pinned versions. Simple, universally understood, and sufficient for a CLI application of this scope.

---

## 4. Deliverables from mock or live responses?

**Source**: CHALLENGE.md says "The committed deliverables may come from mocked or provider responses; each record's `response_source` says which."

**Default**: Deliverables (`extractions.jsonl`, `queue.json`) will be generated from mock responses for reproducibility. The mock responses will be hand-crafted to match the expected extraction for each message. Live provider runs are optional and will be labeled accordingly if used. The `response_source` field in each record will clearly indicate `"mock"` or the provider+model ID.

---

## 5. Interface choice: CLI, service, or web app?

**Source**: CHALLENGE.md says "Choose an interface suited to the IT team."

**Default**: CLI (command-line interface). IT operators are comfortable with terminals, a CLI is the simplest to implement and test, and it aligns with the requirement for "documented commands" and "reproducible retry command."

---

## 6. Model request configuration specifics

**Source**: CHALLENGE.md says "extraction prompt, schema and model request configuration" should be included.

**Default**: Configuration will include: model ID, temperature, max_tokens, and the system prompt. Stored in a `config.json` file. Documented in README.md.

---

## 7. Paraphrase request for accuracy.md

**Source**: CHALLENGE.md says "one paraphrase you author" in accuracy.md.

**Default**: We will create one synthetic request message that paraphrases an existing message (e.g., a reworded version of IT-001) with the same factual content but different wording. Expected fields will be specified independently of the extraction output.

---

## 8. Test framework

**Source**: requirements.md says "Include automated tests" covering specific scenarios.

**Default**: `pytest` with test files in a `tests/` directory. Fixtures in `conftest.py`. Tests will build expected results from the supplied messages and policy, independent of extraction outputs being measured.