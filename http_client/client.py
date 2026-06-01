"""LLM-driven pyramid skill client.

Flow
----
1. Fetch robot_state from GET /api/robot/status.
2. Cold-start: build payload → Ollama → receive plan.
3. In-flight loop:
   a. Execute the next plan step via POST /api/robot/skill/pyramid (blocking,
      no timeout — waits until robot motion completes).
   b. Fetch fresh robot_state from /api/robot/status.
   c. Build in-flight payload → Ollama → decision.
   d. continue: proceed to next step.
      replan:  adopt new plan, proceed.
      done:    exit loop.

/cups_on_table and /stack are supplied as empty dicts; perception mocking
is handled by an external component (fake_aggregator_node).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_client import (  # noqa: E402
    call_ollama,
    load_system_prompt,
    parse_model_json,
    validate_cold_start,
    validate_inflight,
)
from payload_builder import GoalStateBuilder  # noqa: E402

import config  # noqa: E402
from executor import execute_step, fetch_robot_state  # noqa: E402


def _load_prompts() -> tuple[str, str]:
    cold = load_system_prompt(
        (config.PROMPT_DIR / "cold_start_planner.md").read_text(encoding="utf-8")
    )
    inflight = load_system_prompt(
        (config.PROMPT_DIR / "inflight_decider.md").read_text(encoding="utf-8")
    )
    return cold, inflight


def _llm_call(
    prompt: str,
    payload: dict,
    mode: str,
) -> dict | None:
    """Call Ollama with one retry on parse/validation failure."""
    for attempt in (1, 2):
        result, ms, err = call_ollama(
            config.MODEL, prompt, payload,
            ollama_url=config.OLLAMA_URL,
            timeout_seconds=120,
        )
        if err:
            print(f"[ERROR] LLM transport error: {err}")
            return None
        content = (result.get("message") or {}).get("content", "")
        try:
            parsed = parse_model_json(content)
        except Exception as exc:
            print(f"[WARN] attempt {attempt}: JSON parse failed ({exc})")
            continue
        errors = (
            validate_cold_start(parsed)
            if mode == "cold_start"
            else validate_inflight(parsed, payload)
        )
        if errors:
            print(f"[WARN] attempt {attempt}: validation failed: {errors}")
            continue
        print(f"[llm/{mode}] ok ({ms:.0f} ms)")
        return parsed
    print(f"[ERROR] {mode}: validation failed twice — aborting")
    return None


def run() -> int:
    cold_prompt, inflight_prompt = _load_prompts()
    api_url = f"{config.SERVER_URL}/api/robot/skill/pyramid"

    builder = GoalStateBuilder()
    builder.set_user_command(config.COMMAND)
    builder.set_world({}, {})  # perception mocked by fake_aggregator_node

    # ── cold start ────────────────────────────────────────────────────────
    builder.set_robot_state(fetch_robot_state(config.SERVER_URL))
    payload = builder.build_payload()
    print(f"\n[cold_start] command={config.COMMAND!r}  model={config.MODEL}")

    parsed = _llm_call(cold_prompt, payload, "cold_start")
    if parsed is None:
        return 1
    if parsed.get("status") != "ok":
        print(f"[cold_start] LLM refused: {parsed.get('error')}")
        return 1

    steps = (parsed.get("plan") or {}).get("steps", [])
    print(f"[cold_start] plan received: {len(steps)} steps")
    builder.set_plan(parsed["plan"])
    builder.commit(payload)

    # ── in-flight loop ────────────────────────────────────────────────────
    step_num = 0
    while True:
        goal = builder.current_goal()
        if goal is None:
            print("[done] plan exhausted")
            break

        step_num += 1
        color, slot = goal.get("color"), goal.get("target_slot")
        print(f"\n[step {step_num}] {color} → {slot}")

        action_result = execute_step(
            goal,
            config.FAKE_XY,
            api_url,
            timeout=None,  # blocking until robot motion completes; server has no queue
            dry_run=config.DRY_RUN,
        )
        ok = action_result["result"] == "success"
        print(f"[step {step_num}] {'ok' if ok else 'FAIL: ' + str(action_result['failure_reason'])}")

        builder.on_action_result(action_result)
        builder.set_robot_state(fetch_robot_state(config.SERVER_URL))

        payload = builder.build_payload()
        parsed = _llm_call(inflight_prompt, payload, "in_flight")
        if parsed is None:
            return 1
        builder.commit(payload)

        decision = parsed.get("decision")
        print(f"[in_flight] decision={decision}")

        if decision == "done":
            print("[done] LLM declared task complete")
            break
        if decision == "replan":
            new_steps = len((parsed.get("plan") or {}).get("steps", []))
            print(f"[replan] adopting {new_steps} new steps")
            builder.set_plan(parsed["plan"])
        # "continue" → loop naturally to next step

    return 0


if __name__ == "__main__":
    sys.exit(run())
