"""Fake-XY executor for the test_v1.0 YARR integration experiment.

The real plan_executor depends on digital-twin MarkerArray data to select a cup
pose. This temporary node removes that dependency by using configured fake XY
coordinates per target_slot. It still exercises the LLM output -> executor -> pyramid
API request -> action_result path.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

try:
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String
    _HAS_ROS = True
except ImportError:  # pragma: no cover
    _HAS_ROS = False
    Node = object  # type: ignore[assignment,misc]


LLM_TO_API_SLOT: dict[str, str] = {
    'L1_left': '1l',
    'L1_mid': '1m',
    'L1_right': '1r',
    'L2_left': '2l',
    'L2_right': '2r',
    'L3_top': '3m',
}

@dataclass
class HTTPResult:
    ok: bool
    detail: str = ''
    data: Any = None


def llm_to_api_slot(slot: str | None) -> str | None:
    if not slot:
        return None
    return LLM_TO_API_SLOT.get(slot)


def parse_fake_xy_map(text: str) -> dict[str, tuple[float, float]]:
    if not text.strip():
        return {}
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError('fake_xy_by_slot_json must be a JSON object')

    parsed: dict[str, tuple[float, float]] = {}
    for slot, xy in raw.items():
        if not isinstance(slot, str):
            raise ValueError('fake XY slot keys must be strings')
        if slot not in LLM_TO_API_SLOT:
            raise ValueError(f'unknown LLM slot {slot!r}')
        if (
            not isinstance(xy, (list, tuple))
            or len(xy) != 2
            or not all(isinstance(v, (int, float)) for v in xy)
        ):
            raise ValueError(f'fake XY for {slot!r} must be [x, y]')
        parsed[slot] = (float(xy[0]), float(xy[1]))
    return parsed


def choose_fake_xy(
    llm_slot: str | None,
    fake_xy_by_slot: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    if not llm_slot:
        return None
    return fake_xy_by_slot.get(llm_slot)


def build_pyramid_body(
    color: str | None,
    llm_slot: str | None,
    fake_xy_by_slot: dict[str, tuple[float, float]],
) -> tuple[dict | None, str | None]:
    if not color:
        return None, 'pyramid step missing color'
    api_slot = llm_to_api_slot(llm_slot)
    if api_slot is None:
        return None, f'unknown slot {llm_slot!r}'
    xy = choose_fake_xy(llm_slot, fake_xy_by_slot)
    if xy is None:
        return None, f'no fake xy configured for slot {llm_slot!r}'
    x, y = xy
    return {'x': float(x), 'y': float(y), 'slot': api_slot}, None


class TempPlanExecutorNode(Node):
    def __init__(self) -> None:
        super().__init__('temp_plan_executor_node')

        self.declare_parameter('llm_output_topic', '/llm_output')
        self.declare_parameter('action_result_topic', '/action_result')
        self.declare_parameter(
            'api_url_pyramid',
            'http://localhost:8000/api/robot/skill/pyramid',
        )
        self.declare_parameter('api_timeout_s', 15.0)
        self.declare_parameter('dry_run', True)
        self.declare_parameter('execute_on_cold_start', True)
        self.declare_parameter('fake_xy_by_slot_json', '')

        llm_out = str(self.get_parameter('llm_output_topic').value)
        action_topic = str(self.get_parameter('action_result_topic').value)
        self._api_url = str(self.get_parameter('api_url_pyramid').value)
        self._timeout = float(self.get_parameter('api_timeout_s').value)
        self._dry_run = bool(self.get_parameter('dry_run').value)
        self._execute_on_cold_start = bool(
            self.get_parameter('execute_on_cold_start').value
        )
        self._fake_xy_by_slot = parse_fake_xy_map(
            str(self.get_parameter('fake_xy_by_slot_json').value)
        )
        if not self._fake_xy_by_slot:
            raise ValueError('fake_xy_by_slot_json is required')

        self._state_lock = threading.Lock()
        self._plan: list[dict] = []
        self._step_idx = 0
        self._busy = False

        self._action_pub = self.create_publisher(String, action_topic, 10)
        self.create_subscription(String, llm_out, self._on_llm_output, 10)

        self.get_logger().info(
            f'temp_plan_executor_node: api={self._api_url} '
            f'timeout={self._timeout}s dry_run={self._dry_run} '
            f'fake_xy={self._fake_xy_by_slot}'
        )

    def _on_llm_output(self, msg) -> None:
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().error(f'/llm_output invalid JSON: {e}')
            return

        if 'status' in data:
            status = data.get('status')
            if status != 'ok':
                self.get_logger().warn(f'cold-start non-ok ({status})')
                self._clear_plan()
                return
            self._adopt_plan(((data.get('plan') or {}).get('steps') or []),
                             'cold-start')
            if self._execute_on_cold_start:
                self._execute_next()
            return

        decision = data.get('decision')
        if decision == 'continue':
            self._execute_next()
        elif decision == 'replan':
            self._adopt_plan(((data.get('plan') or {}).get('steps') or []),
                             'replan')
            self._execute_next()
        elif decision == 'done':
            self.get_logger().info('LLM decision=done')
            self._clear_plan()
        else:
            self.get_logger().warn(
                f'/llm_output unknown shape: {list(data.keys())}'
            )

    def _adopt_plan(self, steps: list[dict], reason: str) -> None:
        with self._state_lock:
            self._plan = list(steps)
            self._step_idx = 0
        self.get_logger().info(f'adopt plan ({reason}): {len(steps)} steps')

    def _clear_plan(self) -> None:
        with self._state_lock:
            self._plan = []
            self._step_idx = 0

    def _execute_next(self) -> None:
        with self._state_lock:
            if self._busy:
                return
            if self._step_idx >= len(self._plan):
                self.get_logger().info('plan exhausted; awaiting LLM decision')
                return
            step = self._plan[self._step_idx]
            self._busy = True
        threading.Thread(
            target=self._do_step,
            args=(step,),
            daemon=True,
        ).start()

    def _do_step(self, step: dict) -> None:
        result = 'fail'
        reason: str | None = None
        if step.get('action') != 'pyramid':
            reason = f'unknown action {step.get("action")!r}'
        else:
            body, reason = build_pyramid_body(
                step.get('color'),
                step.get('target_slot'),
                self._fake_xy_by_slot,
            )
            if body is not None:
                self.get_logger().info(
                    f'fake pyramid: {step.get("color")} '
                    f'({body["x"]:.3f},{body["y"]:.3f}) '
                    f'-> {step.get("target_slot")} api={body["slot"]}'
                )
                post_result = self._post(body)
                result = 'success' if post_result.ok else 'fail'
                reason = None if post_result.ok else post_result.detail

        self._publish_action_result(step, result, reason)
        with self._state_lock:
            if result == 'success':
                self._step_idx += 1
            self._busy = False

    def _post(self, body: dict) -> HTTPResult:
        if not self._api_url:
            return HTTPResult(ok=False, detail='api_url_pyramid is empty')
        if self._dry_run:
            self.get_logger().info(f'[dry-run] POST {self._api_url} {body}')
            return HTTPResult(ok=True)
        self.get_logger().info(f'POST {self._api_url} {body}')
        return self._http_post_json(self._api_url, body)

    def _http_post_json(self, url: str, payload: dict) -> HTTPResult:
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                url,
                data=data,
                method='POST',
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return HTTPResult(
                ok=False,
                detail=f'HTTP {e.code}: '
                       f'{e.read().decode("utf-8", "replace")[:200]}',
            )
        except urllib.error.URLError as e:
            return HTTPResult(ok=False, detail=f'network: {e.reason}')
        except Exception as e:  # noqa: BLE001
            return HTTPResult(ok=False, detail=f'transport: {e}')
        try:
            parsed = json.loads(body)
        except ValueError:
            return HTTPResult(
                ok=False,
                detail=f'non-JSON response: {body[:200]}',
            )
        if isinstance(parsed, dict) and parsed.get('success') is False:
            return HTTPResult(
                ok=False,
                detail=str(
                    parsed.get('detail')
                    or parsed.get('message')
                    or 'success=false'
                ),
                data=parsed,
            )
        return HTTPResult(ok=True, data=parsed)

    def _publish_action_result(
        self,
        step: dict,
        result: str,
        failure_reason: str | None,
    ) -> None:
        out: dict = {
            'step': step.get('step'),
            'action': step.get('action'),
            'result': result,
            'failure_reason': failure_reason,
        }
        if 'color' in step:
            out['color'] = step['color']
        if 'target_slot' in step:
            out['target_slot'] = step['target_slot']
        self._action_pub.publish(String(data=json.dumps(out)))
        self.get_logger().info(f'/action_result {result}: {out}')


def main(args: list[str] | None = None) -> None:
    if not _HAS_ROS:
        raise SystemExit('rclpy not available; this entrypoint needs ROS 2')
    rclpy.init(args=args)
    node = TempPlanExecutorNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
