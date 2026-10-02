# Project rename: BLACKWALL → BeigeWall

On 2026-10-02, after Phase 7C sealed, this project was permanently renamed from
**BLACKWALL** to **BeigeWall**. The rename is mechanical — package, CLI, module
references, environment variables, tests and documentation — not a semantic
rewrite. The research, phases and results are unchanged.

(The former name collided with an unrelated fictional product; "BeigeWall" was
chosen as the deliberately unglamorous successor. See the older
`blackwall-public-rename` note.)

## What changed

- Python package `blackwall/` → `beigewall/`; all imports and `python3 -m
  blackwall.…` → `beigewall.…`.
- Environment variables `BLACKWALL_LLM_*` → `BEIGEWALL_LLM_*`.
- Wire protocol id `blackwall-ipc/1` → `beigewall-ipc/1` (no external
  consumers; nothing in the sealed ledgers carries it).
- Documentation, findings prose and reproduce instructions refer to BeigeWall.

## Preserved as historical / compatibility references (intentional, not stale)

1. **Sealed artifacts under `results/`** (canonical and exploratory) are left
   byte-for-byte as produced under the BLACKWALL name. They are historical
   evidence and must not be rewritten; the canonical set is verified
   byte-identical by `run_experiment.py`.
2. **The canonical experiment report title** `# BLACKWALL experiment report`
   (in `beigewall/experiment.py`) is frozen verbatim so `run_experiment.py`
   regenerates the sealed `results/canonical/report.md` byte-identically.
3. **The on-disk git working tree** remains at `/data/git/blackwall` (the
   repository directory was not moved, to avoid disturbing the enclosing
   workspace); the three `/data/git/blackwall` path references name that
   location.
4. **Phase findings** (`FINDINGS_*.md`) describe experiments some of which were
   conducted under the BLACKWALL name; their narratives are historically
   accurate and were not rewritten to imply otherwise.

No other `blackwall`/`Blackwall`/`BLACKWALL` occurrences remain in tracked,
non-`results/` source — verified after the rename.
