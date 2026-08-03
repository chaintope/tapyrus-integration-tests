# Known issues, gotchas, design decisions

Consolidated record of open problems, gotchas, and deliberate design choices behind
this repo's scripts, CI workflow, and compose stack. Referenced throughout the
code/config as "see doc/work-done.md" rather than carrying this narrative in comments
themselves -- this file is the one place to look for the *why* behind anything that
looks non-obvious.

See [`weekly-integration-test-plan.md`](weekly-integration-test-plan.md) for the
original scenario design, [`project-plan.md`](project-plan.md) for tracked
done-vs-outstanding progress (including the full list of what's not yet built or
tested), and [`scripts.md`](scripts.md) for what each script does.

## Known issues (open)

- **CI timing budget is bounded by GitHub-hosted's 6h hard cap**, which can't be
  raised past regardless of `timeout-minutes`. Block production only comes from the
  live signer round-robin at `ROUND_DURATION` cadence (no instant-mining shortcut),
  so scenario time is `blocks x ROUND_DURATION` -- `tx_round_count`/`reorg_length`/
  `round_duration` need to stay sized against that.
- **`docker/docker-compose.yml` has no compose-level healthchecks** /
  `depends_on: condition: service_healthy` yet (plan doc section 3 step 6 guidance).
  `scripts/wait_for_topology.py` is a CI-level equivalent -- arguably stronger, since
  it confirms real P2P peer counts rather than just RPC reachability, and its own
  mismatch/timeout-reporting logic is now confirmed working (see `project-plan.md`) --
  but the compose-file enhancement itself is separate, unstarted work.
- **Signer count (3) / threshold (2) is hardcoded**, not a per-run variable -- the
  7-node topology in `docker-compose.yml` is wired 1:1 to exactly 3 signers; changing
  the count means redesigning the topology, not just passing a different number.
- **Scenario mechanics not yet built** (see `project-plan.md`'s Outstanding work for
  the tracked list): per-node lifecycle orchestrator.
- **`scripts/generate_traffic.py`'s hardcoded constants worth revisiting eventually**:
  `FUNDING_AMOUNT_TPC`/`TOKEN_ISSUE_AMOUNT`/etc. were picked conservatively and work
  fine at small round counts, but haven't been stress-tested at a larger round count
  where the balance-shortfall top-up mechanic would trigger much more often.

## Design decisions

- **All scripts are Python** (stdlib only, no third-party dependencies) -- class-based
  (`RepoCheckout`, `AggpubkeyCeremony`, `GenesisSigningCeremony`,
  `SignerConfigAssembler`, `TopologyWaiter`), sharing a uniform logger
  (`scripts/lib/log.py`) and a common ceremony base class (`scripts/lib/ceremony.py`,
  `TapyrusSetupCeremony`) rather than duplicating
  `_run_setup`/`extract_vss_for`/`require_executable` across scripts, and a shared
  `docker compose` service-control module (`scripts/lib/compose.py`) rather than each
  script owning its own subprocess helper.
- **Reorg mechanic**: genuine network isolation, within the current federation (no
  rotation involved) -- `scripts/simulate_reorg.py` drives this via strict
  alternation: only one group's core nodes are ever up and building at a time (never
  both, until the final reconnect), so the same signer set independently
  threshold-signs two genuinely different forks from a common tip, each isolated
  group unable to influence or observe the other's blocks while forking. See the
  script's own module docstring for the exact step sequence.
- **`tapyrus-genesis` invocation in CI**: runs via
  `docker run --rm --entrypoint tapyrus-genesis` against the already-built
  `tapyrus/tapyrusd:master-local` image, bypassing the image's own `entrypoint.sh`
  (which wraps the default CMD in `bash -c "$*"` and expects `GENESIS_BLOCK_WITH_SIG`
  for the long-running daemon). `tapyrus-genesis` is a stateless one-shot tool with
  none of that daemon machinery, and reusing the already-built image guarantees the
  unsigned genesis matches the exact tapyrus-core commit under test.
