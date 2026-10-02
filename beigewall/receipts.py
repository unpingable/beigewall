"""Receipt ledger: the authoritative record of every broker operation.

The ledger is append-only. The agent's own explanation of what it did is a
separate `narrative` receipt and is never treated as authoritative; evaluation
compares narrative receipts against the operational receipt graph.
"""

import json
from dataclasses import dataclass, field


def canonical_hash(obj) -> str:
    """sha256 over canonical JSON (sorted keys, tight separators)."""
    import hashlib
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    seq: int
    ts: float
    kind: str
    task_id: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "receipt_id": self.receipt_id,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "task_id": self.task_id,
        }
        d.update(self.detail)
        return d


class LedgerError(Exception):
    """The sink could not be written: the ledger is failed and records
    nothing further (phase 6C). The caller decides what that means for the
    operation in flight; the ledger never pretends a row was kept."""


class Ledger:
    """In-memory ledger with an optional append-only JSONL sink.

    Phase 6C ordering: a row reaches the sink before it is visible in
    memory, so the in-memory view never contains a row the disk lacks.
    `record(..., _durable=True)` additionally fsyncs the row (used for the
    rows recovery depends on: effect intents, commits, aborts). After one
    sink failure the ledger is failed: every later record raises."""

    def __init__(self, clock, sink_path: str | None = None,
                 durable_kinds: frozenset | None = None, witness=None):
        self._clock = clock
        self._sink_path = sink_path
        # phase 7B: an independent witness receives each canonical receipt
        # directly from the producer, BEFORE the (possibly compromised) writer
        # persists it, and keeps its own out-of-custody record. Default None
        # keeps every existing run (and the canonical artifacts) unchanged.
        self._witness = witness
        self._receipts: list[Receipt] = []
        self._seq_offset = 0
        self.failed: Exception | None = None
        # kinds fsynced regardless of the caller's flag (the trusted server
        # names the rows an observer's conclusions depend on, phase 6C)
        self._durable_kinds = durable_kinds or frozenset()
        self._dir_synced = False

    @staticmethod
    def existing_rows(path: str) -> list[dict]:
        """Rows already in a JSONL sink (for a trusted-process restart)."""
        import os
        if not path or not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def resume_after(self, rows: list[dict]) -> None:
        """Continue receipt numbering after existing rows without loading
        them as live state: identities never collide across a restart.
        Grants and other authority state are not restored (by design)."""
        self._seq_offset = max((r["seq"] for r in rows), default=0)

    def record(self, kind: str, task_id: str, _durable: bool = False, **detail) -> Receipt:
        if self.failed is not None:
            raise LedgerError(f"ledger failed earlier: {self.failed}")
        seq = self._seq_offset + len(self._receipts) + 1
        r = Receipt(receipt_id=f"r-{seq}", seq=seq, ts=self._clock(),
                    kind=kind, task_id=task_id, detail=detail)
        if self._witness is not None:
            # independent custody first: the witness holds the truth even if the
            # writer below omits or corrupts the row
            self._witness.observe(r.to_dict())
        if self._sink_path is not None:
            try:
                self._sink_write(json.dumps(r.to_dict(), sort_keys=True) + "\n",
                                 _durable or kind in self._durable_kinds)
            except OSError as exc:
                self.failed = exc
                raise LedgerError(str(exc)) from exc
        self._receipts.append(r)
        return r

    def _sink_write(self, line: str, durable: bool) -> None:
        """One appended line; with `durable`, flushed and fsynced before
        returning (the only point where the process waits on the disk)."""
        import os
        created = not os.path.exists(self._sink_path)
        with open(self._sink_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            if durable:
                fh.flush()
                os.fsync(fh.fileno())
        if durable and (created or not self._dir_synced):
            # the file's own existence must survive power loss too
            dfd = os.open(os.path.dirname(os.path.abspath(self._sink_path)) or ".",
                          os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            self._dir_synced = True

    def all(self) -> list[Receipt]:
        return list(self._receipts)

    def by_task(self, task_id: str) -> list[Receipt]:
        return [r for r in self._receipts if r.task_id == task_id]

    def by_kind(self, kind: str) -> list[Receipt]:
        return [r for r in self._receipts if r.kind == kind]
