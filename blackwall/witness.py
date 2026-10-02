"""Evidence custody (phase 7B): an independent witness and observer that
detect divergence of a *compromised primary ledger writer*.

Role separation (see docs/EVIDENCE-CUSTODY.md):

  1. authority / effect producer   — the trusted broker, performs the op
  2. receipt producer              — the trusted `Ledger`, mints the canonical
                                     receipt with a monotonic `seq` and a
                                     producer-assigned `receipt_id`
  3. primary ledger writer         — persists the primary JSONL ledger;
                                     ASSUMED COMPROMISED in this phase
  4. independent witness (this)    — receives each canonical receipt directly
                                     from the producer (NOT via the writer) and
                                     keeps a minimal, out-of-custody custody
                                     record: (epoch, seq, occurrence_id,
                                     content_hash, chain_hash)
  5. independent observer (this)   — compares the primary ledger against the
                                     witness history and classifies every
                                     divergence; never trusts the writer's own
                                     statements as independent evidence

The witness is *observational*: it issues no grants, authorizes no effect, is
never promoted into a verdict, and never silently repairs the primary ledger.
Detection here uses only sequence + content hashes + a hash chain — no
signing. Signing would be required only if the producer→witness path itself
were untrusted; in this topology it is not (an explicit nonclaim).

The custody record is deliberately minimal (hashes, not content): it proves
the primary diverged; it is not a second copy of the ledger and cannot by
itself reconstruct authority, budget or effect state. Where the witness shows
the relevant rows are intact, a rebuild from the primary is sound; where it
shows tampering, the safe response is to FAIL CLOSED, never to trust either
side or to let a shorter/edited history look like less authority was used.
"""

from .receipts import canonical_hash

GENESIS = "genesis"

# occurrence verdicts
MATCH = "MATCH"
OMITTED = "OMITTED"
ALTERED = "ALTERED"
DUPLICATED = "DUPLICATED"
REPLAYED = "REPLAYED"
REORDERED = "REORDERED"
FABRICATED = "FABRICATED"
# ledger-level health
HEALTHY = "HEALTHY"
TRUNCATED = "TRUNCATED"
ROLLED_BACK = "ROLLED_BACK"
FORKED = "FORKED"
WITNESS_UNAVAILABLE = "WITNESS_UNAVAILABLE"
WRITER_UNAVAILABLE = "WRITER_UNAVAILABLE"
INDETERMINATE = "INDETERMINATE"


def content_hash(row: dict) -> str:
    """Canonical hash of a primary receipt row exactly as the producer minted
    it. Primary rows carry no chain fields (the chain lives only in the
    witness), so canonical receipts stay byte-identical."""
    return canonical_hash(row)


def chain_step(prev_chain: str, seq: int, chash: str) -> str:
    return canonical_hash({"prev": prev_chain, "seq": seq, "content": chash})


