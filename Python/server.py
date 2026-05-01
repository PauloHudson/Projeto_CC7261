from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import msgpack
import zmq


BACKEND_ENDPOINT = os.getenv("BACKEND_ENDPOINT", "tcp://broker:5556")
PUBSUB_PUB_ENDPOINT = os.getenv("PUBSUB_PUB_ENDPOINT", "tcp://pubsub_proxy:5557")
PUBSUB_SUB_ENDPOINT = os.getenv("PUBSUB_SUB_ENDPOINT", "tcp://pubsub_proxy:5558")
REFERENCE_ENDPOINT = os.getenv("REFERENCE_ENDPOINT", "tcp://reference:5560")
SERVICE_NAME = os.getenv("SERVICE_NAME", "py_server")
DATA_FILE = Path(os.getenv("DATA_FILE", "/app/data/state.json"))
SERVER_SYNC_PORT = int(os.getenv("SERVER_SYNC_PORT", "5561"))
SERVER_SYNC_BIND = f"tcp://*:{SERVER_SYNC_PORT}"
DEFAULT_SERVER_NAMES = ["py_server_1", "py_server_2", "js_server_1", "js_server_2"]
ALL_SERVER_NAMES = [
    name.strip()
    for name in os.getenv("ALL_SERVER_NAMES", ",".join(DEFAULT_SERVER_NAMES)).split(",")
    if name.strip()
]
HEARTBEAT_INTERVAL_MESSAGES = 10
SYNC_INTERVAL_MESSAGES = 15

logical_clock = 0
clock_offset_seconds = 0.0


def now_iso() -> str:
    now = datetime.now(timezone.utc).timestamp() + clock_offset_seconds
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def logical_clock_receive(received_value: int | None) -> None:
    global logical_clock
    incoming = int(received_value or 0)
    logical_clock = max(logical_clock, incoming)


def logical_clock_send() -> int:
    global logical_clock
    logical_clock += 1
    return logical_clock


def sync_physical_clock(reference_time: str | None) -> None:
    global clock_offset_seconds
    if not reference_time:
        return
    try:
        ref_dt = datetime.fromisoformat(reference_time)
        local_dt = datetime.now(timezone.utc)
        clock_offset_seconds = (ref_dt - local_dt).total_seconds()
    except ValueError:
        return


def default_state() -> dict[str, Any]:
    return {
        "logins": [],
        "channels": ["geral"],
        "publications": [],
        "logical_clock": 0,
        "server_rank": None,
        "known_servers": [],
        "last_heartbeat": None,
        "coordinator_name": None,
    }


def load_state() -> dict[str, Any]:
    state = default_state()
    if DATA_FILE.exists():
        try:
            existing = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                state.update(existing)
        except json.JSONDecodeError:
            pass

    state.setdefault("logins", [])
    state.setdefault("channels", ["geral"])
    state.setdefault("publications", [])
    state.setdefault("logical_clock", 0)
    state.setdefault("server_rank", None)
    state.setdefault("known_servers", [])
    state.setdefault("last_heartbeat", None)
    state.setdefault("coordinator_name", None)
    return state


def save_state(state: dict[str, Any]) -> None:
    state["logical_clock"] = logical_clock
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


def ok_response(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "response",
        "action": action,
        "status": "ok",
        "server": SERVICE_NAME,
        "timestamp": now_iso(),
        "logical_clock": logical_clock_send(),
    }
    if payload is not None:
        body["payload"] = payload
    return body


def error_response(action: str, reason: str) -> dict[str, Any]:
    return {
        "type": "response",
        "action": action,
        "status": "error",
        "reason": reason,
        "server": SERVICE_NAME,
        "timestamp": now_iso(),
        "logical_clock": logical_clock_send(),
    }


def reference_request(reference_socket: zmq.Socket, action: str) -> dict[str, Any]:
    request = {
        "type": "request",
        "action": action,
        "name": SERVICE_NAME,
        "timestamp": now_iso(),
        "logical_clock": logical_clock_send(),
    }
    reference_socket.send(msgpack.packb(request, use_bin_type=True))
    raw = reference_socket.recv()
    response = msgpack.unpackb(raw, raw=False)
    logical_clock_receive(response.get("logical_clock"))
    current_time = ((response.get("payload") or {}).get("current_time"))
    sync_physical_clock(current_time)
    return response


