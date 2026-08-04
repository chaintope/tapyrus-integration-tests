# Known issues, gotchas, design decisions

Open problems, gotchas, and design choices behind this repo's scripts, CI workflow,
and compose stack. Code/config comments point here ("see doc/work-done.md") instead
of repeating this detail inline.

See [`weekly-integration-test-plan.md`](weekly-integration-test-plan.md) for the
original scenario design, [`project-plan.md`](project-plan.md) for tracked
done-vs-outstanding progress, and [`scripts.md`](scripts.md) for what each script
does.

## Known issues (open)

- **CI timing is capped at GitHub-hosted's 6h hard limit.** Blocks only come from the
  live signer round-robin (`ROUND_DURATION` cadence, no instant-mining), so scenario
  time is `blocks x ROUND_DURATION`. Size `tx_round_count`/`reorg_length`/
  `round_duration` accordingly.
- **No compose-level healthchecks yet** (`depends_on: condition: service_healthy`).
  `scripts/wait_for_topology.py` covers this at the CI level by checking real P2P
  peer counts, but the compose-file enhancement itself is still unstarted.
- **Signer count (3) and threshold (2) are hardcoded.** The 7-node topology is wired
  1:1 to exactly 3 signers, so changing the count means redesigning the topology, not
  just passing a new number.
- **`generate_traffic.py`'s constants haven't been stress-tested at scale.**
  `FUNDING_AMOUNT_TPC`/`TOKEN_ISSUE_AMOUNT`/etc. work fine at small round counts, but
  a larger round count would trigger the balance-shortfall top-up mechanic much more
  often, and that hasn't been checked.

## Design decisions

- **All scripts are Python** (stdlib only). Class-based (`RepoCheckout`,
  `AggpubkeyCeremony`, `GenesisSigningCeremony`, `SignerConfigAssembler`,
  `TopologyWaiter`), sharing a common logger (`scripts/lib/log.py`), ceremony base
  class (`scripts/lib/ceremony.py`), and compose helper (`scripts/lib/compose.py`).
- **Reorg mechanic**: genuine network isolation within the current federation, not
  rotation. `scripts/simulate_reorg.py` keeps only one group's core nodes up at a
  time (never both) until the final reconnect, so the same signer set independently
  threshold-signs two real forks from a common tip. See the script's own docstring
  for the exact steps.
- **`tapyrus-genesis` runs via `docker run --rm --entrypoint tapyrus-genesis`**
  against the already-built `tapyrus/tapyrusd:master-local` image, bypassing
  `entrypoint.sh`. This keeps the unsigned genesis matched to the exact tapyrus-core
  commit under test.
