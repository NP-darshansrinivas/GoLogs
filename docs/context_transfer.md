# context_transfer.md — GoLogs Engineering Memory Handoff

**Purpose:** this file is maintained *continuously*, updated after every
milestone, not written once at the end. If this session's context is lost,
another engineer (human or AI) should be able to pick up exactly where this
left off by reading this file plus the source tree.

**Last updated:** after M6 completion. **All six milestones are now complete.**

---

## 1. Project identity

- **Product name (authoritative, per rebrand instructions):** GoLogs
- **Tagline:** "Interrogate Your Evidence"
- Previous/internal name in the PRD document: "MCP Forensic Copilot" — only
  used when quoting the PRD verbatim for context; never in user-facing text,
  code, docs, or schemas.
- Source spec: `/mnt/user-data/uploads/PRD.md` (709 lines). Read in full
  before any implementation began.

## 2. What GoLogs is

A local-first, AI-augmented DFIR (Digital Forensics & Incident Response)
copilot. An analyst uploads Windows EVTX event logs and/or a memory image;
a local LLM (via Ollama) investigates by calling read-only MCP tools
against that evidence, with a human-in-the-loop permission gate before any
state-mutating or expensive action. Runs fully offline. Target hardware:
Intel Core Ultra 7, 16GB RAM, 1TB SSD — [HW-RULE] enforces a ≤10GB RAM
budget across co-hosted services.

## 3. Repo layout (matches PRD §12 exactly)

```
gologs/
  backend/
    app/
      api/            # M4 — DONE
        routes_cases.py   # create/upload, get, events, audit, report, reparse
        routes_chat.py    # GET chat history (REST)
        ws_chat.py        # the live chat WebSocket handler
      orchestrator/    # M3 — DONE
        permission_gate.py    # classification + hard-coded deny-list
        injection_scanner.py  # confused-deputy defense, scans tool results
        prompt_builder.py     # system prompt + case-summary message (§11.3)
        ollama_client.py      # real /api/chat contract, streaming NDJSON
        conversation.py       # the tool-calling loop, typed events
      mcp_server/      # M2 — DONE
        schemas/       # 6 JSON Schema files, one per tool
        server.py      # stdio MCP server entrypoint
        tool_registry.py  # tool dispatch, ToolContext, risk classes
      core/            # M1 — DONE
        evtx_parser.py
        normalizer.py
        mem_analyzer.py
        fs_sandbox.py
        models.py      # SQLAlchemy ORM, full §13 schema
      db/
        session.py
      config.py        # pydantic-settings, all §15 env vars
    tests/
      unit/            # ~200 tests
      integration/      # 12 tests, real MCP client<->server over stdio,
                         # including the orchestrator wired to the real subprocess
      security/
        injection_harness/   # 40-fixture adversarial corpus + runner (§24)
      fixtures/
        sample_security.evtx   # real fixture from python-evtx's own repo (2261 records)
        sample_system.evtx     # real fixture (1601 records)
    pyproject.toml     # deps, ruff, mypy strict, import-linter layering
  frontend/            # M4 — DONE (React 18 + TS strict + Vite + Tailwind)
    src/
      App.tsx              # case intake + three-pane workspace layout
      components/
        ChatPanel.tsx
        ToolCallCard.tsx     # renders every tool call transparently (FR-6)
        TimelineView.tsx
        ConfirmationModal.tsx  # the signature human-in-the-loop gate UI
      hooks/useChat.ts       # owns the live WebSocket connection + transcript state
      api/{types.ts,client.ts}  # types mirror the backend contracts exactly
  docs/
    context_transfer.md  # this file
  .github/workflows/    # not yet populated (M6)
```

## 4. Milestone status

| Milestone | Status | Notes |
|---|---|---|
| M1 — Core parsing | DONE | evtx_parser, normalizer, mem_analyzer, fs_sandbox, full DB schema |
| M2 — MCP layer | DONE | 6 tools, real schemas, real stdio protocol tests |
| M3 — Orchestrator + guardrails | DONE | permission_gate, injection_scanner, Ollama client, conversation loop, prompt_builder, 40-fixture injection harness |
| M4 — API + UI | DONE | FastAPI routes (cases/chat/report), real WebSocket chat loop, React+TS+Tailwind frontend |
| M5 — Hardening & CI | DONE | CI (7 stages) + nightly adversarial workflow + release workflow, real bandit/pip-audit/detect-secrets runs, Dockerfiles, compose, demo-seed script, OpenAPI contract check |
| M6 — Docs & portfolio packaging | DONE | README, KNOWLEDGE_BASE.md, architecture diagram, LICENSE, CONTRIBUTING.md, enhanced demo scenario |