def peer_endpoint(server_name: str) -> str:
    return f"tcp://{server_name}:{SERVER_SYNC_PORT}"


def list_known_servers(reference_socket: zmq.Socket, state: dict[str, Any]) -> list[dict[str, Any]]:
    response = reference_request(reference_socket, "list_servers")
    if response.get("status") == "ok":
        state["known_servers"] = (response.get("payload") or {}).get("servers", [])
        save_state(state)
    return list(state.get("known_servers") or [])


def publish_coordinator(publisher: zmq.Socket, coordinator_name: str) -> None:
    message = {
        "type": "server_announcement",
        "action": "coordinator_elected",
        "coordinator": coordinator_name,
        "server": SERVICE_NAME,
        "timestamp": now_iso(),
        "logical_clock": logical_clock_send(),
    }
    publisher.send_multipart([b"servers", msgpack.packb(message, use_bin_type=True)])
    print(f"[PY-SERVER:{SERVICE_NAME}] PUB servers {message}", flush=True)


def request_peer(server_name: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if server_name == SERVICE_NAME:
        return None

    context = zmq.Context.instance()
    socket = context.socket(zmq.REQ)
    socket.setsockopt(zmq.RCVTIMEO, 1000)
    socket.setsockopt(zmq.SNDTIMEO, 1000)
    socket.setsockopt(zmq.LINGER, 0)

    try:
        socket.connect(peer_endpoint(server_name))
        request = {
            "type": "server_request",
            "action": action,
            "server": SERVICE_NAME,
            "timestamp": now_iso(),
            "logical_clock": logical_clock_send(),
        }
        if payload is not None:
            request["payload"] = payload

        socket.send(msgpack.packb(request, use_bin_type=True))
        raw = socket.recv()
        response = msgpack.unpackb(raw, raw=False)
        logical_clock_receive(response.get("logical_clock"))
        return response
    except zmq.ZMQError:
        return None
    finally:
        socket.close(0)


def elect_coordinator(
    state: dict[str, Any],
    publisher: zmq.Socket,
    reference_socket: zmq.Socket,
) -> str:
    known_servers = list_known_servers(reference_socket, state)
    self_rank = int(state.get("server_rank") or 0)

    higher_rank_servers = [
        item
        for item in known_servers
        if item.get("name") != SERVICE_NAME and int(item.get("rank") or 0) > self_rank
    ]

    responders: list[dict[str, Any]] = []
    for item in sorted(higher_rank_servers, key=lambda x: int(x.get("rank") or 0), reverse=True):
        response = request_peer(
            str(item.get("name")),
            "election_request",
            {"candidate": SERVICE_NAME, "candidate_rank": self_rank},
        )
        if response and response.get("status") == "ok":
            responders.append(
                {
                    "name": response.get("server") or item.get("name"),
                    "rank": int((response.get("payload") or {}).get("rank") or item.get("rank") or 0),
                }
            )

    if responders:
        winner = sorted(responders, key=lambda x: int(x.get("rank") or 0), reverse=True)[0]
        coordinator_name = str(winner.get("name"))
        state["coordinator_name"] = coordinator_name
        save_state(state)
        print(f"[PY-SERVER:{SERVICE_NAME}] coordinator set to {coordinator_name} (election response)", flush=True)
        return coordinator_name

    state["coordinator_name"] = SERVICE_NAME
    save_state(state)
    publish_coordinator(publisher, SERVICE_NAME)
    print(f"[PY-SERVER:{SERVICE_NAME}] elected as coordinator", flush=True)
    return SERVICE_NAME


def sync_with_coordinator(
    state: dict[str, Any],
    publisher: zmq.Socket,
    reference_socket: zmq.Socket,
) -> None:
    coordinator_name = str(state.get("coordinator_name") or "")
    if not coordinator_name:
        coordinator_name = elect_coordinator(state, publisher, reference_socket)

    if coordinator_name == SERVICE_NAME:
        return

    response = request_peer(
        coordinator_name,
        "clock_sync_request",
        {"requester": SERVICE_NAME},
    )

    if not response or response.get("status") != "ok":
        print(
            f"[PY-SERVER:{SERVICE_NAME}] coordinator {coordinator_name} unavailable, triggering election",
            flush=True,
        )
        elect_coordinator(state, publisher, reference_socket)
        return

    payload = response.get("payload") or {}
    sync_physical_clock(payload.get("current_time"))
    print(
        f"[PY-SERVER:{SERVICE_NAME}] synchronized with coordinator={coordinator_name} at {payload.get('current_time')}",
        flush=True,
    )


def handle_peer_request(message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    action = str(message.get("action") or "")

    if action == "election_request":
        return ok_response("election_request", {"ok": True, "rank": int(state.get("server_rank") or 0)})

    if action == "clock_sync_request":
        coordinator_name = str(state.get("coordinator_name") or "")
        if coordinator_name != SERVICE_NAME:
            return error_response("clock_sync_request", "not_coordinator")
        return ok_response("clock_sync_request", {"current_time": now_iso()})

    return error_response(action or "unknown", "unknown_peer_action")


def handle_servers_announcement(raw_payload: bytes, state: dict[str, Any]) -> None:
    try:
        message = msgpack.unpackb(raw_payload, raw=False)
    except Exception:
        return

    logical_clock_receive(message.get("logical_clock"))
    if message.get("action") != "coordinator_elected":
        return

    coordinator_name = str(message.get("coordinator") or "").strip()
    if coordinator_name and coordinator_name != state.get("coordinator_name"):
        state["coordinator_name"] = coordinator_name
        save_state(state)
        print(
            f"[PY-SERVER:{SERVICE_NAME}] coordinator updated from announcement: {coordinator_name}",
            flush=True,
        )


def handle_login(message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    username = str(message.get("username") or "").strip()
    if not username:
        return error_response("login", "invalid_username")

    state["logins"].append(
        {
            "username": username,
            "timestamp": message.get("timestamp") or now_iso(),
            "logical_clock": message.get("logical_clock") or 0,
            "server": SERVICE_NAME,
        }
    )
    save_state(state)
    return ok_response("login", {"username": username})


def handle_create_channel(message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    channel = str(message.get("channel") or "").strip().lower()
    if not channel:
        return error_response("create_channel", "invalid_channel")
    if channel in state["channels"]:
        return error_response("create_channel", "channel_already_exists")

    state["channels"].append(channel)
    save_state(state)
    return ok_response("create_channel", {"channel": channel})


def handle_list_channels(state: dict[str, Any]) -> dict[str, Any]:
    return ok_response("list_channels", {"channels": sorted(state["channels"])})


def handle_publish_message(
    message: dict[str, Any], state: dict[str, Any], publisher: zmq.Socket
) -> dict[str, Any]:
    channel = str(message.get("channel") or "").strip().lower()
    text = str(message.get("message") or "").strip()
    username = str(message.get("username") or "").strip() or "anon"

    if not channel:
        return error_response("publish_message", "invalid_channel")
    if not text:
        return error_response("publish_message", "empty_message")

    # Mantem o ajuste da parte 2: publicar em canal ausente nao falha.
    if channel not in state["channels"]:
        state["channels"].append(channel)

    publication = {
        "type": "publication",
        "channel": channel,
        "message": text,
        "username": username,
        "request_timestamp": message.get("timestamp") or now_iso(),
        "request_logical_clock": message.get("logical_clock") or 0,
        "published_timestamp": now_iso(),
        "logical_clock": logical_clock_send(),
        "server": SERVICE_NAME,
    }

    state["publications"].append(publication)
    save_state(state)

    publisher.send_multipart([channel.encode("utf-8"), msgpack.packb(publication, use_bin_type=True)])

    return ok_response(
        "publish_message",
        {
            "channel": channel,
            "message": text,
            "published_timestamp": publication["published_timestamp"],
        },
    )


def process_request(
    message: dict[str, Any], state: dict[str, Any], publisher: zmq.Socket
) -> dict[str, Any]:
    action = str(message.get("action") or "")
    if action == "login":
        return handle_login(message, state)
    if action == "create_channel":
        return handle_create_channel(message, state)
    if action == "list_channels":
        return handle_list_channels(state)
    if action == "publish_message":
        return handle_publish_message(message, state, publisher)
    return error_response(action or "unknown", "unknown_action")


def main() -> None:
    context = zmq.Context.instance()

    backend = context.socket(zmq.REP)
    backend.connect(BACKEND_ENDPOINT)

    publisher = context.socket(zmq.PUB)
    publisher.connect(PUBSUB_PUB_ENDPOINT)

    subscriber = context.socket(zmq.SUB)
    subscriber.connect(PUBSUB_SUB_ENDPOINT)
    subscriber.setsockopt_string(zmq.SUBSCRIBE, "servers")

    reference = context.socket(zmq.REQ)
    reference.connect(REFERENCE_ENDPOINT)

    peer_server = context.socket(zmq.REP)
    peer_server.bind(SERVER_SYNC_BIND)

    state = load_state()

    global logical_clock
    logical_clock = int(state.get("logical_clock") or 0)

    register_resp = reference_request(reference, "register_server")
    if register_resp.get("status") == "ok":
        payload = register_resp.get("payload") or {}
        state["server_rank"] = payload.get("rank")

    list_resp = reference_request(reference, "list_servers")
    if list_resp.get("status") == "ok":
        payload = list_resp.get("payload") or {}
        state["known_servers"] = payload.get("servers", [])

    known_servers = list(state.get("known_servers") or [])
    if known_servers:
        top_server = sorted(known_servers, key=lambda item: int(item.get("rank") or 0), reverse=True)[0]
        state["coordinator_name"] = top_server.get("name")
    else:
        state["coordinator_name"] = SERVICE_NAME

    if state.get("coordinator_name") == SERVICE_NAME:
        publish_coordinator(publisher, SERVICE_NAME)

    save_state(state)

    print(
        f"[PY-SERVER:{SERVICE_NAME}] started backend={BACKEND_ENDPOINT} pubsub={PUBSUB_PUB_ENDPOINT} "
        f"reference={REFERENCE_ENDPOINT} rank={state.get('server_rank')} coordinator={state.get('coordinator_name')} "
        f"sync_bind={SERVER_SYNC_BIND} data={DATA_FILE}",
        flush=True,
    )

    poller = zmq.Poller()
    poller.register(backend, zmq.POLLIN)
    poller.register(peer_server, zmq.POLLIN)
    poller.register(subscriber, zmq.POLLIN)

    messages_received = 0
    try:
        while True:
            events = dict(poller.poll(timeout=1000))

            if backend in events:
                raw = backend.recv()
                message = msgpack.unpackb(raw, raw=False)
                logical_clock_receive(message.get("logical_clock"))
                print(f"[PY-SERVER:{SERVICE_NAME}] RX {message}", flush=True)

                response = process_request(message, state, publisher)
                backend.send(msgpack.packb(response, use_bin_type=True))
                print(f"[PY-SERVER:{SERVICE_NAME}] TX {response}", flush=True)

                messages_received += 1

                if messages_received % HEARTBEAT_INTERVAL_MESSAGES == 0:
                    heartbeat_resp = reference_request(reference, "heartbeat")
                    if heartbeat_resp.get("status") == "ok":
                        state["last_heartbeat"] = now_iso()
                        save_state(state)

                if messages_received % SYNC_INTERVAL_MESSAGES == 0:
                    sync_with_coordinator(state, publisher, reference)

            if peer_server in events:
                raw = peer_server.recv()
                message = msgpack.unpackb(raw, raw=False)
                logical_clock_receive(message.get("logical_clock"))
                print(f"[PY-SERVER:{SERVICE_NAME}] PEER-RX {message}", flush=True)
                response = handle_peer_request(message, state)
                peer_server.send(msgpack.packb(response, use_bin_type=True))
                print(f"[PY-SERVER:{SERVICE_NAME}] PEER-TX {response}", flush=True)

            if subscriber in events:
                topic, raw_payload = subscriber.recv_multipart()
                if topic.decode("utf-8", errors="ignore") == "servers":
                    handle_servers_announcement(raw_payload, state)
    except KeyboardInterrupt:
        print(f"[PY-SERVER:{SERVICE_NAME}] interrupted", flush=True)
    finally:
        backend.close(0)
        peer_server.close(0)
        publisher.close(0)
        subscriber.close(0)
        reference.close(0)
        context.term()


if __name__ == "__main__":
    main()