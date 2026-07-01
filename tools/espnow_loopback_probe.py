"""Epic 17 B1 bench probe: does ESP-NOW broadcast echo back to sender?

The dynamic-repeater FSM assumes that when the badge broadcasts a
hop=1 frame, it does NOT receive its own broadcast. That assumption
lets us treat any received hop=N frame matching (src, seq, hop_txed)
in our TX ring as a peer signal. If the radio loops back, the FSM
false-triggers on its own retransmits.

Bench protocol:

  1.  Copy this file to the badge (or paste into REPL under the
      Nocturnation-Tildagon app directory).
  2.  Ensure the badge is NOT running the NocturNation app
      (release_radio must have run - otherwise the radio is busy).
  3.  Run: `import tools.espnow_loopback_probe as p; p.run()`
  4.  Read the console output:

      LOOPBACK: NO   -> assumption holds; FSM safe with (src, seq, hop)
      LOOPBACK: YES  -> add source-MAC filter to peer detection

Also prints airtime measurement (TX -> earliest echo/timeout) so we
can size the 100 ms peer-watch window from real data.
"""

import time
import network

try:
    import espnow
except ImportError:  # pragma: no cover - runs on badge only
    espnow = None

BROADCAST_MAC = b"\xff\xff\xff\xff\xff\xff"
PROBE_PAYLOAD = b"NN\x02\xAB\x99\x00\xF0\x00"  # distinctive; unused msg_type 0xF0
POLL_TIMEOUT_MS = 500  # wait long enough that any plausible echo would arrive
TX_ITERATIONS = 10     # repeat to measure jitter


def _init_radio():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    try:
        wlan.config(protocol=8)  # WIFI_PROTOCOL_LR
        print("[probe] LR protocol set (readback=%s)" % wlan.config("protocol"))
    except Exception as exc:
        print("[probe] wlan.config(protocol=8) failed: %s" % exc)
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception as exc:
        print("[probe] wlan.config(pm=PM_NONE) failed: %s" % exc)
    esp = espnow.ESPNow()
    esp.active(True)
    try:
        esp.add_peer(BROADCAST_MAC)
    except OSError:
        pass
    return wlan, esp


def run():
    if espnow is None:
        print("[probe] espnow module unavailable - run on the badge")
        return
    wlan, esp = _init_radio()
    print("[probe] radio up; running %d TX iterations" % TX_ITERATIONS)

    echoes = 0
    airtimes = []
    for i in range(TX_ITERATIONS):
        tx_ms = time.ticks_ms()
        esp.send(BROADCAST_MAC, PROBE_PAYLOAD)
        deadline = time.ticks_add(tx_ms, POLL_TIMEOUT_MS)
        echo_seen = False
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            host, msg = esp.recv(0)
            if msg is None:
                continue
            if bytes(msg) == PROBE_PAYLOAD:
                rx_ms = time.ticks_ms()
                airtime = time.ticks_diff(rx_ms, tx_ms)
                airtimes.append(airtime)
                echoes += 1
                echo_seen = True
                print("[probe] iter %d: ECHO from %s in %d ms"
                      % (i, host, airtime))
                break
        if not echo_seen:
            print("[probe] iter %d: no echo in %d ms"
                  % (i, POLL_TIMEOUT_MS))
        time.sleep_ms(50)

    print("[probe] --- result ---")
    if echoes == 0:
        print("[probe] LOOPBACK: NO  (0/%d iterations echoed)" % TX_ITERATIONS)
        print("[probe] FSM assumption safe: any received frame matching")
        print("[probe]   (src, seq, hop_txed) is from a peer device.")
    else:
        print("[probe] LOOPBACK: YES  (%d/%d iterations echoed)"
              % (echoes, TX_ITERATIONS))
        print("[probe] FSM MUST filter own MAC from peer detection.")
        if airtimes:
            print("[probe] echo airtime: min=%d ms max=%d ms mean=%d ms"
                  % (min(airtimes), max(airtimes),
                     sum(airtimes) // len(airtimes)))

    try:
        esp.active(False)
    except Exception:
        pass
