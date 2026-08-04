# Scripts reference

What each script/config file under `scripts/` and `config/` does: usage, arguments,
output, and known limitations, in the order the CI workflow calls them. See the root
[`README.md`](../README.md) for how these fit into the overall CI flow,
[`weekly-integration-test-plan.md`](weekly-integration-test-plan.md) section 4a for
the full ceremony design these scripts implement, and [`work-done.md`](work-done.md)
for known issues, gotchas, and design-decision rationale.

All scripts are Python 3 (stdlib only, no third-party dependencies), executable
directly (`./scripts/<name>.py ...`) or via `python3 scripts/<name>.py ...`. Every
script that does subprocess or network I/O is `asyncio`-based, and runs independent
operations concurrently (e.g. checking out 3 repos, or polling 7 nodes) rather than
looping over them one at a time -- see [`README.md`](../README.md)'s "Developer
notes" for the convention. `assemble_signer_configs.py` is the one exception: it's
pure local file I/O with nothing to overlap, so it stays a plain synchronous script.

## `scripts/lib/`

Shared code the scripts below import rather than duplicate:

- `lib/log.py` -- the uniform, leveled, timestamped logger (`log.step/info/warn/error`)
  every script uses for its own narration. Separate from container log collection (the
  workflow's "Collect logs" step, which pulls each container's own log via
  `docker logs`) -- this is for the scripts' own output, so every step reads the same
  way regardless of which script produced it. Stays plain synchronous `print()` --
  writing a short line to stdout/stderr isn't I/O worth overlapping with anything, so
  it doesn't follow the async convention below.
