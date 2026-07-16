# Scripts reference

What each script/config file under `scripts/` and `config/` does, in the order the CI
workflow calls them. See the root [`README.md`](../README.md) for how these fit into
the overall CI flow, and [`weekly-integration-test-plan.md`](weekly-integration-test-plan.md)
section 4a for the full ceremony design these scripts implement.

## `config/repos.env`

Default checkout targets for the three repos this test spans, sourced by
`checkout-repos.sh`. Each repo's `*_REF` is independently configurable per CI run --
see the root `README.md`'s variable table (`core_repo_ref` / `signer_repo_ref` /
`seeder_repo_ref`). The `${VAR:-default}` pattern below means: if the CI workflow has
already exported the variable (via its job-level `env:` block, itself driven by the
matching `workflow_dispatch` input), that value wins; otherwise the default here
applies -- so this file's defaults are also what a schedule-triggered run (no
`inputs` context) and any local/manual invocation fall back to.

- `SIGNER_REPO_URL` / `SIGNER_REPO_REF` -- default
  `https://github.com/Naviabheeman/tapyrus-signer.git` @ `163_federationChangeTomlSetup`,
  the branch with a working `tapyrus-setup` ceremony CLI. Override both to use the
  locally-patched `federation-setup-review` branch (toolchain + `gmp-mpfr-sys` fixes,
  see `legacy-readme.md` "Dockerfile notes"), e.g.:
  `SIGNER_REPO_URL=/path/to/local/tapyrus-signer SIGNER_REPO_REF=federation-setup-review`.
- `CORE_REPO_URL` / `CORE_REPO_REF` -- default
  `https://github.com/chaintope/tapyrus-core.git` @ `master`.
- `SEEDER_REPO_URL` / `SEEDER_REPO_REF` -- default
  `https://github.com/chaintope/tapyrus-seeder.git` @ `master`. **Note**: `master`
  does not include the four bug fixes documented in `doc/work-done.md` (build failure,
  sprintf buffer overflow, two data races) -- those only exist on a local, unpushed
  `docker-build-fix` branch today. Override `SEEDER_REPO_REF` (and `SEEDER_REPO_URL` if
  testing locally) once a fixed branch is pushed somewhere reachable.

Any variable can be overridden by exporting it before running `checkout-repos.sh`.

## `scripts/checkout-repos.sh`

Clones (or updates) `tapyrus-signer`, `tapyrus-core`, and `tapyrus-seeder` into
`./workdir/` (gitignored), so the Docker builds and `cargo build` have something to
build from.

- **Usage**: `./scripts/checkout-repos.sh` (no arguments -- reads `config/repos.env`,
  overridable via env vars as above).
- **Behavior**: for each repo, if `./workdir/<name>` already has a `.git` dir, fetches
  and checks out the configured ref (`FETCH_HEAD`) in place; otherwise does a shallow
  `--branch <ref> --single-branch` clone, falling back to a full clone + `checkout <ref>`
  if the ref isn't a branch/tag (e.g. a raw commit sha).
- **Output**: `workdir/tapyrus-signer/`, `workdir/tapyrus-core/`, `workdir/tapyrus-seeder/`,
  each left at its requested ref (short sha + subject line printed for confirmation).

## `scripts/generate-dev-secrets.sh`

Runs the real `tapyrus-setup` federation-setup ceremony (steps 1-3 of the ceremony:
`createkey` -> `createnodevss` -> `aggregate`) for a throwaway signer set. Produces a
shared aggregated public key -- fully offline, no core node or Redis involved.

- **Usage**: `./scripts/generate-dev-secrets.sh <set-name> <node-count> <threshold> [tapyrus-setup-bin]`
- **Example**: `./scripts/generate-dev-secrets.sh signer-set-a 3 2`
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
- **Gotcha baked in**: `createnodevss` output lines (`<receiver_pubkey>:<vss_hex>`) are
  sorted by receiver pubkey (BTreeMap iteration), not `--public-key` argument order --
  the script's `extract_vss_for()` helper matches by actual pubkey, never by line
  position, to avoid an opaque `InvalidSS` error.
- Called twice per full scenario run: once for `signer-set-a` (the initial federation)
  and again for `signer-set-b` (the rotation target, scenario step 6).

## `scripts/sign-genesis.sh`

Runs the genesis-signing half of the ceremony (steps 4-7: `createblockvss` -> `sign` ->
`computesig`) against an unsigned genesis block hex, for a signer set
`generate-dev-secrets.sh` already produced.

- **Usage**: `./scripts/sign-genesis.sh <set-name> <unsigned-genesis-hex-file> <output-file> [tapyrus-setup-bin]`
- The unsigned genesis hex is produced separately by tapyrus-core's own tool (no
  private key -- nobody holds one for a threshold-signed federation):

  ```sh
  tapyrus-genesis -dev -signblockpubkey=$(cat secrets/<set-name>/aggregated-public-key.txt) \
    > /tmp/unsigned-genesis.hex
  ```

- **Required env var**: `TAPYRUS_SETUP_THRESHOLD=<n>` -- the same threshold
  `generate-dev-secrets.sh` was run with (not persisted anywhere else, so it must be
  passed again explicitly rather than guessed).
- **Requires** `secrets/<set-name>/` to already exist: `pubkeys.txt`,
  `node-<i>/{signer.key,node-secret-share.hex}`, `raw/nodevss_from_<i>.txt`.
- **Output**: the signed genesis block hex at `<output-file>` (e.g.
  `secrets/<set-name>/genesis.hex`) -- copy it to `<tapyrus-core-datadir>/genesis.dat` to
  use it.
- **Gotcha baked in**: `computesig`'s `--sig`/`--block-vss`/`--node-vss` arrays must all
  be the full signer count (not just `threshold`) -- enforced by an `assert_eq!` in the
  source, so the script always passes all N signers' values even though only
  `threshold` are cryptographically necessary. `computesig` itself is always run by
  `node-0` (hardcoded -- see `weekly-integration-test-plan.md` section 4a, step 6, for
  why a "designated signer" is a v1 limitation, not an oversight).

## `scripts/assemble-signer-configs.sh`

Writes each signer node's `federations.toml` + `tapyrus-signer.toml`, ready to
bind-mount into a `tapyrus-signerd` container at `/etc/tapyrus`.

- **Usage**:

  ```sh
  ./scripts/assemble-signer-configs.sh <set-name> <threshold> <core-rpc-hosts-file> \
    <core-rpc-port> <core-rpc-user> <core-rpc-pass> <redis-host> <redis-port> \
    <addresses-file> [output-dir]
  ```

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

## `docker/docker-compose.yml`

Not a script, but the other piece every scenario run depends on -- the 7-core-node +
`redis` + 3-signer + `seeder` stack described in `weekly-integration-test-plan.md`
section 4b. Brought up in two stages (core nodes first, to mint coinbase addresses;
signers second, once `assemble-signer-configs.sh` has consumed those addresses) -- see
the root `README.md`'s CI step 5 and the file's own inline comments for the
`-connect`/`-listen` topology design and the `command:` override gotcha specific to the
`tapyrus/tapyrusd` image's entrypoint.
