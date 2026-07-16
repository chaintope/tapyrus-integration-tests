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
- `config/repos.env` -- default checkout URL/ref for each upstream repo.
- `scripts/` -- the ceremony + config-assembly scripts the workflow calls. See
  [`doc/scripts.md`](doc/scripts.md) for what each one does.
- `docker/docker-compose.yml` -- the 7-core-node + redis + 3-signer + seeder stack.
- `secrets/`, `runtime/`, `workdir/` -- generated at run time (gitignored), never committed.
- `doc/` -- design doc, progress tracker, script reference

## CI workflow overview

The workflow (`weekly-integration-test.yml`) runs on a weekly schedule (Monday 03:00 UTC)
and on demand via `workflow_dispatch`. A single `integration-test` job runs these steps
in order:

1. **Checkout this repo**, then checkout `tapyrus-core` + `tapyrus-signer` +
   `tapyrus-seeder` (`scripts/checkout-repos.sh`) -- each repo's ref is independently
   configurable, see below.
2. **Build Docker images** for `tapyrus-core` and `tapyrus-signer` from the checkouts;
   build the `tapyrus-seeder` image *(not yet scripted)*.
3. **Offline ceremony for signer-set-a**: build the `tapyrus-setup` binary, then run
   `scripts/generate-dev-secrets.sh` to produce the aggregated public key (no containers
   involved yet).
4. **Genesis signing**: build the unsigned genesis via `tapyrus-genesis` *(not yet
   scripted)*, then sign it with `scripts/sign-genesis.sh`.
5. **Bring the network up**: `docker compose up` redis + the 7 core nodes, collect a
   coinbase address from each first-layer node, assemble each signer's config with
   `scripts/assemble-signer-configs.sh`, then bring up the 3 signer-set-a containers.
6. **Wait for topology convergence** -- poll each node's peer count against the expected
   pattern *(not yet scripted)*.
7. **Per-node activity**: transactions, RPC health/height/mempool queries, and
   stop/restart/resync for every node, plus the max-block-size (xfield) change
   *(not yet scripted)*.
8. **Reorg**: split the network into two groups, let each build its own real
   threshold-signed fork, reconnect, and confirm convergence via `getchaintips`
   *(recipe verified by hand -- see `doc/work-done.md`; not yet scripted as a reusable
   step)*.
9. **Aggpubkey rotation**: run the offline ceremony again for signer-set-b, then the
   `--xfield` sign/computesig handoff and a `federations.toml` with both entries
   *(not yet scripted)*.
10. **Confirm the rotation** took effect at the scheduled height *(not yet scripted)*.
11. **Teardown**: collect every container's logs, upload them as a CI artifact, then
    `docker compose down` -- runs unconditionally (`if: always()`).
12. **Slack report**: pass/fail summary, run metadata, both aggpubkeys, and (on failure)
    the implicated container's tail log, sent unconditionally *(not yet scripted)*.

Steps marked "not yet scripted" currently just `echo` a TODO pointing at the relevant
design-doc section -- see [`doc/project-plan.md`](doc/project-plan.md) for the tracked
list of what's left.

## Variables available to change during a run

Every variable below is a `workflow_dispatch` input with a literal fallback in the `env:`
block, so scheduled runs get the same defaults a manual run would without overriding
anything.

| Variable | Default | Controls |
| --- | --- | --- |
| `core_repo_ref` | `master` | `tapyrus-core` branch/tag/sha to check out |
| `signer_repo_ref` | `master` | `tapyrus-signer` branch/tag/sha to check out |
| `seeder_repo_ref` | `master` | `tapyrus-seeder` branch/tag/sha to check out |
| `chain_height_before_reorg` | `30` | Baseline height to reach (all 7 nodes connected) before splitting for the reorg |
| `reorg_loser_blocks` | `10` | Blocks the losing group builds past the baseline |
| `reorg_winner_margin` | `2` | Extra blocks the winning group builds beyond `reorg_loser_blocks` (winner total = loser + margin, so a tie/shorter-winner is structurally impossible) |
| `tx_total_count` | `20` | Total transactions generated across the run |
| `tx_tpc_percent` | `30` | Percent of `tx_total_count` that are plain TPC transfers (remainder is colored-coin) |
| `tx_interval_seconds` | `30` | Seconds between each generated transaction |
| `round_duration_seconds` | `60` | `tapyrus-signerd` round-duration (block interval) -- 60s avoids the `InvalidBlock` timing race a shorter duration hits, see `doc/work-done.md` |
| `rotation_height_offset` | `10` | Blocks ahead of the current tip to schedule signer-set-b's takeover height |
| `max_block_size_new` | `2000000` | New max-block-size value pushed via the xfield change |
| `prng_seed_base` | `github.run_id` | Base seed for per-node PRNGs (combined with node index) -- override to reproduce a past run's exact transaction sequence |
| `network_id` | `1905960821` | Tapyrus dev network id, used for genesis/seeder/RPC network selection |
| `slack_log_tail_lines` | `100` | Lines of the implicated container's log inlined in the Slack failure report |
| `docker_build_platform` | *(empty, runner-native)* | Docker `--platform` for image builds -- local verification only ever used `linux/arm64` |

Not configurable per-run (hardcoded): the RPC port/user/pass (`12381` /
`rpcuser` / `rpcpassword`), and the signer count (3) / threshold (2) -- the 7-node
topology in `docker/docker-compose.yml` is wired 1:1 to exactly 3 signers, so changing
the count means redesigning the topology, not just passing a different number.

## Where to look next

- [`doc/project-plan.md`](doc/project-plan.md) -- tracked progress, what's done vs. outstanding.
- [`doc/scripts.md`](doc/scripts.md) -- what each script in `scripts/` does, its inputs/outputs, and known gotchas.
- [`doc/weekly-integration-test-plan.md`](doc/weekly-integration-test-plan.md) -- the full scenario design and architecture rationale.
- [`doc/work-done.md`](doc/work-done.md) -- transcript of everything manually verified so far (ceremony, topology, reorg, seeder fixes).
