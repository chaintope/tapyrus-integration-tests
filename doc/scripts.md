# Scripts reference

What each script/config file under `scripts/` and `config/` does: usage, arguments,
output, and known limitations, in the order the CI workflow calls them. See the root
[`README.md`](../README.md) for how these fit into the overall CI flow,
[`weekly-integration-test-plan.md`](weekly-integration-test-plan.md) section 4a for
the full ceremony design these scripts implement, and [`work-done.md`](work-done.md)
for gotchas found the hard way, bugs fixed, and design-decision rationale.

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
  `urllib`, no `requests` dependency) used by `collect_coinbase_addresses.py` and
  `wait_for_topology.py` today, and intended for the per-node tx/lifecycle orchestrator
  and rotation-confirmation step once those are built. `call()` is `async def`; since
  stdlib has no async HTTP client, it wraps the blocking `urllib` call in
  `asyncio.to_thread` so multiple
  calls can still run concurrently. Raises `RpcUnreachable` (connection refused,
  timeout -- treat as "not ready yet") separately from `RpcError` (the node answered
  with a JSON-RPC error).

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

- `SIGNER_REPO_URL` / `SIGNER_REPO_REF` -- **TEMPORARY default**
  `https://github.com/Naviabheeman/tapyrus-signer.git` @ `master-build-fix`, tracking
  unmerged [`chaintope/tapyrus-signer#172`](https://github.com/chaintope/tapyrus-signer/pull/172)
  (the `gmp-mpfr-sys` self-test skip that lets `cargo build --release` succeed --
  `chaintope/tapyrus-signer`'s own `master` doesn't build out of the box, see
  `work-done.md`). Switch back to `https://github.com/chaintope/tapyrus-signer.git` @
  `master` once #172 merges. For federation-change/rotation testing (`--xfield`
  sign/computesig, multi-entry `federations.toml`), override both to
  `https://github.com/Naviabheeman/tapyrus-signer.git` @ `163_federationChangeToml`
  instead, e.g.:
  `SIGNER_REPO_URL=https://github.com/Naviabheeman/tapyrus-signer.git SIGNER_REPO_REF=163_federationChangeToml`.
- `CORE_REPO_URL` / `CORE_REPO_REF` -- default
  `https://github.com/chaintope/tapyrus-core.git` @ `master`.
- `SEEDER_REPO_URL` / `SEEDER_REPO_REF` -- **TEMPORARY default**
  `https://github.com/Naviabheeman/tapyrus-seeder.git` @ `docker-build-fix`, tracking
  unmerged [`chaintope/tapyrus-seeder#5`](https://github.com/chaintope/tapyrus-seeder/pull/5)
  (the four build/runtime bug fixes documented in `doc/work-done.md` --
  `chaintope/tapyrus-seeder`'s own `master` lacks them). Switch back to
  `https://github.com/chaintope/tapyrus-seeder.git` @ `master` once #5 merges.

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
- **Known limitation**: not wired into the `seeder` service's `-i`/`-s` flags (still
  hardcoded to `1905960821` in `docker/docker-compose.yml`) -- deliberately, since the
  seeder isn't brought up by any `docker compose up` invocation yet at all (see
  `project-plan.md` Milestone 4).

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
  generated `tapyrus-signer.toml` -- the block-pacing interval. Use `60` (the CI
  default, `ROUND_DURATION`/`round_duration_seconds`) for anything beyond quick local
  iteration: a short duration leaves too little slack in
  `round_limit_timer = round-duration + round-limit` for a round's signing
  communication to finish before the next round's messages arrive, producing transient
  `InvalidBlock` / "candidate block is not set" errors (see `work-done.md`).
- **Output**: `federations.toml` (one `[[federation]]` entry, genesis-height,
  `aggregated-public-key`, this node's view of all N `node-vss` values) and
  `tapyrus-signer.toml` (`[general]`, `[signer]`, `[rpc]`, `[redis]` sections) per node.
- **Known limitation**: only ever writes a single `[[federation]]` entry -- a rotation
  (scenario step 6) needs a second entry appended with a `signature` field from the
  `--xfield` handoff, which this script doesn't yet support (tracked in
  `doc/project-plan.md` Milestone 3).

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

## `docker/docker-compose.yml`

Not a script, but the other piece every scenario run depends on -- the 7-core-node +
`redis` + 3-signer + `seeder` stack described in `weekly-integration-test-plan.md`
section 4b. Brought up in two stages (core nodes first, to mint coinbase addresses;
signers second, once `assemble_signer_configs.py` has consumed those addresses) -- see
the root `README.md`'s CI step 5.
