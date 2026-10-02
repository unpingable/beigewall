# BeigeWall's role in the surrounding research

BeigeWall is an **adversarial-agent reference vertical** for testing the
separation of language-level behaviour from operational authority. It is a
consumer and test workload for the broader governed-system architecture,
not a replacement for, or umbrella over, the other projects. Everything
below is positioning; it introduces no implementation.

## Why it exists

A governed-agent architecture should be tested against an adversarial
language layer rather than assume the language layer is trustworthy. The
motivating failure class is not "the model lies". It is the set of
behaviours actually observed in BeigeWall's runs: the model claims access it
did not have; infers information it was denied; requests inappropriate or
overbroad authority; carries content across task boundaries; retries or
reuses stale authority; attempts effects nobody asked for; and misreports
provenance. The architecture must remain safe while those behaviours occur,
and the measurement must not depend on the model's own account of what it
did.

## Relationships

**Constellation.** BeigeWall is a concrete adversarial workload for
Constellation's authority / evidence / custody model. The empirical
property it exercises is: *the language layer can be wrong, hallucinate,
mis-narrate, request inappropriate authority, or carry stale content
without thereby acquiring operational authority.* It exercises bounded
authority (scoped, expiring, budgeted, consumed, revocable grants),
explicit projections as the disclosure boundary, effect mediation with
dependency provenance, authoritative receipts, lifecycle pressure, and
capability negotiation. BeigeWall is not the Constellation implementation;
it is one vertical that Constellation's model must survive.

**Jev.** Jev is a candidate language/decision layer *above* the authority
membrane: it may interpret intent, classify dialogue, or make bounded
conversational decisions, but it must not become the authority plane.
BeigeWall supplies the execution boundary in which such a layer can be
wrong without its errors becoming operational authority. The principle:
*rooting the conversation does not root the business process.* BeigeWall
demonstrates this at the tested toy strength only; it is not a solved
property.

**ABSD.** ABSD is a potential lower substrate for enforcing some of the
boundaries that BeigeWall exercised — by an in-process membrane (phases 1–5C), a
trusted-process boundary (6), and a VM/KVM boundary (7). BeigeWall does not run
on ABSD. The progression it followed was: logical authority membrane → process
boundary → VM/OS boundary; substrate/OS enforcement *beneath* the VM remains a
potential future layer, not something BeigeWall claims. ABSD remains an
independent research program, not an "AI OS".

**atproto-ACL.** A domain-specific policy/admission layer that could serve
as a governed policy component for an agent operating on ATProto
resources. The relationship is compositional —
`agent → BeigeWall → policy/admission → governed operation` — and
BeigeWall does not own domain policy.

**Phlogiston and other application surfaces.** Application-layer work can
consume governed projections and decisions without becoming an authority
system. BeigeWall could eventually provide the adversarial-agent boundary
for such applications; no integration exists today and none is claimed.

**Formal / Lean work.** BeigeWall supplies empirical invariants and
concrete failure cases as *candidates* for formalisation: non-escalation of
authority, projection non-disclosure, effect provenance, receipt
completeness and observer reconstruction, grant consumption, revocation,
cross-task isolation, and admissibility relative to disclosed evidence.
None of these is proved by BeigeWall. The relationship is empirical
contract → candidate formal model → formal verification.

## Conceptual architecture

Conceptual only; it does not describe current runtime integration.

```
application / agent
        |
        v
language / decision layer          (e.g. Jev; untrusted)
        |
        v
BeigeWall                          (the membrane under test)
  projections
  bounded authority
  capability negotiation
  effect mediation
  provenance / receipts
        |
        v
governed policy / authority substrate   (Constellation model; atproto-ACL
        |                                as a domain policy component)
        v
ABSD or other substrate            (potential future enforcement layer)
```

## Current empirical boundary

Demonstrated at toy strength (synthetic corpus of ~25 objects, 12 fixed
tasks, a local 20B model). Process isolation (phase 6), a VM/KVM boundary
(phase 7), and independent + recoverable evidence custody (7B/7C) **are**
demonstrated; QEMU/KVM is trusted and OS enforcement *beneath* the VM is not
claimed. See `RESEARCH-SUMMARY.md` and the `docs/*_BOUNDARY` / custody docs.

- The authority membrane held across every tested lifecycle experiment:
  real-model runs produced zero successful stale, foreign, revoked or
  forged authority uses, zero same-grant effect replays, no authority
  inflation.
- Receipt observers reconstructed every tested use from the ledger alone
  with no disagreement, after the observer was taught the broker's exact
  semantics.
- Cross-task content bleed was observed in persistent conversations
  (15 of 23 inadmissible executions in one suite) and disappeared under a
  driver-side per-task context reset, at the cost of renegotiating
  authority every task.
- Capability discovery reduced authorization friction substantially but did
  not, by itself, reliably improve usefulness; task-conditioned discovery
  improved some usefulness measures and also induced unrequested,
  correctly authorised effects.
- The evaluation layer had to become receipt-grounded after frozen answer
  checks were found to accept a fabricated answer and to require an
  undisclosed fact.
- Three broker-side defects were found by hostile review and fixed
  fail-closed (unbounded effect grants, a forgeable move operation, a NaN
  TTL), and a refusal-reason existence oracle was repaired.

These are results about one architecture under one model at toy scale, not
generalised security claims.

## Research progression

1. Logical mediation — completed.
2. Real-model interaction — completed.
3. Receipt-grounded evaluation — completed.
4. Capability negotiation (global, denial-triggered, task-conditioned) — completed.
5. Lifecycle pressure (expiry, revocation, replay, restart, cross-task) — completed.
6. Process boundary; resource bounds; durable evidence/recovery (6/6B/6C) — completed.
7. VM/OS boundary; independent evidence custody; recoverable evidence
   (7/7B/7C) — completed. **Core research complete; stopped here.**
8. Deliberately out of scope (separate questions, not pursued): stronger OS
   isolation beneath the VM, signed/Byzantine/multi-witness custody, real
   desktop resources, production integration — see `RESEARCH-SUMMARY.md`.

BeigeWall is a reusable adversarial reference vertical for the governed-agent
research. It is not a standalone framework and does not absorb the other
projects.
