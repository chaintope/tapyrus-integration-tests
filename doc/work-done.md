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

- **`tapyrus-seeder`'s `master` fails to build** -- reproduced for real (local full
  E2E test): fails on bug #1 below (Alpine g++ rejecting a designated-initializer in
  `dns.cpp`). All four fixes are up as
  [`chaintope/tapyrus-seeder#5`](https://github.com/chaintope/tapyrus-seeder/pull/5),
  not yet merged. **`SEEDER_REPO_REF`/`SEEDER_REPO_URL` default to the PR's own
  branch** (`Naviabheeman/tapyrus-seeder` @ `docker-build-fix`) so at least one CI
  trigger can go green before #5 merges -- switch back to `chaintope/tapyrus-seeder` @
  `master` once it does.
- **GitHub-hosted `ubuntu-latest` runner's CPU/disk sufficiency is unconfirmed** for 7
  core nodes + 3 signers + redis + seeder running concurrently -- may need
  self-hosted.
- **`docker/docker-compose.yml` has no compose-level healthchecks** /
  `depends_on: condition: service_healthy` yet (plan doc section 3 step 6 guidance).
  `scripts/wait_for_topology.py` is a CI-level equivalent -- arguably stronger, since
  it confirms real P2P peer counts rather than just RPC reachability -- but the
  compose-file enhancement itself is separate, unstarted work.
- **`163_federationChangeToml` (Naviabheeman fork) is only actually required for
  federation change/rotation** (the `--xfield` sign/computesig flow, multi-entry
  `federations.toml`) -- `chaintope/tapyrus-signer`'s own `master` already has the base
  ceremony (createkey/createnodevss/aggregate/genesis-signing, confirmed nearly
  identical), and now builds out of the box too
  ([`chaintope/tapyrus-signer#172`](https://github.com/chaintope/tapyrus-signer/pull/172)
  merged -- confirmed directly against a fresh `chaintope/master` fetch, not just PR
  metadata: its new tip is that fix commit, with `gmp-mpfr-sys`'s `c-no-tests` feature
  present in `Cargo.toml`, and `cargo build --release` verified clean against it).
  Override `SIGNER_REPO_URL`/`SIGNER_REPO_REF` to the `Naviabheeman` fork's
  `163_federationChangeToml` branch when testing rotation (Milestone 3/4's rotation
  items).
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
- **`scripts/generate_traffic.py`'s two remaining hardcoded assumptions worth
  revisiting eventually**: `FUNDING_AMOUNT_TPC`/`TOKEN_ISSUE_AMOUNT`/etc. (module
  constants) were picked conservatively and worked fine at `round_count=2` in the local
  E2E run, but haven't been stress-tested at a larger round count where the
  balance-shortfall top-up mechanic would trigger much more often. Also, `_all_colors()`
  only knows about colors this script's own run issued -- rerunning it against a stack
  that already has colors from a previous run (as happened once during local testing)
  leaves those older colors untouched and unverified, which is fine (they're just not
  this run's concern) but worth knowing if wallet balances look richer than expected.

## Lessons learnt (bugs found and fixed, gotchas)

- **`scripts/generate_traffic.py` verified end-to-end against a real 7-node stack**
  (`round_count=2`, all three colored-coin types, real `getconnectioncount`-converged
  topology) -- confirmed the three RPC assumptions flagged when the script was first
  written, and found three additional real bugs along the way, all fixed:
  1. **The colored-address round-trip works as read from source**: a receiver calling
     `getnewaddress("", colorHex)` to mint a receiving address for a specific color,
     handed back to the sender for `transfertoken`, worked cleanly with no
     timing/ordering issues -- confirmed via correct round-robin routing observed live
     (e.g. `core-7`'s issued color landing in exactly the node its round-robin offset
     predicted).
  2. **`issuetoken`'s NON_REISSUABLE/NFT path does accept an unconfirmed self-send
     input** -- `_seed_plain_utxo`'s immediate (no-wait) self-send-then-issue worked for
     every node that hit that path, confirming the source reading (explicit UTXO
     selection via `coin_control.Select(out)` only checks `ISMINE_SPENDABLE`, no
     confirmation-depth check).
  3. **`gettransaction`'s fee is NOT a top-level field, and has two different shapes**
     depending on transaction type (confirmed directly against a live node's JSON, not
     guessed): a plain TPC send nests `"fee"` inside its own `category="send"` detail
     entry; a transaction that also moves a colored output puts the fee in a *separate*
     detail entry tagged `category="fee"` instead, and the send/receive entries in that
     case carry no `"fee"` key of their own. The first version of `_apply_fee` only
     handled the second shape, so every plain TPC round-robin send was silently
     recording a fee of 0 -- invisible at `round_count=2` until the drift accumulated
     enough to fail the ledger assertion (see below for how this was caught). Fixed by
     checking both shapes.
  4. **Funding-phase timing: waiting for one more block isn't enough.** The original
     design waited for a single new block before funding the 4 non-earning nodes from
     `core-1a`/`2a`/`3a`'s coinbase. But with 3 signers rotating as block proposer, one
     new block only credits *one* of those three -- the other two can still have zero
     balance, causing `sendtoaddress` to fail with HTTP 500 (confirmed live: this
     produced real funding failures on the first attempt at this fix). Fixed by polling
     each funding source's actual `getbalance` instead of assuming a fixed block count
     -- `_wait_for_funding_source_balances`.
  5. **`core-1a`/`2a`/`3a`'s TPC balance can't be predicted by this script's own
     ledger, and that's expected, not a bug**: these 3 nodes are each credited a fresh
     50 TPC `"generate"`-category transaction (confirmed via `listtransactions`) every
     time they propose a block as that round's federation master -- entirely
     independent of anything `generate_traffic.py` does, and not something it can
     predict in advance (block proposer rotation is internal to `tapyrus-signerd`).
     Excluded these 3 nodes' TPC from the exact-ledger assertion; their colored
     balances (never coinbase-derived) stay fully asserted, and their TPC is still
     logged for visibility.
  6. **Re-running the script against a stack that still has state from a previous run
     produces a real but misleading mismatch**: doing this during testing left the 4
     non-earning nodes' actual on-chain balance about 1.0 TPC higher than the second
     run's from-zero ledger expected (the first run's leftover funding balance).
     Not a script bug -- CI only ever runs this once per freshly-brought-up stack --
     but worth knowing if re-running locally without tearing the stack down first.

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
- **`tapyrus-seeder`'s four real upstream bugs**, found and fixed in
  [`chaintope/tapyrus-seeder#5`](https://github.com/chaintope/tapyrus-seeder/pull/5),
  all confirmed live:
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
- **`RPC_IN_WARMUP` (-28) is a real, common state to retry through, not just
  connection-refused/timeout** -- fixing `lib/rpc.py`'s `HTTPError`->`RpcError`
  misclassification (see the Design decisions entry above) accidentally regressed
  this: `tapyrusd` serves `-28` as HTTP 500 with a JSON-RPC error body
  (`JSONRPCError` -> `JSONErrorReply`, confirmed by reading `src/rpc/protocol.cpp` /
  `src/httprpc.cpp` in a real `tapyrus-core` checkout), which is exactly the readiness
  window right after `docker compose up` -- so classifying every `HTTPError` as a hard
  `RpcError` killed the retry loop instantly on that window instead of polling through
  it, a regression the previous fix's own review request explicitly asked to avoid.
  Confirmed live against a real container racing the actual warmup window (not
  simulated): 30 consecutive real `-28` responses (`"Verifying wallet(s)...",
  "Loading block index..."`), all correctly retried, then a clean success. Fixed by
  parsing the `HTTPError`'s JSON body and treating `error.code == -28` as retryable
  (`RpcUnreachable`) same as connection-refused, while still raising `RpcError` (now
  with the JSON-RPC error code/message included, not just the bare HTTP status) for
  every other HTTP error. A bad-credentials 401 has **no response body at all**
  (`HTTPReq_JSONRPC`'s auth-failure path calls `WriteReply` with no body argument) --
  confirmed this doesn't crash the JSON parse, verified live against a real container
  too.

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

**Scripted as `scripts/simulate_reorg.py`, re-verified end-to-end via the script (not
by hand) at smoke scale** (`CHAIN_HEIGHT_BEFORE_REORG=5 REORG_LOSER_BLOCKS=2
REORG_WINNER_MARGIN=1`, `ROUND_DURATION=60`): baseline reached height 5 with an
identical tip on all 7 nodes; group A split off and built a 2-block fork to height 7
(tip `04fe1a6d30e9...`); group B came back at the baseline tip, redis force-recreated
fresh, all 3 signers repointed to `core-3a` and restarted; group B built a 3-block
fork to height 8 (baseline + loser + margin, tip `f8087edd7ae4...`); group A
reconnected and the topology reconverged in 3 polling attempts (~10s); `getchaintips`
on every ex-group-A node (`core-1a`/`1b`/`2a`/`2b`) showed exactly two tips -- `active`
matching group B's tip, `valid-fork` matching group A's tip with `branchlen=2` exactly
-- and `core-3a` showed a single active tip. Total real runtime ~12.5 minutes (block
production can only come from the live signer round-robin process at `ROUND_DURATION`
cadence -- no instant-mining shortcut exists here, see `scripts.md`). First run of the
new script standalone, first try, no fixes needed -- the recipe's own precision (exact
heights, exact `branchlen`) left little room for the kind of subtle bugs earlier
scripts in this repo turned up on their first live run. A real bug *did* turn up one
step later, though, once this script started running after `generate_traffic.py`
instead of alone -- see the next entry.

- **`CHAIN_HEIGHT_BEFORE_REORG` must be a floor, not an assumed starting point --
  found the moment `simulate_reorg.py` ran after `generate_traffic.py` in the same
  job instead of standalone**: `_build_baseline` originally waited until height >=
  `CHAIN_HEIGHT_BEFORE_REORG`, then computed the loser/winner fork targets from that
  *same literal input value* (`baseline_height + loser_blocks`, etc.), not from
  whatever height was actually reached. Harmless in isolation (nothing else was
  producing blocks, so the literal value and the actual height matched). But wiring
  `generate_traffic.py` in first meant the chain was typically already well past that
  floor by the time the reorg step started -- confirmed live: `CHAIN_HEIGHT_BEFORE_REORG`
  computed as `TX_ROUND_COUNT + 2 = 4`, but traffic generation had already pushed the
  chain to height 11. Computing the losing-fork target from the literal `4` would have
  given target `6` -- already exceeded by the existing height 11 -- so `_build_losing_fork`
  would have returned immediately with group A having built *zero* new blocks past the
  split: a silent no-op "reorg" that would still report success. Fixed: `_wait_for_height`
  now returns the height actually reached (not just `None`/success), and `_build_baseline`
  reassigns `self._baseline_height` to that real value before the loser/winner targets
  are computed from it. Verified live: with the fix, the same scenario correctly logged
  `baseline confirmed: all 7 nodes at height 11` and computed the losing-fork target as
  `13` (11+2), and the full reorg completed correctly end-to-end afterward (`getchaintips`
  showing the exact expected `branchlen`/active/valid-fork pattern on every ex-group-A
  node). Lesson: a script verified correct in isolation can still hide an assumption
  ("nothing else changes the world between my start and my first check") that only
  breaks once it's composed with something else that changes that world first --
  worth deliberately testing new orchestration scripts back-to-back with whatever
  will really precede them in CI, not just standalone.

## Design decisions

- **`tapyrus-genesis` invocation in CI**: runs via
  `docker run --rm --entrypoint tapyrus-genesis` against the already-built
  `tapyrus/tapyrusd:master-local` image, bypassing the image's own `entrypoint.sh`
  (which wraps the default CMD in `bash -c "$*"` and expects `GENESIS_BLOCK_WITH_SIG`
  for the long-running daemon). `tapyrus-genesis` is a stateless one-shot tool with
  none of that daemon machinery, and reusing the already-built image guarantees the
  unsigned genesis matches the exact tapyrus-core commit under test, rather than a
  second, separately-built copy. Verified against a real image in a full local test.
- **How `NETWORK_ID` actually reaches `tapyrusd`, and why it didn't before**: found by
  reading `tapyrus/tapyrusd`'s own `entrypoint.sh` (there's no `-networkid` flag on
  `tapyrus-genesis`, but `tapyrusd` itself has one). The entrypoint only
  auto-generates a `tapyrus.conf` if none is mounted at `${CONF_DIR}/tapyrus.conf` --
  the auto-generated one hardcodes `dev=1`/`[dev]`/`networkid=1905960821` -- and
  separately, greps `networkid=` out of *whichever* conf ends up there to decide which
  `${DATA_DIR}/genesis.<network_id>` file to write `GENESIS_BLOCK_WITH_SIG` into and
  which `tapyrusd` then loads. Since nothing ever mounted a conf of our own, every
  node always ran that one fixed dev network regardless of `NETWORK_ID`. Fixed by
  `scripts/render_tapyrus_conf.py`, which renders a prod-mode conf (no `-dev` on the
  `tapyrus-genesis` call either -- verified genesis creation itself needs no network
  id, prod is simply what you get by omitting `-dev`) with `networkid=$NETWORK_ID`,
  mounted into all 7 core-* services. Verified against a real container with a network
  id other than the old hardcoded default: `getblockchaininfo` reports `"mode":
  "prod"`, `"chain": "<the overridden id>"`, genesis loads correctly, `getnewaddress`
  and a deliberately-wrong-credentials call both behave as expected. Not wired into
  the seeder -- it isn't brought up by any `docker compose up` invocation yet at all
  (see Known issues/Milestone 4), so wiring `NETWORK_ID` into it now would just be
  another way for it to silently drift from the daemons' actual network id the moment
  an override is used.
- **The prod-mode switch above initially broke P2P entirely, caught only by testing
  the real 7-node topology, not a single container**: the first version of
  `render_tapyrus_conf.py` also pinned `port=12383` (dev mode's default P2P port,
  matching the old auto-generated conf) to keep the RPC-port-table story simple. But
  `docker/docker-compose.yml`'s `-connect=<service-name>` targets have no explicit
  port, and `tapyrus-core` resolves a portless `-connect` against the *chain's own
  default* P2P port (`CConnman::ConnectNode`), not whatever `port=` says in that
  node's own conf. Prod mode's default P2P port is `2357` (confirmed against a real
  container's `Bound to 0.0.0.0:2357` log line with no `port=` override present) --
  not `12383`. With `port=12383` pinned, every node listened on `12383` but every
  `-connect` dialed port `2357` where nothing was listening: zero P2P connections,
  4/7 nodes stuck permanently at 0 peers. A single-container `getblockchaininfo` check
  (`"mode": "prod"`) can't catch this at all -- it says nothing about what port the
  node bound to or who successfully connected to whom. Only running the real 7-node
  `docker-compose.yml` and polling `getconnectioncount` (`wait_for_topology.py`)
  surfaced it. Fixed by dropping `port=` from the rendered conf entirely, letting
  every node fall back to the same chain-default port `-connect` already resolves
  against -- verified for real: `wait_for_topology.py` against the live 7-node stack
  converged on attempt 1, matching the expected 1/2/1/2/1/2/3 pattern exactly, and
  `getpeerinfo` on a second-layer node directly confirmed 2 real peer connections.
  Lesson: whenever a P2P-relevant conf value changes, verify against the full
  multi-node topology, not a single node in isolation -- the smoke run's "Wait for
  topology to converge" step exists specifically to catch this class of bug and now
  runs unconditionally (it only depends on the 7 core nodes, not signers, so it isn't
  gated behind the signers-never-brought-up block -- see "PR review response").
- **`generate_traffic.py` "passed" while generating zero real transactions, because
  `estimatesmartfee` has no history on a brand-new chain and `fallbackfee` defaults to
  disabled** -- found running the actual traffic-generation step against a freshly
  brought-up 7-node stack (signers included) for the first time on a real multi-round
  run: every single funding/send/issuance call across both rounds failed with `-4 Fee
  estimation failed. Fallbackfee is disabled. Wait a few blocks or enable
  -fallbackfee.` (the four non-earning nodes then also failed downstream with `-8
  Insufficient token balance in wallet`, since they were never funded). The script
  still **exited 0** and logged "all N round(s) settled with balances matching the
  ledger" -- its ledger-vs-reality assertion can't distinguish "real activity,
  correctly tracked" from "total failure, trivially consistent" (both a never-funded
  node's ledger entry and its actual RPC balance stay at exactly `0.0`). A green run
  of this step is not by itself evidence traffic was generated -- check the log for
  `round TPC send skipped` / `round colored action failed` warnings, not just the
  final exit code. `tapyrusd --help` shows `-fallbackfee=<amt>` with "(default:
  0.0002)" in its description, but that's documenting the example value, not the
  actual default state -- the real default is disabled (0), matching Bitcoin Core's
  own mainnet-safety convention; confirmed live (the exact error message says so, and
  setting it changes behavior). Fixed by adding `fallbackfee=0.0002` to
  `render_tapyrus_conf.py`'s rendered conf (safe to enable unconditionally here --
  there's no real fee market on a throwaway dev chain to misjudge). Verified for real:
  the same `sendtoaddress` call against a still-empty wallet now fails with the
  *expected* `-8 Insufficient token balance` instead of `-4 Fee estimation failed`,
  confirming fee estimation no longer blocks it (a real balance is still required, as
  it should be) -- `estimatesmartfee` itself still reports "Insufficient data", which
  is fine, since `fallbackfee` is exactly the documented escape hatch for that case.
  Also added `dbcache=64` (450MB default is pure overhead for 7 concurrent containers
  running a chain with a handful of blocks), `maxorphantx=20` (100 default is sized
  for a real internet-facing node; this network's only peers are the other 6 fixed
  `-connect` targets, but kept above 0 since the planned reorg step can transiently
  orphan real transactions), and `mempoolexpiry=2` (hours; 336h/2-week default targets
  a long-running production node, not a several-hour CI job). **Re-verified with a
  full `generate_traffic.py` run** (not just the isolated `sendtoaddress` check
  above), immediately before enabling both it and `simulate_reorg.py` in the
  workflow: `tx_round_count=2` at smoke scale, zero `round TPC send skipped` / `round
  colored action failed` warnings across both rounds (previously every single one
  failed), real funding/issuance/transfer/topup activity throughout (including a
  NON_REISSUABLE/NFT color rotation via the topup-mints-fresh-color path), settled
  with balances matching the ledger for real this time, not vacuously. Chain reached
  height 11 in ~12 minutes.
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
  trigger runs the identical job at reduced scale (`reorg_loser_blocks`/
  `reorg_winner_margin`, `tx_round_count`, `rotation_height_offset` all drop to
  smaller fallbacks, keyed off `github.event_name` since `pull_request`/`push` runs
  have no `inputs` context -- `chain_height_before_reorg` isn't in that list anymore;
  it's always derived from `tx_round_count`, so it shrinks along with it automatically
  rather than needing its own fallback) and a shorter
  `timeout-minutes` (60 vs. 360). `tx_round_count` is the one of these already wired to
  a real step (`generate_traffic.py`); the reorg/rotation variables still aren't
  consumed by anything -- wired in now so the smoke run is already fast once Milestone
  3/4 finishes landing them, rather than needing a second pass then. Not yet verified against
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
- **`generate_traffic.py`'s round-count-only design**: everything (block-height budget,
  transaction count) derives from one number, `tx_round_count`, rather than independent
  knobs for total tx count and send interval. Earlier drafts had a separate
  `tx_interval_seconds` (pacing between sends from the same node) and `tx_total_count`
  -- both dropped once the design settled on exactly one send per node per round: with
  no sequence of same-node sends within a round, there's nothing left for an interval to
  pace, and the total is just `round_count * 14` (7 nodes x {TPC, colored}) by
  construction, so a separate total would only ever have to agree with that derived
  number or drift from it.
- **Colored-coin balance shortfall mints instead of skipping**: rather than issue every
  node's full lifetime token supply once at the start (all colored-coin activity then
  front-loaded into one phase, nothing but transfers for the rest of the run), each node
  only ever holds a small working balance (`TOKEN_ISSUE_AMOUNT`/`TOKEN_TOPUP_AMOUNT` =
  3). A round where the sender doesn't have enough issues/reissues more instead of
  transferring -- still exactly one transaction for that node, so total tx count per
  round stays fixed at 14 regardless of how many nodes hit a shortfall that round. This
  is also why NFT nodes (forced to `value=1`) end up minting a fresh NFT color on almost
  every other round -- expected, not a bug: they transfer their one unit away, then have
  zero until the next mint.
