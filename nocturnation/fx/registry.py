"""FX registry.

Holds a dict mapping fx_id -> Fx subclass. Effects register via the
@fx_registry.register class decorator. The runner uses get() to look
up an incoming LIGHT_FX_RUN's fx_id and instantiate the matching class.

Singleton: one registry per process. fx_registry is the canonical
instance; Fx classes register themselves at module-import time.
"""


class FxRegistry:
    """Maps fx_id (int) -> Fx subclass.

    Effects register via the @register class decorator. Lookup returns
    None for unknown ids (the runner silently drops; spec-compliant
    forward-compat).
    """

    def __init__(self):
        self._by_id = {}

    def register(self, cls):
        """Class decorator. Registers cls under cls.id.

        Raises ValueError on:
          - cls.id == 0 (reserved as the cancel sentinel)
          - cls.id == 255 (reserved per protocol)
          - duplicate registration of the same id
        """
        fx_id = getattr(cls, "id", 0)
        if fx_id == 0 or fx_id == 255 or not (1 <= fx_id <= 254):
            raise ValueError(
                "Fx id must be in 1..254 (0 and 255 reserved); got %r" % (fx_id,)
            )
        if fx_id in self._by_id:
            raise ValueError(
                "Fx id %d already registered as %r"
                % (fx_id, self._by_id[fx_id].__name__)
            )
        self._by_id[fx_id] = cls
        return cls

    def get(self, fx_id):
        """Return the registered Fx class for fx_id, or None."""
        return self._by_id.get(fx_id)

    def has(self, fx_id):
        return fx_id in self._by_id

    def all_ids(self):
        """Sorted list of registered fx_ids. Useful for the manifest
        generator and diagnostics."""
        return sorted(self._by_id.keys())

    def clear(self):
        """Drop all registrations. Test-only; the production registry
        accumulates from module imports for the lifetime of the
        process."""
        self._by_id.clear()


# Canonical singleton. Effects register against this at import time:
#
#     @fx_registry.register
#     class SparkleOnBeat(Fx):
#         id = 11
#         ...
fx_registry = FxRegistry()
