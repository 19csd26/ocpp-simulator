import argparse
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="Volkai: OCPP 1.6 / 2.0.1 charge point simulator (web UI + API)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address; anything but localhost requires SIM_TOKEN")
    ap.add_argument("--insecure", action="store_true", help="allow a non-local bind without SIM_TOKEN (anyone who can reach the port controls the simulator)")
    ap.add_argument("--port", type=int, default=8100)
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("SIM_TOKEN") and not args.insecure:
        ap.error(f"refusing to listen on {args.host} without authentication: set SIM_TOKEN to a long random value (or pass --insecure)")
    from ocpp_simulator import __author__, __copyright__, __title__, __url__, __version__

    print(f"{__title__} {__version__}  ·  simulate any charger, break it on purpose  ·  designed and built by {__author__}  ·  {__url__}\n{__copyright__}\n", flush=True)
    uvicorn.run("ocpp_simulator.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