- **Top-up timing needs no cross-round lookahead**: an earlier draft of this design
  considered checking one round ahead (during round K's settle phase, top up for round
  K+1's send) specifically to avoid a same-round top-up-then-spend needing to chain onto
  its own unconfirmed output. Dropped once the source read showed `issuetoken`'s
  NON_REISSUABLE/NFT path selects its input UTXO explicitly
  (`coin_control.Select(out)`) and only checks `IsMine(...) == ISMINE_SPENDABLE` --  no
  confirmation-depth check -- so spending a same-round unconfirmed self-send is expected
  to work, confirmed live in the local E2E run (see Lessons learnt above). A shortfall
  now just substitutes a mint for that round's transfer, with no phase held back a round
  to accommodate it.
- **Per-transaction fee tracked via `gettransaction`, not predicted**: the ledger's TPC
  bookkeeping reads each transaction's actual fee back from `gettransaction` right after
  broadcasting it, rather than trying to predict it from a fee rate and an assumed tx
  size. Fee-rate-based prediction would have needed tx vsize to be near-constant across
  every transaction shape this script produces (plain TPC send, colored transfer,
  REISSUABLE's 2-tx issuance, NON_REISSUABLE/NFT's single-tx issuance) to stay accurate
  -- reading the real fee sidesteps needing that assumption to hold at all.

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
