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
REFERENCE_ENDPOINT = os.getenv("REFERENCE_ENDPOINT", "tcp://reference:5560")
SERVICE_NAME = os.getenv("SERVICE_NAME", "py_server")
DATA_FILE = Path(os.getenv("DATA_FILE", "/app/data/state.json"))

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

    reference = context.socket(zmq.REQ)
    reference.connect(REFERENCE_ENDPOINT)

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

    save_state(state)

    print(
        f"[PY-SERVER:{SERVICE_NAME}] started backend={BACKEND_ENDPOINT} pubsub={PUBSUB_PUB_ENDPOINT} "
        f"reference={REFERENCE_ENDPOINT} rank={state.get('server_rank')} data={DATA_FILE}",
        flush=True,
    )

    messages_received = 0
    try:
        while True:
            raw = backend.recv()
            message = msgpack.unpackb(raw, raw=False)
            logical_clock_receive(message.get("logical_clock"))
            print(f"[PY-SERVER:{SERVICE_NAME}] RX {message}", flush=True)

            response = process_request(message, state, publisher)
            backend.send(msgpack.packb(response, use_bin_type=True))
            print(f"[PY-SERVER:{SERVICE_NAME}] TX {response}", flush=True)

            messages_received += 1
            if messages_received % 10 == 0:
                heartbeat_resp = reference_request(reference, "heartbeat")
                if heartbeat_resp.get("status") == "ok":
                    payload = heartbeat_resp.get("payload") or {}
                    state["last_heartbeat"] = payload.get("current_time")
                    save_state(state)
    except KeyboardInterrupt:
        print(f"[PY-SERVER:{SERVICE_NAME}] interrupted", flush=True)
    finally:
        backend.close(0)
        publisher.close(0)
        reference.close(0)
        context.term()


if __name__ == "__main__":
    main()