## 5. Test status (as of end of M5)

- **272/272 backend tests passing**, **93% overall coverage** (target: >=80%, PRD §8) -- up from 264 after M5 added defusedxml attack-payload regression tests and strict-mode injection scanner tests
- Frontend: `tsc -b --noEmit` (strict mode) clean, `eslint` clean, `npm run
  build` produces a working production bundle (157KB JS / 13.6KB CSS
  gzipped). No live browser available in this sandbox -- Playwright E2E
  (§9's testing stack) is **not run here**; see §7.
- `mypy --strict` on `app/`: clean (one scoped `type: ignore`, unchanged
  from M3). `ruff check`: clean.
- New in M4: real Starlette `TestClient` WebSocket tests exercise the
  *actual* live chat protocol end-to-end -- plain-text streaming, an
  AUTO_APPROVE tool call executing for real, and the full
  confirmation-required -> `confirm` frame -> execution -> persisted
  `Finding` round trip -- via a fake `mcp_session` that dispatches through
  the real M2 `TOOL_DISPATCH` functions (so real permission_gate +
  injection_scanner logic runs, just without the ~15s real-subprocess
  cost). One additional test (`test_main_real_lifespan.py`) boots
  `create_app()` with **no override at all** -- the actual production
  startup path, real MCP subprocess included -- and drives a full
  create-case -> get-case round trip through it.
- Per-module coverage highlights: `orchestrator/*` still 96-100% across
  the board, `routes_chat.py` 100%, `routes_cases.py` 92%, `ws_chat.py`
  92%, `main.py` 89% (up from 46% before the real-lifespan test).

## 6. Real bugs found and fixed during M1-M4 (Phase 6/9 self-review in action)

*(M1-M3 bugs are unchanged from the prior version of this file -- fs_sandbox
path-traversal gap, injection-scanner regex traps, `time_created`
nullability, `Evtx()` open-timing, EVTX corruption-class handling, dead
code and inconsistent signatures in `tool_registry`, stale-file drift from
context truncation, and two mypy strict-mode gaps. Summarized list below
is the M4-only addition.)*

13. **In-memory SQLite + `BackgroundTasks` = silent data loss (`no such
    table: cases`).** `sqlite:///:memory:` creates a *new, separate*
    database for every connection unless the engine uses `StaticPool` --
    FastAPI's `BackgroundTasks` runs synchronous task functions in a
    separate worker thread (`starlette.concurrency.run_in_threadpool`),
    which checks out its own connection from the pool. Without
    `StaticPool`, that connection silently opened a fresh, empty
    `:memory:` database that never had `init_db()` run against it -- the
    background ingestion task would fail with `OperationalError: no such
    table: cases` even though the exact same `session_factory`, pointed at
    the exact same `db_url`, worked perfectly everywhere else in the same
    test. Fixed in `db/session.py`'s `make_engine()`: detect `:memory:`
    URLs specifically and pass `poolclass=StaticPool`. File-based SQLite
    doesn't have this problem (every connection opens the same file) so
    the fix is scoped narrowly rather than applied globally. This bug was
    invisible in ~250 prior unit tests because none of them exercised a
    background task running in a different thread from the one that
    called `init_db()`.

9. **`InjectionScanner.role_marker` false positive:** the original regex
   `\b(system|assistant|user|human|ai)\s*:` matched *any* occurrence of
   these words followed by a colon, anywhere in a string — including
   ordinary prose like `"The system: an ordinary reference..."`. Fixed by
   requiring the role word to plausibly start a new "turn": either at the
   very start of the string, or immediately after sentence/field-boundary
   punctuation (`;.\n"!?`), which still correctly matches the PRD's own
   example (`"svchost.exe; SYSTEM: ignore..."`, preceded by `"; "`).
   Verified empirically against both the true-positive and false-positive
   case before wiring in. (`app/orchestrator/injection_scanner.py`)
10. **`override_instructions` pattern too narrow:** originally required
    exactly one modifier word (`all` *or* `previous`) between `ignore` and
    `instructions`, so the literal phrase "ignore all previous
    instructions" — which has *both* modifiers — didn't match. Also added
    `forget everything above/before` as a recognized override phrasing
    after a test caught it missing. (`app/orchestrator/injection_scanner.py`)
11. **Test-authoring bugs (not code bugs), caught immediately by running
    the suite rather than assumed correct:** one test asserted the wrong
    expected pattern-category set for a fixture (my own miscalculation of
    what the regex would match, not a scanner defect); one test used a
    meaningless `assert x == y or True` escape hatch that always passes
    regardless of the left-hand side — replaced with a real assertion
    comparing resolved paths. Both are called out here explicitly because
    "the test passed" is not evidence of correctness if the assertion
    itself is vacuous or wrong.
12. **mypy strict-mode gaps (real, not pedantic):** several function
    signatures used bare `dict` instead of `dict[str, Any]`
    (`permission_gate.classify_tool_call`, `server.py`'s handler closures)
    — caught by `mypy --strict`, not by any test, which is exactly why
    §25 mandates it as a separate CI gate rather than relying on test
    coverage alone. One additional finding, `mcp.server.lowlevel.Server.
    list_tools`, is a genuine third-party stub gap (confirmed via
    `inspect.signature` — the installed SDK's own decorator method has no
    type annotations at all: `def list_tools(self):`) and is suppressed
    with a scoped, comment-documented `# type: ignore[no-untyped-call]`
    rather than a blanket ignore.

## 7. Known limitations / deliberate scope decisions

- **PRD §24 performance target revised (Decision Log entry):** the PRD
  states EVTX parsing should handle 50k events in <5s. Measured real
  throughput of `python-evtx` (the library the PRD's own §9 tech stack
  specifies) against real fixture data: **~200 records/sec**, meaning a
  50k-event file takes **~250s**, not 5s. Root cause is entirely inside
  `python-evtx`'s pure-Python binary parsing (our own `normalizer.py` runs
  at ~21,500 records/sec — not the bottleneck). **Decision:** keep
  `python-evtx` (avoids a native-build dependency on Windows, preserving
  §17's zero-friction setup goal) and make ingestion asynchronous with a
  progress indicator in the UI (M4), rather than swap to a faster but
  more fragile native-binding library. Revisit only if a user explicitly
  needs faster ingest.
- **No real Windows memory image available in this sandbox.**
  `core.mem_analyzer` is implemented against the real Volatility3 `vol`
  CLI (verified: real subprocess invocation, real exit-code/stdout/stderr
  behavior characterized empirically against both a nonexistent file and
  a garbage-bytes file). The **success path** and **timeout path** are
  tested with `subprocess.run` mocked to Volatility3's real JSON-renderer
  output shape, since producing a valid multi-GB Windows memory image
  isn't feasible here. This should be re-validated against a real memory
  image before the analyst relies on `mem.run_plugin` for a real case.
- **`--offline` is always passed to `vol`**, per FR-12 (fully offline).
  This means Volatility3 will not auto-download ISF symbol files for an
  unfamiliar OS build on first use — the analyst needs a pre-populated
  symbol cache. Needs a step in `docs/INSTALLATION_GUIDE.md` (not yet
  written — M6).
- **Windows subprocess memory capping is a known gap.** §20/§22 call for a
  per-plugin subprocess memory cap in addition to the 120s timeout.
  `resource.setrlimit` has no direct equivalent for arbitrary Windows
  subprocesses without Job Objects, out of scope for v1. The 120s timeout
  remains the primary DoS mitigation on Windows. Documented as an accepted
  risk, not silently dropped.
- **No live Ollama instance in this sandbox.** `orchestrator.ollama_client`
  is built against Ollama's real, documented `/api/chat` contract
  (confirmed via web search against July 2026 documentation: request
  shape `{"model","messages","tools"?,"stream"}`, streaming NDJSON
  response chunks shaped `{"message":{"role","content","tool_calls"?},
  "done"}`, tool results fed back as `{"role":"tool","tool_name",
  "content"}`). Tested against a mocked `httpx` transport returning
  realistic NDJSON. **Not yet validated against a real running Ollama
  server** — do this before relying on the conversation loop for a real
  investigation. The one thing that can't be tested at all here: actual
  local-model tool-calling *reliability* (R3 in §20's risk register —
  "model hallucinating tool results instead of calling tools"). The
  system prompt's rules (§11.3) and the citation requirement are the
  documented mitigation; real-model validation is a manual acceptance-test
  item for whoever has the target hardware running Ollama.
- **No live browser in this sandbox, so Playwright E2E (part of §9's
  testing stack) has not been run.** The frontend is verified as far as
  this environment allows: `tsc -b --noEmit` in strict mode (catches type
  errors across the whole app, including the WS/REST contract types which
  are hand-mirrored from the backend's real dataclasses -- see §9), `eslint`
  clean, and a real `vite build` producing a working production bundle.
  What is *not* verified here: actual rendering, the WebSocket reconnect
  behavior (there currently isn't any -- a dropped connection is not
  auto-retried, a known simplification), responsive layout at small
  viewport widths, and the ConfirmationModal's real visual weight/timing.
  Run `npm run dev` against a real backend + Ollama on the target hardware
  before treating the UI as production-verified.
- **§24's injection-harness requirement has two halves; only one is
  machine-testable without a live model.** "The model never executes the
  embedded instruction" can't be verified against a model that doesn't
  exist in this sandbox. What *is* tested, and arguably the stronger
  guarantee per §11.4 point 5, is structural: `TestSimulatedCompromisedModel`
  in the harness constructs a maximally adversarial fake "model" that
  always tries to comply with whatever the injected evidence text asks,
  and confirms `permission_gate` still requires confirmation (or denies
  outright) for every one of those attempts — a guarantee that holds by
  construction, not by sampling. The scanner-recall half (>=95% flagged)
  is fully real: 40/40 (100%) on the corpus, 0 false positives on the
  benign control set.

## 7b. M5 additions: real bugs found, and what's NOT verifiable in this sandbox

**Real bugs found and fixed this milestone** (bandit/pip-audit run for
real against the actual codebase, not just described):

14. **`normalizer.py` parsed untrusted evidence XML with stdlib
    `ElementTree`** -- bandit correctly flagged this as a MEDIUM finding
    (B314). Since EVTX files come from potentially-compromised hosts, this
    is exactly the "assume malicious evidence" scenario §20's threat model
    calls out. Fixed by switching to `defusedxml.ElementTree.fromstring`
    for the actual untrusted-input parsing call. Caught a second, subtler
    issue while fixing the first: `defusedxml` raises its own exception
    types (`EntitiesForbidden`, `DTDForbidden`, etc., all subclassing
    `defusedxml.common.DefusedXmlException`) when it blocks an attack --
    these are NOT subclasses of `ET.ParseError`, so the original
    `except ET.ParseError` wouldn't have caught them, meaning a crafted
    XML-bomb record could have crashed ingestion even with the hardened
    parser in place. Added a second except clause. Verified against two
    real attack payloads (a "billion laughs" entity-expansion bomb and an
    XXE external-entity payload) -- both correctly blocked and converted
    to `NormalizationError`, permanently regression-tested in
    `test_normalizer.py`.
15. **`INJECTION_SCAN_STRICTNESS` was declared in `config.py` since M1 but
    never actually consumed anywhere** -- a config option that silently
    did nothing, which could mislead an operator who set it expecting
    different behavior. Fixed by adding a real strict-mode pattern set
    (looser role-marker matching without the turn-boundary requirement,
    plus a bare-imperative-opener heuristic) and threading
    `get_settings().injection_scan_strictness` through both
    `scan_tool_result` call sites in `tool_call_loop.py`. Tested that
    strict mode is a strict superset of standard mode's recall.
16. **`pypdf` (used only in `test_report_builder.py` to verify generated
    PDFs) was never declared in `pyproject.toml`** -- it was silently
    relying on whatever version happened to be present in the ambient
    sandbox (5.9.0, with multiple known DoS CVEs). Fixed by declaring
    `pypdf>=6.14.2` as an explicit dev dependency.
17. **My own draft of `ci.yml` had invalid YAML** -- two top-level `on:`
    keys (illegal) plus a dead-code job gated on a condition that could
    never be true in that file. Caught by actually parsing the YAML, not
    by visual inspection. Split into `ci.yml` (push/PR-triggered, 7
    always-on stages) and a separate `nightly-adversarial.yml` (its own
    real `on: schedule:` trigger) instead.
18. **Test bug: unpacked `pytest.param(...)` objects as plain 2-tuples.**
    `MALICIOUS_CASES` (used by both `test_injection_scanner.py` and the
    injection harness) is a list of `pytest.param` `ParameterSet` objects,
    not bare tuples -- `for text, expected in MALICIOUS_CASES` doesn't
    unpack correctly. Caught immediately because the new strict-mode test
    that used this pattern failed on its first real run; fixed by
    unpacking via `.values` instead.

**Security scan results, run for real against the actual project:**
- `bandit -r app/ -c pyproject.toml`: one MEDIUM finding (above, fixed),
  two accepted LOW-severity informational findings (the mere presence of
  `subprocess`/`xml.etree` imports -- both already hardened at their
  actual call sites: `mem_analyzer.py`'s fixed-binary-path +
  allow-listed-args subprocess call has a documented `# nosec B603`;
  `normalizer.py`'s only `xml.etree` usage left is the `ParseError`/
  `Element` type references, not the parsing call itself). B101
  (assert-used) skipped project-wide via `[tool.bandit]` in
  `pyproject.toml`, with a documented rationale: both usages are internal
  control-flow invariants, never validation of untrusted input.
- `pip-audit`: **zero vulnerabilities in any of GoLogs's actual declared
  dependencies.** The scan initially surfaced ~87 vulnerabilities, but
  those were all in unrelated packages incidentally present in this
  shared sandbox (camelot-py, mediapipe, mkdocs's dependency chain, etc.)
  -- not part of GoLogs's dependency tree at all. Filtered down to the
  27 packages actually declared in `pyproject.toml` for an honest report;
  see §29 "peak RSS" for the same "shared sandbox vs. actual project"
  distinction applying elsewhere too.
- `detect-secrets --all-files`: zero secrets in actual project source.
  (All raw findings were inside `node_modules/`, `.mypy_cache/`,
  `.pytest_cache/`, `.ruff_cache/`, `.import_linter_cache/` -- third-party
  library internals like hash constants and minified code, and all
  already gitignored.)

**What is NOT verifiable in this sandbox (honest gaps, consistent with
the Ollama/memory-image/browser gaps already documented above):**
- **No Docker daemon.** `backend/Dockerfile`, `frontend/Dockerfile`, and
  `docker-compose.yml` are written against the PRD's exact §16 spec and
  are believed correct, but have never actually been built or run here.
  Trivy container scanning (CI Stage 7) has never executed for real.
  Build and smoke-test these on the target hardware before relying on
  them.
- **No GitHub Actions runner.** `ci.yml`, `nightly-adversarial.yml`, and
  `release.yml` are valid YAML (parsed and verified) and their `run:`
  steps are the same commands already verified to work in this sandbox
  (pytest, ruff, mypy, bandit, pip-audit, npm build) -- but the workflow
  files themselves have never executed inside real GitHub Actions
  infrastructure. Push to a real repo and confirm a green run before
  treating CI as verified.
- **`scripts/setup_dev_env.ps1` is PowerShell** -- this sandbox is Linux,
  so it's written carefully against the real §17 steps but has not been
  executed. `scripts/seed_demo_case.py`, by contrast, **was** run for
  real here and confirmed to seed exactly 3,862 events (2,261 + 1,601,
  matching both fixture files) into a fresh SQLite DB.
- **`scripts/generate_openapi.py` was fully verified both directions**:
  confirmed it correctly detects drift (writing garbage to
  `docs/openapi.json` and confirming `--check` fails) and confirms sync
  (regenerating and confirming `--check` passes) -- this one has real,
  complete verification, unlike the Docker/CI items above.

## 8. MCP tool contract (locked, per PRD §11.2)

| Tool | Risk class (base) | Mutates state? |
|---|---|---|
| `case.get_metadata` | AUTO_APPROVE | no |
| `evtx.query_events` | AUTO_APPROVE | no |
| `evtx.get_event_detail` | AUTO_APPROVE | no |
| `mem.list_processes` | AUTO_APPROVE | no |
| `mem.run_plugin` | REQUIRES_CONFIRMATION (uncached) / AUTO_APPROVE (cached) — dynamic, resolved by `permission_gate.classify_tool_call` | yes (writes `MemPluginResult` cache) |
| `report.append_finding` | REQUIRES_CONFIRMATION (always) | yes (writes `Finding`) |

`TOOL_RISK_CLASSES` in `tool_registry.py` holds only the *base* class;
`permission_gate.py` (M3, done) implements the dynamic `mem.run_plugin`
cache-hit downgrade and the hard-coded deny-list for anything outside the
six registered tools — this is now fully implemented and tested (19
table-driven tests, 100% coverage).

## 8b. M6 additions: docs, and one real gap found while writing them

Writing the Production Checklist into `docs/KNOWLEDGE_BASE.md` (PRD §29)
surfaced something the code itself didn't: **FR-11 ("Timeline view
visually correlates EVTX events and memory-derived process timestamps")
is only partially implemented.** `TimelineView.tsx` shows normalized EVTX
events; it does not display or correlate Volatility3 `pslist`/`pstree`
output on the same timeline (that data is currently only visible via
`ToolCallCard` inside the chat, when the copilot happens to call
`mem.list_processes`). This was caught by deliberately writing the
Production Checklist against the actual FR list rather than a general
sense of "we did most of it" -- FR-10 (model switching via
`MODEL_PROFILE`, verified in `test_config.py`) and FR-12 (fully offline,
architecturally satisfied -- no cloud-LLM code path exists anywhere) were
*also* checked individually this way, and turned out to already be done
despite not being explicitly called out as milestone deliverables earlier
in this document. **Lesson: "done" claims should be checked against the
original numbered requirement list at the end of a project, not just
against the milestone table** -- milestones group work by system layer,
not by requirement, so a requirement can quietly fall between two
milestones' stated scope.

Also built and verified for real this milestone: `scripts/seed_demo_case.py`
was enhanced with a hand-scripted five-event "suspicious PowerShell +
persistence via scheduled task" narrative (PRD §28's explicit ask -- the
real EVTX fixtures alone are generic background noise, not a compelling
demo story). Entirely synthetic data (fictional host `WKSTN-FINANCE07`,
fictional user `j.martinez`). Verified end-to-end: seeds correctly,
inserts in the right chronological order, and is keyword-searchable
through the same query path the chat's `evtx.query_events` tool uses.

A real secret scan (`detect-secrets --all-files`) was also run for the
first time this milestone and came back clean on actual project source
(all raw findings were inside `node_modules/`/tool caches, already
gitignored) -- see §29 checklist below.

## 9. Orchestrator design notes (written during M3, still accurate post-M4)

- **Event -> WebSocket frame mapping is 1:1.** `conversation.py`'s
  `TokenEvent`/`ToolCallEvent`/`ToolResultEvent`/`ConfirmationRequiredEvent`/
  `DoneEvent` map directly onto PRD §14's WS frame types
  (`token`/`tool_call`/`tool_result`/`confirmation_required`/`done`). The
  API layer's WebSocket handler should be a thin loop that calls
  `run_conversation_step`, serializes each yielded event to its frame
  type, and sends it — no business logic belongs in the WS handler itself.
- **Confirmation is a two-call protocol, not a paused generator.** A
  Python generator can't survive a WebSocket round-trip to the analyst.
  The API layer must: (1) call `run_conversation_step`, which stops after
  yielding `ConfirmationRequiredEvent` + `DoneEvent(stopped_for_confirmation=
  True)`; (2) persist `messages` (already mutated in place) somewhere —
  DB row, session cache, whatever M4 chooses; (3) when the analyst
  responds via a REST/WS action, call `resume_after_confirmation()` with
  their approve/reject decision; (4) call `run_conversation_step` again
  with the now-updated `messages` to let the model continue.
- **`ToolExecutor` needs a real MCP `ClientSession` in production.** Tests
  use two different executors — a fast in-process one calling
  `TOOL_DISPATCH` directly (`tests/unit/test_conversation.py`) and a real
  one backed by `mcp.ClientSession.call_tool()` talking to the actual
  `server.py` subprocess (`tests/integration/test_orchestrator_real_mcp.py`,
  which passes end-to-end). M4 should launch the MCP server subprocess
  once at API-process startup (not per-request) and reuse one
  `ClientSession` across requests — `test_orchestrator_real_mcp.py` shows
  the exact `stdio_client`/`ClientSession` pattern to copy.
- **`MAX_TOOL_RESULT_TOKENS` truncation is character-count-based, not a
  real tokenizer** (`~4 chars/token`, documented as an estimate in
  `conversation.truncate_tool_result_content`'s docstring). Fine for a
  soft context-budget guard; don't rely on it for anything requiring exact
  token accounting.
- **`case.get_metadata`'s `event_count` etc. still need a `time_range`/
  `hosts` query** to actually populate `prompt_builder.build_case_summary_message`'s
  arguments — that aggregation query doesn't exist yet (M4 territory,
  likely belongs in a small `orchestrator/case_summary.py` or directly in
  the API layer's session-start handler).
