"""NocturNation Tildagon implementation package.

Submodules:
  protocol   - ESP-NOW frame parser + protocol constants. Host-side
               unit-testable; runs unchanged on the badge.
  render     - Perimeter LED + round LCD renderers. Requires Tildagon
               hardware APIs; not exercised by host tests.
"""

__version__ = "0.1.0"
