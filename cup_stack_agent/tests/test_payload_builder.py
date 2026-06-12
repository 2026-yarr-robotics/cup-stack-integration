"""Offline unit tests for GoalStateBuilder plan tracking."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from payload_builder import (  # noqa: E402
    GoalStateBuilder,
    action_result_reflected,
)


class GoalStateBuilderTest(unittest.TestCase):
    def test_cold_start_output_preserves_top_level_target(self):
        builder = GoalStateBuilder()
        builder.set_plan({
            'status': 'ok',
            'target': {
                'base_levels': 3,
                'cup_budget': 3,
                'target_slots': ['L1_left', 'L1_mid', 'L1_right'],
            },
            'plan': {
                'steps': [
                    {'step': 1, 'action': 'pyramid', 'color': 'red',
                     'target_slot': 'L1_left'},
                    {'step': 2, 'action': 'pyramid', 'color': 'red',
                     'target_slot': 'L1_mid'},
                ],
            },
        })

        payload = builder.build_payload()

        self.assertEqual(payload['mode'], 'in_flight')
        self.assertEqual(payload['current_plan']['target']['cup_budget'], 3)
        self.assertEqual(
            payload['current_goal'],
            {'step': 1, 'action': 'pyramid', 'color': 'red',
             'target_slot': 'L1_left'},
        )

    def test_success_advances_remaining_steps(self):
        builder = GoalStateBuilder()
        builder.set_plan({
            'target': {'base_levels': 3, 'cup_budget': 3},
            'steps': [
                {'step': 1, 'action': 'pyramid', 'color': 'red',
                 'target_slot': 'L1_left'},
                {'step': 2, 'action': 'pyramid', 'color': 'red',
                 'target_slot': 'L1_mid'},
            ],
        })

        builder.on_action_result({
            'step': 1,
            'action': 'pyramid',
            'result': 'success',
            'color': 'red',
            'target_slot': 'L1_left',
        })
        payload = builder.build_payload()

        self.assertEqual(
            payload['current_goal'],
            {'step': 2, 'action': 'pyramid', 'color': 'red',
             'target_slot': 'L1_mid'},
        )
        self.assertEqual(len(payload['current_plan']['remaining_steps']), 1)

    def test_success_consumes_skipped_steps(self):
        # plan_executor drops occupied-slot steps WITHOUT emitting an
        # action_result, so a success may arrive for a mid-list step. The
        # builder must consume the skipped steps too, or remaining_steps/
        # current_goal stay frozen for the rest of the run.
        builder = GoalStateBuilder()
        builder.set_plan({
            'target': {'base_levels': 3, 'cup_budget': 5},
            'steps': [
                {'step': 1, 'action': 'pyramid', 'color': 'blue',
                 'target_slot': 'L1_mid'},     # occupied -> silently skipped
                {'step': 2, 'action': 'pyramid', 'color': 'blue',
                 'target_slot': 'L1_right'},
                {'step': 3, 'action': 'pyramid', 'color': 'blue',
                 'target_slot': 'L2_left'},
            ],
        })

        builder.on_action_result({
            'step': 2,
            'action': 'pyramid',
            'result': 'success',
            'color': 'blue',
            'target_slot': 'L1_right',
        })
        payload = builder.build_payload()

        self.assertEqual(
            payload['current_goal'],
            {'step': 3, 'action': 'pyramid', 'color': 'blue',
             'target_slot': 'L2_left'},
        )
        self.assertEqual(len(payload['current_plan']['remaining_steps']), 1)

    def test_unmatched_or_failed_result_does_not_advance(self):
        builder = GoalStateBuilder()
        builder.set_plan({
            'target': {'base_levels': 3, 'cup_budget': 2},
            'steps': [
                {'step': 1, 'action': 'pyramid', 'color': 'red',
                 'target_slot': 'L1_left'},
                {'step': 2, 'action': 'pyramid', 'color': 'red',
                 'target_slot': 'L1_mid'},
            ],
        })
        builder.on_action_result({
            'step': 9, 'action': 'pyramid', 'result': 'success',
            'color': 'red', 'target_slot': 'L1_left',
        })
        builder.on_action_result({
            'step': 1, 'action': 'pyramid', 'result': 'fail',
            'color': 'red', 'target_slot': 'L1_left',
        })
        payload = builder.build_payload()
        self.assertEqual(len(payload['current_plan']['remaining_steps']), 2)

    def test_action_result_waits_for_expected_world_delta(self):
        result = {
            'step': 1,
            'action': 'pyramid',
            'result': 'success',
            'color': 'red',
            'target_slot': 'L1_left',
        }
        before = {
            'cups_on_table': {'red': 3},
            'stack': {'L1_left': None},
        }
        stale = {
            'cups_on_table': {'red': 3},
            'stack': {'L1_left': None},
        }
        reflected = {
            'cups_on_table': {'red': 2},
            'stack': {'L1_left': {'color': 'red'}},
        }

        self.assertFalse(action_result_reflected(result, before, stale))
        self.assertTrue(action_result_reflected(result, before, reflected))

    def test_action_result_allows_same_count_when_disturbed(self):
        result = {
            'step': 5,
            'action': 'pyramid',
            'result': 'success',
            'color': 'blue',
            'target_slot': 'L2_right',
        }
        before = {
            'cups_on_table': {'blue': 2},
            'stack': {
                'L2_left': {'color': 'blue'},
                'L2_right': None,
            },
        }
        disturbed = {
            'cups_on_table': {'blue': 2},
            'stack': {
                'L2_left': None,
                'L2_right': {'color': 'blue'},
            },
        }

        self.assertTrue(action_result_reflected(result, before, disturbed))

    def test_previous_world_state_is_last_published_snapshot(self):
        builder = GoalStateBuilder()
        builder.set_world({'blue': 6}, {'L1_left': None})
        payload = builder.build_payload()
        builder.commit(payload)
        builder.set_world({'blue': 5}, {'L1_left': 'blue'})

        self.assertEqual(
            builder.previous_world_state()['cups_on_table'],
            {'blue': 6},
        )
        self.assertEqual(
            builder.current_world_state()['cups_on_table'],
            {'blue': 5},
        )

    # ── fallen-cup count / recovery interrupt ─────────────────────────────

    def test_fallen_count_defaults_to_zero(self):
        builder = GoalStateBuilder()
        self.assertEqual(builder.build_payload()['fallen_count'], 0)

    def test_set_fallen_count_normalizes(self):
        builder = GoalStateBuilder()
        for raw, expected in (
                (2, 2), ('3', 3), (-1, 0), (None, 0), ('bad', 0), (True, 0)):
            builder.set_fallen_count(raw)
            self.assertEqual(
                builder.build_payload()['fallen_count'], expected,
                f'raw={raw!r}')

    def test_fallen_count_survives_user_command_reset(self):
        builder = GoalStateBuilder()
        builder.set_fallen_count(1)
        builder.set_user_command('3단 피라미드 쌓아줘')
        payload = builder.build_payload()
        self.assertEqual(payload['mode'], 'cold_start')
        self.assertEqual(payload['fallen_count'], 1)

    def test_fallen_recovery_result_does_not_advance_plan(self):
        builder = GoalStateBuilder()
        builder.set_plan({
            'steps': [
                {'step': 1, 'action': 'pyramid', 'color': 'blue',
                 'target_slot': 'L1_left'},
                {'step': 2, 'action': 'pyramid', 'color': 'blue',
                 'target_slot': 'L1_mid'},
            ],
        })

        builder.on_action_result({
            'step': None,
            'action': 'fallen_recovery',
            'result': 'success',
            'failure_reason': None,
        })

        payload = builder.build_payload()
        self.assertEqual(len(payload['current_plan']['remaining_steps']), 2)
        self.assertEqual(payload['current_goal']['step'], 1)
        self.assertEqual(
            payload['last_action_result']['action'], 'fallen_recovery')


if __name__ == '__main__':
    unittest.main()
