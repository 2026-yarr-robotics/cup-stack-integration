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


def _payload(fallen_count: int) -> dict:
    return {
        'fallen_count': fallen_count,
        'current_plan': {'remaining_steps': []},
        'last_action_result': {'result': 'success'},
    }


class ValidateFallenRecoveryTest(unittest.TestCase):
    def test_valid_interrupt_passes(self):
        resp = {
            'reasoning': 'a cup is fallen',
            'decision': 'fallen_recovery',
            'plan': None,
        }
        self.assertEqual(
            validate_fallen_recovery(resp, _payload(1)), [])

    def test_plan_must_be_null(self):
        resp = {
            'decision': 'fallen_recovery',
            'plan': {'steps': []},
        }
        errs = validate_fallen_recovery(resp, _payload(1))
        self.assertTrue(any('plan=null' in e for e in errs))

    def test_zero_fallen_count_rejected(self):
        resp = {'decision': 'fallen_recovery', 'plan': None}
        errs = validate_fallen_recovery(resp, _payload(0))
        self.assertTrue(any('fallen_count=0' in e for e in errs))

    def test_missing_fallen_count_rejected(self):
        resp = {'decision': 'fallen_recovery', 'plan': None}
        payload = _payload(0)
        del payload['fallen_count']
        errs = validate_fallen_recovery(resp, payload)
        self.assertTrue(any('fallen_count=0' in e for e in errs))

    def test_garbage_fallen_count_rejected(self):
        resp = {'decision': 'fallen_recovery', 'plan': None}
        errs = validate_fallen_recovery(resp, _payload('bad'))  # type: ignore[arg-type]
        self.assertTrue(any('fallen_count=0' in e for e in errs))

    def test_legacy_fallen_recovery_object_tolerated(self):
        # The LLM may still emit the retired object form; it is ignored.
        resp = {
            'decision': 'fallen_recovery',
            'fallen_recovery': {'color': 'red', 'count': 1},
            'plan': None,
        }
        self.assertEqual(validate_fallen_recovery(resp, _payload(2)), [])

    def test_inflight_decisions_unchanged(self):
        # The mode-specific validator still rejects the new decision value —
        # llm_node routes fallen_recovery to its own validator first.
        resp = {'decision': 'fallen_recovery', 'plan': None}
        errs = validate_inflight(resp, _payload(0))
        self.assertTrue(any('bad decision' in e for e in errs))
        ok = {'decision': 'done', 'plan': None}
        self.assertEqual(validate_inflight(ok, _payload(0)), [])


if __name__ == '__main__':
    unittest.main()
