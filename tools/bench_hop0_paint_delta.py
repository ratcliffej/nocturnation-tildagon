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
DROP_RE = re.compile(
    r"\[BENCH-DROP\]\s+src=(?P<src>\d+)\s+seq=(?P<seq>\d+)\s+"
    r"ticks=(?P<ticks>\d+)\s+reason=(?P<reason>\w+)"
)
GAP_RE = re.compile(
    r"\[BENCH-GAP\]\s+ticks=(?P<ticks>\d+)\s+gap_ms=(?P<gap>\d+)"
)

FrameEvent = namedtuple("FrameEvent", "src seq hop rx_ticks pt_ticks delay_ms")
Drop = namedtuple("Drop", "src seq ticks reason")
Gap = namedtuple("Gap", "ticks gap_ms")


def parse_log(path):
    """Read one Tildagon serial capture. Returns (frames, drops, gaps).
    frames: dict keyed on (src, seq) to FrameEvent; RX-only or PT-only
    entries (from mid-capture start/stop) are dropped.
    drops: list of Drop entries (rate_limit / black / wash_gated).
    gaps: list of Gap entries where the receive loop stalled >=25 ms."""
    rx = {}
    pt = {}
    drops = []
    gaps = []
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
                continue
            m = DROP_RE.search(line)
            if m:
                drops.append(Drop(int(m["src"]), int(m["seq"]),
                                  int(m["ticks"]), m["reason"]))
                continue
            m = GAP_RE.search(line)
            if m:
                gaps.append(Gap(int(m["ticks"]), int(m["gap"])))
    frames = {}
    for key, (rx_ticks, hop) in rx.items():
        if key in pt:
            pt_ticks, delay = pt[key]
            frames[key] = FrameEvent(key[0], key[1], hop,
                                     rx_ticks, pt_ticks, delay)
    return frames, drops, gaps


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
    ap.add_argument("--extended", action="store_true",
                    help="Also report BENCH-DROP counts by reason and "
                         "BENCH-GAP stall summary. Only useful when the "
                         "capture was taken with _BENCH_HOP0 = True and "
                         "perimeter._BENCH_DISPATCH_LOG mirrored on.")
    ap.add_argument("--drop-window-ms", type=int, default=250,
                    help="When --extended, pair a drop on one badge with "
                         "an RX on the other within this window "
                         "(default 250 ms) to catch \"one side rendered, "
                         "the other side rate-limited\" cases.")
    args = ap.parse_args()

    frames_a, drops_a, gaps_a = parse_log(args.log_a)
    frames_b, drops_b, gaps_b = parse_log(args.log_b)

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

    if args.extended:
        print()
        print("= Extended diagnostics")
        for label, drops in [("A", drops_a), ("B", drops_b)]:
            print(f"  Device {label} drops: {len(drops)} total")
            by_reason = {}
            for d in drops:
                by_reason.setdefault(d.reason, 0)
                by_reason[d.reason] += 1
            for reason, count in sorted(by_reason.items()):
                print(f"    {reason:12s} = {count}")

        # Asymmetric drops: one badge dropped, the other painted the
        # same (src, seq). Direct evidence of visible desync from a
        # per-device filter (rate limit / dedup race etc.).
        drop_keys_a = {(d.src, d.seq) for d in drops_a}
        drop_keys_b = {(d.src, d.seq) for d in drops_b}
        a_dropped_b_painted = drop_keys_a & set(frames_b)
        b_dropped_a_painted = drop_keys_b & set(frames_a)
        print(f"  A dropped but B painted: {len(a_dropped_b_painted)}")
        print(f"  B dropped but A painted: {len(b_dropped_a_painted)}")

        print()
        for label, gaps in [("A", gaps_a), ("B", gaps_b)]:
            if gaps:
                gs = sorted(g.gap_ms for g in gaps)
                print(f"  Device {label} poll gaps >= 25 ms: {len(gs)}")
                print(f"    max = {gs[-1]:>4} ms, P95 = {percentile(gs, 95):>4} ms")
            else:
                print(f"  Device {label} poll gaps >= 25 ms: 0")

    if deltas:
        return 0 if status == "PASS" else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
