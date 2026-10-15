import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="OCPP 2.0.1 charging station simulator (web UI + API)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (keep 127.0.0.1: the API has no authentication)")
    ap.add_argument("--port", type=int, default=8100)
    args = ap.parse_args()
    uvicorn.run("ocpp_simulator.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
