import json
import os
import re
import time
from pathlib import Path
from typing import Any

import msgpack
import zmq


BACKEND_ENDPOINT = os.getenv("BACKEND_ENDPOINT", "tcp://broker:5556")
PUBSUB_PUB_ENDPOINT = os.getenv("PUBSUB_PUB_ENDPOINT", "tcp://pubsub_proxy:5557")
SERVICE_NAME = os.getenv("SERVICE_NAME", "py_server")
DATA_FILE = Path(os.getenv("DATA_FILE", f"/app/data/{SERVICE_NAME}.json"))

USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
CHANNEL_REGEX = re.compile(r"^[a-z0-9_-]{2,24}$")


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict[str, Any]:
    return {
        "logins": [],
        "channels": ["geral"],
        "publications": [],
    }


def load_state() -> dict[str, Any]:
    state = default_state()
    if DATA_FILE.exists():
        with DATA_FILE.open("r", encoding="utf-8") as file:
            loaded_state = json.load(file)
        state.update(loaded_state)

    state.setdefault("logins", [])
    state.setdefault("channels", ["geral"])
    state.setdefault("publications", [])
    return state


def save_state(state: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=True, indent=2)


def ok_response(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "type": "response",
        "action": action,
        "status": "ok",
        "server": SERVICE_NAME,
        "timestamp": now_iso(),
    }
    if payload:
        data["payload"] = payload
    return data


def error_response(action: str, reason: str) -> dict[str, Any]:
    return {
        "type": "response",
        "action": action,
        "status": "error",
        "reason": reason,
        "server": SERVICE_NAME,
        "timestamp": now_iso(),
    }


def handle_login(message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    username = str(message.get("username", "")).strip()
    if not USERNAME_REGEX.fullmatch(username):
        return error_response("login", "invalid_username")

    state["logins"].append(
        {
            "username": username,
            "timestamp": message.get("timestamp", now_iso()),
            "server": SERVICE_NAME,
        }
    )
    save_state(state)
    return ok_response("login", {"username": username})


def handle_create_channel(message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    channel = str(message.get("channel", "")).strip().lower()
    if not CHANNEL_REGEX.fullmatch(channel):
        return error_response("create_channel", "invalid_channel")

    if channel in state["channels"]:
        return error_response("create_channel", "channel_already_exists")

    state["channels"].append(channel)
    save_state(state)
    return ok_response("create_channel", {"channel": channel})


def handle_list_channels(state: dict[str, Any]) -> dict[str, Any]:
    channels = sorted(state["channels"])
    return ok_response("list_channels", {"channels": channels})


def handle_publish_message(
    message: dict[str, Any], state: dict[str, Any], publisher: zmq.Socket
) -> dict[str, Any]:
    channel = str(message.get("channel", "")).strip().lower()
    text = str(message.get("message", "")).strip()
    username = str(message.get("username", "")).strip() or "anon"

    if not CHANNEL_REGEX.fullmatch(channel):
        return error_response("publish_message", "invalid_channel")

    if channel not in state["channels"]:
        state["channels"].append(channel)

    if not text:
        return error_response("publish_message", "empty_message")

    publication = {
        "type": "publication",
        "channel": channel,
        "message": text,
        "username": username,
        "request_timestamp": message.get("timestamp", now_iso()),
        "published_timestamp": now_iso(),
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
    action = message.get("action")

    if action == "login":
        return handle_login(message, state)
    if action == "create_channel":
        return handle_create_channel(message, state)
    if action == "list_channels":
        return handle_list_channels(state)
    if action == "publish_message":
        return handle_publish_message(message, state, publisher)

    return error_response(str(action), "unknown_action")


def main() -> None:
    context = zmq.Context.instance()
    socket = context.socket(zmq.REP)
    socket.connect(BACKEND_ENDPOINT)

    publisher = context.socket(zmq.PUB)
    publisher.connect(PUBSUB_PUB_ENDPOINT)

    state = load_state()
    save_state(state)

    print(
        (
            f"[PY-SERVER:{SERVICE_NAME}] Connected to {BACKEND_ENDPOINT} "
            f"pubsub={PUBSUB_PUB_ENDPOINT} data={DATA_FILE}"
        ),
        flush=True,
    )

    try:
        time.sleep(0.2)
        while True:
            raw = socket.recv()
            message = msgpack.unpackb(raw, raw=False)
            print(f"[PY-SERVER:{SERVICE_NAME}] RX {message}", flush=True)

            response = process_request(message, state, publisher)
            encoded = msgpack.packb(response, use_bin_type=True)

            socket.send(encoded)
            print(f"[PY-SERVER:{SERVICE_NAME}] TX {response}", flush=True)
    except KeyboardInterrupt:
        print(f"[PY-SERVER:{SERVICE_NAME}] Interrupted", flush=True)
    finally:
        socket.close(0)
        publisher.close(0)
        context.term()


if __name__ == "__main__":
    main()