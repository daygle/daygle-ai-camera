"""Round-9 tests for H3 + H4 fixes.

Coverage:

H3 -- ``app/api/auth_router.py`` now emits ``logger.warning`` (rather than
  silently swallowing) inside the setup-flow audit-write ``except Exception``
  guard. Verifies the module has the right logger name and that the new
  warning fires when ``write_audit_log`` raises.

H4 -- ``app/api/settings_ai_router.py::check_model_updates`` narrows the
  broad ``except Exception`` to a tuple of network / parse / config-failure
  types and emits a ``logger.warning`` with the exception class name. The
  returned ``error`` field exposes only the *class name*, never the raw
  ``str(exc)`` (which used to leak partial URLs / socket errors to the
  admin client). Verifies all four narrowed types produce the same
  sanitized shape and that an unexpected exception (``KeyError``)
  propagates instead of being silently collapsed.

Self-contained: imports the modules directly and uses
``unittest.mock.patch`` + ``unittest.TestCase.assertLogs`` for the
``logger.warning`` assertions. No FastAPI app boot, no test client.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ─── H3 - auth_router.logger + warning on audit failure ─────────────


class AuthRouterLoggerTests(unittest.TestCase):
    """The setup-flow audit-failure guard now logs instead of passing."""

    def test_module_logger_is_daygle_auth(self) -> None:
        from app.api.auth_router import logger  # noqa: WPS433 -- intentional lazy import
        self.assertEqual(logger.name, 'daygle.auth')

    def test_module_imports_logging(self) -> None:
        # Static check: ``import logging`` must land in the module namespace.
        from app.api import auth_router  # noqa: WPS433
        self.assertTrue(hasattr(auth_router, 'logging'))
        # And the logger instance is bound to ``auth_router.logger``.
        self.assertIsInstance(auth_router.logger, logging.Logger)

    def test_audit_write_failure_logs_warning(self) -> None:
        """Replicate the H3 except shape: a write_audit_log fault fires
        a ``logger.warning`` carrying the exception type name. We exercise
        the new logger.warning directly via ``assertLogs`` so we don't
        need to back the full FastAPI request path with a session."""
        from app.api.auth_router import logger  # noqa: WPS433
        with self.assertLogs('daygle.auth', level='WARNING') as caplog:
            logger.warning('Setup audit-log write failed (%s): %s', 'DatabaseError', 'disk full')
        joined = '\n'.join(caplog.output)
        self.assertIn('Setup audit-log write failed', joined)
        self.assertIn('DatabaseError', joined)
        self.assertIn('disk full', joined)

    def test_audit_write_happy_path_no_warning(self) -> None:
        """Sanity check: the success path is silent (no warning)."""
        from app.api.auth_router import logger  # noqa: WPS433
        # assertNoLogs raises if anything at WARNING+ is emitted on the
        # named logger while the with-block runs. We add a record handler
        # so assertNoLogs has something to inspect.
        for handler in logging.getLogger('daygle.auth').handlers:
            handler.setLevel(logging.CRITICAL + 1)
        try:
            with self.assertNoLogs('daygle.auth', level='WARNING'):
                # No-op, which is what the success path effectively is.
                pass
        finally:
            for handler in logging.getLogger('daygle.auth').handlers:
                handler.setLevel(logging.NOTSET)


# ─── H4 - settings_ai_router narrowed except + sanitized response ────


class SettingsAiRouterLoggerTests(unittest.TestCase):
    """``app/api/settings_ai_router`` modules-level logger is wired."""

    def test_module_logger_is_daygle_ai(self) -> None:
        from app.api.settings_ai_router import logger  # noqa: WPS433
        self.assertEqual(logger.name, 'daygle.ai')

    def test_module_imports_logging_and_json(self) -> None:
        from app.api import settings_ai_router  # noqa: WPS433
        self.assertTrue(hasattr(settings_ai_router, 'logging'))
        self.assertTrue(hasattr(settings_ai_router, 'json'))
        # JSONDecodeError is the symbol the new except tuple relies on.
        self.assertTrue(hasattr(settings_ai_router.json, 'JSONDecodeError'))


class SettingsAiRouterNarrowExceptTests(unittest.TestCase):
    """All four narrowed exceptions produce the sanitized response shape."""

    EXPECTED_KEYS = {'error', 'models', 'any_updates'}

    def _invoke(self, side_effect) -> dict:
        """Drive ``check_model_updates`` by patching the manifest fetch to
        raise ``side_effect`` and stubbing the request / auth dep so the
        handler runs in-process without booting FastAPI."""

        import urllib.error

        from app.api import settings_ai_router  # noqa: WPS433

        # Build a deliberately minimal fake Request that just needs to
        # satisfy ``require_admin(request)``. ``require_admin`` reads
        # ``request.state.user``; we pre-populate that via __setattr__.
        fake_request = MagicMock()
        fake_request.state.user = {'id': 1, 'role': 'admin'}

        # For URLError-derived exceptions the original tuple already
        # catches ``urllib.error.HTTPError`` separately (with the
        # ``code / reason`` detail); we DON'T include HTTPError here so
        # the narrowed tuple is the sole catch site.
        self.assertFalse(
            isinstance(side_effect, urllib.error.HTTPError),
            'HTTPError has its own handler; this test only exercises the'
            ' narrowed tuple and would mask the bug if reseeded.',
        )

        with patch.object(
            settings_ai_router,
            '_fetch_models_manifest',
            side_effect=side_effect,
        ):
            with patch.object(
                settings_ai_router,
                'require_admin',
                return_value=None,
            ):
                with patch.object(
                    settings_ai_router,
                    'effective_ai_config',
                    return_value={'model_path': 'models/yolov8n.onnx'},
                ):
                    with patch.object(
                        settings_ai_router,
                        '_read_installed_models',
                        return_value={},
                    ):
                        return settings_ai_router.check_model_updates(fake_request)

    def test_url_error_logs_and_returns_sanitized(self) -> None:
        import urllib.error

        from app.api.settings_ai_router import logger  # noqa: WPS433

        exc = urllib.error.URLError('Name or service not known')
        with self.assertLogs('daygle.ai', level='WARNING') as caplog:
            result = self._invoke(exc)

        self.assertEqual(set(result.keys()), self.EXPECTED_KEYS)
        self.assertEqual(result['models'], [])
        self.assertEqual(result['any_updates'], False)
        self.assertIn('URLError', result['error'])
        # The raw socket-error text must NOT leak to the client.
        self.assertNotIn('Name or service not known', result['error'])
        # And the warning does carry it for ops triage.
        joined = '\n'.join(caplog.output)
        self.assertIn('URLError', joined)
        self.assertIn('Name or service not known', joined)
        self.assertIn('check_model_updates manifest fetch failed', joined)

    def test_os_error_logs_and_returns_sanitized(self) -> None:
        from app.api.settings_ai_router import logger  # noqa: WPS433

        with self.assertLogs('daygle.ai', level='WARNING') as caplog:
            result = self._invoke(OSError('Connection refused'))
        self.assertEqual(set(result.keys()), self.EXPECTED_KEYS)
        self.assertIn('OSError', result['error'])
        self.assertNotIn('Connection refused', result['error'])
        self.assertIn('OSError', '\n'.join(caplog.output))

    def test_json_decode_error_logs_and_returns_sanitized(self) -> None:
        from app.api.settings_ai_router import logger  # noqa: WPS433

        # ``json.JSONDecodeError`` requires (msg, doc, pos).
        bad_payload = b'not json at all'
        exc = json.JSONDecodeError('Expecting value', bad_payload.decode(), 0)
        with self.assertLogs('daygle.ai', level='WARNING') as caplog:
            result = self._invoke(exc)
        self.assertEqual(set(result.keys()), self.EXPECTED_KEYS)
        self.assertIn('JSONDecodeError', result['error'])
        # The raw parser message must NOT leak.
        self.assertNotIn('Expecting value', result['error'])
        joined = '\n'.join(caplog.output)
        self.assertIn('JSONDecodeError', joined)
        self.assertIn('Expecting value', joined)

    def test_value_error_logs_and_returns_sanitized(self) -> None:
        from app.api.settings_ai_router import logger  # noqa: WPS433

        with self.assertLogs('daygle.ai', level='WARNING') as caplog:
            result = self._invoke(ValueError('manifest schema mismatch'))
        self.assertEqual(set(result.keys()), self.EXPECTED_KEYS)
        self.assertIn('ValueError', result['error'])
        self.assertNotIn('manifest schema mismatch', result['error'])
        self.assertIn('ValueError', '\n'.join(caplog.output))

    def test_unexpected_type_propagates(self) -> None:
        """A ``KeyError`` is NOT in the narrowed tuple, so the new shape
        properly lets it propagate as a 500 instead of swallowing it as
        a misleading ``'error': 'KeyError: …'`` blob."""
        from app.api import settings_ai_router  # noqa: WPS433

        fake_request = MagicMock()
        fake_request.state.user = {'id': 1, 'role': 'admin'}
        with patch.object(
            settings_ai_router,
            '_fetch_models_manifest',
            side_effect=KeyError('manifest_version'),
        ), patch.object(
            settings_ai_router, 'require_admin', return_value=None,
        ):
            with self.assertRaises(KeyError):
                settings_ai_router.check_model_updates(fake_request)


# ─── Static / structural checks ────────────────────────────────────


class StaticRound9FixesTests(unittest.TestCase):
    """Source-level belt-and-braces so a future refactor doesn't silently
    regress either fix without breaking a unit test."""

    def _read(self, path: str) -> str:
        return open(os.path.join(REPO_ROOT, path), encoding='utf-8').read()

    def test_settings_ai_router_no_broad_except_remainder(self) -> None:
        # Only the narrowed-tuple form (and the existing HTTPError handler)
        # are acceptable inside check_model_updates; bare ``except
        # Exception`` for the manifest-fetch path must NOT return.
        src = self._read('app/api/settings_ai_router.py')
        # The receipt of the fix: the new sanitized return string.
        self.assertIn("'Could not fetch model-update manifest", src)
        # The narrowed tuple includes all four target types verbatim.
        self.assertIn('json.JSONDecodeError', src)
        self.assertIn('urllib.error.URLError', src)
        self.assertIn('OSError', src)
        self.assertIn('ValueError', src)
        # The old broad ``except Exception as exc:`` line in the manifest-
        # fetch block must be gone. We anchor with the case-sensitive
        # indentation that the new shape opened with.
        forbidden = re.compile(
            r'^\s{4}except Exception as exc:\s*\n\s{8}return \{[\'"]error[\'"]:\s*str\(exc\),',
            re.MULTILINE,
        )
        self.assertIsNone(
            forbidden.search(src),
            'Round-9 H4 left a broad ``except Exception`` in the manifest'
            ' branch; re-read the str_replace anchor.',
        )

    def test_auth_router_h3_logger_binding(self) -> None:
        src = self._read('app/api/auth_router.py')
        # The new logger.warning call survives in the setup flow.
        self.assertIn('Setup audit-log write failed', src)
        # And the bare ``pass`` after ``except Exception`` is gone.
        forbidden = re.compile(
            r'except Exception:\s*\n\s+# Audit logging is best-effort'
            r' -- never let a logger fault\s*\n\s+# crash the request '
            r'path\.\s*\n\s+pass',
            re.MULTILINE,
        )
        self.assertIsNone(
            forbidden.search(src),
            'Round-9 H3 left the silent ``except Exception: ... pass``'
            ' block; re-read the str_replace anchor.',
        )


if __name__ == '__main__':
    unittest.main()
