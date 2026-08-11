"""Narrow XSS guards for the timeline, YAMNet, and Sounds renderers.

The per-camera sound-detector table now belongs to ``web/sounds.js`` alongside
its configuration controls. The guards below keep the backend/model renderer
on ``yamnet-tflite.js`` and the moved camera-status renderer covered without
requiring the two pages to share implementation details.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


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
        self.assertIn(
            'renderOverall',
            source,
            msg='renderOverall removed from yamnet-tflite.js - re-audit its innerHTML= call sites by hand',
        )

    def test_escapeHtml_is_used(self) -> None:
        self.assertIn(
            'escapeHtml',
            _read(self.path),
            msg='escapeHtml dropped from yamnet-tflite.js - re-audit every innerHTML= call site',
        )

    def test_model_and_status_outputs_remain_escaped(self) -> None:
        source = _read(self.path)
        for substring in (
            '${escapeHtml(yesNo(status.running))}',
            '${escapeHtml(enabledCameras.length)}',
            '${escapeHtml(percentValue(status.last_confidence))}',
            "const sizeText = info.model_size ? escapeHtml(formatBytes(info.model_size)) : 'Unknown';",
            "const hashText = info.sha256 ? escapeHtml(info.sha256) : 'Not available';",
            "const installedAt = info.installed_at ? escapeHtml(new Date(info.installed_at).toLocaleDateString()) : '';",
        ):
            self.assertIn(substring, source)


class _SoundsFileGuard(unittest.TestCase):
    path = REPO_ROOT / 'web' / 'sounds.js'

    def test_detector_renderer_is_referenced(self) -> None:
        source = _read(self.path)
        self.assertIn(
            'renderDetectorStatuses',
            source,
            msg='renderDetectorStatuses removed from sounds.js - re-audit its innerHTML= call sites by hand',
        )

    def test_detector_dynamic_cells_remain_escaped(self) -> None:
        source = _read(self.path)
        for substring in (
            '${escapeHtml(detectorStatusClass(camera, status))}',
            '${escapeHtml(detectorCameraLabel(camera))}',
            '${escapeHtml(detectorEnabledRules(camera).length)}',
            '${escapeHtml(detectorConfidenceMap(status.last_confidences))}',
        ):
            self.assertIn(substring, source)

    def test_escapeHtml_is_used(self) -> None:
        self.assertIn(
            'escapeHtml',
            _read(self.path),
            msg='escapeHtml dropped from sounds.js - re-audit every innerHTML= call site',
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
