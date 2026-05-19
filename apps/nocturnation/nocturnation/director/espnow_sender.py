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
    """
    try:
        esp.add_peer(broadcast_mac)
    except OSError:
        # Peer already registered (or the radio rejected a duplicate);
        # either way the peer exists and send() will work.
        pass

    def send_fn(payload):
        esp.send(broadcast_mac, payload)

    return send_fn
