"""Offline unit tests for plan_executor pure logic."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from plan_executor_node import (  # noqa: E402
    TrackedCup,
    build_move_body,
    llm_to_api_slot,
    parse_label,
    select_cup,
)


class PlanExecutorTest(unittest.TestCase):
    def test_slot_mapping_all_six(self):
        self.assertEqual(llm_to_api_slot('L1_left'), '1l')
        self.assertEqual(llm_to_api_slot('L1_mid'), '1m')
        self.assertEqual(llm_to_api_slot('L1_right'), '1r')
        self.assertEqual(llm_to_api_slot('L2_left'), '2l')
        self.assertEqual(llm_to_api_slot('L2_right'), '2r')
        self.assertEqual(llm_to_api_slot('L3_top'), '3m')

    def test_parse_label_locked_color_class(self):
        color, cls, locked = parse_label(
            '[L]_#7_c=red_upright-cup_0.87_(0.31,0.04,0.18)')
        self.assertEqual((color, cls, locked), ('red', 'upright-cup', True))

    def test_select_first_available_cup_by_color(self):
        cups = {
            1: TrackedCup(pos=(0.280, -0.15, 0.0), color='red',
                          cls='upright-cup', locked=True),
            2: TrackedCup(pos=(0.280, 0.00, 0.0), color='red',
                          cls='upright-cup', locked=True),
        }
        self.assertEqual(select_cup(cups, set(), 'red'), (1, (0.280, -0.15)))
        self.assertEqual(select_cup(cups, {1}, 'red'), (2, (0.280, 0.00)))

    def test_move_body_is_xyz_mode(self):
        self.assertEqual(
            build_move_body(0.280, -0.15, 0.45),
            {'x': 0.280, 'y': -0.15, 'z': 0.45, 'mode': 'absolute'},
        )


if __name__ == '__main__':
    unittest.main()
