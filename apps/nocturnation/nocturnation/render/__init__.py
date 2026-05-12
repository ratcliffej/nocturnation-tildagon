"""Render surfaces: perimeter LEDs (Block 3), round LCD (Block 4).

The renderers translate parsed LIGHT_COMMAND frames into hardware drive
calls. Each is pure logic with a callback-based hardware abstraction so
the full envelope contract is host-testable; the badge-specific drive
happens in app.py via tildagonos.
"""

from .perimeter import (
    PerimeterRenderer,
    LED_MIN_INDEX,
    LED_MAX_INDEX,
    LED_COUNT,
    CALM_MIN_INTERVAL_MS,
    FULL_MIN_INTERVAL_MS,
)

__all__ = [
    "PerimeterRenderer",
    "LED_MIN_INDEX",
    "LED_MAX_INDEX",
    "LED_COUNT",
    "CALM_MIN_INTERVAL_MS",
    "FULL_MIN_INTERVAL_MS",
]
