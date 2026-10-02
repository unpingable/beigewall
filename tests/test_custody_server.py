"""Phase 7B — evidence custody wired into the trusted server's restart path.

A witnessed server, on restart, verifies the primary ledger (the writer's
output) against the independent witness, fails closed on any divergence, and
resumes identities from the witness head — so a compromised writer that
tampers with, truncates or rolls back the ledger cannot make the server start
on a false history or reuse identities.
"""

import json
import os
import tempfile
import unittest

from beigewall.receipts import Ledger
from beigewall.witness import Witness
from beigewall.ipc.server import TrustedServer


def _seed(ledger_path, witness_path):
    """Write a small, consistent ledger + witness as an honest session would."""
    t = [1000.0]

    def clock():
        t[0] += 1.0
        return t[0]

    w = Witness(sink_path=witness_path)
    lg = Ledger(clock=clock, sink_path=ledger_path, witness=w)
    lg.record("grant_mint", "t1", grant_id="g-1", operation="read",
              resource="messages/thread/x")
    lg.record("read", "t1", grant_id="g-1", resource="messages/thread/x",
              projection={"name": "conversation@1"})
    lg.record("grant_mint", "t1", grant_id="g-2", operation="effect",
              resource="calendar/events")
    lg.record("effect", "t1", grant_id="g-2",
              effect={"name": "create_event", "params": {}}, derived_from=[])
    lg.record("task_end", "t1", answer="done", truncated=False)
    return w.head_seq


def _make_server(ledger_path, witness_path):
    # socket_path is unused by __init__ (only serve_forever touches it)
    return TrustedServer(os.path.join(os.path.dirname(ledger_path), "unused.sock"),
                         ledger_path, condition="control", witness_path=witness_path)


class TestWitnessedRestart(unittest.TestCase):
    def test_healthy_restart_passes_and_resumes_from_witness(self):
        with tempfile.TemporaryDirectory() as d:
            lg, wt = os.path.join(d, "l.jsonl"), os.path.join(d, "w.jsonl")
            head = _seed(lg, wt)
            srv = _make_server(lg, wt)
            self.assertEqual(srv.custody["restart_report_health"], "HEALTHY")
            self.assertFalse(srv.custody["decision"]["fail_closed"])
            # identities continue strictly after the witness head (no reuse),
            # and the witness keeps attesting new rows (incl. the restart marker)
            r = srv.ledger.record("read", "t1", grant_id="g-1", resource="x",
                                  projection={"name": "p"})
            self.assertGreater(r.seq, head)
            self.assertEqual(r.seq, srv.witness.head_seq)
            srv._lock_fh.close()

    def test_altered_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            lg, wt = os.path.join(d, "l.jsonl"), os.path.join(d, "w.jsonl")
            _seed(lg, wt)
            rows = [json.loads(x) for x in open(lg) if x.strip()]
            rows[1]["resource"] = "messages/thread/evil"      # tamper a read
            with open(lg, "w") as fh:
                for r in rows:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            with self.assertRaises(SystemExit) as c:
                _make_server(lg, wt)
            self.assertIn("ALTERED", str(c.exception))

    def test_rolled_back_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            lg, wt = os.path.join(d, "l.jsonl"), os.path.join(d, "w.jsonl")
            _seed(lg, wt)
            rows = [json.loads(x) for x in open(lg) if x.strip()]
            with open(lg, "w") as fh:                           # drop the tail
                for r in rows[:2]:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            with self.assertRaises(SystemExit) as c:
                _make_server(lg, wt)
            self.assertIn("ROLLED_BACK", str(c.exception))

    def test_omitted_row_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            lg, wt = os.path.join(d, "l.jsonl"), os.path.join(d, "w.jsonl")
            _seed(lg, wt)
            rows = [json.loads(x) for x in open(lg) if x.strip()]
            del rows[1]                                         # hide a disclosure
            with open(lg, "w") as fh:
                for r in rows:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            with self.assertRaises(SystemExit):
                _make_server(lg, wt)


if __name__ == "__main__":
    unittest.main()
