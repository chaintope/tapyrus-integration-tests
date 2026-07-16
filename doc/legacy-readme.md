# tapyrus-integration-tests -- original spike README (moved)

> **This is the original project README**, kept here for history. It was written during
> the local, pre-CI spike phase and documents everything manually verified before the
> workflow was wired up. The root `README.md` now covers the CI workflow itself
> (step summary + configurable variables); `doc/project-plan.md` tracks what's left to
> do; `doc/scripts.md` documents each script. This file's content is otherwise
> unchanged from the original root README.

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