- **How `NETWORK_ID` reaches `tapyrusd`**: `tapyrus/tapyrusd`'s own `entrypoint.sh`
  only auto-generates a `tapyrus.conf` if none is mounted at
  `${CONF_DIR}/tapyrus.conf`, and separately greps `networkid=` out of whichever conf
  ends up there to decide which `${DATA_DIR}/genesis.<network_id>` file to write
  `GENESIS_BLOCK_WITH_SIG` into and which `tapyrusd` then loads.
  `scripts/render_tapyrus_conf.py` renders a prod-mode conf (no `-dev` on the
  `tapyrus-genesis` call either -- prod is simply what you get by omitting `-dev`)
  with `networkid=$NETWORK_ID`, mounted into all 7 core-* services. Not wired into the
  seeder's own `-i`/`-s` flags, which stay hardcoded to `1905960821` regardless of
  `NETWORK_ID` (see `docker-compose.yml`'s own `seeder` comment).
- **P2P topology relies on the chain's default port, not an explicit `port=`**:
  `docker/docker-compose.yml`'s `-connect=<service-name>` targets have no explicit
  port, and `tapyrus-core` resolves a portless `-connect` against the *chain's own
  default* P2P port (`CConnman::ConnectNode`), not whatever `port=` says in that
  node's own conf. Prod mode's default P2P port is `2357`. `render_tapyrus_conf.py`
  deliberately omits `port=` from the rendered conf so every node falls back to the
  same chain-default port `-connect` already resolves against.
- **`generate_traffic.py` needs `fallbackfee` enabled**: `estimatesmartfee` has no
  history on a brand-new chain, and `tapyrus-signerd`'s `-fallbackfee` default is
  disabled (0) -- a deliberate mainnet-safety convention, not a bug. Without it, every
  funding/send/issuance call fails with `-4 Fee estimation failed`. Fixed by adding
  `fallbackfee=0.0002` to `render_tapyrus_conf.py`'s rendered conf (safe to enable
  unconditionally here -- there's no real fee market on a throwaway dev chain to
  misjudge). Also sets `dbcache=64` (450MB default is pure overhead for 7 concurrent
  containers running a chain with a handful of blocks), `maxorphantx=20` (100 default
  is sized for a real internet-facing node; this network's only peers are the other 6
  fixed `-connect` targets, but kept above 0 since the reorg step can transiently
  orphan real transactions), and `mempoolexpiry=2` (hours; 336h/2-week default targets
  a long-running production node, not a several-hour CI job).
- **`generate_traffic.py`'s round-count-only design**: everything (block-height
  budget, transaction count) derives from one number, `tx_round_count` -- exactly one
  send per node per round, so the total is always `round_count * 14` (7 nodes x
  {TPC, colored}) by construction, with no separate total/interval knob to drift from
  that.
- **Colored-coin balance shortfall mints instead of skipping**: each node only ever
  holds a small working balance (`TOKEN_ISSUE_AMOUNT`/`TOKEN_TOPUP_AMOUNT` = 3). A
  round where the sender doesn't have enough issues/reissues more instead of
  transferring -- still exactly one transaction for that node, so total tx count per
  round stays fixed at 14 regardless of how many nodes hit a shortfall. NFT nodes
  (forced to `value=1`) mint a fresh NFT color on almost every other round as a
  result -- expected, not a bug: they transfer their one unit away, then have zero
  until the next mint.
- **Top-up timing needs no cross-round lookahead**: `issuetoken`'s NON_REISSUABLE/NFT
  path selects its input UTXO explicitly (`coin_control.Select(out)`) and only checks
  `IsMine(...) == ISMINE_SPENDABLE` -- no confirmation-depth check -- so a same-round
  top-up-then-spend chaining onto its own unconfirmed output works fine.
