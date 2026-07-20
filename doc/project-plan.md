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

- [x] Per-node transaction generation -- `scripts/generate_traffic.py` (`TrafficNode` + `TrafficGenerator`). Round-robin TPC + colored-coin (REISSUABLE/NON_REISSUABLE/NFT mix, one type per node) sends across all 7 nodes, everything derived from a single round count (`tx_round_count`); balance verified against a tracked ledger after every block. Verified end-to-end against a real 7-node stack (`round_count=2`) -- three bugs found and fixed along the way, see `work-done.md`'s Lessons learnt. Wired into the workflow (`Generate round-robin TPC + colored-coin traffic` step).
- [ ] Per-node lifecycle orchestrator (RPC health/height/mempool query, stop, restart, confirm resync)
- [ ] Max-block-size (xfield) change -- push, confirm an over-limit block is rejected and an in-limit block is accepted
- [ ] Aggpubkey rotation handoff -- `--xfield` `sign`/`computesig` flow (current federation signs off on the new one) and a `federations.toml` writer that can append a second entry (today's `assemble_signer_configs.py` only ever writes one `[[federation]]` entry)

## Milestone 4 -- Wire verified/built pieces into actual CI

Each of these corresponds to a `TODO` placeholder step in
`.github/workflows/weekly-integration-test.yml`:

- [x] `tapyrus-seeder` checkout in CI (`SEEDER_REPO_URL`/`SEEDER_REPO_REF` added to `config/repos.py`, wired into `checkout_repos.py` and the `seeder_repo_ref` CI variable)
- [x] `tapyrus-seeder` image build in CI -- inline `docker build` (same `DOCKER_BUILD_PLATFORM` pattern as the core/signer builds), tagged `tapyrus-seeder:integration-test` to match `docker/docker-compose.yml`'s `seeder` service. `SEEDER_REPO_REF`/`SEEDER_REPO_URL` default to [`chaintope/tapyrus-seeder#5`](https://github.com/chaintope/tapyrus-seeder/pull/5)'s own branch (temporary, until it merges) so this step can actually pass in CI.
- [x] Unsigned genesis build step (`tapyrus-genesis`) -- resolved toward `docker run --entrypoint tapyrus-genesis` against the already-built `tapyrus/tapyrusd:master-local` image (bypassing the image's daemon-oriented `entrypoint.sh` entirely), rather than a native binary from a second, separately-built copy -- guarantees the unsigned genesis matches the exact tapyrus-core commit under test. Not yet run against a real CI runner.
- [x] Topology-convergence wait step -- `scripts/wait_for_topology.py` (`TopologyWaiter` class) polls `getconnectioncount` on all 7 nodes' published RPC ports for the expected 1/2/1/2/1/2/3 pattern, with a timeout and per-node mismatch reporting. **Not yet exercised against real or fake nodes** -- its own polling/convergence/timeout logic has no automated test; only the `CoreRpcClient` it's built on (see below) has been verified for real. Still open: `docker/docker-compose.yml` doesn't yet have compose-level healthchecks + `depends_on: condition: service_healthy` (plan doc section 3 step 6) -- this script is a CI-level equivalent, but the compose-file enhancement itself is separate, unstarted work.
- [x] RPC-readiness retry for the coinbase-address-collection step -- `scripts/collect_coinbase_addresses.py` (flagged in PR review against commit `4d0a384`: the prior inline `curl | jq` loop had no retry and could silently write an empty/null address). **Verified against a real `tapyrusd` container**: the happy path (`getnewaddress` against an already-up node) and, at the shared `CoreRpcClient` level, a successful call and the HTTP 401-\>`RpcError` misclassification fix (see `work-done.md`). The retry-after-initial-unreachable, empty-result, and timeout-never-reachable paths are **not yet covered** by any automated test, real or fake -- this was previously (inaccurately) documented as "tested against fake RPC servers"; no such tests ever existed in this repo (see the original PR review's finding).
- [x] Transaction-generation step wired in (`generate_traffic.py`, see Milestone 3) --
  currently commented out in the workflow alongside the rest of the
  signers-never-brought-up block (see "PR review response (round 2)"); re-enable once
  that block is restored.
- [ ] Orchestrator step for per-node query/lifecycle + max-block-size change (depends on the remaining Milestone 3 pieces existing first)
- [ ] Reorg scripted as a reusable CI step (recipe is verified by hand -- see `work-done.md` -- but every command was typed against a live stack, not captured as a script)
- [ ] Rotation step scripted (signer-set-b ceremony + `--xfield` handoff + config regen/restart at scheduled height)
- [ ] Rotation-confirmation step
- [ ] Slack report step (pass/fail, run metadata, both aggpubkeys, failure log tail) -- needs a Slack webhook secret provisioned (see Milestone 5)
- [ ] `tapyrus-seeder` service actually brought up (`docker compose up` today only ever
  starts `redis`/`core-*`/`signer-*` -- the image is built and validated but the
  service itself is never started) + a `dig`-based pass/fail check against it -- see
  the workflow's "Bring up tapyrus-seeder" step and "PR review response (round 3)"
  below.

## PR review response (against commit `4d0a384`)

Most of this review's blocking findings turned out to already be fixed by `1aca161`
(scripts/ and `doc/work-done.md` committed, `.gitignore` added, duplicated README
content removed from the plan doc). What was still open, now fixed:

- [x] Stale "plan doc section 3b" references (actual topology section is 4b) --
  `docker/docker-compose.yml`, `scripts/assemble_signer_configs.py`.
- [x] `.gitignore` missing `logs/` (the "Collect logs" step's local output dir).
- [x] No RPC-readiness wait before `getnewaddress` -- see `scripts/collect_coinbase_addresses.py` above.
- [x] `pull_request`/`push` triggers (path-filtered, smoke-scale) so this repo's own changes are validated pre-merge -- see `work-done.md`'s Design decisions.
- [x] `permissions: contents: read` and a `concurrency:` group added to the workflow.
- [x] Duplicated defaults between `workflow_dispatch.inputs.*.default` and the `env:`
  fallback literal -- `default:` removed from all 16 inputs (can't hold an expression
  anyway); `env:` is now the sole place each default is written. A manual dispatch left
  blank resolves the same value schedule uses; the dispatch form just shows blank
  fields instead of pre-filled ones (each input's `description:` states the default in
  words instead). See `work-done.md`'s Design decisions.
- [x] `SIGNER_REPO_URL`/`SIGNER_REPO_REF` switched to `chaintope/tapyrus-signer` @
  `master` (base ceremony only -- no federation-CHANGE/rotation support). Until
  Milestone 3/4's rotation items are built and tested, override both to the
  `Naviabheeman` fork's `163_federationChangeToml` branch to exercise rotation; see
  `work-done.md`'s Known issues.

## PR review response (round 2 -- build failures and smoke-run execution)

- [x] "Every trigger is guaranteed to fail at the build steps" (blocking) --
  `SIGNER_REPO_URL`/`SIGNER_REPO_REF` and `SEEDER_REPO_URL`/`SEEDER_REPO_REF` now
  default (temporarily) to the `Naviabheeman` fork branches backing
  [`chaintope/tapyrus-signer#172`](https://github.com/chaintope/tapyrus-signer/pull/172)
  and [`chaintope/tapyrus-seeder#5`](https://github.com/chaintope/tapyrus-seeder/pull/5),
  so a trigger can actually go green instead of relying on those PRs merging by the
  next scheduled run. Revert both to their `chaintope`/`master` defaults once the PRs
  merge -- see `work-done.md`'s Known issues and `config/repos.py`.
- [x] "Get the smoke run to actually execute on this PR before merge" (blocking) --
  `push:` trigger's `branches:` changed from an explicit `main`-only list to `'**'`
  (any branch), so a push to this PR's own branch fires the smoke run for real
  instead of relying only on `pull_request` (which GitHub may be holding on a
  first-time-contributor approval gate) -- and no branch name needs adding/removing
  around merges going forward. `pull_request`/`push` also switched from a `paths:`
  allowlist to `paths-ignore: ['doc/**']`, so any non-doc change triggers the smoke
  run without needing to keep the allowlist in sync with new directories.
- [x] Push cancelling an in-progress scheduled run -- `concurrency.group` now includes
  `github.event_name`, not just `github.ref`; previously a `push` to `main` and the
  Sunday `schedule` run shared the same group (same ref) and the push would cancel an
  in-flight full-scale run.
- [x] All scenario steps past "confirm tapyrusd is up" (Assemble signer-set-a configs
  through Confirm rotation took effect, plus the Slack report stub) commented out for
  this smoke run, falling straight through to Collect logs/Upload logs/Teardown --
  scopes the run to what's actually meant to be validated right now (finding #2)
  instead of running into Milestone 3/4's still-TODO territory. Restore once that work
  lands.

## PR review response (round 3 -- network_id, seeder, clone depth, timeout, arg validation)

- [x] `network_id` input was declared and threaded into `NETWORK_ID` but never actually
  consumed -- genesis creation relied on `tapyrus-genesis -dev`'s baked-in default, and
  the compose seeder hardcoded `1905960821`. Root cause (confirmed against a real
  container): `tapyrus/tapyrusd`'s own `entrypoint.sh` only auto-generates a
  `tapyrus.conf` if none is mounted, and the auto-generated one hardcodes
  `dev=1`/`[dev]`/`networkid=1905960821` -- nothing ever mounted a conf of our own, so
  `NETWORK_ID` had nowhere to go. Fixed: `tapyrus-genesis` no longer passes `-dev`
  (verified: genesis creation itself needs no network id at all, prod mode is the
  default when `-dev` is omitted); a new `scripts/render_tapyrus_conf.py` renders a
  prod-mode `tapyrus.conf` with `networkid=$NETWORK_ID`, mounted into all 7 core-*
  services (`docker/docker-compose.yml`). Verified end-to-end against a real
  `tapyrusd` container with a network id other than the old hardcoded default:
  `getblockchaininfo` reports `"mode": "prod"` and `"chain": "<the overridden id>"`,
  genesis loads correctly. Deliberately **not** wired into the seeder -- see the next
  item, the seeder isn't brought up at all yet, so wiring it now would silently drift
  from `NETWORK_ID` the moment it is.
- [x] `tapyrus-seeder`'s image is built and validated but the service was never
  actually started by any `docker compose up` invocation, and there's no `dig`-based
  pass/fail check -- deliberately deferred (see Milestone 4), but was invisible/easy
  to forget. Added a visible `echo`-TODO step ("Bring up tapyrus-seeder") to the
  workflow so it stays tracked.
- [x] `doc/scripts.md` called `checkout_repos.py`'s clone "shallow", but the actual
  `git clone --branch <ref> --single-branch` never passed `--depth` -- single-branch
  alone doesn't truncate history. Added `--depth 1` to the primary clone attempt
  (the existing full-clone-then-`checkout <ref>` fallback for raw commit SHAs is
  unaffected, and already handles the case a shallow `--branch` clone can't) -- also
  speeds up CI checkouts of `tapyrus-core` considerably.
- [x] The 60-minute smoke-run timeout may be tight for a cold run (C++ `tapyrus-core`
  Docker build + signer image build + a native `cargo build --release` with its own
  vendored GMP build, all on a 2-core runner) -- bumped to 3h for the smoke run (6h for
  the full-scale run, unchanged) as a generous safety margin; worth re-measuring
  against a real run and tightening back down once actual timings are in.
- [x] `assemble_signer_configs.py`'s `threshold` CLI arg had no `type=int` -- a
  non-numeric value would land in the generated `federations.toml` unvalidated
  instead of failing loudly at parse time. Fixed.

## PR review response (round 4 -- workflow validity, P2P port break, HTTP -28, stale signer default)

- [x] "Every trigger ends in startup_failure" (blocking) -- `workflow_dispatch`
  defined 16 inputs; GitHub Actions caps it at 10, and exceeding that makes the whole
  workflow file invalid for every trigger, not just `workflow_dispatch` (confirmed via
  two real fork runs, both `startup_failure`, once the `push: branches: '**'` change
  from round 3 started actually exercising the trigger). Cut to 10: dropped
  `tx_total_count`/`tx_tpc_percent`/`tx_interval_seconds`/`rotation_height_offset`/
  `max_block_size_new`/`prng_seed_base` -- all consumed only by still-commented-out/TODO
  steps, so nothing else changes. Their `env:` fallback literals stay in place
  (referencing a now-undeclared `inputs.x` is not a schema error -- it just evaluates
  to `null`/falls through to the literal at runtime, for every trigger, not only the
  ones that already had no `inputs` context); reintroduce as real inputs, a JSON
  "overrides" input, or repo vars once a Milestone 3/4 step actually needs one.
- [x] "The prod-mode switch breaks the P2P topology" (blocking) --
  `render_tapyrus_conf.py` also pinned `port=12383` (dev mode's old P2P port) in the
  rendered conf, but `docker-compose.yml`'s portless `-connect=<service-name>`
  resolves against the *chain's default* P2P port, which for prod mode is `2357`, not
  `12383`. Every `-connect` edge (core-1b/2b/3b/7) was dialing a port nothing
  listened on -- zero P2P connections, topology never converges. Fixed by dropping
  `port=` from the rendered conf entirely (not pinning `:12383` on every `-connect`
  instead, to avoid a second place the port number has to stay in sync). Also fixed
  the seeder's `-s` crawl-start target (`docker-compose.yml`), which encoded the same
  stale `12383` P2P port. Verified for real against the full 7-node topology (not just
  one container): `wait_for_topology.py` converged on attempt 1 matching the expected
  1/2/1/2/1/2/3 pattern, `getpeerinfo` on `core-1b` directly confirmed 2 real peers.
  See `work-done.md`'s Lessons learnt for the full incident writeup.
- [x] Related/compounding -- "Wait for topology to converge" moved out of the
  commented-out signers-never-brought-up block and now runs unconditionally right
  after the 7 core nodes come up: it only depends on `getconnectioncount` against the
  core nodes, not signers, and it's exactly the check that would have caught the P2P
  break above. `collect_coinbase_addresses.py`'s RPC-only check could pass on a fully
  partitioned network, so this closes that gap for future P2P-relevant changes too.
- [x] "HTTPError fix regressed warmup handling" -- the round-3 `HTTPError`->`RpcError`
  fix (correctly) stopped misclassifying real RPC errors as retryable, but along with
  it also stopped retrying `RPC_IN_WARMUP` (-28), which `tapyrusd` serves as HTTP 500
  with a JSON-RPC error body -- exactly the readiness window right after `docker
  compose up`, and exactly what the *original* review request asked to keep working.
  Fixed in `lib/rpc.py`: `-28` is now parsed out of the `HTTPError` body and raises
  `RpcUnreachable` (retryable), same as connection-refused; every other HTTP error
  still raises `RpcError`, now with the JSON-RPC code/message included instead of
  just the bare HTTP status. Verified live racing a real container's actual warmup
  window (not simulated): 30 consecutive real `-28` responses, all retried, then a
  clean success; a bad-credentials 401 (which has no response body at all) still
  raises `RpcError` without crashing the JSON parse. See `work-done.md`'s Lessons
  learnt for the full writeup, including the exact `tapyrus-core` source read to
  confirm the response shape.
- [x] "chaintope/tapyrus-signer#172 has merged -- temporary signer default is
  obsolete" -- confirmed independently (not just PR metadata): fetched
  `chaintope/tapyrus-signer`'s `master` fresh, its new tip is literally the
  `c-no-tests` fix commit, `Cargo.toml` has the feature, and `cargo build --release`
  against it is clean. `config/repos.py`'s `SIGNER_REPO_URL`/`SIGNER_REPO_REF` default
  switched from the `Naviabheeman` fork's `master-build-fix` straight back to
  `chaintope/tapyrus-signer` @ `master`; the `TEMPORARY` comment block trimmed to just
  the seeder case (`chaintope/tapyrus-seeder#5` -- checked too, still unmerged:
  `chaintope/tapyrus-seeder`'s `master` tip is still the old pre-fix commit, `main.cpp`
  still has the `sprintf`/`%d` bugs).

## PR review response (round 5 -- push/pull_request double-run, comment fix, test suggestion)

- [x] "push: branches: '**' double-runs with pull_request after merge" -- correct: any
  push to a PR branch that lives in the base repo (not a fork) fires both `push` and
  `pull_request`, two identical smoke runs per commit. `'**'` stays for now -- it's the
  only way to verify a fix by pushing directly while this PR is still blocked on the
  fork's first-time-contributor approval gate -- but flagged with a `TEMPORARY`
  comment in the workflow explaining the double-run and pointing back here: narrow
  `push.branches` to `[main]` once this PR merges.
- [x] "docker-compose.yml's seeder comment got mangled mid-edit" -- already fixed by
  the time this was raised (a full rewrite of that comment block landed in round 4);
  re-verified no fragment/double-space artifacts remain.
- [ ] "A minimal tests/ for CoreRpcClient (warmup-retry, 401, unreachable-timeout) as
  a cheap early CI step would pay for itself" -- flagged non-blocking; not built yet.
  All three paths have been verified by hand against real containers this cycle (see
  `work-done.md`'s Lessons learnt: the `-28` warmup-retry regression and its fix, plus
  the 401 empty-body path) but none of that is captured as a committed, automated
  test -- the exact gap finding #3 (round 4) pointed out. Worth doing before the next
  time `lib/rpc.py` changes, not before this PR merges.

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
