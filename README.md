# tapyrus-integration-tests

Weekly cross-repo integration test spanning `tapyrus-core`, `tapyrus-signer`, and
`tapyrus-seeder`. It stands up a real 3-signer/threshold-2 federation over a 7-node
`tapyrus-core` topology using the actual `tapyrus-setup` ceremony (no faked signatures),
then drives that live network through the operations a real federation performs:
per-node transactions, node lifecycle (stop/restart/resync), a genuine chain reorg,
aggpubkey rotation, and a max-block-size change. See
[`doc/weekly-integration-test-plan.md`](doc/weekly-integration-test-plan.md) for the full
design and rationale, and [`doc/project-plan.md`](doc/project-plan.md) for what's
implemented vs. still outstanding.

## Repo layout

- `.github/workflows/weekly-integration-test.yml` -- the CI workflow (see below).
- `config/repos.py` -- default checkout URL/ref for each upstream repo.
- `scripts/` -- the ceremony + config-assembly scripts the workflow calls. See
  [`doc/scripts.md`](doc/scripts.md) for what each one does.
- `docker/docker-compose.yml` -- the 7-core-node + redis + 3-signer + seeder stack.
- `secrets/`, `runtime/`, `workdir/` -- generated at run time (gitignored), never committed.
- `doc/` -- design doc, progress tracker, script reference

## CI workflow overview

The workflow (`weekly-integration-test.yml`) has two trigger shapes:

- **`schedule`** (Sunday 03:00 UTC / Sunday 12:00 noon JST) and **`workflow_dispatch`**
  (on demand) run the full-scale scenario -- the defaults in the variable table below.
- **`pull_request`** and **`push`** to `main` (path-filtered to `scripts/**`,
  `docker/**`, `config/**`, `.github/workflows/**`) run the same job at a smaller
  "smoke" scale, so a change to this repo's own scripts/compose/workflow is validated
  before the following Sunday's run rather than after. See `doc/work-done.md` for why.

A single `integration-test` job runs these steps in order:

1. **Checkout this repo**, then checkout `tapyrus-core` + `tapyrus-signer` +
   `tapyrus-seeder` (`scripts/checkout_repos.py`) -- each repo's ref is independently
   configurable, see below.
2. **Build Docker images** for `tapyrus-core`, `tapyrus-signer`, and `tapyrus-seeder`
   from the checkouts.
3. **Offline ceremony for signer-set-a**: build the `tapyrus-setup` binary, then run
   `scripts/generate_dev_secrets.py` to produce the aggregated public key (no containers
   involved yet).
4. **Genesis signing**: build the unsigned genesis via `tapyrus-genesis`, then sign it
   with `scripts/sign_genesis.py`.
5. **Render `tapyrus.conf`, bring up `redis`.**
6. **Bring up `tapyrus-seeder` and verify it** (`scripts/verify_seeder.py`) -- also
   brings up the 7 core-* nodes itself, in two bring-up modes in sequence: first
   addseeder-only (no `-connect` at all), confirming every node's peer count grows
   organically from nothing via the seeder's DNS-seed answers alone; then the fixed
   `-connect` topology every later step below depends on, confirming the seeder
   reports only genuinely-listening nodes (never `core-7`, the one node that doesn't
   listen in that mode) and that a brand-new 8th node with no topology knowledge of
   its own genuinely auto-bootstraps through the seeder's DNS answer alone.
