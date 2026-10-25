#!/usr/bin/env python3
"""Capture timestamped CR6 ComUSB text and send tester keys through a FIFO."""

import argparse
import os
import select
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import TextIO

import serial

from .pakbus import TextFilter, find_port


def now() -> str:
    return time.strftime("%H:%M:%S")


def reader(
    port: serial.Serial,
    log: TextIO,
    stop: threading.Event,
    raw: TextIO | None = None,
) -> None:
    pending = bytearray()
    text_filter = TextFilter()
    while not stop.is_set():
        try:
            data = port.read(port.in_waiting or 1)
        except serial.SerialException as exc:
            log.write(f"[{now()}] !! serial error: {exc}\n")
            log.flush()
            stop.set()
            return
        if not data:
            continue
        if raw is not None:
            raw.write(f"[{now()}] {data.hex(' ')}\n")
            raw.flush()
        pending.extend(text_filter.feed(data))
        while b"\n" in pending:
            line, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            decoded = line.decode("ascii", "replace").rstrip("\r")
            if decoded:
                log.write(f"[{now()}] {decoded}\n")
                log.flush()


def serve(args: argparse.Namespace) -> int:
    fifo = Path(args.fifo)
    if fifo.exists():
        fifo.unlink()
    os.mkfifo(fifo)

    port_name = args.port or find_port()
    if port_name is None:
        fifo.unlink()
        print("no CR6 serial port found; is it attached to WSL?", file=sys.stderr)
        return 1

    stop = threading.Event()
    fifo_fd = -1
    thread: threading.Thread | None = None
    try:
        with ExitStack() as stack:
            port = stack.enter_context(
                serial.Serial(port_name, args.baud, timeout=0.1)
            )
            log = stack.enter_context(open(args.log, "a", buffering=1))
            raw = (
                stack.enter_context(
                    open(str(args.log) + ".hex", "a", buffering=1, encoding="ascii")
                )
                if args.rawlog
                else None
            )
            log.write(f"\n[{now()}] === opened {port_name} @ {args.baud} 8N1 ===\n")

            thread = threading.Thread(
                target=reader, args=(port, log, stop, raw), daemon=True
            )
            thread.start()
            fifo_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)

            while not stop.is_set():
                if not select.select([fifo_fd], [], [], 1.0)[0]:
                    continue
                payload = os.read(fifo_fd, 4096)
                if not payload:
                    continue
                if payload.startswith(b"#"):
                    log.write(f"[{now()}] {payload.decode('utf-8', 'replace').strip()}\n")
                    continue
                if payload == b"\x00quit":
                    break
                log.write(f"[{now()}] >>> sent {payload!r}\n")
                port.write(payload)
                port.flush()
            log.write(f"[{now()}] === closed ===\n")
        return 0
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1)
        if fifo_fd >= 0:
            os.close(fifo_fd)
        if fifo.exists():
            fifo.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: autodetected CR6)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--fifo", default="/tmp/cr6-comusb.key")
    parser.add_argument("--rawlog", action="store_true")
    return serve(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())