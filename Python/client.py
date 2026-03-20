import os
import time
from datetime import datetime, timezone

import msgpack
import zmq


FRONTEND_ENDPOINT = os.getenv("FRONTEND_ENDPOINT", "tcp://broker:5555")
USERNAME = os.getenv("USERNAME", "bot_python")
CHANNEL_CANDIDATES = [
    os.getenv("CHANNEL_1", "geral"),
    os.getenv("CHANNEL_2", "duvidas"),
    os.getenv("CHANNEL_3", "avisos"),
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


def main() -> None:
    context = zmq.Context.instance()
    socket = context.socket(zmq.REQ)
    socket.connect(FRONTEND_ENDPOINT)

    print(f"[PY-CLIENT:{USERNAME}] Connected to {FRONTEND_ENDPOINT}", flush=True)

    try:
        login_with_retry(socket)

        step = 0
        while True:
            if step % 3 == 0:
                channel = CHANNEL_CANDIDATES[(step // 3) % len(CHANNEL_CANDIDATES)]
                send_request(
                    socket,
                    {
                        "type": "request",
                        "action": "create_channel",
                        "channel": channel,
                    },
                )
            else:
                send_request(
                    socket,
                    {
                        "type": "request",
                        "action": "list_channels",
                    },
                )

            step += 1
            time.sleep(5)
    except KeyboardInterrupt:
        print(f"[PY-CLIENT:{USERNAME}] Interrupted", flush=True)
    finally:
        socket.close(0)
        context.term()


if __name__ == "__main__":
    main()
