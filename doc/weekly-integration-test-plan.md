# Proposal: Weekly Cross-Repo Integration Test (tapyrus-core + tapyrus-signer + tapyrus-seeder)

Status: **Draft for team review**

## 1. Motivation

Today's aggpubkey/reorg/maxblocksize coverage (`feature_federation_management.py`,
`feature_xfield_maxblocksize.py` in tapyrus-core) is fast and self-contained *because it fakes
the signing ceremony* — blocks are hand-signed in Python with hardcoded test private keys. That's
good for exercising tapyrus-core's validation logic in isolation, but it has never actually
exercised:

- a real tapyrus-signer threshold-signing ceremony producing a genuine aggpubkey,
- the two repos' current `master`/working branches interoperating at all.

Each repo's CI is green independently; nothing today proves they still work *together*. This
proposal adds a weekly (not per-PR — too slow/expensive) job that sets up a real, isolated
network and drives it through the operations a live federation actually performs.

It also gives tool and dependency drift somewhere safe to surface. Base images, toolchains, and
upstream branches age quietly between releases — when they diverge or go stale, this environment
can make it visible.

## 2. Scope of the test scenario

1. **Bootstrap a signer network**: 3 tapyrus-signer nodes (threshold 2-of-3) run the
   `tapyrus-setup` federation-setup ceremony (see section 4a) to produce a shared aggregate public
   key. This is a fully offline, scriptable CLI ceremony — no live tapyrus-core node is touched by
   this step, but **Redis is required from here on** (the signer daemons coordinate rounds over
   it) — see section 4b's "Runtime services" note.
2. **Sign the genesis block**: the same 3 signers run `tapyrus-setup`'s genesis-signing ceremony
   (section 4a) against an unsigned genesis candidate (built by tapyrus-core's own
   `tapyrus-genesis -signblockpubkey=<aggpubkey>`, no private key) to produce a validly-signed
   genesis block — also fully offline (no Redis needed for this specific step either, only from
   step 3 onward once `tapyrus-signerd` itself is running).
3. **Bootstrap a core network**: 7 tapyrus-core nodes, genesis-configured with the signed genesis
   from step 2, in the specific topology described in section 4b (not a flat/full-mesh network) —
   this shapes later work testing how an adversarial node could exploit the topology.
4. **Per-node activity**: each of the 7 core nodes creates and broadcasts a random transaction.
   **PRNG must be seeded deterministically per run** (e.g. `github.run_id` combined with the
   node's own index) — an unseeded PRNG makes CI non-deterministic and un-reproducible on
   failure, which matters for a job that already runs infrequently (weekly) and takes hours.
5. **Per-node lifecycle**: for each node — query state (RPC health/height/mempool checks), stop,
   restart, confirm it rejoins/resyncs correctly.
6. **Aggpubkey rotation**: a *second*, disjoint signer set (signer-set-b, same 3-node/threshold-2
   shape, independent keys) runs its own setup ceremony (step 1) to produce a new aggpubkey; the
   **current** federation (signer-set-a) signs off on the handoff to it (`tapyrus-setup sign
   --xfield` / `computesig --xfield` ); all `tapyrus-signerd` processes restart
   with a `federations.toml` containing both the original and the new entry, rotating control at
   the scheduled height.
7. **Max block size change**: push an xfield block changing max block size; confirm it's honored
   (a block violating the new limit is rejected, one respecting it is accepted).
8. **Reorg**: independent of step 6's rotation (tapyrus won't reorg past a federation-change block,
   so a genuine reorg needs an ordinary chain split *within* the current federation, not the
   rotation mechanism) — must therefore run *before* step 6 in the actual CI flow (section 3 step
   8), not after. **Verdict: GO** — run for real against the local spike as a genuine two-sided
   fork, not a partition heal. Full recipe, the one required fix it depends on, and how both were
   verified are all in section 4d; not repeated here.

## 3. Step-by-step CI flow (draft)

1. Checkout tapyrus-core, tapyrus-signer, tapyrus-seeder
2. Build both Docker images.
3. Run the offline `tapyrus-setup` ceremony (section 4a, steps 1–3) for signer-set-a to produce
   its aggregated public key — no containers needed yet for this part.
4. Run `tapyrus-genesis -signblockpubkey=<aggpubkey>` (unsigned), then the offline genesis-signing
   ceremony (section 4a, steps 4–7) to produce the signed genesis block; write `genesis.<networkid>`.
