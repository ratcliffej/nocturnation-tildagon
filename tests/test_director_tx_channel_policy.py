"""Pinned policy: a Tildagon must NOT broadcast as Director on a
forbidden ESP-NOW channel.

Channel 11 is the commercial / show band (Epic 5.5 §3.4 - reserved
for Performance-range source IDs 0x40-0xFE; random-per-boot allocation
keeps multiple commercial Directors from colliding). A Tildagon swarm
broadcasting Director frames on ch 11 at EMF would compete with the
orchestrator's StickC Director and drown out cue traffic - hence the
hard policy that Tildagons stay on the hobby channel (1) only.

These tests pin the policy at the source-of-truth level by inspecting
the constants imported from app.py. They run on the host suite (no
badge hardware needed) so accidental drift is caught at CI rather
than at the bench."""

import os
import sys


# app.py lives at the repo root and isn't a regular package module on
# the host suite; import it via direct file load so the test pins
# whatever constants the deployed badge code actually sees.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


def _load_app_module():
    """Read DIRECTOR_CHANNEL / DIRECTOR_FORBIDDEN_TX_CHANNELS straight
    from app.py without importing the whole module (which would pull
    in MicroPython-only modules like ``events.input`` / ``espnow``).
    We parse the small slice we care about as a Python expression."""
    path = os.path.join(_REPO_ROOT, "app.py")
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    namespace = {"frozenset": frozenset}
    for name in ("DIRECTOR_CHANNEL", "DIRECTOR_FORBIDDEN_TX_CHANNELS"):
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(name + " ="):
                exec(stripped, namespace)
                break
        else:
            raise AssertionError("%s not defined in app.py" % name)
    return namespace


def test_director_channel_constant_is_hobby_channel():
    ns = _load_app_module()
    assert ns["DIRECTOR_CHANNEL"] == 1, (
        "Tildagon Director TX is pinned to channel 1 (hobby band). "
        "Changing this constant is a wire-spec break: see Epic 5.5 §3.4."
    )


def test_channel_11_is_in_the_forbidden_set():
    ns = _load_app_module()
    assert 11 in ns["DIRECTOR_FORBIDDEN_TX_CHANNELS"], (
        "Channel 11 (commercial / show band) must remain in the "
        "Tildagon Director TX blocklist. Removing it lets a Tildagon "
        "swarm broadcast on ch 11 and trample the orchestrator's "
        "StickC Director at EMF."
    )


def test_director_channel_is_not_itself_in_the_forbidden_set():
    """Sanity: the chosen TX channel can't be one of the channels we
    refuse to TX on, otherwise Director mode would never run."""
    ns = _load_app_module()
    assert ns["DIRECTOR_CHANNEL"] not in ns["DIRECTOR_FORBIDDEN_TX_CHANNELS"]
