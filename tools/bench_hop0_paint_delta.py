#!/usr/bin/env python3
"""Phase 1 hop-0 paint-delta bench analyser (fleet-sync-design.md §4.1).

Reads two Tildagon serial captures (one per device), pairs BENCH-RX and
BENCH-PT lines by (source_id, sequence_number), and reports:

  1. Per-device paint delay distribution (P50 / P95 / max).
       ``paint_delay = PT.ticks - RX.ticks`` — how long after receipt
       the device first painted the frame's envelope.
  2. Inter-device paint delta distribution.
       For every frame both devices saw, ``|paint_delay_A - paint_delay_B|``
       is the visible desync moment for that frame.

Radio propagation + WiFi task jitter across the two receivers is <1 ms,
so we treat their RX ticks as coincident and attribute the whole delta
to the render-tick phase gap on each device.

Usage
-----

    # Capture ~60 seconds of Test Pulse @ 2 Hz on each Tildagon:
    mpremote connect /dev/tty.usbmodem-A > tildagon_A.log &
    mpremote connect /dev/tty.usbmodem-B > tildagon_B.log &
    # ... run bench ...

    python3 tools/bench_hop0_paint_delta.py tildagon_A.log tildagon_B.log

Success criterion (Phase 1 gate): P95 inter-device paint delta <= 2 ms.
Above that, Phase 1 fix hasn't closed the gap enough.
"""

import argparse
import re
import sys
from collections import namedtuple


RX_RE = re.compile(
    r"\[BENCH-RX\]\s+src=(?P<src>\d+)\s+seq=(?P<seq>\d+)\s+"
    r"hop=(?P<hop>\d+)\s+ticks=(?P<ticks>\d+)"
)
PT_RE = re.compile(
    r"\[BENCH-PT\]\s+src=(?P<src>\d+)\s+seq=(?P<seq>\d+)\s+"
    r"ticks=(?P<ticks>\d+)\s+delay_ms=(?P<delay>\d+)"
)

FrameEvent = namedtuple("FrameEvent", "src seq hop rx_ticks pt_ticks delay_ms")


def parse_log(path):
    """Read one Tildagon serial capture. Returns dict keyed on
    (src, seq) to FrameEvent; frames with only RX or only PT are
    dropped (mid-capture start/stop)."""
    rx = {}
    pt = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = RX_RE.search(line)
            if m:
                key = (int(m["src"]), int(m["seq"]))
                rx[key] = (int(m["ticks"]), int(m["hop"]))
                continue
            m = PT_RE.search(line)
            if m:
                key = (int(m["src"]), int(m["seq"]))
                pt[key] = (int(m["ticks"]), int(m["delay"]))
    frames = {}
    for key, (rx_ticks, hop) in rx.items():
        if key in pt:
            pt_ticks, delay = pt[key]
            frames[key] = FrameEvent(key[0], key[1], hop,
                                     rx_ticks, pt_ticks, delay)
    return frames


def percentile(sorted_values, p):
    if not sorted_values:
        return float("nan")
    idx = int(round((p / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[idx]


def summarise(label, values):
    if not values:
        print(f"{label}: no samples")
        return
    vs = sorted(values)
    n = len(vs)
    print(f"{label} (n={n})")
    print(f"  min    = {vs[0]:>6} ms")
    print(f"  P50    = {percentile(vs, 50):>6} ms")
    print(f"  P95    = {percentile(vs, 95):>6} ms")
    print(f"  max    = {vs[-1]:>6} ms")
    print(f"  mean   = {sum(vs) / n:>6.1f} ms")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("log_a", help="Serial capture from Tildagon A")
    ap.add_argument("log_b", help="Serial capture from Tildagon B")
    ap.add_argument("--gate-ms", type=int, default=2,
                    help="Inter-device P95 gate for Phase 1 success (default 2)")
    ap.add_argument("--hop", type=int, default=0,
                    help="Restrict analysis to this hop_count (default 0)")
    args = ap.parse_args()

    frames_a = parse_log(args.log_a)
    frames_b = parse_log(args.log_b)

    if args.hop is not None:
        frames_a = {k: v for k, v in frames_a.items() if v.hop == args.hop}
        frames_b = {k: v for k, v in frames_b.items() if v.hop == args.hop}

    print(f"= Device A: {args.log_a}")
    summarise("  paint delay (RX -> PT)", [f.delay_ms for f in frames_a.values()])
    print()
    print(f"= Device B: {args.log_b}")
    summarise("  paint delay (RX -> PT)", [f.delay_ms for f in frames_b.values()])
    print()

    shared = set(frames_a) & set(frames_b)
    print(f"= Inter-device (frames seen by both: {len(shared)})")
    deltas = []
    for key in shared:
        a = frames_a[key]
        b = frames_b[key]
        deltas.append(abs(a.delay_ms - b.delay_ms))
    summarise("  |paint_delay_A - paint_delay_B|", deltas)
    print()

    if deltas:
        p95 = percentile(sorted(deltas), 95)
        gate = args.gate_ms
        status = "PASS" if p95 <= gate else "FAIL"
        print(f"= Phase 1 gate (P95 <= {gate} ms): {status} (P95 = {p95} ms)")
        return 0 if status == "PASS" else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