5. `docker compose up` **redis**, the 7 core containers (topology per section 4b) using that
   genesis file, and the 3 signer-set-a containers with a `federations.toml` containing the
   genesis entry. (`tapyrus-seeder` is a separate service, not required for this or any later
   step — see section 4b's "Runtime services" note.)
6. Wait for all 7 nodes to be up and connected per the intended topology (peer count / peer
   address checks per node, not just "any 7 are up"). **Concretely**: give each `core-*` service a
   compose healthcheck against its own RPC port (e.g. `getblockchaininfo`), and make every
   `signer-*` service's `depends_on` use `condition: service_healthy` on its RPC target, not the
   default `service_started` — a signer that starts before its target's RPC is actually accepting
   connections will race it and can crash or spin retrying, exactly like the `tapyrus-signerd`
   startup panic documented in `doc/work-done.md`. Only proceed to step 7 once every healthcheck
   is green.
7. Orchestrator drives per-node tx/query/stop-restart (scenario steps 4–5) and the max-block-size
   change (step 7).
8. **Reorg (scenario step 8, full design in section 4d) — runs here, before step 9's rotation,**
   because tapyrus won't reorg past a federation-change block: doing this after signer-set-b's
   handoff has landed would make a genuine reorg untestable for the rest of the run.
9. Run the offline ceremony again for signer-set-b (its own aggpubkey), then the `--xfield`
   sign/computesig flow (section 4a) with signer-set-a signing off on the handoff; regenerate
   `federations.toml` with both entries; restart all `tapyrus-signerd` processes (signer-set-a's
   eventually stopping, signer-set-b's taking over) at the scheduled rotation height.
10. Confirm the rotation took effect at the scheduled height.
11. Teardown; always collect logs (`docker logs` per container) and upload as CI artifacts,
    especially on failure.
12. **Send a report to the tapyrus slack channel** summarizing the run — see section 3a below.
    This step runs unconditionally (`if: always()`), so a report goes out whether the run passed
    or failed.

### 3a. Report on slack

Sent once, at the very end of every run (pass or fail). Contents:

- Overall pass/fail, and which of the scenario steps (section 2) it got through before failing,
  if it failed.
- Run metadata: trigger (scheduled vs manual), repo refs/commits tested for both repos, run
  duration, link to the full CI run.
- The aggpubkey(s) generated during the run (signer-set-a's initial key, signer-set-b's
  post-rotation key).
- On failure: which node(s)/container(s) were implicated, **and the last ~100 lines of that
  container's own log inlined directly in the message** (before the link to the full artifact) —
  for a job that only runs weekly and takes hours, this lets a first-cut triage happen without
  leaving Slack; the full artifact link stays for anything deeper.

## 4. Proposed architecture

### 4a. The federation-setup ceremony (verified against real `tapyrus-setup` source)

All of this is driven by the `tapyrus-setup` CLI , run by an orchestrator that shells out to
each signer's copy of the binary and pipes outputs between steps — no Redis or live tapyrus-core
node is touched by any of this:

**Aggpubkey generation** (per signer `i`, of `n`, threshold `t`):
1. `tapyrus-setup createkey` → `(private_key[i], public_key[i])`. Exchange all `public_key[]`
   between signers; sort them (this sorted order is each signer's index for later steps).
2. `tapyrus-setup createnodevss --public-key=<all n> --private-key=<own> --threshold=<t>` → one
   `node_vss[i,j]` per receiver `j` (including self). Distribute `node_vss[i,j]` to signer `j`
   out-of-band (the orchestrator can just do this directly, since it's all synchronous CLI I/O).
3. `tapyrus-setup aggregate --vss=<all node_vss[*, i] addressed to me> --private-key=<own>` →
   `(aggregated_public_key, node_secret_share[i])`. Every signer runs this independently and
   arrives at the identical `aggregated_public_key`.

**Genesis-signing** (after tapyrus-core's `tapyrus-genesis -signblockpubkey=<aggregated_public_key>`
produces an unsigned genesis hex, no `-signblockprivatekey`):
4. `tapyrus-setup createblockvss --public-key=<all n> --private-key=<own> --threshold=<t>` → block
   VSS blobs, distributed the same way as node VSS.
5. `tapyrus-setup sign --block-vss=<all n addressed to me> --aggregated-public-key=<...>
   --node-secret-share=<own> --private-key=<own> --block=<unsigned genesis hex> --threshold=<t>` →
   `local_sig[i]`. Each signer broadcasts (shares) their local signature.
6. **One designated signer — fixed as `signer-0` for v1** — runs `tapyrus-setup computesig
   --sig=<t of the local_sig[]> --private-key=<own> --block=<unsigned genesis hex>
   --block-vss=<own> --node-vss=<own> --aggregated-public-key=<...> --node-secret-share=<own>
   --threshold=<t>` → the final signed genesis block hex. `computesig` needs one specific
   signer's own key material to run — it can't be run by a neutral party without borrowing a
   signer's secrets, so the picker can't be "any available signer" without also handing that
   signer's secrets to the orchestrator. Hardcoding `signer-0` is fine for v1 (if `signer-0` is
   down, the run fails, which is an acceptable v1 limitation); making this dynamic/fault-tolerant
   (falling back to another signer's own secrets if the fixed pick is unavailable) is a v2 item —
   see section 6.
7. Write the result to a `genesis.<networkid>` file, distribute to all tapyrus-core nodes.

**Federation change**: a `federations.toml` file with a list of entries, each with `block-height`,
`threshold`, `node-vss`, and *either* `aggregated-public-key` *or* `max-block-size` (exactly one).
Every entry except `block-height = 0` (genesis) requires a `signature` field — the hex-encoded
output of the `--xfield` sign/computesig flow above, produced by the *previous* federation. So a
rotation is deployed by regenerating `federations.toml` with the new entry appended and restarting
every signer process — not a live call.

### 4b. Core network topology (7 nodes) — deliberately not full-mesh

This shapes a planned follow-up (an adversarial node exploiting the topology — out of scope for
v1 itself, but the topology is designed now so that extension doesn't require rework):

- **First layer** (3 nodes, `core-1a`/`core-2a`/`core-3a`): each is the RPC target for exactly one
  of the 3 signers (`signer-0 → core-1a`, `signer-1 → core-2a`, `signer-2 → core-3a`).
- **Second layer** (3 nodes, `core-1b`/`core-2b`/`core-3b`): each is P2P-connected *only* to its
  corresponding first-layer node (`core-1a ↔ core-1b`, etc.) — no other P2P edges.
- **Node 7** (`core-7`): P2P-connected to **all three** second-layer nodes (`core-7 ↔ core-1b`,
  `core-7 ↔ core-2b`, `core-7 ↔ core-3b`). An earlier draft of this section only specified "any
  two ... e.g. core-1b, core-2b", which would leave `core-3a`/`core-3b` an isolated pair with no
  path to the other 5 nodes — confirmed with the team that full connectivity is required (the
  sync/lifecycle/reorg scenario steps depend on all 7 nodes being able to reach each other), so
  this is resolved as all three, not two.
- **(Future, not v1)**: an adversary core node connecting P2P to **two first-layer nodes**
  (`core-1a`, `core-2a`) — i.e. attaching itself closer to the signer-facing nodes than the
  legitimate topology would allow, to explore what that lets it observe/influence.
- **Enforcement is one `-connect` per edge, not both ends** — a real, live-verified correction to
  an earlier draft of this section, which called for `-connect` on both ends of every edge. That
  turned out to be broken: tapyrus-core auto-disables listening the instant `-connect` is set at
  all (`InitParameterInteraction: -connect set -> setting -listen=0`), so if both ends of an edge
  set it, *neither* can ever accept the other's inbound connection. The working design instead has
  exactly one side of each edge dial out via `-connect` (the "child" dials its "parent"), with an
  explicit `-listen=1` added back wherever a node also needs to accept an inbound edge — see
  `docker/docker-compose.yml`'s own comments for the full per-node breakdown.

**Enforcement mechanism**: this topology is controlled entirely at the tapyrus-core P2P layer via
`-connect=<peer>` (which, used consistently, both restricts a node's own outbound dialing to just
its listed peers and disables its automatic DNS-seed/addrman-driven discovery). All containers
still sit on one flat docker-compose network — deliberately, **not** segmented into separate Docker
networks per edge — because the planned adversary extension needs to actually be able to reach
first-layer nodes at the network level; if Docker itself blocked that reachability, the exploit
scenario the topology exists to enable would be structurally impossible to build later.

**Runtime services (Redis and tapyrus-seeder) — not part of the `-connect` topology above, but
required/present in the compose picture**:

- **Redis is a required service, not optional infrastructure.** It's not needed for the offline
  ceremony (section 4a) — `tapyrus-setup` never touches it — but every `tapyrus-signerd` process
  coordinates its rounds (candidate blocks, VSS shares, completed-block announcements) over Redis
  pub/sub, so it must be up before any signer container starts (section 3 step 5 onward) and stay
  up for the rest of the run. Name it as an explicit `redis` service in the compose file, same as
  every `core-*`/`signer-*` service.
- **`tapyrus-seeder` is checked out (section 3 step 1) but is not part of this topology's peer
  discovery.** The `-connect` graph above is fully explicit and static — every edge is hardcoded,
  and container DNS resolves the hostnames — so nothing in the v1 scenario actually depends on
  `tapyrus-seeder` crawling the network or serving DNS results to anyone. It's included purely for
  its *own* standalone coverage (it's a real integration point between tapyrus-seeder and a live
  tapyrus-core node's P2P/RPC surface, and the local spike found and fixed four real bugs in it —
  see `doc/work-done.md` — so it's worth exercising even though nothing else consumes its output).
  Run it as its own service, pointed at one core node, with its own pass/fail check (`dig` against
  it resolves a real discovered peer) — but don't wire anything else in the scenario to depend on
  it succeeding.

### 4c. Images, versioning, hosting, scheduling

- **Images**: tapyrus-core and tapyrus-signer have working Dockerfiles already.
- **Versioning**: the job checks out `master` (or a pinned tag/branch) of tapyrus-core and
  tapyrus-signer independently and builds fresh images each run — this would catch drift between
  independently released repos before it reaches production. **`tapyrus-seeder` is pinned to a
  known-good tag/commit instead of tracking its own `master`** — it isn't one of the two repos
  this job exists to catch drift on, and its upstream source is old enough (2019-era, four real
  bugs found and fixed just to get it working at all — see `doc/work-done.md`) that silently
  tracking its `master` risks an unrelated seeder-side breakage failing the whole run for a repo
  nobody was trying to test. Bump the pin deliberately when there's a reason to.
- **Where it lives**: a **new, dedicated repo** (`tapyrus-integration-tests`) owning the compose
  file, orchestration script, and its own CI workflow — since neither existing repo should "own"
  a test that spans both. A local-only scaffold under this name exists as spike work to de-risk
  the design above (see `doc/work-done.md` for what's been verified there); it has not been
  pushed anywhere and stands separately from team approval to make it a real, permanent repo.
- **Scheduling**: weekly github action, plus `workflow_dispatch` for on-demand runs, mirroring the pattern
  already used by tapyrus-core's `weekly-heavy-tests.yml` (long timeout, artifact/log upload on
  failure, core-dump collection where applicable).
- **Runtime budget**: expected to run **4-6 hours** end-to-end, generous relative to the per-step
  model below to leave headroom for CI-specific overhead (cold image pulls, no warm build cache)
  that the local spike's timings don't include. **Assumes a fast, CI-appropriate
  `round-duration`** (the local spike settled on `round-duration = 60` seconds — see
  `doc/work-done.md` — not mainnet's ~10-minute block interval; running this scenario at mainnet
  pacing would make the reorg step alone (section 4d, ~20+ blocks across both sides) take upwards
  of 3+ hours by itself, blowing the whole budget on one step):

  | Step | What | Estimated time |
  | --- | --- | --- |
  | 3.1–3.2 | Checkout + build both Docker images (cold) | 10–15 min |
  | 3.3–3.4 | Offline ceremony (aggpubkey + genesis-signing, all local CLI) | < 1 min |
  | 3.5–3.6 | Bring up 7 core + redis + 3 signers, confirm topology + healthchecks | 2–5 min |
  | 3.7 (tx/query/lifecycle) | Per-node tx + query + stop/restart/resync, all 7 nodes | 20–35 min |
  | 3.7 (max-block-size) | xfield block lands and is confirmed honored | 2–3 min |
  | 3.8 (reorg, section 4d) | Baseline build + both groups' block production (≈20+ blocks total across the two forks at `round-duration = 60`) + reconnect/convergence (seconds) | 20–30 min |
  | 3.9 (rotation) | signer-set-b ceremony + `--xfield` signoff + config regen/restart + wait for scheduled rotation height | 10–15 min |
  | 3.10 | Confirm rotation took effect | < 1 min |
  | 3.11 | Teardown, log collection, artifact upload | 3–5 min |
  | 3.12 | Slack report | < 1 min |
  | **Total (well-behaved run)** | | **~70–110 min** |

  This leaves the stated 4-6 hour budget as a safety ceiling (retries, transient round failures
  like the benign `"duplicate"`-response warning documented in `doc/work-done.md`, slower CI
  runners) rather than the expected common case — worth revisiting once a real orchestrator run
  gives actual numbers instead of this estimate.

### 4d. Reorg

Run for real against the local spike, not just designed — a genuine two-sided
fork, built by two isolated groups each independently threshold-signing their own blocks from a
common tip, reconnected, and confirmed via `getchaintips` showing the losing side's own former tip
as a real `status: "valid-fork"` entry with the correct `branchlen`. The recipe below is the actual
sequence that was run (see `doc/work-done.md`, "The full two-group fork-and-reconverge reorg", for
the full transcript); the one required fix it depends on — repointing the isolated group's signers
to its own surviving core node — is folded into the steps below, not an afterthought.

1. **Build a common baseline.** Let the full, connected 7-node network (default signer mapping:
   `signer-0→core-1a`, `signer-1→core-2a`, `signer-2→core-3a`) run to some tip — it doesn't need to
   be genesis, just a height every node agrees on before the split (verified run: height 30).
   Confirm identical tips on all 7 nodes, then stop the 3 signers to freeze this baseline cleanly.
2. **Split.** Stop `core-3a`, `core-3b`, and `core-7` (3 of the 7 core nodes). Leave `core-1a`,
   `core-1b`, `core-2a`, `core-2b` running — this group is naturally one connected component
   (`core-1a↔core-1b`, `core-2a↔core-2b` are real P2P edges) with two of the three signers'
   (`signer-0`, `signer-1`) RPC targets live, safely meeting the threshold-2 requirement. Restart
   the 3 signers with their RPC mapping **unchanged** — no repoint needed for this side. (`signer-2`
   will fail to start with no reachable RPC target; expected and harmless.)
3. **First group builds its fork.** With the 4-node group live, let the signer network extend the
   chain some number of blocks past the baseline (verified run: **10 blocks**, to height 40), then
   freeze it: stop the 3 signers, then stop all 4 of that group's core nodes. Record its tip hash.
4. **Bring the second group back — still at the old tip.** Restart `core-3a`/`core-3b`/`core-7`,
   which have been down since step 2, so they're still sitting at the baseline tip, unaware of the
   first group's blocks. Reset Redis fresh (a clean slate for round-coordination state, avoiding
   any carryover from step 3's rounds).
5. **Repoint and restart — the required fix, not optional.** Repoint all three signers'
   `rpc-endpoint-host` to `core-3a` and restart the `tapyrus-signerd` processes. Without this, this
   group cannot sign at all (a signer with a dead RPC target can't contribute a threshold share,
   even purely as a non-master over Redis — confirmed directly: 5 full rounds with no repoint, zero
   progress), and the "reorg" would really just be a partition heal — the first group's blocks
   simply propagating in on reconnect, nothing to reorg away. With the repoint, the chain resumed
   advancing immediately and sustainably (verified over 27+ consecutive blocks in a standalone
   check, then again over the 39 blocks of this run), propagating correctly via P2P relay to
   `core-3b` and `core-7` throughout.
6. **Second group builds the longer fork.** Let it extend the chain further past the baseline than
   the first group did (verified run: targeted 12 blocks, actually reached **39 blocks**, to
   height 69, due to ordinary polling-interval slack — either is fine, the only hard requirement is
   an unambiguously longer chain, not a specific margin), then freeze it (stop the 3 signers).
   Record its tip hash and confirm it diverges from the first group's chain.
7. **Reconnect.** Bring the first group's core nodes (`core-1a`/`core-1b`/`core-2a`/`core-2b`)
   back up alongside the still-running second group.
8. **Confirm convergence — via `getchaintips`, not just height.** `getconnectioncount` on all 7
   nodes should re-match the topology's expected pattern (1/2/1/2/1/2/3, section 4b) within
   seconds, and `getblockchaininfo` should show the same (longer) tip everywhere. The load-bearing
   check is `getchaintips` on every node that had been in the first (losing) group: it must show
   exactly two tips — the new shared tip (`status: "active"`) and its own former tip
   (`status: "valid-fork"`, `branchlen` matching step 3's block count exactly). Verified run: all
   four ex-first-group nodes showed `height: 69 / active` and `height: 40 / valid-fork,
   branchlen: 10`; `core-3a` (which was on the winning side throughout) showed a single active tip,
   as expected — it never had a competing fork of its own to abandon.

## 5. Open questions to resolve before implementation

- **Key management in CI — smaller than it first looks.** The ceremony (section 4a) generates
  fresh key shares every run, entirely inside the runner (`generate-dev-secrets.sh`, verified —
  see `doc/work-done.md`); nothing needs to be pre-provisioned or committed, and the shares never
  need to leave the runner's own filesystem. So there's no real secret-provisioning problem here —
  the only actual GitHub secret this job needs is the Slack webhook URL (section 3a).
- **New repo setup**: `tapyrus-integration-tests` pushed github repo; initial owners; whether
  it needs its own release/versioning story or is purely a CI-only repo.

## 6. Explicitly out of scope for v1

- The adversarial-node topology exploit (section 4b) — the v1 topology is deliberately designed to
  make this addable later without rework, but building/running that adversary node is its own
  follow-up effort.
- Performance/load testing (this is a correctness/interop smoke test, not a benchmark).
- Testing signer fault tolerance (e.g. Byzantine signers, signer count below threshold) — could be
  a v2 addition once the basic ceremony works end-to-end.
- Running this on every PR — cost and runtime make that impractical; weekly + on-demand only.


##  Spike work already done (local-only, not yet team-reviewed)

A local git repo `tapyrus-integration-tests` (sibling to the tapyrus-core/tapyrus-signer/
tapyrus-seeder checkouts, **not pushed anywhere**) has some early scaffolding, done to de-risk
this plan before asking the team to commit to it.

### The real `tapyrus-setup` ceremony — verified fully end-to-end

Built `tapyrus-setup`/`tapyrus-signerd` from `163_federationChangeTomlSetup` and manually drove
the complete ceremony for 3 signers, threshold 2, by hand (raw CLI calls, no orchestrator script
yet):

1. `createkey` ×3 → 3 keypairs.
2. `createnodevss` ×3 (each signer, given all 3 pubkeys + own private key + threshold) →
   distributed node VSS.
3. `aggregate` ×3 (each signer, given the 3 node-VSS values addressed to them) → **all 3 signers
   independently produced the identical aggregated public key** (33-byte compressed, confirmed
   byte-for-byte equal across all 3).
4. tapyrus-core's `tapyrus-genesis -signblockpubkey=<aggpubkey>` (no private key) → unsigned
   genesis block hex.
5. `createblockvss` ×3, then `sign` ×3 (each signer, given the block VSS + their own
   `node-secret-share` from step 3) → 3 local signatures.
6. `computesig` (run once, by one designated signer, given **all 3** local signatures, all 3
   block-VSS, and all 3 node-VSS — see gotcha below) → the final signed genesis block hex.
7. **Loaded the resulting `genesis.dat` into a real `tapyrusd` node.** It logged `Genesis Block
   [...] Loaded successfully` and `UpdateTip: new best=... height=0` — i.e. tapyrus-core's real
   `CheckBlockHeader` Schnorr-proof verification against the ceremony's aggpubkey genuinely
   passed. This is a real, working federation genesis, not a mocked one.

**Non-obvious gotchas hit along the way, worth keeping in mind when scripting this for real:**

- `createnodevss`/`createblockvss` output lines (`<receiver_pubkey>:<vss_hex>`) are **sorted by
  receiver pubkey, not by the `--public-key` argument order** — an orchestrator must match by
  the actual pubkey, never by line position (this cost real trial-and-error to find: it fails
  with an opaque `InvalidSS` error, not a helpful message).
- `computesig`'s `--sig`/`--block-vss`/`--node-vss` arrays must **all be the same length — the
  full signer count, not just the threshold** (enforced by an `assert_eq!` in the source),
  despite `doc/setup.md` saying "t Local signatures are required."
- `sign`/`computesig` also accept `--xfield` as an alternative to `--block` — present in code,
  undocumented in `doc/setup.md` — this is the real mechanism for federation rotation (signing an
  aggpubkey change or max-block-size change instead of a genesis block).
- Toolchain: same as the old branch, rustc 1.76+ breaks on a transitive `rustc-serialize`
  dependency (via `curv`) — needs `rust-toolchain.toml` pinning `1.70.0`.
- New build issue on this branch: `gmp-mpfr-sys` (a direct dependency here) vendors and *builds
  GMP from source*, and its build script runs GMP's own test suite as part of the build — which
  segfaults on Apple Silicon. Fixed by adding `features = ["c-no-tests"]` to the `gmp-mpfr-sys`
  dependency in `Cargo.toml` to skip that self-test.
- If a build is attempted with an unpinned/newer toolchain first, it silently upgrades
  `Cargo.lock`'s format and re-resolves the `tapyrus` dependency from crates.io instead of the
  git tag the committed lock file specifies — pinning back to 1.70.0 afterward then fails to
  parse the now-newer lock file. Fix: `git checkout HEAD -- Cargo.lock` before rebuilding.

### Repo scaffolding

- `scripts/checkout-repos.sh` + `config/repos.env` — clones tapyrus-signer and tapyrus-core at
  configurable URL/ref. `config/repos.env`'s default signer ref is now
  `163_federationChangeTomlSetup`. Verified: `checkout-repos.sh` correctly lands on the
  build-fix commit when pointed at a local path/ref (see below).
- `scripts/generate-dev-secrets.sh <set-name> <node-count> <threshold> [tapyrus-setup-bin]` —
  rewritten around the real ceremony (steps 1-3 above: `createkey`/`createnodevss`/`aggregate`).
  **Verified against the real binary**: correct output layout, all N signers converge on the
  same aggpubkey (asserted in-script, not just eyeballed), two disjoint sets generated cleanly
  (needed for signer-set-a/signer-set-b), and correct error handling for a missing binary or a
  re-run against an already-populated set directory.
- `scripts/sign-genesis.sh <set-name> <unsigned-genesis-hex-file> <output-file>` — scripts steps
  4-7 above (`createblockvss`/`sign`/`computesig`) against a set `generate-dev-secrets.sh` already
  produced. **Verified against the real binary end-to-end, including loading the scripted output
  into a real `tapyrusd` node a second time** (independent of the manual run above) — same
  `Genesis Block [...] Loaded successfully` result.
- **Branch-drift investigation** (section 1): tapyrus-signer's fork has several diverged
  branches; an earlier pass used `feature/sign_with_schnorr_signature` (a 2019 Redis DKG-round
  prototype) and patched it (`expose-aggpubkey-file` branch, committed locally, not pushed) to
  export its DKG result to a file. **That entire path is superseded** by the ceremony above — the
  patch is kept committed for the record but is no longer part of the plan.
- `scripts/assemble-signer-configs.sh <set-name> <threshold> ...` — writes per-node
  `federations.toml` + `tapyrus-signer.toml`, ready to bind-mount into `tapyrus-signerd`
  containers. **Verified**: see the docker-compose run below.

### Docker Compose — verified end-to-end, including a sustained real-block-production run

`docker/docker-compose.yml` (1 tapyrusd + redis + 3 signers, signer-set-a) was brought up twice
with real scripted ceremony output (not a mock):

- **tapyrus-signer's Dockerfile builds cleanly as-is** (`rust:1.61.0` builder → `ubuntu:22.04`
  final) — both `tapyrus-signerd` and `tapyrus-setup` run correctly inside the built image. The
  earlier suspicion that its `apt-get update` with no following `apt-get install` was missing a
  system dependency (section 3c) **was a false alarm**; `rust:1.61.0`'s base image already had
  what was needed, and that image's bundled rustc (1.61.0) is old enough to sidestep the
  rustc-1.76+ `rustc-serialize` issue entirely (only relevant outside Docker).
- **tapyrus-core built from master, successfully**: the locally
  cached `tapyrus/builder:v0.7.0` tag turned out to be a *stale, locally-mistagged image*
  (`RepoDigests` showed it was actually tagged from something called `tapyrus_builder_cmake`, a
  different local image) — not the real image published to Docker Hub. Confirmed by digest: the
  stale cached tag was `sha256:09efcc3a2abc...`, but a fresh `docker pull --platform linux/arm64
  tapyrus/builder:v0.7.0` resolved to a completely different `sha256:15ac522c18bf...`, which has
  exactly the nested `/tapyrus-core/depends/aarch64-unknown-linux-gnu` layout the Dockerfile
  expects. With the correct image pulled, **built `tapyrus/tapyrusd:master-local` directly from a
  real tapyrus-core checkout at `master`** (HEAD confirmed detached exactly at the upstream
  `ct/master` tip) using CI's own invocation (`docker build --platform linux/arm64 -t
  tapyrus/tapyrusd:master-local -f Dockerfile .`) — built cleanly in ~142s, no file changes needed.
  `tapyrusd -version` reports **v0.7.2** (master is ahead of the `v0.7.1` published tag used
  elsewhere in this doc) — exactly the kind of drift this whole project exists to catch. The
  7-core-node compose stack (below) was re-verified against this `master-local` image with
  identical results to the `v0.7.1` run.
- **First run** (`round-duration = 10`, fast iteration): chain reached height 2 within a minute,
  but logs showed transient `InvalidBlock` / "candidate block is not set" errors around round
  boundaries.
- **Second run** (`round-duration = 60`, the documented default): sustained for ~32 minutes,
  chain reached height 56, **zero** `InvalidBlock` errors. Root cause confirmed in source
  (`signer_node/mod.rs:118-119`): `round_limit_timer = round-duration + round-limit` — a short
  `round-duration` leaves too little slack for a round's actual signing communication before the
  next round's messages start arriving, a race that a longer duration simply resolves. A
  different, benign warning remains regardless of duration: a non-master signer's own
  `submitblock` occasionally races a block another signer already submitted, and tapyrus-core's
  `"duplicate"` string response doesn't match what the Rust client expects
  (`invalid type: string "duplicate", expected unit`) — harmless, already-accepted block via the
  other path.
- Confirmed blocks don't need externally-submitted transactions to be valid — inspected a
  produced block directly and it contained exactly one transaction (the coinbase reward). A
  transaction-generation script (needed for the "each node broadcasts a transaction" scenario
  step) is separate, not-yet-built work.

### Docker Compose — 7-core-node topology — verified end-to-end

`docker/docker-compose.yml` has since been rewritten from the minimal 1-tapyrusd predecessor
above to the full 7-core-node topology (section 3b): `core-1a`/`core-1b`/`core-2a`/`core-2b`/
`core-3a`/`core-3b`/`core-7`, plus `redis` and the 3 `signer-set-a` containers now pointed at
their own first-layer node (`signer-0 → core-1a`, `signer-1 → core-2a`, `signer-2 → core-3a`)
instead of one shared `tapyrusd`.

**Brought up for real** (all 7 core nodes + redis + 3 signers, real ceremony output) and both
open questions from the draft resolved:

- **P2P graph matches the design exactly** — confirmed via `getpeerinfo` on every node's RPC
  port (per plan section 4 step 6), not just "containers came up": each `-a` node has exactly one
  peer (its `-b` sibling), each `-b` node has exactly two (its `-a` parent and `core-7`), and
  `core-7` has exactly three (all three `-b` nodes) — matching the intended edge set precisely.
- **Real signed blocks propagate across the full topology** — the signer network advanced the
  chain to height 1 then height 2, and all 7 nodes converged on the same tip both times, including
  `core-7`, which has no signer attached and only received those blocks via P2P relay (confirmed
  directly via `core-7`'s own `UpdateTip` log lines). No `InvalidBlock` errors; only the
  already-documented benign `"duplicate"` warning from the minimal-stack run above.

Two real, previously-undocumented bugs were found and fixed to get here:

- **tapyrus-core auto-disables listening the instant `-connect` is set** (`InitParameterInteraction:
  -connect set -> setting -listen=0`) — not mentioned by section 3c's original assumption that
  `-connect` only restricts outbound dialing. Putting `-connect` on *both* ends of every edge (the
  original draft's design, meant to symmetrically restrict each node's own outbound dialing) gave
  every node `-listen=0`, so neither end of any edge could ever accept the other's connection.
  Fixed: exactly one `-connect` per edge (the "child" dials its "parent"), with an explicit
  `-listen=1` added back on whichever node also needs to accept an inbound edge (the three
  second-layer nodes, since `core-7` dials into them).
- **`tapyrus/tapyrusd`'s entrypoint does `exec bash -c "$*"` against the image's default
  `CMD`**, and docker-compose's `command:` replaces that default CMD rather than appending to it —
  a bare `command: [-connect=core-1b]` crashed every container. Fixed by having each `command:`
  repeat the full default invocation and append the `-connect`/`-listen` flags to it.

`scripts/assemble-signer-configs.sh` was also changed to take a `<core-rpc-hosts-file>` (one RPC
host per signer) instead of one shared `<core-rpc-host>`, since each signer now targets a
different core node — **verified**: used directly in the live run above, produced correct
per-signer `rpc-endpoint-host` values.

Not yet done: the 7-node run above only confirmed 2 rounds, not the longer sustained run (32
minutes / 56 blocks) the minimal stack got — worth repeating before relying on this for the full
weekly scenario; an orchestrator tying the scripts + tapyrus-genesis + core node bring-up into one
automated flow (currently four separate manual/scripted steps run by hand in sequence);
`federations.toml`/`tapyrus-signerd` daemon behavior beyond what's been exercised so far; a
transaction-generation script; and actually running the reorg recipe designed in section 5 (not
yet built/verified).

#### Reorg precursor — signer RPC-connectivity requirement confirmed, fallback verified

Ran the standalone check the plan doc (section 4d) flagged as its one unconfirmed assumption,
against the live 7-node stack (all real: `signer-set-a`'s existing ceremony output, no mocking):

1. Brought up all 7 core nodes + redis + 3 signers, confirmed the chain at height 1 across all
   nodes (same baseline as the earlier 7-node topology run).
2. Stopped `core-1a`, `core-1b`, `core-2a`, `core-2b` (4 of 7), leaving `core-3a`/`core-3b`/
   `core-7` up. `signer-0`/`signer-1` processes kept running unmodified (RPC target now dead);
   `signer-2` still had a live RPC target (`core-3a`).
3. **Result: the chain did not advance.** Polled `core-3a`'s `getblockchaininfo` every round for 5
   full rounds (5 minutes at `round-duration = 60`) — stuck at height 1 throughout. Signer logs
   showed the master role round-robins across all three signers regardless of RPC health, and
   `signer-0`/`signer-1` fail whether or not they're master that round: as master, `RPC
   getnewblock failed... No route to host`; as non-master (even during the one round `signer-2`
   *did* successfully broadcast a valid candidate as master), processing someone else's candidate
   block *also* requires a live RPC call to their own node, so it fails the same way (`Received
   Invalid candidate block... No route to host` → `candidate block is not set` → `InvalidBlock`).
   **Confirmed conclusion**: `tapyrus-signerd` needs a live RPC connection to its own configured
   core node to participate in a round at all, even purely as a non-master over Redis — the "maybe
   Redis alone is enough" assumption in the plan doc was wrong.
4. **Fallback (also directly requested, also verified)**: re-ran `assemble-signer-configs.sh` with
   all three signers' `rpc-endpoint-host` repointed to the one surviving core node (`core-3a`),
   restarted the three `tapyrus-signerd` containers (config is bind-mounted, no rebuild needed).
   The chain resumed advancing immediately and kept going sustainably — polled over 5 more rounds
   (height 1→2→3→4→4→5, one round's normal timing noise, not a stall) and then let it run
   further unattended to height 28 before stopping the signers to end the experiment. Confirmed
   via `getpeerinfo`/`getblockchaininfo` that `core-3b` and `core-7` (no signer attached to
   either) both converged on the same height-28 tip via P2P relay — `core-7`'s own log showed
   `UpdateTip` climbing block-by-block in step with `core-3a`'s signer-driven production, exactly
   like the original 7-node verification's relay-only confirmation for `core-7`.

**Implication for the actual reorg recipe** (plan doc section 4d, steps 3-4): the second group's
phase can't just "let the signer network extend the chain" as originally worded — it must first
repoint all three signers to whichever core node in that group is still live (`core-3a`) and
restart the signer containers, exactly as verified here. The plan doc's step 3 has been updated to
say this explicitly.

### The full two-group fork-and-reconverge reorg — run for real, genuine reorg confirmed

Followed up the connectivity check above with the actual scenario: a real competing fork built by
two isolated groups from a common tip, then reconnected to watch tapyrus-core's own reorg logic
pick the winner. Not a mock — every block in both forks was a real threshold-signed block from the
live `tapyrus-signerd` network.

1. **Common baseline**: reset the whole stack (`docker compose down` — core nodes and redis have
   no volumes, so this returns every node to genesis) and brought all 7 core nodes + redis + the 3
   signers (default mapping: `signer-0→core-1a`, `signer-1→core-2a`, `signer-2→core-3a`) up fresh,
   fully connected. Let it run past the original height-20 target to **height 30** (confirmed
   identical tip, `b2d52c5a...`, on all 7 nodes) before freezing it as the split point.
2. **Split**: stopped the 3 signers, then `core-3a`/`core-3b`/`core-7`, leaving `core-1a`/`core-1b`/
   `core-2a`/`core-2b` (group A) live. Restarted the signers with the *default* mapping unchanged —
   `signer-0`/`signer-1` still had live RPC targets in this group, so (per the finding above) no
   repoint was needed for this side; `signer-2` panicked on startup with no RPC target reachable
   (`Connection refused`), which is expected and harmless here.
3. **Group A builds the losing fork**: polled `core-1a` until it reached **height 40** (10 blocks
   past the split, tip `ab21a837...`), then froze it (stopped signers, stopped all 4 group-A core
   nodes).
4. **Group B builds the winning fork**: restarted `core-3a`/`core-3b`/`core-7` — confirmed still at
   height 30, tip `b2d52c5a...`, completely unaware of group A's 10 blocks. Reset redis fresh (to
   avoid any stale round-coordination state from group A's phase carrying over) and repointed all
   three signers to `core-3a` per the confirmed fallback. Polled `core-3a`; by the time the poll's
   completion notification was processed and the signers were stopped, it had run past the intended
   height-42 target to **height 69** (39 blocks past the split, tip `50e3732a...`) — a
   polling-interval/notification-latency overshoot, not a bug in the mechanism. This doesn't affect
   the actual thing being tested: the reorg depth is defined by how many of the *losing* chain's
   blocks get discarded, which is still exactly the intended 10 (group A's blocks 31–40), just with
   a wider winning margin than planned.
5. **Reconnect**: brought `core-1a`/`core-1b`/`core-2a`/`core-2b` back up alongside the running
   group B. P2P topology re-established immediately to the expected pattern
   (`getconnectioncount`: 1/2/1/2/1/2/3 across the 7 nodes, matching the original topology
   verification), and **all 7 nodes converged on height 69 within seconds** — no manual
   intervention, no orchestrator, just tapyrus-core's own headers-first sync and reorg logic doing
   its job against two real, independently-signed chains.
6. **Confirmed via `getchaintips`, not just height**: on all four ex-group-A nodes
   (`core-1a`/`1b`/`2a`/`2b`), the response showed exactly two tips — `height: 69` /
   `status: "active"` (group B's chain) and `height: 40` / `status: "valid-fork"` /
   `branchlen: 10` (group A's own former tip, now a disconnected, still-valid-but-abandoned fork,
   branch length 10 confirming precisely the intended reorg depth). `core-3a` (which was on the
   winning side throughout) shows a single active tip, as expected — it never had a competing fork
   to abandon.

This is scenario step 8 from the plan doc (section 2) and the full section 4d recipe, genuinely
run end-to-end for the first time — not just the connectivity precursor above. `getchaintips`
showing a real `valid-fork` entry with the correct `branchlen` is the strongest evidence available
short of instrumenting the C++ reorg code directly: tapyrus-core's own chain-selection logic saw
two real competing valid chains and picked the longer one, exactly as the design intends.

## tapyrus-seeder — added back and verified working, four real bugs found and fixed

Originally deferred out of v1 scope (section 1) since container DNS already handles peer discovery
for this test. Added back for its own coverage and wired into `docker/docker-compose.yml` as a
`seeder` service, pointed at `core-1a` with this network's real dev network id (`1905960821`) and
P2P port (`12383`), rather than the image's hardcoded production defaults.

The upstream `tapyrus-seeder` source (2019-era, last touched well before this project) does not
work as-is — four real, distinct bugs were found and fixed, each confirmed against a live core
node, on a local `docker-build-fix` branch:

1. **Build failure**: Alpine 3.7's g++ rejects a designated-initializer used for `struct msghdr` in
   `dns.cpp` as "non-trivial" — confirmed not architecture-specific (fails identically on
   `linux/arm64` and `linux/amd64`). Fixed with plain field assignment after `memset`, instead of
   the designated initializer (identical semantics, no compiler-version dependency).
2. **Runtime segfault on the very first real connection**: `char filename[25]` in `main.cpp` is
   too small for its own `sprintf` format strings — `TAPYRUS_STAT_FILE`
   (`"tapyrusdnsstats_%d.log"`) alone needs up to 31 bytes for a 10-digit network id, overflowing
   even for the image's own default hardcoded production network id (`1939510133`, also 10
   digits), not just this project's. Alpine/musl's fortified `sprintf` catches this at runtime
   (`SIGTRAP`) rather than silently corrupting the stack — confirmed via `gdb` against a real core
   dump, reproduced reliably. Fixed by bumping both `filename` buffers (`main.cpp` ~418, ~658) to
   64 bytes.
3. **Data race in the DNS server threads**: fixing bugs 1–2 made the seeder work *most* of the
   time, but repeated `docker compose` restarts still crashed intermittently (roughly 1 in 3
   runs) — a different class of bug (non-deterministic) needing a different tool. Since
   Alpine/musl has weak ThreadSanitizer support, the diagnostic build used a separate glibc-based
   image (`ubuntu:22.04`, same source, build-only, not the runtime image) with
   `-fsanitize=thread`. This reliably caught a real race on every run: `dns.cpp`'s global
   `listenSocket` was checked-and-created with no synchronization across the 4 concurrent DNS
   threads (`nDnsThreads`, default), so multiple threads could simultaneously see it unset and
   each independently create/overwrite it. Fixed with a mutex around the check-and-create block.
4. **Data race in the crawler-thread spawn loop**: the same TSan run also caught `main.cpp`'s
   `ThreadCrawler_options` being stack-allocated *inside* the loop that spawns the 96 crawler
   threads, then handed to `pthread_create` by address — the main thread's next loop iteration
   could (and did) overwrite that same stack slot before the newly spawned thread reliably read
   it. Fixed by heap-allocating it instead (never freed, matching this process's existing
   long-running-daemon style already used for its DNS-thread options).

**Verified end-to-end after all four fixes**: 8 consecutive `docker compose` restarts of the
`seeder` service all stayed up (compare: crashed on the very first run before fixes 1–2, and ~1 in
3 restarts even after those before fixes 3–4); 8 repeated TSan runs showed 0 races (compare: every
run showed the same 2 races before the fix); and `dig` against the running container returned a
real, live-discovered peer address from this project's own network once the crawl had run long
enough to vet one — confirming the DNS-serving side, not just the crawler, works correctly.

# tapyrus-integration-tests

Weekly cross-repo integration test for tapyrus-signer + tapyrus-core + tapyrus-seeder
(see `doc/weekly-integration-test-plan.md`).

Status: **the minimal 1-core-node stack, the full 7-core-node topology (plan doc section
3b), tapyrus-seeder, and a genuine two-group reorg (scenario step 8) are all verified
end-to-end via Docker Compose, local-only.** Nothing in this repo has been pushed
anywhere. A real 3-signer federation (setup ceremony + genesis-signing + ongoing live
block production) has been run end-to-end in containers, first on the minimal stack (see
"Docker Compose — verified end-to-end" below) and since on the full 7-node topology (see
"Docker Compose — 7-node topology — verified end-to-end" below), where the resulting P2P
graph and cross-network block propagation were both confirmed live. tapyrus-seeder (see
"tapyrus-seeder — added back and verified working" below) needed four real bug fixes (a
build failure, a buffer overflow, and two data races) before it worked reliably. Most
recently, the network was split into two isolated groups, each built its own real
threshold-signed fork from a common tip, and reconnecting all 7 nodes produced a genuine,
tapyrus-core-adjudicated 10-block reorg — see "Known gaps" below for the full writeup and
`doc/work-done.md` for the run. Still missing: an orchestrator and CI wiring — see "Known
gaps".

## What's here so far

- `config/repos.env` — configurable checkout targets (URL + ref) for
  tapyrus-signer and tapyrus-core. tapyrus-signer defaults to
  `163_federationChangeTomlSetup` (see "Which tapyrus-signer branch" below).
- `scripts/checkout-repos.sh` — clones both into `./workdir/` (gitignored).
- `scripts/generate-dev-secrets.sh <set-name> <node-count> <threshold>` — runs the
  real `tapyrus-setup` federation-setup ceremony (`createkey` → `createnodevss` →
  `aggregate`) for a throwaway signer set, under `./secrets/` (gitignored, dev-only
  — see the secrets-scope decision below). **Verified against the real binary.**
- `scripts/sign-genesis.sh <set-name> <unsigned-genesis-hex-file> <output-file>` —
  runs the genesis-signing half of the ceremony (`createblockvss` → `sign` →
  `computesig`) against a signer set `generate-dev-secrets.sh` already produced.
  **Verified against the real binary, including loading the result into a real
  `tapyrusd` node.**
- `scripts/assemble-signer-configs.sh <set-name> <threshold> ...` — writes per-node
  `federations.toml` + `tapyrus-signer.toml`, ready to bind-mount into
  `tapyrus-signerd` containers. **Verified** against both the minimal 1-core-node
  stack and the full 7-core-node topology (see below). Now takes a
  `<core-rpc-hosts-file>` (one RPC host per signer, since each signer targets a
  different core node in the 7-node topology) instead of one shared host.
- `docker/docker-compose.yml` — the full 7-core-node topology (see "Docker Compose —
  7-node topology — verified end-to-end" below): all 7 nodes up, P2P graph confirmed
  to match the design exactly, and the signer network's real signed blocks confirmed
  propagating to every node. The 1 tapyrusd + redis + 3 signers shape it replaces
  **was also verified fully end-to-end, including a sustained run**: brought up with
  real scripted ceremony output, the signer network produced actual live-signed
  blocks past genesis for ~32 minutes straight (56 blocks, 19+ successful rounds,
  zero protocol errors) — not mocked, not just genesis. See "Docker Compose —
  verified end-to-end" below for that history.

## Docker Compose — verified end-to-end

`docker/docker-compose.yml` runs 1 `tapyrusd` + `redis` + 3 `tapyrus-signerd`
containers (signer-set-a). Verified by hand, twice:

1. Built `tapyrus-signer:federation-setup-review` locally from the checkout (the
   Dockerfile builds cleanly — see "Dockerfile notes" below, the earlier "missing
   apt-get install" concern turned out to be a false alarm, both binaries run fine).
2. Used the published `tapyrus/tapyrusd:v0.7.1` image for the core side (not
   `v0.5.1`, which is old) rather than building tapyrus-core's own Dockerfile locally
   — see "About tapyrus-core's Dockerfile" below for why.
3. Ran the real ceremony (`generate-dev-secrets.sh` → `tapyrus-genesis` →
   `sign-genesis.sh` → `assemble-signer-configs.sh`), fed the result into the compose
   stack, and confirmed via `tapyrusd`'s own `getblockchaininfo` RPC that the chain
   advanced past genesis under real, live signer-produced blocks.
4. **First run** (`round-duration = 10`, for fast iteration): chain reached height 2
   in under a minute, but logs showed transient `InvalidBlock` / "candidate block is
   not set" errors around round boundaries — a timing race where a new round's
   Redis messages arrive before the local node has started its own round.
5. **Second run** (`round-duration = 60`, the documented default): over a ~32 minute
   sustained run, chain reached height 56 with **zero** `InvalidBlock` errors — the
   extra slack in `round_limit_timer` (`round-duration + round-limit`,
   `signer_node/mod.rs:118-119`) eliminates the race. A different, benign warning
   remains at any duration: a non-master signer's own `submitblock` call sometimes
   races a block already submitted via another signer, and tapyrus-core's `"duplicate"`
   string response doesn't match what the Rust client's JSON deserializer expects
   (`invalid type: string "duplicate", expected unit`) — harmless, the block was
   already accepted through the other path.
6. Blocks don't need any externally-submitted transactions to be valid — each round's
   candidate block contains just the coinbase (reward) transaction if the mempool is
   empty. Confirmed by inspecting a produced block directly (`"nTx":1`). A
   transaction-generation script is a separate, not-yet-built piece (see Known gaps),
   needed for the "each node broadcasts a transaction" scenario step, not for the
   chain to keep advancing.

**Recommendation**: use `round-duration = 60` (or higher) for anything beyond quick
manual iteration — `ROUND_DURATION` env var on `assemble-signer-configs.sh` controls
this.

### About tapyrus-core's Dockerfile

Originally did not build tapyrus-core's own root `Dockerfile` locally — it failed on
this machine expecting `depends/aarch64-unknown-linux-gnu` inside the
`tapyrus/builder:v0.7.0` base image, but that image's `/tapyrus-core` layout had
`aarch64-unknown-linux-gnu` present without a nested `depends/` subfolder. At the time
this was written off as environment-specific (confirmed with the team it builds fine in
real CI) and left alone.

**Root cause since found and fixed**: the locally-cached `tapyrus/builder:v0.7.0` tag
was a *stale, locally-mistagged image* (its `RepoDigests` showed it was actually tagged
from something called `tapyrus_builder_cmake`, a different local image), not the real
image published to Docker Hub. Confirmed by diffing digests: the cached tag was
`sha256:09efcc3a2abc...`, but a fresh `docker pull --platform linux/arm64
tapyrus/builder:v0.7.0` resolved to `sha256:15ac522c18bf...` — a completely different
image. The real image has exactly the nested `/tapyrus-core/depends/aarch64-unknown-linux-gnu`
layout the child Dockerfile expects. Once the correct image was pulled, **tapyrus-core's
Dockerfile built cleanly**, no changes needed to the file at all — this really was just a
bad local tag, not a bug in the Dockerfile or CI.

### Building tapyrusd from tapyrus-core master — verified

Built `tapyrus/tapyrusd:master-local` directly from a real tapyrus-core checkout at
`master` (HEAD confirmed detached exactly at the upstream `ct/master` tip), using the
same invocation `push_docker_image.yml` uses in CI:

```sh
docker build --platform linux/arm64 -t tapyrus/tapyrusd:master-local -f Dockerfile .
```

Built cleanly (~142s for the cmake build stage), installing `tapyrusd`, `tapyrus-cli`,
`tapyrus-tx`, and `tapyrus-genesis`. `tapyrusd -version` reports **v0.7.2** — i.e. master
is ahead of the `v0.7.1` published tag used elsewhere in this README, which is exactly
the kind of drift this whole project exists to catch.

`docker/docker-compose.yml`'s 7 core-node services now use this `master-local` image
(not the published `v0.7.1` tag) — **re-verified the full topology and ceremony against
it** with identical results to the earlier `v0.7.1` run: `getconnectioncount`/
`getpeerinfo` confirmed the same peer counts (1/1/1/2/2/2/3) on every node, and the
signer network advanced the chain to height 1 across all 7 nodes again. The unsigned
genesis produced by master's own `tapyrus-genesis` for the same aggpubkey was
byte-identical to the `v0.7.1` version except for the block timestamp field (expected --
`tapyrus-genesis` stamps the current time on each invocation).

### Docker Compose — 7-node topology — verified end-to-end

`docker/docker-compose.yml` was rewritten from the minimal 1-tapyrusd stack above to the
full 7-core-node topology (plan doc section 3b): `core-1a`/`core-1b`/`core-2a`/`core-2b`/
`core-3a`/`core-3b`/`core-7`, `redis`, and the 3 `signer-set-a` containers now each
pointed at their own first-layer node (`signer-0 → core-1a`, `signer-1 → core-2a`,
`signer-2 → core-3a`) instead of one shared `tapyrusd`.

**Brought up for real** (all 7 core nodes + redis + 3 signers, real ceremony output —
not a mock) and verified two distinct things:

1. **The P2P graph matches the intended topology exactly** — confirmed via
   `getpeerinfo` on every node's RPC port, not just "containers came up": each `-a`
   node has exactly its `-b` sibling as a peer (connection count 1), each `-b` node has
   its `-a` parent plus `core-7` (connection count 2), and `core-7` has exactly the
   three `-b` nodes (connection count 3) — matching the designed edge set precisely.
2. **The signer network produces real signed blocks that propagate across the full
   topology** — signer-set-a advanced the chain to height 1, then height 2, and *all
   seven* core nodes converged on the same tip each time, including `core-7`, which has
   no signer attached and only ever received those blocks via P2P relay through the
   second-layer nodes (confirmed directly in `core-7`'s own log: `UpdateTip: new
   best=... height=1` then `height=2`, with no `InvalidBlock` errors — only the
   already-documented benign `"duplicate"` warning from the minimal-stack verification
   above).

Two real bugs were found and fixed while getting here (both are explained in detail as
comments directly in `docker/docker-compose.yml` — read them before changing the
`command:` lines):

- **tapyrus-core auto-disables listening the instant `-connect` is set at all**
  (`InitParameterInteraction: -connect set -> setting -listen=0`) — undocumented by the
  plan doc, which assumed `-connect` only restricted outbound dialing. A first attempt
  put `-connect` on *both* ends of every edge (to symmetrically restrict each node's own
  outbound dialing, per the plan doc's original wording) — that gave every node
  `-listen=0`, so neither end of any edge could ever accept the other's connection, and
  every node spun forever on `connect() ... Connection refused`. Fixed by using exactly
  one `-connect` per edge (the "child" dials its "parent") and adding an explicit
  `-listen=1` back wherever a node also needs to accept an *inbound* edge (the three
  second-layer nodes, since `core-7` dials into them).
- **`tapyrus/tapyrusd`'s entrypoint does `exec bash -c "$*"` against the image's
  default `CMD`**, and docker-compose's `command:` *replaces* that default CMD rather
  than appending to it — a bare `command: [-connect=core-1b]` crashed every container
  (`bash -c "-connect=core-1b"` → invalid option). Fixed by having each `command:`
  repeat the full default invocation (`tapyrusd -datadir=$${DATA_DIR}
  -conf=$${CONF_DIR}/tapyrus.conf`) and appending the `-connect`/`-listen` flags to it.

Topology notes (unchanged from the design, now confirmed live):

- `core-7` connects to **all three** second-layer nodes (`core-1b`, `core-2b`,
  `core-3b`) — the plan doc's earlier wording ("any two ... e.g.") would leave
  `core-3a`/`core-3b` an isolated pair with no path to the rest of the network;
  confirmed with the team that full connectivity is needed instead (see plan doc
  section 3b).
- All 7 nodes stay on one flat compose network (not Docker-network-segmented per edge),
  matching the plan doc's rationale: a future adversary node needs real network-level
  reachability to first-layer nodes, which segmentation would foreclose.

`scripts/assemble-signer-configs.sh` changed accordingly: its `<core-rpc-host>` argument
is now `<core-rpc-hosts-file>` (one RPC host per signer, since each signer targets a
different core node) — see that script's header comment. **Verified**: used directly in
the live run above, produced correct per-signer `rpc-endpoint-host` values.

## tapyrus-seeder — added back and verified working

Originally deferred out of v1 scope (container DNS already handles peer discovery for
this test) — added back for its own coverage as a `seeder` service in
`docker/docker-compose.yml`, pointed at `core-1a` with this project's real dev network
id (`1905960821`) and P2P port (`12383`).

The upstream `tapyrus-seeder` source doesn't work as-is: **four real bugs found and
fixed** on a local `docker-build-fix` branch, each confirmed live:

1. **Build failure** — Alpine 3.7's g++ rejects a designated-initializer for `struct
   msghdr` in `dns.cpp` as "non-trivial" (not architecture-specific). Fixed with plain
   field assignment instead.
2. **Runtime segfault on first connection** — `char filename[25]` in `main.cpp` is too
   small for its own `sprintf` format strings (up to 31 bytes needed), overflowing even
   for the image's own default production network id. Caught by Alpine/musl's fortified
   `sprintf`, confirmed via `gdb` + a core dump. Fixed by bumping both buffers to 64
   bytes.
3. **Data race in the DNS threads** — found via a ThreadSanitizer build (on a separate
   `ubuntu:22.04` image, same source — musl/Alpine has weak TSan support): the global
   `listenSocket` was checked-and-created with no synchronization across 4 concurrent DNS
   threads. Fixed with a mutex.
4. **Data race in the crawler-thread spawn loop** — `ThreadCrawler_options` was
   stack-allocated inside the spawning loop and passed by address into `pthread_create`,
   reused by the next iteration before the new thread reliably read it. Fixed by
   heap-allocating it instead.

**Verified end-to-end**: 8/8 consecutive `docker compose` restarts stayed up (vs.
crashing on the very first run before fixes 1–2, and ~1 in 3 restarts even after those);
0 races across 8 TSan runs (vs. every run before); and `dig` against the running
container returned a real, live-discovered peer address once the crawl had run long
enough to vet one. See `doc/weekly-integration-test-plan.md`'s section 8 for the full
writeup.

## Try it

```sh
SIGNER_REPO_URL=/path/to/local/tapyrus-signer SIGNER_REPO_REF=163_federationChangeTomlSetup \
CORE_REPO_URL=/path/to/local/tapyrus-core CORE_REPO_REF=master \
  ./scripts/checkout-repos.sh
(cd workdir/tapyrus-signer && cargo build --release)   # needs the toolchain/gmp fixes below

./scripts/generate-dev-secrets.sh signer-set-a 3 2

# tapyrus-core's own tool builds the unsigned genesis candidate (no private key --
# nobody holds one for a threshold-signed federation):
tapyrus-genesis -dev -signblockpubkey=$(cat secrets/signer-set-a/aggregated-public-key.txt) \
  > /tmp/unsigned-genesis.hex

TAPYRUS_SETUP_THRESHOLD=2 ./scripts/sign-genesis.sh signer-set-a /tmp/unsigned-genesis.hex \
  secrets/signer-set-a/genesis.hex
# copy secrets/signer-set-a/genesis.hex to <tapyrus-core-datadir>/genesis.dat to use it.

# bring up just the 7 core nodes + redis first, to get real dev-network addresses for
# coinbase payout from each signer's own first-layer node:
cd docker
GENESIS_BLOCK_WITH_SIG=$(cat ../secrets/signer-set-a/genesis.hex) \
  docker compose up -d redis core-1a core-1b core-2a core-2b core-3a core-3b core-7
cd ..

for port in 12381 12383 12385; do curl -s --user rpcuser:rpcpassword --data-binary \
  '{"jsonrpc":"1.0","id":"t","method":"getnewaddress","params":[]}' \
  -H 'content-type: text/plain;' http://127.0.0.1:$port/; echo; done > /tmp/addrs_raw.txt
# (extract just the address strings into /tmp/addrs.txt, one per line)

# each signer now targets its own first-layer core node (not one shared host):
printf 'core-1a\ncore-2a\ncore-3a\n' > /tmp/core-rpc-hosts.txt

ROUND_DURATION=60 ./scripts/assemble-signer-configs.sh signer-set-a 2 /tmp/core-rpc-hosts.txt \
  12381 rpcuser rpcpassword redis 6379 /tmp/addrs.txt

cd docker && GENESIS_BLOCK_WITH_SIG=$(cat ../secrets/signer-set-a/genesis.hex) \
  docker compose up -d signer-0 signer-1 signer-2
```

Federation shape is 3 tapyrus-signer nodes (threshold 2) + 7 tapyrus-core nodes — see
`doc/weekly-integration-test-plan.md` for the full scenario.

## Which tapyrus-signer branch

**Updated finding**: chaintope's own official `tapyrus-signer` repo has the entire
federation-setup ceremony on its **own `master` branch** — no fork needed at all. Diffing
trees against the fork branch below (`163_federationChangeTomlSetup`) showed only 4
files / 11 lines differ, with near-identical commit messages — the same work, landed on
chaintope's master via a rebased series, plus extra fixes (a `rustc-serialize` bump, CI
updates) the fork lacks. Its Dockerfile (identical, `rust:1.61.0` builder) builds and runs
successfully. The local `tapyrus-signer` checkout's `master` branch has been
fast-forwarded to match `chaintope/master` (was a strict ancestor, zero divergence, so a
clean fast-forward). Still to do: point `config/repos.env` at `chaintope/tapyrus-signer.git`'s
`master` instead of the fork below, and re-apply the toolchain/`gmp-mpfr-sys` fixes (see
"Dockerfile notes") on the new base — the compose stack still runs against
`federation-setup-review` (built on the fork) for now. See
`doc/weekly-integration-test-plan.md` section 1/5 for the full writeup.

**Original investigation** (kept for history): the fork (`Naviabheeman/tapyrus-signer`)
has several diverged branches (not a linear history). `master` (the fork's own, before the
sync above) was a 2019 prototype with no aggpubkey concept at all — incompatible with
current tapyrus-core. An earlier pass at this used `feature/sign_with_schnorr_signature`
(also 2019, a Redis DKG-round prototype) with a patch (`expose-aggpubkey-file`, still
committed on its own branch, unused) to surface its result. Both were superseded by
`163_federationChangeTomlSetup` — a properly versioned (`0.4.0`) branch with a real
`tapyrus-setup` CLI for the whole federation-setup + genesis-signing ceremony, and a
`tapyrus-signerd` daemon reading `federations.toml` — which, per the finding above, turned
out to just be an unmerged-looking copy of what chaintope had already merged into their
own master. Sibling branches (`163_federationChangeTomlSetup48`, `BlockHeightu32`,
`FixCINode12Warning`) carry small incremental fixes not present here — now moot given the
finding above.

## The real ceremony — verified end-to-end

Ran manually against the real `tapyrus-setup` binary (not simulated): 3 signers,
threshold 2, each ran `createkey` → `createnodevss` → `aggregate` and **independently
converged on the identical 33-byte aggpubkey**. Then built an unsigned genesis via
tapyrus-core's `tapyrus-genesis -signblockpubkey=<aggpubkey>`, signed it via
`createblockvss` → `sign` → `computesig`, and **loaded the result into a real
`tapyrusd` node — it logged `Genesis Block [...] Loaded successfully`**, meaning
tapyrus-core's actual Schnorr proof verification against the ceremony's aggpubkey
passed for real.

Both halves are now scripted (`generate-dev-secrets.sh` for aggpubkey generation,
`sign-genesis.sh` for genesis-signing) and tested against the real binary end-to-end
— including re-running the whole flow through the scripts and loading the scripted
output into a real `tapyrusd` node a second time, with the same result.

### Gotchas found the hard way (all baked into the script; noted here so they don't get rediscovered)

- `createnodevss`/`createblockvss` output lines (`<receiver_pubkey>:<vss_hex>`) are
  **sorted by receiver pubkey, not by `--public-key` argument order** — must match by
  actual pubkey, never by line position. Getting this wrong fails with an opaque
  `InvalidSS` error.
- `computesig`'s `--sig`/`--block-vss`/`--node-vss` arrays must **all be the same
  length — the full signer count, not just the threshold** (enforced by an
  `assert_eq!` in the source), despite the upstream doc (`doc/setup.md` in
  tapyrus-signer) saying "t Local signatures are required."
- `sign`/`computesig` also accept `--xfield` as an alternative to `--block`, signing
  an aggpubkey rotation or max-block-size change instead of a genesis block — real,
  present in code, **undocumented upstream**. This is the actual mechanism for
  federation rotation (see the plan doc's reorg-mechanic section).
- `tapyrus-setup createkey` always produces mainnet-prefixed WIFs (`K`/`L`, `0x80`),
  unlike tapyrus-core's own `-dev` network convention (`c` prefix, `0xef`). Didn't
  cause problems in the verification above, but not stress-tested beyond that.

## Design decisions made so far

- **Secrets scope**: this repo only generates *local dev* secrets via
  `generate-dev-secrets.sh`. It does not create or upload anything into a
  real GitHub repo's Settings > Secrets — that needs repo-admin access this
  tooling doesn't have. The `.github/workflows/` job (not yet written) will
  *reference* `${{ secrets.* }}` and expect them to be populated by hand in
  GitHub's UI once this repo has a real home.
- **Reorg mechanic**: forced via federation rotation, not network partition — a
  *second*, disjoint signer set (`generate-dev-secrets.sh signer-set-b ...`) runs its
  own ceremony, and the *current* federation signs off on the handoff via the
  `--xfield` sign/computesig flow above. Not yet scripted — see Known gaps.
- **Core network topology**: not full-mesh. 3 "first-layer" core nodes (each the RPC
  target for one signer), 3 "second-layer" nodes (each P2P-connected only to its
  first-layer parent), and a 7th node P2P-connected to two of the second-layer nodes.
  Enforced via tapyrus-core's `-connect=` flag, on a single flat Docker network
  (deliberately not network-segmented, so a planned future adversary node — connecting
  P2P to two first-layer nodes — remains buildable without rework). See the plan doc's
  section 3b for the full design and rationale.

## Dockerfile notes

tapyrus-signer's `Dockerfile` (builder: `rust:1.61.0`, final: `ubuntu:22.04`) builds
cleanly as-is and produces working `tapyrus-signerd`/`tapyrus-setup` binaries —
confirmed by actually building and running both inside the container. The earlier
concern that its `RUN apt-get update` with no following `apt-get install` might be
missing a system dependency (e.g. for GMP) **was a false alarm** — `rust:1.61.0`'s
base image already has what's needed, and `rust:1.61.0`'s bundled rustc (1.61.0) is
old enough that the `rustc-serialize`/rustc-1.76+ issue below never triggers inside
Docker either (only relevant for local/non-Docker builds using a newer system
toolchain). The fixes below were needed for building outside Docker (this repo's
scripts default to that, via a locally-built binary path):

- **Toolchain**: this codebase depends on `curv` (git-pinned, 2019-era), which pulls
  in `rustc-serialize` transitively — that fails to compile on rustc 1.76+ (a known
  lifetime-checking break, <https://github.com/rust-lang/rust/issues/134362>). Fixed by
  `rust-toolchain.toml` pinning `channel = "1.70.0"` — rustup picks this up
  automatically for any `cargo`/`rustc` invocation in that directory.
- **GMP self-test segfault**: `gmp-mpfr-sys` (a direct dependency) vendors and builds
  GMP from source, and its build script runs GMP's own test suite as part of the
  build — which segfaults on Apple Silicon. Fixed by adding
  `features = ["c-no-tests"]` to the `gmp-mpfr-sys` dependency in `Cargo.toml`, which
  skips that self-test (we only need GMP to build and link, not pass its own tests).
- **Cargo.lock drift**: building with an unpinned/newer toolchain first will silently
  upgrade the lock file format and re-resolve the `tapyrus` dependency from crates.io
  instead of the git tag the committed lock specifies; pinning back to 1.70.0
  afterward then fails to parse the newer lock file. Fix: `git checkout HEAD --
  Cargo.lock` before rebuilding with the pinned toolchain, if this happens.
- On macOS, also needs `brew install gmp` + `LIBRARY_PATH=/opt/homebrew/lib` for the
  linker to find the (unrelated, separately-needed) system `libgmp` some other
  transitive dependency wants when building outside Docker.

## Known gaps / not yet done

- The 7-node topology run above confirmed 2 rounds (chain height 2) with correct
  topology and propagation to all 7 nodes — it hasn't yet had the same longer sustained
  run (32 minutes / 56 blocks) that the minimal 1-core-node stack got. Worth repeating
  for a longer duration before relying on it for the full weekly scenario.
- No orchestrator (the thing that actually drives the full scenario end-to-end —
  today's verification is 4 manual/scripted steps run by hand in sequence).
- No transaction-generation script (blocks currently only ever contain the coinbase
  transaction — see "Docker Compose" above).
- No `.github/workflows/` yet.
- **Reorg (scenario step 8, plan doc section 4d) — now verified for real**: split the 7-node
  network into two isolated groups from a common tip (height 30), let each build its own
  real threshold-signed fork (group A: 10 blocks to height 40; group B: repointed to its one
  live core node per the confirmed connectivity finding below, 39 blocks to height 69), then
  reconnected all 7 nodes. They converged within seconds on group B's chain, with `getchaintips`
  on every ex-group-A node confirming the old height-40 tip as a `valid-fork` with
  `branchlen: 10` — a genuine, tapyrus-core-adjudicated reorg, not simulated. See
  `doc/work-done.md` ("The full two-group fork-and-reconverge reorg") for the full run, and the
  plan doc section 4d for the recipe (now updated with the connectivity requirement below).
- **Confirmed finding on signer RPC connectivity**: `tapyrus-signerd` needs a *live* RPC
  connection to its own configured core node to participate in a signing round at all — even
  purely as a non-master over Redis. A signer whose core-node RPC target is down cannot
  contribute its threshold share, full stop; there is no Redis-only fallback. This resolves the
  plan doc section 4d's previously-open assumption (it was wrong) and is why the reorg's
  second group above needed all three signers repointed at its one surviving core node. See
  `doc/work-done.md` for the standalone check that found this.
- Federation rotation (`signer-set-b` taking over via the `--xfield` sign/computesig
  flow) is designed but not yet scripted or verified.
