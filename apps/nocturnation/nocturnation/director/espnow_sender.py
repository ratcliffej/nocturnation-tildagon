"""ESP-NOW broadcast sender adapter (hardware-side).

Wraps a badge `espnow.ESPNow` object into the `send_fn` callable
that RenderDispatcher needs. This is the only Director-side module
that touches the badge radio, so it is NOT imported by the package
`__init__` (host-side pytest must not require the `espnow` module).
B6 imports it directly when wiring the real Director host in app.py.

ESP-NOW requires a registered peer before `send`. For broadcast we
register the all-ones MAC once; every Lume on the channel receives
the frame. The Director never unicasts.

Not host-tested: this is a thin hardware adapter verified at the
bench (Epic 6B B9). The testable logic - frame encoding, class+group
routing, loopback - lives in render_dispatch.py.
"""

# All-ones MAC: ESP-NOW broadcast address. Every peer on the channel
# receives frames sent here.
BROADCAST_MAC = b"\xff\xff\xff\xff\xff\xff"


def make_sender(esp, broadcast_mac=BROADCAST_MAC):
    """Return a `send_fn(payload_bytes)` bound to `esp`.

    Registers the broadcast peer once (idempotent: a second
    add_peer for the same MAC raises OSError, which we swallow so
    callers don't have to track registration state). The returned
    callable sends one frame per call.

    Lightweight TX diagnostics (Epic 6B B9): prints the first
    successful broadcast and every 50th thereafter, and surfaces send
    failures (the first few) before re-raising so the caller's
    dispatch result stays accurate. This is the only window onto
    "is the Director actually transmitting?" from the badge REPL.
    """
    try:
        esp.add_peer(broadcast_mac)
    except OSError:
        # Peer already registered (or the radio rejected a duplicate);
        # either way the peer exists and send() will work.
        pass

    state = {"sent": 0, "errs": 0}

    def send_fn(payload):
        try:
            esp.send(broadcast_mac, payload)
        except Exception as exc:
            state["errs"] += 1
            if state["errs"] <= 5:
                print("[director] ESP-NOW TX failed: %s" % exc)
            raise
        state["sent"] += 1
        if state["sent"] == 1 or state["sent"] % 50 == 0:
            print("[director] ESP-NOW TX #%d (%d bytes)" % (state["sent"], len(payload)))

    return send_fn
