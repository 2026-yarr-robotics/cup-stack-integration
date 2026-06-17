"""llm_node — consume /llm_input, call the LLM, publish /llm_output.

Closes the planner loop:

    goal_state_publisher  --/llm_input-->  llm_node  --/llm_output-->  goal_state_publisher
                                                       (plan fed back; goal advances)

The payload's `mode` field (set by goal_state_publisher) selects the prompt —
`cold_start` → planner, `in_flight` → decider — so the model never has to
self-route. Output is JSON-parsed and run through the §8 semantic validation;
on failure the node retries once, then drops with an error log (HITL escalation
is a later concern). This node does NOT execute skills — it only decides.

Prompts are vendored under this package's `prompts/` (installed to share/).
"""
from __future__ import annotations

import json
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import String

from llm_client import (
    DEFAULT_MODEL, DEFAULT_OLLAMA_URL, call_ollama, load_system_prompt,
    parse_model_json, validate_cold_start, validate_fallen_recovery,
    validate_inflight)

COLD_PROMPT = 'cold_start_planner.md'
INFLIGHT_PROMPT = 'inflight_decider.md'


class LLMNode(Node):
    def __init__(self) -> None:
        super().__init__('llm_node')

        self.declare_parameter('llm_input_topic', '/llm_input')
        self.declare_parameter('llm_output_topic', '/llm_output')
        self.declare_parameter('model', DEFAULT_MODEL)
        self.declare_parameter('ollama_url', DEFAULT_OLLAMA_URL)
        self.declare_parameter('timeout_seconds', 120)
        self.declare_parameter('prompt_dir', '')  # '' → vendored share/prompts
        # Cap output tokens per mode. An in-flight decision is a short JSON
        # ({reasoning, decision, plan}); cold-start carries a full 6-step plan.
        # Unbounded, the model rambled ~5.5KB / 37s and slipped a raw control
        # char into the JSON, stalling the loop.
        self.declare_parameter('cold_num_predict', 1536)
        self.declare_parameter('inflight_num_predict', 768)

        self._model = str(self.get_parameter('model').value)
        self._url = str(self.get_parameter('ollama_url').value)
        self._timeout = int(self.get_parameter('timeout_seconds').value)
        self._cold_num_predict = int(
            self.get_parameter('cold_num_predict').value)
        self._inflight_num_predict = int(
            self.get_parameter('inflight_num_predict').value)

        prompt_dir = self._resolve_prompt_dir(
            str(self.get_parameter('prompt_dir').value))
        self._cold_prompt = load_system_prompt(
            (prompt_dir / COLD_PROMPT).read_text(encoding='utf-8'))
        self._inflight_prompt = load_system_prompt(
            (prompt_dir / INFLIGHT_PROMPT).read_text(encoding='utf-8'))

        self._pub = self.create_publisher(
            String, str(self.get_parameter('llm_output_topic').value), 10)
        self.create_subscription(
            String, str(self.get_parameter('llm_input_topic').value),
            self._on_llm_input, 10)

        self.get_logger().debug(
            f'llm_node: model={self._model} url={self._url} '
            f'prompts={prompt_dir}')

    def _resolve_prompt_dir(self, override: str) -> Path:
        if override:
            return Path(override)
        return Path(__file__).resolve().parents[1] / 'prompts'

    # ── Inference ──────────────────────────────────────────────────────────

    def _on_llm_input(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().error(f'/llm_input invalid JSON: {e}')
            return

        mode = payload.get('mode')
        cold = mode == 'cold_start'
        prompt = self._cold_prompt if cold else self._inflight_prompt

        # One retry on parse/validation failure (spec §8.4), then give up.
        for attempt in (1, 2):
            result, ms, err = call_ollama(
                self._model, prompt, payload,
                ollama_url=self._url, timeout_seconds=self._timeout,
                num_predict=(self._cold_num_predict if cold
                             else self._inflight_num_predict))
            if err:
                self.get_logger().error(f'LLM call failed: {err}')
                break  # transport error -> HITL cold-start fallback below
            content = (result.get('message') or {}).get('content', '')
            try:
                parsed = parse_model_json(content)
            except json.JSONDecodeError as e:
                self.get_logger().warn(
                    f'attempt {attempt}: bad JSON ({e}); raw[{len(content)}b] '
                    f'head={content[:1000]!r} tail={content[-1000:]!r}')
                continue
            # fallen_recovery is a mode-independent interrupt: both prompts
            # may emit it (cold-start included), so route it before the
            # mode-specific schema validation.
            if parsed.get('decision') == 'fallen_recovery':
                errors = validate_fallen_recovery(parsed, payload)
            else:
                # COLD-START SAFETY NET: the model (temp 0) tends to emit a FULL
                # pyramid even when cups < cup_budget, inventing phantom cups
                # (e.g. a 4th blue when only 3 exist) → validate rejects
                # 'steps > available cups' → drop → needs-HITL loop, BUILD 0.
                # The prompt already says "stop early when cups run out"; enforce
                # it here so a valid PARTIAL is built instead of nothing.
                if cold:
                    self._trim_cold_start_to_inventory(parsed, payload)
                errors = (validate_cold_start(parsed, payload) if cold
                          else validate_inflight(parsed, payload))
            if errors:
                self.get_logger().warn(
                    f'attempt {attempt}: validation failed: {errors}')
                continue
            self._publish(parsed, mode, ms)
            return

        # HITL fallback: rather than silently stalling the loop (no /llm_output
        # -> no action -> GSP never re-triggers), re-plan from scratch on the
        # CURRENT world (cold-start). plan_executor adopts it as a fresh plan and
        # skips already-filled slots, so the build resumes instead of hanging.
        if not self._hitl_cold_start(payload):
            self.get_logger().error(
                f'{mode}: unrecoverable, cold-start fallback failed — '
                'needs HITL (publishing drop-notice so GSP re-arms the timed '
                'cold-start retry watchdog)')
            self._publish_hitl_drop(mode)

    def _publish_hitl_drop(self, mode: str) -> None:
        """Emit a non-ok cold-start /llm_output when planning is unrecoverable.

        Without this, a hard drop publishes nothing — so plan_executor never
        clears its plan and, critically, GSP's no-progress watchdog never arms
        (it only arms off a RECEIVED /llm_output that yields no executable step).
        The timed cold-start retry that lets a human fix the scene (e.g. add the
        missing cup, then it re-plans) would never fire. This notice carries
        status != 'ok', so plan_executor's existing cold-start branch clears the
        plan and GSP arms the watchdog — timed from THIS drop, not the dispatch,
        so there is no race with the model's planning latency (it has already
        given up here). Schema mirrors the planner's unsupported/insufficient
        shape (status, plan=null, error.code) so downstream readers are unchanged.
        """
        notice = {
            'status': 'needs_hitl',
            'target': None,
            'plan': None,
            'error': {
                'code': 'NEEDS_HITL',
                'message': (f'{mode} planning unrecoverable after retries + '
                            'cold-start fallback'),
            },
        }
        self._publish(notice, mode, 0.0)

    def _trim_cold_start_to_inventory(self, parsed: dict, payload: dict) -> None:
        """Drop over-planned tail steps the table cannot supply (build order).

        Faithful, in-code enforcement of the prompt's "stop early when cups run
        out": walk steps in order decrementing a per-color copy of
        cups_on_table; at the first step whose color is exhausted, STOP and drop
        it + everything after (build order is sequential — you cannot skip a
        lower slot and keep a higher one). The full target / cup_budget /
        slot_colors are LEFT UNCHANGED (validator already allows steps < budget,
        and a constrained slot with no step is valid), so this only ever shortens
        plan.steps. No-op when every step is suppliable (≥6-cup builds untouched)
        or for non-ok / planless outputs. If trimming empties the plan, the
        normal validator rejects it (>=1 step) → existing retry/HITL path.
        """
        if not isinstance(parsed, dict) or parsed.get('status') != 'ok':
            return
        plan = parsed.get('plan')
        if not isinstance(plan, dict):
            return
        steps = plan.get('steps')
        if not isinstance(steps, list) or not steps:
            return
        cw = (payload or {}).get('current_world_state') or {}
        raw = cw.get('cups_on_table') or {}
        remaining = {str(k).lower(): int(v) for k, v in raw.items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool)}
        kept = []
        for s in steps:
            color = str((s or {}).get('color') or '').lower()
            if remaining.get(color, 0) > 0:
                remaining[color] -= 1
                kept.append(s)
            else:
                break  # build order: stop at the first unsuppliable step
        if len(kept) != len(steps):
            plan['steps'] = kept
            self.get_logger().warn(
                f'cold-start plan trimmed {len(steps)}→{len(kept)} steps to fit '
                f'available cups (dropped over-planned/phantom tail)')

    def _hitl_cold_start(self, payload: dict) -> bool:
        """Re-decide a stuck in-flight step via the cold-start planner on the
        current world. Returns True if a valid fresh plan was published."""
        if payload.get('mode') == 'cold_start':
            return False  # a failing cold-start cannot be cold-started again
        cold = dict(payload)
        cold['mode'] = 'cold_start'
        cold.pop('current_goal', None)
        # Keep current_plan: in-flight payloads carry user_command=null, so the
        # ONLY record of the original color constraint is current_plan.target.
        # slot_colors. The cold planner reuses that target and plans just the
        # still-null slots (mid-build), so the re-plan keeps "1단 빨강" etc.
        result, ms, err = call_ollama(
            self._model, self._cold_prompt, cold, ollama_url=self._url,
            timeout_seconds=self._timeout, num_predict=self._cold_num_predict)
        if err:
            self.get_logger().error(f'HITL cold-start call failed: {err}')
            return False
        try:
            parsed = parse_model_json((result.get('message') or {}).get('content', ''))
        except json.JSONDecodeError:
            return False
        self._trim_cold_start_to_inventory(parsed, cold)
        errs = validate_cold_start(parsed, cold)
        if errs:
            self.get_logger().error(f'HITL cold-start invalid: {errs}')
            return False
        self.get_logger().warn(
            'HITL fallback → cold-start replan from current world')
        self._publish(parsed, 'cold_start', ms)
        return True

    def _publish(self, decision: dict, mode: str, ms: float) -> None:
        msg = String()
        msg.data = json.dumps(decision, ensure_ascii=False, sort_keys=True)
        self._pub.publish(msg)
        verdict = decision.get('status') or decision.get('decision')
        self.get_logger().info(f'{mode} -> {verdict} ({ms:.0f} ms)')


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = LLMNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
