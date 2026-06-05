"""plan_executor_node — bridge LLM pyramid plans to a coarse robot move.

Canonical source (lives in LLM-prompting; copied into the integration package
`system_state_aggregator`). Drives the planner→robot loop, but only the COARSE
half of it:

    llm_node --/llm_output--> plan_executor_node --POST /api/robot/move--> robot
                                    │
                                    +--/move_result--> pick_node

Two-stage pick (per the v1.1 design)
------------------------------------
The old flow had this node call /api/robot/skill/pyramid directly with an
exo-view XY. That XY (external camera) was too imprecise to pick on. v1.1 splits
the pick into coarse → fine:

  * plan_executor (this node): resolve color → exo-view cup XY and MOVE the arm
    roughly above it via POST /api/robot/move {x, y, z}. z is a fixed approach
    height so the hand-eye camera can then see the cup.
  * pick_node (other team): once the move returns 200, it reads its hand-eye view,
    computes the precise XY of the nearest cup, and calls /api/robot/skill/pyramid
    itself. pick_node also owns the /action_result completion signal back to GSP,
    because the cup is only actually placed at its pyramid call — not at our move.

So this node carries NO pick/place geometry and never touches the skill server.
Its ONLY output is /move_result: on a successful move it carries the target color,
the API slot key, and the coarse move target XY (pick_node's search center — it
picks the nearest hand-eye cup to it); on failure it carries result="fail" with a
reason and no slot. It never publishes /action_result — that
GSP completion signal is pick_node's job, because the cup is only actually placed
at pick_node's pyramid call, not at our move.

LLM plan steps are still the combined `pyramid` action:
    {"step": 1, "action": "pyramid", "color": "red", "target_slot": "L1_left"}

Color→cup resolution still needs ROS perception (the server exposes neither):
  * /digital_twin/boxes (MarkerArray) → per-cup pose + color/class labels
  * /stack_track_ids (Int32MultiArray) → track ids already stacked (excluded)

dry_run:=true logs each POST body and synthesises success without hitting the
server — safe before the robot stack is up.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

try:  # ROS is optional so the pure helpers below stay unit-testable offline.
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import Int32MultiArray, String
    from visualization_msgs.msg import Marker, MarkerArray
    _HAS_ROS = True
except ImportError:  # pragma: no cover - exercised only outside a ROS env
    _HAS_ROS = False
    Node = object  # type: ignore[assignment,misc]


# ── Slot translation: LLM canonical convention → server API slot key ──────
# Keep in sync with docs/pipeline_runtime.md §3 and the prompts in prompts/.
# pick_node calls /api/robot/skill/pyramid (body field `slot`) with this key, so
# we hand it the already-mapped value in /move_result.
_LLM_TO_API_SLOT: dict[str, str] = {
    'L1_left': '1l', 'L1_mid': '1m', 'L1_right': '1r',
    'L2_left': '2l', 'L2_right': '2r',
    'L3_top':  '3m',
}

# Color / class tokens depth_digital_twin writes into box_labels text. Mirror of
# the skill-manager + perception vocabularies so we agree on pickable cups.
_KNOWN_COLORS: frozenset[str] = frozenset({
    'red', 'orange', 'yellow', 'green', 'blue', 'purple',
    'white', 'black', 'gray', 'unknown',
})
_KNOWN_CLASSES: frozenset[str] = frozenset({
    'upright-cup', 'fallen-cup', 'cup',
})


@dataclass
class TrackedCup:
    pos: tuple[float, float, float] | None = None
    color: str = 'unknown'
    cls: str = 'unknown'
    locked: bool = False


@dataclass
class _HTTPResult:
    ok: bool
    detail: str = ''
    data: Any = None


@dataclass
class _MoveOutcome:
    """Result of resolving a step and moving the arm above the target cup."""
    result: str                  # 'success' | 'fail'
    reason: str | None = None
    color: str | None = None
    api_slot: str | None = None  # mapped key (1l) pick_node passes to pyramid
    x: float | None = None       # coarse move target XY — pick_node's search center
    y: float | None = None


# ── Pure helpers (no ROS) — unit-tested in test_plan_executor.py ──────────

def parse_label(text: str) -> tuple[str, str, bool]:
    """Pull (color, class, locked) out of a box_labels marker text.

    Format: `[L]_#7_c=red_upright-cup_0.87_r=12mm_(0.31,0.04,0.18)`.
    """
    if not text:
        return 'unknown', 'unknown', False
    locked = text.startswith('[L]')
    color = 'unknown'
    cls = 'unknown'
    for tok in text.replace('\n', '_').split('_'):
        t = tok.strip().lower()
        if t.startswith('c=') and t[2:] in _KNOWN_COLORS:
            color = t[2:]
        elif t in _KNOWN_COLORS and color == 'unknown':
            color = t
        if t in _KNOWN_CLASSES:
            cls = t
    return color, cls, locked


def llm_to_api_slot(slot: str | None) -> str | None:
    """Map a canonical LLM slot (L1_left) to the server API key (1l)."""
    if not slot:
        return None
    return _LLM_TO_API_SLOT.get(slot)


def select_cup(
    cups: dict[int, TrackedCup],
    stacked: set[int],
    color: str,
) -> tuple[int, tuple[float, float]] | None:
    """First upright, non-stacked, located cup of `color` → (track id, (x, y)).

    The arm only needs the coarse pick cup's XY to move above it; z is a fixed
    approach height and the hand-eye stage refines XY. Same-color cups are
    interchangeable, so first match (track-id dict order) is fine.
    """
    wanted = color.lower()
    for tid, cup in cups.items():
        if cup.cls == 'fallen-cup':
            continue
        # `tid in stacked` (via /stack_track_ids) handles "already placed".
        # cup.locked is orthogonal (every tracked cup is locked) — do not skip.
        if tid in stacked:
            continue
        if cup.pos is None:
            continue
        if cup.color.lower() != wanted:
            continue
        return tid, (float(cup.pos[0]), float(cup.pos[1]))
    return None


def build_move_body(x: float, y: float, z: float, mode: str = 'absolute') -> dict:
    """The POST body for /api/robot/move — x, y, z are all required."""
    return {'x': float(x), 'y': float(y), 'z': float(z), 'mode': mode}


class PlanExecutorNode(Node):
    def __init__(self) -> None:
        super().__init__('plan_executor_node')

        self.declare_parameter('llm_output_topic', '/llm_output')
        self.declare_parameter('move_result_topic', '/move_result')
        self.declare_parameter('boxes_topic', '/digital_twin/boxes')
        self.declare_parameter('stack_track_ids_topic', '/stack_track_ids')
        # Coarse move endpoint (robot base_link). pick_node owns the subsequent
        # fine pick + /api/robot/skill/pyramid call.
        self.declare_parameter(
            'api_url_move',
            'https://yarr-api-31.simplyimg.com/api/robot/move')
        self.declare_parameter('api_timeout_s', 15.0)
        # Fixed approach height so hand-eye can see the cup after the move.
        self.declare_parameter('move_z', 0.45)
        self.declare_parameter('dry_run', True)

        llm_out = str(self.get_parameter('llm_output_topic').value)
        move_topic = str(self.get_parameter('move_result_topic').value)
        boxes_topic = str(self.get_parameter('boxes_topic').value)
        stacked_topic = str(self.get_parameter('stack_track_ids_topic').value)
        self._api_url = str(self.get_parameter('api_url_move').value)
        self._timeout = float(self.get_parameter('api_timeout_s').value)
        self._move_z = float(self.get_parameter('move_z').value)
        self._dry_run = bool(self.get_parameter('dry_run').value)

        self._state_lock = threading.Lock()
        self._cups: dict[int, TrackedCup] = {}
        self._stacked_ids: set[int] = set()
        self._plan: list[dict] = []
        self._step_idx: int = 0
        self._busy: bool = False

        self._move_pub = self.create_publisher(String, move_topic, 10)
        self.create_subscription(String, llm_out, self._on_llm_output, 10)
        self.create_subscription(
            MarkerArray, boxes_topic, self._on_boxes, 10)
        self.create_subscription(
            Int32MultiArray, stacked_topic, self._on_stack_ids, 10)

        self.get_logger().info(
            f'plan_executor_node: api={self._api_url} '
            f'timeout={self._timeout}s move_z={self._move_z} '
            f'dry_run={self._dry_run} move_result={move_topic}')

    # ── Perception tracking ────────────────────────────────────────────────

    def _on_boxes(self, msg) -> None:
        with self._state_lock:
            for m in msg.markers:
                if m.action == Marker.DELETEALL:
                    self._cups.clear()
                    continue
                if m.action == Marker.DELETE:
                    if m.ns in ('box_top', 'boxes', 'box_labels'):
                        self._cups.pop(m.id, None)
                    continue
                entry = self._cups.setdefault(m.id, TrackedCup())
                if m.ns == 'box_top':
                    entry.pos = (
                        float(m.pose.position.x),
                        float(m.pose.position.y),
                        float(m.pose.position.z))
                elif m.ns == 'box_labels':
                    entry.color, entry.cls, entry.locked = parse_label(m.text)

    def _on_stack_ids(self, msg) -> None:
        with self._state_lock:
            self._stacked_ids = {int(x) for x in msg.data}

    # ── /llm_output handling ────────────────────────────────────────────────

    def _on_llm_output(self, msg) -> None:
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().error(f'/llm_output invalid JSON: {e}')
            return

        if 'status' in data:  # cold-start
            status = data.get('status')
            if status != 'ok':
                err = (data.get('error') or {}).get('message') or status
                self.get_logger().warn(f'cold-start non-ok ({status}): {err}')
                with self._state_lock:
                    self._plan = []
                    self._step_idx = 0
                return
            steps = ((data.get('plan') or {}).get('steps') or [])
            self._adopt_plan(steps, 'cold-start')
            self._execute_next()
            return

        decision = data.get('decision')  # in-flight
        if decision == 'continue':
            self._execute_next()
        elif decision == 'replan':
            steps = ((data.get('plan') or {}).get('steps') or [])
            self._adopt_plan(steps, 'replan')
            self._execute_next()
        elif decision == 'done':
            self.get_logger().info('LLM decision=done — plan complete')
            with self._state_lock:
                self._plan = []
                self._step_idx = 0
        else:
            self.get_logger().warn(
                f'/llm_output unknown shape: {list(data.keys())}')

    def _adopt_plan(self, steps: list[dict], reason: str) -> None:
        with self._state_lock:
            self._plan = list(steps)
            self._step_idx = 0
        self.get_logger().info(f'adopt plan ({reason}): {len(steps)} steps')

    # ── Step pump ────────────────────────────────────────────────────────────

    def _execute_next(self) -> None:
        with self._state_lock:
            if self._busy:
                return
            if self._step_idx >= len(self._plan):
                self.get_logger().info('plan exhausted — awaiting LLM decision')
                return
            step = self._plan[self._step_idx]
            self._busy = True
        threading.Thread(
            target=self._do_step, args=(step,), daemon=True).start()

    def _do_step(self, step: dict) -> None:
        action = step.get('action')
        try:
            if action == 'pyramid':
                outcome = self._do_move(
                    step.get('color'), step.get('target_slot'))
            else:
                outcome = _MoveOutcome('fail', f'unknown action {action!r}')
        except Exception as exc:  # noqa: BLE001
            outcome = _MoveOutcome('fail', f'executor exception: {exc}')

        # Single output channel: /move_result carries success (with slot, handed
        # off to pick_node) or failure. We never talk to GSP — pick_node owns the
        # /action_result completion signal. Advance only on a successful move so
        # the next LLM `continue` (which only arrives after pick_node finishes
        # this cup) fires the next move.
        self._publish_move_result(step, outcome)
        with self._state_lock:
            if outcome.result == 'success':
                self._step_idx += 1
            self._busy = False

    # ── pyramid step: resolve color→cup, move arm above it ───────────────────

    def _do_move(
        self, color: str | None, llm_slot: str | None,
    ) -> _MoveOutcome:
        if not color:
            return _MoveOutcome('fail', 'pyramid step missing color')
        api_slot = llm_to_api_slot(llm_slot)
        if api_slot is None:
            return _MoveOutcome('fail', f'unknown slot {llm_slot!r}')
        with self._state_lock:
            chosen = select_cup(self._cups, set(self._stacked_ids), color)
            tracked, stacked = len(self._cups), len(self._stacked_ids)
        if chosen is None:
            return _MoveOutcome('fail', (
                f'no upright {color} cup available '
                f'(tracked={tracked}, stacked={stacked})'))
        tid, (x, y) = chosen
        z = self._move_z
        body = build_move_body(x, y, z)
        self.get_logger().info(
            f'move: #{tid} {color} at ({x:.3f},{y:.3f},{z:.3f}) '
            f'→ {llm_slot} (api={api_slot})')
        result, reason = self._post_move(body, source_tid=tid, llm_slot=llm_slot)
        return _MoveOutcome(
            result, reason, color=color, api_slot=api_slot, x=x, y=y)

    def _post_move(self, body: dict, *, source_tid: int,
                   llm_slot: str | None) -> tuple[str, str | None]:
        if not self._api_url:
            return 'fail', 'api_url_move is empty'
        log = f'#{source_tid} → {llm_slot}'
        if self._dry_run:
            self.get_logger().info(
                f'[dry-run] POST {self._api_url} {body}  ({log})')
            return 'success', None
        self.get_logger().info(f'POST {self._api_url} {body}  ({log})')
        result = self._http_post_json(self._api_url, body)
        return ('success', None) if result.ok else ('fail', result.detail)

    def _http_post_json(self, url: str, payload: dict) -> _HTTPResult:
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                url, data=data, method='POST',
                headers={'Content-Type': 'application/json',
                         'Accept': 'application/json',
                         'User-Agent': 'curl/7.81.0'})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return _HTTPResult(
                ok=False,
                detail=f'HTTP {e.code}: '
                       f'{e.read().decode("utf-8", "replace")[:200]}')
        except urllib.error.URLError as e:
            return _HTTPResult(ok=False, detail=f'network: {e.reason}')
        except Exception as e:  # noqa: BLE001
            return _HTTPResult(ok=False, detail=f'transport: {e}')
        try:
            parsed = json.loads(body)
        except ValueError:
            return _HTTPResult(
                ok=False, detail=f'non-JSON response: {body[:200]}')
        if isinstance(parsed, dict) and parsed.get('success') is False:
            return _HTTPResult(
                ok=False,
                detail=str(parsed.get('detail') or parsed.get('message')
                           or 'success=false'),
                data=parsed)
        return _HTTPResult(ok=True, data=parsed)

    # ── publish ───────────────────────────────────────────────────────────────

    def _publish_move_result(self, step: dict, outcome: _MoveOutcome) -> None:
        """Emit the move outcome on /move_result — this node's only output.

        On success the arm is above the target cup and `slot` carries the API key
        pick_node passes to its pyramid call. On failure `slot` is omitted and
        `failure_reason` explains why. ROS publish is fire-and-forget, so an
        exception is the only failure we can observe — we log it (there is no
        other channel to fall back to by design).
        """
        out: dict = {
            'step': step.get('step'),
            'action': step.get('action'),
            'color': step.get('color'),
            'result': outcome.result,
        }
        if outcome.result == 'success':
            out['slot'] = outcome.api_slot
            out['x'] = outcome.x   # coarse move target — pick_node's search center
            out['y'] = outcome.y
        else:
            out['failure_reason'] = outcome.reason
        try:
            self._move_pub.publish(String(data=json.dumps(out)))
        except Exception as exc:  # noqa: BLE001
            self.get_logger().error(f'/move_result publish failed: {exc}')
            return
        self.get_logger().info(f'/move_result {outcome.result}: {out}')


def main(args: list[str] | None = None) -> None:
    if not _HAS_ROS:
        raise SystemExit('rclpy not available — this entrypoint needs ROS 2')
    rclpy.init(args=args)
    node = PlanExecutorNode()
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
