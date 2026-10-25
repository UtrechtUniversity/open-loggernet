#!/usr/bin/env python3
"""Read the CR6 clock, or set it to the host's UTC time.

`architecture.md` derives every schedule index in `Main.CR6` from the logger's own clock and
requires that clock to be UTC. The logger stores no time zone, so a logger left on local time
silently shifts the whole grid, including the SBD send unit of day.
"""

import argparse
import datetime
import sys
import time

from .pakbus import PakBusLink, PakBusError, find_port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: autodetected CR6)")
    parser.add_argument(
        "--set-utc",
        action="store_true",
        help="set the logger to the host's current UTC time",
    )
    parser.add_argument(
        "--max-drift",
        type=float,
        default=0.5,
        help="seconds of remaining offset to accept after a set (default: 0.5)",
    )
    args = parser.parse_args()

    port = args.port or find_port()
    if port is None:
        print("no CR6 serial port found; is it attached to WSL?", file=sys.stderr)
        return 1

    with PakBusLink(port) as link:
        if not link.hello():
            print("the logger did not answer a hello", file=sys.stderr)
            return 1

        before = time.time()
        logger = link.clock()
        after = time.time()
        host = datetime.datetime.fromtimestamp((before + after) / 2, datetime.UTC)
        offset = (logger - host).total_seconds()

        print(f"logger      {logger.isoformat(timespec='milliseconds')}")
        print(f"host UTC    {host.isoformat(timespec='milliseconds')}")
        print(f"offset      {offset:+.3f} s   (logger minus host, round trip "
              f"{after - before:.3f} s)")

        if not args.set_utc:
            return 0

        # The adjustment is applied by the logger to its own clock, so it is sent as the
        # negated offset measured a moment earlier; the transport delay is what limits the
        # result, which is why the outcome is measured again rather than assumed.
        adjust = -offset
        whole = int(adjust)
        nanos = int(round((adjust - whole) * 1e9))
        print(f"adjusting   {adjust:+.3f} s")
        try:
            link.clock(whole, nanos)
        except PakBusError as exc:
            print(f"clock set failed: {exc}", file=sys.stderr)
            return 1

        before = time.time()
        logger = link.clock()
        after = time.time()
        host = datetime.datetime.fromtimestamp((before + after) / 2, datetime.UTC)
        offset = (logger - host).total_seconds()
        print(f"logger now  {logger.isoformat(timespec='milliseconds')}")
        print(f"offset now  {offset:+.3f} s")

        if abs(offset) > args.max_drift:
            print(f"offset still above {args.max_drift} s", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
