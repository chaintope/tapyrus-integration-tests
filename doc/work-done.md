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
  time is `blocks x ROUND_DURATION`. Size `tx_round_count`/`reorg_length_blocks`/
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
- **RPC auth is tapyrus-core's own auto-generated cookie file, not a static
  password.** Setting `rpcuser`/`rpcpassword` disables cookie generation entirely
  (`InitRPCAuthentication`, `httprpc.cpp`) -- the two are mutually exclusive, so
  `render_tapyrus_conf.py` sets neither. `entrypoint_wrapper.sh` passes
  `-rpccookiefile=/cookies/$NODE_NAME.cookie`, where `/cookies` is
  `runtime/rpc-cookies/` bind-mounted into every core-* container (and the host),
  so any container -- or a host-side script -- can read any node's cookie, not just
  its own. The cookie is regenerated with a fresh random value on every single
  process startup, with no persistence option, so `scripts.lib.rpc.CoreRpcClient`
  reads it fresh on every RPC call rather than caching credentials at construction
  time -- a chaos-restarted node's cookie changes every 30-180s
  (`MIN/MAX_ACTION_INTERVAL_SECONDS`, `node_orchestrator.py`) throughout a run.
  `assemble_signer_configs.py` points each signer at its own RPC target's cookie
  file directly (`rpc-endpoint-cookiefile` in `tapyrus-signer.toml`, `/cookies` bind
  mounted into every signer service too) rather than resolving and baking a value
  in -- `tapyrus-signer` reads it fresh on every RPC call for the same reason
  `CoreRpcClient` does, so a signer's own core-RPC-target restarting (chaos or a
  genuine crash-recovery) doesn't require restarting the signer either.
- **`scripts.lib.rpc.cookie_path()`'s default resolves relative to `REPO_ROOT`
  (`Path(__file__).resolve().parent.parent.parent`), which is only the repo root
  on the CI host.** `node_orchestrator.py` runs inside a core-* container, where
  the same relative walk from `/app/scripts/lib/rpc.py` lands on `/app` --
  `/app/runtime/rpc-cookies/`, not the real `/cookies` mount.
  Fixed with an optional `cookie_dir` override on `cookie_path()`/
  `CoreRpcClient`, which `node_orchestrator.py` passes explicitly
  (`COOKIE_DIR = Path("/cookies")`) at both of its call sites.
- **`entrypoint_wrapper.sh` re-`chmod`s every cookie file to 644 in a background
  loop, once a second, for as long as the container runs.** tapyrus-core always
  writes the cookie file `0600` (owner-only, `umask 077`) -- every core-*
  container (and seeder-test-node, routed through the same wrapper) runs as
  root, so container-to-container reads already work regardless of that mode,
  but the CI host process reading the same file straight off the
  `runtime/rpc-cookies/` bind mount is a non-root user and got `PermissionError`
  every time. A one-shot `chmod` right after startup isn't
  enough since a chaos restart regenerates the file from scratch at the same
  mode; the loop re-applies it on every regeneration instead of trying to catch
  the exact moment each one happens.
- **The workflow's "Record runner environment" step reports, not enforces.**
  `runs-on: ubuntu-latest` is a floating tag -- GitHub repoints it to a new
  image periodically, with its own default Docker/kernel/package versions, no
  pin in this repo's control. A hard baseline diff would need updating every
  time GitHub moves it or start failing builds for no real reason. So it just captures the runner image identity (`ImageOS`/`ImageVersion` -- the exact `actions/runner-images` release), OS/kernel, Docker, Python, and Rust versions into the step log and an `environment-fingerprint` artifact on every run -- no dump of every installed package, just the toolchains this repo's own build actually depends on. Rust is captured deliberately, not redundantly with Python's explicit `setup-python` pin: `cargo build --release` (building `tapyrus-setup` for the offline ceremony) runs directly on this runner against whatever toolchain it happens to preinstall, unlike the containerized `tapyrus-signer:integration-test` image, which pins `rust:1.82-bookworm` in its own Dockerfile.
- **`generate_traffic.py` needs `fallbackfee` enabled.** `-fallbackfee` defaults to
  disabled, so `estimatesmartfee` fails with no fee history on a new chain. `render_tapyrus_conf.py` sets `fallbackfee=0.0002`, `dbcache=64`, `maxorphantx=20`, and `mempoolexpiry=2` -- all sized down from mainnet defaults for a small, short-lived CI chain.
- **`generate_traffic.py`'s round-count-only design**: everything derives from
  `tx_round_count` -- exactly one send per node per round, so total tx count is
  always `round_count * 14` (7 nodes x {TPC, colored}).
