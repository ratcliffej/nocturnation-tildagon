"""Render surfaces: perimeter LEDs (Block 3), round LCD (Block 4).

The renderers translate parsed LIGHT_COMMAND frames into hardware drive
calls. Each is pure logic with a callback / query interface so the full
envelope contract is host-testable; the badge-specific drive happens in
app.py via tildagonos.leds (perimeter) and ctx (LCD).

Shared envelope math (Time enum -> milliseconds, attack/sustain/release
brightness function) lives in envelope.py so both renderers agree on
timings without depending on each other.
"""

from .envelope import TIME_MS, envelope_brightness
from .perimeter import (
    PerimeterRenderer,
    LED_MIN_INDEX,
    LED_MAX_INDEX,
    LED_COUNT,
    CALM_MIN_INTERVAL_MS,
    FULL_MIN_INTERVAL_MS,
)
from .lcd import (
    LcdRenderer,
    LCD_MIN_INTERVAL_MS,
    FULL_BRIGHTNESS_CAP,
)

__all__ = [
    "TIME_MS",
    "envelope_brightness",
    "PerimeterRenderer",
    "LED_MIN_INDEX",
    "LED_MAX_INDEX",
    "LED_COUNT",
    "CALM_MIN_INTERVAL_MS",
    "FULL_MIN_INTERVAL_MS",
    "LcdRenderer",
    "LCD_MIN_INTERVAL_MS",
    "FULL_BRIGHTNESS_CAP",
]
