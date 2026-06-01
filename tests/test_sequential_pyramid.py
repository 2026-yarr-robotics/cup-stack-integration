"""Verify that 3 pyramid skill steps are dispatched in order: L1_left → L1_mid → L1_right.

No actual HTTP calls are made. LLM responses and execute_step are mocked.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "http_client"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import client  # noqa: E402

_COLD_START_RESPONSE = {
    "status": "ok",
    "target": {
        "base_levels": 3,
        "cup_budget": 3,
        "target_slots": ["L1_left", "L1_mid", "L1_right"],
    },
    "plan": {
        "steps": [
            {"step": 1, "action": "pyramid", "color": "red",   "target_slot": "L1_left"},
            {"step": 2, "action": "pyramid", "color": "blue",  "target_slot": "L1_mid"},
            {"step": 3, "action": "pyramid", "color": "green", "target_slot": "L1_right"},
        ]
    },
    "error": None,
}

_INFLIGHT_CONTINUE = {"decision": "continue", "plan": None}
_INFLIGHT_DONE     = {"decision": "done",     "plan": None}

_EMPTY_ROBOT_STATE = {"gripper": {"holding": None, "force_n": 0.0}}


def _success_action_result(step: dict) -> dict:
    return {
        "step": step.get("step"),
        "action": "pyramid",
        "color": step.get("color"),
        "target_slot": step.get("target_slot"),
        "result": "success",
        "failure_reason": None,
    }


class TestSequentialPyramid(unittest.TestCase):

    def test_three_steps_executed_left_mid_right(self):
        """3개 스텝이 L1_left → L1_mid → L1_right 순서로 한 번씩 실행된다."""
        llm_responses = iter([
            _COLD_START_RESPONSE,
            _INFLIGHT_CONTINUE,
            _INFLIGHT_CONTINUE,
            _INFLIGHT_DONE,
        ])
        executed_slots: list[str] = []

        def fake_llm_call(prompt, payload, mode):
            return next(llm_responses)

        def fake_execute_step(step, fake_xy, api_url, timeout, dry_run):
            executed_slots.append(step["target_slot"])
            return _success_action_result(step)

        with (
            patch("client._load_prompts", return_value=("cold", "inflight")),
            patch("client._llm_call", side_effect=fake_llm_call),
            patch("client.execute_step", side_effect=fake_execute_step),
            patch("client.fetch_robot_state", return_value=_EMPTY_ROBOT_STATE),
        ):
            exit_code = client.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(executed_slots, ["L1_left", "L1_mid", "L1_right"])

    def test_exactly_three_requests(self):
        """pyramid skill 요청이 정확히 3회 발생해야 한다."""
        llm_responses = iter([
            _COLD_START_RESPONSE,
            _INFLIGHT_CONTINUE,
            _INFLIGHT_CONTINUE,
            _INFLIGHT_DONE,
        ])
        call_count = 0

        def fake_llm_call(prompt, payload, mode):
            return next(llm_responses)

        def fake_execute_step(step, fake_xy, api_url, timeout, dry_run):
            nonlocal call_count
            call_count += 1
            return _success_action_result(step)

        with (
            patch("client._load_prompts", return_value=("cold", "inflight")),
            patch("client._llm_call", side_effect=fake_llm_call),
            patch("client.execute_step", side_effect=fake_execute_step),
            patch("client.fetch_robot_state", return_value=_EMPTY_ROBOT_STATE),
        ):
            client.run()

        self.assertEqual(call_count, 3)

    def test_robot_state_fetched_before_cold_start_and_after_each_step(self):
        """robot_state는 cold_start 전 1회 + 각 스텝 실행 후 1회 = 총 4회 조회된다."""
        llm_responses = iter([
            _COLD_START_RESPONSE,
            _INFLIGHT_CONTINUE,
            _INFLIGHT_CONTINUE,
            _INFLIGHT_DONE,
        ])
        fetch_count = 0

        def fake_llm_call(prompt, payload, mode):
            return next(llm_responses)

        def fake_execute_step(step, fake_xy, api_url, timeout, dry_run):
            return _success_action_result(step)

        def fake_fetch(server_url, *args, **kwargs):
            nonlocal fetch_count
            fetch_count += 1
            return _EMPTY_ROBOT_STATE

        with (
            patch("client._load_prompts", return_value=("cold", "inflight")),
            patch("client._llm_call", side_effect=fake_llm_call),
            patch("client.execute_step", side_effect=fake_execute_step),
            patch("client.fetch_robot_state", side_effect=fake_fetch),
        ):
            client.run()

        self.assertEqual(fetch_count, 4)


if __name__ == "__main__":
    unittest.main()
