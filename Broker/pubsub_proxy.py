import time

import zmq


XSUB_ENDPOINT = "tcp://*:5557"
XPUB_ENDPOINT = "tcp://*:5558"


def main() -> None:
    context = zmq.Context.instance()
    xsub = context.socket(zmq.XSUB)
    xpub = context.socket(zmq.XPUB)

    xsub.bind(XSUB_ENDPOINT)
    xpub.bind(XPUB_ENDPOINT)

    print(
        f"[PUBSUB-PROXY] started xsub={XSUB_ENDPOINT} xpub={XPUB_ENDPOINT}",
        flush=True,
    )

    try:
        zmq.proxy(xsub, xpub)
    except KeyboardInterrupt:
        print("[PUBSUB-PROXY] Interrupted", flush=True)
    finally:
        xsub.close(0)
        xpub.close(0)
        context.term()
        time.sleep(0.2)


if __name__ == "__main__":
    main()