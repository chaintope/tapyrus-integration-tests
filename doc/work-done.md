# Work done: known issues, lessons learnt, design decisions

Consolidated record of open problems, gotchas found the hard way, and deliberate
design choices made while building this repo's scripts, CI workflow, and compose
stack. Referenced throughout the code/config as "see doc/work-done.md" rather than
carrying this narrative in comments themselves -- this file is the one place to look
for the *why* behind anything that looks non-obvious.

See [`weekly-integration-test-plan.md`](weekly-integration-test-plan.md) for the
original scenario design, [`project-plan.md`](project-plan.md) for tracked
done-vs-outstanding progress, and [`scripts.md`](scripts.md) for what each script does.

## Known issues (open)

- **`tapyrus-seeder` fails to build on the default `SEEDER_REPO_REF=master`** --
  reproduced for real (local full E2E test): fails on bug #1 below (Alpine g++
  rejecting a designated-initializer in `dns.cpp`). All four fixes only exist on a
  local, unpushed `docker-build-fix` branch today. Override `SEEDER_REPO_REF` (and
  `SEEDER_REPO_URL` if testing locally) once a fixed branch is pushed somewhere
  reachable.
- **`tapyrus-signer` doesn't build out of the box** -- neither the
  `163_federationChangeTomlSetup` branch nor `chaintope/tapyrus-signer`'s own `master`
  has the toolchain pin or GMP fix (see Lessons learnt below) committed. Applied by
  hand to the disposable `workdir/` checkout for local testing; not yet committed
  anywhere reachable (only on a local, unpushed `federation-setup-review` branch).
- **GitHub-hosted `ubuntu-latest` runner's CPU/disk sufficiency is unconfirmed** for 7
  core nodes + 3 signers + redis + seeder running concurrently -- may need
  self-hosted.
- **`docker/docker-compose.yml` has no compose-level healthchecks** /
  `depends_on: condition: service_healthy` yet (plan doc section 3 step 6 guidance).
  `scripts/wait_for_topology.py` is a CI-level equivalent -- arguably stronger, since
  it confirms real P2P peer counts rather than just RPC reachability -- but the
  compose-file enhancement itself is separate, unstarted work.
- **`163_federationChangeTomlSetup` is only actually required for federation
  change/rotation** (the `--xfield` sign/computesig flow, multi-entry
  `federations.toml`) -- `chaintope/tapyrus-signer`'s own `master` already has the base
  ceremony (createkey/createnodevss/aggregate/genesis-signing, confirmed nearly
  identical). Switch `config/repos.py`'s signer default to `master` once this
  integration test has validated `163_federationChangeTomlSetup`'s rotation support
  for real and that work lands upstream.
- **Signer count (3) / threshold (2) is hardcoded**, not a per-run variable -- the
  7-node topology in `docker-compose.yml` is wired 1:1 to exactly 3 signers; changing
  the count means redesigning the topology, not just passing a different number.
- **Scenario mechanics not yet built** (see `project-plan.md` Milestone 3/4 for the
  tracked list): per-node tx generation, lifecycle orchestrator, max-block-size xfield
  change, reorg (recipe verified by hand against a live stack, not yet scripted as a
  reusable step), rotation handoff scripting, rotation confirmation, Slack report.
- **Planned runner-matrix expansion**: once CI is stable on a single `ubuntu-latest`
  runner, add macOS (native arm64) and x86_64 nodes to the runner mix, so each
  platform builds natively instead of relying on `DOCKER_BUILD_PLATFORM`-forced QEMU
  emulation on a mismatched runner architecture.

## Lessons learnt (bugs found and fixed, gotchas)

- **Git submodule init** (found via a real full local build, not inspection):
  `tapyrus-core` vendors `secp256k1` as a git submodule. `checkout_repos.py`'s
  clone/update logic never initialized submodules, so `src/secp256k1` was left an
  empty directory -- looked like a complete checkout but failed the Docker image build
  at cmake's configure step (`does not contain a CMakeLists.txt file`). Fixed by
  running `git submodule update --init --recursive` after every clone/update
  (harmless no-op for repos without submodules).
- **stdout buffering reordered log output**: Python's stdout is fully block-buffered
  when not attached to a tty (the normal case in CI), which reordered the uniform
  logger's own lines against each other and against subprocess output (e.g. git's own
  clone progress, written directly to the inherited fd, uncaptured). Fixed by making
  every log write `flush=True`.
- **`createnodevss`/`createblockvss` output ordering**: output lines
  (`<receiver_pubkey>:<vss_hex>`) are sorted by receiver pubkey (BTreeMap iteration in
  the Rust source), NOT by `--public-key` argument order. Extracting by line position
  instead of matching the actual pubkey fails with an opaque `InvalidSS` error from
  `tapyrus-setup` itself.
