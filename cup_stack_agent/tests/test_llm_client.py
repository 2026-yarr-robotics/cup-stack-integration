"""Offline unit tests for the LLM output validation (fallen_recovery)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from llm_client import (  # noqa: E402
    validate_fallen_recovery,
    validate_inflight,
)


def _payload(fallen: dict | None) -> dict:
    return {
        'fallen': fallen,
        'current_plan': {'remaining_steps': []},
        'last_action_result': {'result': 'success'},
    }


class ValidateFallenRecoveryTest(unittest.TestCase):
    def test_valid_interrupt_passes(self):
        resp = {
            'reasoning': 'red cup fallen',
            'decision': 'fallen_recovery',
            'fallen_recovery': {'color': 'red', 'count': 1},
            'plan': None,
        }
        self.assertEqual(
            validate_fallen_recovery(resp, _payload({'red': 1})), [])

    def test_plan_must_be_null(self):
        resp = {
            'decision': 'fallen_recovery',
            'fallen_recovery': {'color': 'red', 'count': 1},
            'plan': {'steps': []},
        }
        errs = validate_fallen_recovery(resp, _payload({'red': 1}))
        self.assertTrue(any('plan=null' in e for e in errs))

    def test_missing_fallen_recovery_object(self):
        resp = {'decision': 'fallen_recovery', 'plan': None}
        errs = validate_fallen_recovery(resp, _payload({'red': 1}))
        self.assertTrue(any('fallen_recovery object' in e for e in errs))

    def test_missing_color(self):
        resp = {
            'decision': 'fallen_recovery',
            'fallen_recovery': {'count': 1},
            'plan': None,
        }
        errs = validate_fallen_recovery(resp, _payload({'red': 1}))
        self.assertTrue(any('missing color' in e for e in errs))

    def test_color_not_in_fallen_map(self):
        resp = {
            'decision': 'fallen_recovery',
            'fallen_recovery': {'color': 'blue', 'count': 1},
            'plan': None,
        }
        errs = validate_fallen_recovery(resp, _payload({'red': 1}))
        self.assertTrue(any('not present in fallen map' in e for e in errs))

    def test_color_with_zero_count_rejected(self):
        resp = {
            'decision': 'fallen_recovery',
            'fallen_recovery': {'color': 'red', 'count': 1},
            'plan': None,
        }
        errs = validate_fallen_recovery(resp, _payload({'red': 0}))
        self.assertTrue(any('not present in fallen map' in e for e in errs))

    def test_inflight_decisions_unchanged(self):
        # The mode-specific validator still rejects the new decision value —
        # llm_node routes fallen_recovery to its own validator first.
        resp = {'decision': 'fallen_recovery', 'plan': None}
        errs = validate_inflight(resp, _payload({}))
        self.assertTrue(any('bad decision' in e for e in errs))
        ok = {'decision': 'done', 'plan': None}
        self.assertEqual(validate_inflight(ok, _payload({})), [])


if __name__ == '__main__':
    unittest.main()