- **Per-transaction fee tracked via `gettransaction`, not predicted**: the ledger's
  TPC bookkeeping reads each transaction's actual fee back from `gettransaction`
  right after broadcasting it, rather than predicting it from a fee rate and assumed
  tx size -- sidesteps needing tx vsize to stay near-constant across this script's
  several different transaction shapes.
- **`generate_traffic.py` seeds its ledger from each node's real on-chain balance**
  at startup (`getbalance`) -- the workflow runs this script multiple times in the
  same job (before and after the reorg/rotation steps), so a node can genuinely
  carry a non-zero balance into a later invocation.
- **`core-1a`/`2a`/`3a`'s TPC balance can't be predicted by the traffic-generation
  ledger, and that's expected, not a bug**: these 3 nodes are each credited a fresh
  50 TPC `"generate"`-category transaction every time they propose a block as that
  round's federation master, entirely independent of anything `generate_traffic.py`
  does. Excluded from the exact-ledger TPC assertion; their colored balances (never
  coinbase-derived) stay fully asserted, and their TPC is still logged for
  visibility.
- **`computesig` array-length requirement**: `--sig`/`--block-vss`/`--node-vss`
  arrays must all be the full signer count (not just `threshold`) -- enforced by an
  `assert_eq!` in the source. `computesig` must also be run with one specific
  signer's own key material (hardcoded to signer-0 in this repo's scripts) -- it
  can't be run by a neutral party without borrowing a signer's secrets, so a
  "designated signer" is a real v1 limitation, not an oversight.
- **`tapyrus-signerd` needs a *live* RPC connection to its own configured core node
  to participate in a signing round at all** -- even purely as a non-master over
  Redis. With a signer's core-node RPC target down, it fails whether or not it's
  master that round. There is no Redis-only fallback -- this is why every signer in
  the reorg recipe's "losing" group gets repointed to that group's one surviving core
  node before it can sign at all.
- **`sign`/`computesig` also accept `--xfield` as an alternative to `--block`**,
  signing an aggpubkey rotation or max-block-size change instead of a genesis block --
  the actual mechanism behind the federation-rotation design.
- **`tapyrus-core` auto-disables listening the instant `-connect` is set at all**
  (`InitParameterInteraction: -connect set -> setting -listen=0`), not just
  restricting outbound dialing. Every `-connect` edge needs exactly one `-connect`
  side (the "child" dials its "parent"), with an explicit `-listen=1` added back
  wherever a node also needs to accept an inbound edge.
- **`tapyrus/tapyrusd`'s entrypoint does `exec bash -c "$*"`** against the image's
  default CMD, and docker-compose's `command:` *replaces* that default CMD rather
  than appending to it -- every `command:` override repeats the full default
  invocation and appends its own flags.
- **`tapyrus-setup createkey` always produces mainnet-prefixed WIFs** (`K`/`L`,
  `0x80`), unlike tapyrus-core's own `-dev` network convention (`c` prefix, `0xef`).
- **A benign warning, any `round-duration`**: a non-master signer's own `submitblock`
  call occasionally races a block another signer already submitted, and
  tapyrus-core's `"duplicate"` string response doesn't match what the Rust client's
  JSON deserializer expects (`invalid type: string "duplicate", expected unit`).
  Harmless -- the block was already accepted through the other path.
- **`createnodevss`/`createblockvss` output ordering**: output lines
  (`<receiver_pubkey>:<vss_hex>`) are sorted by receiver pubkey (BTreeMap iteration in
  the Rust source), NOT by `--public-key` argument order -- extracting by line
  position instead of matching the actual pubkey fails with an opaque `InvalidSS`
  error from `tapyrus-setup` itself.
- **`getnewaddress` right after `docker compose up -d` needs a retry, not a single
  shot**: a container reported "running" only means the process started, not that
  `tapyrusd`'s RPC server has finished initializing. `scripts/collect_coinbase_addresses.py`
  retries each node via `lib/rpc.py`'s `RpcUnreachable` until it answers (or times out
  loudly) and raises on an empty address instead of writing one.
