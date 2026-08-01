"""Run the dependency-free selfcheck under pytest too.

`selfcheck.py` is what covers the oldest supported interpreters, where uv
cannot install a Python at all and current pytest releases refuse to run (see
its module docstring). Executing it here as well means those same assertions
are not dead code on modern Python: they run in the normal `uv run pytest`
matrix, so a check that breaks is caught by the fast job rather than only by
the compatibility matrix.
"""

import selfcheck


def test_every_selfcheck_passes():
    failures = selfcheck.run()
    assert not failures, "; ".join(
        "%s: %s: %s" % (name, type(exc).__name__, exc) for name, exc in failures
    )


def test_selfcheck_actually_has_checks():
    """Guard against the suite silently becoming a no-op."""
    assert len(selfcheck.CHECKS) >= 8