class Witness:
    """Independent custody of the canonical receipt stream. Fed by the
    producer, one `observe` per minted receipt, in production order."""

    def __init__(self, sink_path: str | None = None, epoch: int = 1,
                 retain: bool = False):
        self.records: list[dict] = []
        self.epoch = epoch
        self._chain = GENESIS
        self._last_seq = 0
        self._sink_path = sink_path
        # phase 7C: retain the canonical receipt envelope (the full row) so an
        # omitted/truncated/rolled-back primary entry can be RECONSTRUCTED, not
        # just detected. Opt-in: hash-only mode (default) keeps the 7B record
        # shape and the "minimal, not a second copy" property. In retain mode
        # the witness holds canonical content and is a second EVIDENCE store —
        # still observational (it never authorizes); see docs/EVIDENCE-CUSTODY.
        self._retain = retain

    @classmethod
    def load(cls, path: str) -> "Witness":
        """Reload a witness from its own sink (host-side, out of writer
        custody). The chain and head resume from the stored records."""
        import json
        import os
        w = cls(sink_path=path)
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    w.records.append(rec)
            if w.records:
                w._chain = w.records[-1]["chain_hash"]
                w._last_seq = w.records[-1]["seq"]
                w.epoch = max(r["epoch"] for r in w.records) + 1
                w._retain = any("envelope" in r for r in w.records)
        return w

    @property
    def head_seq(self) -> int:
        return self._last_seq

    def observe(self, row: dict) -> dict:
        seq = row["seq"]
        if seq <= self._last_seq:
            # the producer is trusted and strictly monotonic; a regression
            # means the witness was fed out of order, which is a producer bug,
            # not a writer attack. Fail loudly rather than record a bad chain.
            raise ValueError(f"witness fed non-monotonic seq {seq} after {self._last_seq}")
        chash = content_hash(row)
        self._chain = chain_step(self._chain, seq, chash)
        rec = {"epoch": self.epoch, "seq": seq,
               "occurrence_id": row["receipt_id"], "content_hash": chash,
               "chain_hash": self._chain}
        if self._retain:
            # the canonical row exactly as minted; its hash is `chash`, so a
            # tampered envelope self-detects on reconstruction
            rec["envelope"] = dict(row)
        self.records.append(rec)
        self._last_seq = seq
        if self._sink_path is not None:
            self._persist(rec)
        return rec

    def _persist(self, rec: dict) -> None:
        import json
        import os
        with open(self._sink_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _index_by_seq(witness_records):
    return {r["seq"]: r for r in witness_records}


def classify(primary_rows, witness_records, *, at_restart: bool = False,
             prior_head: int | None = None) -> dict:
    """Compare a single primary-ledger snapshot against the witness history.

    Returns {health, detected, occurrences:[{seq, verdict, ...}], missing,
    fabricated, structural}. The writer's own fields are never used as
    independent evidence: a primary row counts only if the witness attests
    its (seq, content_hash)."""
    if not witness_records:
        return {"health": WITNESS_UNAVAILABLE, "detected": True,
                "occurrences": [], "missing": [], "fabricated": [],
                "structural": {"reason": "no witness records"}}
    w_by_seq = _index_by_seq(witness_records)
    w_seqs = [r["seq"] for r in witness_records]       # authoritative order
    w_head = w_seqs[-1]

    occ = []
    seen_counts = {}
    max_seq_seen = 0
    prev_seq = None
    matched_order = []        # seqs of MATCH/ALTERED rows, in file order
    for pos, row in enumerate(primary_rows):
        seq = row.get("seq")
        rid = row.get("receipt_id")
        chash = content_hash(row)
        n = seen_counts.get(seq, 0)
        seen_counts[seq] = n + 1
        verdict = None
        if seq not in w_by_seq:
            verdict = FABRICATED
        else:
            w = w_by_seq[seq]
            same = (chash == w["content_hash"] and rid == w["occurrence_id"])
            if n >= 1:
                # this seq already appeared earlier in the file
                if prev_seq == seq:
                    verdict = DUPLICATED
                elif seq < max_seq_seen:
                    verdict = REPLAYED
                else:
                    verdict = DUPLICATED
            elif not same:
                verdict = ALTERED
            else:
                verdict = MATCH
        if verdict in (MATCH, ALTERED, DUPLICATED, REPLAYED):
            matched_order.append(seq)
        occ.append({"seq": seq, "receipt_id": rid, "verdict": verdict, "pos": pos})
        max_seq_seen = max(max_seq_seen, seq if isinstance(seq, int) else 0)
        prev_seq = seq

    primary_seqs = set(seen_counts)
    missing = [s for s in w_seqs if s not in primary_seqs]
    fabricated = [o["seq"] for o in occ if o["verdict"] == FABRICATED]

    # reordering: the MATCH/ALTERED rows must appear in strictly increasing
    # seq order (the witness order); first-occurrence only
    first_pos_order = []
    seen = set()
    for s in matched_order:
        if s not in seen:
            seen.add(s)
            first_pos_order.append(s)
    reordered = first_pos_order != sorted(first_pos_order)
    if reordered:
        for o in occ:
            if o["verdict"] == MATCH:
                o["verdict"] = REORDERED

    structural = {"primary_head": max_seq_seen, "witness_head": w_head,
                  "missing_count": len(missing)}
    health = _overall_health(occ, missing, w_seqs, max_seq_seen, reordered,
                             at_restart, prior_head, w_head)
    detected = health != HEALTHY
    return {"health": health, "detected": detected, "occurrences": occ,
            "missing": missing, "fabricated": fabricated, "structural": structural}


def _overall_health(occ, missing, w_seqs, primary_head, reordered,
                    at_restart, prior_head, w_head):
    verdicts = {o["verdict"] for o in occ}
    if FABRICATED in verdicts:
        return FABRICATED
    if ALTERED in verdicts:
        return ALTERED
    if REPLAYED in verdicts:
        return REPLAYED
    if DUPLICATED in verdicts:
        return DUPLICATED
    if reordered or REORDERED in verdicts:
        return REORDERED
    if missing:
        # everything present MATCHes, but seqs are missing.
        present = {o["seq"] for o in occ}
        is_prefix = (present == set(w_seqs[:len(present)]))
        tail_missing = set(missing) == set(w_seqs[len(present):])
        if is_prefix and tail_missing:
            # primary is a clean prefix of the witness. If the witness head
            # is ahead of what the writer presents, the writer reverted to an
            # older state (rollback); otherwise it dropped its tail.
            if at_restart or (prior_head is not None and primary_head < prior_head):
                return ROLLED_BACK
            return TRUNCATED
        return OMITTED
    return HEALTHY


def classify_equivocation(snapshot_a, snapshot_b, witness_records) -> dict:
    """Two histories the writer presented (e.g. before and after a restart).
    A fork is a shared matching prefix followed by DIFFERENT content at the
    same seq — the writer equivocated. Returns {forked, fork_seq?}."""
    if not witness_records:
        return {"forked": False, "health": WITNESS_UNAVAILABLE}
    a = {r["seq"]: content_hash(r) for r in snapshot_a}
    b = {r["seq"]: content_hash(r) for r in snapshot_b}
    for seq in sorted(set(a) & set(b)):
        if a[seq] != b[seq]:
            return {"forked": True, "health": FORKED, "fork_seq": seq}
    return {"forked": False, "health": HEALTHY}


# health states from which missing canonical material can be reconstructed
# (pure omission of entries, not active rewriting of surviving ones)
RECOVERABLE_HEALTH = frozenset({OMITTED, TRUNCATED, ROLLED_BACK})


def verify_chain(witness_records) -> dict:
    """Re-derive the hash chain from the witness records' own content hashes
    and check it against the stored chain_hash of each record. Detects
    internal corruption of the witness store itself (defence in depth; the
    witness's primary protection is the out-of-custody boundary). Also checks
    any retained envelope hashes to its record's content_hash."""
    chain = GENESIS
    last = 0
    for r in witness_records:
        if r["seq"] <= last:
            return {"ok": False, "reason": "non-monotonic witness seq", "seq": r["seq"]}
        if "envelope" in r and content_hash(r["envelope"]) != r["content_hash"]:
            return {"ok": False, "reason": "envelope does not match its hash",
                    "seq": r["seq"]}
        chain = chain_step(chain, r["seq"], r["content_hash"])
        if chain != r["chain_hash"]:
            return {"ok": False, "reason": "broken chain", "seq": r["seq"]}
        last = r["seq"]
    return {"ok": True, "head_chain": chain, "head_seq": last}


def reconstruct(primary_rows, witness_records) -> dict:
    """Phase 7C: where the primary ledger is merely MISSING canonical entries
    (OMITTED / TRUNCATED / ROLLED_BACK), rebuild the full canonical event list
    from the witness's retained envelopes, with every entry tagged by
    provenance ("original" = survived in the primary; "recovered" = supplied by
    the witness). The reconstruction is re-verified against the attested chain,
    so it is exactly the producer's history — not an invented one.

    Recovery is refused (recoverable=False) when:
      * surviving entries were actively rewritten (ALTERED/DUPLICATED/REPLAYED/
        REORDERED/FABRICATED/FORKED) — detect and fail closed, never "fix";
      * the witness is unavailable, its chain is broken, or it retained hashes
        only (detectable but not reconstructable → INDETERMINATE).

    This returns EVIDENCE only. It mints no authority, restores no budget,
    authorizes no effect, and never claims the writer persisted the recovered
    rows."""
    report = classify(primary_rows, witness_records)
    health = report["health"]
    if not witness_records:
        return {"recoverable": False, "health": WITNESS_UNAVAILABLE,
                "reason": "no witness", "reconstructed": None}
    chain_ok = verify_chain(witness_records)
    if not chain_ok["ok"]:
        return {"recoverable": False, "health": INDETERMINATE,
                "reason": "witness integrity: " + chain_ok["reason"],
                "reconstructed": None}
    if health == HEALTHY:
        recon = [{"seq": r["seq"], "provenance": "original",
                  "row": _primary_by_seq(primary_rows)[r["seq"]]}
                 for r in witness_records]
        return {"recoverable": True, "health": HEALTHY, "reason": "intact",
                "reconstructed": recon, "recovered_count": 0}
    if health not in RECOVERABLE_HEALTH:
        return {"recoverable": False, "health": health,
                "reason": f"{health} is active tampering, not recoverable omission",
                "reconstructed": None}
    if not all("envelope" in r for r in witness_records):
        return {"recoverable": False, "health": INDETERMINATE,
                "reason": "witness retained hashes only; detectable, not "
                          "reconstructable", "reconstructed": None}
    primary = _primary_by_seq(primary_rows)
    recon = []
    rebuilt_chain = GENESIS
    for r in witness_records:
        seq = r["seq"]
        if seq in primary and content_hash(primary[seq]) == r["content_hash"]:
            row, prov = primary[seq], "original"
        else:
            row, prov = dict(r["envelope"]), "recovered"
        # every entry re-verified against the witness content hash
        if content_hash(row) != r["content_hash"]:
            return {"recoverable": False, "health": INDETERMINATE,
                    "reason": f"reconstruction hash mismatch at seq {seq}",
                    "reconstructed": None}
        rebuilt_chain = chain_step(rebuilt_chain, seq, r["content_hash"])
        recon.append({"seq": seq, "provenance": prov, "row": row})
    if rebuilt_chain != witness_records[-1]["chain_hash"]:
        return {"recoverable": False, "health": INDETERMINATE,
                "reason": "reconstructed chain does not match the witness head",
                "reconstructed": None}
    return {"recoverable": True, "health": health,
            "reason": f"reconstructed {sum(1 for x in recon if x['provenance']=='recovered')} "
                      "missing entries from witness custody",
            "reconstructed": recon,
            "recovered_count": sum(1 for x in recon if x["provenance"] == "recovered")}


def _primary_by_seq(primary_rows):
    out = {}
    for row in primary_rows:
        out.setdefault(row.get("seq"), row)   # first occurrence wins
    return out


def recovery_decision(report: dict, *, safety_dependent: bool = True) -> dict:
    """Map an observer report to a recovery state and a fail-closed decision.

    Fail-closed means: do not perform any operation whose safety depends on
    the ledger's completeness or integrity. Absence of witness evidence is
    NOT evidence that nothing happened; it fails closed. A witness receipt is
    never promoted to authority, and a disagreeing history is never silently
    rebuilt and called original."""
    health = report["health"]
    if health == HEALTHY:
        return {"state": HEALTHY, "fail_closed": False,
                "safe_to_continue": True,
                "reason": "primary ledger agrees with the witness"}
    reason = {
        WITNESS_UNAVAILABLE: "no independent witness; cannot confirm the ledger",
        ALTERED: "a receipt's content diverges from the witness",
        FABRICATED: "the ledger contains a receipt the producer never emitted",
        REPLAYED: "an old receipt was re-presented",
        DUPLICATED: "a receipt was persisted more than once",
        REORDERED: "receipts are out of the producer's order",
        OMITTED: "receipts the witness attests are missing",
        TRUNCATED: "the ledger tail the witness attests is missing",
        ROLLED_BACK: "the ledger reverted to an earlier state than the witness",
        FORKED: "the writer presented divergent histories (equivocation)",
        INDETERMINATE: "evidence is insufficient to classify",
    }.get(health, "ledger diverges from the witness")
    # every divergence fails closed for safety-dependent operations; it never
    # silently continues. Non-safety-dependent callers may still observe.
    return {"state": health, "fail_closed": bool(safety_dependent),
            "safe_to_continue": not safety_dependent, "reason": reason}


def budget_safe_consumption(primary_rows, witness_records, effect_resource) -> dict:
    """Trusted effect-budget consumption, rebuilt from the primary ledger
    ONLY when the witness attests the primary is intact. Any divergence fails
    closed: a compromised writer can never make consumption look smaller (or
    larger) and thereby change what further effects are permitted.

    Returns {fail_closed, reason, consumption?} where consumption maps a
    resource scope to its consumed count (as resources.ResourceLedger would
    rebuild it). On fail_closed the caller must DENY further effects whose
    safety depends on the count — never fall back to a writer-supplied number."""
    report = classify(primary_rows, witness_records)
    decision = recovery_decision(report, safety_dependent=True)
    if decision["fail_closed"]:
        return {"fail_closed": True, "state": decision["state"],
                "reason": "ledger integrity unverified: " + decision["reason"],
                "consumption": None}
    # witness-attested intact: a rebuild from the primary is sound
    consumption = {}
    for row in primary_rows:
        if row.get("kind") == "effect":
            eff = row.get("effect")
            if isinstance(eff, dict):               # canonical shape {name, params}
                name, params = eff.get("name", ""), eff.get("params") or {}
            else:                                    # flat shape {effect_name, params}
                name, params = row.get("effect_name", ""), row.get("params") or {}
            res = effect_resource(name, params)
            if res:
                consumption[res] = consumption.get(res, 0) + 1
    return {"fail_closed": False, "state": HEALTHY, "reason": "witness-attested",
            "consumption": consumption}