- **How `NETWORK_ID` reaches `tapyrusd`**: `entrypoint.sh` reads `networkid=` from
  the mounted conf to decide which `genesis.<network_id>` file to write
  `GENESIS_BLOCK_WITH_SIG` into. `render_tapyrus_conf.py` renders that conf with
  `networkid=$NETWORK_ID` for all 7 core-* services. The seeder's own `-i`/`-s` flags
  stay hardcoded to `1905960821` (see `docker-compose.yml`'s `seeder` comment).
- **P2P topology relies on the chain's default port, not `port=`.** `-connect=
  <service-name>` has no explicit port, and `tapyrus-core` resolves it against the
  chain's own default P2P port (`2357` in prod mode), not the conf's `port=`.
  `render_tapyrus_conf.py` omits `port=` so every node matches.
- **`generate_traffic.py` needs `fallbackfee` enabled.** `-fallbackfee` defaults to
  disabled (a mainnet-safety default, not a bug), so `estimatesmartfee` fails with no
  fee history on a new chain. `render_tapyrus_conf.py` sets `fallbackfee=0.0002`,
  `dbcache=64`, `maxorphantx=20`, and `mempoolexpiry=2` -- all sized down from
  mainnet defaults for a small, short-lived CI chain.
- **`generate_traffic.py`'s round-count-only design**: everything derives from
  `tx_round_count` -- exactly one send per node per round, so total tx count is
  always `round_count * 14` (7 nodes x {TPC, colored}).
- **Colored-coin balance shortfall mints instead of skipping.** Each node holds a
  small working balance (`TOKEN_ISSUE_AMOUNT`/`TOKEN_TOPUP_AMOUNT` = 3). When a
  sender is short, it issues/reissues more instead of transferring -- still one
  transaction, so per-round tx count stays fixed at 14. NFT nodes mint a fresh color
  almost every other round as a result; that's expected.
- **Top-up timing needs no cross-round lookahead.** `issuetoken`'s NON_REISSUABLE/NFT
  path only checks `IsMine(...) == ISMINE_SPENDABLE`, no confirmation-depth check, so
  a same-round top-up-then-spend works fine.
- **Per-transaction fee is read via `gettransaction`, not predicted.** This avoids
  needing tx vsize to stay constant across this script's different transaction
  shapes.
- **`generate_traffic.py` seeds its ledger from each node's real balance**
  (`getbalance`) at startup, since the workflow runs this script multiple times per
  job and a node can carry a non-zero balance into a later run.
- **`core-1a`/`2a`/`3a`'s coinbase income is fully asserted, not excluded --
  observed directly per block.** Which of the 3 earns at a given height is
  whichever signer sorts first by raw pubkey bytes (`tapyrus-signer`'s
  `Federation::signers()`, `net.cpp`'s `SignerID::Ord`) -- not creation order,
  and not something worth reimplementing in Python: doing it from
  height-since-genesis would silently drift if any round in the chain's whole
  history ever failed to produce a block. `_credit_coinbase_for_height` reads
  each height's real coinbase transaction directly instead (asks each of the 3
  wallets for it, credits whichever one actually has it) -- no anchor needed,
  so a skipped round costs nothing. `_next_block_with_coinbase` credits every
  height since the last one credited, not just the height it happens to
  observe, so two blocks landing between polls don't leave the one in between
  uncredited. The reward itself isn't a flat 50 TPC either -- confirmed live,
  it's subsidy plus whatever transaction fees that block happened to include,
  so every height reads the actual amount from the earner's own `generate`
  transaction rather than assuming one.
- **`computesig` requires full-signer-count arrays.** `--sig`/`--block-vss`/
  `--node-vss` must list every signer, not just `threshold` (enforced by an
  `assert_eq!`). It also needs one specific signer's key material (hardcoded to
  signer-0 here) -- a real v1 "designated signer" limitation, not an oversight.
- **`tapyrus-signerd` needs a live RPC connection to its own core node to sign at
  all**, even as a non-master. There's no Redis-only fallback -- this is why the
  reorg recipe repoints the losing group's signers to their one surviving core node
  before signing.
- **`sign`/`computesig` also accept `--xfield`** as an alternative to `--block`, to
  sign an aggpubkey rotation or max-block-size change instead of a genesis block --
  the mechanism behind federation rotation.
- **`tapyrus-core` disables listening entirely once `-connect` is set** (not just
  outbound dialing). Every `-connect` edge needs an explicit `-listen=1` added back
  wherever a node also needs to accept an inbound connection.
- **`tapyrus/tapyrusd`'s entrypoint runs `exec bash -c "$*"`**, and compose's
  `command:` replaces the image's default CMD rather than appending to it -- every
  override needs to repeat the full invocation.
- **`tapyrus-setup createkey` always produces mainnet-prefixed WIFs** (`K`/`L`,
  `0x80`), unlike tapyrus-core's own `-dev` convention (`c` prefix, `0xef`).
- **A harmless warning at any round-duration**: a non-master's `submitblock`
  occasionally races another signer's already-submitted block, and tapyrus-core's
  `"duplicate"` response doesn't match the Rust client's expected type. The block is
  already accepted through the other path.
- **`createnodevss`/`createblockvss` output is sorted by receiver pubkey**, not
  `--public-key` argument order. Extracting by line position instead of matching the
  pubkey fails with an opaque `InvalidSS` error.
- **`getnewaddress` right after `docker compose up -d` needs a retry.** A "running"
  container just means the process started, not that RPC is ready.
  `scripts/collect_coinbase_addresses.py` retries via `RpcUnreachable` and raises on
  an empty address.
- **`RPC_IN_WARMUP` (-28) is a common, retryable state**, not just
  connection-refused/timeout. `lib/rpc.py` treats it the same as `RpcUnreachable`,
  while still raising `RpcError` for any other RPC error.
- **`workflow_dispatch` inputs have no `default:`.**
  `workflow_dispatch.inputs.*.default` can't hold an expression, and
  `schedule`/`pull_request`/`push` have no `inputs` context at all -- so every
  default lives in the `env:` block's `inputs.x || 'literal'` fallback instead.
- **`pull_request`/`push` smoke trigger** validates changes to `scripts/**`,
  `docker/**`, `config/**`, or the workflow itself before merge, at reduced scale
  (`timeout-minutes: 180` vs. 360).
- **`CHAIN_HEIGHT_BEFORE_REORG`/`FEDERATION_CHANGE_HEIGHT` are floors, not literal
  targets.** `CHAIN_HEIGHT_BEFORE_REORG` is `TX_ROUND_COUNT + 2`;
  `simulate_reorg.py` waits for at least that height, then uses the actual height
  reached. `FEDERATION_CHANGE_HEIGHT` is `REORG_LENGTH`, counted from whatever height
  the chain is at when `simulate_federation_change.py` runs.
- **Git submodules**: `tapyrus-core` vendors `secp256k1` as a submodule.
  `checkout_repos.py` runs `git submodule update --init --recursive` after every
  clone/update.
- **Python's stdout is block-buffered when not attached to a tty** (the normal case
  in CI). `scripts/lib/log.py` uses `flush=True` so output interleaves correctly with
  subprocess output.
- **Core network topology** is enforced entirely via `-connect=`, with all 7 nodes on
  one flat Docker network.
- **`tapyrus-seeder` genuinely bootstraps a new node** (`scripts/verify_seeder.py`).
  A brand-new 8th node with no hardcoded topology knowledge discovers and connects to
  the network entirely through it. Both that node (`seeder-test-node`) and `seeder`
  itself are stopped and removed once `run()` finishes, in a `finally` (guaranteed on
  failure too) -- neither is part of the fixed topology every later step (reorg,
  federation change) runs against. Left running, `seeder-test-node`'s persistent
  connection into whichever listener it discovered permanently mismatches
  `wait_for_topology.py`'s exact `getconnectioncount` check on that node, and during
  `simulate_reorg.py`'s isolated-build phases (one group's core nodes entirely
  stopped while the other builds alone) gives the supposedly-isolated group a real
  P2P path to learn the *other* group's blocks via header relay before it's supposed
  to see them at all -- silently defeating the strict-alternation the whole reorg
  recipe depends on. The seeder's own crawler adds flakiness on top even where
  `seeder-test-node` never attached: it re-tests every address on its own ~60s cycle
  with real TCP connections held open waiting for a reply that never comes (see
  Lessons learnt below), which can transiently perturb any node's exact-count poll.
- **Phase 1's addseeder-mode check looks for one real (non-seeder) peer, not a peer
  count.** The original design captured each node's peer count right after the
  post-restart `_wait_for_all_rpc_ready` returned, then waited for it to exceed
  that snapshot -- but P2P connections start forming the moment each process
  restarts, independent of when its own RPC server finishes initializing, so some
  nodes were already 2-3 peers in by the time the "baseline" was captured, leaving
  no observable room to grow within the timeout even though discovery had
  genuinely worked. Tried requiring an absolute floor of 2+ peers instead (sidesteps
  that race), but that failed too, for a deeper reason confirmed by reading
  `tapyrus-core`'s own `net.cpp` (`ThreadOpenConnections`, "Only connect out to one
  peer per network group"): every core-* node here sits on the same `/24`
  (`51.51.51.0/24`), so they're all one netgroup, and tapyrus-core's own
  outbound-connection diversity logic caps each node's OWN dialing at ~1 real peer
  *permanently* -- a node not also lucky enough to be picked as someone else's
  inbound target legitimately never exceeds 1 real peer, no matter how long you
  wait. Confirmed live: a node stuck at 1 only ever reached "2" again once the
  seeder's own periodic `-s` crawl cycled back around and re-probed it (a second,
  transient connection) 15+ minutes later -- not new organic discovery, just the
  seeder's own crawl cadence. The seeder's crawl connection is transient anyway
  (see Lessons learnt below), so it can't be relied on to pad a count either way.
  The right, achievable signal is simply: is there a peer whose `subver` isn't the
  seeder's own (`SEEDER_SUBVER`)? One real peer is enough proof, as long as it's
  the right one.
- **The node orchestrator runs inside each core-* container, not as a host-driven
  script** (`scripts/container/node_orchestrator.py`, `scripts/start_node_orchestrator.py`).
  Every core-* node's `command:` (`docker-compose.yml`) is now
  `entrypoint_wrapper.sh`, which hands off to `node_orchestrator.py` once
  `NODE_ORCHESTRATOR` is set -- it launches `tapyrusd` as a child process and
  supervises it directly, rather than being it, so it can genuinely stop/restart/
  reindex/invalidate it via real RPC calls and still be the one to bring it back.
  Continuous for the rest of the job (traffic generation, reorg, federation change,
  max-block-size change all run against chaos-supervised nodes), not its own
  isolated phase -- see the pause-file bullet below for how that's kept safe.
- **core-1a/2a/3a never take a deliberate chaos action -- only core-1b/2b/3b/core-7
  do** (`CHAOS_NODES` in `node_orchestrator.py`). core-1a/2a/3a are the 3 signers'
  own RPC targets; live testing found that even one of them briefly catching up from
  a chaos-triggered restart could make it miss its turn in `tapyrus-signer`'s
  round-robin master selection, throwing off `generate_traffic.py`'s
  coinbase-rotation tracking mid-calibration. They still get crash-recovery
  supervision like every other node (`_supervise_crashes`), just never a deliberate
  stop/restart/invalidate. `NODE_ORCHESTRATOR_FLAVOR` is still set for these 3 in
  `docker-compose.yml` (matching the round-robin assignment below) but is unused
  dead config for them specifically, since nothing ever reads it without the chaos
  loop running.
- **Restart flavor is a static, round-robin assignment per node, not re-randomized
  per action.** `-reindex`/`-reindex-chainstate`/`-reloadxfield` cycle across the 7
  nodes (`NODE_ORCHESTRATOR_FLAVOR` in `docker-compose.yml`), though only
  core-1b/2b/3b/core-7's assignment is actually exercised (see above). Every chaos
  node still does a plain restart, its one flavored restart, and an
  invalidate/reconsider at least once per cycle (shuffled order, random delays).
- **A restarted node stays down until the rest of the network has produced a couple
  more real blocks**, polled from another node, not a timer -- restarting at the
  very next block wouldn't give peers real time to notice and drop the now-stale
  connection. Bounded at 90s (`DOWNTIME_TIMEOUT_SECONDS`): kept well under
  `wait_for_topology.py`'s own 300s convergence budget, since that check runs before
  any signer/traffic exists, so the block-count condition can never be satisfied
  during it and every downtime would otherwise fall through to the timeout.
- **Chaos waits out a 360s startup grace period before its first action**
  (`STARTUP_GRACE_SECONDS`). With several nodes each independently churning every
  30-180s, some node is essentially always mid-restart, which fights
  `wait_for_topology.py`'s own purpose of confirming the mesh formed correctly right
  after bring-up. Chaos still runs continuously for the rest of the job; this only
  delays its first action past that check's own budget.
- **A shared pause file protects the other scenario scripts' own precise node
  up/down assumptions from the orchestrator's chaos** (`scripts/lib/orchestrator_control.py`).
  `simulate_reorg.py`'s isolated-build phases hard-depend on exactly one group being
  completely up and building alone; `simulate_federation_change.py`/
  `simulate_maxblocksize_change.py` each have a single, non-retrying
  `getblockchaininfo` confirmation check that a node mid-restart at the wrong moment
  would fail spuriously; `generate_traffic.py` brackets every all-nodes-reachable
  RPC sequence the same way (address collection, balance seeding, coinbase-rotation
  calibration, every block wait, every balance verification). All touch the pause
  file before their sensitive window and remove it in a `finally` (guaranteed even
  on failure) -- every core node's orchestrator checks for it before any action, not
  just restarts, so it also covers `invalidateblock` (which doesn't take RPC down).
  Calls nest via a depth counter (`pause_node_orchestrators`/
  `resume_node_orchestrators`), so an inner pause/resume pair (e.g.
  `generate_traffic.py`'s own `_wait_for_next_block`, called from within its broader
  calibration window) doesn't prematurely resume chaos while an outer caller still
  needs it paused. The pause file only stops *new* actions -- it can't interrupt one
  already in flight the instant it lands, so `generate_traffic.py`'s own RPC calls
  additionally retry on `RpcUnreachable` (`_call_with_retry`) rather than treating a
  momentary straggler as a hard failure.
- **`NODE_ORCHESTRATOR` must be persisted to `$GITHUB_ENV`, not just set within
  `start_node_orchestrator.py`'s own process.** `signer-0`/`signer-1`/`signer-2` and
  signer-set-b's services all `depends_on` a core-1a/2a/3a node in
  `docker-compose.yml`. Without persisting it job-wide, any later `docker compose
  up` touching those dependents (Bring up signers, Federation change) would resolve
  `NODE_ORCHESTRATOR` back to unset, and Compose would recreate that core node to
  match its now-different resolved config -- silently reverting it to plain
  `tapyrusd`. Confirmed live: this exact drift happened mid-session.
- **Secrets scope**: this repo only generates local dev secrets
  (`generate_dev_secrets.py`); it never provisions real GitHub secrets. No CI
  secret is currently needed at all -- Slack notification was deferred (see
  Outstanding work in `project-plan.md`); GitHub's own built-in failure
  notifications cover failure detection for now.
- **Why the network uses `51.51.51.0/24`**: it needs to pass `IsRoutable()`, and
  neither RFC1918 private ranges nor RFC 5737 test ranges qualify -- `IsRoutable()`
  excludes both. `51.51.51.0/24` is an arbitrary block confirmed to fall outside
  every exclusion (see Lessons learnt above). If this ever needs to change, pick a
  different block and re-check it against that exclusion list.

## Lessons learnt

- **`tapyrus-setup`'s offline `--xfield sign`/`computesig` rejected valid signatures
  with `InvalidSig` about half the time.** Root cause: `Sign::format_signature`
  (`sign.rs`) encoded a signature's `v` point using only its x-coordinate, dropping
  y-parity, so the verification side's reconstruction guessed the wrong y about half
  the time. A separate theory (inconsistent Schnorr share selection across process
  invocations) was tested and ruled out via 60/60 clean ceremony rounds. This was a
  `tapyrus-signer` bug, fixed upstream.
- **A silent `docker compose up`-triggered recreate stranded two nodes and hung
  a real CI run for 3+ hours until the job's own timeout killed it.**
  `simulate_reorg.py` restarts group A (`start_nodes`, which is `docker compose up
  -d --no-deps ...`) from its own fresh process, which had never re-set
  `CORE_1B_ARGS`/`CORE_2B_ARGS` -- Compose resolved them to empty, saw that as a
  real config change from what the container was created with, and recreated
  core-1b/core-2b *without* their `-connect=` flag, permanently stranding them
  with zero peers (`--no-deps` only stops cascading to *other* services'
  `depends_on` targets; it doesn't protect a service's own `command:` from
  drifting when its own env var isn't set). core-1a/core-2a kept building blocks
  completely normally, so the height-wait the rest of the recipe was blocked on
  could never succeed -- confirmed live: 137 blocks past the intended target
  before the job was externally killed, while core-1b/core-2b sat frozen at their
  restart-time height the entire time. The same root cause, independently, made
  "Bring up signers" silently recreate core-1a/2a/3a mid-run too (`SEEDER_IP`
  unset there this time) -- survived only by luck (their connect-mode args happen
  to be empty strings, so nothing actually changed that run). Fixed at the
  source: `verify_seeder.py` persists `GENESIS_BLOCK_WITH_SIG`/`SEEDER_IP`/every
  `CORE_*_ARGS` to `$GITHUB_ENV` once connect mode is up, so every later step in
  the job resolves the same values regardless of which fresh process touches
  `docker compose` next -- not a per-script patch, since any future script with
  the same oversight would reintroduce the same failure mode. "Bring up signers"
  also gained `--no-deps` as defense in depth.
- **`round-duration=60` avoids transient `InvalidBlock` errors** around round
  boundaries that shorter durations (e.g. 10) hit. `round-duration=30` also verified
  clean.
- **Building `tapyrus-signer` outside Docker on macOS** also needs
  `brew install gmp` + `LIBRARY_PATH=/opt/homebrew/lib`, for the linker to find the
  system `libgmp`.
- **How `tapyrus-seeder` discovers nodes, and what changes between the two
  `docker-compose.yml` bring-up modes** (`scripts/verify_seeder.py`):

  1. **DNS-seed crawling is a direct per-address liveness test, not gossip.**
     `-s <networkid>:<host>:<port>` seeds an initial candidate list; a pool of
     crawler threads (`-t`) each open a real TCP connection, do a VERSION/VERACK
     handshake, and send `getaddr`.
  2. **`GETADDR` only ever returns what the responding node's own addrman already
     has, and `-connect` targets are never added to it.** `CAddrMan::Good_()`
     (`addrman.cpp`) only updates an address already present via `Add()`; `-connect`
     targets are resolved and dialed directly, skipping `Add()` entirely. So in
     connect mode every node's addrman stays empty regardless of subnet (confirmed
     live: no `addr` message ever exchanged on any `-connect` edge, even after hours
     of uptime) -- crawling from a single node would never find the other 6. Fixed
     by seeding every node's address directly (`-s` once each) instead of relying on
     gossip from one entry point.
  3. **One successful test isn't enough to be servable via DNS.** `IsGood()` needs
     3+ successes at least `MIN_RETRY` (60s) apart. Before that, `GetIPs_` falls back
     to one arbitrary attempted address.
  4. **The default thread count (96) fights small networks.** Idle-retry backoff
     scales with thread count (`rand() % (500 * nThreads)`), so 96 threads meant up
     to ~48s of random sleep between retries on top of the 60s `MIN_RETRY`. Turned
     down to `-t 2`.
  5. **Neither `tapyrus-seeder` nor `tapyrus-core` will ever treat a private/internal
     address as usable.** Both gate on `CNetAddr::IsRoutable()`. Docker's default
     bridge range fails this, and so does RFC 5737 (TEST-NET-3, tried first) --
     `tapyrus-core`'s `IsRoutable()` explicitly excludes it, unlike the seeder's own
     simpler check. No IANA special-purpose block passes; `51.51.51.0/24` is an
     arbitrary block confirmed to fall outside all of them. Safe: this network is an
     isolated Docker bridge, never reachable from the real internet.
  6. **Which nodes end up "good" depends on the bring-up mode, not anything the
     seeder does differently.** In connect mode, `core-7`'s `-connect` (with no
     offsetting `-listen=1`) disables its listening entirely, so it's the one
     address that never becomes good -- seeded anyway, deliberately, as a real
     negative case. In addseeder mode `core-7` has no `-connect` at all, so it
     listens like the other 6 and the seeder legitimately reports all 7 as good
     (confirmed live) -- `core-7` never appearing is a connect-mode-only invariant.
  7. **A node with no `-connect` at all uses its addrman normally, so `-addseeder`
     genuinely grows its peer count from nothing.** Confirmed live: all 7 nodes
     given only `-addseeder` organically connect to each other. `-connect`
     auto-disables `-dnsseed` too, and `-addseeder` with `-dnsseed` off is a fatal
     `InitError` -- an explicit `-dnsseed=1` override is needed to boot a node that
     (temporarily) needs both. `ThreadDNSAddressSeed` also only runs once, at
     process startup -- a node's first attempt always sees a not-yet-converged
     seeder, so a restart is needed to get real results.

  End state: in connect mode the seeder converges on exactly the 6 nodes that
  actually listen there (never `core-7`), and a brand-new 8th node with only
  `-addseeder` genuinely discovers and connects to the network through it. In
  addseeder mode, all 7 nodes -- `core-7` included -- discover and connect to each
  other organically, confirmed by every node's own peer count growing from nothing.