- `lib/ceremony.py` -- the `TapyrusSetupCeremony` base class (`_run_setup`, shared by
  `generate_dev_secrets.py`'s `AggpubkeyCeremony` and `sign_genesis.py`'s
  `GenesisSigningCeremony`), the standalone `extract_vss_for()` helper (also used
  directly by `assemble_signer_configs.py`, which doesn't run `tapyrus-setup` itself),
  and `require_executable()`. `_run_setup` is `async def`, via
  `asyncio.create_subprocess_exec`.
- `lib/rpc.py` -- `CoreRpcClient`, a minimal Tapyrus Core JSON-RPC client (stdlib
  `urllib`, no `requests` dependency) used by `collect_coinbase_addresses.py`,
  `wait_for_topology.py`, `generate_traffic.py`, `simulate_reorg.py`,
  `simulate_federation_change.py`, `simulate_maxblocksize_change.py`, and
  `scripts/container/node_orchestrator.py` (bind-mounted into each core-* container
  -- confirmed stdlib-only, so it runs there unchanged, see that section below).
  `call()` is `async def`; since stdlib has no async HTTP client, it wraps the
  blocking `urllib` call in `asyncio.to_thread` so multiple calls can still run
  concurrently. Raises `RpcUnreachable` (connection refused, timeout -- treat as "not
  ready yet") separately from `RpcError` (the node answered with a JSON-RPC error).
- `lib/orchestrator_control.py` -- `pause_node_orchestrators()`/
  `resume_node_orchestrators()`, a shared pause file every core-* node's
  `node_orchestrator.py` checks before any chaos action. Calls nest (a depth
  counter, not a plain touch/unlink), so an inner pause/resume pair doesn't
  prematurely resume chaos while an outer caller still needs it paused. Used by
  `simulate_reorg.py`, `simulate_federation_change.py`, `simulate_maxblocksize_change.py`,
  and `generate_traffic.py` to protect their own precise node up/down assumptions --
  see `work-done.md`.
- `lib/compose.py` -- `start_nodes()`/`stop_nodes()`/`bring_up()`/`recreate_fresh()`,
  thin wrappers around `docker compose <subcommand> <service names>` (run from
  `docker/`), used by `simulate_reorg.py` and `simulate_federation_change.py`. Every
  function takes explicit service names rather than a fixed/implied set -- each
  caller already knows exactly which ones it means at a given point in its recipe.
  Raises `ComposeError` on a non-zero exit.

## `config/repos.py`

Default checkout targets for the three repos this test spans, read by
`checkout_repos.py`. Each repo's `*_REF` is independently configurable per CI run --
see the root `README.md`'s variable table (`core_repo_ref` / `signer_repo_ref` /
`seeder_repo_ref`). `ReposConfig` resolves each field via `os.environ.get(name,
default)` when constructed: if the CI workflow has already exported the variable (via
its job-level `env:` block, itself driven by the matching `workflow_dispatch` input),
that value wins; otherwise the default here applies -- so this file's defaults are
also what a schedule-triggered run (no `inputs` context) and any local/manual
invocation fall back to.

- `SIGNER_REPO_URL` / `SIGNER_REPO_REF` -- default
  `https://github.com/chaintope/tapyrus-signer.git` @ `master`, which has the full
  `tapyrus-setup` ceremony (createkey/createnodevss/aggregate/genesis-sign), the
  federation-change/rotation ceremony (`--xfield` sign/computesig, multi-entry
  `federations.toml`), and `federations.toml` live-reload.
- `CORE_REPO_URL` / `CORE_REPO_REF` -- default
  `https://github.com/chaintope/tapyrus-core.git` @ `master`.
- `SEEDER_REPO_URL` / `SEEDER_REPO_REF` -- default
  `https://github.com/chaintope/tapyrus-seeder.git` @ `master`.

Any variable can be overridden by exporting it before running `checkout_repos.py`.

## `scripts/checkout_repos.py`

Clones (or updates) `tapyrus-signer`, `tapyrus-core`, and `tapyrus-seeder` into
`./workdir/` (gitignored), so the Docker builds and `cargo build` have something to
build from. Implemented as a `RepoCheckout` class; all three repos are checked out
**concurrently** via `asyncio.gather`, not one after another.

- **Usage**: `./scripts/checkout_repos.py` (no arguments -- reads `config/repos.py`,
  overridable via env vars as above).
- **Behavior**: for each repo, if `./workdir/<name>` already has a `.git` dir, fetches
  and checks out the configured ref (`FETCH_HEAD`) in place; otherwise does a shallow
  `--branch <ref> --single-branch --depth 1` clone, falling back to a full clone +
  `checkout <ref>` if the ref isn't a branch/tag (e.g. a raw commit sha) -- `--depth 1`
  only applies to the first attempt, so the full-clone fallback (which needs complete
  history to `checkout` an arbitrary sha) is unaffected.
- **Output**: `workdir/tapyrus-signer/`, `workdir/tapyrus-core/`, `workdir/tapyrus-seeder/`,
  each left at its requested ref (short sha + subject line printed for confirmation).
  Also runs `git submodule update --init --recursive` on each repo (needed for
  `tapyrus-core`'s vendored `secp256k1`; a harmless no-op for the other two).

## `scripts/generate_dev_secrets.py`

Runs the real `tapyrus-setup` federation-setup ceremony (steps 1-3 of the ceremony:
`createkey` -> `createnodevss` -> `aggregate`) for a throwaway signer set. Produces a
shared aggregated public key -- fully offline, no core node or Redis involved.
Implemented as `AggpubkeyCeremony(TapyrusSetupCeremony)`. Within each of the three
steps, all N signers' `tapyrus-setup` calls run **concurrently** (`asyncio.gather`) --
only the three steps themselves are sequential, since each depends on the previous
step's output (e.g. `aggregate` needs every signer's `createnodevss` result first).

- **Usage**: `./scripts/generate_dev_secrets.py <set-name> <node-count> <threshold> [tapyrus-setup-bin]`
- **Example**: `./scripts/generate_dev_secrets.py signer-set-a 3 2`
- `tapyrus-setup-bin` defaults to `workdir/tapyrus-signer/target/release/tapyrus-setup`
  (build it first with `cd workdir/tapyrus-signer && cargo build --release`).
- **Guards**: refuses to run if `threshold > node-count`, if the binary isn't
  found/executable (prints the build instructions), or if `secrets/<set-name>/` already
  has generated keys (remove the directory first to regenerate).
- **Output** (under `secrets/<set-name>/`, gitignored):
  - `pubkeys.txt` -- all N compressed pubkeys, one per line
  - `aggregated-public-key.txt` -- the converged aggpubkey, asserted identical across
    all N signers before being written
  - `node-<i>/signer.key` (mode 600), `node-<i>/public-key.txt`,
    `node-<i>/node-secret-share.hex` (mode 600) -- per-signer key material
  - `raw/nodevss_from_<i>.txt` -- raw `createnodevss` stdout per sender, kept for the
    genesis-signing step and for audit/debugging
- Called twice per full scenario run: once for `signer-set-a` (the initial federation)
  and again for `signer-set-b` (the rotation target, scenario step 6).

## `scripts/sign_genesis.py`

Runs the genesis-signing half of the ceremony (steps 4-7: `createblockvss` -> `sign` ->
`computesig`) against an unsigned genesis block hex, for a signer set
`generate_dev_secrets.py` already produced. Implemented as
`GenesisSigningCeremony(TapyrusSetupCeremony)`. Same concurrency shape as
`generate_dev_secrets.py`: all N signers' `createblockvss`/`sign` calls run
concurrently within each step; `computesig` is a single call (signer-0 only), so
there's nothing to parallelize there.

- **Usage**: `./scripts/sign_genesis.py <set-name> <unsigned-genesis-hex-file> <output-file> [tapyrus-setup-bin]`
- The unsigned genesis hex is produced separately by tapyrus-core's own tool (no
  private key -- nobody holds one for a threshold-signed federation):

  ```sh
  tapyrus-genesis -signblockpubkey=$(cat secrets/<set-name>/aggregated-public-key.txt) \
    > /tmp/unsigned-genesis.hex
  ```

  No `-dev` -- prod mode is the default when it's omitted, and genesis creation itself
  needs no network id either way (verified against a real container; see
  `work-done.md`).
- **Required env var**: `TAPYRUS_SETUP_THRESHOLD=<n>` -- the same threshold
  `generate_dev_secrets.py` was run with (not persisted anywhere else, so it must be
  passed again explicitly rather than guessed).
- **Requires** `secrets/<set-name>/` to already exist: `pubkeys.txt`,
  `node-<i>/{signer.key,node-secret-share.hex}`, `raw/nodevss_from_<i>.txt`.
