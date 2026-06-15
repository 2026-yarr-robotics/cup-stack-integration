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
    parse_fallen_count,
    parse_label,
    select_cup,
    select_unstack_dest,
    stably_occupied,
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

    def test_parse_fallen_count_valid_samples(self):
        self.assertEqual(parse_fallen_count({'count': 2}), 2)
        self.assertEqual(parse_fallen_count({'count': 0}), 0)
        self.assertEqual(parse_fallen_count({'count': '3'}), 3)

    def test_parse_fallen_count_unusable_returns_none(self):
        # A message that is not a valid hand-eye sample must NOT become a
        # count — a false 0 would fail-fast a recovery whose cup is there.
        self.assertIsNone(parse_fallen_count(None))
        self.assertIsNone(parse_fallen_count('garbage'))
        self.assertIsNone(parse_fallen_count({}))
        self.assertIsNone(parse_fallen_count({'red': 1}))
        self.assertIsNone(parse_fallen_count({'count': 'bad'}))
        self.assertIsNone(parse_fallen_count({'count': True}))

    def test_parse_fallen_count_clamps_negative(self):
        self.assertEqual(parse_fallen_count({'count': -1}), 0)


class SelectUnstackDestTest(unittest.TestCase):
    """Phase 3: unstack drop-spot selection (reuse empty pick spots)."""

    FB = (0.30, -0.15)   # fallback base
    RAD = 0.06

    def test_prefers_most_recent_empty_spot(self):
        spots = [(0.40, 0.00), (0.40, 0.10)]
        dest, idx = select_unstack_dest(spots, (), self.RAD, self.FB, 0.08, 6)
        self.assertEqual(dest, (0.40, 0.10))   # most recent first
        self.assertEqual(idx, 1)

    def test_skips_occupied_spot(self):
        spots = [(0.40, 0.00), (0.40, 0.10)]
        # the most-recent spot now has a cup -> fall through to the older one
        dest, idx = select_unstack_dest(
            spots, ((0.40, 0.10),), self.RAD, self.FB, 0.08, 6)
        self.assertEqual(dest, (0.40, 0.00))
        self.assertEqual(idx, 0)

    def test_fallback_when_all_spots_occupied(self):
        spots = [(0.40, 0.00)]
        dest, idx = select_unstack_dest(
            spots, ((0.40, 0.00),), self.RAD, self.FB, 0.08, 6)
        self.assertIsNone(idx)
        self.assertEqual(dest, self.FB)

    def test_fallback_grid_avoids_occupied_base(self):
        # base busy -> nudge laterally to the next free grid slot
        dest, idx = select_unstack_dest(
            [], (self.FB,), self.RAD, self.FB, 0.08, 6)
        self.assertIsNone(idx)
        self.assertNotEqual(dest, self.FB)


class SelectCupStaleTest(unittest.TestCase):
    """F2: coarse pick must skip a coasting (stale) exo ghost track."""

    def test_skips_stale_picks_fresh(self):
        cups = {
            1: TrackedCup(pos=(0.78, -0.26, 0.2), color='blue',
                          cls='upright-cup', stale=True),    # far ghost
            2: TrackedCup(pos=(0.35, -0.10, 0.2), color='blue',
                          cls='upright-cup', stale=False),   # real, fresh
        }
        res = select_cup(cups, set(), 'blue')
        self.assertIsNotNone(res)
        self.assertEqual(res[0], 2)

    def test_all_stale_returns_none(self):
        cups = {1: TrackedCup(pos=(0.78, -0.26, 0.2), color='blue',
                              cls='upright-cup', stale=True)}
        self.assertIsNone(select_cup(cups, set(), 'blue'))


class StablyOccupiedTest(unittest.TestCase):
    """A1: a planned step is skipped only if its slot is occupied continuously
    for skip_debounce_s — a transient phantom must not skip a real step."""

    STACK = {'L1_left': {'color': 'blue'}, 'L2_left': {'color': 'blue'}}
    DB = 5.0

    def test_stable_when_old_enough(self):
        occ = {'L1_left': 100.0}
        self.assertTrue(stably_occupied(self.STACK, occ, 'L1_left', 106.0, self.DB))

    def test_phantom_too_recent_not_stable(self):
        # L2_left occupied but only 2.4s ago (the observed phantom) -> NOT skipped
        occ = {'L2_left': 100.0}
        self.assertFalse(
            stably_occupied(self.STACK, occ, 'L2_left', 102.4, self.DB))

    def test_empty_slot_not_occupied(self):
        self.assertFalse(stably_occupied(self.STACK, {}, 'L1_right', 999.0, self.DB))

    def test_occupied_but_no_since_not_stable(self):
        self.assertFalse(stably_occupied(self.STACK, {}, 'L1_left', 999.0, self.DB))


if __name__ == '__main__':
    unittest.main()
