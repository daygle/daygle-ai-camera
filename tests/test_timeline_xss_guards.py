"""Round-9 narrow regression guard for the timeline.js + yamnet-tflite.js patches.

These two files were NOT in the original R9 H2 sweep (the sweep covered
``web/live.js``, ``cameras.js``, ``recordings.js``, ``sounds.js``,
``settings.js``, ``nav.js``, ``onnx.js``, ``profile.js``). A targeted audit
after R9 confirmed each ``innerHTML = …`` site either routes data through
``escapeHtml`` (``timeline.js`` populates selects / detail rows / grid via
helpers whose ``.map`` callbacks call escapeHtml) or interpolates only
typed-fixed values (formatted numbers, CSS class composition, ``Math.round``
percentages, ternaries on discriminated enum values) that cannot carry
attacker-controlled strings. The audit also surfaced 6 helper-output sites
in ``yamnet-tflite.js`` (L120 / L136) that were unwrapped - those were
patched in this round to match the L200 ``error.message`` convention.

A broader per-interpolation check was tried first but it kept flagging
legitimate ternaries (``camera ? 'selected' : ''``), numerical formatting
(``Math.round(p * 100)``), and CSS-class composition - all of which are
safe-by-input-type. The R9 H2 sweep regression guard
(``tests.test_round9_h2_xss_sweep``) already covers the broader
are-everywhere-escapeHtml question for the 8 originally-scoped JS files;
this module is the **narrow regression guard** for the patches this round
shipped, restricted to:

1. **Sameness** - the load-bearing render helpers in each file still
   exist and the ``escapeHtml`` token is still imported, so a future
   commit that drops the import is caught immediately.
2. **Patched sites** - the 6 yamnet-tflite.js helper-output sites
   wrapped in ``escapeHtml`` in R9 still carry that exact wrapping.
   The substring check is intentionally verbatim so a refactor that
   produces an equivalent-looking but semantically different
   interpolation still breaks this guard.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCOPED_FILES: tuple[str, ...] = (
    'web/timeline.js',
    'web/yamnet-tflite.js',
)

# Exact substrings of the patches applied to yamnet-tflite.js in R9.
# Each substring is the ``${escapeHtml(<helper_call>)}`` wrapper with
# the helper invocation, not just ``escapeHtml`` alone - so this guard
# is unaffected by other escapeHtml call sites the file may legitimately
# add. Substrings are deduplicated by literal text: ``yesNo(status.running)``
# appears in both the statusPanel and the rows.map callback as the same
# ``${escapeHtml(yesNo(status.running))}`` substring, so we list it once.
YAMNET_PATCHED_SITES: tuple[tuple[str, str], ...] = (
    # statusPanel (renderOverall) - 3 helper-output sites wrapped in R9.
    (
        '${escapeHtml(yesNo(status.running))}',
        'statusPanel ``Running`` row',
    ),
    (
        '${escapeHtml(enabledCameras.length)}',
        'statusPanel ``Sound Cameras`` row',
    ),
    (
        '${escapeHtml(percentValue(status.last_confidence))}',
        'statusPanel ``Last Confidence`` row',
    ),
    # rows.map callback (renderCameraStatuses) - 3 helper-output sites.
    (
        '${escapeHtml(yesNo(soundConfigured(camera)))}',
        'rows.map ``Configured`` cell',
    ),
    (
        '${escapeHtml(enabledSoundRules(camera).length)}',
        'rows.map ``Rules`` cell',
    ),
    # The rows.map ``Running`` cell uses the same wrapped substring as the
    # statusPanel site. Covered by the first entry above.
    # loadYamnetModelInfo (model card) keeps model_size, sha256, and
    # installed_at values escaped before interpolation.
    (
        'const sizeText = info.model_size ? escapeHtml(formatBytes(info.model_size)) : \'Unknown\';',
        'model-card ``size`` value',
    ),
    (
        'const hashText = info.sha256 ? escapeHtml(info.sha256) : \'Not available\';',
        'model-card ``SHA-256`` value',
    ),
    (
        "const installedAt = info.installed_at ? escapeHtml(new Date(info.installed_at).toLocaleDateString()) : '';",
        'model-card ``installed_at`` value',
    ),
)


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


class _TimelineFileGuard(unittest.TestCase):
    path = REPO_ROOT / 'web' / 'timeline.js'

    def test_render_helpers_referenced(self) -> None:
        source = _read(self.path)
        for helper in ('renderTimeline', 'renderRecordingDetails'):
            self.assertIn(
                helper,
                source,
                msg=(
                    f'{helper} removed from timeline.js - the innerHTML= '
                    'call sites around its usage need to be re-audited by hand'
                ),
            )

    def test_escapeHtml_is_used(self) -> None:
        self.assertIn(
            'escapeHtml',
            _read(self.path),
            msg='escapeHtml dropped from timeline.js - re-audit every innerHTML= call site',
        )


class _YamnetTfliteFileGuard(unittest.TestCase):
    path = REPO_ROOT / 'web' / 'yamnet-tflite.js'

    def test_renderer_helpers_referenced(self) -> None:
        source = _read(self.path)
        for helper in ('renderOverall', 'renderCameraStatuses'):
            self.assertIn(
                helper,
                source,
                msg=f'{helper} removed from yamnet-tflite.js - re-audit its innerHTML= call sites by hand',
            )

    def test_escapeHtml_is_used(self) -> None:
        self.assertIn(
            'escapeHtml',
            _read(self.path),
            msg='escapeHtml dropped from yamnet-tflite.js - re-audit every innerHTML= call site',
        )

    def test_patched_sites_still_wrapped_in_escapeHtml(self) -> None:
        """Each yamnet-tflite.js helper-output interpolation R9 added
        escapeHtml around must still carry that wrapping. Verbatim
        substring check so a refactor that produces a different-looking
        but semantically equivalent interpolation still breaks this
        guard."""
        source = _read(self.path)
        missing: list[tuple[str, str]] = [
            (substr, label)
            for substr, label in YAMNET_PATCHED_SITES
            if substr not in source
        ]
        self.assertEqual(
            missing,
            [],
            msg=(
                'yamnet-tflite.js R9 patches regressed - these helper '
                'interpolations lost their escapeHtml wrapper: '
                + repr([label for _, label in missing])
            ),
        )


class _UtilsRuleExpandRowGuard(unittest.TestCase):
    """``renderRuleExpandRow`` in utils.js interpolates the row ``key`` into
    a ``data-*-email-recipients`` attribute. The sibling time-select fields
    already route ``key`` through ``escapeHtml`` (via ``renderTimeSelect``'s
    ``escapeHtml(dataAttrValue)``); this guard keeps the email-recipients
    attribute consistent so a future caller passing a non-numeric key can't
    break out of the attribute."""

    path = REPO_ROOT / 'web' / 'utils.js'

    def test_email_recipients_key_is_escaped(self) -> None:
        source = _read(self.path)
        self.assertIn(
            'data-${prefix}-email-recipients="${escapeHtml(key)}"',
            source,
            msg=(
                'renderRuleExpandRow no longer escapes the row key in the '
                'email-recipients data-attribute - restore escapeHtml(key) '
                'so it matches the escaped time-select fields'
            ),
        )
        self.assertNotIn(
            'data-${prefix}-email-recipients="${key}"',
            source,
            msg='renderRuleExpandRow regressed to raw ${key} interpolation',
        )


if __name__ == '__main__':
    unittest.main()
