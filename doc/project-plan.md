# Project Plan: Weekly Cross-Repo Integration Test

Tracks progress toward a fully automated CI run of the scenario in
[`weekly-integration-test-plan.md`](weekly-integration-test-plan.md). Update this file
as work lands -- it's the single place to check "what's actually done" vs. "what's
designed but not built yet."

Legend: `[x]` done and verified -- `[~]` verified by hand, not yet wired into CI --
`[ ]` not started.

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
- [x] `tapyrus-seeder` integrated as a compose service; four real upstream bugs found and fixed (build failure, sprintf buffer overflow, two data races), verified via 8/8 clean restarts + 0 TSan races + live `dig` resolution
- [x] Signer RPC-connectivity requirement confirmed: `tapyrus-signerd` needs a live RPC connection to its own core node even as a non-master over Redis (no Redis-only fallback)
- [x] Reorg (scenario step 8): full two-group fork-and-reconverge run for real, confirmed via `getchaintips` (`valid-fork`, correct `branchlen`)
- [x] Re-verified fresh, end-to-end, through the new Python scripts (not the original bash scripts)

See [`work-done.md`](work-done.md) for the full transcripts.

## Milestone 3 -- Scenario mechanics not yet built at all

- [x] Per-node transaction generation -- `scripts/generate_traffic.py` (`TrafficNode` + `TrafficGenerator`). Round-robin TPC + colored-coin (REISSUABLE/NON_REISSUABLE/NFT mix, one type per node) sends across all 7 nodes, everything derived from a single round count (`tx_round_count`); balance verified against a tracked ledger after every block. Wired into the workflow (`Generate round-robin TPC + colored-coin traffic` step). Re-verified end-to-end with the `fallbackfee` fix in place (see `work-done.md`'s Lessons learnt): zero send/issuance failures across 2 full rounds, real balance changes throughout, settled correctly.
- [ ] `generate_traffic.py`'s settle-height ledger assertion can't distinguish "real activity, correctly tracked" from "total failure, trivially consistent" (see above) -- worth a hard floor (e.g. assert at least N successful sends/mints happened this run, not just that tracked-vs-actual balances agree) so a fully-broken run fails loudly instead of exiting 0.
- [ ] Per-node lifecycle orchestrator (RPC health/height/mempool query, stop, restart, confirm resync)
- [ ] Max-block-size (xfield) change -- push, confirm an over-limit block is rejected and an in-limit block is accepted
- [ ] Aggpubkey rotation handoff -- `--xfield` `sign`/`computesig` flow (current federation signs off on the new one) and a `federations.toml` writer that can append a second entry (today's `assemble_signer_configs.py` only ever writes one `[[federation]]` entry)
- [x] Verify live transactions aren't lost during a reorg -- `scripts/simulate_reorg.py`
  now injects one canary TPC transaction into group A right after the split (input
  predates the fork point, so it can't legitimately conflict with anything group B
  does), lets it confirm only on the losing fork, and after reconnection asserts it's
  either re-confirmed or back in the mempool -- not vanished. Deliberately the simple
  case only; see below for what's deferred.
- [ ] harder transaction-survival-at-reorg cases beyond the simple
  canary above -- (a) a dependent-transaction chain (a second transaction spending
  the canary's own output, both confirmed only post-split), to check whether the
  mempool correctly cascades multiple orphaned transactions back in, not just a
  single one; (b) a deliberate conflicting double-spend of the same input on both
  forks, to confirm the losing side's version is correctly (not buggily) dropped,
  distinguishing "lost because it conflicted" from "lost due to a bug"; (c) whether
  an orphaned colored-coin issuance (`issuetoken`) that returns to the mempool keeps
  its original color id correctly, or whether that identity can drift on
  reconfirmation.

## Milestone 4 -- Wire verified/built pieces into actual CI

Each of these corresponds to a `TODO` placeholder step in
`.github/workflows/weekly-integration-test.yml`:

- [x] `tapyrus-seeder` checkout in CI (`SEEDER_REPO_URL`/`SEEDER_REPO_REF` added to `config/repos.py`, wired into `checkout_repos.py` and the `seeder_repo_ref` CI variable)
- [x] `tapyrus-seeder` image build in CI -- inline `docker build` (same `DOCKER_BUILD_PLATFORM` pattern as the core/signer builds), tagged `tapyrus-seeder:integration-test` to match `docker/docker-compose.yml`'s `seeder` service. `SEEDER_REPO_REF`/`SEEDER_REPO_URL` default to [`chaintope/tapyrus-seeder#5`](https://github.com/chaintope/tapyrus-seeder/pull/5)'s own branch (temporary, until it merges) so this step can actually pass in CI.
- [x] Unsigned genesis build step (`tapyrus-genesis`) -- resolved toward `docker run --entrypoint tapyrus-genesis` against the already-built `tapyrus/tapyrusd:master-local` image (bypassing the image's daemon-oriented `entrypoint.sh` entirely), rather than a native binary from a second, separately-built copy -- guarantees the unsigned genesis matches the exact tapyrus-core commit under test. Not yet run against a real CI runner.
- [x] Topology-convergence wait step -- `scripts/wait_for_topology.py` (`TopologyWaiter` class) polls `getconnectioncount` on all 7 nodes' published RPC ports for the expected 1/2/1/2/1/2/3 pattern, with a timeout and per-node mismatch reporting. **Not yet exercised against real or fake nodes** -- its own polling/convergence/timeout logic has no automated test; only the `CoreRpcClient` it's built on (see below) has been verified for real. Still open: `docker/docker-compose.yml` doesn't yet have compose-level healthchecks + `depends_on: condition: service_healthy` (plan doc section 3 step 6) -- this script is a CI-level equivalent, but the compose-file enhancement itself is separate, unstarted work.
- [x] RPC-readiness retry for the coinbase-address-collection step -- `scripts/collect_coinbase_addresses.py` (flagged in PR review against commit `4d0a384`: the prior inline `curl | jq` loop had no retry and could silently write an empty/null address). **Verified against a real `tapyrusd` container**: the happy path (`getnewaddress` against an already-up node) and, at the shared `CoreRpcClient` level, a successful call and the HTTP 401-\>`RpcError` misclassification fix (see `work-done.md`). The retry-after-initial-unreachable, empty-result, and timeout-never-reachable paths are **not yet covered** by any automated test, real or fake -- this was previously (inaccurately) documented as "tested against fake RPC servers"; no such tests ever existed in this repo (see the original PR review's finding).
- [x] Transaction-generation step wired in and **active** (`generate_traffic.py`) --
  uncommented alongside "Assemble signer-set-a configs"/"Bring up signer-set-a" (the
  rest of the per-node/max-block-size/rotation block stays commented, still genuinely
  unbuilt). Re-verified end-to-end at smoke scale (`tx_round_count=2`) immediately
  before the reorg step, back-to-back in the same job, with the `fallbackfee` fix in
  place: zero send/issuance failures across both rounds (previously 100%), settled
  with balances matching the ledger for real, not vacuously. See "PR review response
  (round 6)".
- [ ] Orchestrator step for per-node query/lifecycle + max-block-size change (depends on the remaining Milestone 3 pieces existing first)
- [x] Reorg scripted as a reusable CI step and **active** -- `scripts/simulate_reorg.py`
  (`ReorgSimulator`). Encodes the hand-verified 8-step recipe
  (`weekly-integration-test-plan.md` section 4d): build baseline, split, group A
  builds its fork, bring group B back + reset redis fresh, repoint + restart signers
  (reuses `assemble_signer_configs.py`'s `SignerConfigAssembler` directly), group B
  builds the longer fork, reconnect (reuses `wait_for_topology.py`'s `TopologyWaiter`
  directly), confirm via `getchaintips`. First script to drive `docker compose`
  itself (stop/start/force-recreate specific services), not just RPC. Uncommented
  right after the traffic-generation step. Re-verified end-to-end running
  immediately after `generate_traffic.py` in the same job (not standalone): baseline
  height derives from `TX_ROUND_COUNT + 2` (a floor, not a literal target -- the
  script captures the height actually reached, well past that floor once traffic
  generation has run, as the real reference point for fork-length math); every
  ex-group-A node showed exactly 2 tips (`active` matching group B's winning tip,
  `valid-fork` with `branchlen` matching group A's fork exactly); `core-3a` showed a
  single active tip as expected. See "PR review response (round 6)".
- [ ] Rotation step scripted (signer-set-b ceremony + `--xfield` handoff + config regen/restart at scheduled height)
- [ ] Rotation-confirmation step
- [ ] Slack report step (pass/fail, run metadata, both aggpubkeys, failure log tail) -- needs a Slack webhook secret provisioned (see Milestone 5)
- [ ] `tapyrus-seeder` service actually brought up (`docker compose up` today only ever
  starts `redis`/`core-*`/`signer-*` -- the image is built and validated but the
  service itself is never started) + a `dig`-based pass/fail check against it -- see
  the workflow's "Bring up tapyrus-seeder" step and "PR review response (round 3)"
  below.

## Milestone 5 -- Team/repo readiness

- [x] Dedicated repo exists (`chaintope/tapyrus-integration-tests`)
- [ ] Team review/sign-off on the design in `weekly-integration-test-plan.md`
- [x] Switch `tapyrus-signer` source to `master` -- see "PR review response" above.
- [ ] Slack webhook URL provisioned as a GitHub Actions secret
- [ ] Confirm a GitHub-hosted runner (`ubuntu-latest`) has enough CPU/disk for 7 core nodes + 3 signers + redis, or move to self-hosted
- [ ] Switch core-node RPC auth from the static `rpcuser`/`rpcpassword` (hardcoded in
  `scripts/render_tapyrus_conf.py`, `CORE_RPC_USER`/`CORE_RPC_PASS` in the workflow's
  `env:` block, and every script that calls `CoreRpcClient`) to cookie authentication
  (tapyrusd's auto-generated per-run `.cookie` file) -- avoids a fixed shared password
  sitting in the conf/env for the run's lifetime.

## Non-goals for v1

- Adversarial-node topology exploit (topology is designed to allow this later without rework)
- Performance/load testing
- Signer fault tolerance (Byzantine signers, below-threshold signer count)
- Running the full-scale scenario on every `tapyrus-core`/`tapyrus-signer` PR (weekly +
  on-demand only, by design). Does not cover this repo's own PRs -- those now run a
  smoke-scale pass, see "PR review response" above.
