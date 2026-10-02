# Findings — Phase 7B (evidence custody against a compromised ledger writer)

Status: **sealed**. Design and role separation: `docs/EVIDENCE-CUSTODY.md`.

## Question

Can BeigeWall preserve tamper-evident, independently reconstructable evidence
when the component that persists the primary receipt ledger is itself
compromised? Phase 6C assumed an honest writer; this phase removes that
assumption.

## Topology and trust

- **Trusted** (TCB): authority broker, policy authority, projection engine,
  effect mediation, receipt producer (`Ledger`), independent witness
  (`Witness`), observer (`witness.classify`).
- **Compromised**: the primary ledger writer. It receives each canonical
  receipt to persist and nothing else; it has **no** authority to mint grants,
  authorize effects, change broker policy, change trusted task identity, or
  change the authoritative effect state.
- **Custody boundary**: the Phase 7 VM boundary. Writer in the guest, producer
  + witness on the host. The producer sends each canonical receipt to the
  witness **directly, before the writer persists**, so the witness holds the
  truth even if the writer omits or alters the row.

## Mechanism (smallest that works)

An independent witness keeps, per occurrence, `(epoch, seq, occurrence_id,
content_hash, chain_hash)` — a monotonic sequence, the producer-assigned
occurrence id, a content hash, and a hash chain. No cryptographic signing:
with hashes + chain + an out-of-custody witness, every tested writer mutation
is detectable, and the producer→witness path is trusted in this topology, so
signing is not required (explicit nonclaim). Primary receipt rows are
unchanged, so **canonical artifacts stay byte-identical** (verified).

## Attack matrix (deterministic, reproducible)

A controlled compromised writer (`blackwall/writer_corruption.py`) mutates a
canonical stream every way the threat model allows; the observer classifies
each without trusting the writer (`tests/test_evidence_custody.py`).

| # | writer mutation | detected | classification | recovery |
|---|---|---|---|---|
| 1 | omitted event | ✓ | OMITTED | fail closed |
| 2 | altered field | ✓ | ALTERED | fail closed |
| 3 | altered source object | ✓ | ALTERED | fail closed |
| 4 | altered provenance | ✓ | ALTERED | fail closed |
| 5 | altered task identity | ✓ | ALTERED | fail closed |
| 6 | altered grant identity | ✓ | ALTERED | fail closed |
| 7 | altered effect identity | ✓ | ALTERED | fail closed |
| 8 | duplicated event | ✓ | DUPLICATED | fail closed |
| 9 | replayed old event | ✓ | REPLAYED | fail closed |
| 10 | reordered events | ✓ | REORDERED | fail closed |
| 11 | truncated ledger | ✓ | TRUNCATED | fail closed |
| 12 | rollback to older state | ✓ | ROLLED_BACK (at restart) | fail closed |
| 13 | fabricated event | ✓ | FABRICATED | fail closed |
| 14 | selective omission | ✓ | OMITTED | fail closed |
| 15 | forked history (equivocation) | ✓ | FORKED | fail closed |
| 16 | corrupted budget consumption | ✓ | OMITTED | fail closed |
| 17 | corrupted lifecycle/restart row | ✓ | ALTERED | fail closed |
| 18 | writer crash during persistence | ✓ | TRUNCATED | fail closed |

**Detected 18/18. Missed 0. False positives 0** (an honest writer →
`HEALTHY`, `detected=False`). **False negatives 0.**

Headline answers:

- **Can the observer detect every tested writer mutation without trusting the
  writer?** Yes, for all 18 classes.
- **Does any writer mutation incorrectly increase authority, decrease recorded
  consumption, or permit an unsafe retry?** No. Every divergence fails closed;
  a forged receipt authorizes nothing; removing/altering a consumed effect is
  detected and denies rather than relaxing a budget.

## Exact occurrence identity

Occurrence identity is the producer's monotonic `seq` + `receipt_id`, never
inferred from payload equality, timestamps, task name, filename, or prose. Two
identical payloads at different `seq` are distinct occurrences; a replay is the
same occurrence id re-presented; the witness chain is order-sensitive (feeding
it out of order is rejected, `test_witness_chain_is_order_sensitive`).

## Server restart (custody wired into the real pipeline)

`TrustedServer(..., witness_path=…)` (server `--witness`): on restart the prior
primary ledger is verified against the witness. `tests/test_custody_server.py`:

- healthy restart → starts, and identities resume from the **witness head**
  (no reuse even if the writer lies about its head);
- altered / omitted / rolled-back primary ledger → **`SystemExit`, fail
  closed**, refusing to start on a false history.

