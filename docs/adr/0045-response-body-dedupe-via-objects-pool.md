# Response-body dedupe via the objects pool and hardlinks

Captured response bodies are deduplicated per project. `FlowRecordWriter.write_response`
hashes each body once in memory (sha256) and stores one canonical copy at
`.odda/objects/<sha256hex>`; every flow carrying those bytes gets a hardlink named
`response_body.<ext>` inside its own flow dir, `body_file` keeps pointing at that
hardlink (unchanged), and the flows.jsonl record gains `body_sha256`. The objects dir
doubles as the scanner's digest source: `_body_digest` reads the record field instead
of re-reading and re-hashing the body file, feeding the existing digest-keyed
body-stage memo. Measured on 70 real targets: 43.1 GB of bodies dedupe to 20.0 GB
(53%), and 77% of duplicate bytes arrive within a single session, so new-writes-only
(no migration, no priming of existing flows) captures most of the win.

Hardlinks instead of pool-only copies keep every reader unchanged: flow dirs stay
self-contained and raw-greppable (`rg -a flows/` finds strings in bodies — a
deliberate property the ws.jsonl latin-1 escaping also serves), the per-flow `.ext`
survives, and deleting `.odda/objects/` harms nothing because each hardlink keeps
the content alive. Write order is pool-first, hardlink-second: a crash in between
leaves a pool entry the next session links from, self-healing without any index
state. `os.link` failure (filesystem without hardlink support) falls back to writing
the body inline, the pre-ADR behavior.

Rejected alternatives:

- **Pool as the only copy** (`body_file` → objects path): breaks the learned contract
  that `flows/<id>/` contains the body; makes stored bodies ungreppable in place;
  scanner suffix sniffing loses the per-flow extension. Hardlinks cost one extra dir
  entry per body and none of that.
- **Compression** (zstd -6 measured: 0.454 ratio globally, 0.13–0.99 per project):
  the bigger saver on paper but it makes stored bodies opaque to `rg -a`, and the
  per-project spread (krisp 0.998, files.com 0.948 — already-compressed payloads)
  means the win is unreliable anyway.
- **Cross-project shared store**: only ~1.0 GB more savings than per-project unique
  bytes (19.0 vs 20.0 GB) in exchange for losing "delete the project dir and its
  odda state is gone" and for requiring refcounting.
- **One-time compaction/priming of existing flows**: deliberately rejected scope —
  existing data stays as-is; the 77% intra-session split means the pool earns most
  of its win from new captures alone.
- **Persistent jsonl body index**: the objects dir is the index (existence check per
  new body); no boot load, no append, no stale-entry healing.
- **blake3**: µs-level gain at avg 21 KB per body, a new Rust dependency, and the
  scanner already speaks sha256 — one digest, one computation, at the writer.

Consequences: `.odda/objects/` grows monotonically — no pruning is policy (ADR
scope: flows are never deleted); manually removing flow dirs orphans pool entries,
accepted. Requests (measured 92% unique), `ws.jsonl` transcripts, and response
headers stay undeduped. Pre-upgrade flows lack `body_sha256`; `_body_digest` hashes
those bodies as before. This is recorded because the on-disk shape (nlink>1 bodies,
a hash-named pool) is sticky: a future contributor must not "fix" the hardlinks into
copies, and the objects dir must stay kind-agnostic (request-file dedupe can land in
the same pool; identical bytes shared across kinds is correct, each hardlink keeps
its kind-specific name).
