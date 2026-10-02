# FINDINGS — phase 6: process boundary

**Status: the smallest real process boundary around the unchanged BLACKWALL
semantics; adversarial probes, lifecycle tests and real-model runs scored
from the trusted ledger with the unchanged evaluator and observer.**
No FUSE, containers, VMs, seccomp, namespaces, desktop data or network
mediation. Canonical artifacts byte-identical. Design, threat model and
protocol: `docs/PROCESS_BOUNDARY.md`.

## Research question

> Which guarantees survive when the trusted authority plane is separated
> from a compromised agent by an actual OS process boundary?

## Five things kept apart

1. **Logical authority membrane** — grants, projections, effects,
   provenance, receipts (phases 1–5C; unchanged here).
2. **Process isolation** — the agent and the membrane are different OS
   processes joined only by a defined protocol (new in this phase).
3. **OS/filesystem enforcement** — whether the OS stops the agent process
   from reaching the membrane's storage or socket by other paths (NOT
   achieved here: same user; see below).
4. **Agent behaviour** — what the real model does across the boundary.
5. **Provenance correctness** — whether an independent observer still
   reconstructs everything from the trusted ledger.

## Architecture

Trusted process (`python3 -m blackwall.ipc.server`): corpus, policy
authority, broker, projections, effects, capability catalog, ledger, task
definitions. Untrusted process (`run_agent_process.py`): harness, driver,
a remote facade with exactly the agent's three methods (plus `discover` in
the global condition); every other attribute is a trap that *reports* an
unmediated attempt. The orchestrator creates a run directory (0700) with
the ledger and a private socket directory, starts the server, and starts
the agent with `--socket --condition --driver --tasks` only. Prompts and
prompt hashes are issued by the server (`begin_task`); the agent cannot
choose them. The agent process imports no trusted module (asserted by a
test on its import graph; its scripted self-check reads scripts as data).

## Protocol `blackwall-ipc/1`

4-byte length prefix + one JSON object, ≤ 256 KiB, exact top-level keys,
duplicate keys rejected, per-op argument whitelist with types and bounds,
non-finite numbers rejected, nesting bounded; unknown ops (including
`revoke`, `task_view`, `ledger`, `eval`, `__import__`) rejected and
receipted (`ipc_reject`); framing errors close the connection; 30 s read
timeout; one request in flight per connection; client discards responses
whose id does not match. Refusals cross the boundary with their receipt id
so the facade raises exactly as in-process. Receipt sequence, logical
clock and grant-id counter resume across a trusted restart (identities
never collide, the clock never regresses, a `trusted_restart` row is
written); grants, task state and anything else in memory do not survive.

## OS/filesystem boundary — what is and is not established

The two processes run as the same OS user. The OS therefore does **not**
prevent the agent process from opening the ledger or the socket if it
learns their paths, and both are readable from the server's own
`/proc/<pid>/cmdline` by any same-user process. What this phase
establishes is the *protocol* boundary: the trusted process accepts only
`blackwall-ipc/1` frames and gives the agent no path, handle, module or
object. What it does not establish — and does not simulate — is OS
enforcement of that boundary: user separation (or a mount/namespace
boundary) is the missing mechanism and is a separate experiment.
`tests/test_ipc_boundary.py::test_18_trusted_file_access_limitation`
demonstrates the same-user read explicitly so the distinction is in the
test record, not only in prose.

## Adversarial probes (`tests/test_ipc_boundary.py`, real server subprocess)

Forged grant ids and another task's grant id → "unknown grant", no read;
fabricated projection refs and another task's real hash → "never
disclosed"; client-supplied prompt hash impossible (field rejected;
server-issued hashes differ per task); altered projection, resource,
operation → mismatch/outside-scope refusals; overbroad or path-like scopes
→ policy denials, nothing read; replayed reads budgeted (9th refused
"exhausted"); replayed effect → "effect grant consumed", one effect
receipt; malformed JSON, non-objects, invalid UTF-8 → protocol rejection,
receipted, connection closed; oversized prefixes (262145, 2³¹) rejected
before the body is read; unexpected/missing/duplicate fields and wrong
versions rejected; half-frames and interrupted connections leave only
`ipc_connection`/`ipc_reject` rows and the server alive; fragmented frames
reassemble; response ids echo in order; unavailable ops rejected and
receipted; no response string names a path or the ledger; the server binds
no TCP/UDP socket; the agent process's import graph contains no trusted
module. Invariant check across the class: every `read` has a mint for the
same task and grant, exactly one legitimate effect, every rejection
carries a reason, the server survives.

