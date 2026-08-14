# Project Plan: Weekly Cross-Repo Integration Test

Tracks progress toward a fully automated CI run of the scenario in
[`weekly-integration-test-plan.md`](weekly-integration-test-plan.md). Update this file
as work lands -- it's the single place to check "what's actually done" vs. "what's
designed but not built yet."

Legend: `[x]` done and verified -- `[ ]` not started.

**The entire scenario is confirmed end-to-end in real GitHub Actions CI**, not just
locally: a `pull_request`-triggered run (smoke scale) went through ceremony, 7-node
bring-up, traffic generation, reorg, federation change, max-block-size change, and
traffic generation again afterward, all successfully --
[run](https://github.com/chaintope/tapyrus-integration-tests/actions/runs/30790964795/job/91614204236).
This also confirms the `pull_request`/`push` smoke trigger's `inputs.<name> || (...)`
fallback expressions evaluate correctly on a real event, not just in locally-simulated
env vars. Individual items below no longer restate this; only gaps genuinely untouched
by that run keep their own caveats.

## Milestone 1 -- Repo scaffolding

- [x] `config/repos.py` + `scripts/checkout_repos.py` (all 3 repos: tapyrus-core, tapyrus-signer, tapyrus-seeder -- each ref independently configurable via CI variable)
- [x] `scripts/generate_dev_secrets.py` (aggpubkey ceremony)
- [x] `scripts/sign_genesis.py` (genesis-signing ceremony)
- [x] `scripts/assemble_signer_configs.py` (per-node `federations.toml` + `tapyrus-signer.toml`)
- [x] All four scripts use Python (class-based: `RepoCheckout`, `AggpubkeyCeremony`, `GenesisSigningCeremony`, `SignerConfigAssembler`), plus a uniform leveled/timestamped logger (`scripts/lib/log.py`) shared across all of them
- [x] `docker/docker-compose.yml` (7-core-node + redis + 3-signer + seeder topology)
- [x] `.github/workflows/weekly-integration-test.yml` skeleton (steps present, most bodies are TODO placeholders)
- [x] Repo pushed to its real GitHub home (`chaintope/tapyrus-integration-tests`)

## Milestone 2 -- Core mechanics verified by hand (local spike)

- [x] Real `tapyrus-setup` ceremony (createkey -> createnodevss -> aggregate -> genesis sign) verified end-to-end against a real `tapyrusd` node
- [x] Minimal 1-core-node + redis + 3-signer stack: sustained real block production (~32 min, 56 blocks, zero `InvalidBlock` errors at `round-duration=60`)
- [x] Full 7-core-node topology: P2P graph confirmed to match design exactly (`getpeerinfo`), real signed blocks propagate to all 7 nodes including the signer-less `core-7`
- [x] `tapyrus-seeder` integrated as a compose service, verified via 8/8 clean restarts + 0 TSan races + live `dig` resolution
- [x] Signer RPC-connectivity requirement confirmed: `tapyrus-signerd` needs a live RPC connection to its own core node even as a non-master over Redis (no Redis-only fallback)
- [x] Reorg (scenario step 8): full two-group fork-and-reconverge run for real, confirmed via `getchaintips` (`valid-fork`, correct `branchlen`)

See [`work-done.md`](work-done.md) for known issues and design decisions.

## Milestone 3 -- Scenario mechanics

- [x] Per-node transaction generation -- `scripts/generate_traffic.py` (`TrafficNode` + `TrafficGenerator`). Round-robin TPC + colored-coin (REISSUABLE/NON_REISSUABLE/NFT mix, one type per node) sends across all 7 nodes, everything derived from a single round count (`tx_round_count`); balance verified against a tracked ledger after every block, including the 3 coinbase-earning nodes' TPC (observed directly per block, not excluded -- see `work-done.md`). Wired into the workflow (`Generate traffic (before reorg)` step). Verified end-to-end: zero send/issuance failures and zero balance mismatches across 3 full rounds, real balance changes throughout (including coinbase), settled correctly. Also guards against a fully-broken run trivially passing: `MIN_SUCCESSFUL_ACTION_FRACTION` asserts at least half the expected send/mint actions actually succeeded, not just that tracked-vs-actual balances agree (a never-funded node's ledger and real balance would otherwise both sit at `0.0` and look "consistent").
- [x] Max-block-size (xfield) change -- `scripts/simulate_maxblocksize_change.py`
  (`MaxBlockSizeChangeSimulator`). Signer-set-b (already active after the rotation
  above) signs off on a new max-block-size via a fresh `--xfield sign/computesig`
  round, reusing `simulate_federation_change.py`'s `XFieldSignoffCeremony` directly
  (generic over which xfield gets signed). `federations.toml` gains a new member
  entry for signer-set-b's 3 currently-active nodes (node-0, `signer-b-1`,
  `signer-b-2`) -- no new signer identities or containers needed, unlike the rotation
  itself. Confirmed via RPC, not logs (`_confirm_change_via_rpc`:
  `getblockchaininfo`'s `maxBlockSizes` array on all 7 core nodes must show the new
  value at the scheduled height). Verified live and active in the workflow (see
  `work-done.md`'s note on a `tapyrus-setup` offline xfield-signing bug hit and fixed
  upstream along the way).
