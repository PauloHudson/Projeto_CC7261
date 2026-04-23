from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List

import msgpack
import zmq


REFERENCE_ENDPOINT = "tcp://*:5560"
HEARTBEAT_TTL_SECONDS = 20


@dataclass
class ServerEntry:
    name: str
    rank: int
    last_seen_unix: float


logical_clock = 0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_unix() -> float:
    return datetime.now(timezone.utc).timestamp()


def clock_receive(received: int | None) -> None:
    global logical_clock
    incoming = int(received or 0)
    logical_clock = max(logical_clock, incoming)


def clock_send() -> int:
    global logical_clock
    logical_clock += 1
    return logical_clock


def prune_inactive(servers: Dict[str, ServerEntry]) -> None:
    limit = now_unix() - HEARTBEAT_TTL_SECONDS
    expired_names = [name for name, entry in servers.items() if entry.last_seen_unix < limit]
    for name in expired_names:
        del servers[name]


def get_next_rank(servers: Dict[str, ServerEntry]) -> int:
    if not servers:
        return 1
    return max(entry.rank for entry in servers.values()) + 1


def response(action: str, status: str, payload: dict | None = None, reason: str | None = None) -> dict:
    body = {
        "type": "response",
        "action": action,
        "status": status,
        "timestamp": now_iso(),
        "logical_clock": clock_send(),
    }
    if payload is not None:
        body["payload"] = payload
    if reason is not None:
        body["reason"] = reason
    return body


def handle_register_server(message: dict, servers: Dict[str, ServerEntry]) -> dict:
    name = str(message.get("name") or "").strip()
    if not name:
        return response("register_server", "error", reason="invalid_name")

    if name not in servers:
        servers[name] = ServerEntry(name=name, rank=get_next_rank(servers), last_seen_unix=now_unix())
    else:
        servers[name].last_seen_unix = now_unix()

    payload = {
        "rank": servers[name].rank,
        "current_time": now_iso(),
    }
    return response("register_server", "ok", payload=payload)


def handle_list_servers(servers: Dict[str, ServerEntry]) -> dict:
    payload = {
        "servers": sorted(
            [{"name": entry.name, "rank": entry.rank} for entry in servers.values()],
            key=lambda item: item["rank"],
        ),
        "current_time": now_iso(),
    }
    return response("list_servers", "ok", payload=payload)


def handle_heartbeat(message: dict, servers: Dict[str, ServerEntry]) -> dict:
    name = str(message.get("name") or "").strip()
    if not name:
        return response("heartbeat", "error", reason="invalid_name")

    if name not in servers:
        servers[name] = ServerEntry(name=name, rank=get_next_rank(servers), last_seen_unix=now_unix())
    else:
        servers[name].last_seen_unix = now_unix()

    payload = {
        "ok": True,
        "rank": servers[name].rank,
        "current_time": now_iso(),
    }
    return response("heartbeat", "ok", payload=payload)


def process_message(message: dict, servers: Dict[str, ServerEntry]) -> dict:
    clock_receive(message.get("logical_clock"))
    prune_inactive(servers)

    action = str(message.get("action") or "")
    if action == "register_server":
        return handle_register_server(message, servers)
    if action == "list_servers":
        return handle_list_servers(servers)
    if action == "heartbeat":
        return handle_heartbeat(message, servers)
    return response(action or "unknown", "error", reason="unknown_action")


def main() -> None:
    context = zmq.Context.instance()
    socket = context.socket(zmq.REP)
    socket.bind(REFERENCE_ENDPOINT)

    servers: Dict[str, ServerEntry] = {}

    print(f"[REFERENCE] started endpoint={REFERENCE_ENDPOINT}", flush=True)

    try:
        while True:
            raw = socket.recv()
            message = msgpack.unpackb(raw, raw=False)
            print(f"[REFERENCE] RX {message}", flush=True)
            reply = process_message(message, servers)
            socket.send(msgpack.packb(reply, use_bin_type=True))
            print(f"[REFERENCE] TX {reply}", flush=True)
    except KeyboardInterrupt:
        print("[REFERENCE] interrupted", flush=True)
    finally:
        socket.close(0)
        context.term()


if __name__ == "__main__":
    main()