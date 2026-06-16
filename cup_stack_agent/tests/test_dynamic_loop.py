"""End-to-end pure-logic walks of the dynamic loop (no LLM, no ROS).

Drives GoalStateBuilder + validate_* + action_result_reflected through the
scenarios in docs/dynamic_loop_plan.md §7 to guard the loop WIRING against
regressions. The LLM decision itself is injected (it is the model, not testable
offline); these tests verify the state machine AROUND it:
  * plan advance on a pyramid success, NON-advance on fallen_recovery/unstack,
  * reflection gating (pyramid fills a slot / unstack empties one),
  * validator accept/reject (done-race guard, slot_colors, top-down removal).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from payload_builder import (  # noqa: E402
    STACK_SLOTS,
    GoalStateBuilder,
    action_result_reflected,
)
from llm_client import validate_cold_start, validate_inflight  # noqa: E402


# ── tiny builders so the scenarios read like the world, not boilerplate ─────

def _pyramid(step, color, slot):
    return {'step': step, 'action': 'pyramid', 'color': color,
            'target_slot': slot}


def _stack(filled: dict) -> dict:
    """filled: {slot: color} -> normalized {slot: {"color": c}|null} (all 6)."""
    return {s: ({'color': filled[s]} if s in filled else None)
            for s in STACK_SLOTS}


def _world(cups: dict, filled: dict) -> dict:
    st = _stack(filled)
    return {'cups_on_table': cups, 'stack': st,
            'filled_slots': sum(1 for v in st.values() if v),
            'total_slots': 6}


def _full_target(slot_colors=None) -> dict:
    if slot_colors is None:
        slot_colors = {s: 'any' for s in STACK_SLOTS}
    return {'base_levels': 3, 'cup_budget': 6,
            'target_slots': list(STACK_SLOTS), 'slot_colors': slot_colors}


def _payload(cups, filled, target, last, fallen=0, remaining=None):
    return {
        'current_world_state': _world(cups, filled),
        'current_plan': {'target': target, 'remaining_steps': remaining or []},
        'last_action_result': last,
        'fallen_count': fallen,
    }


_SUCCESS = {'action': 'pyramid', 'result': 'success'}
_BASE5 = {'L1_left': 'red', 'L1_mid': 'red', 'L1_right': 'red',
          'L2_left': 'blue', 'L2_right': 'blue'}   # L3_top still null


class Scenario1PartialToFull(unittest.TestCase):
    """5 upright + 1 fallen, '3단': build 5, recover, GROW the 6th — never
    stop early while a null target slot can still be filled."""

    def test_done_race_guard_then_grow(self):
        target = _full_target()
        # 5 filled, L3_top null, a stood-up green cup is now on the table.
        pay = _payload({'green': 1}, _BASE5, target, _SUCCESS)
        # done is FORBIDDEN — L3_top is null and a usable cup exists.
        errs = validate_inflight({'decision': 'done', 'plan': None}, pay)
        self.assertTrue(any('still be filled' in e for e in errs), errs)
        # GROW replan (one step for the still-null slot) is accepted.
        grow = {'decision': 'replan',
                'plan': {'target': target,
                         'steps': [_pyramid(1, 'green', 'L3_top')]}}
        self.assertEqual(validate_inflight(grow, pay), [])
        # Once L3_top is filled, done is accepted.
        done_pay = _payload({}, {**_BASE5, 'L3_top': 'green'}, target, _SUCCESS)
        self.assertEqual(
            validate_inflight({'decision': 'done', 'plan': None}, done_pay), [])

    def test_fallen_recovery_does_not_advance_plan(self):
        b = GoalStateBuilder()
        b.set_plan({'status': 'ok', 'target': _full_target(),
                    'plan': {'steps': [_pyramid(1, 'green', 'L3_top')]}})
        b.on_action_result(
            {'step': None, 'action': 'fallen_recovery', 'result': 'success'})
        self.assertEqual(len(b._current_plan['remaining_steps']), 1)


class Scenario2UnconstrainedRegression(unittest.TestCase):
    """Color-free command (slot_colors all 'any') must behave exactly as
    before Phase 2/3 — no spurious color blocks, no unstack pressure."""

    def test_cold_start_all_any_validates(self):
        target = _full_target()
        resp = {'status': 'ok', 'target': target,
                'plan': {'steps': [_pyramid(i + 1, 'blue', s)
                                   for i, s in enumerate(STACK_SLOTS)]},
                'error': None}
        self.assertEqual(validate_cold_start(resp, None), [])

    def test_done_accepted_when_full_regardless_of_color(self):
        target = _full_target()
        # all six filled with mixed colors; with everything 'any' this is done.
        filled = {'L1_left': 'red', 'L1_mid': 'blue', 'L1_right': 'green',
                  'L2_left': 'red', 'L2_right': 'blue', 'L3_top': 'green'}
        pay = _payload({}, filled, target, _SUCCESS)
        self.assertEqual(
            validate_inflight({'decision': 'done', 'plan': None}, pay), [])

    def test_replan_all_any_unconstrained_color(self):
        target = _full_target()
        pay = _payload({'blue': 1}, _BASE5, target, _SUCCESS)
        # 'any' slot -> any color is fine (no slot_colors error).
        replan = {'decision': 'replan',
                  'plan': {'target': target,
                           'steps': [_pyramid(1, 'blue', 'L3_top')]}}
        self.assertEqual(validate_inflight(replan, pay), [])


class Scenario3ColorViolationCorrection(unittest.TestCase):
    """Color constraint + wrong color in the TOP slot: unstack -> refill with
    the required color -> done."""

    SC = {'L1_left': 'red', 'L1_mid': 'red', 'L1_right': 'red',
          'L2_left': 'any', 'L2_right': 'any', 'L3_top': 'red'}
    # L3_top holds blue but requires red.
    BAD = {'L1_left': 'red', 'L1_mid': 'red', 'L1_right': 'red',
           'L2_left': 'blue', 'L2_right': 'blue', 'L3_top': 'blue'}

    def test_unstack_top_violation_accepted(self):
        target = _full_target(self.SC)
        pay = _payload({'red': 1}, self.BAD, target, _SUCCESS)
        self.assertEqual(
            validate_inflight(
                {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}, pay),
            [])

    def test_done_rejected_while_fixable_violation_present(self):
        # all 6 slots filled, L3_top wrong, but red IS available: done must be
        # refused so the loop unstacks instead of terminating.
        target = _full_target(self.SC)
        pay = _payload({'red': 1}, self.BAD, target, _SUCCESS)
        errs = validate_inflight({'decision': 'done', 'plan': None}, pay)
        self.assertTrue(any('color violation' in e for e in errs), errs)

    def test_done_allowed_when_violation_unfixable(self):
        # same wrong L3_top but no red anywhere -> graceful partial done, and
        # unstack must be refused (nothing to refill with).
        target = _full_target(self.SC)
        pay = _payload({}, self.BAD, target, _SUCCESS)
        self.assertEqual(
            validate_inflight({'decision': 'done', 'plan': None}, pay), [])
        unstack = {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}
        self.assertTrue(
            any('none is available' in e
                for e in validate_inflight(unstack, pay)))

    def test_unstack_success_does_not_advance_and_reflects_on_empty(self):
        b = GoalStateBuilder()
        b.set_plan({'status': 'ok', 'target': _full_target(self.SC),
                    'plan': {'steps': [_pyramid(1, 'red', 'L3_top')]}})
        res = {'step': None, 'action': 'unstack', 'target_slot': 'L3_top',
               'color': 'blue', 'result': 'success'}
        b.on_action_result(res)
        self.assertEqual(len(b._current_plan['remaining_steps']), 1)
        # not reflected while the slot still reads filled (stale /stack)...
        self.assertFalse(
            action_result_reflected(res, None, {'stack': _stack(self.BAD)}))
        # ...reflected once it reads empty.
        emptied = {k: v for k, v in self.BAD.items() if k != 'L3_top'}
        self.assertTrue(
            action_result_reflected(res, None, {'stack': _stack(emptied)}))

    def test_refill_after_unstack_honors_required_color(self):
        target = _full_target(self.SC)
        emptied = {k: v for k, v in self.BAD.items() if k != 'L3_top'}
        res = {'action': 'unstack', 'target_slot': 'L3_top', 'result': 'success'}
        pay = _payload({'red': 1, 'blue': 1}, emptied, target, res)
        # correct-color refill accepted
        good = {'decision': 'replan',
                'plan': {'target': target,
                         'steps': [_pyramid(1, 'red', 'L3_top')]}}
        self.assertEqual(validate_inflight(good, pay), [])
        # wrong-color refill rejected by the slot_colors step check
        bad = {'decision': 'replan',
               'plan': {'target': target,
                        'steps': [_pyramid(1, 'blue', 'L3_top')]}}
        self.assertTrue(
            any('requires color' in e for e in validate_inflight(bad, pay)))


class Scenario4TopDownAndStaleGuard(unittest.TestCase):
    """Bottom violation must remove the top blocker first; a stale /stack after
    an unstack must NOT read as reflected (so the loop never re-unstacks)."""

    SC_BURIED = {'L1_left': 'red', 'L1_mid': 'red', 'L1_right': 'red',
                 'L2_left': 'any', 'L2_right': 'any', 'L3_top': 'red'}
    # L1_mid wrong (needs red) but buried under correct L2/L3.
    BURIED = {'L1_left': 'red', 'L1_mid': 'blue', 'L1_right': 'red',
              'L2_left': 'blue', 'L2_right': 'blue', 'L3_top': 'red'}

    def test_buried_violation_unstack_rejected(self):
        target = _full_target(self.SC_BURIED)
        pay = _payload({'red': 1}, self.BURIED, target, _SUCCESS)
        errs = validate_inflight(
            {'decision': 'unstack', 'slot': 'L1_mid', 'plan': None}, pay)
        self.assertTrue(any('buried' in e for e in errs), errs)

    def test_buried_fixable_violation_blocks_done(self):
        # new policy: a buried wrong cup whose required color is obtainable is
        # FIXABLE -> done is refused; the loop must peel to reach it.
        target = _full_target(self.SC_BURIED)
        pay = _payload({'red': 1}, self.BURIED, target, _SUCCESS)
        errs = validate_inflight({'decision': 'done', 'plan': None}, pay)
        self.assertTrue(any('color violation' in e for e in errs), errs)

    def test_buried_fixable_peel_top_blocker_allowed(self):
        # to reach buried L1_mid, peel the topmost cup above it (L3_top) — a
        # correct cup, allowed only because it clears a fixable violation below.
        target = _full_target(self.SC_BURIED)
        pay = _payload({'red': 1}, self.BURIED, target, _SUCCESS)
        self.assertEqual(
            validate_inflight(
                {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}, pay),
            [])

    def test_buried_unfixable_violation_done_allowed(self):
        # no red anywhere (table empty, L3_top blue, red siblings are not above
        # L1_mid) -> genuinely unfixable -> partial done is valid, peel refused.
        unfixable = dict(self.BURIED, L3_top='blue')
        sc = dict(self.SC_BURIED, L3_top='any')
        target = _full_target(sc)
        pay = _payload({}, unfixable, target, _SUCCESS)
        self.assertEqual(
            validate_inflight({'decision': 'done', 'plan': None}, pay), [])
        self.assertTrue(any(
            'clears no fixable buried violation' in e
            for e in validate_inflight(
                {'decision': 'unstack', 'slot': 'L3_top', 'plan': None}, pay)))

    def test_stale_stack_after_unstack_not_reflected(self):
        # /stack still shows the removed cup's old color -> not reflected ->
        # GSP holds the next decision (no premature re-unstack).
        res = {'action': 'unstack', 'target_slot': 'L3_top', 'result': 'success'}
        stale = {'L1_left': 'red', 'L3_top': 'blue'}
        self.assertFalse(
            action_result_reflected(res, None, {'stack': _stack(stale)}))


if __name__ == '__main__':
    unittest.main()
