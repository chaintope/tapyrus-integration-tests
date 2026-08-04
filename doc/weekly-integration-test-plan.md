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
  `core-7 ↔ core-2b`, `core-7 ↔ core-3b`) — full connectivity is required since the
  sync/lifecycle/reorg scenario steps depend on all 7 nodes being able to reach each other; any
  fewer would leave a pair of nodes isolated with no path to the rest.
- **(Future, not v1)**: an adversary core node connecting P2P to **two first-layer nodes**
  (`core-1a`, `core-2a`) — i.e. attaching itself closer to the signer-facing nodes than the
  legitimate topology would allow, to explore what that lets it observe/influence.
- **Enforcement is one `-connect` per edge, not both ends** — tapyrus-core auto-disables listening
  the instant `-connect` is set at all (`InitParameterInteraction: -connect set -> setting
  -listen=0`), so if both ends of an edge set it, *neither* can ever accept the other's inbound
  connection. The design instead has exactly one side of each edge dial out via `-connect` (the
  "child" dials its "parent"), with an explicit `-listen=1` added back wherever a node also needs
  to accept an inbound edge — see `docker/docker-compose.yml`'s own comments for the full
  per-node breakdown.

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
