# GoLogs — Knowledge Base

This is the deep-dive companion to the [README](../README.md), written for
a technical reader who wants to understand *why* GoLogs is built the way
it is, not just what it does. It draws on `docs/context_transfer.md`, the
engineering log kept continuously throughout development, but is
restructured here as a narrative rather than a running handoff document.

## Table of contents

1. [Architecture rationale](#architecture-rationale)
2. [Threat model](#threat-model)
3. [Testing philosophy](#testing-philosophy)
4. [Real bugs found during development](#real-bugs-found-during-development)
5. [What's verified vs. not, and why](#whats-verified-vs-not-and-why)
6. [Production checklist](#production-checklist)

---

## Architecture rationale

### Why MCP for the tool layer

The Model Context Protocol gives GoLogs a clean seam between "the model
decides what it wants" and "the system decides what's allowed to happen."
The orchestrator (`orchestrator/tool_call_loop.py`) never lets a model's
tool-call request reach the MCP server directly — every request passes
through `permission_gate.py` first. This means the MCP layer itself can
stay simple (six tools, JSON-Schema-validated, each doing exactly one
thing) while all the actual trust decisions live in one auditable module.

### Why SQLite, not Postgres

GoLogs is a single-analyst, local-first tool. A case's event table holds
thousands of rows, not millions. Postgres would add operational weight
(a service to run, a port to secure, a connection pool to tune) with no
corresponding benefit at this scale — and would compete for the same RAM
budget as Ollama. SQLite is zero-ops and single-file, which also makes the
whole application trivially portable: a case is just a `.db` file plus a
folder of evidence.

### Why Ollama runs natively, not in Docker

The target hardware has a hard 10GB RAM ceiling for the entire stack
(Windows itself + IDE + browser already consume 4-6GB idle). Running
Ollama inside Docker Desktop on Windows adds WSL2 VM overhead — typically
1.5-2.5GB — on top of the model weights themselves. That overhead alone
can be the difference between comfortably fitting the budget and blowing
through it. `docker-compose.yml` therefore treats Ollama as an external
dependency the analyst runs natively; `docker-compose.override.yml.example`
exists for reviewers who'd rather accept the RAM cost for a fully
containerized demo.

### Why `python-evtx` despite a real performance finding

The original spec assumed EVTX parsing would handle 50,000 events in
under 5 seconds. Measured against real fixture data, `python-evtx` (pure
Python) parses at roughly 200 records/second — a 50k-event file takes
about 4 minutes, not 5 seconds. This was verified, not assumed: see
[Real bugs found during development](#real-bugs-found-during-development).

The fix wasn't switching libraries. A native-binding EVTX parser would be
faster, but would reintroduce exactly the kind of build-chain fragility
on Windows that `python-evtx` avoids — and this is a project whose
primary deployment target is a Windows laptop, not a CI runner. The
practical answer is to make ingestion asynchronous with a progress
indicator, which is what `routes_cases.py`'s `POST /api/cases` endpoint
does: it returns immediately and ingests in a background task, with
`Case.status` and a resumable parse checkpoint tracking progress.

### Why the orchestrator shells out to `vol`, not the Volatility3 framework API

`core/mem_analyzer.py` invokes the `vol` CLI as a subprocess rather than
embedding Volatility3's framework API (`contexts`/`automagic`/`plugins`)
in-process. This gets three things for free that would otherwise require
much more invasive engineering:

- **Isolation.** A crafted or corrupted memory image can hang or crash
  Volatility3's parsing without taking down the backend process —
  `subprocess.run(..., timeout=120)` gives this for free.
- **Bounded concurrency.** A subprocess pool is trivial to rate-limit
  externally; fighting Volatility3's internal threading model to achieve
  the same thing is not.
- **A hard allow-list boundary.** Only six hardcoded, fully-qualified
  plugin names (`ALLOWED_PLUGINS` in `mem_analyzer.py`) are ever placed on
  a command line — never a caller-supplied string — which forecloses
  argument injection at the point of construction, not just at review time.

## Threat model

GoLogs's threat model treats the evidence itself as untrusted input,
because in a real investigation it might genuinely be attacker-controlled
(a process name, a scheduled task name, a registry value — anything an
attacker with code execution on the source machine could have written).
Three independent defense layers, each of which is designed to hold even
if the others fail:

| Layer | Mechanism | Holds even if... |
|---|---|---|
| Capability minimalism | Only six tools exist; no shell, file, or network tool | ...the model is fully compromised and tries to do anything else |
| Code-enforced deny-list | `permission_gate.py`'s hardcoded `frozenset` of allowed tool names | ...a new tool is added to `tool_registry.py` without also being added here |
| Post-execution injection scanning | `injection_scanner.py` re-scans every tool *result* before it re-enters the model's context | ...the model would have otherwise complied with embedded evidence text |

The third layer is explicitly documented as defense-in-depth, not a
silver bullet — the first two are structural and don't depend on pattern
matching succeeding. See the [40-fixture adversarial
corpus](../backend/tests/security/injection_harness/corpus.py) and
`TestSimulatedCompromisedModel` in
[`test_injection_harness.py`](../backend/tests/security/injection_harness/test_injection_harness.py)
for how this is actually tested — the compromised-model test in
particular checks a structural guarantee (the deny-list holds for every
adversarial fixture, by construction) rather than only a statistical one
(scanner recall, which is real but is a rate, not a guarantee).

## Testing philosophy

A few choices worth calling out explicitly:

- **Real fixtures over synthetic mocks, wherever feasible.** The two
  `.evtx` test fixtures are real binary EVTX files (sourced from
  `python-evtx`'s own GitHub test corpus — `sample_security.evtx`,
  2,261 records; `sample_system.evtx`, 1,601 records), not hand-built XML
  snippets. This caught real parsing bugs (see below) that a synthetic
  fixture engineered to "look right" would never have surfaced.
- **Real subprocesses over mocked protocol layers, for the MCP boundary.**
  `tests/integration/test_mcp_server_protocol.py` and
  `test_orchestrator_real_mcp.py` spawn the actual `server.py` subprocess
  and speak real MCP protocol messages to it — not a mock of what the SDK
  is assumed to do.
- **Mocking is scoped to what's genuinely unavailable in a sandboxed dev
  environment**: a live Ollama server and a real multi-gigabyte Windows
  memory image. Both are mocked at the narrowest possible seam (HTTP
  transport for Ollama; `subprocess.run` for Volatility3's success/timeout
  paths only — the failure paths are tested against the real `vol`
  binary), with everything downstream of that seam exercised for real.
- **The adversarial corpus reports two different kinds of evidence.**
  Scanner recall (40/40 fixtures flagged) is a statistical claim, honestly
  reported as a rate. The compromised-model test is a structural claim
  (the deny-list holds for 100% of tool-invocation attempts, by
  construction) — the knowledge base is explicit about which is which
  rather than blending them into one undifferentiated "95%+ pass rate."

## Real bugs found during development

The full list — eighteen entries — lives in `docs/context_transfer.md`
§6/§7b, written up as they were found rather than reconstructed after the
fact. A few that are worth understanding regardless of whether you read
the source:

**In-memory SQLite + FastAPI's `BackgroundTasks` silently lost data.**
`sqlite:///:memory:` creates a *new, separate* database for every
connection unless the engine explicitly uses `StaticPool`. FastAPI runs
synchronous background task functions in a separate worker thread, which
checks out its own connection — without `StaticPool`, that connection
opened a fresh, empty in-memory database that had never had `init_db()`
run against it. The background ingestion task failed with `no such table:
cases`, using the exact same `session_factory` that worked everywhere
else in the same test. This was invisible across roughly 250 prior unit
tests because none of them exercised a background task running in a
different thread from the one that initialized the schema. Root-caused by
comparing thread IDs across the failing call stack, not by guesswork.

**A security scanner (`bandit`) caught a real vulnerability class, not
just style noise.** `core/normalizer.py` parsed untrusted evidence XML
with the standard library's `ElementTree` — flagged as a MEDIUM-severity
finding (entity-expansion / XXE risk). Fixed with `defusedxml`, then
verified against two real attack payloads (a "billion laughs" entity
bomb and an XXE external-entity reference), both now permanent regression
tests. Fixing this surfaced a second, subtler issue: `defusedxml` raises
its own exception hierarchy when it blocks an attack, and those exceptions
are *not* subclasses of the standard library's `ParseError` — the
original except clause wouldn't have caught them, meaning a successfully
*blocked* attack could still have crashed ingestion.

**Two regex traps in the injection scanner, on the exact phrase the
threat model is built around.** The pattern for the PRD's own worked
example — `"...; SYSTEM: ignore all prior instructions..."` — had a
trailing `\b` after a colon, which can never match because `:` is a
non-word character followed by whitespace. A second pattern for
"ignore \[all\|previous\] instructions" only matched *one* modifier word,
missing the literal three-word phrase "ignore all previous instructions,"
which has both. Both were caught by running the test suite against the
scanner's own documented worked example, not by inspection.

**A config option that had existed since the first milestone did nothing
at all.** `INJECTION_SCAN_STRICTNESS` was declared in `config.py` and
documented in `.env.example`, but no code ever read it — the scanner
always ran in its single default mode. This is arguably a worse class of
bug than a crash: a silently inert setting can mislead an operator who
sets it expecting different behavior. Fixed by adding a real strict-mode
pattern set and threading it through both scanner call sites, with a test
proving strict mode's coverage is a strict superset of standard mode's.

## What's verified vs. not, and why

GoLogs was built and tested in a sandboxed environment without a live
browser, a running Ollama instance, a real multi-gigabyte Windows memory
image, or a Docker daemon. Rather than paper over those gaps, here's
exactly what that means for each:

| Component | Verified how | Not verified |
|---|---|---|
| EVTX parsing, normalization, DB schema | Real binary fixtures, 264+ tests | — |
| MCP tool layer | Real stdio subprocess + real protocol client | — |
| Permission gate, injection scanner | Real logic, 40-fixture adversarial corpus | — |
| Ollama integration | Real, documented `/api/chat` contract; mocked HTTP transport | Actual model tool-calling reliability on real hardware |
| Volatility3 integration | Real `vol` subprocess for failure paths; mocked for success/timeout | Behavior against a real memory image |
| FastAPI backend, WebSocket chat | Real Starlette `TestClient`, real DB, real permission/scanner logic | — |
| React frontend | `tsc --strict` clean, `eslint` clean, real `vite build` | Actual rendering, WS reconnect behavior, responsive layout |
| Docker images, compose | Written against PRD §16 spec | Never built or run — no Docker daemon available |
| CI/release workflows | Valid YAML, using already-proven commands | Never executed on real GitHub Actions infrastructure |
| `scripts/seed_demo_case.py` | Run for real — seeds exactly 3,867 events | — |
| `scripts/setup_dev_env.ps1` | Written against real §17 steps | Never executed — this is a Linux sandbox |

Before relying on GoLogs for a real investigation, or before a "1.0.0"
tag: build and smoke-test the Docker images, push to a real GitHub repo
and confirm a green CI run, and run the full stack against a real Ollama
instance and a real memory image on the actual target hardware.

## Production checklist

Tracking PRD §29:

- [x] FR-1 through FR-10 and FR-12 implemented, each covered by at least
      one automated test — FR-10 (model switching via `MODEL_PROFILE`
      config, no code change) verified in `test_config.py`; FR-12 (fully
      offline) satisfied architecturally — no cloud-LLM-calling code path
      exists anywhere, `--offline` is always passed to `vol`
- [ ] **FR-11 is partial, honestly flagged rather than silently claimed
      done**: `TimelineView.tsx` displays normalized EVTX events, but does
      not yet also display or correlate Volatility3's memory-derived
      process timestamps (`pslist`/`pstree` results) on the same timeline
      — those are currently only visible via the chat's `ToolCallCard`
      when the copilot calls `mem.list_processes`/`mem.run_plugin`, not
      as a first-class timeline view. Closing this gap is the single
      largest remaining functional item.
- [ ] CI pipeline green on `main` — workflows are written and locally-equivalent
      commands all pass, but have not run on real GitHub Actions infrastructure yet
- [x] Adversarial injection suite ≥95% pass rate — **100%** (40/40), see above
- [ ] Peak RSS measured on target hardware — not measurable in this sandbox;
      needs a real run on the Core Ultra 7 / 16GB target machine
- [x] README complete per §27 outline
- [x] `KNOWLEDGE_BASE.md` present and complete (this document)
- [x] No secrets committed — verified with `detect-secrets --all-files`
- [x] License file present (MIT)
- [x] Demo case seeds cleanly — verified: 3,867 events across two real
      fixture files plus a five-event scripted scenario, on a fresh SQLite DB
