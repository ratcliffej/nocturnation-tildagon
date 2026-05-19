"""NocturNation Show library.

Each subdirectory is one Show plug-in:

    shows/
      simple_tap/
        __init__.py     # defines make_show() -> Show instance
        README.md       # optional per-Show documentation
      motion_wave/
        __init__.py
        palettes.json   # optional per-Show data

The framework's `discover_shows()` (in
`nocturnation.shows.registry`) walks this directory at boot and
auto-registers every Show. See [docs/developing-shows.md] in the
nocturnation-m5 repo for the authoring guide.
"""