## Lifecycle (`tests/test_ipc_lifecycle.py`)

Server before client; client before server (connect retry); trusted
restart mid-run (sequence, clock and grant ids continue; observer
disagreement-free; a pre-restart grant id is refused "unknown grant");
connection drop during a request → transport error, counted, no hang;
client dies during an effect → the effect landed, the retry on the same
grant is refused "consumed", exactly one effect; duplicate request after
reconnect → two mints (existing semantics); stale response ids discarded.

## Equivalence (`tests/test_ipc_equivalence.py`)

The scripted suite across the two processes reproduces the canonical
ledger exactly after removing sequence, clock, `ipc_*` rows and the
`self_reported` marker on advisory unmediated-attempt rows; the evaluator
returns the canonical verdicts and the observer agrees on every use.

## Defects discovered and fixed

| # | found by | defect | class | fix |
|---|---|---|---|---|
| 1 | lifecycle test (restart) | grant ids restarted at `g-1` after a trusted restart: a stale pre-restart id could collide with a fresh mint (observer: `duplicate_mint_id`) | identity across restart | the authority's grant counter resumes from the ledger's highest minted id; sequence, clock and grant ids all continue, authority state does not |
| 2 | first real-model run | the model's malformed `derived_from` entries were rejected by the IPC schema as *protocol* errors; the agent loop did not catch them and every run crashed on the first effect — a semantics change versus in-process, where the broker refuses them with a receipt | boundary changed semantics | provenance entries cross the boundary as bounded JSON values and the broker refuses malformed ones with its own receipt; the remote facade turns protocol rejections into observations, never exceptions |
| 3 | lifecycle test review | the server served one connection at a time, so an idle connection blocked every other client for the read timeout | availability | one thread per connection; broker dispatch serialized by a lock |
| 4 | lifecycle test review | a clean EOF from the server was raised as a transport error without counting it | telemetry | counted |
| 5 | hostile review (high) | **task identity was client-chosen**: a compromised client could begin any task in any order any number of times, relabel itself as a task with a more permissive policy (t5's denied directory read approved under t10's label) and write receipts onto finished or undesignated tasks | confused deputy | the server owns task identity: designated tasks only, each once, one active, ops only for the active task, closure persists across restart |
| 6 | hostile review (high) | agent-shaped narrative claims (a list of strings) and telemetry crashed the run's evaluation so no metrics were written | evaluation robustness | claim schema at the protocol; tolerant comparator; type-checked telemetry; per-run evaluation failure recorded |
| 7 | hostile review (medium) | nested NaN/Infinity, overflowing numbers and lone surrogates reached the ledger (invalid JSON on disk) | parser | constants rejected at parse; nested numbers finite and bounded; strings valid UTF-8 |
| 8 | hostile review (medium) | the `task` condition was never delivered over IPC (behaved as control) | semantics | `begin_task` returns the task view in that condition |
| 9 | hostile review (medium) | a second server could take over a live socket and both wrote colliding sequence and grant ids | identity | ledger `flock`; live-socket probe; inode-checked unlink |
| 10 | hostile review (medium) | a read whose response exceeded the frame limit was receipted as disclosed but never reached the client, with no record | receipt precision | `ipc_reject response_too_large` naming the read receipt |
| 11 | hostile review (low) | huge integer TTL → internal error; `v: 1.0` accepted; rejection replies dropped a parsed id; narrative/task_end rows unmarked; agent inherited the orchestrator's full environment | hygiene | bounded numbers; strict int version; id echoed; `self_reported` marks; minimal agent env |

## Real-model results

5 runs × 12 tasks of `gpt-oss:20b` through the separated architecture
(control condition, per-task conversation as in phase 5C-A), compared with
the sealed in-process phase 5 control (10 runs). Same model, tasks, corpus,
policy, projections, effects, evaluator; no prompt change.

| measure | in-process (120) | process-separated (60) | per-run permutation test |
|---|---|---|---|
| useful | 64 (53%) | 33 (55%) | p = 0.85 |
| honest + admissible + useless | 47 | 24 | p = 1.00 |
| inadmissible / capability fail / unknown | 7 / 0 / 2 | 2 / 0 / 1 | — |
| useful share of honest+admissible, per-run mean (sd) | 0.580 (0.143) | 0.579 (0.062) | p = 1.00 |
| empty answers | 23 | 11 | p = 1.00 |
| grants requested / minted / denied per run | 42.1 / 16.8 / 25.3 | 46.0 / 16.4 / 29.6 | p = 0.36 (requested) |
| broker calls per run | 65.9 | 67.4 | — |
| disclosure bytes per run | 8212 | 7237 | — |
| safety invariants | all 0 | all 0 | — |
| observer disagreements | 0 | 0 of 517 uses | — |
| protocol rejections (`ipc_reject`) | — | 0 | the honest model never sent a malformed frame |
| transport failures / reconnects | — | 0 / 0 | one connection per run |
| self-reported unmediated attempts | 0 | 0 | — |

