import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Iterable

import msgpack
import zmq


FRONTEND_ENDPOINT = os.getenv("FRONTEND_ENDPOINT", "tcp://broker:5555")
PUBSUB_SUB_ENDPOINT = os.getenv("PUBSUB_SUB_ENDPOINT", "tcp://pubsub_proxy:5558")
USERNAME = os.getenv("USERNAME", "bot_python")
MESSAGE_BANK = [
    "checando status",
    "mensagem de teste",
    "publicacao automatica",
    "sincronizando canal",
    "novo evento recebido",
    "validando fluxo pubsub",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def send_request(socket: zmq.Socket, payload: dict) -> dict:
    payload["timestamp"] = now_iso()
    packed = msgpack.packb(payload, use_bin_type=True)
    print(f"[PY-CLIENT:{USERNAME}] TX {payload}", flush=True)
    socket.send(packed)

    raw = socket.recv()
    response = msgpack.unpackb(raw, raw=False)
    print(f"[PY-CLIENT:{USERNAME}] RX {response}", flush=True)
    return response


def login_with_retry(socket: zmq.Socket) -> None:
    while True:
        response = send_request(
            socket,
            {
                "type": "request",
                "action": "login",
                "username": USERNAME,
            },
        )
        if response.get("status") == "ok":
            return
        time.sleep(2)


def list_channels(socket: zmq.Socket) -> list[str]:
    response = send_request(
        socket,
        {
            "type": "request",
            "action": "list_channels",
        },
    )
    payload = response.get("payload", {})
    return list(payload.get("channels", []))


def create_new_channel(socket: zmq.Socket, current_channels: Iterable[str]) -> list[str]:
    channels = list(current_channels)
    for _ in range(10):
        if len(channels) >= 5:
            return channels

        candidate = f"auto_{len(channels) + 1}_{random.randint(100, 999)}"[:24]
        response = send_request(
            socket,
            {
                "type": "request",
                "action": "create_channel",
                "channel": candidate,
            },
        )
        if response.get("status") == "ok":
            channels = list_channels(socket)
        else:
            time.sleep(1)

    return channels


def ensure_subscriptions(
    sub_socket: zmq.Socket, subscribed_channels: set[str], available_channels: list[str]
) -> None:
    remaining = [channel for channel in available_channels if channel not in subscribed_channels]
    while len(subscribed_channels) < 3 and remaining:
        channel = random.choice(remaining)
        sub_socket.setsockopt_string(zmq.SUBSCRIBE, channel)
        subscribed_channels.add(channel)
        remaining.remove(channel)
        print(f"[PY-CLIENT:{USERNAME}] SUBSCRIBED {channel}", flush=True)


def listen_publications(sub_socket: zmq.Socket, stop_event: threading.Event) -> None:
    poller = zmq.Poller()
    poller.register(sub_socket, zmq.POLLIN)

    while not stop_event.is_set():
        events = dict(poller.poll(250))
        if sub_socket not in events:
            continue

        topic, raw = sub_socket.recv_multipart()
        publication = msgpack.unpackb(raw, raw=False)
        received_timestamp = now_iso()
        channel = topic.decode("utf-8")
        print(
            (
                f"[PY-CLIENT:{USERNAME}] PUB channel={channel} "
                f"message={publication.get('message')} "
                f"sent={publication.get('published_timestamp')} "
                f"received={received_timestamp}"
            ),
            flush=True,
        )


def publish_message(socket: zmq.Socket, channel: str) -> None:
    message = random.choice(MESSAGE_BANK) + f" #{random.randint(1000, 9999)}"
    send_request(
        socket,
        {
            "type": "request",
            "action": "publish_message",
            "username": USERNAME,
            "channel": channel,
            "message": message,
        },
    )


def main() -> None:
    context = zmq.Context.instance()
    req_socket = context.socket(zmq.REQ)
    req_socket.connect(FRONTEND_ENDPOINT)

    sub_socket = context.socket(zmq.SUB)
    sub_socket.connect(PUBSUB_SUB_ENDPOINT)

    print(
        f"[PY-CLIENT:{USERNAME}] Connected to {FRONTEND_ENDPOINT} pubsub={PUBSUB_SUB_ENDPOINT}",
        flush=True,
    )

    stop_event = threading.Event()
    listener = threading.Thread(
        target=listen_publications, args=(sub_socket, stop_event), daemon=True
    )

    try:
        login_with_retry(req_socket)

        available_channels = list_channels(req_socket)
        available_channels = create_new_channel(req_socket, available_channels)

        subscribed_channels: set[str] = set()
        ensure_subscriptions(sub_socket, subscribed_channels, available_channels)

        listener.start()

        while True:
            if not available_channels:
                available_channels = list_channels(req_socket)

            channel = random.choice(available_channels)
            for _ in range(10):
                publish_message(req_socket, channel)
                time.sleep(1)

            available_channels = list_channels(req_socket)
            available_channels = create_new_channel(req_socket, available_channels)
            ensure_subscriptions(sub_socket, subscribed_channels, available_channels)
    except KeyboardInterrupt:
        print(f"[PY-CLIENT:{USERNAME}] Interrupted", flush=True)
    finally:
        stop_event.set()
        sub_socket.close(0)
        req_socket.close(0)
        context.term()


if __name__ == "__main__":
    main()