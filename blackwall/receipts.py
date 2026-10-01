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


class Ledger:
    """In-memory ledger with an optional append-only JSONL sink."""

    def __init__(self, clock, sink_path: str | None = None):
        self._clock = clock
        self._sink_path = sink_path
        self._receipts: list[Receipt] = []

    def record(self, kind: str, task_id: str, **detail) -> Receipt:
        seq = len(self._receipts) + 1
        r = Receipt(receipt_id=f"r-{seq}", seq=seq, ts=self._clock(),
                    kind=kind, task_id=task_id, detail=detail)
        self._receipts.append(r)
        if self._sink_path is not None:
            with open(self._sink_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r.to_dict(), sort_keys=True) + "\n")
        return r

    def all(self) -> list[Receipt]:
        return list(self._receipts)

    def by_task(self, task_id: str) -> list[Receipt]:
        return [r for r in self._receipts if r.task_id == task_id]

    def by_kind(self, kind: str) -> list[Receipt]:
        return [r for r in self._receipts if r.kind == kind]