- **`RPC_IN_WARMUP` (-28) is a real, common state to retry through, not just
  connection-refused/timeout**: `tapyrusd` serves `-28` as HTTP 500 with a JSON-RPC
  error body, which is exactly the readiness window right after `docker compose up`.
  `lib/rpc.py` parses the `HTTPError`'s JSON body and treats `error.code == -28` as
  retryable (`RpcUnreachable`) same as connection-refused, while still raising
  `RpcError` (with the JSON-RPC error code/message included) for every other HTTP
  error. A bad-credentials 401 has no response body at all
  (`HTTPReq_JSONRPC`'s auth-failure path calls `WriteReply` with no body argument) --
  doesn't crash the JSON parse.
- **`workflow_dispatch` inputs have no `default:`**: `workflow_dispatch.inputs.*.default`
  can't hold an expression, and `schedule`/`pull_request`/`push` runs have no `inputs`
  context at all -- so every default lives in the `env:` block's `inputs.x || 'literal'`
  fallback instead (the one place each is written, avoiding the drift risk of writing
  it twice), with each input's `description:` stating its default in words. A manual
  dispatch run left blank resolves to the exact same value `schedule` uses.
- **`pull_request`/`push` smoke trigger, scoped to this repo's own changes**: a change
  to `scripts/**`, `docker/**`, `config/**`, or the workflow itself is validated before
  merge, not just discovered on the following Sunday's scheduled run -- a different
  concern from `weekly-integration-test-plan.md` section 6's "not on every PR"
  non-goal (that's about the full-scale scenario against `tapyrus-core`/`tapyrus-signer`
  PRs, cost/runtime prohibitive). Runs the identical job at reduced scale, keyed off
  `github.event_name` since these triggers have no `inputs` context, and a shorter
  `timeout-minutes` (180 vs. 360).
- **`CHAIN_HEIGHT_BEFORE_REORG`/`FEDERATION_CHANGE_HEIGHT` are floors/offsets, not
  literal targets**: `CHAIN_HEIGHT_BEFORE_REORG` is always `TX_ROUND_COUNT + 2`, and
  `simulate_reorg.py`'s `_build_baseline` waits until the chain reaches at least that
  height, then uses whatever height was *actually* reached (typically well past the
  floor, since `generate_traffic.py` runs first in the same job) as the real
  reference point for both forks' target height. `FEDERATION_CHANGE_HEIGHT` is
  always `REORG_LENGTH`, and `simulate_federation_change.py` schedules its rotation
  that many blocks past whatever height the chain is at when *it* runs.
- **Git submodules**: `tapyrus-core` vendors `secp256k1` as a git submodule --
  `checkout_repos.py` runs `git submodule update --init --recursive` after every
  clone/update (harmless no-op for repos without submodules).
- **Python's stdout is fully block-buffered when not attached to a tty** (the normal
  case in CI) -- every log write in `scripts/lib/log.py` uses `flush=True` so output
  interleaves correctly with subprocess output.
- **Core network topology**: enforced entirely via `-connect=`, all 7 nodes on one
  flat Docker network, not segmented per edge.
- **`tapyrus-seeder` genuinely bootstraps a new node** (`scripts/verify_seeder.py`):
  a brand-new 8th node with no hardcoded topology knowledge discovers and connects to
  the network entirely through it. Container DNS handles peer discovery for the fixed
  7-node topology itself -- nothing there depends on the seeder.
- **Secrets scope**: this repo only ever generates local dev secrets
  (`generate_dev_secrets.py`); it never provisions real GitHub secrets. The only
  actual CI secret needed is the Slack webhook URL.

## Lessons learnt

