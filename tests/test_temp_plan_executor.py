from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from temp_plan_executor_node import (  # noqa: E402
    build_pyramid_body,
    choose_fake_xy,
    llm_to_api_slot,
    parse_fake_xy_map,
)


class TempPlanExecutorTest(unittest.TestCase):
    def test_slot_mapping_all_six(self):
        self.assertEqual(llm_to_api_slot('L1_left'), '1l')
        self.assertEqual(llm_to_api_slot('L1_mid'), '1m')
        self.assertEqual(llm_to_api_slot('L1_right'), '1r')
        self.assertEqual(llm_to_api_slot('L2_left'), '2l')
        self.assertEqual(llm_to_api_slot('L2_right'), '2r')
        self.assertEqual(llm_to_api_slot('L3_top'), '3m')

    def test_parse_fake_xy_map(self):
        parsed = parse_fake_xy_map(
            '{"L1_left":[0.280,-0.15],"L1_mid":[0.280,0]}'
        )
        self.assertEqual(
            parsed,
            {'L1_left': (0.280, -0.15), 'L1_mid': (0.280, 0.0)},
        )

    def test_parse_fake_xy_map_empty_when_not_provided(self):
        self.assertEqual(parse_fake_xy_map(''), {})

    def test_parse_fake_xy_map_rejects_bad_shape(self):
        with self.assertRaises(ValueError):
            parse_fake_xy_map('{"L1_left":[0.1]}')

    def test_parse_fake_xy_map_rejects_unknown_slot(self):
        with self.assertRaises(ValueError):
            parse_fake_xy_map('{"1l":[0.280,-0.15]}')

    def test_choose_fake_xy_uses_slot(self):
        mapping = {'L1_left': (0.280, -0.15)}
        self.assertEqual(
            choose_fake_xy('L1_left', mapping),
            (0.280, -0.15),
        )
        self.assertIsNone(choose_fake_xy('L1_mid', mapping))
        self.assertIsNone(choose_fake_xy(None, mapping))

    def test_build_pyramid_body_fake_xy_slot_only(self):
        body, error = build_pyramid_body(
            'red',
            'L1_mid',
            {'L1_mid': (0.280, 0.0)},
        )
        self.assertIsNone(error)
        self.assertEqual(body, {'x': 0.280, 'y': 0.0, 'slot': '1m'})

    def test_build_pyramid_body_rejects_unknown_slot(self):
        body, error = build_pyramid_body(
            'red',
            '1m',
            {'L1_mid': (0.280, 0.0)},
        )
        self.assertIsNone(body)
        self.assertIn('unknown slot', error)


if __name__ == '__main__':
    unittest.main()
