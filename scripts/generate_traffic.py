#!/usr/bin/env python3
"""Drive round-robin TPC + colored-coin traffic across all 7 core-* nodes and confirm
every node's wallet balance (TPC and every colored type in play) after each block.

Everything is derived from a single round count (--round-count). Each round spans 3
block-heights, polled the same way across all 7 nodes so a slower node (core-7 is 3 P2P
hops from any first-layer node, see docker/docker-compose.yml) never causes a race:
  - send height:   every node concurrently sends TPC to its round-robin target, and
                    either transfers its own colored type to the same target (enough
                    balance) or mints more of it (not enough) -- see "Colored-coin
                    balance shortfall" below. Broadcasting doesn't touch the ledger yet
                    -- see "Deferred ledger crediting" below.
  - check height:  each send's ledger delta is applied once its txid actually confirms
                    (still-pending ones get one more try at settle height); balances
                    are then polled and logged, not asserted -- P2P propagation to all
                    7 nodes isn't guaranteed complete the instant the height ticks
                    over, so asserting here would be a false-negative risk.
  - settle height: any send still unconfirmed here is dropped, not credited -- and
                    balances are polled and asserted against the ledger this script
                    has been keeping since the funding phase.

Deferred ledger crediting: a send/issuance whose RPC call succeeds can still be
dropped from the mempool before it confirms -- e.g. a chaos node's own invalidateblock
racing the broadcast (confirmed live: two chaos nodes coincidentally invalidated their
shared tip in the same instant a round's sends were going out, and one of those sends
never confirmed again). Crediting the ledger the instant broadcast succeeds would
leave it permanently out of sync with reality in that case. So sends/issuances return
a PendingChange instead of touching self._ledger directly; _resolve_pending_changes
applies it only once every one of its txids has confirmed on-chain, and drops it --
not counted as a successful round action either -- if it never does. Every call site
gets two attempts (an initial check, then one more at the next block), not one:
confirmed live that a single shot isn't always enough even for a perfectly valid
transaction (setup's funding/issuance calls used to get only one, and a transaction
that just needed one more block got dropped anyway -- silently corrupting the ledger
two ways at once, the missed credit itself plus the fee deduction bundled in the
same PendingChange).

Usage:
    ./scripts/generate_traffic.py <round-count>

Requires the 7-node topology already up and converged (scripts/wait_for_topology.py)
and signer-set-a producing blocks. CORE_RPC_USER / CORE_RPC_PASS env vars match the
workflow's job-level env of the same names.

Round-robin target: node i's round R send goes to node (i + offset) mod 7, where
offset = 1 + ((R - 1) mod 6) -- cycles through all 6 other nodes every 6 rounds and
never sends to itself (an offset of 0 mod 7 would).

Colored-coin balance shortfall: rather than issue every node's full lifetime supply of
its color up front (all the chain's colored-coin activity would then happen once, at
the very start), each node only ever holds a small working balance. When a round's
transfer would exceed it, that node issues/reissues more instead of transferring --
still exactly one transaction for that node this round, so the total transaction count
stays exactly round_count * 14 (7 nodes * {TPC send, colored send-or-mint}) regardless
of how many rounds hit a shortfall. REISSUABLE tops up via reissuetoken (only the
original issuer can -- see doc/work-done.md); NON-REISSUABLE and NFT have no reissue
path at all (fixed supply by design), so their "top-up" mints an entirely new color
each time -- a node's NON-REISSUABLE/NFT identity rotates through multiple distinct
colors over a long run, which is expected, not a bug.

Coinbase observation: core-1a/2a/3a (the 3 first-layer, RPC-target nodes) earn a
coinbase reward whenever their signer is round master -- subsidy plus whatever
transaction fees that block happened to include (not a flat amount once real traffic
is flowing), immediately spendable with no coinbase-maturity delay in this codebase
at all. Which physical node earns at a given height depends on tapyrus-signer's own
sorted-pubkey master selection, not anything this script controls. Every height's
earner is read directly: _credit_coinbase_for_height asks each candidate's wallet
for that height's real coinbase transaction and credits whichever one actually has
it, for every height since the last one credited (not just the latest observed --
two blocks landing between polls would otherwise leave the one in between never
credited at all).

See doc/work-done.md for the tapyrus-core RPC facts this script is built on (colored
address round-trip requirement, issuetoken/transfertoken semantics) -- verified
against a real 7-node stack, and since via real GitHub Actions CI too.
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.log import log  # noqa: E402
from scripts.lib.orchestrator_control import pause_node_orchestrators, resume_node_orchestrators  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402

RPC_HOST = "127.0.0.1"
HEIGHT_POLL_INTERVAL_SECONDS = 3
HEIGHT_POLL_TIMEOUT_SECONDS = 180
RPC_READY_TIMEOUT_SECONDS = 120
RPC_READY_POLL_INTERVAL_SECONDS = 3
# A block landing mid-read (across either all 7 nodes concurrently, or one node
# after another sequentially) would otherwise mix pre- and post-block state into
# one supposedly-consistent snapshot -- a torn read that looks like a real balance
# mismatch with no real cause. Retried, not just risked once: block cadence is
# tens of seconds, so a same-height re-read on retry is expected within a couple
# attempts, not a sign of a stuck chain.
HEIGHT_CONSISTENT_READ_MAX_ATTEMPTS = 5

# (node name, host-published RPC port) -- see docker/docker-compose.yml's port mappings.
NODES = (
    ("core-1a", 12381),
    ("core-1b", 12382),
    ("core-2a", 12383),
    ("core-2b", 12384),
    ("core-3a", 12385),
    ("core-3b", 12386),
    ("core-7", 12387),
)

# Only the 3 first-layer nodes are a signer's RPC target (assemble_signer_configs.py's
# to-address), so only they ever receive coinbase. The other 4 need on-chain funding
# before they can send anything.
COINBASE_EARNING_NODES = {"core-1a", "core-2a", "core-3a"}
FUNDING_PAIRS = (
    ("core-1a", "core-1b"),
    ("core-2a", "core-2b"),
    ("core-3a", "core-3b"),
    ("core-1a", "core-7"),
)

# Token types, per tapyrus-core's issuetoken RPC (src/wallet/rpcwallet.cpp).
REISSUABLE, NON_REISSUABLE, NFT = 1, 2, 3
TOKEN_TYPE_NAMES = {REISSUABLE: "REISSUABLE", NON_REISSUABLE: "NON_REISSUABLE", NFT: "NFT"}
# Cycled across the 7 nodes in NODES order: REISSUABLE, NON_REISSUABLE, NFT, repeat.
NODE_TOKEN_TYPES = (REISSUABLE, NON_REISSUABLE, NFT, REISSUABLE, NON_REISSUABLE, NFT, REISSUABLE)

FUNDING_AMOUNT_TPC = 1.0
ROUND_SEND_AMOUNT_TPC = 0.001
SEED_UTXO_AMOUNT_TPC = 0.001
TOKEN_ISSUE_AMOUNT = 3
TOKEN_TOPUP_AMOUNT = 3
ROUND_SEND_AMOUNT_TOKEN = 1
TPC = "TPC"

# _send_node_round catches RpcError/RpcUnreachable per action and just warns, on
# purpose (one node's bad round shouldn't abort everyone else's) -- but that means a
# systemic failure (e.g. the fallbackfee incident, see doc/work-done.md, where every
# sendtoaddress call failed) leaves the ledger untouched right along with the real
# balances, so _verify_round's ledger-vs-actual comparison matches trivially and the
# run exits 0 despite doing nothing. This floor catches that: at least this fraction
# of the round_count * 14 (7 nodes x {TPC send, colored send-or-mint}) actions must
# actually confirm on-chain (see PendingChange/_resolve_pending_changes -- broadcasting
# alone doesn't count), or the run is treated as failed regardless of what the ledger
# comparison says.
MIN_SUCCESSFUL_ACTION_FRACTION = 0.5


class TrafficGenerationError(Exception):
    """Balance verification found a mismatch, or a round-robin action failed."""


class TrafficNode:
    """One core-* node's identity for this script: its RPC client, receiving address,
    and its designated colored type (assigned once, see NODE_TOKEN_TYPES)."""

    def __init__(self, index, name, rpc):
        self.index = index
        self.name = name
        self.rpc = rpc
        self.address = None
        self.token_type = NODE_TOKEN_TYPES[index]
        self.color_id = None


class PendingChange:
    """A ledger mutation whose transaction(s) haven't been confirmed on-chain yet --
    applied only once every one of them is (see _resolve_pending_changes), not the
    instant broadcast succeeds. A send/issuance that broadcasts fine (no RpcError) can
    still be dropped from the mempool by a coincidental chaos invalidateblock racing
    the broadcast -- confirmed live. Crediting the ledger immediately on broadcast
    would then leave it permanently out of sync with the real, on-chain outcome.

    node: whose wallet owns every txid below (gettransaction is queried against it).
    txids: all of them must confirm before deltas apply -- a multi-tx operation (e.g.
        reissuetoken's several component txs) is all-or-nothing, not partial credit.
    deltas: (node_name, asset, amount) tuples applied to self._ledger once confirmed.
    """

    def __init__(self, node, txids, deltas, description):
        self.node = node
        self.txids = list(txids)
        self.deltas = deltas
        self.description = description


class TrafficGenerator:
    def __init__(self, nodes, round_count):
        self._nodes = nodes
        self._round_count = round_count
        # ledger[node_name][asset] -- asset is TPC or a color-id hex string. TPC
        # starts empty here, not at a hardcoded 0.0 -- seeded from each node's REAL
        # current balance in _seed_ledger_with_current_balances() instead, since this
        # script runs multiple times in the same job (before and after the
        # reorg/rotation steps) against wallets that already carry a balance.
        self._ledger = {node.name: {} for node in nodes}
        self._mismatches = []
        self._successful_round_actions = 0
        # Every _next_block_with_coinbase() call credits every height since this
        # one, not just the height it happens to observe -- so a block that lands
        # between two polls still gets its coinbase credited. Set for real once
        # run() knows the starting height, before any crediting begins.
        self._last_credited_height = None

    async def run(self):
        # The node orchestrator's background chaos runs continuously, including
        # right through this phase (by design -- see doc/work-done.md), so any of
        # the 4 uncapped nodes may be mid-restart at the exact moment this starts.
        # Round actions later on already tolerate that per-call (see
        # MIN_SUCCESSFUL_ACTION_FRACTION above), but setup isn't a round action --
        # it needs every node reachable at least once before it can proceed at all.
        await self._wait_for_all_rpc_ready()
        await self._collect_addresses()
        # A prior step (e.g. simulate_reorg.py's canary) can leave a transaction
        # pending in some node's mempool -- getbalance only counts confirmed balance,
        # so seeding from it now and having that transaction confirm mid-run would
        # silently offset the ledger by exactly its amount for the rest of the run.
        await self._wait_for_empty_mempool()
        self._last_credited_height = await self._seed_ledger_with_current_balances()
        # 3 blocks' worth of coinbase is far more than FUNDING_AMOUNT_TPC needs --
        # ensures core-1a/2a/3a have real spendable balance before funding the
        # other 4 nodes from them, on a fresh chain where they start at 0.
        for _ in range(3):
            await self._next_block_with_coinbase()
        # Same two-attempt check/settle tolerance as the round loop below, not a
        # single shot -- confirmed live, a single block's grace isn't always enough
        # even for a transaction that's perfectly fine: caught one setup issuance
        # that simply needed one more block (33s), dropped anyway, which silently
        # corrupted the ledger two ways at once (the missed color credit drifted it
        # negative over subsequent sends, and the fee deduction bundled in the same
        # PendingChange never applied either, leaving TPC permanently too high
        # relative to reality). A genuinely failed tx still gets dropped -- just
        # after two tries, not one.
        funding_changes = await self._fund_unfunded_nodes()
        check_height = await self._next_block_with_coinbase()
        funding_changes = await self._resolve_pending_changes(funding_changes, check_height, final=False, count_success=False)
        settle_height = await self._next_block_with_coinbase()
        await self._resolve_pending_changes(funding_changes, settle_height, final=True, count_success=False)

        issuance_changes = await self._issue_colors()
        check_height = await self._next_block_with_coinbase()
        issuance_changes = await self._resolve_pending_changes(issuance_changes, check_height, final=False, count_success=False)
        settle_height = await self._next_block_with_coinbase()
        await self._resolve_pending_changes(issuance_changes, settle_height, final=True, count_success=False)

        for round_number in range(1, self._round_count + 1):
            log.step(f"round {round_number}/{self._round_count}")
            await self._next_block_with_coinbase()
            pending = await self._send_round(round_number)
            check_height = await self._next_block_with_coinbase()
            pending = await self._resolve_pending_changes(pending, check_height, final=False)
            await self._log_balances(check_height, "check")
            settle_height = await self._next_block_with_coinbase()
            await self._resolve_pending_changes(pending, settle_height, final=True)
            await self._verify_round(settle_height)

        expected_actions = self._round_count * len(self._nodes) * 2
        min_required_actions = max(1, int(expected_actions * MIN_SUCCESSFUL_ACTION_FRACTION))
        if self._successful_round_actions < min_required_actions:
            raise TrafficGenerationError(
                f"only {self._successful_round_actions}/{expected_actions} round actions actually "
                f"succeeded (need at least {min_required_actions}) -- a ledger that matches reality "
                f"isn't enough on its own, since a systemic failure leaves both sides untouched"
            )

        if self._mismatches:
            raise TrafficGenerationError(
                f"{len(self._mismatches)} balance mismatch(es) across the run:\n"
                + "\n".join(self._mismatches)
            )
        log.info(
            f"done. all {self._round_count} round(s) settled with balances matching the ledger "
            f"({self._successful_round_actions}/{expected_actions} round actions succeeded)"
        )

    # -- setup: addresses, funding, issuance -----------------------------------

    async def _call_with_retry(self, node, method, params=None):
        """Retries on RpcUnreachable instead of failing on the first attempt.
        Pausing chaos (see callers) stops NEW actions but can't interrupt one
        already in flight the instant the pause landed, so a node can still be
        briefly unreachable inside an otherwise-paused window -- a bare single
        call would treat that straggler as a hard failure instead of riding it
        out the same way _wait_for_next_block's own polling already does."""
        deadline = time.monotonic() + RPC_READY_TIMEOUT_SECONDS
        while True:
            try:
                return await node.rpc.call(method, params)
            except RpcUnreachable as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"{node.name}: {method} never became reachable within "
                        f"{RPC_READY_TIMEOUT_SECONDS}s: {exc}"
                    )
                await asyncio.sleep(RPC_READY_POLL_INTERVAL_SECONDS)

    async def _wait_for_all_rpc_ready(self):
        log.step("waiting for all nodes' RPC to be reachable before setup")
        await asyncio.gather(*(self._call_with_retry(node, "getblockcount") for node in self._nodes))

    async def _collect_addresses(self):
        log.step("collecting a receiving address from each node")

        async def collect(node):
            node.address = await self._call_with_retry(node, "getnewaddress")

        pause_node_orchestrators()
        try:
            await asyncio.gather(*(collect(node) for node in self._nodes))
        finally:
            resume_node_orchestrators()

    async def _seed_ledger_with_current_balances(self):
        """Returns the height this snapshot was taken at, still inside the paused
        window -- callers seeding _last_credited_height from this must use that
        return value rather than re-reading the height after chaos resumes: a
        chaos invalidateblock landing in that instant could regress the height
        below what the balances were actually seeded at, and the first crediting
        pass would then re-credit a coinbase already folded into the seeded
        balance (same double-credit failure mode as elsewhere)."""
        log.step("reading each node's current TPC balance to seed the ledger (not assuming a fresh 0.0 start)")

        pause_node_orchestrators()
        try:
            for attempt in range(1, HEIGHT_CONSISTENT_READ_MAX_ATTEMPTS + 1):
                before = await self._all_heights()
                balances = await asyncio.gather(
                    *(self._call_with_retry(node, "getbalance", [False]) for node in self._nodes)
                )
                after = await self._all_heights()
                if before == after:
                    break
                log.warn(
                    f"height changed ({before} -> {after}) while seeding balances -- "
                    f"retrying for a consistent snapshot (attempt {attempt}/{HEIGHT_CONSISTENT_READ_MAX_ATTEMPTS})"
                )
            else:
                raise TrafficGenerationError(
                    f"height kept changing across {HEIGHT_CONSISTENT_READ_MAX_ATTEMPTS} attempts -- "
                    "never got a stable snapshot to seed the ledger from"
                )
            for node, balance in zip(self._nodes, balances):
                self._ledger[node.name][TPC] = balance
                if balance:
                    log.info(f"{node.name}: starting balance {balance} TPC (carried over from before this run)")
            reachable = [h for h in after.values() if h is not None]
            if not reachable:
                raise RpcUnreachable("no node was reachable to determine the seeded ledger's height")
            return min(reachable)
        finally:
            resume_node_orchestrators()

    async def _fund_unfunded_nodes(self):
        log.step(f"funding the {len(FUNDING_PAIRS)} nodes with no coinbase income")
        by_name = {node.name: node for node in self._nodes}

        async def fund(funder_name, recipient_name):
            funder, recipient = by_name[funder_name], by_name[recipient_name]
            try:
                txid = await funder.rpc.call("sendtoaddress", [recipient.address, FUNDING_AMOUNT_TPC])
            except (RpcError, RpcUnreachable) as exc:
                log.warn(f"{funder_name}: funding {recipient_name} failed ({exc}) -- {recipient_name} stays unfunded")
                return None
            return PendingChange(
                node=funder, txids=[txid],
                deltas=[(funder.name, TPC, -FUNDING_AMOUNT_TPC), (recipient.name, TPC, FUNDING_AMOUNT_TPC)],
                description=f"funding {recipient_name}",
            )

        results = await asyncio.gather(*(fund(f, r) for f, r in FUNDING_PAIRS))
        return [r for r in results if r is not None]

    async def _issue_colors(self):
        log.step("each node issuing its own colored type")

        async def issue(node):
            try:
                return await self._issue_color(node)
            except (RpcError, RpcUnreachable) as exc:
                log.warn(f"{node.name}: initial {TOKEN_TYPE_NAMES[node.token_type]} issuance failed ({exc})"
                         " -- this node sits out colored-coin activity until a later round mints one")
                return None

        results = await asyncio.gather(*(issue(node) for node in self._nodes))
        return [r for r in results if r is not None]

    async def _issue_color(self, node):
        if node.token_type == REISSUABLE:
            script_pubkey = await self._own_script_pubkey(node)
            result = await node.rpc.call("issuetoken", [REISSUABLE, TOKEN_ISSUE_AMOUNT, script_pubkey])
            txids = result["txids"]
        else:
            seed_txid, vout = await self._seed_plain_utxo(node)
            value = 1 if node.token_type == NFT else TOKEN_ISSUE_AMOUNT
            result = await node.rpc.call("issuetoken", [node.token_type, value, seed_txid, vout])
            # Bundled with the issuance's own txid -- the seed self-send's fee is
            # part of the same all-or-nothing change, not credited separately.
            txids = [seed_txid, result["txid"]]

        node.color_id = result["color"]
        amount = 1 if node.token_type == NFT else float(TOKEN_ISSUE_AMOUNT)
        log.info(f"{node.name}: issuing {TOKEN_TYPE_NAMES[node.token_type]} color {node.color_id} (pending confirmation)")
        return PendingChange(
            node=node, txids=txids,
            deltas=[(node.name, node.color_id, amount)],
            description=f"{TOKEN_TYPE_NAMES[node.token_type]} issuance ({node.color_id[:12]}...)",
        )

    async def _own_script_pubkey(self, node):
        address = await node.rpc.call("getnewaddress")
        info = await node.rpc.call("getaddressinfo", [address])
        return info["scriptPubKey"]

    async def _seed_plain_utxo(self, node):
        """NON_REISSUABLE/NFT issuance needs an existing plain, uncolored, unambiguous
        UTXO to consume -- rather than parse listunspent's color field to find one,
        self-send a small amount to a fresh address, giving a UTXO we already know the
        exact (txid, vout) of."""
        address = await node.rpc.call("getnewaddress")
        txid = await node.rpc.call("sendtoaddress", [address, SEED_UTXO_AMOUNT_TPC])
        tx = await node.rpc.call("gettransaction", [txid])
        for detail in tx["details"]:
            if detail.get("address") == address:
                return txid, detail["vout"]
        raise TrafficGenerationError(f"{node.name}: seed self-send {txid} has no output paying {address}")

    # -- round-robin sends -------------------------------------------------------

    async def _send_round(self, round_number):
        offset = 1 + ((round_number - 1) % (len(self._nodes) - 1))
        results = await asyncio.gather(*(
            self._send_node_round(sender, self._nodes[(sender.index + offset) % len(self._nodes)])
            for sender in self._nodes
        ))
        return [change for pair in results for change in pair if change is not None]

    async def _send_node_round(self, sender, target):
        tpc_change = None
        color_change = None
        try:
            tpc_change = await self._send_tpc(sender, target)
        except (RpcError, RpcUnreachable) as exc:
            log.warn(f"{sender.name}: round TPC send skipped ({exc})")

        try:
            color_change = await self._send_or_topup_color(sender, target)
        except (RpcError, RpcUnreachable) as exc:
            log.warn(f"{sender.name}: round colored action failed ({exc})")

        return (tpc_change, color_change)

    async def _send_tpc(self, sender, target):
        txid = await sender.rpc.call("sendtoaddress", [target.address, ROUND_SEND_AMOUNT_TPC])
        return PendingChange(
            node=sender, txids=[txid],
            deltas=[(sender.name, TPC, -ROUND_SEND_AMOUNT_TPC), (target.name, TPC, ROUND_SEND_AMOUNT_TPC)],
            description=f"round TPC send to {target.name}",
        )

    async def _send_or_topup_color(self, sender, target):
        if sender.color_id is None:
            # No successful issuance yet (the initial one failed) -- getbalance/
            # getnewaddress silently treat a null color as "no color" instead of
            # raising, so this must be checked explicitly rather than falling through
            # to them, or it'd silently send plain TPC while believing it sent a token.
            return await self._issue_color(sender)

        balance = await sender.rpc.call("getbalance", [False, sender.color_id])
        if balance >= ROUND_SEND_AMOUNT_TOKEN:
            colored_address = await target.rpc.call("getnewaddress", ["", sender.color_id])
            txid = await sender.rpc.call("transfertoken", [colored_address, ROUND_SEND_AMOUNT_TOKEN])
            return PendingChange(
                node=sender, txids=[txid],
                deltas=[
                    (sender.name, sender.color_id, -ROUND_SEND_AMOUNT_TOKEN),
                    (target.name, sender.color_id, ROUND_SEND_AMOUNT_TOKEN),
                ],
                description=f"round colored transfer to {target.name} ({sender.color_id[:12]}...)",
            )
        else:
            return await self._topup_color(sender)

    async def _topup_color(self, sender):
        if sender.token_type == REISSUABLE:
            result = await sender.rpc.call("reissuetoken", [sender.color_id, TOKEN_TOPUP_AMOUNT])
            log.info(f"{sender.name}: reissuing {sender.color_id} to top up by {TOKEN_TOPUP_AMOUNT} (pending confirmation)")
            return PendingChange(
                node=sender, txids=result["txids"],
                deltas=[(sender.name, sender.color_id, TOKEN_TOPUP_AMOUNT)],
                description=f"colored reissue top-up ({sender.color_id[:12]}...)",
            )
        else:
            # NON_REISSUABLE/NFT have no reissue path (fixed supply by design) -- mint
            # an entirely new color instead. The node's "own color" moves forward.
            log.info(f"{sender.name}: minting a fresh {TOKEN_TYPE_NAMES[sender.token_type]} color (old one exhausted)")
            return await self._issue_color(sender)

    # -- height polling ------------------------------------------------------------

    async def _wait_for_empty_mempool(self):
        # Same all-7-nodes-reachable shape as _wait_for_next_block, same fix.
        pause_node_orchestrators()
        try:
            deadline = time.monotonic() + HEIGHT_POLL_TIMEOUT_SECONDS
            while True:
                mempools = await asyncio.gather(*(self._node_mempool(node) for node in self._nodes))
                if all(mempool == [] for mempool in mempools):
                    return
                if time.monotonic() >= deadline:
                    raise TrafficGenerationError(
                        f"mempool(s) still non-empty after {HEIGHT_POLL_TIMEOUT_SECONDS}s: "
                        f"{ {node.name: mempool for node, mempool in zip(self._nodes, mempools)} }"
                    )
                await asyncio.sleep(HEIGHT_POLL_INTERVAL_SECONDS)
        finally:
            resume_node_orchestrators()

    async def _node_mempool(self, node):
        try:
            return await node.rpc.call("getrawmempool")
        except RpcUnreachable:
            return None

    async def _current_height(self):
        # Pausing chaos (see callers) stops NEW actions but can't interrupt a
        # node already mid-downtime from an earlier one -- so this needs the same
        # per-node tolerance _node_height already has, not a bare gather.
        heights = await asyncio.gather(*(self._node_height(node) for node in self._nodes))
        reachable = [h for h in heights if h is not None]
        if not reachable:
            raise RpcUnreachable("no node was reachable to determine the current height")
        return min(reachable)

    async def _wait_for_next_block(self):
        # Requires literally all 7 nodes to catch up -- incompatible with chaos
        # churning core-1b/2b/3b/core-7 mid-wait (they have no cap, by design), so
        # background chaos is paused for the span of this one wait and resumed
        # right after, rather than loosening this method's own all-7 requirement
        # or pausing chaos for generate_traffic.py's entire run. A node already
        # mid-downtime when the pause lands isn't interrupted -- it finishes that
        # downtime on its own (bounded, see node_orchestrator.py) -- which is why
        # this still needs its own timeout below, not just the pause file alone.
        pause_node_orchestrators()
        try:
            target = await self._current_height() + 1
            deadline = time.monotonic() + HEIGHT_POLL_TIMEOUT_SECONDS
            while True:
                heights = await asyncio.gather(*(self._node_height(node) for node in self._nodes))
                if all(height is not None and height >= target for height in heights):
                    return target
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"not all nodes reached height {target} within {HEIGHT_POLL_TIMEOUT_SECONDS}s")
                await asyncio.sleep(HEIGHT_POLL_INTERVAL_SECONDS)
        finally:
            resume_node_orchestrators()

    async def _node_height(self, node):
        try:
            return await node.rpc.call("getblockcount")
        except RpcUnreachable:
            return None

    async def _all_heights(self):
        # Per-node, not _current_height()'s min() across all 7 -- a balance-read
        # consistency check needs to catch ANY node advancing during the read, not
        # just whichever one happens to be the slowest. A node that wasn't the
        # minimum before the read could still advance during it without moving
        # the aggregate min at all, silently letting a torn read through.
        heights = await asyncio.gather(*(self._node_height(node) for node in self._nodes))
        return {node.name: h for node, h in zip(self._nodes, heights)}

    async def _next_block_with_coinbase(self):
        """_wait_for_next_block, plus crediting the ledger for every height since
        the last one credited -- not just the height just reached. Master
        selection rotates by ROUND, not by block, and a round doesn't always
        produce one, so two blocks can land between polls; crediting only the
        latest would silently leave the one in between never credited at all,
        surfacing as a bogus mismatch at settle time."""
        await self._wait_for_next_block()
        actual = await self._current_height()
        for height in range(self._last_credited_height + 1, actual + 1):
            await self._credit_coinbase_for_height(height)
        # max(), not a bare assignment: _current_height() takes min() across all 7
        # nodes, and a chaos node's own invalidateblock can transiently regress its
        # locally-reported height. A bare assignment would let that regression rewind
        # this backward, making a later call re-credit a height already credited --
        # confirmed live: two chaos nodes invalidated their shared tip simultaneously,
        # regressing the min-height read, and the next call double-credited that
        # height's coinbase reward. max() keeps this monotonic regardless of when a
        # regression lands.
        self._last_credited_height = max(self._last_credited_height, actual)
        return actual

    async def _credit_coinbase_for_height(self, height):
        """Reads the height's real coinbase transaction and credits whichever of
        core-1a/2a/3a's wallet actually has it. The reward isn't a flat 50 TPC --
        it's subsidy plus whatever transaction fees that block happened to
        include, so once real traffic is flowing it varies block to block
        (confirmed live)."""
        pause_node_orchestrators()
        try:
            by_name = {node.name: node for node in self._nodes}
            probe = self._nodes[0]
            blockhash = await self._call_with_retry(probe, "getblockhash", [height])
            block = await self._call_with_retry(probe, "getblock", [blockhash])
            coinbase_txid = block["tx"][0]
            for name in sorted(COINBASE_EARNING_NODES):
                try:
                    tx = await self._call_with_retry(by_name[name], "gettransaction", [coinbase_txid])
                except RpcError:
                    continue  # not in this node's wallet -- try the next candidate
                for detail in tx.get("details", []):
                    if detail.get("category") == "generate":
                        self._ledger[name][TPC] += detail["amount"]
                        return
            raise TrafficGenerationError(
                f"height {height}: no coinbase-earning node's wallet has a 'generate' "
                f"transaction for {coinbase_txid}"
            )
        finally:
            resume_node_orchestrators()

    # -- balance logging / verification --------------------------------------------

    async def _all_colors(self):
        return [node.color_id for node in self._nodes if node.color_id]

    async def _log_balances(self, height, label):
        # Same all-7-nodes-reachable shape as _wait_for_next_block, same fix --
        # every node's getbalance is read here with no per-call error tolerance.
        pause_node_orchestrators()
        try:
            colors = await self._all_colors()
            for node in self._nodes:
                tpc = await self._call_with_retry(node, "getbalance", [False])
                colored = {
                    color: await self._call_with_retry(node, "getbalance", [False, color]) for color in colors
                }
                log.info(f"height {height} ({label}): {node.name} TPC={tpc} {colored}")
        finally:
            resume_node_orchestrators()

    async def _verify_round(self, height):
        # This is the run's actual correctness check (ledger vs real balances) --
        # same chaos-paused window, so an unreachable node can't be misread as a
        # real balance mismatch or abort verification outright. The per-node loop
        # below is sequential, not concurrent, so it can take long enough for a
        # new block to land mid-loop -- nodes read before it would reflect the old
        # height, nodes read after would reflect the new one, a torn snapshot that
        # looks like a real mismatch. Guarded the same way as
        # _seed_ledger_with_current_balances: re-read from scratch if the height
        # moved during the read, discarding whatever this attempt already recorded.
        pause_node_orchestrators()
        try:
            colors = await self._all_colors()
            for attempt in range(1, HEIGHT_CONSISTENT_READ_MAX_ATTEMPTS + 1):
                before = await self._all_heights()
                mismatches_before = len(self._mismatches)
                for node in self._nodes:
                    actual_tpc = await self._call_with_retry(node, "getbalance", [False])
                    self._compare(height, node.name, TPC, self._ledger[node.name][TPC], actual_tpc)
                    for color in colors:
                        actual = await self._call_with_retry(node, "getbalance", [False, color])
                        expected = self._ledger[node.name].get(color, 0)
                        self._compare(height, node.name, color, expected, actual)
                after = await self._all_heights()
                if before == after:
                    return
                del self._mismatches[mismatches_before:]
                log.warn(
                    f"height changed ({before} -> {after}) while verifying round {height} -- discarding that "
                    f"attempt's reads and retrying for a consistent snapshot (attempt {attempt}/{HEIGHT_CONSISTENT_READ_MAX_ATTEMPTS})"
                )
            raise TrafficGenerationError(
                f"height kept changing across {HEIGHT_CONSISTENT_READ_MAX_ATTEMPTS} attempts -- "
                f"never got a stable snapshot to verify round at height {height}"
            )
        finally:
            resume_node_orchestrators()

    def _compare(self, height, node_name, asset, expected, actual):
        # Token balances are exact integers (no fee taken from token value itself);
        # TPC has floating-point drift from repeated add/subtract, so give it a small
        # tolerance instead of exact equality.
        tolerance = 1e-6 if asset == TPC else 0
        if abs(actual - expected) > tolerance:
            message = f"height {height}: {node_name} {asset}: expected {expected}, got {actual}"
            log.error(message)
            self._mismatches.append(message)
        else:
            log.info(f"height {height}: {node_name} {asset}: {actual} (matches ledger)")

    async def _resolve_pending_changes(self, pending_changes, height, final, count_success=True):
        """Applies each PendingChange's ledger deltas only once every one of its
        txids has actually confirmed (see PendingChange's own docstring for why).
        Not final: unconfirmed changes are returned for a later retry at the next
        block height (mirrors the round's own check/settle two-step). Final: an
        unconfirmed change is dropped instead -- logged, not credited, not counted
        as a successful round action -- since nothing later in the run gives it
        another chance to confirm. count_success=False for the one-time setup calls
        (funding/issuance) -- MIN_SUCCESSFUL_ACTION_FRACTION's denominator is
        round_count * 14 round-loop actions only; folding setup successes in would
        let a count exceed that denominator instead of meaning what it claims to."""
        if not pending_changes:
            return []
        still_pending = []
        pause_node_orchestrators()
        try:
            for change in pending_changes:
                confirmed = True
                total_fee = 0.0
                for txid in change.txids:
                    try:
                        tx = await self._call_with_retry(change.node, "gettransaction", [txid])
                    except RpcError:
                        confirmed = False  # evicted from the wallet entirely -- lost, not just pending
                        break
                    if tx.get("confirmations", 0) < 1:
                        confirmed = False
                        break
                    total_fee += self._extract_fee(tx)
                if confirmed:
                    self._ledger[change.node.name][TPC] += total_fee
                    for node_name, asset, amount in change.deltas:
                        self._ledger[node_name][asset] = self._ledger[node_name].get(asset, 0) + amount
                    if count_success:
                        self._successful_round_actions += 1
                elif final:
                    txids_preview = ", ".join(t[:12] for t in change.txids)
                    log.warn(
                        f"{change.node.name}: {change.description} ({txids_preview}...) never confirmed "
                        f"by height {height} -- dropped, not credited"
                    )
                else:
                    still_pending.append(change)
        finally:
            resume_node_orchestrators()
        return still_pending

    def _extract_fee(self, tx):
        # tapyrus-core's gettransaction has no top-level "fee" field here -- and the
        # fee's shape within "details" itself varies by tx type (confirmed
        # against a live node; see doc/work-done.md): a plain TPC send nests "fee"
        # inside its own category="send" entry, while a transaction that also moves a
        # colored output puts it in a separate category="fee" entry instead (and the
        # send/receive entries have no "fee" key of their own in that case).
        for detail in tx.get("details", []):
            if detail.get("category") == "fee":
                return detail["amount"]
            if detail.get("category") == "send" and "fee" in detail:
                return detail["fee"]
        return 0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Drive round-robin TPC + colored-coin traffic and verify wallet balances after each block."
    )
    parser.add_argument("round_count", type=int, help="number of send/check/settle cycles to run")
    return parser.parse_args()


async def main():
    args = parse_args()
    rpc_user = os.environ.get("CORE_RPC_USER", "rpcuser")
    rpc_pass = os.environ.get("CORE_RPC_PASS", "rpcpassword")

    nodes = [
        TrafficNode(index, name, CoreRpcClient(RPC_HOST, port, rpc_user, rpc_pass))
        for index, (name, port) in enumerate(NODES)
    ]

    log.step(f"generating traffic across {len(nodes)} nodes for {args.round_count} round(s)")
    generator = TrafficGenerator(nodes, args.round_count)
    await generator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (TrafficGenerationError, TimeoutError, RpcError, RpcUnreachable) as exc:
        log.error(str(exc))
        sys.exit(1)