- **Output**: the signed genesis block hex at `<output-file>` (e.g.
  `secrets/<set-name>/genesis.hex`) -- load it via `tapyrus/tapyrusd`'s
  `GENESIS_BLOCK_WITH_SIG` env var (see `docker/docker-compose.yml`); its
  `entrypoint.sh` writes it to `<datadir>/genesis.<network_id>` itself (see
  `render_tapyrus_conf.py` below and `work-done.md`).
- **Known limitation**: `computesig` always runs with `node-0`'s own key material
  (hardcoded) -- a fixed "designated signer", not configurable.

## `scripts/render_tapyrus_conf.py`

Renders `docker/generated/tapyrus.conf` (gitignored), the one conf file every
`core-*` service mounts (see `docker/docker-compose.yml`). Plain synchronous script --
just string formatting and a file write, nothing to overlap.

- **Usage**: `./scripts/render_tapyrus_conf.py [output-file]` (default:
  `docker/generated/tapyrus.conf`).
- **Reads env vars**: `NETWORK_ID` (default `1905960821`), `CORE_RPC_USER`/
  `CORE_RPC_PASS` (defaults `rpcuser`/`rpcpassword`) -- same job-level env vars the
  other steps already use.
- **Why this exists**: `tapyrus/tapyrusd`'s own `entrypoint.sh` only auto-generates a
  conf if none is mounted at `${CONF_DIR}/tapyrus.conf`, and the auto-generated one
  hardcodes `dev=1`/`[dev]`/`networkid=1905960821` -- so without this script,
  `NETWORK_ID` had nowhere to go and every node silently ran that one fixed dev
  network regardless of what it was set to (see `work-done.md`). This renders a
  prod-mode conf instead (paired with dropping `-dev` from `tapyrus-genesis`, see
  `sign_genesis.py` above), with `networkid=$NETWORK_ID` -- verified against a real
  container: `getblockchaininfo` reports `"mode": "prod"` and `"chain":
  "<NETWORK_ID>"`.
- **No `port=` (P2P listen port) override**: deliberately left at prod mode's own
  default (`2357`) rather than pinned to dev mode's old `12383` -- `-connect=` in
  `docker/docker-compose.yml` has no explicit port, so it resolves against the
  chain's *default* P2P port, not this conf's `port=`. Pinning `port=` here would
  desync from that and silently break every P2P edge (see `work-done.md`'s Design
  decisions).
- **`fallbackfee=0.0002`, `dbcache=64`, `maxorphantx=20`, `mempoolexpiry=2`** (hours):
  `fallbackfee` is required, not just tuning -- `tapyrusd`'s real default is disabled,
  and on the brand-new chain every run starts, `estimatesmartfee` never has enough
  history to let `sendtoaddress`/`issuetoken` succeed without it (see `work-done.md`'s
  Design decisions). The other three are memory/lifetime bounds sized down from their
  production-node defaults for a small, short-lived, closed-topology test chain (7
  fixed peers, a handful of blocks, a run measured in hours not weeks).
- **Known limitation**: not wired into the `seeder` service's `-i`/`-s` flags (still
  hardcoded to `1905960821` in `docker/docker-compose.yml`) -- the seeder is brought
  up and verified now (`scripts/verify_seeder.py`), this specific value just hasn't
  been made configurable yet.

## `scripts/verify_seeder.py`

Brings up the `seeder` service and the 7 core-* nodes itself, in two bring-up modes
in sequence, and verifies both end-to-end. Implemented as `SeederVerifier`. See
`doc/work-done.md`'s Lessons learnt for the full investigation behind why this
needed a custom Docker subnet, a reduced crawler thread count, and this two-phase
design rather than a single `dig` call.

- **Usage**: `./scripts/verify_seeder.py` (no arguments -- reads `CORE_RPC_USER` /
  `CORE_RPC_PASS` from the environment). Takes over bringing the 7 core-* nodes up
  entirely -- run it in place of a separate "bring up the 7 core nodes" step, before
  anything that depends on the fixed topology being stable (`wait_for_topology.py`,
  traffic generation, reorg, etc.), since it tears the nodes down and recreates them
  partway through.
- **Phase 1 -- addseeder mode, verifies organic peer discovery**: all 7 core-*
  nodes come up with only `-addseeder=<seed-hostname>`, no `-connect` at all. Waits
  for the seeder's own `-s`-driven crawl to converge (independent of the core nodes'
  own config), then restarts the 7 nodes so their one-shot DNS-seed lookup
  (`tapyrus-core` only runs this once, at process startup) runs again with real
  answers available, then polls `getpeerinfo` on all 7 until each has a peer whose
  `subver` isn't the seeder's own (`SEEDER_SUBVER`). Not a raw peer count, and not
  "grew from a captured baseline" either (two designs tried and rejected in the same
  investigation, see `work-done.md`) -- every core-* node here sits on the same /24,
  so tapyrus-core's own netgroup-diversity outbound logic caps each node's OWN
  dialing at ~1 real peer permanently, regardless of how many addresses `-addseeder`
  handed it; a node not also lucky enough to be picked as someone else's inbound
  target legitimately never exceeds 1. The seeder's own crawl connection is
  transient too, so it can't be relied on to pad a count either. A single real
  (non-seeder) peer is both the right signal and the achievable one.