- **Colored-coin balance shortfall mints instead of skipping.** Each node holds a
  small working balance (`TOKEN_ISSUE_AMOUNT`/`TOKEN_TOPUP_AMOUNT` = 3). When a
  sender is short, it issues/reissues more instead of transferring -- still one
  transaction, so per-round tx count stays fixed at 14. NFT nodes mint a fresh color
  almost every other round as a result.
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
- **`REORG_BASELINE_HEIGHT` is a floor, not a literal target; `FEDERATION_CHANGE_OFFSET_BLOCKS`
  is an offset, not an absolute height at all** -- the two are named to reflect that
  difference, not interchangeable "height" concepts. `REORG_BASELINE_HEIGHT` is
  `TX_ROUND_COUNT + 2`; `simulate_reorg.py` waits for at least that height, then uses
  the actual height reached. `FEDERATION_CHANGE_OFFSET_BLOCKS` is `REORG_LENGTH_BLOCKS`,
  added to whatever height the chain is at when `simulate_federation_change.py` runs --
  there's no floor/target distinction for it, since it was never a height to wait for
  in the first place.
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
  count.** The right, achievable signal is simply: is there a peer whose `subver` isn't the seeder's own (`SEEDER_SUBVER`)? One real peer is enough proof, as long as it's
  the right one.
- **The node orchestrator runs inside each core-* container, not as a host-driven
  script** (`scripts/container/node_orchestrator.py`, `scripts/start_node_orchestrator.py`). Every core-* node's `command:` (`docker-compose.yml`) is now
  `entrypoint_wrapper.sh`, which hands off to `node_orchestrator.py` once
  `NODE_ORCHESTRATOR` is set -- it launches `tapyrusd` as a child process and
  supervises it directly, rather than being it, so it can genuinely stop/restart/
  reindex/invalidate it via real RPC calls and still be the one to bring it back.
  Continuous for the rest of the job (traffic generation, reorg, federation change,
  max-block-size change all run against chaos-supervised nodes), not its own
  isolated phase -- see the pause-file bullet below for how that's kept safe.