## Budget and effect safety

`budget_safe_consumption` returns a consumption count only when the witness
attests the ledger intact; otherwise it fails closed. Tests: removing a
consumed effect fails closed (never a smaller count), inserting a fake effect
fails closed (never a larger one), an honest ledger yields the true count. A
fabricated receipt is never promoted to authority.

## VM custody demonstration

`run_vm_experiment.py --probe` → the in-guest probe's `witness_custody`
category attempts read / overwrite / append / truncate / unlink / rename
against the **real host witness and ledger paths**: **all 12 attempts fail
(ENOENT)**; whole-probe `reached_trusted_state = 0` over 75 checks. The
compromised writer inside the guest cannot read or modify the host witness —
the custody boundary is the hypervisor boundary. The same-user control
confirms the contrast: a same-uid **host** process *can* read the ledger that
the guest cannot (the Phase-6 convention gap, now an OS boundary for the
guest).

## Live pipeline + behavior neutrality

A deterministic scripted run through the VM produced a 106-row primary ledger
and a 106-record witness; the host observer reports **HEALTHY**
(`detected=False`), and the receipt-grounded result is unchanged (9/12, as
canonical). The custody layer does not alter agent behavior.

Real-model confirmation (gpt-oss:20b through the relay, 3 tasks): the witnessed
run produced a 44-row ledger and 44-record witness, observer **HEALTHY**
(`detected=False`), agent behaviour normal (2/3 receipt-grounded success). The
custody layer is transparent to a stochastic driver; the model was not used as
the ledger adversary (corruption is deterministic and controlled, above).

## Overhead

- **Storage**: the witness is fixed-size per occurrence (~215 B/record:
  two sha256 hashes + seq/epoch/id); ≈ 0.58× the primary ledger size for the
  suite's small receipts, and constant regardless of receipt size.
- **Compute**: `witness.observe` ≈ 10 µs/receipt (hashing) beyond the existing
  ledger write; the witness's own fsync is one more durable append per receipt,
  alongside the durable ledger writes already present.

## What this establishes — and the nonclaims

**Establishes.** Tamper-**evident** evidence for all 18 tested corruption
classes: with an independent, out-of-custody witness, a compromised writer
cannot alter, reorder, duplicate, fabricate, fork, roll back, or silently drop
receipts without detection, and detection fails closed so no mutation increases
authority, decreases recorded consumption, or permits an unsafe retry. The
custody boundary is demonstrated, not asserted: the guest-side writer cannot
reach the host witness.

**Nonclaims.**
- Not tamper-**proof**. A compromised writer can still **suppress** evidence
  (omit/truncate/roll back); the witness makes suppression **detectable** and
  the system **fails closed**, so it cannot masquerade as "nothing happened" or
  as less authority/consumption — but the suppressed primary content is gone
  unless independently held.
- No cryptographic attestation: sound only because the producer→witness path is
  trusted (both host-side). If that path were untrusted, signing would be
  required.
- The witness stores hashes, not content: it detects divergence and attests
  integrity; it is not a second ledger and cannot reconstruct suppressed
  content.
- QEMU/KVM is trusted (Phase 7 scope); producer and witness share the broker's
  host uid (TCB, not the attacker).
- Two integration strengths, stated apart. The in-process server `--witness`
  runs the witness in the **same process** as the producer/writer: it detects
  external **ledger-file** tampering, truncation and restart rollback (the
  `test_custody_server` cases) and fails closed — but it does not survive a
  compromise of that process itself. The **writer-compromise** separation is the
  VM topology (writer in the guest, witness on the host), where the guest
  demonstrably cannot reach the host witness; that is the configuration the
  custody claim rests on.
- Not quorum/consensus/voting. The witness is a second evidence path, not a
  second authority.

## Recommended next research question

The demonstrated gap is **suppression under single-witness custody**: a
compromised writer can still erase its primary content (detectably). The next
question, chosen from that gap (and only on request), is whether the primary
ledger itself can be made **append-only / suppression-resistant** at the
custody boundary — e.g. the witness (or an out-of-custody store) retaining
enough to *reconstruct*, not merely detect, suppressed receipts — without
becoming a second authority or a consensus system.

## Reproduce

```
python3 -m unittest tests.test_evidence_custody tests.test_custody_server
# VM custody demonstration + live observer (needs the Phase 7 guest image):
python3 run_vm_experiment.py --probe   --kernel <vmlinuz> --initrd <initramfs> --out <dir>
python3 run_vm_experiment.py --driver scripted --kernel <vmlinuz> --initrd <initramfs> --out <dir>
```
