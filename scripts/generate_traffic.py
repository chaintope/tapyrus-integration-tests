#!/usr/bin/env python3
"""Drive round-robin TPC + colored-coin traffic across all 7 core-* nodes and confirm
every node's wallet balance (TPC and every colored type in play) after each block.

Everything is derived from a single round count (--round-count). Each round spans 3
block-heights, polled the same way across all 7 nodes so a slower node (core-7 is 3 P2P
hops from any first-layer node, see docker/docker-compose.yml) never causes a race:
  - send height:   every node concurrently sends TPC to its round-robin target, and
                    either transfers its own colored type to the same target (enough
                    balance) or mints more of it (not enough) -- see "Colored-coin
                    balance shortfall" below.
  - check height:  balances are polled and logged, not asserted -- P2P propagation to
                    all 7 nodes isn't guaranteed complete the instant the height ticks
                    over, so asserting here would be a false-negative risk.
  - settle height: balances are polled and asserted against the ledger this script has
                    been keeping since the funding phase.

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

See doc/work-done.md for the tapyrus-core RPC facts this script is built on (colored
address round-trip requirement, coinbase maturity, issuetoken/transfertoken semantics)
and which parts are still unverified against a live stack.
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
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402

RPC_HOST = "127.0.0.1"
HEIGHT_POLL_INTERVAL_SECONDS = 3
HEIGHT_POLL_TIMEOUT_SECONDS = 180

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
# actually succeed, or the run is treated as failed regardless of what the ledger
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

    async def run(self):
        await self._collect_addresses()
        await self._seed_ledger_with_current_balances()
        # Waiting for just one more block isn't enough: with 3 signers rotating as
        # block proposer, one new block only credits coinbase to whichever signer
        # proposed it, not all three -- core-1a/2a/3a need to each have proposed at
        # least once before every funding source has spendable TPC. Poll actual
        # balances instead of guessing how many blocks that takes.
        await self._wait_for_funding_source_balances()
        await self._fund_unfunded_nodes()
        await self._wait_for_next_block()
        await self._issue_colors()
        await self._wait_for_next_block()

        for round_number in range(1, self._round_count + 1):
            log.step(f"round {round_number}/{self._round_count}")
            await self._wait_for_next_block()
            await self._send_round(round_number)
            check_height = await self._wait_for_next_block()
            await self._log_balances(check_height, "check")
            settle_height = await self._wait_for_next_block()
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

    async def _collect_addresses(self):
        log.step("collecting a receiving address from each node")

        async def collect(node):
            node.address = await node.rpc.call("getnewaddress")

        await asyncio.gather(*(collect(node) for node in self._nodes))

    async def _seed_ledger_with_current_balances(self):
        log.step("reading each node's current TPC balance to seed the ledger (not assuming a fresh 0.0 start)")

        async def seed(node):
            balance = await node.rpc.call("getbalance", [False])
            self._ledger[node.name][TPC] = balance
            if balance:
                log.info(f"{node.name}: starting balance {balance} TPC (carried over from before this run)")

        await asyncio.gather(*(seed(node) for node in self._nodes))

    async def _wait_for_funding_source_balances(self):
        log.step(f"waiting for each funding source to hold at least {FUNDING_AMOUNT_TPC} TPC")
        by_name = {node.name: node for node in self._nodes}
        funders = sorted(COINBASE_EARNING_NODES)
        deadline = time.monotonic() + HEIGHT_POLL_TIMEOUT_SECONDS * 3

        async def balance_of(name):
            try:
                return await by_name[name].rpc.call("getbalance", [False])
            except RpcUnreachable:
                return 0

        while True:
            balances = await asyncio.gather(*(balance_of(name) for name in funders))
            if all(balance >= FUNDING_AMOUNT_TPC for balance in balances):
                return
            if time.monotonic() >= deadline:
                missing = [name for name, balance in zip(funders, balances) if balance < FUNDING_AMOUNT_TPC]
                raise TrafficGenerationError(
                    f"funding source(s) never accumulated {FUNDING_AMOUNT_TPC} TPC "
                    f"within {HEIGHT_POLL_TIMEOUT_SECONDS * 3}s: {missing}"
                )
            await asyncio.sleep(HEIGHT_POLL_INTERVAL_SECONDS)

    async def _fund_unfunded_nodes(self):
        log.step(f"funding the {len(FUNDING_PAIRS)} nodes with no coinbase income")
        by_name = {node.name: node for node in self._nodes}

        async def fund(funder_name, recipient_name):
            funder, recipient = by_name[funder_name], by_name[recipient_name]
            try:
                txid = await funder.rpc.call("sendtoaddress", [recipient.address, FUNDING_AMOUNT_TPC])
            except (RpcError, RpcUnreachable) as exc:
                log.warn(f"{funder_name}: funding {recipient_name} failed ({exc}) -- {recipient_name} stays unfunded")
                return
            await self._apply_fee(funder, txid)
            self._ledger[funder.name][TPC] -= FUNDING_AMOUNT_TPC
            self._ledger[recipient.name][TPC] += FUNDING_AMOUNT_TPC

        await asyncio.gather(*(fund(f, r) for f, r in FUNDING_PAIRS))

    async def _issue_colors(self):
        log.step("each node issuing its own colored type")

        async def issue(node):
            try:
                await self._issue_color(node)
            except (RpcError, RpcUnreachable) as exc:
                log.warn(f"{node.name}: initial {TOKEN_TYPE_NAMES[node.token_type]} issuance failed ({exc})"
                         " -- this node sits out colored-coin activity until a later round mints one")

        await asyncio.gather(*(issue(node) for node in self._nodes))

    async def _issue_color(self, node):
        if node.token_type == REISSUABLE:
            script_pubkey = await self._own_script_pubkey(node)
            result = await node.rpc.call("issuetoken", [REISSUABLE, TOKEN_ISSUE_AMOUNT, script_pubkey])
            for txid in result["txids"]:
                await self._apply_fee(node, txid)
        else:
            txid, vout = await self._seed_plain_utxo(node)
            value = 1 if node.token_type == NFT else TOKEN_ISSUE_AMOUNT
            result = await node.rpc.call("issuetoken", [node.token_type, value, txid, vout])
            await self._apply_fee(node, result["txid"])

        node.color_id = result["color"]
        self._ledger[node.name][node.color_id] = (
            1 if node.token_type == NFT else float(TOKEN_ISSUE_AMOUNT)
        )
        log.info(f"{node.name}: issued {TOKEN_TYPE_NAMES[node.token_type]} color {node.color_id}")

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
        await self._apply_fee(node, txid)
        tx = await node.rpc.call("gettransaction", [txid])
        for detail in tx["details"]:
            if detail.get("address") == address:
                return txid, detail["vout"]
        raise TrafficGenerationError(f"{node.name}: seed self-send {txid} has no output paying {address}")

    # -- round-robin sends -------------------------------------------------------

    async def _send_round(self, round_number):
        offset = 1 + ((round_number - 1) % (len(self._nodes) - 1))
        await asyncio.gather(*(
            self._send_node_round(sender, self._nodes[(sender.index + offset) % len(self._nodes)])
            for sender in self._nodes
        ))

    async def _send_node_round(self, sender, target):
        try:
            await self._send_tpc(sender, target)
            self._successful_round_actions += 1
        except (RpcError, RpcUnreachable) as exc:
            log.warn(f"{sender.name}: round TPC send skipped ({exc})")

        try:
            await self._send_or_topup_color(sender, target)
            self._successful_round_actions += 1
        except (RpcError, RpcUnreachable) as exc:
            log.warn(f"{sender.name}: round colored action failed ({exc})")

    async def _send_tpc(self, sender, target):
        txid = await sender.rpc.call("sendtoaddress", [target.address, ROUND_SEND_AMOUNT_TPC])
        await self._apply_fee(sender, txid)
        self._ledger[sender.name][TPC] -= ROUND_SEND_AMOUNT_TPC
        self._ledger[target.name][TPC] += ROUND_SEND_AMOUNT_TPC

    async def _send_or_topup_color(self, sender, target):
        if sender.color_id is None:
            # No successful issuance yet (the initial one failed) -- getbalance/
            # getnewaddress silently treat a null color as "no color" instead of
            # raising, so this must be checked explicitly rather than falling through
            # to them, or it'd silently send plain TPC while believing it sent a token.
            await self._issue_color(sender)
            return

        balance = await sender.rpc.call("getbalance", [False, sender.color_id])
        if balance >= ROUND_SEND_AMOUNT_TOKEN:
            colored_address = await target.rpc.call("getnewaddress", ["", sender.color_id])
            txid = await sender.rpc.call("transfertoken", [colored_address, ROUND_SEND_AMOUNT_TOKEN])
            await self._apply_fee(sender, txid)
            self._ledger[sender.name][sender.color_id] -= ROUND_SEND_AMOUNT_TOKEN
            self._ledger[target.name][sender.color_id] = (
                self._ledger[target.name].get(sender.color_id, 0) + ROUND_SEND_AMOUNT_TOKEN
            )
        else:
            await self._topup_color(sender)

    async def _topup_color(self, sender):
        if sender.token_type == REISSUABLE:
            result = await sender.rpc.call("reissuetoken", [sender.color_id, TOKEN_TOPUP_AMOUNT])
            for txid in result["txids"]:
                await self._apply_fee(sender, txid)
            self._ledger[sender.name][sender.color_id] += TOKEN_TOPUP_AMOUNT
            log.info(f"{sender.name}: topped up {sender.color_id} by reissuing {TOKEN_TOPUP_AMOUNT}")
        else:
            # NON_REISSUABLE/NFT have no reissue path (fixed supply by design) -- mint
            # an entirely new color instead. The node's "own color" moves forward.
            await self._issue_color(sender)
            log.info(f"{sender.name}: minted a fresh {TOKEN_TYPE_NAMES[sender.token_type]} color (old one exhausted)")

    # -- height polling ------------------------------------------------------------

    async def _current_height(self):
        heights = await asyncio.gather(*(node.rpc.call("getblockcount") for node in self._nodes))
        return min(heights)

    async def _wait_for_next_block(self):
        target = await self._current_height() + 1
        deadline = time.monotonic() + HEIGHT_POLL_TIMEOUT_SECONDS
        while True:
            heights = await asyncio.gather(*(self._node_height(node) for node in self._nodes))
            if all(height is not None and height >= target for height in heights):
                return target
            if time.monotonic() >= deadline:
                raise TimeoutError(f"not all nodes reached height {target} within {HEIGHT_POLL_TIMEOUT_SECONDS}s")
            await asyncio.sleep(HEIGHT_POLL_INTERVAL_SECONDS)

    async def _node_height(self, node):
        try:
            return await node.rpc.call("getblockcount")
        except RpcUnreachable:
            return None

    # -- balance logging / verification --------------------------------------------

    async def _all_colors(self):
        return [node.color_id for node in self._nodes if node.color_id]

    async def _log_balances(self, height, label):
        colors = await self._all_colors()
        for node in self._nodes:
            tpc = await node.rpc.call("getbalance", [False])
            colored = {
                color: await node.rpc.call("getbalance", [False, color]) for color in colors
            }
            log.info(f"height {height} ({label}): {node.name} TPC={tpc} {colored}")

    async def _verify_round(self, height):
        colors = await self._all_colors()
        for node in self._nodes:
            actual_tpc = await node.rpc.call("getbalance", [False])
            if node.name in COINBASE_EARNING_NODES:
                # Ongoing coinbase income (whichever signer proposes each round) makes
                # this node's TPC balance unpredictable from this script's own ledger
                # alone -- log it, don't assert it. Colored balances are unaffected
                # (coinbase never carries a color) and stay fully asserted below.
                log.info(f"height {height}: {node.name} TPC: {actual_tpc} (coinbase-earning, not asserted)")
            else:
                self._compare(height, node.name, TPC, self._ledger[node.name][TPC], actual_tpc)
            for color in colors:
                actual = await node.rpc.call("getbalance", [False, color])
                expected = self._ledger[node.name].get(color, 0)
                self._compare(height, node.name, color, expected, actual)

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

    async def _apply_fee(self, node, txid):
        # tapyrus-core's gettransaction has no top-level "fee" field here -- and the
        # fee's shape within "details" itself varies by tx type (confirmed
        # against a live node; see doc/work-done.md): a plain TPC send nests "fee"
        # inside its own category="send" entry, while a transaction that also moves a
        # colored output puts it in a separate category="fee" entry instead (and the
        # send/receive entries have no "fee" key of their own in that case).
        tx = await node.rpc.call("gettransaction", [txid])
        fee = 0
        for detail in tx.get("details", []):
            if detail.get("category") == "fee":
                fee = detail["amount"]
                break
            if detail.get("category") == "send" and "fee" in detail:
                fee = detail["fee"]
                break
        self._ledger[node.name][TPC] += fee


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
