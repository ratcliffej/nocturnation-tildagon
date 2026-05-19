"""ShowRegistry + discovery tests."""

import os
import sys

import pytest

from nocturnation.shows import (
    Show,
    ShowRegistry,
    discover_shows,
    show_registry,
)


def _make_show(show_id, display="Test"):
    class _S(Show):
        def __init__(self, sid, name):
            self._sid = sid
            self._name = name
        def id(self):           return self._sid
        def display_name(self): return self._name
        def context(self):      return None
    return _S(show_id, display)


class TestRegistryBasics:
    def test_empty_registry(self):
        r = ShowRegistry()
        assert len(r) == 0
        assert tuple(r) == ()
        assert r.find("anything") is None

    def test_register_one(self):
        r = ShowRegistry()
        s = _make_show("a")
        r.register(s)
        assert len(r) == 1
        assert r.find("a") is s
        assert "a" in r

    def test_register_preserves_order(self):
        # Registration order is load-bearing - the picker UI uses it
        # to pick the default cursor on a fresh boot.
        r = ShowRegistry()
        for sid in ("a", "z", "m"):
            r.register(_make_show(sid))
        assert [s.id() for s in r] == ["a", "z", "m"]

    def test_duplicate_id_raises(self):
        r = ShowRegistry()
        r.register(_make_show("a"))
        with pytest.raises(ValueError):
            r.register(_make_show("a"))

    def test_clear(self):
        r = ShowRegistry()
        r.register(_make_show("a"))
        r.clear()
        assert len(r) == 0
        assert r.find("a") is None


class TestSingletonRegistry:
    def test_show_registry_returns_same_instance(self):
        assert show_registry() is show_registry()


def _write_show_package(root, show_id):
    """Create a temp Show package at `root`/<show_id>/__init__.py."""
    pkg = os.path.join(root, show_id)
    os.makedirs(pkg)
    init_py = """
from nocturnation.shows import Show


class _Generated(Show):
    def id(self):           return "{sid}"
    def display_name(self): return "Generated {sid}"
    def context(self):      return None


def make_show():
    return _Generated()
""".format(sid=show_id)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write(init_py)


class TestDiscovery:
    def setup_method(self):
        # Each test starts with a clean registry singleton.
        show_registry().clear()
        # Snapshot sys.path so we don't leak the tmp shim into other tests.
        self._saved_path = list(sys.path)
        self._saved_modules = set(sys.modules)

    def teardown_method(self):
        show_registry().clear()
        sys.path[:] = self._saved_path
        # Drop any shim modules we imported.
        for k in list(sys.modules):
            if k not in self._saved_modules:
                del sys.modules[k]

    def test_discover_empty_root(self, tmp_path):
        # No `_shims_` parent on sys.path means the registry returns 0
        # without raising.
        n = discover_shows(shows_root="nonexistent_shows_root_xyz")
        assert n == 0
        assert len(show_registry()) == 0

    def test_discover_finds_shows(self, tmp_path):
        # Layout:
        #   tmp_path/discovery_shim/__init__.py
        #   tmp_path/discovery_shim/alpha/__init__.py  -> make_show -> id "alpha"
        #   tmp_path/discovery_shim/beta/__init__.py   -> make_show -> id "beta"
        shim_root = tmp_path / "discovery_shim"
        os.makedirs(shim_root)
        with open(shim_root / "__init__.py", "w") as f:
            f.write("")
        _write_show_package(str(shim_root), "alpha")
        _write_show_package(str(shim_root), "beta")

        sys.path.insert(0, str(tmp_path))
        n = discover_shows(shows_root="discovery_shim")
        assert n == 2
        ids = [s.id() for s in show_registry()]
        # Alphabetical, per registry contract.
        assert ids == ["alpha", "beta"]

    def test_discover_skips_underscored(self, tmp_path):
        shim_root = tmp_path / "discovery_skip"
        os.makedirs(shim_root)
        with open(shim_root / "__init__.py", "w") as f:
            f.write("")
        _write_show_package(str(shim_root), "real")
        # Underscore-prefixed name: skipped (looks like __pycache__).
        os.makedirs(shim_root / "_internal")
        with open(shim_root / "_internal" / "__init__.py", "w") as f:
            f.write("def make_show(): return None")

        sys.path.insert(0, str(tmp_path))
        n = discover_shows(shows_root="discovery_skip")
        assert n == 1
        assert [s.id() for s in show_registry()] == ["real"]

    def test_discover_skips_packages_without_factory(self, tmp_path):
        shim_root = tmp_path / "discovery_no_factory"
        os.makedirs(shim_root)
        with open(shim_root / "__init__.py", "w") as f:
            f.write("")
        _write_show_package(str(shim_root), "good")
        # Bad package: no make_show()
        os.makedirs(shim_root / "bad")
        with open(shim_root / "bad" / "__init__.py", "w") as f:
            f.write("# no make_show here")

        sys.path.insert(0, str(tmp_path))
        n = discover_shows(shows_root="discovery_no_factory")
        assert n == 1
        assert [s.id() for s in show_registry()] == ["good"]

    def test_discover_skips_factory_that_raises(self, tmp_path):
        shim_root = tmp_path / "discovery_raise"
        os.makedirs(shim_root)
        with open(shim_root / "__init__.py", "w") as f:
            f.write("")
        _write_show_package(str(shim_root), "good")
        # Bad package: factory raises - must not crash discovery.
        os.makedirs(shim_root / "bad")
        with open(shim_root / "bad" / "__init__.py", "w") as f:
            f.write("def make_show(): raise RuntimeError('boom')")

        sys.path.insert(0, str(tmp_path))
        n = discover_shows(shows_root="discovery_raise")
        assert n == 1
        assert show_registry().find("bad") is None
        assert show_registry().find("good") is not None

    def test_discover_skips_duplicate_ids(self, tmp_path):
        shim_root = tmp_path / "discovery_dup"
        os.makedirs(shim_root)
        with open(shim_root / "__init__.py", "w") as f:
            f.write("")
        # Two packages both call their Show id "dup".
        for dirname in ("aaa", "bbb"):
            pkg = shim_root / dirname
            os.makedirs(pkg)
            with open(pkg / "__init__.py", "w") as f:
                f.write("""
from nocturnation.shows import Show

class _C(Show):
    def id(self):           return "dup"
    def display_name(self): return "Dup"
    def context(self):      return None

def make_show():
    return _C()
""")
        sys.path.insert(0, str(tmp_path))
        n = discover_shows(shows_root="discovery_dup")
        # First one wins; second silently dropped.
        assert n == 1
        assert len(show_registry()) == 1
