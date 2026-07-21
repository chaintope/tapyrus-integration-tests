"""Minimal Tapyrus Core JSON-RPC client (stdlib only, no third-party dependency).

Shared by every script that talks to a core-* node's RPC port: the
topology-convergence wait (wait_for_topology.py) today, and the per-node tx/lifecycle
orchestrator and rotation-confirmation step once those are built.

`call()` is async so a caller can poll multiple nodes concurrently (e.g. via
asyncio.gather) instead of one at a time -- the underlying urllib call is blocking, so
it runs in a worker thread via asyncio.to_thread rather than on the event loop itself.
"""
import asyncio
import base64
import json
import urllib.error
import urllib.request

RPC_ID = "tapyrus-integration-tests"


class RpcError(Exception):
    """The RPC call reached the node, but it returned a JSON-RPC error."""


class RpcUnreachable(Exception):
    """The RPC call couldn't even reach the node (connection refused, timeout, node
    still starting up, ...). Callers polling for readiness should treat this as
    "not ready yet", not a hard failure."""


class CoreRpcClient:
    """One node's RPC endpoint. Cheap to construct -- holds no connection state of
    its own, so a fresh instance per call (or per polling attempt) is fine.
    """

    def __init__(self, host, port, user, password, timeout_seconds=5):
        self._url = f"http://{host}:{port}/"
        self._user = user
        self._password = password
        self._timeout_seconds = timeout_seconds

    async def call(self, method, params=None):
        return await asyncio.to_thread(self._call_sync, method, params)

    def _call_sync(self, method, params):
        body = json.dumps(
            {"jsonrpc": "1.0", "id": RPC_ID, "method": method, "params": params or []}
        ).encode()
        credentials = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
        request = urllib.request.Request(
            self._url, data=body,
            headers={"Content-Type": "text/plain", "Authorization": f"Basic {credentials}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # The node answered (wrong RPC credentials, malformed request, ...) --
            # HTTPError is a URLError subclass, so it must be caught here, ahead of
            # the URLError branch below, or it would be misclassified as "not
            # reachable yet" and retried forever instead of surfacing the real error.
            raise RpcError(f"{method} against {self._url}: HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise RpcUnreachable(f"{method} against {self._url}: {exc}") from exc

        if payload.get("error") is not None:
            raise RpcError(f"{method} against {self._url}: {payload['error']}")
        return payload["result"]
