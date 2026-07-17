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

- [ ] Per-node transaction generation (TPC + colored-coin mix, deterministic PRNG seeding) -- blocks today only ever contain the coinbase transaction
- [ ] Per-node lifecycle orchestrator (RPC health/height/mempool query, stop, restart, confirm resync)
- [ ] Max-block-size (xfield) change -- push, confirm an over-limit block is rejected and an in-limit block is accepted
- [ ] Aggpubkey rotation handoff -- `--xfield` `sign`/`computesig` flow (current federation signs off on the new one) and a `federations.toml` writer that can append a second entry (today's `assemble_signer_configs.py` only ever writes one `[[federation]]` entry)

## Milestone 4 -- Wire verified/built pieces into actual CI

Each of these corresponds to a `TODO` placeholder step in
`.github/workflows/weekly-integration-test.yml`:

- [x] `tapyrus-seeder` checkout in CI (`SEEDER_REPO_URL`/`SEEDER_REPO_REF` added to `config/repos.py`, wired into `checkout_repos.py` and the `seeder_repo_ref` CI variable)
- [x] `tapyrus-seeder` image build in CI -- inline `docker build` (same `DOCKER_BUILD_PLATFORM` pattern as the core/signer builds), tagged `tapyrus-seeder:integration-test` to match `docker/docker-compose.yml`'s `seeder` service. Will still fail until `seeder_repo_ref` is overridden to a branch with the fixes in `work-done.md` -- `SEEDER_REPO_REF` defaults to `master`, which lacks them.
- [x] Unsigned genesis build step (`tapyrus-genesis`) -- resolved toward `docker run --entrypoint tapyrus-genesis` against the already-built `tapyrus/tapyrusd:master-local` image (bypassing the image's daemon-oriented `entrypoint.sh` entirely), rather than a native binary from a second, separately-built copy -- guarantees the unsigned genesis matches the exact tapyrus-core commit under test. Not yet run against a real CI runner.
- [x] Topology-convergence wait step -- `scripts/wait_for_topology.py` (`TopologyWaiter` class) polls `getconnectioncount` on all 7 nodes' published RPC ports for the expected 1/2/1/2/1/2/3 pattern, with a timeout and per-node mismatch reporting. Tested against fake local RPC servers (converge-over-time, timeout, and unreachable-node paths). Still open: `docker/docker-compose.yml` doesn't yet have compose-level healthchecks + `depends_on: condition: service_healthy` (plan doc section 3 step 6) -- this script is a CI-level equivalent, but the compose-file enhancement itself is separate, unstarted work.
- [ ] Orchestrator step for per-node tx/query/lifecycle + max-block-size change (depends on Milestone 3 pieces existing first)
- [ ] Reorg scripted as a reusable CI step (recipe is verified by hand -- see `work-done.md` -- but every command was typed against a live stack, not captured as a script)
- [ ] Rotation step scripted (signer-set-b ceremony + `--xfield` handoff + config regen/restart at scheduled height)
- [ ] Rotation-confirmation step
- [ ] Slack report step (pass/fail, run metadata, both aggpubkeys, failure log tail) -- needs a Slack webhook secret provisioned (see Milestone 5)

## Milestone 5 -- Team/repo readiness

- [x] Dedicated repo exists (`chaintope/tapyrus-integration-tests`)
- [ ] Team review/sign-off on the design in `weekly-integration-test-plan.md`
- [ ] Switch `tapyrus-signer` source to `master`: `chaintope/tapyrus-signer`'s own `master` already has the base ceremony (createkey/createnodevss/aggregate/genesis-signing, confirmed nearly identical, see plan doc section 1/5) -- `163_federationChangeTomlSetup` is only actually required for federation CHANGE/rotation (`--xfield` sign/computesig, multi-entry `federations.toml`), which `master` doesn't have yet. Switch `config/repos.py`'s default once this integration test has validated `163_federationChangeTomlSetup`'s rotation support for real (Milestone 3/4's rotation items) and that work has landed on `master`.
- [ ] Slack webhook URL provisioned as a GitHub Actions secret
- [ ] Confirm a GitHub-hosted runner (`ubuntu-latest`) has enough CPU/disk for 7 core nodes + 3 signers + redis, or move to self-hosted


## Non-goals for v1

- Adversarial-node topology exploit (topology is designed to allow this later without rework)
- Performance/load testing
- Signer fault tolerance (Byzantine signers, below-threshold signer count)
- Running on every PR (weekly + on-demand only, by design)
