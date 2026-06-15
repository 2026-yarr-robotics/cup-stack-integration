"""Offline unit tests for the LLM output validation (fallen_recovery)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from llm_client import (  # noqa: E402
    validate_cold_start,
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


class ValidateSlotColorsTest(unittest.TestCase):
    """Phase 2: target.slot_colors structural validation (optional field)."""

    def _resp(self, slot_colors):
        ts = ['L1_left', 'L1_mid', 'L1_right']
        target = {'base_levels': 3, 'cup_budget': 3, 'target_slots': ts}
        if slot_colors is not None:
            target['slot_colors'] = slot_colors
        return {
            'status': 'ok', 'target': target,
            'plan': {'steps': [{'step': 1, 'action': 'pyramid',
                                'color': 'red', 'target_slot': 'L1_left'}]},
            'error': None,
        }

    def test_valid_constraint(self):
        sc = {'L1_left': 'red', 'L1_mid': 'red', 'L1_right': 'any'}
        errs = validate_cold_start(self._resp(sc), None)
        self.assertFalse([e for e in errs if 'slot_colors' in e], errs)

    def test_all_any(self):
        sc = {'L1_left': 'any', 'L1_mid': 'any', 'L1_right': 'any'}
        errs = validate_cold_start(self._resp(sc), None)
        self.assertFalse([e for e in errs if 'slot_colors' in e], errs)

    def test_absent_is_ok_backward_compat(self):
        errs = validate_cold_start(self._resp(None), None)
        self.assertFalse([e for e in errs if 'slot_colors' in e], errs)

    def test_bad_keys_rejected(self):
        errs = validate_cold_start(self._resp({'L1_left': 'red'}), None)
        self.assertTrue(any('slot_colors keys' in e for e in errs))

    def test_bad_value_rejected(self):
        sc = {'L1_left': 'mauve', 'L1_mid': 'any', 'L1_right': 'any'}
        errs = validate_cold_start(self._resp(sc), None)
        self.assertTrue(any('not a known color' in e for e in errs))

    def test_step_matches_constraint(self):
        resp = {
            'status': 'ok',
            'target': {'base_levels': 3, 'cup_budget': 2,
                       'target_slots': ['L1_left', 'L1_mid'],
                       'slot_colors': {'L1_left': 'red', 'L1_mid': 'any'}},
            'plan': {'steps': [
                {'step': 1, 'action': 'pyramid', 'color': 'red',
                 'target_slot': 'L1_left'},
                {'step': 2, 'action': 'pyramid', 'color': 'blue',
                 'target_slot': 'L1_mid'}]},  # 'any' slot → any color ok
            'error': None,
        }
        self.assertEqual(
            [e for e in validate_cold_start(resp, None) if 'requires color' in e],
            [])

    def test_step_wrong_color_for_constrained_slot_rejected(self):
        resp = {
            'status': 'ok',
            'target': {'base_levels': 1, 'cup_budget': 1,
                       'target_slots': ['L1_left'],
                       'slot_colors': {'L1_left': 'red'}},
            'plan': {'steps': [{'step': 1, 'action': 'pyramid', 'color': 'blue',
                                'target_slot': 'L1_left'}]},
            'error': None,
        }
        self.assertTrue(
            any('requires color' in e
                for e in validate_cold_start(resp, None)))


class ValidateUnstackTest(unittest.TestCase):
    """Phase 3: decision="unstack" structural + top-down safety checks."""

    def _payload(self, stack):
        return {'current_world_state': {'stack': stack}, 'current_plan': {}}

    def test_valid_unstack(self):
        # L3_top filled, nothing resting on it -> removable.
        resp = {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}
        stack = {'L3_top': {'color': 'blue'}}
        self.assertEqual(validate_inflight(resp, self._payload(stack)), [])

    def test_unstack_requires_slot(self):
        resp = {'decision': 'unstack', 'plan': None}
        errs = validate_inflight(resp, self._payload({}))
        self.assertTrue(any('valid slot' in e for e in errs))

    def test_unstack_plan_must_be_null(self):
        resp = {'decision': 'unstack', 'slot': 'L3_top', 'plan': {'steps': []}}
        errs = validate_inflight(
            resp, self._payload({'L3_top': {'color': 'blue'}}))
        self.assertTrue(any('plan=null' in e for e in errs))

    def test_unstack_empty_slot_rejected(self):
        resp = {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}
        errs = validate_inflight(resp, self._payload({'L3_top': None}))
        self.assertTrue(any('already empty' in e for e in errs))

    def test_unstack_buried_rejected(self):
        # L1_mid is buried under an occupied L2_left -> never tear it down.
        resp = {'decision': 'unstack', 'slot': 'L1_mid', 'plan': None}
        stack = {'L1_mid': {'color': 'blue'}, 'L2_left': {'color': 'red'}}
        errs = validate_inflight(resp, self._payload(stack))
        self.assertTrue(any('buried' in e for e in errs))

    def test_unstack_correct_cup_rejected(self):
        # L3_top already holds its required color -> not a violation, never unstack.
        resp = {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}
        payload = {
            'current_world_state': {'stack': {'L3_top': {'color': 'red'}},
                                    'cups_on_table': {'red': 1}},
            'current_plan': {'target': {
                'target_slots': ['L3_top'], 'slot_colors': {'L3_top': 'red'}}},
            'fallen_count': 0,
        }
        errs = validate_inflight(resp, payload)
        self.assertTrue(any('not a color violation' in e for e in errs), errs)

    def _done_payload(self, slot_color, observed, cups=None):
        target = {'target_slots': ['L3_top'], 'slot_colors': {'L3_top': slot_color}}
        return {
            'current_world_state': {'stack': {'L3_top': {'color': observed}},
                                    'cups_on_table': cups or {}},
            'current_plan': {'target': target, 'remaining_steps': []},
            'last_action_result': {'action': 'pyramid', 'result': 'success'},
            'fallen_count': 0,
        }

    def test_done_rejected_with_fixable_color_violation(self):
        # L3_top blue where red is required AND red is on the table -> not done
        errs = validate_inflight(
            {'decision': 'done', 'plan': None},
            self._done_payload('red', 'blue', cups={'red': 1}))
        self.assertTrue(any('color violation' in e for e in errs), errs)

    def test_done_allowed_when_violation_unfixable(self):
        # same violation but NO red anywhere -> partial done is legitimate
        self.assertEqual(
            validate_inflight({'decision': 'done', 'plan': None},
                              self._done_payload('red', 'blue', cups={})),
            [])

    def test_done_ok_when_color_satisfied(self):
        self.assertEqual(
            validate_inflight({'decision': 'done', 'plan': None},
                              self._done_payload('red', 'red')),
            [])

    def test_unstack_rejected_without_replacement(self):
        # wrong color but the required color is unavailable -> pointless teardown
        resp = {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}
        payload = {
            'current_world_state': {'stack': {'L3_top': {'color': 'blue'}},
                                    'cups_on_table': {'blue': 1}},
            'current_plan': {'target': {
                'target_slots': ['L3_top'], 'slot_colors': {'L3_top': 'red'}}},
            'fallen_count': 0,
        }
        errs = validate_inflight(resp, payload)
        self.assertTrue(any('none is available' in e for e in errs), errs)

    def test_unstack_allowed_with_replacement(self):
        resp = {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}
        payload = {
            'current_world_state': {'stack': {'L3_top': {'color': 'blue'}},
                                    'cups_on_table': {'red': 1}},
            'current_plan': {'target': {
                'target_slots': ['L3_top'], 'slot_colors': {'L3_top': 'red'}}},
            'fallen_count': 0,
        }
        self.assertEqual(validate_inflight(resp, payload), [])


if __name__ == '__main__':
    unittest.main()
