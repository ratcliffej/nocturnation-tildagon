#!/usr/bin/env python3
"""Phase 1 hop-0 bench analyser (fleet-sync-design.md §4.1).

Reads two Tildagon serial captures and computes the metric that actually
matters for visible sync: **cross-device arrival skew**. For each frame
both devices received, we compare when each device saw it in wall-clock
time (accounting for the constant device-clock offset between them).
A large skew means one device stalled before receiving that frame while
the other didn't — the pulse envelope on the two badges then starts at
different wall-clock instants and the desync is visible.

Two per-device metrics survive from the earlier tool:

  paint delay (RX -> PT)
      How long after RX each device first painted the frame. Should
      stay tight (<10 ms) on both. Large values here would mean the
      envelope math or render tick is losing frames, but bench data
      (2026-07-22) showed this to be a non-issue at 2-6 ms P95 per
      device.

  cross-device paint delta
      |paint_delay_A - paint_delay_B|. Preserved for continuity but is
      NOT the sync ceiling — it hides the arrival-skew problem below.

The primary metric added here:

  arrival skew
      For each shared frame, compute (A.rx_ticks - B.rx_ticks) minus the
      median of that difference over all pairs. The median is the
      constant device-clock offset (each badge boots at a different real
      time); the residual is the per-frame arrival skew — how much later
      ONE device saw this specific frame vs the other, in wall-clock ms.

  paired BENCH-GAP context
      For every outlier skew, we look up the poll-loop gap on each
      device within a small window before the RX. A big gap on one
      device correlates one-to-one with that device seeing the frame
      late — this is what proves the stall is per-device (asyncio /
      GC / LCD refresh) rather than a radio or Director event.

Sequence numbers wrap at 255 -> 1, so raw (src, seq) collides across
wraps every ~2 minutes at 2 Hz. The pairing step below handles this by
matching each RX in A to the seq-and-source-matching RX in B whose ticks
land closest under a rough median offset (bootstrapped from
single-occurrence seqs). Frames whose closest match is more than
`--pair-window-ms` off (default 5000 ms) are treated as unpaired
across-wrap orphans and dropped from the skew analysis.

Usage
-----

    python3 tools/bench_hop0_paint_delta.py tildagon_A.log tildagon_B.log
    python3 tools/bench_hop0_paint_delta.py --extended A.log B.log

The default summary reports paint delay + arrival skew percentiles and
a Phase 1 pass/fail gate on P95 arrival skew. `--extended` adds:
  - per-frame outlier list (largest skew events) with paired GAPs
  - drop-reason counts + asymmetric drops (A dropped, B rendered)
  - poll-gap summary per device
"""

import argparse
import bisect
import re
import sys
from collections import defaultdict, namedtuple


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

# Event stored in file-order lists; index also serves as a stable id
# so PT can pair to the most-recent matching RX within one log.
FrameEvent = namedtuple("FrameEvent",
                        "src seq hop rx_ticks pt_ticks delay_ms")
Drop = namedtuple("Drop", "src seq ticks reason")
Gap = namedtuple("Gap", "ticks gap_ms")


def parse_log(path):
    """Read one Tildagon serial capture.

    Returns (frames, drops, gaps). ``frames`` is a list of FrameEvent
    in file order. Each PT line is matched to the most-recent RX for
    the same (src, seq) that hasn't already been matched — so if the
    seq wraps and appears again, both instances are captured as
    separate FrameEvents in the list.
    """
    rx_events = []      # list of (src, seq, hop, ticks); mutable pt slot filled later
    rx_by_src_seq = defaultdict(list)   # (src, seq) -> list of indices into rx_events
    pt_pending = []     # list of (src, seq, ticks, delay)
    drops = []
    gaps = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = RX_RE.search(line)
            if m:
                src = int(m["src"])
                seq = int(m["seq"])
                idx = len(rx_events)
                rx_events.append([src, seq, int(m["hop"]),
                                  int(m["ticks"]), None, None])
                rx_by_src_seq[(src, seq)].append(idx)
                continue
            m = PT_RE.search(line)
            if m:
                pt_pending.append((int(m["src"]), int(m["seq"]),
                                   int(m["ticks"]), int(m["delay"])))
                continue
            m = DROP_RE.search(line)
            if m:
                drops.append(Drop(int(m["src"]), int(m["seq"]),
                                  int(m["ticks"]), m["reason"]))
                continue
            m = GAP_RE.search(line)
            if m:
                gaps.append(Gap(int(m["ticks"]), int(m["gap"])))
    # Pair each PT with the FIRST unmatched RX in the same (src, seq)
    # bucket. In practice the dispatched-key state in app.py means only
    # one RX is pending PT at a time per source, so this is unambiguous.
    matched_head = defaultdict(int)   # (src, seq) -> next unmatched idx
    for (src, seq, ticks, delay) in pt_pending:
        bucket = rx_by_src_seq.get((src, seq))
        if not bucket:
            continue
        head = matched_head[(src, seq)]
        if head >= len(bucket):
            continue
        rx_events[bucket[head]][4] = ticks
        rx_events[bucket[head]][5] = delay
        matched_head[(src, seq)] = head + 1
    frames = [FrameEvent(src, seq, hop, ticks, pt, delay)
              for (src, seq, hop, ticks, pt, delay) in rx_events
              if pt is not None]
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


