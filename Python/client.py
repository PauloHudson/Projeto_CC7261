from __future__ import annotations

import os
import random
import threading
import time
from datetime import datetime, timezone

import msgpack
import zmq


FRONTEND_ENDPOINT = os.getenv("FRONTEND_ENDPOINT", "tcp://broker:5555")
PUBSUB_SUB_ENDPOINT = os.getenv("PUBSUB_SUB_ENDPOINT", "tcp://pubsub_proxy:5558")
USERNAME = os.getenv("USERNAME", "py_bot")

MESSAGE_BANK = [
    "checando status",
    "mensagem de teste",
    "publicacao automatica",
    "sincronizando canal",
    "novo evento recebido",
    "validando fluxo pubsub",
]

logical_clock = 0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def logical_clock_receive(received_value: int | None) -> None:
    global logical_clock
    incoming = int(received_value or 0)
    logical_clock = max(logical_clock, incoming)


def logical_clock_send() -> int:
    global logical_clock
    logical_clock += 1
    return logical_clock


def send_request(socket: zmq.Socket, payload: dict) -> dict:
    payload["timestamp"] = now_iso()
    payload["logical_clock"] = logical_clock_send()

    print(f"[PY-CLIENT:{USERNAME}] TX {payload}", flush=True)
    socket.send(msgpack.packb(payload, use_bin_type=True))
    raw = socket.recv()
    response = msgpack.unpackb(raw, raw=False)
    logical_clock_receive(response.get("logical_clock"))
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
    response = send_request(socket, {"type": "request", "action": "list_channels"})
    return list((response.get("payload") or {}).get("channels") or [])


def create_channels_until_five(socket: zmq.Socket, channels: list[str]) -> list[str]:
    while len(channels) < 5:
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


def subscribe_up_to_three(subscriber: zmq.Socket, subscribed: set[str], channels: list[str]) -> None:
    choices = [channel for channel in channels if channel not in subscribed]
    while len(subscribed) < 3 and choices:
        picked = random.choice(choices)
        subscriber.setsockopt_string(zmq.SUBSCRIBE, picked)
        subscribed.add(picked)
        choices.remove(picked)
        print(f"[PY-CLIENT:{USERNAME}] SUBSCRIBED {picked}", flush=True)


def publication_listener(subscriber: zmq.Socket, running: threading.Event) -> None:
    while running.is_set():
        try:
            parts = subscriber.recv_multipart(flags=zmq.NOBLOCK)
        except zmq.Again:
            time.sleep(0.05)
            continue

        if len(parts) != 2:
            continue

        channel = parts[0].decode("utf-8", errors="replace")
        publication = msgpack.unpackb(parts[1], raw=False)
        logical_clock_receive(publication.get("logical_clock"))
        print(
            f"[PY-CLIENT:{USERNAME}] PUB channel={channel} message={publication.get('message')} "
            f"sent={publication.get('published_timestamp')} received={now_iso()}",
            flush=True,
        )


def publish_message(socket: zmq.Socket, channel: str) -> None:
    text = f"{random.choice(MESSAGE_BANK)} #{random.randint(1000, 9999)}"
    send_request(
        socket,
        {
            "type": "request",
            "action": "publish_message",
            "username": USERNAME,
            "channel": channel,
            "message": text,
        },
    )


def main() -> None:
    context = zmq.Context.instance()

    requester = context.socket(zmq.REQ)
    requester.connect(FRONTEND_ENDPOINT)

    subscriber = context.socket(zmq.SUB)
    subscriber.connect(PUBSUB_SUB_ENDPOINT)

    print(
        f"[PY-CLIENT:{USERNAME}] started frontend={FRONTEND_ENDPOINT} pubsub={PUBSUB_SUB_ENDPOINT}",
        flush=True,
    )

    run_flag = threading.Event()
    run_flag.set()
    listener = threading.Thread(target=publication_listener, args=(subscriber, run_flag), daemon=True)
    listener.start()

    try:
        login_with_retry(requester)

        channels = list_channels(requester)
        channels = create_channels_until_five(requester, channels)

        subscribed_channels: set[str] = set()
        subscribe_up_to_three(subscriber, subscribed_channels, channels)

        while True:
            if not channels:
                channels = list_channels(requester)

            channel = random.choice(channels)
            for _ in range(10):
                publish_message(requester, channel)
                time.sleep(1)

            channels = list_channels(requester)
            channels = create_channels_until_five(requester, channels)
            subscribe_up_to_three(subscriber, subscribed_channels, channels)
    except KeyboardInterrupt:
        print(f"[PY-CLIENT:{USERNAME}] interrupted", flush=True)
    finally:
        run_flag.clear()
        listener.join(timeout=1)
        requester.close(0)
        subscriber.close(0)
        context.term()


if __name__ == "__main__":
    main()