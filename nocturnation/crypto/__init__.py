"""Cryptographic primitives for NocturNation Tildagon.

Scope limited to what Epic 21's authenticated config channel needs:
HMAC-SHA256 truncated to 8 bytes, constant-time verify. See
`hmac_sha256.py` for the normative API.

MicroPython gotcha: this package imports `hashlib`, which is available
on both CPython (host pytest) and MicroPython (badge). No further
compatibility shims are needed - `hashlib.sha256` is the common seam
and both backends return a stable `.digest()` for the HMAC construction.
"""