- **Phase 2 -- connect mode, the fixed topology**: stops and removes the 7 core-*
  nodes and the seeder (a fresh seeder avoids phase 1's now-stale "good" set, keyed
  on IPs that no longer exist once the nodes are recreated), then brings both back up
  with the fixed `-connect` topology every other script in this repo runs against.
  Re-verifies the seeder converges on exactly the 6 nodes that actually listen here
  (`core-1a`/`1b`/`2a`/`2b`/`3a`/`3b`) -- `core-7` is seeded too, deliberately, as a
  real negative case (it never listens, see `docker-compose.yml`'s "CONNECT/LISTEN
  DESIGN" note) -- then brings up `seeder-test-node` (an 8th node with no `-connect`
  at all, only `-addseeder`, its container DNS resolver pointed at the seeder via
  `SEEDER_IP`) and confirms it auto-bootstraps onto one of the 6 listening nodes
  through the seeder alone.
- **Both `seeder-test-node` and `seeder` itself are stopped and removed once `run()`
  finishes, in a `finally` (guaranteed on failure too, not just success).** Left
  running, `seeder-test-node`'s persistent connection into whichever listener it
  discovered permanently mismatches `wait_for_topology.py`'s exact
  `getconnectioncount` check on that node, and during `simulate_reorg.py`'s
  isolated-build phases gives the supposedly-isolated group a real path to learn
  the other group's blocks via header relay -- silently defeating the
  strict-alternation the whole reorg recipe depends on. The seeder's own crawler
  adds flakiness on top even where `seeder-test-node` never attached (see
  `work-done.md`'s Lessons learnt on its held-open probe connections).
- **Output**: none written to disk -- verification results are logged; a mismatch
  at any check raises `SeederVerificationError` (non-zero exit).
- **Active in the workflow** as "Bring up tapyrus-seeder and verify it".

## `scripts/start_node_orchestrator.py`

Switches the 7 core-* nodes `verify_seeder.py`'s connect-mode phase left running
into "connect + orchestrator" mode: tears them down and recreates them with
`NODE_ORCHESTRATOR` set, so each one's `entrypoint_wrapper.sh` hands off to
`scripts/container/node_orchestrator.py` instead of running `tapyrusd` directly.
From this point on, every core-* node randomly stops/restarts/reindexes/invalidates
itself for the rest of the job -- see `doc/work-done.md` for the full design.

- **Usage**: `./scripts/start_node_orchestrator.py` (no arguments -- reuses
  `verify_seeder.py`'s own `CONNECT_MODE_ARGS`/`CORE_NODES`/`CORE_RPC_PORTS`
  directly rather than duplicating them).
- Only brings the chaos-supervised nodes up and confirms their RPC is reachable --
  does not itself wait for chaos or assert recovery. The rest of the workflow's own
  existing checks (`wait_for_topology.py`, every `generate_traffic.py` settle-height
  assertion, `simulate_reorg.py`'s `getchaintips` checks) are what actually prove the
  network keeps converging under chaos.
- **Output**: none written to disk -- a node whose RPC never comes back up raises
  `NodeOrchestratorStartupError` (non-zero exit).
- **Active in the workflow** as "Start node orchestrator", right after "Bring up
  tapyrus-seeder and verify it" -- that workflow step also appends
  `NODE_ORCHESTRATOR=1` to `$GITHUB_ENV` after this script succeeds, since setting it
  only within this script's own process isn't enough: `signer-0`/`signer-1`/
  `signer-2` and signer-set-b's services all `depends_on` a core-1a/2a/3a node, so
  any later `docker compose up` touching them needs to resolve the same value or
  Compose recreates that core node to match, reverting it to plain `tapyrusd` (see
  `work-done.md`).

## `scripts/container/`

Two files that run **inside** a core-* container, not on the CI host like
everything else in `scripts/` -- bind-mounted read-only (`../scripts:/app/scripts:ro`,
`docker/docker-compose.yml`) and only active once `start_node_orchestrator.py`
switches a node into orchestrator mode.

- **`entrypoint_wrapper.sh`**: every core-* service's `command:` now, in every
  bring-up mode. Reads the `NODE_ORCHESTRATOR` env var: unset, `exec tapyrusd "$@"`
  directly (identical to before this existed); set, hands off to
  `node_orchestrator.py` instead. Keeps `docker-compose.yml`'s own command line
  unchanged across modes instead of nesting a conditional inside the image's own
  `bash -c "$*"` entrypoint.
- **`node_orchestrator.py`**: launches `tapyrusd` as a child process and supervises
  it -- crash recovery (relaunches plain if it ever exits unexpectedly) always
  applies, for every node. core-1b/2b/3b/core-7 additionally run a randomized action
  loop for as long as the container runs, after a 360s startup grace period (see
  `work-done.md`). Every cycle shuffles and runs: a plain restart, a restart with
  this node's assigned flavor (`-reindex`/`-reindex-chainstate`/`-reloadxfield`,
  round-robin across all 7 nodes -- see `NODE_ORCHESTRATOR_FLAVOR` in
  `docker-compose.yml`), and an invalidateblock-then-reconsiderblock pair on the
  current tip -- guaranteeing every chaos node does at least one of each, in random
  order, with random delays between. A restarted node stays down until the rest of
  the network produces a couple more real blocks, bounded at 90s (`work-done.md`).
  core-1a/2a/3a (the 3 signers' own RPC targets) never join this loop at all -- see
  `work-done.md` for why. Every action also checks a shared pause file first
  (`scripts/lib/orchestrator_control.py`) -- see `simulate_reorg.py`/
  `simulate_federation_change.py`/`simulate_maxblocksize_change.py`/
  `generate_traffic.py` for where and why they pause it. `PRNG_SEED_BASE`
  (`github.run_id`) seeds each node's own RNG -- deterministic and reproducible per
  run, previously defined but unconsumed anywhere.
- Reuses `scripts/lib/rpc.py` and `scripts/lib/log.py` unchanged (both pure stdlib,
  confirmed safe in a minimal container) -- not `scripts/lib/compose.py`, which needs
  the `docker` CLI/socket and stays host-only.

## `scripts/wait_for_topology.py`

Polls every core-* node's RPC port until the 7-node topology (plan doc section 4b)
has fully converged -- each node's own `getconnectioncount` matches the pattern the
`-connect` graph is supposed to produce (the `1/2/1/2/1/2/3` sequence in the root
README's variable table), not just "all 7 containers are up". Implemented as
`TopologyWaiter`, using `lib/rpc.py`'s `CoreRpcClient`. All 7 nodes are polled
**concurrently** each attempt (`asyncio.gather`) -- one slow or unreachable node
doesn't delay checking the other 6.

- **Usage**: `./scripts/wait_for_topology.py [--timeout-seconds N] [--poll-interval-seconds N]`
  (defaults: 300s timeout, 5s poll interval).
- Node names, host-published RPC ports, and expected connection counts are hardcoded
  (see `docker/docker-compose.yml`'s port mappings) -- not configurable per-run, since
  the 7-node topology itself is wired 1:1 to exactly 3 signers (see the root
  `README.md`'s variable table).
- **`CORE_RPC_USER` / `CORE_RPC_PASS` env vars** (defaults `rpcuser` / `rpcpassword`)
  -- match the workflow's existing job-level env vars of the same names.
- A node that isn't reachable yet (connection refused -- still starting) is treated
  the same as a wrong connection count: "not converged yet", not a hard failure,
  until the timeout is hit.
- **On timeout**: logs every still-mismatched node's actual vs. expected state, then
  exits 1.
- Note: `docker/docker-compose.yml` doesn't yet have the compose-level healthcheck +
  `depends_on: condition: service_healthy` guidance from the same plan doc step --
  this script is a CI-level equivalent (arguably stronger, since it confirms real P2P
  peer counts rather than just RPC reachability), but the compose-file enhancement
  itself is separate, unstarted work.

## `scripts/collect_coinbase_addresses.py`

Collects one coinbase payout address from each first-layer core-* node's wallet
(`getnewaddress`), retrying each node until its RPC actually answers instead of
assuming `docker compose up -d` returning means the RPC server is ready. Also fails
loudly (non-zero exit) on an empty address instead of writing a blank line to the
output file.

- **Usage**: `./scripts/collect_coinbase_addresses.py <port> [<port> ...] [--output FILE] [--timeout-seconds N] [--poll-interval-seconds N]`
  (defaults: `./runtime/addrs.txt`, 120s per-node timeout, 3s poll interval).
- Ports run **concurrently** (`asyncio.gather`), same convention as the rest of
  `scripts/`. **`CORE_RPC_USER` / `CORE_RPC_PASS` env vars** match the workflow's
  existing job-level env vars.
- **Output**: one address per line, in the same order as the ports given.

## `scripts/assemble_signer_configs.py`

Writes each signer node's `federations.toml` + `tapyrus-signer.toml`, ready to
bind-mount into a `tapyrus-signerd` container at `/etc/tapyrus`. Implemented as
`SignerConfigAssembler` -- doesn't run `tapyrus-setup` at all, only reads the
`raw/*.txt` files a prior `generate_dev_secrets.py` run already produced (via the same
`extract_vss_for()` helper `generate_dev_secrets.py` and `sign_genesis.py` use). The
one script in `scripts/` that's plain synchronous, not `asyncio`-based -- pure local
file reads/writes, no subprocess or network I/O to run concurrently.

- **Usage**:

  ```sh
  ./scripts/assemble_signer_configs.py <set-name> <threshold> <core-rpc-hosts-file> \
    <core-rpc-port> <core-rpc-user> <core-rpc-pass> <redis-host> <redis-port> \
    <addresses-file> [output-dir]
  ```

- `<threshold>` is parsed as `int` (argparse `type=int`) -- a non-numeric value fails
  loudly at parse time instead of landing unvalidated in the generated TOML.
- `<core-rpc-hosts-file>`: one RPC host (container DNS name) per line, N lines -- each
  signer targets its **own** first-layer core node in the 7-node topology
  (`signer-0 -> core-1a`, `signer-1 -> core-2a`, `signer-2 -> core-3a`), not one shared
  host. Port/user/pass are shared across all core nodes.
- `<addresses-file>`: one coinbase payout address per line, N lines -- fetch real ones
  from each signer's first-layer node's wallet (`getnewaddress` RPC); doesn't need to
  correspond to the signer's own key.
- `[output-dir]` defaults to `./runtime/signers` -- writes
  `node-<i>/{federations.toml,tapyrus-signer.toml}`.
- **`ROUND_DURATION` env var** (default `10`) sets `[general] round-duration` in the
  generated `tapyrus-signer.toml` -- the block-pacing interval. Use `30` (the CI
  default, `ROUND_DURATION`/`round_duration_seconds`) for anything beyond quick local
  iteration: a short duration leaves too little slack in
  `round_limit_timer = round-duration + round-limit` for a round's signing
  communication to finish before the next round's messages arrive, producing transient
  `InvalidBlock` / "candidate block is not set" errors (see `work-done.md` -- both 30
  and 60 have been verified clean).
- **Output**: `federations.toml` (one `[[federation]]` entry, genesis-height,
  `aggregated-public-key`, this node's view of all N `node-vss` values) and
  `tapyrus-signer.toml` (`[general]`, `[signer]`, `[rpc]`, `[redis]` sections) per node.
- **`SignerConfigAssembler` itself only ever writes a single `[[federation]]` entry**
  -- a rotation needs a second entry appended with a `signature` field from the
  `--xfield` handoff. Handled by a subclass rather than editing this class directly:
  see `simulate_federation_change.py`'s `RotationSignerConfigAssembler` below.

## `scripts/generate_traffic.py`

Drives round-robin TPC + colored-coin traffic across all 7 nodes and confirms every
node's wallet balance (TPC and every colored type in play) after each block. Everything
is derived from a single `--round-count`: each round spans 3 block-heights (send, check,
settle), polled the same way across all 7 nodes -- see the script's own docstring for
the full design (funding the 4 nodes with no coinbase income, the per-node colored-type
assignment, and the balance-shortfall top-up mechanic). Implemented as `TrafficNode` +
`TrafficGenerator`.

- **Usage**: `./scripts/generate_traffic.py <round-count>`
- Requires the 7-node topology already converged (`wait_for_topology.py`) and
  signer-set-a producing blocks.
- **Output**: none written to disk -- verification results are logged; a settle-height
  balance mismatch raises `TrafficGenerationError` (non-zero exit) after all rounds run,
  listing every mismatch found, not just the first.
- Verified end-to-end against a real 7-node stack (`round_count=3`, all three
  colored-coin types, all 7 nodes' TPC -- coinbase-earning ones included).
- **Requires `fallbackfee` set** (see `render_tapyrus_conf.py` above, which sets
  it) -- without it, every funding/send/issuance call on a brand-new chain fails with
  `-4 Fee estimation failed`. This incident is why `run()` now tracks how many of the
  `round_count * 14` round actions actually succeeded and raises
  `TrafficGenerationError` if fewer than `MIN_SUCCESSFUL_ACTION_FRACTION` did, instead
  of relying solely on the settle-height ledger comparison -- a never-funded node's
  ledger and its actual balance both stay `0.0`, so that comparison alone can't tell
  "real activity, correctly tracked" apart from "total failure, trivially consistent."
  Re-verified live after the `fallbackfee` fix: zero `round TPC send skipped` / `round
  colored action failed` warnings across 2 full rounds, real balance changes
  throughout. See `doc/work-done.md`'s Lessons learnt for the full incident.
- **Active in the workflow**, right after "Bring up signers" -- uncommented along
  with `simulate_reorg.py` (which runs right after it) and their shared prerequisite,
  signer-set-a bring-up (rotation and max-block-size change are both active too, see
  their own sections below).
- **Every setup/verification RPC sequence is chaos-tolerant**, since this script runs
  continuously against the node orchestrator's background chaos (`work-done.md`):
  address collection, balance seeding, coinbase-rotation calibration, every block
  wait, and every balance verification all pause chaos for their own span
  (`scripts/lib/orchestrator_control.py`) *and* retry individual RPC calls on
  `RpcUnreachable` (`_call_with_retry`) rather than treating one node's momentary
  restart as a hard failure -- the pause file alone can't interrupt a restart already
  in flight the instant it lands. Coinbase-rotation calibration additionally retries
  the whole 3-height observation (up to 5 attempts) if a signer's turn gets skipped.
  Verified live against a real chaos-supervised 7-node stack: multiple full runs,
  every balance/color check across all 7 nodes matched the ledger, zero mismatches.
- **`core-1a`/`2a`/`3a`'s coinbase income is fully asserted too**, not excluded --
  observed directly. `_credit_coinbase_for_height` reads each height's real
  coinbase transaction and credits whichever of the 3 wallets actually has it,
  for every height since the last one credited (not just the latest observed,
  so two blocks landing between polls don't leave one uncredited). The reward
  itself is not a flat amount -- subsidy plus whatever transaction fees that
  block happened to include, confirmed live.

## `scripts/simulate_reorg.py`

Drives a genuine two-sided reorg via strict alternation: only one group's core nodes
are ever up and building at a time (never both, until the final reconnect), so
neither side can influence or observe the other's blocks while forking. Confirms the
losing group's fork shows up as a real `valid-fork` tip via `getchaintips`, not just
that the height advanced. Implemented as `ReorgSimulator`, following
`generate_traffic.py`'s class-based/asyncio pattern. The first script to also drive
`docker compose` itself (stop/start/force-recreate specific services), not just RPC --
via `scripts/lib/compose.py`'s shared helpers.

- **Usage**: `./scripts/simulate_reorg.py` (no arguments -- reads
  `CHAIN_HEIGHT_BEFORE_REORG` / `REORG_LENGTH` / `CORE_RPC_USER` / `CORE_RPC_PASS` /
  `ROUND_DURATION` from the environment, same job-level env vars the workflow already
  sets).
- Requires the 7-node topology and signer-set-a already up and converged (same
  precondition as `generate_traffic.py`, which this step runs right after in the
  workflow).
- **The recipe, in order**: build a common baseline (all 7 nodes) -- stop group A
  entirely, repoint all 3 signers to group B (`core-3a`), let it build `REORG_LENGTH`
  blocks completely alone -- stop group B, restart group A, reset redis fresh --
  repoint signers back to group A's default mapping, inject the canary transaction,
  let group A build its *own* `REORG_LENGTH` blocks (genuinely different from group
  B's, since it never saw group B's blocks or round-state) -- reconnect group B
  alongside group A (a real tie: same height, different tips) and confirm the tie
  alone doesn't cause a reorg -- repoint signers to group B one more time and extend
  its chain by 2 blocks, not 1 (`core-3a` produced group A's original tip itself, so
  it needs a second block to reclassify that tip as `valid-fork` instead of
  `valid-headers`), waiting for **all 7 nodes** (not just group B) to reach that
  height, since group A can only satisfy that by actually reorging --
  confirm convergence via `getchaintips` on all 7 nodes -- verify the canary
  survived.
- **`CHAIN_HEIGHT_BEFORE_REORG` is a floor, not an assumed starting point**: the
  workflow always sets it to `TX_ROUND_COUNT + 2` (see the root `README.md`'s
  variable table), tying it to whatever `generate_traffic.py` produces first in the
  same job. `_build_baseline` waits until height >= that floor, then captures
  whatever height was *actually* reached (typically well past the floor) as the real
  reference point for both forks' target height -- using the literal floor value
  instead would let already-elapsed height silently satisfy that target too,
  producing a no-op "reorg" (group A/B "build their fork" without any new blocks).
- **Why not `tapyrus-core`'s `generatetoaddress` RPC** (instant block mining): it needs
  the aggregate *private* key as a parameter, not available here by design (threshold-
  shared across the 3 signers, no single party holds it in a real ceremony). Blocks can
  only come from the live `tapyrus-signerd` trio's normal round-robin process at
  `ROUND_DURATION` cadence -- this script is inherently as slow as that process.
- **Signers get repointed three times, not once** (to group B, back to group A's
  default mapping, then to group B again) -- `_repoint_signers` is a shared helper,
  reusing `assemble_signer_configs.py`'s `SignerConfigAssembler` directly (not a
  subprocess) each time. Pubkeys come from `secrets/<set-name>/pubkeys.txt` (persists
  for the whole job); each node's `to-address` is re-read from its own already-written
  `runtime/signers/node-<i>/tapyrus-signer.toml` (via stdlib `tomllib`) rather than
  depending on the original "Collect coinbase addresses" step's `/tmp` file still
  being around. The reconnect step reuses `wait_for_topology.py`'s `TopologyWaiter`
  directly, same reasoning.
- **`core-7` needs a different convergence check than `core-3a`/`core-3b`**:
  `core-3a`/`core-3b` are only P2P-connected within group B (see
  `docker-compose.yml`'s topology), so each should show a single active
  `getchaintips` tip. `core-7`, by design, is P2P-connected to *all three*
  second-layer nodes, bridging both groups -- so it legitimately learns group A's
  abandoned fork via header relay even though it never builds/extends it, showing a
  second tip at group A's height with status `valid-headers` (headers relayed and
  known, not necessarily fully-validated as a real candidate), not `valid-fork` like
  the ex-group-A nodes, since group B's own chain was always at least as long by the
  time `core-7` reconnects to it. `_confirm_convergence` only asserts `core-7`'s
  *active* tip matches group B, not its total tip count.
- **Output**: none written to disk -- verification results are logged; a mismatch at
  any step raises `ReorgError` (non-zero exit); failures across all 7 nodes are
  collected and reported together, not just the first one hit.
- **Verified live, twice, against a real 7-node + signer-set-a stack** (smoke scale,
  `REORG_LENGTH=2`, `ROUND_DURATION=60`): both runs produced genuinely different
  group A/group B forks (distinct tip hashes throughout, unlike the old design) and
  converged correctly -- 6/7 nodes exactly matching expectations plus the `core-7`
  nuance above on the first attempt; the second run (re-run against the same
  long-lived containers without tearing down first) correctly flagged 3 leftover tips
  on the ex-group-A nodes from the *first* run's still-present fork, which is accurate
  reporting of real on-chain state, not a script bug -- a real CI run always starts
  from fresh containers and wouldn't hit this.
- **Active in the workflow**, right after "Generate traffic (before reorg)".

## `scripts/simulate_federation_change.py`

Drives a genuine aggpubkey rotation: signer-set-b runs its own offline ceremony (one
member's identity deliberately reused from signer-set-a's node-0, so it stays a valid
signer straight through the handoff), signer-set-a signs off on the handoff via
`tapyrus-setup`'s `--xfield sign/computesig` flow, `federations.toml` is regenerated
for all 5 involved signer nodes, signer-set-b's two genuinely new identities
(`signer-b-1`/`signer-b-2`) are brought up alongside the still-running signer-set-a,
and the script waits for the scheduled height to confirm the new aggpubkey took
effect. Implemented as `FederationChangeSimulator`. See the script's own module
docstring for the full design (xfield encoding, `federations.toml` membership rules,
the shared node-0 identity, live-reload mechanics, the shared-redis open question).

- **Usage**: `./scripts/simulate_federation_change.py` (no arguments -- reads
  `CORE_RPC_USER` / `CORE_RPC_PASS` / `ROUND_DURATION` / `FEDERATION_CHANGE_HEIGHT`
  from the environment).
- Requires signer-set-a already generated and signing (same precondition as
  `simulate_reorg.py`).
- **signer-0/1/2 are never stopped or restarted** -- their `federations.toml` is
  live-reloaded in place (write-to-temp-then-rename, matching
  `federation_watcher.rs`'s own requirement for a complete-file write).
- **Confirmed via RPC, not container/signer logs**: `_confirm_rotation_via_rpc`
  checks `getblockchaininfo`'s `aggregatePubkeys` array on all 7 core nodes for the
  new pubkey at the scheduled height -- core's own consensus-level record, not
  anything the script or a signer log merely claims happened.
- **Output**: none written to disk -- verification results are logged; a mismatch
  raises `CeremonyError` (non-zero exit).
- **Active in the workflow** as "Federation change (aggpubkey rotation)" -- uncommented
  after a confirmed real run, same bar `simulate_reorg.py` cleared before it was wired
  in.

## `scripts/simulate_maxblocksize_change.py`

Drives a genuine max-block-size (xfield) change: the currently-active federation
(signer-set-b, after `simulate_federation_change.py`'s rotation) signs off on a new
max-block-size via the same `--xfield sign/computesig` flow, `federations.toml` gains
a new entry for it on signer-set-b's 3 active members, live-reloaded in place, and the
script waits for the scheduled height to confirm the new value took effect.
Implemented as `MaxBlockSizeChangeSimulator`, reusing `simulate_federation_change.py`'s
`XFieldSignoffCeremony` directly (it's generic over which xfield gets signed).

- **Usage**: `./scripts/simulate_maxblocksize_change.py` (no arguments -- reads
  `CORE_RPC_USER` / `CORE_RPC_PASS` / `ROUND_DURATION` / `MAX_BLOCK_SIZE_HEIGHT` /
  `MAX_BLOCK_SIZE_NEW` from the environment).
- **Requires signer-set-b already active** -- `simulate_federation_change.py`'s
  rotation must have already been confirmed; this step signs off using signer-set-b's
  own key material, not signer-set-a's.
- **Simpler than the rotation script**: no new signer identities and no membership
  change -- all 3 currently-active members (node-0, `signer-b-1`, `signer-b-2`) sign
  off on and remain full members of the new entry too, so no new containers are
  brought up.
- **`--xfield` encoding differs from AggregatePublicKey's**: `MaxBlockSize(u32)` has a
  fixed-size payload -- type tag `0x02` followed by the raw 4-byte little-endian u32,
  no CompactSize length prefix (unlike the pubkey's variable-length payload).
  Verified against rust-tapyrus v0.4.8's own `xfield_max_block_size_test` vector. See
  `_maxblocksize_xfield_hex`.
- **Confirmed via RPC**: `_confirm_change_via_rpc` checks `getblockchaininfo`'s
  `maxBlockSizes` array on all 7 core nodes for the new value at the scheduled height
  -- note the key is a decimal string (`XFieldMaxBlockSize::ToString()` =
  `std::to_string(data)`), not hex like `aggregatePubkeys`.
- **Active in the workflow** as "Max block size change" -- uncommented after a
  confirmed real run.

## `docker/docker-compose.yml`

Not a script, but the other piece every scenario run depends on -- the 7-core-node +
`redis` + 3-signer + `seeder` stack described in `weekly-integration-test-plan.md`
section 4b, on a custom subnet (`51.51.51.0/24`, not Docker's own default bridge
range -- see `doc/work-done.md`'s Lessons learnt for why that's required at all).
Each core-* node's extra P2P flags come from its own `CORE_<NAME>_ARGS` env var, not
a fixed `command:` -- `scripts/verify_seeder.py` sets these to bring the 7 nodes up
in two modes in the same run (addseeder-only, then the fixed `-connect` topology)
before anything else in the workflow depends on them being stable. Also defines
`seeder-test-node`, a throwaway 8th node brought up only by that same script.