- **core-1a/2a/3a never take a deliberate chaos action -- only core-1b/2b/3b/core-7
  do** (`CHAOS_NODES` in `node_orchestrator.py`). core-1a/2a/3a are the 3 signers'
  own RPC targets, threshold 2-of-3 -- disrupting them risks reducing available
  signing capacity below threshold if more than one is ever down at once. They
  still get crash-recovery supervision like every other node
  (`_supervise_crashes`), just never a deliberate stop/restart/invalidate.
  `NODE_ORCHESTRATOR_FLAVOR` is still set for these 3 in
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
  up/down assumptions from the orchestrator's chaos** (`scripts/lib/orchestrator_control.py`)`simulate_reorg.py`'s isolated-build phases hard-depend on exactly one group being
  completely up and building alone; `simulate_federation_change.py`/
  `simulate_maxblocksize_change.py` each have a single, non-retrying
  `getblockchaininfo` confirmation check that a node mid-restart at the wrong moment
  would fail spuriously; `generate_traffic.py` brackets every all-nodes-reachable
  RPC sequence the same way (address collection, balance seeding, every
  per-height coinbase credit, every block wait, every balance verification). All
  touch the pause file before their sensitive window and remove it in a `finally`
  (guaranteed even on failure) -- every core node's orchestrator checks for it
  before any action, not just restarts, so it also covers `invalidateblock`
  (which doesn't take RPC down). Calls nest via a depth counter
  (`pause_node_orchestrators`/`resume_node_orchestrators`), so an inner
  pause/resume pair (e.g. `generate_traffic.py`'s own `_wait_for_next_block`,
  called from within `_next_block_with_coinbase`'s own paused window) doesn't
  prematurely resume chaos while an outer caller still needs it paused. The
  pause file only stops *new* actions -- it can't interrupt one
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
  `tapyrusd`. Confirmed live: this exact drift happened mid-session. Persisted by
  the script itself (`_persist_env_for_rest_of_job`, same pattern as
  `verify_seeder.py`'s own), not by a separate `echo >> $GITHUB_ENV` in the
  workflow step -- self-contained, so running this script in any other context
  doesn't silently reintroduce the same drift.
- **Every script logs a `done.` line as its last action, naming whatever it
  handed off** (a file it wrote, an env var it persisted, a condition it
  confirmed) -- a deliberate, uniform convention, not incidental: `grep '\] done\.'`
  across a job's combined log surfaces every script's completion point, in order,
  as a one-line trace of the whole pipeline's handoffs.
- **Secrets scope**: this repo only generates local dev secrets
  (`generate_dev_secrets.py`); it never provisions real GitHub secrets. No CI
  secret is currently needed at all.
- **Why the network uses `51.51.51.0/24`**: it needs to pass `IsRoutable()`, and
  neither RFC1918 private ranges nor RFC 5737 test ranges qualify -- `IsRoutable()`
  excludes both. `51.51.51.0/24` is an arbitrary block confirmed to fall outside
  every exclusion (see Lessons learnt above). If this ever needs to change, pick a
  different block and re-check it against that exclusion list.
- **Per-node RPC checks fetch concurrently, not in a sequential loop, wherever
  every node's result is independent** (`generate_traffic.py`'s
  `_settle_and_verify` via `_fetch_node_balances`;
  `simulate_federation_change.py`/`simulate_maxblocksize_change.py`'s rotation/
  max-block-size confirmation; `verify_seeder.py`'s peer-discovery poll). Beyond
  the obvious wall-clock win, `_settle_and_verify` specifically also shrinks the
  window a new block could land inside mid-read (still guarded by its own
  re-read-if-height-moved check, just less likely to need it). Every one of these
  log lines names the node it's about -- `asyncio.gather()` doesn't preserve the
  per-node ordering a sequential loop would, so a log with several nodes' output
  interleaved needs that to stay readable.

## Lessons learnt

- **`tapyrus-setup`'s offline `--xfield sign`/`computesig` rejected valid
  signatures with `InvalidSig` about half the time** -- `Sign::format_signature`
  dropped y-parity when encoding a signature's `v` point. A `tapyrus-signer` bug,
  fixed upstream.
- **`docker compose up` from a fresh process can silently recreate an
  already-running container if that process never re-set the same env vars
  the container was created with**, dropping args like `-connect=` and
  stranding it with the wrong config -- hung a real CI run for hours before its
  own timeout caught it. Fixed by persisting every env var any `docker compose`
  call depends on (`GENESIS_BLOCK_WITH_SIG`/`SEEDER_IP`/`CORE_*_ARGS`) to
  `$GITHUB_ENV` once, in `verify_seeder.py`, rather than patching each call site.
- **A `PendingChange` (`generate_traffic.py`) used to be able to look "never
  confirmed" and get dropped even though it actually did confirm on-chain** --
  surfaced several ways across real runs: crediting the ledger before
  confirmation instead of after; a bare `_last_credited_height` assignment
  that a transient height regression could rewind; a chaos node's own restart
  hiding a self-broadcast tx from its own `gettransaction` view; and plain
  senders never getting more than two confirmation attempts. The fixed-attempt
  check/settle/grace design that grew out of chasing each of those (three
  attempts, plus a `CHAOS_SENDER_GRACE_NODES` tip-sync check before the final
  one) was itself later replaced outright -- see the settle-loop entry below.
- **`_seed_ledger_with_current_balances` anchored the seeded ledger to
  `min()` across nodes, not `max()`** -- the same double-credit failure mode
  as above, a different path in: the consistency check only requires each
  node's own height to be stable, not that every node agrees, so a chaos node
  sitting stably behind the tip could pass while still lagging, seeding the
  ledger at a lower height than its balances already reflected. Fixed with
  `max()`: a block is always submitted to its earner's own RPC node first,
  and the 3 earners are never chaos-restarted, so every height up to the
  snapshot's max is already folded into that earner's seeded balance.
- **The check/settle/grace design above was itself still capable of a false
  mismatch, traced to a real CI failure (`core-2a` off by exactly +50 TPC at
  height 110, one run's only mismatch).** `_verify_round(height)` trusted a
  `height` captured once, earlier, by whichever `_next_block_with_coinbase()`
  call computed it -- but real wall-clock time inside `_resolve_pending_changes`
  (RPC round-trips per pending change) could pass before `_verify_round`
  actually read balances. `getbalance()` reflects the chain *at query time*,
  so if another block landed in that gap, the real wallet already reflected
  its coinbase while the ledger, credited only through the older `height`,
  did not -- the before/after retry only guarded against the height moving
  *during* its own read, never against it having already moved before the
  read started, so a fully self-consistent read could still be compared
  against a stale ledger and pass as a real mismatch. Confirmed via container
  logs: height 111 (`core-2a`'s own next block, empty, `fees: 0`) landed one
  second before the settle check ran -- 50 TPC is exactly a fee-less coinbase.
  Fixed by replacing the whole check/settle/grace/verify sequence with one
  loop (`_settle_and_verify`/`_settle_pending`, both built on the shared
  `_advance_ledger_and_resolve`): every pass re-reads each node's real height,
  requires all 7 to agree before trusting it, and re-syncs coinbase crediting
  to that height immediately before comparing. Retrying past an apparent
  mismatch is now conditioned on *why* it might still resolve (a send still
  unconfirmed in some node's mempool, or nodes not yet converged) rather than
  a fixed attempt count; a mismatch with nothing pending and every node
  already converged is real and gets reported immediately. Also generalizes
  the old `CHAOS_SENDER_GRACE_NODES`-only `_wait_for_sender_sync` safeguard to
  run on every settle pass, not just a final grace block.
- **The settle loop above could itself die on a misleading convergence error
  instead of producing its own final diagnostics, caught on review before a
  live failure.** `_advance_ledger_and_resolve` took the caller's overall
  `SETTLE_TIMEOUT_SECONDS` deadline and passed it straight into
  `_wait_for_convergence` -- fine while there was still time left, but once
  that deadline had already passed (exactly the case for the final `final=True`
  pass, and for the very last non-final pass immediately before it),
  `_wait_for_convergence` checked convergence exactly once and, unless the 7
  nodes happened to agree at that precise instant, raised
  `TrafficGenerationError("node heights never converged while settling")` --
  which propagates straight out of `run()`, replacing the mismatch-list/
  dropped-pending-change logging this pass exists to produce with a
  convergence error that misdescribes what actually happened. Fixed by no
  longer conflating "how long to wait for the 7 nodes to agree on a height
  this one pass" with "how long to keep retrying past a mismatch overall" --
  `_advance_ledger_and_resolve` now always gives `_wait_for_convergence` its
  own fresh `HEIGHT_POLL_TIMEOUT_SECONDS` window regardless of which pass it
  is, and the outer `SETTLE_TIMEOUT_SECONDS` deadline is used only to decide
  whether a given pass should be the final one.
- **`core-3b`, not just `core-7`, can legitimately see group A's abandoned fork
  after a reorg reconnect.** Two propagation paths matter, not just P2P
  adjacency: a signer submits its own mastered block directly to its RPC
  target, and P2P relay runs along the static edges with `core-7` as the only
  bridge between the otherwise-disconnected node pairs -- so `core-3b` can pick
  up group A's fork as headers-only knowledge the same way `core-7` can.
  `simulate_reorg.py`'s convergence check now derives each node's expected tip
  shape from the actual topology, tightened to require any second tip
  specifically be group A's fork, not just tolerate any extra tip.
- **`round-duration=60` avoids transient `InvalidBlock` errors** around round
  boundaries that shorter durations (e.g. 10) hit. `round-duration=30` also
  verified clean.
- **Building `tapyrus-signer` outside Docker on macOS** also needs
  `brew install gmp` + `LIBRARY_PATH=/opt/homebrew/lib`, for the linker to find
  the system `libgmp`.
- **How `tapyrus-seeder` discovers nodes** (`scripts/verify_seeder.py`):
  1. DNS-seed crawling is a direct per-address liveness test (VERSION/VERACK +
     `getaddr`), not gossip.
  2. `GETADDR` only returns what the responding node's own addrman already has,
     and `-connect` targets are never added to it -- so in connect mode,
     crawling from one entry point can't find the other 6. Fixed by seeding
     every node's address directly instead of relying on gossip.
  3. `IsGood()` needs 3+ successes at least `MIN_RETRY` (60s) apart before a
     node is servable via DNS.
  4. The default thread count (96) scales idle-retry backoff up with it --
     turned down to `-t 2` for a small network.
  5. Both `tapyrus-seeder` and `tapyrus-core` gate usable addresses on
     `CNetAddr::IsRoutable()`; Docker's default bridge range and RFC 5737 both
     fail it. `51.51.51.0/24` is confirmed to pass.
  6. Which nodes end up "good" depends on bring-up mode: in connect mode,
     `core-7`'s `-connect` (no offsetting `-listen=1`) disables its listening,
     so it's the one node that never becomes good, seeded anyway as a
     deliberate negative case. In addseeder mode all 7 become good.
  7. A node with no `-connect` at all uses its addrman normally, so
     `-addseeder` alone genuinely grows its peer count from nothing.
     `-addseeder` with `-dnsseed` off is a fatal `InitError`, and
     `ThreadDNSAddressSeed` only runs once at startup, so a node's first
     attempt against a not-yet-converged seeder needs a restart to get real
     results.

  End state: connect mode converges on the 6 nodes that actually listen (never
  `core-7`); addseeder mode has all 7 discover each other organically.