- [x] Aggpubkey rotation handoff -- `scripts/simulate_federation_change.py`
  (`FederationChangeSimulator`). Signer-set-b runs its own offline ceremony, but with
  one member's identity deliberately REUSED from signer-set-a's node-0
  (`PartialReuseAggpubkeyCeremony`, subclasses `generate_dev_secrets.py`'s
  `AggpubkeyCeremony`) rather than 3 entirely fresh keys -- that shared signer ends up
  a full member of both federation entries, so it keeps contributing to signer-set-a's
  threshold right up to the handoff height and continues under signer-set-b
  immediately after, with no gap where nobody currently running has a valid entry in
  the about-to-be-active federation. Only `signer-b-1`/`signer-b-2` (node-0's two
  signer-set-b co-members) are genuinely new identities/containers. Signer-set-a signs
  off via a fresh `--xfield` `sign`/`computesig` round (`XFieldSignoffCeremony` -- not
  the genesis-signing block-vss reused, a fresh one, to avoid nonce reuse);
  `federations.toml` regenerated for all 5 involved nodes -- node-0 gets a full member
  copy of the new entry, signer-1/signer-2 get a non-member copy (they're leaving the
  federation), signer-b-1/signer-b-2 get a non-member copy of the original entry plus
  their own full one (`RotationSignerConfigAssembler`, subclasses
  `SignerConfigAssembler` rather than editing it) -- correctly omitting
  `threshold`/`node-vss` for federations a given node isn't a member of (confirmed
  from `tapyrus-signer` source this is required, not just tidiness). **signer-0/1/2
  are never stopped or restarted** -- their `federations.toml` is live-reloaded in
  place instead (write-to-temp-then-rename, `_write_atomic`; `src/federation_watcher.rs`,
  read directly). Confirmed via RPC, not container/signer logs
  (`_confirm_rotation_via_rpc`: `getblockchaininfo`'s `aggregatePubkeys` array on all
  7 core nodes shows the new pubkey at the scheduled height). Built by reading the
  real `tapyrus-signer`/`rust-tapyrus` v0.4.8 source directly (see the script's own
  module docstring's "Five things worth knowing"). Verified live and active in the
  workflow, including `signer-b-1`/`signer-b-2` sharing signer-set-a's redis
  instance and global round-coordination channel -- no cross-talk observed across
  several full runs.
- [x] Verify live transactions aren't lost during a reorg -- `scripts/simulate_reorg.py`
  injects one canary TPC transaction into group A right after the split (input
  predates the fork point, so it can't legitimately conflict with anything group B
  does), lets it confirm only on the losing fork, and after reconnection asserts it's
  either re-confirmed or back in the mempool -- not vanished. Deliberately the simple
  case only; harder cases (dependent-tx chains, deliberate conflicts) are listed under
  Outstanding work below.

## Milestone 4 -- Wire verified/built pieces into actual CI

Each of these corresponds to a step in `.github/workflows/weekly-integration-test.yml`:

- [x] `tapyrus-seeder` checkout in CI (`SEEDER_REPO_URL`/`SEEDER_REPO_REF` added to `config/repos.py`, wired into `checkout_repos.py` and the `seeder_repo_ref` CI variable)
- [x] `tapyrus-seeder` image build in CI -- inline `docker build` (same `DOCKER_BUILD_PLATFORM` pattern as the core/signer builds), tagged `tapyrus-seeder:integration-test` to match `docker/docker-compose.yml`'s `seeder` service.
- [x] Unsigned genesis build step (`tapyrus-genesis`) -- resolved toward `docker run --entrypoint tapyrus-genesis` against the already-built `tapyrus/tapyrusd:master-local` image (bypassing the image's daemon-oriented `entrypoint.sh` entirely), rather than a native binary from a second, separately-built copy -- guarantees the unsigned genesis matches the exact tapyrus-core commit under test.
- [x] Topology-convergence wait step -- `scripts/wait_for_topology.py` (`TopologyWaiter` class) polls `getconnectioncount` on all 7 nodes' published RPC ports for the expected 1/2/1/2/1/2/3 pattern, with a timeout and per-node mismatch reporting. Happy path confirmed via the real CI run above; the mismatch/timeout-reporting path itself was also exercised for real (deliberately stopped `core-1b`, confirmed correct per-node diagnostics -- `core-1a`/`core-7`'s wrong connection counts, `core-1b` reported unreachable -- and a clean `TimeoutError` instead of hanging; restored and confirmed reconvergence). Compose-level healthchecks (`depends_on: condition: service_healthy`) remain unbuilt -- see Outstanding work.
- [x] RPC-readiness retry for the coinbase-address-collection step -- `scripts/collect_coinbase_addresses.py` retries rather than a single inline call, avoiding a silently empty/null address. All real, exercisable paths now verified live: the happy path, the retry-after-initial-unreachable path (stopped `core-1a`, confirmed the script retries silently through `RpcUnreachable` and succeeds once RPC comes back), and the timeout-never-reachable path (confirmed a clean `TimeoutError` after the configured timeout, no hang). The empty-result path can't be exercised against real infrastructure -- a real node's `getnewaddress` never legitimately returns empty -- so it stays defensive-only code, not a tracked gap.
- [x] Transaction-generation step wired in and **active** (`generate_traffic.py`) --
  uncommented alongside "Assemble signer configs"/"Bring up signers". Verified
  end-to-end at smoke scale (`tx_round_count=2`) immediately before the reorg step,
  back-to-back in the same job: zero send/issuance failures across both rounds,
  settled with balances matching the ledger for real, not vacuously.
- [x] Reorg scripted as a reusable CI step and **active** -- `scripts/simulate_reorg.py`
  (`ReorgSimulator`). Encodes the hand-verified alternating-isolation recipe
  (`weekly-integration-test-plan.md` section 4d): build baseline, split, group B
  builds its fork alone, bring group A back + reset redis fresh, repoint + restart
  signers (reuses `assemble_signer_configs.py`'s `SignerConfigAssembler` directly),
  group A builds its own fork alone, reconnect (reuses `wait_for_topology.py`'s
  `TopologyWaiter` directly), extend group B by two blocks, confirm convergence via
  `getchaintips`. First script to drive `docker compose` itself (stop/start/force-recreate
  specific services), not just RPC. Uncommented right after the traffic-generation
  step. Verified end-to-end running immediately after `generate_traffic.py` in the
  same job (not standalone): baseline height derives from `TX_ROUND_COUNT + 2` (a
  floor, not a literal target -- the script captures the height actually reached,
  well past that floor once traffic generation has run, as the real reference point
  for fork-length math); every ex-group-A node showed exactly 2 tips (`active`
  matching group B's winning tip, `valid-fork` with `branchlen` matching group A's
  fork exactly); `core-3a`/`core-3b` showed a single active tip as expected.
- [x] Rotation step scripted and **active** -- `scripts/simulate_federation_change.py`,
  see Milestone 3 above for what it does. Verified live and uncommented in the
  workflow.
- [x] Rotation-confirmation step -- folded into the same script
  (`_confirm_rotation_via_rpc`: `getblockchaininfo`'s `aggregatePubkeys` array on all
  7 core nodes must show the new pubkey at the scheduled height), rather than a
  separate step.
- [x] `tapyrus-seeder` service actually brought up and verified, in both bring-up
  modes docker-compose.yml supports for the 7 core-* nodes -- `scripts/verify_seeder.py`
  (see `scripts.md`). Addseeder mode confirms every node's peer count grows
  organically from nothing via `-addseeder` alone (no `-connect`); connect mode
  confirms the seeder reports only genuinely-listening nodes (never `core-7`,
  deliberately seeded as a real negative case there) and that a brand-new node with
  no hardcoded topology knowledge auto-bootstraps through it. See `work-done.md`'s
  Lessons learnt for the underlying investigation (ADDR gossip, DNS-seed reliability
  thresholds, why `-connect` nodes never populate their own addrman, and why the
  compose network needs a specific custom subnet).
- [x] Per-node lifecycle orchestrator (scenario step 5) -- `scripts/container/node_orchestrator.py`,
  running inside every core-* container (`scripts/start_node_orchestrator.py` switches
  them into this mode right after the seeder step). core-1b/2b/3b/core-7 randomly
  stop/restart/reindex/invalidate themselves for the rest of the job; core-1a/2a/3a
  (the signers' own RPC targets) get crash-recovery supervision but never a
  deliberate chaos action, since even one of them briefly catching up from a
  restart could throw off `generate_traffic.py`'s coinbase-rotation tracking. A
  shared pause file protects `simulate_reorg.py`/`simulate_federation_change.py`/
  `simulate_maxblocksize_change.py`/`generate_traffic.py`'s own precise node
  up/down assumptions during their sensitive windows. See `work-done.md` for the
  full design. Verified live: `wait_for_topology.py` converges cleanly under
  chaos, and multiple full `generate_traffic.py` runs against the chaos-supervised
  stack settled with balances matching the ledger every time, zero mismatches.
- [x] Core-node and signer RPC auth switched from a static shared `rpcuser`/
  `rpcpassword` to tapyrus-core's own auto-generated per-process cookie file --
  every core-* node (plus `seeder-test-node`) writes it to a shared
  `runtime/rpc-cookies/` mount (`scripts.lib.rpc.cookie_path`/`read_cookie`), read
  fresh on every RPC call, not cached, since a chaos-restarted node's cookie
  changes on every restart. Signer configs point each signer directly at its own
  RPC target's cookie file (`rpc-endpoint-cookiefile`, `assemble_signer_configs.py`)
  rather than resolving and baking in a value, closing the earlier
  resolve-once-at-assembly-time staleness gap -- `tapyrus-signer` itself now reads
  it fresh on every RPC call too. `entrypoint_wrapper.sh` re-`chmod`s each cookie
  file to 644 in a background loop, since tapyrus-core always writes it `0600` and
  every core-* container runs as root -- confirmed live on GitHub Actions that
  without this, the CI host's own (non-root) reads get `PermissionError`.

## Outstanding work

Everything not yet implemented or not yet testable, in one place:

- **Harder transaction-survival-at-reorg cases** beyond the simple canary already
  covered: (a) a dependent-transaction chain (a second transaction spending the
  canary's own output, both confirmed only post-split), to check whether the mempool
  correctly cascades multiple orphaned transactions back in, not just a single one;
  (b) a deliberate conflicting double-spend of the same input on both forks, to
  confirm the losing side's version is correctly (not buggily) dropped; (c) whether
  an orphaned colored-coin issuance (`issuetoken`) that returns to the mempool keeps
  its original color id correctly, or whether that identity can drift on
  reconfirmation.
- **`docker/docker-compose.yml` has no compose-level healthchecks** /
  `depends_on: condition: service_healthy` -- `scripts/wait_for_topology.py` is a
  confirmed-working CI-level equivalent (see Milestone 4), but the compose-file
  enhancement itself is separate, unstarted work.
- **`scripts/generate_traffic.py`'s hardcoded constants** (`FUNDING_AMOUNT_TPC`/
  `TOKEN_ISSUE_AMOUNT`/etc.) haven't been stress-tested at a larger round count where
  the balance-shortfall top-up mechanic would trigger much more often

## Non-goals for v1

- Adversarial-node topology exploit (topology is designed to allow this later without rework)
- Performance/load testing
- Signer fault tolerance (Byzantine signers, below-threshold signer count)
- Running the full-scale scenario on every `tapyrus-core`/`tapyrus-signer` PR (weekly +
  on-demand only, by design). Does not cover this repo's own PRs -- those run a
  smoke-scale pass instead (see the `pull_request`/`push` triggers in the workflow).
