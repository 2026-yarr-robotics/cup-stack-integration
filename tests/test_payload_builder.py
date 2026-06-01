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


if __name__ == '__main__':
    unittest.main()