def median(values):
    if not values:
        return 0
    vs = sorted(values)
    n = len(vs)
    if n % 2 == 1:
        return vs[n // 2]
    return (vs[n // 2 - 1] + vs[n // 2]) // 2


def pair_frames(frames_a, frames_b, window_ms):
    """Pair frames across the two logs by (src, seq) proximity in ticks.

    Bootstraps a rough device-clock offset from any (src, seq) that
    appears exactly once in each log (these are unambiguous). Then walks
    every seq occurrence: for each A event, picks the B event with the
    matching (src, seq) whose (a.ticks - b.ticks - offset) has smallest
    magnitude — the closest physical frame under the offset. Rejects a
    pairing whose residual exceeds ``window_ms``; those get counted as
    unpaired orphans (seq-wrap mismatches, mostly).
    """
    by_a = defaultdict(list)   # (src, seq) -> list[FrameEvent]
    for f in frames_a:
        by_a[(f.src, f.seq)].append(f)
    by_b = defaultdict(list)
    for f in frames_b:
        by_b[(f.src, f.seq)].append(f)

    # Bootstrap offset from seqs that appear exactly once in each log.
    bootstrap = []
    for key in set(by_a) & set(by_b):
        if len(by_a[key]) == 1 and len(by_b[key]) == 1:
            bootstrap.append(by_a[key][0].rx_ticks - by_b[key][0].rx_ticks)
    offset = median(bootstrap) if bootstrap else 0

    pairs = []
    orphans_a = 0
    orphans_b = 0
    consumed_b = defaultdict(set)   # (src, seq) -> set of idx already paired
    for key in set(by_a) | set(by_b):
        a_list = by_a.get(key, [])
        b_list = by_b.get(key, [])
        for a in a_list:
            best = None
            best_dist = None
            for j, b in enumerate(b_list):
                if j in consumed_b[key]:
                    continue
                dist = abs((a.rx_ticks - b.rx_ticks) - offset)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = j
            if best is not None and best_dist <= window_ms:
                pairs.append((a, b_list[best]))
                consumed_b[key].add(best)
            else:
                orphans_a += 1
        orphans_b += sum(1 for j in range(len(b_list))
                         if j not in consumed_b[key])
    return offset, pairs, orphans_a, orphans_b


def gap_before(sorted_gap_ticks, gap_by_ticks, target_ticks, window_ms=200):
    """Return the largest BENCH-GAP that fired in [target - window, target].
    None if no gap in that window."""
    lo = bisect.bisect_left(sorted_gap_ticks, target_ticks - window_ms)
    hi = bisect.bisect_right(sorted_gap_ticks, target_ticks)
    best = 0
    best_t = None
    for i in range(lo, hi):
        t = sorted_gap_ticks[i]
        g = gap_by_ticks[t]
        if g > best:
            best = g
            best_t = t
    if best_t is None:
        return None
    return (best_t, best)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("log_a", help="Serial capture from Tildagon A")
    ap.add_argument("log_b", help="Serial capture from Tildagon B")
    ap.add_argument("--gate-ms", type=int, default=20,
                    help="P95 cross-device arrival skew Phase 1 gate. "
                         "Default 20 ms; below this the visible desync "
                         "should be below perceptual threshold at DnB "
                         "tempos.")
    ap.add_argument("--hop", type=int, default=0,
                    help="Restrict analysis to this hop_count (default 0)")
    ap.add_argument("--pair-window-ms", type=int, default=5000,
                    help="Reject cross-log pairings whose residual "
                         "after median offset exceeds this. Prevents "
                         "seq-wrap mispairs from polluting the analysis "
                         "(default 5000 ms).")
    ap.add_argument("--outlier-ms", type=int, default=20,
                    help="Under --extended, list per-frame skews at or "
                         "above this magnitude (default 20)")
    ap.add_argument("--skip-boot-ms", type=int, default=5000,
                    help="Ignore each device's first N ms of RX events. "
                         "Filters the boot burst where a late-booting "
                         "device drains queued frames rapidly (default "
                         "5000 ms). Set to 0 to include them.")
    ap.add_argument("--extended", action="store_true",
                    help="Add per-frame outlier list correlated to "
                         "BENCH-GAP events, drop-reason counts, and "
                         "poll-gap summary.")
    args = ap.parse_args()

    frames_a, drops_a, gaps_a = parse_log(args.log_a)
    frames_b, drops_b, gaps_b = parse_log(args.log_b)

    if args.hop is not None:
        frames_a = [f for f in frames_a if f.hop == args.hop]
        frames_b = [f for f in frames_b if f.hop == args.hop]

    if args.skip_boot_ms > 0:
        if frames_a:
            boot_a = frames_a[0].rx_ticks + args.skip_boot_ms
            frames_a = [f for f in frames_a if f.rx_ticks >= boot_a]
        if frames_b:
            boot_b = frames_b[0].rx_ticks + args.skip_boot_ms
            frames_b = [f for f in frames_b if f.rx_ticks >= boot_b]

    offset, pairs, orphans_a, orphans_b = pair_frames(
        frames_a, frames_b, args.pair_window_ms)

    per_frame = []
    for (a, b) in pairs:
        signed = (a.rx_ticks - b.rx_ticks) - offset
        per_frame.append((signed, a, b))
    abs_skews = [abs(sk) for (sk, _, _) in per_frame]

    print(f"= Device A: {args.log_a}")
    summarise("  paint delay (RX -> PT)",
              [f.delay_ms for f in frames_a])
    print()
    print(f"= Device B: {args.log_b}")
    summarise("  paint delay (RX -> PT)",
              [f.delay_ms for f in frames_b])
    print()

    print(f"= Cross-device arrival skew (paired frames: {len(pairs)}; "
          f"orphans A={orphans_a} B={orphans_b})")
    print(f"  A vs B device-clock offset (median A - B): {offset} ms")
    summarise("  |arrival_skew_A_vs_B|", abs_skews)
    print()

    if abs_skews:
        p95 = percentile(sorted(abs_skews), 95)
        gate = args.gate_ms
        status = "PASS" if p95 <= gate else "FAIL"
        print(f"= Phase 1 gate (arrival skew P95 <= {gate} ms): "
              f"{status} (P95 = {p95} ms)")
        print()

    if args.extended:
        print("= Extended diagnostics")
        print()

        gap_ticks_a = sorted(g.ticks for g in gaps_a)
        gap_by_ticks_a = {g.ticks: g.gap_ms for g in gaps_a}
        gap_ticks_b = sorted(g.ticks for g in gaps_b)
        gap_by_ticks_b = {g.ticks: g.gap_ms for g in gaps_b}

        outliers = [(sk, a, b) for (sk, a, b) in per_frame
                    if abs(sk) >= args.outlier_ms]
        outliers.sort(key=lambda t: -abs(t[0]))
        print(f"  Per-frame arrival skew >= {args.outlier_ms} ms "
              f"({len(outliers)} of {len(per_frame)}):")
        if not outliers:
            print("    none")
        else:
            print(f"    {'seq':>5}  {'skew_ms':>7}  {'who_late':>8}  "
                  f"{'A_rx':>7}  {'B_rx':>7}  "
                  f"{'A_gap_before':>14}  {'B_gap_before':>14}")
            for (sk, a, b) in outliers[:40]:
                who = "A" if sk > 0 else "B"
                ga = gap_before(gap_ticks_a, gap_by_ticks_a, a.rx_ticks)
                gb = gap_before(gap_ticks_b, gap_by_ticks_b, b.rx_ticks)
                ga_str = f"{ga[1]}ms@{ga[0]}" if ga else "-"
                gb_str = f"{gb[1]}ms@{gb[0]}" if gb else "-"
                print(f"    {a.seq:>5}  {sk:>+7}  {who:>8}  "
                      f"{a.rx_ticks:>7}  {b.rx_ticks:>7}  "
                      f"{ga_str:>14}  {gb_str:>14}")
            if len(outliers) > 40:
                print(f"    ...and {len(outliers) - 40} more")
        print()

        paint_deltas = [abs(a.delay_ms - b.delay_ms)
                        for (_, a, b) in per_frame]
        summarise("  cross-device paint delta (RX->PT)", paint_deltas)
        print()

        for label, drops in [("A", drops_a), ("B", drops_b)]:
            print(f"  Device {label} drops: {len(drops)} total")
            by_reason = {}
            for d in drops:
                by_reason.setdefault(d.reason, 0)
                by_reason[d.reason] += 1
            for reason, count in sorted(by_reason.items()):
                print(f"    {reason:12s} = {count}")

        # Asymmetric drops
        drop_keys_a = {(d.src, d.seq) for d in drops_a}
        drop_keys_b = {(d.src, d.seq) for d in drops_b}
        painted_keys_a = {(f.src, f.seq) for f in frames_a}
        painted_keys_b = {(f.src, f.seq) for f in frames_b}
        a_drop_b_paint = drop_keys_a & painted_keys_b
        b_drop_a_paint = drop_keys_b & painted_keys_a
        print(f"  A dropped but B painted: {len(a_drop_b_paint)}")
        print(f"  B dropped but A painted: {len(b_drop_a_paint)}")

        print()
        for label, gs in [("A", gaps_a), ("B", gaps_b)]:
            if gs:
                sorted_gaps = sorted(g.gap_ms for g in gs)
                over_100 = sum(1 for g in sorted_gaps if g >= 100)
                print(f"  Device {label} poll gaps >= 25 ms: {len(sorted_gaps)}"
                      f"  (>=100 ms: {over_100})")
                print(f"    max = {sorted_gaps[-1]:>4} ms, "
                      f"P95 = {percentile(sorted_gaps, 95):>4} ms, "
                      f"P50 = {percentile(sorted_gaps, 50):>4} ms")
            else:
                print(f"  Device {label} poll gaps >= 25 ms: 0")

    if abs_skews:
        return 0 if percentile(sorted(abs_skews), 95) <= args.gate_ms else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
