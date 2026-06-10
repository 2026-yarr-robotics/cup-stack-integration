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
    parse_model_json, validate_cold_start, validate_inflight)

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
                return  # transport errors are not retried here
            content = (result.get('message') or {}).get('content', '')
            try:
                parsed = parse_model_json(content)
            except json.JSONDecodeError as e:
                self.get_logger().warn(
                    f'attempt {attempt}: bad JSON ({e}); raw[{len(content)}b] '
                    f'head={content[:1000]!r} tail={content[-1000:]!r}')
                continue
            errors = (validate_cold_start(parsed) if cold
                      else validate_inflight(parsed, payload))
            if errors:
                self.get_logger().warn(
                    f'attempt {attempt}: validation failed: {errors}')
                continue
            self._publish(parsed, mode, ms)
            return

        self.get_logger().error(
            f'{mode}: validation failed twice — dropping (needs HITL)')

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
