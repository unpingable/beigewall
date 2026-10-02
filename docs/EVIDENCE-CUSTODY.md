# Phase 7B — evidence custody

Research question: can BeigeWall preserve tamper-evident, independently
reconstructable evidence when the component responsible for **persisting the
primary receipt ledger is itself compromised**?

Phase 6C established a three-state evidence invariant (did-not-occur /
occurred-durably / explicitly-indeterminate) but **assumed the ledger writer
was honest**. Phase 7 showed a compromised guest cannot reach trusted host
state. Phase 7B removes the honest-writer assumption: the writer may lie about
what it stored, and an independent witness must make the lie detectable.

This is about **evidence custody, not making the writer trustworthy**, and it
is explicitly **not** quorum, consensus, voting, threshold authority, or
receipt-derived authorization. The witness is a second *evidence* path, never
a second *authority*.

## Seven roles, kept distinct

A receipt establishes only the claim supported by the custody stage it
survived. The findings separate:

1. **authority** — who may mint grants / authorize effects (`PolicyAuthority`,
   `CapabilityBroker`). Unchanged; never derived from a receipt.
2. **execution** — the broker performs the operation.
3. **receipt production** — the trusted `Ledger` mints a canonical receipt with
   a monotonic `seq` and a producer-assigned `receipt_id` (not payload-derived,
   so two identical payloads at different `seq` are different occurrences).
4. **primary persistence** — the **primary ledger writer**, *assumed
   compromised*. It receives the receipt to store and nothing else; it has no
   authority to mint, authorize, or change trusted state.
5. **independent witnessing** — the `Witness` (`blackwall/witness.py`) receives
   each canonical receipt **directly from the producer, before the writer**,
   and keeps a minimal out-of-custody record.
6. **observer reconstruction** — `witness.classify` compares the primary ledger
   against the witness and classifies every divergence, never trusting the
   writer's own statements as evidence.
7. **external effect state** — the real world the effect touched; out of scope
   here beyond the receipt that records it.

## The witness

Per occurrence the witness stores only: `epoch`, `seq`, `occurrence_id`
(the producer's `receipt_id`), `content_hash` (sha256 of the canonical receipt
row exactly as minted), and `chain_hash` (a hash chain:
`chain_n = H(chain_{n-1} ‖ seq_n ‖ content_hash_n)`). It is **minimal by
design**: hashes, not content. It can therefore *detect* that the primary
diverged, but is not a second copy of the ledger and cannot by itself rebuild
authority, budget, or effect state. It is strictly observational — it issues no
grants, authorizes no effects, is never promoted into a verdict, and never
silently repairs the primary ledger.

Primary receipt rows are **unchanged** (the chain lives only in the witness),
so canonical artifacts remain byte-identical. The witness attaches only when
enabled (`Ledger(witness=…)`, server `--witness`); default runs are untouched.

### Why no cryptography (yet)

Detection uses only sequence + content hashes + the chain — no signing. In
this topology the producer→witness path is trusted (both host-side); the writer
never sits between them. Signing/attestation would be required only if that
path itself were untrusted, which it is not here. This is an explicit nonclaim,
not an oversight.

## Custody boundary (uses the Phase 7 VM boundary)

Preferred topology: the **writer runs in the guest** (compromised, its primary
ledger guest-local); the **producer and witness run on the host**. Phase 7
established that guest code cannot reach any host file. Phase 7B demonstrates
this specifically for the witness: the in-guest OS-boundary probe
(`witness_custody` category) attempts every mutation a writer could want —
read, overwrite, append, truncate, unlink, rename — against the real host
witness and ledger paths, and every attempt fails (ENOENT) across the VM
boundary. The custody boundary is therefore the hypervisor boundary, not a
file-permission convention.

What this establishes: a compromised writer **inside the guest** cannot read or
modify the host witness. What it does not: it does not claim hypervisor
integrity beyond Phase 7's scope, and the producer/witness remain same-host,
same-uid as the broker (that uid is TCB, not the attacker).

## Observer classifications

`classify(primary_rows, witness_records)` emits per-occurrence verdicts and an
overall health, never collapsing all disagreement into "bad ledger":

`MATCH, OMITTED, ALTERED, DUPLICATED, REPLAYED, REORDERED, FABRICATED,
TRUNCATED, ROLLED_BACK, FORKED, WITNESS_UNAVAILABLE, INDETERMINATE`.

Single snapshot vs cross-snapshot: `ALTERED/OMITTED/DUPLICATED/REPLAYED/
REORDERED/FABRICATED/TRUNCATED` are detectable against one primary snapshot;
`ROLLED_BACK` uses the restart context (witness head ahead of the writer's
head with a clean prefix); `FORKED` (equivocation) is detected by
`classify_equivocation` comparing two histories the writer presented.

## Recovery semantics

`recovery_decision(report)` maps health to a recovery state and a fail-closed
decision. The rules:

- **Any divergence fails closed** for operations whose safety depends on the
  evidence. It never silently continues.
- **Absence of witness evidence is not evidence that nothing happened**
  (`WITNESS_UNAVAILABLE` fails closed).
- **A witness receipt is never promoted to authority.**
- **A disagreeing history is never silently rebuilt and called the original.**
  Recovery preserves the distinction between original occurrence, recovered
  reconstruction, current state, and unresolved history.

On a witnessed server restart (`TrustedServer(..., witness_path=…)`), the prior
primary ledger is verified against the witness; the server **refuses to start**
(`SystemExit`) on any divergence, and otherwise resumes `seq`/clock from the
**witness head** — so a rolled-back or truncated writer ledger can never cause
identity reuse.

Two integration strengths, kept distinct. The in-process `--witness` runs the
witness in the *same process* as the producer/writer: it detects external
ledger-**file** tampering, truncation and restart rollback and fails closed, but
does not survive a compromise of that process. The writer-**compromise**
separation is the VM topology above (writer in the guest, witness on the host),
which is the configuration the custody claim rests on.

## Budget interaction

`budget_safe_consumption` rebuilds effect-budget consumption from the primary
ledger **only when the witness attests it is intact**; any divergence fails
closed. A compromised writer therefore can never make consumption look smaller
(remove/alter a consumed effect) or larger (insert a fake one) and thereby
change what further effects are permitted — tampering is detected and denies,
it never relaxes a bound.

## Effect interaction

The evidence writer is never an effect authority. A forged receipt cannot
authorize a new effect, resurrect consumed authority, create a grant, justify a
retry, or turn an indeterminate effect into a known success — there is no API
that turns a witness or ledger row into authorization, and a fabricated ledger
fails closed.

## Claim discipline

The ledger is **tamper-evident for the tested corruption classes**, not
tamper-proof. The writer can still *suppress* evidence (omit, truncate, roll
back) — but suppression is **detected** by the witness and **fails closed**, so
it cannot be passed off as "nothing happened" or as reduced authority/consumed
budget. See `FINDINGS_PHASE7B.md` for the attack matrix and the exact
nonclaims.