The boundary did not materially change ordinary behaviour. The one
failure class that process separation *did* introduce before the fix —
the model's malformed `derived_from` entries becoming protocol errors that
crashed the agent loop — was a protocol/semantics defect, not an agent or
authority failure, and after the fix those entries are refused by the
broker with receipts exactly as in-process.

## IPC overhead

| call | in-process median / p95 (ms) | over IPC median / p95 (ms) |
|---|---|---|
| request_grant | 0.008 / 0.013 | 0.174 / 0.252 |
| read (conversation@1, 1206-byte response) | 0.027 / 0.032 | 0.229 / 0.323 |
| effect (send_message) | 0.044 / 0.054 | 0.301 / 0.594 |
| connect (open + hello + close) | — | 0.52 / 0.74 |

Real-model runs: 517 IPC requests, median 0.33 ms, p95 1.26 ms, maximum
113 ms (GC/scheduling), against model latencies of seconds per turn. The
boundary costs roughly 0.2–0.3 ms per broker call and is irrelevant to the
experiment's timescale.

## What is established

1. **Logical authority membrane** — unchanged and still holding: under a
   compromised client every forged, foreign, replayed, altered or
   fabricated request is refused with its receipt; no corpus value leaves
   except through an authorized projection; effects need their own grant,
   valid provenance and are consumed on success.
2. **Process isolation** — established at the protocol level: the trusted
   state (corpus, authority, grants, ledger, policy, prompts) lives in a
   process the agent cannot address except through `blackwall-ipc/1`;
   the server evaluates, imports, opens and executes nothing the client
   names; the agent process imports no trusted module; task identity,
   prompts and hashes are server-owned; restart continues identities
   without restoring authority. Twenty-three probes and seven lifecycle
   cases against real subprocesses, plus a hostile review whose eleven
   findings were fixed, support this.
3. **OS/filesystem enforcement** — **not established.** Both processes run
   as one user; the agent can read the ledger and connect to the socket
   if it learns their paths, and the paths are discoverable (the agent's
   own argv; the server's `/proc` cmdline); the corpus and policy modules
   are importable by any same-user process; the agent can write into the
   results tree. The experiment shows the distinction (the convention is
   not an OS boundary) rather than closing it. User or namespace
   separation is the missing mechanism and is a separate experiment.
4. **Agent behaviour** — unchanged across the boundary (table above).
5. **Provenance correctness** — preserved: the observer reconstructs
   every use from the trusted ledger with no disagreement; broker receipt
   identifiers are the same identifiers across the boundary (no second
   authority identity); protocol rejections are receipted separately and
   carry no task attribution; the one receipt-precision gap found (a
   disclosed response too large to deliver) is now itself receipted.

Two quantified properties of the design surfaced by the review, not
changed here: grants and effects per task are unbounded by policy (a
compromised client landed 200 correctly-authorised sends in 0.09 s), and
a trusted restart resets the in-memory world state while the ledger keeps
the effects. Both are recorded for the policy and persistence questions.

## Limitations

- Same OS user; no OS enforcement (above). No containers, namespaces,
  seccomp, VMs, real desktop data or network.
- Availability is not bounded: ledger growth, memory and threads per
  connection are unlimited by the protocol.
- Self-reported telemetry (unmediated attempts, narratives, answers) is
  advisory across the boundary and marked `self_reported`.
- One model, n = 5 runs over the boundary.

## Answer

**Which guarantees survive when the trusted authority plane is separated
from a compromised agent by an actual OS process boundary?** All of the
logical ones — bounded authority, projection-only disclosure, provenanced
and consumed effects, authoritative and complete receipts, lifecycle
identity — survive separation, under a hostile client, with the agent's
ordinary behaviour unchanged and sub-millisecond overhead. What does not
yet exist is OS enforcement of the storage and socket secrecy that the
protocol boundary assumes: two processes of one user are a protocol
boundary, not a security boundary. That is the next experiment, not this
one's claim.

## Stop

Process boundary established at protocol strength; OS enforcement not
demonstrated. No further isolation layer was started.

