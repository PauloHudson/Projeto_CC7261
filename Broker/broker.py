import time
import zmq


FRONTEND_ENDPOINT = "tcp://*:5555" #cliente
BACKEND_ENDPOINT = "tcp://*:5556" #servidor


def main() -> None:
    context = zmq.Context.instance()
    frontend = context.socket(zmq.ROUTER)
    backend = context.socket(zmq.DEALER)

    frontend.bind(FRONTEND_ENDPOINT)
    backend.bind(BACKEND_ENDPOINT)

    print(f"[BROKER] iniciou o frontend={FRONTEND_ENDPOINT} backend={BACKEND_ENDPOINT}", flush=True)

    try:
        zmq.proxy(frontend, backend)
    except KeyboardInterrupt:
        print("[BROKER] Interrupted", flush=True)
    finally:
        frontend.close(0)
        backend.close(0)
        context.term()
        time.sleep(0.2)


if __name__ == "__main__":
    main()