- **`computesig` array-length requirement**: `--sig`/`--block-vss`/`--node-vss` arrays
  must all be the full signer count (not just `threshold`) -- enforced by an
  `assert_eq!` in the source -- despite tapyrus-signer's own `doc/setup.md` saying "t
  Local signatures are required". `computesig` must also be run with one specific
  signer's own key material (hardcoded to signer-0 in this repo's scripts) -- it can't
  be run by a neutral party without borrowing a signer's secrets, so a "designated
  signer" is a real v1 limitation, not an oversight.
- **tapyrus-core auto-disables listening the instant `-connect` is set at all**
  (`InitParameterInteraction: -connect set -> setting -listen=0`), not just
  restricting outbound dialing as originally assumed. Putting `-connect` on both ends
  of an edge gives both ends `-listen=0`, so neither can ever accept the other's
  connection. Fixed: exactly one `-connect` per edge (the "child" dials its "parent"),
  with an explicit `-listen=1` added back wherever a node also needs to accept an
  inbound edge.
- **`tapyrus/tapyrusd`'s entrypoint does `exec bash -c "$*"`** against the image's
  default CMD, and docker-compose's `command:` *replaces* that default CMD rather than
  appending to it -- a bare `command: [-connect=core-1b]` crashes every such container
  (`bash -c "-connect=core-1b"` -> invalid option). Fixed by having every `command:`
  override repeat the full default invocation and append its own flags.
- **`tapyrus-seeder`'s four real upstream bugs**, found and fixed on a local
  `docker-build-fix` branch, all confirmed live:
  1. Build failure -- Alpine 3.7's g++ rejects a designated-initializer used for
     `struct msghdr` in `dns.cpp` as "non-trivial" (not architecture-specific). Fixed
     with plain field assignment instead.
  2. Runtime segfault on the first real connection -- `char filename[25]` in
     `main.cpp` is too small for its own `sprintf` format strings (up to 31 bytes
     needed for a 10-digit network id), overflowing even the image's own default
     production network id. Caught by Alpine/musl's fortified `sprintf` (SIGTRAP),
     confirmed via `gdb` + a core dump. Fixed by bumping both `filename` buffers to 64
     bytes.
  3. Data race in the DNS threads -- `dns.cpp`'s global `listenSocket` was
     checked-and-created with no synchronization across the 4 concurrent DNS threads.
     Found via a ThreadSanitizer build on `ubuntu:22.04` (Alpine/musl has weak TSan
     support). Fixed with a mutex.
  4. Data race in the crawler-thread spawn loop -- `main.cpp`'s per-crawler-thread
     `ThreadCrawler_options` was stack-allocated inside the spawning loop and reused
     by the next iteration before the new thread reliably read it. Fixed by
     heap-allocating it instead.

     Verified end-to-end after all four fixes: 8/8 consecutive `docker compose`
     restarts stayed up (vs. crashing on the very first run pre-fix, then ~1/3 of
     restarts pre-fix-3/4); 0 TSan races across 8 runs (vs. every run before); `dig`
     against the running container returned a real, live-discovered peer address.
- **Building `tapyrus-core`'s Dockerfile** needs the real `tapyrus/builder:v0.7.0`
  image, not a stale, locally-mistagged image of the same name -- confirmed once by
  diffing digests. Environment-specific, unlikely to recur, noted here in case it
  does.
- **`tapyrus-setup createkey` always produces mainnet-prefixed WIFs** (`K`/`L`,
  `0x80`), unlike tapyrus-core's own `-dev` network convention (`c` prefix, `0xef`).
  Hasn't caused problems in practice, not stress-tested beyond that.
