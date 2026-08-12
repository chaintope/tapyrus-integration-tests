"""Minimal Tapyrus Core JSON-RPC client (stdlib only, no third-party dependency).

Shared by every script that talks to a core-* node's RPC port: the
topology-convergence wait (wait_for_topology.py), coinbase-address collection
(collect_coinbase_addresses.py), traffic generation (generate_traffic.py), the reorg
(simulate_reorg.py), the rotation/max-block-size change confirmation steps
(simulate_federation_change.py/simulate_maxblocksize_change.py), and the per-node
lifecycle orchestrator (scripts/container/node_orchestrator.py).

`call()` is async so a caller can poll multiple nodes concurrently (e.g. via
asyncio.gather) instead of one at a time -- the underlying urllib call is blocking, so
it runs in a worker thread via asyncio.to_thread rather than on the event loop itself.

Auth is tapyrus-core's own auto-generated per-process cookie file (see cookie_path/
read_cookie below), not a static password -- see doc/work-done.md.
"""
import asyncio
import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

RPC_ID = "tapyrus-integration-tests"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class RpcError(Exception):
    """The RPC call reached the node, but it returned a JSON-RPC error."""


class RpcUnreachable(Exception):
    """The RPC call couldn't even reach the node (connection refused, timeout, node
    still starting up, ...). Callers polling for readiness should treat this as
    "not ready yet", not a hard failure."""


def cookie_path(name):
    """The shared, host-visible cookie file tapyrus-core writes for node `name`
    (docker/docker-compose.yml's ../runtime/rpc-cookies:/cookies mount,
    entrypoint_wrapper.sh's -rpccookiefile) -- see doc/work-done.md."""
    return REPO_ROOT / "runtime" / "rpc-cookies" / f"{name}.cookie"


def read_cookie(path):
    """Reads and parses a tapyrus-core cookie file (__cookie__:<64 hex chars>) into
    (user, password). A missing file -- not written yet (node still starting up) or
    momentarily absent mid-restart (tapyrus-core deletes it on clean shutdown, then
    regenerates fresh on the next startup) -- is RpcUnreachable, not a hard failure,
    so it flows through the same retry paths callers already use for an unreachable
    RPC port."""
    try:
        content = path.read_text().strip()
    except OSError as exc:
        raise RpcUnreachable(f"cookie file {path} not readable: {exc}") from exc
    user, _, password = content.partition(":")
    return user, password


class CoreRpcClient:
    """One node's RPC endpoint, authenticated via its own cookie file (resolved from
    `name` via cookie_path() above, read fresh on every call via read_cookie() --
    not cached at construction time, since a chaos-restarted node's cookie changes
    on every restart). `host` is separate from `name` since it varies by caller:
    127.0.0.1 for host-side scripts (docker-compose's published host ports), a
    container DNS name for node_orchestrator.py's cross-node peer checks. Cheap to
    construct -- holds no connection state of its own, so a fresh instance per call
    (or per polling attempt) is fine.
    """

    def __init__(self, host, port, name, timeout_seconds=5):
        self._url = f"http://{host}:{port}/"
        self._cookie_file = cookie_path(name)
        self._timeout_seconds = timeout_seconds

    async def call(self, method, params=None):
        return await asyncio.to_thread(self._call_sync, method, params)

    def _call_sync(self, method, params):
        user, password = read_cookie(self._cookie_file)
        body = json.dumps(
            {"jsonrpc": "1.0", "id": RPC_ID, "method": method, "params": params or []}
        ).encode()
        credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
        request = urllib.request.Request(
            self._url, data=body,
            headers={"Content-Type": "text/plain", "Authorization": f"Basic {credentials}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # The node answered (wrong RPC credentials, malformed request, still
            # warming up, ...) -- HTTPError is a URLError subclass, so it must be
            # caught here, ahead of the URLError branch below, or it would be
            # misclassified as "not reachable yet" and retried forever instead of
            # surfacing the real error.
            #
            # tapyrusd serves RPC_IN_WARMUP (-28) as HTTP 500 with a JSON-RPC error
            # body (JSONRPCError -> JSONErrorReply, src/rpc/protocol.cpp /
            # src/httprpc.cpp) -- that's the readiness window right after `docker
            # compose up`, so it must stay retryable like RpcUnreachable, not become a
            # hard failure. A bad-credentials 401 has no body at all
            # (HTTPReq_JSONRPC's auth-failure path calls WriteReply with no body), so
            # the JSON parse below is expected to fail for that case -- caught and
            # treated as "no error object", not re-raised.
            try:
                error = json.loads(exc.read()).get("error") or {}
            except (json.JSONDecodeError, ValueError):
                error = {}
            if error.get("code") == -28:
                raise RpcUnreachable(
                    f"{method} against {self._url}: still warming up ({error.get('message', 'RPC_IN_WARMUP')})"
                ) from exc
            detail = f"{error['code']} {error['message']}" if error else f"HTTP {exc.code} {exc.reason}"
            raise RpcError(f"{method} against {self._url}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise RpcUnreachable(f"{method} against {self._url}: {exc}") from exc

        if payload.get("error") is not None:
            raise RpcError(f"{method} against {self._url}: {payload['error']}")
        return payload["result"]
