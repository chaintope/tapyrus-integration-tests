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
2. **Build Docker images** for `tapyrus-core` and `tapyrus-signer` from the checkouts;
   build the `tapyrus-seeder` image *(not yet scripted)*.
3. **Offline ceremony for signer-set-a**: build the `tapyrus-setup` binary, then run
   `scripts/generate_dev_secrets.py` to produce the aggregated public key (no containers
   involved yet).
4. **Genesis signing**: build the unsigned genesis via `tapyrus-genesis` *(not yet
   scripted)*, then sign it with `scripts/sign_genesis.py`.
5. **Bring the network up**: `docker compose up` redis + the 7 core nodes, then wait
   for the P2P topology to converge (`scripts/wait_for_topology.py`, polling
   `getconnectioncount` against the expected 1/2/1/2/1/2/3 pattern), then collect a
   coinbase address from each first-layer node (`scripts/collect_coinbase_addresses.py`,
   retries until each node's RPC is actually up).
6. **Bring up signers and per-node activity**: assemble each signer's config with
   `scripts/assemble_signer_configs.py`, bring up the 3 signer-set-a containers,
   round-robin TPC + colored-coin traffic across all 7 nodes with balances confirmed
   after each block (`scripts/generate_traffic.py` -- built and verified against a
   live stack, see `doc/work-done.md`), RPC health/height/mempool queries, and
   stop/restart/resync for every node, plus the max-block-size (xfield) change
   *(the query/lifecycle/max-block-size pieces are still not yet scripted -- and the
   whole step is currently commented out in the workflow, signers included, pending
   Milestone 3/4; see `doc/project-plan.md`)*.
7. **Reorg**: split the network into two groups, let each build its own real
   threshold-signed fork, reconnect, and confirm convergence via `getchaintips`
   *(recipe verified by hand -- see `doc/work-done.md`; not yet scripted as a reusable
   step)*.
8. **Aggpubkey rotation**: run the offline ceremony again for signer-set-b, then the
   `--xfield` sign/computesig handoff and a `federations.toml` with both entries
   *(not yet scripted)*.
9. **Confirm the rotation** took effect at the scheduled height *(not yet scripted)*.
10. **Bring up tapyrus-seeder** and confirm it resolves a real peer via `dig`
    *(image is built, but the service itself is never started -- not yet scripted)*.
11. **Teardown**: collect every container's logs, upload them as a CI artifact, then
    `docker compose down` -- runs unconditionally (`if: always()`).
12. **Slack report**: pass/fail summary, run metadata, both aggpubkeys, and (on failure)
    the implicated container's tail log, sent unconditionally *(not yet scripted)*.

Steps marked "not yet scripted" currently just `echo` a TODO pointing at the relevant
design-doc section -- see [`doc/project-plan.md`](doc/project-plan.md) for the tracked
list of what's left.

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
  smaller "smoke" values for the reorg variables instead of the full-scale defaults
  below (see the workflow's `env:` block).

GitHub Actions caps `workflow_dispatch` at 10 inputs (exceeding it invalidates the
whole workflow file, for every trigger -- see `doc/work-done.md`), so only variables
with a wired-in consuming step get an actual input; the rest fall back straight to
their `env:` literal on every trigger, `workflow_dispatch` included, with no way to
override per-run yet.

| Variable | Default | Controls |
| --- | --- | --- |
| `core_repo_ref` | see `config/repos.py` | `tapyrus-core` branch/tag/sha to check out |
| `signer_repo_ref` | see `config/repos.py` | `tapyrus-signer` branch/tag/sha to check out (override to `163_federationChangeToml` on the `Naviabheeman` fork to test rotation) |
| `seeder_repo_ref` | see `config/repos.py` | `tapyrus-seeder` branch/tag/sha to check out |
| `chain_height_before_reorg` | `30` | Baseline height to reach (all 7 nodes connected) before splitting for the reorg |
| `reorg_loser_blocks` | `10` | Blocks the losing group builds past the baseline |
| `reorg_winner_margin` | `2` | Extra blocks the winning group builds beyond `reorg_loser_blocks` (winner total = loser + margin, so a tie/shorter-winner is structurally impossible) |
| `round_duration_seconds` | `60` | `tapyrus-signerd` round-duration (block interval) -- 60s avoids the `InvalidBlock` timing race a shorter duration hits, see `doc/work-done.md` |
| `network_id` | `1905960821` | Tapyrus network id (prod mode, see `doc/work-done.md`), used by every core-* node's rendered `tapyrus.conf` and the `genesis.<id>` file `tapyrusd` looks for |
| `slack_log_tail_lines` | `100` | Lines of the implicated container's log inlined in the Slack failure report |
| `docker_build_platform` | *(empty, runner-native)* | Docker `--platform` for image builds -- local verification only ever used `linux/arm64` |

Not yet real inputs (env-literal only, no per-run override -- see above): `tx_total_count`
(`20`), `tx_tpc_percent` (`30`), `tx_interval_seconds` (`30`), `tx_round_count` (`5`,
`scripts/generate_traffic.py`'s round-robin send/check/settle cycle count -- each is 3
block-heights and 14 transactions, 7 nodes x {TPC send, colored send-or-mint}, so this
alone determines the tx/block/height totals for that step), `rotation_height_offset`
(`10`), `max_block_size_new` (`2000000`), `prng_seed_base` (`github.run_id`). All feed
steps that are still commented-out/TODO (Milestone 3/4) -- `generate_traffic.py`
itself is fully built and verified (see `doc/work-done.md`), but its step is disabled
along with the rest of the signers-never-brought-up block for now. Reintroduce as real
inputs once a step lands for real, if the 10-input budget allows, or via a JSON
"overrides" input / repo vars otherwise.

Not configurable per-run at all (hardcoded): the RPC port/user/pass (`12381` /
`rpcuser` / `rpcpassword`), and the signer count (3) / threshold (2) -- the 7-node
topology in `docker/docker-compose.yml` is wired 1:1 to exactly 3 signers, so changing
the count means redesigning the topology, not just passing a different number.

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
  script (e.g. tx generation, the Slack report) genuinely needs something stdlib can't
  do reasonably.

## Where to look next

- [`doc/project-plan.md`](doc/project-plan.md) -- tracked progress, what's done vs. outstanding.
- [`doc/scripts.md`](doc/scripts.md) -- what each script in `scripts/` does, its inputs/outputs, and known gotchas.
- [`doc/weekly-integration-test-plan.md`](doc/weekly-integration-test-plan.md) -- the full scenario design and architecture rationale.
- [`doc/work-done.md`](doc/work-done.md) -- transcript of everything manually verified so far (ceremony, topology, reorg, seeder fixes).
