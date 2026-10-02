# Findings — Phase 7C (recoverable evidence custody)

Status: **sealed** — final core experiment. Design: `docs/EVIDENCE-CUSTODY.md`.

## Question

Phase 7B's witness detected 18/18 corruption classes but retained only compact
hashes — it could *detect* omission, not *reconstruct* it. The remaining
question: **can the independent custody path reconstruct omitted ledger
evidence without becoming a second authority plane?**

## Mechanism (smallest that works)

The witness gains an **opt-in retain mode** (`Witness(retain=True)`, server
`--witness-retain`): alongside the existing `(epoch, seq, occurrence_id,
content_hash, chain_hash)` it stores the **canonical receipt envelope** (the
row exactly as minted). Nothing else is added — no consensus, quorum, signing,
or second authority. Hash-only mode (7B) is unchanged and remains the default,
so canonical artifacts and existing witness files are untouched.

`reconstruct(primary_rows, witness_records)`:

- re-verifies the witness's own chain first (`verify_chain`): a tampered
  envelope (hash ≠ stored `content_hash`) or a broken chain → refuse;
- for **omission classes** (OMITTED / TRUNCATED / ROLLED_BACK) rebuilds the
  full canonical event list, taking each entry from the surviving primary where
  it MATCHes and from the witness envelope where it is missing;
- tags every entry `provenance: "original" | "recovered"` — recovered evidence
  is never presented as writer-persisted;
- re-verifies the reconstructed sequence against the **attested chain head**, so
  the result is exactly the producer's history, not an invented one.

Recovery returns **evidence only** — entries are `{seq, provenance, row}`. It
mints no authority, restores no budget, authorizes no effect, and makes no
retry decision.

## Recovery invariant (verified)

| primary ledger state | behaviour | tests |
|---|---|---|
| intact | HEALTHY | `test_evidence_custody` |
| altered / duplicated / reordered | detect, **fail closed**, not recoverable | `TestNonRecoverable` |
| fabricated | detect, fail closed, not recoverable | `TestNonRecoverable` |
| omitted entries | **reconstructed** from witness custody | `TestReconstructOmissionClasses` |
| selective deletion | **reconstructed** | `test_selective_deletion_reconstructs` |
| truncated / writer crash | **reconstructed** | `test_truncation_/_writer_crash_...` |
| rolled back | **reconstructed** where witness material exists | `test_rollback_reconstructs` |
| witness retained hashes only | detectable, **not** reconstructable → INDETERMINATE | `test_hash_only_witness_...` |
| witness unavailable | INDETERMINATE / fail closed | `test_no_witness_indeterminate` |

21 recovery tests (`tests/test_evidence_recovery.py`), plus the 7B custody and
server-restart suites, all pass. **466 tests total.**

Recovery never:
- mints authority (reconstruct exposes no grant/effect API; results are
  evidence rows + provenance only — `test_recovery_is_evidence_not_authority`);
- restores consumed budget (the budget path still fails closed on a divergent
  primary; reconstruction only ever *adds back* omitted consumption, never
  reduces it — `test_recovery_does_not_restore_budget`);
- authorizes an effect or a retry (a recovered effect row is evidence, not an
  authorization);
- silently claims the writer persisted recovered rows (provenance tag).

## Live VM demonstration

A deterministic run through the VM (`run_vm_experiment.py --driver scripted`,
server with `--witness --witness-retain`) produced a 106-row ledger and a
106-record retain witness (envelopes present). The orchestrator then simulated a
compromised writer truncating the last 3 rows **on a copy** and reconstructed
from the live witness: `recoverable: true`, `recovered_count: 3`,
`full_history_rebuilt: true`, health TRUNCATED — the canonical history was
rebuilt from independent custody in the real pipeline, and the actual ledger was
never touched. The 7B custody boundary still holds (the compromised guest cannot
reach the host witness; `witness_custody` probe, 12 attempts all ENOENT).

## Hostile review (recovery-specific)

| attack | result |
|---|---|
| recovery becoming authority | reconstruct returns only `{seq, provenance, row}`; no grant/effect path |
| recovered-event substitution | envelope hash is checked against the witness `content_hash`; a swapped envelope → INDETERMINATE (`test_tampered_witness_envelope_self_detects`) |
| duplicate recovery | each `seq` rebuilt once (`test_duplicate_recovery_yields_each_seq_once`) |
| budget reset | budget stays fail-closed; recovery never reduces consumption |
| effect replay | recovered effect is evidence, not authorization |
| witness rollback | a short/rolled-back witness cannot "recover" extra primary rows — they are unattested (FABRICATED) and fail closed (`test_witness_rollback_does_not_enable_false_recovery`) |
| writer/witness identity confusion | a mismatched `receipt_id` at a seq is ALTERED, not recovered (`test_identity_confusion_is_altered_not_recovered`) |
| tampered witness chain | `verify_chain` detects it (`test_tampered_witness_chain_self_detects`) |

## Overhead

Retain mode: the witness holds the canonical envelope, so its file is ≈ 1.6×
the primary ledger size (ledger + the per-record hashes/chain). This is the
deliberate cost of *reconstruction* over *detection*: in retain mode the witness
is a second **evidence** store (still observational, never a second authority).
Hash-only mode (≈ 0.58×, detect-only) remains available for custody paths that
only need tamper-evidence.

## What this establishes — and the nonclaims

**Establishes.** Recoverable evidence custody: with an independent,
out-of-custody witness in retain mode, canonical entries a compromised writer
**omitted, truncated, or rolled back** are reconstructed exactly (chain-verified,
provenance-tagged), while actively **rewritten** entries remain
detect-and-fail-closed and hash-only custody remains detectable-but-not-
reconstructable. Reconstruction is evidence, not authority: it never relaxes the
authority, provenance, budget, or VM-boundary guarantees of the earlier phases.

**Nonclaims.**
- Not tamper-proof against a party that reaches the witness itself; the witness
  is protected by the out-of-custody (VM) boundary, not by cryptography. No
  signing (the producer→witness path is trusted here).
- Reconstruction requires the witness to have retained the envelope; a hash-only
  witness is INDETERMINATE for missing content (by design).
- Retain mode makes the witness a second evidence store; it is still strictly
  observational and never an authority.
- QEMU/KVM trusted (Phase 7 scope); producer/witness share the broker's host
  uid (TCB, not the attacker). In-process `--witness` detects ledger-file
  tampering; writer-compromise separation is the VM topology.

## Closeout

Phase 7C establishes recoverable evidence custody **without** weakening the
authority, provenance, budget, or VM-boundary guarantees. Per the closeout gate,
**core BeigeWall research is complete.** No Phase 8; no further VM hardening,
consensus, real desktop integration, or agent experiments are undertaken absent
a concrete demonstrated defect.

## Reproduce

```
python3 -m unittest tests.test_evidence_recovery tests.test_evidence_custody \
    tests.test_custody_server
python3 run_vm_experiment.py --driver scripted \
    --kernel <vmlinuz> --initrd <initramfs> --out <dir>   # see result.json custody.reconstruction_demo
```
