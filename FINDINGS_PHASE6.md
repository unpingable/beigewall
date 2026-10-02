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

RESULTS_PLACEHOLDER
