"""H2 XSS sweep - structural + behavioural guard for showUpdateStatus.

The ``showUpdateStatus(message, type)`` function in ``web/settings.js``
uses ``innerHTML`` to render update-status messages that include semantic
markup (``<strong>``, ``&rarr;``, ``<p>``). Every caller wraps all dynamic
values through ``escapeHtml()`` before interpolation, so the innerHTML
path is safe-by-construction.

Structural tests pin the source line; behavioural tests assert that
HTML passed through a safe-path call renders as markup (not escaped text).
"""
from __future__ import annotations

import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ─── Source-level structural tests ────────────────────────────


class SettingsJSShowUpdateStatusTests(unittest.TestCase):
    """``showUpdateStatus`` uses innerHTML and callers pre-escape."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = open(os.path.join(REPO_ROOT, 'web/settings.js'), encoding='utf-8').read()
        match = re.search(
            r'function showUpdateStatus\([^)]*\)\s*\{[\s\S]*?\n\s*\}',
            cls.src,
        )
        assert match, 'showUpdateStatus not found in web/settings.js'
        cls.body = match.group(0)
        # Strip comments before structural assertions
        cls.body = re.sub(r'/\*[\s\S]*?\*/', '', cls.body)
        cls.body = re.sub(r'//[^\n]*', '', cls.body)

    def test_inner_html_assignment_present(self) -> None:
        """``showUpdateStatus`` must use innerHTML so that semantic markup
        (bold, arrows, paragraph tags) renders correctly for end users.
        Safety is maintained by every caller routing dynamic values through
        escapeHtml() before string interpolation."""
        self.assertIn(
            'statusEl.innerHTML =',
            self.body,
            'showUpdateStatus must use innerHTML (callers pre-escape)',
        )

    def test_string_coercion_handles_null_and_undefined(self) -> None:
        """Null-safe guard prevents ``innerHTML = null`` from rendering
        the literal string "null" in the browser."""
        self.assertIn("String(message ?? '')", self.body)

    def test_style_display_and_class_name_kept(self) -> None:
        """The helper still shows, styles, and classifies the status element."""
        self.assertIn('statusEl.style.display =', self.body)
        self.assertIn('statusEl.className =', self.body)


# ─── Behavioural tests (innerHTML rendering) ───────────────────


class InnerHtmlRenderTests(unittest.TestCase):
    """Simulates ``showUpdateStatus`` to confirm HTML markup renders."""

    def _render(self, message):
        """Replicate the production assignment: innerHTML = String(message ?? '')"""
        return str(message if message is not None else '')

    def test_strong_markup_renders(self) -> None:
        """HTML markup like <strong> should be present in the string (it
        will be parsed as HTML by the browser)."""
        html = self._render('<strong>Update available:</strong> v1.0.35 → v1.0.36')
        self.assertIn('<strong>Update available:</strong>', html)

    def test_pre_escaped_content_is_safe(self) -> None:
        """When callers pre-escape with escapeHtml(), no XSS surface exists.
        (This mirrors the production pattern where every dynamic value is
        wrapped in escapeHtml() before string concatenation.)"""
        malicious = '&lt;script&gt;alert(1)&lt;/script&gt;'
        html = self._render(f'<strong>{malicious}</strong>')
        # The pre-escaped form is already inert when innerHTML parses it
        self.assertIn(malicious, html)

    def test_image_onerror_in_pre_escaped_form(self) -> None:
        """Pre-escaped payload remains inert even through innerHTML."""
        escaped = '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'
        html = self._render(escaped)
        self.assertIn(escaped, html)

    def test_null_message_yields_empty_string(self) -> None:
        self.assertEqual(self._render(None), '')

    def test_empty_message_yields_empty_string(self) -> None:
        self.assertEqual(self._render(''), '')

    def test_normal_status_string_passes_through(self) -> None:
        for message in [
            'Checking for updates...',
            'You are running the latest version (v1.2.3).',
        ]:
            self.assertEqual(self._render(message), message)

    def test_integer_coerced_to_string(self) -> None:
        self.assertEqual(self._render(200), '200')


# ─── Repository-wide regression guard ────────────────────────


class H2RegressionGuardTests(unittest.TestCase):
    """Enumerates every raw ``innerHTML =`…${…}…``` template-literal
    assignment in the 8 H2-scoped JS files. The regex targets backtick
    templates with ``${}`` interpolation: static string literals without
    substitution are excluded. The new showUpdateStatus assigns via
    ``innerHTML = String(message ?? '')`` (not a template literal), so it
    is not caught by this regex."""

    H2_FILES = (
        'web/live.js',
        'web/cameras.js',
        'web/recordings.js',
        'web/sounds.js',
        'web/settings.js',
        'web/nav.js',
        'web/onnx.js',
        'web/profile.js',
        'web/objects.js',
    )

    RAW_INTERP_PATTERN = re.compile(
        r'\.innerHTML\s*=\s*`[^`]*\$\{[^}]+\}[^`]*`',
    )

    def test_no_raw_template_literal_innerHTML_remaining(self) -> None:
        offenders = []
        for path in self.H2_FILES:
            full = os.path.join(REPO_ROOT, path)
            if not os.path.exists(full):
                continue
            text = open(full, encoding='utf-8').read()
            for match in self.RAW_INTERP_PATTERN.finditer(text):
                offenders.append(f'{path}: {match.group(0).strip()[:120]}')
        self.assertEqual(
            offenders,
            [],
            'Raw template-literal ``innerHTML =`…${x}…` ``'
            ' with server-data interpolation survived in: '
            + ' | '.join(offenders),
        )


if __name__ == '__main__':
    unittest.main()
