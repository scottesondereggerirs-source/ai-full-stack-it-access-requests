# Local request lab verification

The web demo wraps the existing intake pipeline with a localhost-only HTTP server and packaged HTML/CSS/JavaScript. It adds no runtime dependencies. The assignment's existing exported records and accuracy artifacts are preserved; the documented launch command uses a separate demonstration database.

## Automated checks

- [Red run](web-red.txt): seven new contract tests failed before implementation; committed as `a51e3bf`.
- [Green web run](web-green.txt): eight tests passed. W-01–W-07 cover source bytes/file confinement, actual mock routing and cache reuse, intercepted live authentication failure and successful retry, credential isolation, stale hashes/invalid tokens/providers, writer admission while reads remain available, packaged assets/text rendering, and launch options.
- W-08 exercises actual HTTP request parsing with an in-memory connection. A rejected POST closes its connection so unread body bytes cannot be interpreted as another request. Added after browser QA exposed a persistent-connection rejection bug.
- [Full regression run](web-test-run.txt): 61 tests passed, including the original pipeline, durable state, subprocess recovery/concurrency, mocked live transport, exports/accuracy, 400/2,000-document workloads, and clean offline installation.
- The evidence-index test now checks allowed verdicts and matching counts instead of requiring every human-review verdict to remain unaccepted. This permits the user's validation checklist edits without assigning acceptance decisions in code.

## Browser checks

[Browser results](web-browser.txt) record successful headless Chromium checks at 1440×1050 and 390×844:

- All 20 supplied files available; mock selected by default.
- IT-001 produces an actual processed review record; repeated analysis reuses its attempt.
- Input and output JSON views work. Selecting a supporting quote highlights the exact source substring.
- Selection/provider changes clear the previous output.
- IT-004 resolves the conflicting environment to null and routes for clarification.
- IT-006 shows instruction-override issues and routes for inspection.
- Live selection with API_KEY unset displays an explicit error and retry; repeated retry remains usable. No real model or key was used.
- No page JavaScript errors or horizontal overflow at the narrow viewport.
- Foreign Origin and missing CSRF token requests are rejected with HTTP 403.

[Desktop screenshot](../deliverables/web-preview.png) shows the conflict scenario. Temporary Playwright/browser libraries were downloaded into /tmp only for QA; they are not application dependencies. The QA server was stopped afterward. A native user-facing preview tool was unavailable in this environment.

To repeat browser QA, install Playwright in a separate tooling environment and make its Chromium executable/libraries available. Start the server from the repository root with API_KEY unset and a temporary database:

```bash
env -u API_KEY it-access --db /tmp/request-lab-qa.sqlite3 serve --port 8765
```

In another terminal, with Playwright resolvable by Node (or NODE_PATH pointed to its node_modules directory):

```bash
node tests/check_web.cjs http://127.0.0.1:8765 /tmp/request-lab-screenshots
```

Stop the server with Ctrl+C. The script expects the unchanged supplied 20 messages and saved fixtures. The in-browser live test intentionally exercises a missing-key error, while W-03 independently verifies a successful live-shaped reply using an intercepted transport. Actual live extraction quality, remote hosting, and multiuser deployment remain outside this local demo verification.