7. **Start the node orchestrator** (`scripts/start_node_orchestrator.py`) -- switches
   the 7 core-* nodes into chaos-supervised mode. core-1b/2b/3b/core-7 randomly
   stop/restart/reindex/invalidate themselves for the rest of the job;
   core-1a/2a/3a (the signers' own RPC targets) get crash-recovery supervision but
   never a deliberate chaos action, since disrupting them risks throwing off
   `generate_traffic.py`'s coinbase-rotation tracking (see `doc/work-done.md`).
8. **Wait for the P2P topology to converge** (`scripts/wait_for_topology.py`, polling
   `getconnectioncount` against the expected 1/2/1/2/1/2/3 pattern) against the
   now-finalized fixed topology, then collect a coinbase address from each
   first-layer node (`scripts/collect_coinbase_addresses.py`, retries until each
   node's RPC is actually up).
9. **Bring up signers**: assemble each signer's config with
   `scripts/assemble_signer_configs.py`, bring up the 3 signer-set-a containers.
10. **Per-node activity**: round-robin TPC + colored-coin traffic across all 7 nodes
    with balances confirmed after each block (`scripts/generate_traffic.py`),
    including the 3 first-layer nodes' coinbase income -- calibrated from 3 real,
    consecutive height observations early in the run (see `doc/work-done.md`), not
    just excluded from the assertion. Runs continuously against the node
    orchestrator's background chaos from step 7 onward.
11. **Reorg**: split the network into two groups, let each build its own real
    threshold-signed fork, reconnect, and confirm convergence via `getchaintips`
    (`scripts/simulate_reorg.py`).
12. **Aggpubkey rotation**: run the offline ceremony again for signer-set-b, then the
    `--xfield` sign/computesig handoff and a `federations.toml` with both entries
    (`scripts/simulate_federation_change.py`).
13. **Max block size change**: signer-set-b signs off on a new max-block-size via the
    same `--xfield` flow, confirmed in effect via RPC at the scheduled height
    (`scripts/simulate_maxblocksize_change.py`).
14. **Teardown**: collect every container's logs, upload them as a CI artifact, then
    `docker compose down` -- runs unconditionally (`if: always()`).

The entire scenario above has run successfully end-to-end in real GitHub Actions CI,
not just locally -- see `doc/work-done.md`'s "Full real-CI end-to-end verification".
See [`doc/project-plan.md`](doc/project-plan.md)'s Outstanding work for what's still
untested or unbuilt.

## Variables available to change during a run

Every variable below is a `workflow_dispatch` input. `workflow_dispatch` input
`default:` fields can't hold an expression, and `schedule`/`pull_request`/`push` runs
have no `inputs` context to read one from anyway, so no default is set on the inputs
themselves. Two different mechanisms supply one when a field is left blank (a manual
dispatch run that leaves a field blank gets the same value schedule does either way):

- `core_repo_ref`/`signer_repo_ref`/`seeder_repo_ref` fall back to
  [`config/repos.py`](config/repos.py)'s own default for that repo -- the single
  source of truth for these three, not restated here (see that file for the current
  values and why).
- Every other variable below falls back to a literal in the workflow's `env:` block
  (`inputs.x || 'literal'`), shown in the table below. `pull_request`/`push` runs use
  smaller "smoke" values for the reorg variable instead of the full-scale default
  below (see the workflow's `env:` block). This table is the one place those numbers
  are written down -- each input's own `description:` field just points back here
  instead of restating them, so the two can't silently drift apart.
  
Only variables with a wired-in consuming step get an actual `workflow_dispatch`
input; the rest fall back straight to their `env:` literal on every trigger,
`workflow_dispatch` included, with no way to override per-run yet.

| Variable | Default | Controls |
| --- | --- | --- |
| `core_repo_ref` | see `config/repos.py` | `tapyrus-core` branch/tag/sha to check out |
| `signer_repo_ref` | see `config/repos.py` | `tapyrus-signer` branch/tag/sha to check out |
| `seeder_repo_ref` | see `config/repos.py` | `tapyrus-seeder` branch/tag/sha to check out |
| `tx_round_count` | `60` (`10` on pull_request/push) | Round-robin send/check/settle cycles `scripts/generate_traffic.py` runs -- each is 3 block-heights and 14 transactions (7 nodes x {TPC send, colored send-or-mint}), so this alone determines the tx/block/height totals for that step. Also determines the reorg's baseline height -- see below. Sized against the CI timing budget, see `doc/work-done.md` |
| `reorg_length` | `30` (`5` on pull_request/push) | Blocks each isolated group builds past the baseline, alone, before reconnecting at the tie (`scripts/simulate_reorg.py`: group B builds first while group A is stopped entirely, then group A builds its own, genuinely different set while group B is stopped). Group B is then always extended by exactly 2 more blocks to win (not 1 -- `core-3a` produced group A's original tip itself, so it needs a second block to reclassify that tip as `valid-fork` instead of `valid-headers`) -- not configurable, and not probed for -- see `doc/scripts.md` |
| `round_duration_seconds` | `30` | `tapyrus-signerd` round-duration (block interval) -- verified clean, see `doc/work-done.md`'s Lessons learnt (`10` is confirmed to hit transient `InvalidBlock` errors) |
| `network_id` | `1905960821` | Tapyrus network id (prod mode, see `doc/work-done.md`), used by every core-* node's rendered `tapyrus.conf` and the `genesis.<id>` file `tapyrusd` looks for |
| `docker_build_platform` | *(empty, runner-native)* | Docker `--platform` for image builds -- local verification only ever used `linux/arm64` |

`FEDERATION_CHANGE_HEIGHT` is always `REORG_LENGTH` (computed by the "Derive
FEDERATION_CHANGE_HEIGHT from REORG_LENGTH" workflow step), and `MAX_BLOCK_SIZE_HEIGHT`
is always `FEDERATION_CHANGE_HEIGHT` in turn -- `scripts/simulate_federation_change.py`
and `scripts/simulate_maxblocksize_change.py` each schedule their change that many
blocks past whatever height the chain is at when they run, not a fixed literal.
`max_block_size_new` (`2000000`) is env-literal only, no per-run `workflow_dispatch`
override yet.

**No input at all, and no env-literal fallback either** (not just not-yet-wired --
gone entirely): `tx_total_count`, `tx_tpc_percent`, `tx_interval_seconds`. Earlier
drafts of `scripts/generate_traffic.py` took independent knobs for total tx count,
the TPC/colored-coin split, and per-send pacing; the script settled on a single
`tx_round_count` knob instead (see `doc/work-done.md`'s "`generate_traffic.py`'s
round-count-only design"), so these three no longer correspond to anything the script
reads -- not obsolete inputs waiting for a slot, just dead names.

**No input at all** (not even an env-literal fallback): `chain_height_before_reorg`.
`CHAIN_HEIGHT_BEFORE_REORG` is always `TX_ROUND_COUNT + 2` (computed by the "Derive
CHAIN_HEIGHT_BEFORE_REORG from TX_ROUND_COUNT" workflow step, since `env:` block
expressions have no arithmetic operators) -- ties the reorg's baseline directly to
whatever `scripts/generate_traffic.py` actually produces first in the same job,
rather than an independently-configured value that could silently drift out of sync
with it. `scripts/simulate_reorg.py` treats this as a floor, not a literal target: it
waits until the chain reaches at least that height, then uses whatever height was
*actually* reached (which is typically well past the floor, since traffic generation
runs first) as the real reference point for both forks' target -- see
`doc/work-done.md` for why that distinction matters.

Same treatment for `federation_change_height` and `max_block_size_height`:
`FEDERATION_CHANGE_HEIGHT` is always `REORG_LENGTH` (computed by the "Derive
FEDERATION_CHANGE_HEIGHT from REORG_LENGTH" workflow step), and `MAX_BLOCK_SIZE_HEIGHT`
is always `FEDERATION_CHANGE_HEIGHT` in turn -- `scripts/simulate_federation_change.py`
and `scripts/simulate_maxblocksize_change.py` each schedule their change this many
blocks past whatever height the chain is at when *they* run (already well past the
reorg/rotation and another traffic round by then), so there's no independently-meaningful
absolute value to expose for either; tying each to the previous step's own height
variable keeps them in the same ballpark instead of separately-tuned literals that
could silently drift apart.

Not configurable per-run at all (hardcoded): `max_block_size_new` (`2000000`, the new
value `scripts/simulate_maxblocksize_change.py` pushes -- no per-run override yet),
the RPC port/user/pass (`12381` / `rpcuser` / `rpcpassword`), the signer count (3) /
threshold (2) -- the 7-node topology in `docker/docker-compose.yml` is wired 1:1 to
exactly 3 signers, so changing the count means redesigning the topology, not just
passing a different number -- and
`prng_seed_base` (always `github.run_id`): `doc/weekly-integration-test-plan.md`
requires the PRNG be seeded deterministically per run so a failure is reproducible,
so this is deliberately never a per-run override, not just an as-yet-unwired one.

## Developer notes

Conventions the scripts in `scripts/` follow, so a new one stays consistent with the
rest -- see [`doc/scripts.md`](doc/scripts.md) for what each script actually does, and
[`doc/work-done.md`](doc/work-done.md) for the reasoning behind each of these.

- **Uniform logging.** Every script imports `scripts/lib/log.py`'s `log` and uses
  `log.step/info/warn/error` for its own narration -- never a bare `print()`. Every
  line is timestamped (UTC, numeric-only, locale-independent) and leveled, so output
  reads the same way regardless of which script produced it. This is separate from
  container log collection (the workflow's "Collect logs" step, which pulls each
  container's own log via `docker logs`) -- that captures what tapyrus-core/
  tapyrus-signer/tapyrus-seeder themselves logged; `log.py` captures what *this
  repo's own orchestration* did.
- **Async, not blocking subprocess calls + polling loops.** Every script that does
  subprocess or network I/O is `asyncio`-based (`asyncio.create_subprocess_exec` for
  external binaries, `asyncio.to_thread`-wrapped `urllib` for RPC calls -- stdlib has
  no native async HTTP client). Independent operations run concurrently via
  `asyncio.gather` rather than looping one at a time: checking out 3 repos, each
  ceremony step's N signer calls, polling all 7 nodes' topology. The one exception is
  `assemble_signer_configs.py`, which is pure local file I/O with nothing to overlap,
  so it stays a plain synchronous script.
- **All variables configurable at the CI level.** Anything a script needs that a CI
  run might reasonably want to vary is a `workflow_dispatch` input with a matching
  `env:` entry (the table above), not hardcoded inside the script. Scripts read these
  via `os.environ.get(NAME, local_default)`, so the same script works unmodified
  whether it's invoked by the workflow (env var set) or run by hand locally (falls
  back to a sane default). `config/repos.py` follows the same pattern for the three
  upstream repos' checkout targets.
- **Python stdlib only, no third-party dependencies, so far.** Nothing in `scripts/`
  has needed anything beyond the standard library (`asyncio`, `urllib`, `argparse`,
  `pathlib`, etc.) -- no `requirements.txt`, no virtualenv/package manager setup for
  CI to install before running these scripts. Worth reconsidering only if a future
  script genuinely needs something stdlib can't do reasonably.

## Where to look next

- [`doc/project-plan.md`](doc/project-plan.md) -- tracked progress, what's done vs. outstanding.
- [`doc/scripts.md`](doc/scripts.md) -- what each script in `scripts/` does, its inputs/outputs, and known gotchas.
- [`doc/weekly-integration-test-plan.md`](doc/weekly-integration-test-plan.md) -- the full scenario design and architecture rationale.
- [`doc/work-done.md`](doc/work-done.md) -- transcript of everything manually verified so far (ceremony, topology, reorg, seeder fixes).
