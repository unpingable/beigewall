# BeigeWall — research summary

BeigeWall is a research artifact that investigated one question:

> Can an untrusted agent stay useful when the computer it believes it controls
> is a mediated projection with no ambient authority — and can the authority,
> disclosure, effect, resource, and evidence boundaries hold **without trusting
> the agent**?

The answer it set out to find, stated as plainly as possible:

> **An untrusted agent does not need to be trustworthy if the authority,
> disclosure, effect, resource, and evidence boundaries do not depend on
> trusting it.**

The work is complete. It is a research result, not a product, and makes no
production-security claim.

## The progression

```
1–3   logical mediation + real model + receipt-grounded evaluation
4–5   capability discovery + agent planning
5B–5C lifecycle + context isolation + evidence semantics
6     process boundary
6B    resource bounds
6C    durable evidence / recovery
7     VM boundary
7B    independent evidence custody
7C    recoverable evidence
      ↓
      BeigeWall
```

Each phase found a **different class of failure** and addressed it **without
weakening any earlier layer**:

| observed failure | mechanism that answers it | phase |
|---|---|---|
| model hallucination / overclaim | receipt-grounded evaluator | 1–3 |
| policy opacity | capability-discovery (negotiation) experiment | 4 |
| cross-task content bleed | per-task context boundary | 5B–5C |
| stale / replayed authority | grant lifecycle controls | 5B–5C |
| runaway *authorized* effects | server-enforced task/resource budgets | 6B |
| same-user process leakage | VM / KVM boundary | 7 |
| malicious ledger writer | independent, out-of-custody witness | 7B |
| ledger suppression | recoverable evidence (witness retains envelopes) | 7C |

## What is demonstrated — per boundary

A guarantee at one layer is not a guarantee at another; each is reported
separately (see the "Boundaries and claims" table in `README.md`).

- **Protocol boundary** — logical authority/disclosure/provenance survive a
  strict `beigewall-ipc/1` wire. (phase 6)
- **Process boundary** — all logical guarantees hold across a real OS-process
  split, at protocol strength. (phase 6, `docs/PROCESS_BOUNDARY.md`)
- **Resource bounds** — a hostile but authorized client is held to the exact
  effect budget. (phase 6B, `docs/RESOURCE_BOUNDS.md`)
- **VM boundary** — a fully compromised guest reaches the host only through two
  mediated vsock endpoints; a 63-check probe finds no path to host state.
  (phase 7, `docs/VM_BOUNDARY.md`)
- **Evidence custody** — an independent witness detects all 18 tested
  ledger-corruption classes and fails closed; omitted / truncated / rolled-back
  entries are reconstructable, provenance-tagged. (phases 7B/7C,
  `docs/EVIDENCE-CUSTODY.md`)

## Explicit nonclaims

- No formal verification / no proof. BeigeWall supplies empirical invariants as
  *candidates* for formalisation; it proves none of them.
- QEMU/KVM is trusted; no VM-escape-CVE or side-channel analysis.
- The witness is tamper-**evident**, not tamper-**proof**; no cryptographic
  signing; no consensus/quorum.
- No production-security claim; no real desktop-data integration.
- Real-model results are small-sample exploratory telemetry, not statistics.
- No integration with Constellation, Jev, ABSD, or atproto-ACL exists or is
  claimed (`docs/ECOSYSTEM-ROLE.md` is positioning only).

## Future directions (separate questions — deliberately NOT pursued here)

These are tempting next steps that BeigeWall's result invites. Each is a
**different research question or an engineering project**, not a continuation,
and pursuing any of them would not strengthen the claim BeigeWall already
established. They are recorded here so the boundary of this work is explicit:

- **Cryptographically signed / attested witness** — only needed if the
  producer→witness path becomes untrusted; changes the trust model.
- **Byzantine / colluding writers, or N-of-M witnesses** — a distributed-systems
  question (consensus/quorum), explicitly out of BeigeWall's single-witness,
  no-consensus scope.
- **Suppression-resistant primary ledger** (reconstruct suppressed content
  beyond the witness) — the gap 7C leaves; an append-only/externally-anchored
  store question, not more VM hardening.
- **Stronger OS isolation** (containers, namespaces, seccomp, FUSE) layered
  under the VM boundary — engineering, not a new result.
- **Real desktop-data integration** (e.g. a real Messages/Mail/Calendar source)
  — a productisation and safety-review effort, out of scope for a synthetic
  corpus spike.
- **Model breadth** (many models, larger samples, statistical power) — turns the
  exploratory real-model telemetry into a proper study; a separate evaluation
  project.
- **A production daemon / deployment** — out of scope; all code here is
  research-grade.

## Findings index

- `FINDINGS.md` — canonical (normative) results and interpretation
- `FINDINGS_EVALUATOR.md` — receipt-grounded evaluator
- `FINDINGS_REALMODEL.md` — exploratory real-model results
- `FINDINGS_PHASE4.md` / `FINDINGS_PHASE5.md` / `FINDINGS_PHASE5B.md` /
  `FINDINGS_PHASE5C.md` — negotiation, task-view, lifecycle, context reset
- `FINDINGS_PHASE6.md` / `FINDINGS_PHASE6B.md` / `FINDINGS_PHASE6C.md` —
  process boundary, resource bounds, evidence durability
- `FINDINGS_PHASE7.md` / `FINDINGS_PHASE7B.md` / `FINDINGS_PHASE7C.md` —
  VM boundary, evidence custody, recoverable custody
- Design docs: `docs/PLAN.md`, `docs/EVALUATOR.md`, `docs/CAPABILITIES.md`,
  `docs/LIFECYCLE.md`, `docs/RESOURCE_BOUNDS.md`, `docs/EVIDENCE_DURABILITY.md`,
  `docs/PROCESS_BOUNDARY.md`, `docs/VM_BOUNDARY.md`, `docs/EVIDENCE-CUSTODY.md`,
  `docs/ECOSYSTEM-ROLE.md`
- Rename: `RENAME.md`

## Reproduce

```
python3 -m unittest discover -s tests          # 468 tests, stdlib only
python3 run_experiment.py --out /tmp/bw        # canonical, byte-identical
diff /tmp/bw/receipts.jsonl results/canonical/receipts.jsonl
```

VM and real-model reproduction: see `README.md` ("Requirements", "Usage") and
`docs/VM_BOUNDARY.md`.
