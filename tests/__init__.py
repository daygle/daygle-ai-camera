"""Marker package file making ``tests/`` a Python package.

This module enables cross-file test-helper imports of the form
``from tests.<file> import <helper>``. It is referenced by
``tests/test_web_auth_router_integration.py``, which imports
``LocalClient``, ``_load_app``, ``_login``, ``_server``,
``_setup_admin`` from ``tests.test_api``.

Without this marker, pytest's default importmode treats individual test
files as top-level modules and any cross-file ``from tests.X import Y``
raises ``ModuleNotFoundError: No module named 'tests.X'``. Adding this
empty package init switches importmode to package-aware and unblocks the
cross-import.

Do NOT delete this file in cleanup passes -- the Phase-13 surface-contract
integration tests rely on it.
"""