- **`tapyrus-setup`'s offline `--xfield sign`/`computesig` rejected a fresh, otherwise
  valid signature with `InvalidSig` roughly half the time** -- confirmed via repeated
  isolated trials (2/6 accepted, all 3 verifying nodes always agreeing, so it was the
  signature itself, not a per-node view difference). Root cause: `Sign::format_signature`
  (`sign.rs`) encoded a signature's `v` point as only its x-coordinate, discarding the
  y-parity entirely, before it was re-parsed back into a point on the
  `federation_watcher.rs` verification side (`multi_party_signature_from_hex`) --
  whichever y the reconstruction assumed only matched the original half the time. (An
  earlier theory -- that the positive/negative Schnorr share selection in
  `crypto/vss.rs` is re-derived inconsistently across the separate `sign`/`computesig`
  process invocations -- was tested and ruled out: 60/60 fresh ceremony rounds against
  the real `tapyrus-setup` binary verified correctly, and that selection is a pure
  function of public VSS commitment data, identical across every process by
  construction.) This was a `tapyrus-signer` bug, not this repo's scripts; fixed
  upstream.
- **`round-duration=60` avoids transient `InvalidBlock`/"candidate block is not set"
  errors around round boundaries** that a shorter duration (e.g. 10) hits.
  `round-duration=30` also verified clean (zero `InvalidBlock` errors, real local run).