- **`tapyrus-signerd` needs a *live* RPC connection to its own configured core node to
  participate in a signing round at all** -- even purely as a non-master over Redis.
  Confirmed directly: with a signer's core-node RPC target down, it fails whether or
  not it's master that round (as master: `RPC getnewblock failed... No route to
  host`; as non-master: processing someone else's candidate block also requires a
  live RPC call to its own node, so it fails the same way). There is no Redis-only
  fallback. This is why the reorg recipe below needs every signer in the "losing"
  group repointed to that group's one surviving core node before it can sign at all.
- **`sign`/`computesig` also accept `--xfield` as an alternative to `--block`**,
  signing an aggpubkey rotation or max-block-size change instead of a genesis block --
  real, present in code, undocumented upstream. This is the actual mechanism behind
  the federation-rotation design decision below.
- **`gmp-mpfr-sys`'s Cargo.lock can drift**: building with an unpinned/newer toolchain
  first silently upgrades the lock file format and re-resolves the `tapyrus` dependency
  from crates.io instead of the git tag the committed lock specifies; pinning back to
  1.70.0 afterward then fails to parse the newer lock file. Fix: `git checkout HEAD --
  Cargo.lock` before rebuilding with the pinned toolchain, if this happens.
- **On macOS, building tapyrus-signer outside Docker also needs `brew install gmp` +
  `LIBRARY_PATH=/opt/homebrew/lib`** for the linker to find the system `libgmp` a
  transitive dependency wants (separate from the vendored `gmp-mpfr-sys` build above).
  Confirmed working during the full local E2E test.
- **A benign warning, any `round-duration`**: a non-master signer's own `submitblock`
  call occasionally races a block another signer already submitted, and tapyrus-core's
  `"duplicate"` string response doesn't match what the Rust client's JSON deserializer
  expects (`invalid type: string "duplicate", expected unit`). Harmless -- the block
  was already accepted through the other path.
- **The minimal 1-core-node + redis + 3-signer stack sustained real block production
  for ~32 minutes** (56 blocks, 19+ successful rounds, zero `InvalidBlock` errors) at
  `round-duration=60`, vs. transient `InvalidBlock`/"candidate block is not set"
  errors around round boundaries at `round-duration=10` -- the evidence behind the
  `round-duration=60` recommendation elsewhere in this file and in `README.md`.
- **`chaintope/tapyrus-signer`'s own `master` already has the federation-setup
  ceremony** -- diffing trees against the `163_federationChangeTomlSetup` fork branch
  showed only 4 files / 11 lines differ (same work, landed upstream via a rebased
  series). The fork's other, now-moot branches (`feature/sign_with_schnorr_signature`,
  a 2019 Redis DKG-round prototype; `expose-aggpubkey-file`, a patch on top of it) were
  early, superseded investigation -- kept on their own branches for history, not part
  of the current plan.
- **`getnewaddress` right after `docker compose up -d` needs a retry, not a single
  shot** (flagged in PR review, not caught by local testing since a local Docker daemon
  usually has the containers' RPC servers up well before the next command runs): a
  container reported "running" only means the process started, not that `tapyrusd`'s
  RPC server has finished initializing -- a bare `curl` can hit connection-refused. A
  naive `curl | jq -r .result` pipeline also masks this: `jq` prints the string `"null"`
  for a failed/empty response and the pipeline still exits 0, so a bad address could
  reach `/tmp/addrs.txt` silently. Fixed by replacing the inline bash/curl/jq loop with
  `scripts/collect_coinbase_addresses.py`, which retries each node via
  `lib/rpc.py`'s `RpcUnreachable` until it answers (or times out loudly) and raises on
  an empty address instead of writing one.

## Reorg -- full run transcript

Genuine two-sided fork, built by two isolated groups each independently
threshold-signing their own blocks from a common tip, reconnected, and confirmed via
`getchaintips` -- not simulated:

1. Full 7-node network ran to a common baseline (height 30, identical tip on all 7
   nodes), then the 3 signers were stopped to freeze it cleanly.
2. **Split**: stopped `core-3a`/`core-3b`/`core-7`, leaving `core-1a`/`core-1b`/
   `core-2a`/`core-2b` (group A) live. Restarted the 3 signers with the default RPC
   mapping unchanged (`signer-0`/`signer-1` still had live targets in this group).
3. **Group A built the losing fork**: 10 blocks past the split, to height 40, then
   frozen (signers stopped, then all 4 group-A core nodes).
4. **Group B brought back**: `core-3a`/`core-3b`/`core-7` restarted, still at height
   30, unaware of group A's blocks. Redis reset fresh. All three signers repointed to
   `core-3a` (the RPC-connectivity requirement above) and restarted.
5. **Group B built the winning fork**: 39 blocks past the split, to height 69 (a
   polling-interval overshoot past the original ~12-block target -- harmless, the
   reorg depth that matters is defined by the losing chain's own length).
6. **Reconnected** group A's core nodes alongside the running group B. All 7 nodes
   converged on height 69 within seconds -- no manual intervention.
7. **Confirmed via `getchaintips`, not just height**: every ex-group-A node showed
   exactly two tips -- the new shared tip (`height: 69`, `status: "active"`) and its
   own former tip (`height: 40`, `status: "valid-fork"`, `branchlen: 10`, matching
   group A's block count exactly). `core-3a` (winning side throughout) showed a single
   active tip, as expected.

## Design decisions

- **`tapyrus-genesis` invocation in CI**: runs via
  `docker run --rm --entrypoint tapyrus-genesis` against the already-built
  `tapyrus/tapyrusd:master-local` image, bypassing the image's own `entrypoint.sh`
  (which wraps the default CMD in `bash -c "$*"` and expects `GENESIS_BLOCK_WITH_SIG`
  for the long-running daemon). `tapyrus-genesis` is a stateless one-shot tool with
  none of that daemon machinery, and reusing the already-built image guarantees the
  unsigned genesis matches the exact tapyrus-core commit under test, rather than a
  second, separately-built copy. Verified against a real image in a full local test.
- **All scripts are Python** (stdlib only, no third-party dependencies), converted
  from an earlier bash version -- class-based (`RepoCheckout`, `AggpubkeyCeremony`,
  `GenesisSigningCeremony`, `SignerConfigAssembler`, `TopologyWaiter`), sharing a
  uniform logger (`scripts/lib/log.py`) and a common ceremony base class
  (`scripts/lib/ceremony.py`, `TapyrusSetupCeremony`) rather than duplicating
  `_run_setup`/`extract_vss_for`/`require_executable` across scripts.
- **Reorg mechanic**: forced via federation rotation (a second, disjoint signer set
  signs off on the handoff via `--xfield`), not a network partition -- a genuine
  two-sided fork built by two isolated groups independently threshold-signing their
  own blocks from a common tip, then reconnected.
- **Core network topology**: enforced entirely via `-connect=`, all 7 nodes on one
  flat Docker network (not segmented per edge), so a planned future adversary-node
  extension (connecting P2P to two first-layer nodes) remains buildable without
  rework.
- **`tapyrus-seeder` is included** for its own integration coverage even though
  nothing in the v1 scenario actually depends on it for peer discovery (container DNS
  already handles that) -- worth exercising given the four real bugs found.
- **Secrets scope**: this repo only ever generates local dev secrets
  (`generate_dev_secrets.py`); it never provisions real GitHub secrets. The only
  actual CI secret needed is the Slack webhook URL.
- **`pull_request`/`push` smoke trigger, scoped to this repo's own changes**: added so
  a change to `scripts/**`, `docker/**`, `config/**`, or the workflow itself is
  validated before merge, not just discovered on the following Sunday's scheduled run.
  This is deliberately read as a different concern from `weekly-integration-test-plan.md`
  section 6's "not on every PR" non-goal: that non-goal is about running the full-scale
  scenario against every PR opened on `tapyrus-core`/`tapyrus-signer` (cost/runtime
  prohibitive), not about validating this repo's own, rarely-changing PRs. The smoke
  trigger runs the identical job at reduced scale (`chain_height_before_reorg`,
  `reorg_loser_blocks`/`reorg_winner_margin`, `tx_total_count`, `tx_interval_seconds`,
  `rotation_height_offset` all drop to smaller fallbacks, keyed off `github.event_name`
  since `pull_request`/`push` runs have no `inputs` context) and a shorter
  `timeout-minutes` (60 vs. 360). Currently these scaled-down variables don't change
  anything observable, since the steps that consume them (per-node tx, reorg, rotation)
  are still TODO placeholders -- wired in now so the smoke run is already fast once
  Milestone 3/4 lands, rather than needing a second pass then. Not yet verified against
  a real GitHub Actions run (this repo's local testing can't exercise `on:` trigger
  behavior) -- worth confirming `inputs.<name> || (...)` evaluates as expected on a
  `pull_request` event on the first real PR.
- **`workflow_dispatch` inputs have no `default:`**: every default used to be written
  twice -- once as the input's `default:`, once as the `env:` block's `|| 'literal'`
  fallback -- a real drift risk if one got updated without the other (flagged in PR
  review). `workflow_dispatch.inputs.*.default` can't hold an expression, and
  `schedule`/`pull_request`/`push` runs have no `inputs` context at all, so there was no
  way to make `env:` read the input's declared default programmatically -- the literal
  had to live somewhere outside the input either way. Chose to remove it from the input
  side rather than the env side: `env:` keeps `inputs.x || 'literal'` as the one place
  each default is written, and every input's `description:` now states its default in
  words instead. A manual dispatch run left blank resolves to the exact same value
  `schedule` uses -- the only visible change is the dispatch form showing blank fields
  instead of pre-filled ones.

## Full local end-to-end verification (Tier 3 test)

Verified for real, locally, with a fresh checkout, real Rust toolchain, real Docker
builds, and real containers (not simulated):

- Real checkout of all three repos (with the submodule fix above)
- Real `tapyrus-setup`/`tapyrus-signerd` build (toolchain + GMP fixes applied by hand)
- Real `tapyrus-core` and `tapyrus-signer` Docker images built successfully
- `tapyrus-seeder`'s build reproduced its documented failure exactly (expected, not a
  regression)
- Real 3-signer ceremony converged on one genuine aggpubkey
- Real genesis signed by that ceremony, loaded and validated by real `tapyrusd`
  (`Genesis Block [...] Loaded successfully`)
- Real 7-node + redis compose stack came up; `wait_for_topology.py` correctly
  converged against live containers (cross-verified independently via raw `curl`)
- Real signer network produced real threshold-signed blocks to height 2, zero
  `InvalidBlock` errors (`round-duration=60`), confirmed P2P relay to signer-less
  `core-7`
- Log collection and teardown steps both verified
