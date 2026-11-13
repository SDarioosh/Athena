"""athena-pda-syncd: `python3 -m athena_pda_sync [--once] [--debug]`.

Environment: `ATHENA_PDA_DIR` (token, pda.json, config.json; default /var/lib/athena-pda),
`ATHENA_PDA_RUN` (glance.sock; default /run/athena-pda), `ATHENA_RELAY_PATH` (where the
`athena_relay` package is, if not importable; see relay_path.py).

Pair a device without the UI (development): `--pair 'odysseus-pair:v1?url=…&code=…'`.
Logs go to stderr; no tokens, and no content at INFO.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from . import pairing as pr
from .config import ConfigFile, Paths
from .daemon import Daemon


def setup_logging(debug: bool) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not debug:  # its INFO lines name to-do items and conversation ids
        logging.getLogger("athena_relay").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="athena-pda-syncd", description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true",
                    help="connect to the UI, sync once and exit (0 ok, 1 changes waiting, "
                         "2 retry later, 3 unpaired or token refused)")
    ap.add_argument("--pair", metavar="LINK",
                    help="redeem a pairing link (the hub's QR text) and exit")
    ap.add_argument("--debug", action="store_true", help="log everything (names items)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    setup_logging(args.debug)
    paths = Paths.from_env()
    if args.pair:
        try:
            url, code = pr.parse_link(args.pair)
            paired = pr.redeem(url, code, proxy=ConfigFile(paths.config).config.proxy)
            pr.TokenStore(paths.token).save(paired)
        except (pr.PairError, OSError) as e:
            logging.getLogger("athena_pda_sync").error("pairing failed: %s", e)
            return 3
        logging.getLogger("athena_pda_sync").info("paired with %s", paired.host)
        return 0
    daemon = Daemon(paths)
    try:
        return asyncio.run(daemon.run_forever(once=args.once))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