- **How `tapyrus-seeder` actually discovers nodes, and why it took several real,
  live-tested wrong turns to get a working setup** (`docker-compose.yml`'s `seeder`
  service, `scripts/verify_seeder.py`):

  1. **DNS-seed crawling isn't peer-to-peer gossip -- it's a direct, per-address
     liveness test.** `tapyrusseed`'s `-s <networkid>:<host>:<port>` flags (repeatable
     -- `main.cpp`'s parsing just appends each to a vector, confirmed from source) seed
     an initial candidate list. A pool of crawler threads (`-t`, default 96) each pull
     candidates from that list (`db.cpp`'s `CAddrDb::Get_`/`GetMany`) and directly
     TCP-connect + VERSION/VERACK handshake + `getaddr` each one (`tapyrus.cpp`'s
     `CNode::Run`/`TestNode`) -- a real connection attempt per address, not a
     lightweight ping.
  2. **Crawling from just one node (`core-1a`) doesn't work in this topology.**
     Confirmed live: `core-1a` completes a real handshake with the seeder and answers
     every other message, but never sends an `addr` response to its `getaddr`.
     Root cause, from `tapyrus-core`'s `net_processing.cpp`: the `GETADDR` handler
     replies from `connman->GetAddresses()` -- i.e. the responding node's OWN addrman
     -- and `core-1a`'s addrman is empty. Nodes in this fixed `-connect` topology never
     auto-add an inbound peer's source address, and with every node's own addrman
     similarly empty, there's nothing for the normal ADDR-relay gossip to ever
     propagate in the first place. Fixed by seeding the crawler directly from every
     node's own address (`-s` once per node) instead of relying on gossip/discovery
     to find the rest from one entry point.
  3. **A single successful test isn't enough to be servable via DNS.** `tapyrus-seeder`
     only serves addresses from its small, reliability-vetted "good" set once one
     exists per address -- `db.cpp`'s `CAddrInfo::IsGood()` requires 3+ successful
     tests, each at least `MIN_RETRY` (60s) apart. Before that threshold is reached for
     any address, `GetIPs_` falls back to handing out exactly one arbitrary address
     from `ourId` (which tracks every address ever ATTEMPTED, good or bad, not just
     successes) -- so a node that's expected to always fail (`core-7`, seeded
     deliberately as a real negative case -- it never listens in this topology, see
     `docker-compose.yml`'s "CONNECT/LISTEN DESIGN" note) can legitimately be that one
     arbitrary answer for a while, before the good set stabilizes. Confirmed via a
     debug-instrumented build of the real `tapyrus-seeder` binary (its own existing
     `// printf` diagnostics, just commented out -- uncommented, rebuilt, and watched a
     real GOOD/BAD/RECV trace for every seeded address).
  4. **The default thread count (96) actively fights small test networks.** With only
     6-7 addresses total, 96 crawler threads spend almost all their time idle, and
     `main.cpp`'s idle-retry backoff scales with thread count
     (`rand() % (500 * nThreads)`) -- at 96 threads that's up to ~48 real seconds of
     random sleep before a thread even looks for new work again, on top of `db.cpp`'s
     own 60s `MIN_RETRY` per address. Confirmed live this meant addresses sat idle far
     longer than necessary between retest attempts. Turned down to `-t 2` -- still far
     more concurrency than 6-7 addresses need, but with a idle-backoff ceiling of a few
     seconds instead of most of a minute.
  5. **Neither `tapyrus-seeder` nor `tapyrus-core` will ever treat a private/internal
     address as usable, full stop -- not slowly, not eventually, never.** Every
     container in this stack gets a Docker-bridge IP, and both binaries independently
     gate real behavior on `CNetAddr::IsRoutable()`: `tapyrus-seeder`'s own `IsGood()`
     requires it (so no address could ever leave the single-arbitrary-address fallback
     above and reach the real "good" set, confirmed live via the same debug build: all
     6 real nodes tested GOOD repeatedly, `goodId` stayed empty regardless of how many
     times or how long), and -- discovered only after fixing the above and still seeing
     zero results -- `tapyrus-core`'s own `CAddrMan::Add_` (`addrman.cpp`)
     unconditionally rejects non-routable addresses too, so even a brand-new node that
     successfully learned an address via `-addseeder` could never actually add it to
     its own addrman to connect to it (confirmed live: "N addresses found from DNS
     seeds" in that node's own log, yet zero peers, ever, no matter how long it
     waited). First attempted fix was `docker-compose.yml`'s default network subnet on
     `203.0.113.0/24` (RFC 5737 TEST-NET-3 -- the "obviously safe, this is what
     documentation/test examples use" range) -- confirmed live this does NOT work
     either: `tapyrus-core`'s `IsRoutable()` (`netaddress.cpp`) explicitly excludes
     `IsRFC5737()` too (an exclusion tapyrus-seeder's own, older/simpler `IsRoutable()`
     doesn't have, which is why step 3 above worked on that subnet while this step
     didn't). Reading every `Is*()` function `IsRoutable()` actually calls (RFC1918,
     RFC2544, RFC6598, link-local, loopback, RFC5737) confirmed there is no IANA
     special-purpose block left that both looks like a real address and passes this
     check -- so `docker-compose.yml`'s custom subnet (`51.51.51.0/24`) is simply an
     arbitrary, clearly-deliberate choice outside all of them, not an "official" test
     range. Safe regardless: this network is a fully isolated Docker bridge, NAT'd
     outbound and never actually reachable from or routed to the real internet.

  End state, all confirmed live on the `51.51.51.0/24` subnet: the seeder correctly
  converges on exactly the 6 genuinely-listening nodes (never `core-7`), and a
  brand-new 8th node configured with no `-connect` at all -- only `-addseeder` --
  genuinely discovers and connects to the live network through the seeder's DNS
  answer alone, confirmed via that new node's own `getpeerinfo`.

## Developer notes

- **Building `tapyrus-signer` outside Docker on macOS** also needs
  `brew install gmp` + `LIBRARY_PATH=/opt/homebrew/lib`, for the linker to find the
  system `libgmp` a transitive dependency wants (separate from the vendored
  `gmp-mpfr-sys` build).
- **Configuring the Slack webhook** (`scripts/send_slack_report.py`,
  `SLACK_WEBHOOK_URL`): Slack webhooks are per-app, not per-workspace, so this needs
  a one-time setup, not just a repo secret:
  1. Go to <https://api.slack.com/apps?new_app=1>, choose "From scratch", name the
     app (e.g. "tapyrus-integration-tests"), pick the workspace to install it into.
  2. In the app's settings sidebar: **Features -> Incoming Webhooks**, toggle
     **Activate Incoming Webhooks** on.
  3. **Add New Webhook to Workspace**, pick the channel it should post to, authorize.
  4. Copy the resulting URL from **Webhook URLs for Your Workspace**
     (`https://hooks.slack.com/services/…`) -- treat it as a secret, it lets anyone
     post to that channel.
  5. In this repo on GitHub: **Settings -> Secrets and variables -> Actions -> New
     repository secret**, name it `SLACK_WEBHOOK_URL`, paste the value. The workflow
     already reads it (`inputs.slack_webhook_url || secrets.SLACK_WEBHOOK_URL`) --
     no further code changes needed once the secret exists.
- **Why `docker-compose.yml`'s network uses `51.51.51.0/24`, specifically**: because
  it needs to pass `tapyrus-core`'s `CNetAddr::IsRoutable()` check (required for both
  the seeder to ever call an address "good" and for any node to ever add a
  DNS-seed-discovered address to its own addrman) -- and, somewhat counterintuitively,
  none of the ranges most people would reach for first (RFC1918 private ranges, or
  RFC 5737's "documentation/test" TEST-NET ranges) actually qualify, since
  `IsRoutable()` excludes all of them deliberately. `51.51.51.0/24` is just an
  arbitrary block confirmed to fall outside every exclusion -- see this file's
  Lessons learnt above for the full investigation and the complete exclusion list, if
  this ever needs to change (e.g. if `51.0.0.0/8` is ever needed for something else,
  pick a different octet and re-check it against that list, don't just guess).

## Full local end-to-end verification (Tier 3 test)

Verified for real, locally, with a fresh checkout, real Rust toolchain, real Docker
builds, and real containers (not simulated):

- Real checkout of all three repos
- Real `tapyrus-setup`/`tapyrus-signerd` build
- Real `tapyrus-core`, `tapyrus-signer`, and `tapyrus-seeder` Docker images built
  successfully
- Real 3-signer ceremony converged on one genuine aggpubkey
- Real genesis signed by that ceremony, loaded and validated by real `tapyrusd`
  (`Genesis Block [...] Loaded successfully`)
- Real 7-node + redis compose stack came up; `wait_for_topology.py` correctly
  converged against live containers
- Real signer network produced real threshold-signed blocks, zero `InvalidBlock`
  errors (`round-duration=60`), confirmed P2P relay to signer-less `core-7`
- Real two-sided reorg via `scripts/simulate_reorg.py`'s alternating-isolation
  recipe, confirmed via `getchaintips` (exact `branchlen`/active/valid-fork pattern
  on every node)
- Real round-robin TPC + colored-coin traffic via `scripts/generate_traffic.py`,
  balances confirmed against the tracked ledger
- Log collection and teardown steps both verified

## Full real-CI end-to-end verification

Beyond the local Tier 3 test above, the entire scenario has also run successfully in
real GitHub Actions CI (not simulated, not local) -- a `pull_request`-triggered smoke
run went through ceremony, 7-node bring-up, traffic generation, reorg, federation
change, max-block-size change, and traffic generation again afterward, all
successfully:
[run](https://github.com/chaintope/tapyrus-integration-tests/actions/runs/30790964795/job/91614204236).
This also confirms the smoke trigger's `inputs.<name> || (...)` fallback expressions
evaluate correctly on a real `pull_request` event, closing the one open question
local testing couldn't exercise (`on:` trigger behavior). It also settles the
GitHub-hosted `ubuntu-latest` runner's capacity for this workload -- 7 core nodes +
3 signers + redis ran concurrently on a real hosted runner with no resource
problems, so that no longer needs to be tracked as an open question.
