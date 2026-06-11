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
  * /digital_twin/boxes_filtered (MarkerArray) → stabilized per-cup pose +
    color/class labels (median-filtered real exo view from the stabilizer)
  * /stack_track_ids (Int32MultiArray) → track ids already stacked (excluded)

dry_run:=true logs each POST body and synthesises success without hitting the
server — safe before the robot stack is up.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

try:  # ROS is optional so the pure helpers below stay unit-testable offline.
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import ExternalShutdownException
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

_VERIFIER_TO_CANONICAL_SLOT: dict[str, str] = {
    'L1_L': 'L1_left', 'L1_M': 'L1_mid', 'L1_R': 'L1_right',
    'L2_L': 'L2_left', 'L2_R': 'L2_right',
    'L3_T': 'L3_top',
}
_STACK_SLOTS: tuple[str, ...] = tuple(_LLM_TO_API_SLOT)

_PYRAMID_CUP_SPACING = 0.078
_DEFAULT_STACK_CENTER_X = 0.54
_DEFAULT_STACK_CENTER_Y = 0.0
_DEFAULT_STACK_DEGREE = 90.0
_API_SLOT_OFFSETS: dict[str, float] = {
    "1l": -_PYRAMID_CUP_SPACING,
    "1m": 0.0,
    "1r": _PYRAMID_CUP_SPACING,
    "2l": -_PYRAMID_CUP_SPACING / 2.0,
    "2r": _PYRAMID_CUP_SPACING / 2.0,
    "3m": 0.0,
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
    source_tid: int | None = None


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


def canonical_slot(slot: str | None) -> str | None:
    if not slot:
        return None
    return _VERIFIER_TO_CANONICAL_SLOT.get(slot, slot)


def normalize_stack(stack: Any) -> dict[str, Any]:
    out = {slot: None for slot in _STACK_SLOTS}
    if not isinstance(stack, dict):
        return out
    for key, value in stack.items():
        slot = canonical_slot(str(key))
        if slot in out:
            out[slot] = value
    return out


def default_stack_slot_xy(
    center_x: float = _DEFAULT_STACK_CENTER_X,
    center_y: float = _DEFAULT_STACK_CENTER_Y,
    degree: float = _DEFAULT_STACK_DEGREE,
) -> dict[str, tuple[float, float]]:
    rad = math.radians(degree)
    ux, uy = math.cos(rad), math.sin(rad)
    return {
        key: (float(center_x) + lat * ux, float(center_y) + lat * uy)
        for key, lat in _API_SLOT_OFFSETS.items()
    }


def stack_slot_occupied(stack: dict[str, Any], slot: str | None) -> bool:
    canonical = canonical_slot(slot)
    if canonical not in _LLM_TO_API_SLOT:
        return False
    value = stack.get(canonical)
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ('', 'none', 'null', 'empty', 'false')
    return bool(value)


def drop_occupied_steps(
    steps: list[dict],
    stack: dict[str, Any],
) -> tuple[list[dict], list[dict]]:
    kept = []
    skipped = []
    for step in steps:
        if (
            step.get('action') == 'pyramid'
            and stack_slot_occupied(stack, step.get('target_slot'))
        ):
            skipped.append(step)
            continue
        kept.append(step)
    return kept, skipped


def llm_to_api_slot(slot: str | None) -> str | None:
    """Map a canonical LLM/verifier slot to the server API key."""
    canonical = canonical_slot(slot)
    if not canonical:
        return None
    return _LLM_TO_API_SLOT.get(canonical)


def select_cup(
    cups: dict[int, TrackedCup],
    stacked: set[int],
    color: str,
    excluded_xy: tuple[tuple[float, float], ...] = (),
    exclude_radius_m: float = 0.0,
) -> tuple[int, tuple[float, float]] | None:
    """First upright, non-stacked, located cup of `color` → (track id, (x, y)).

    The arm only needs the coarse pick cup's XY to move above it; z is a fixed
    approach height and the hand-eye stage refines XY. Same-color cups are
    interchangeable, so first match (track-id dict order) is fine.
    """
    wanted = color.lower()
    r2 = max(0.0, exclude_radius_m) ** 2
    for tid, cup in cups.items():
        if cup.cls == 'fallen-cup':
            continue
        # `tid in stacked` (via /stack_track_ids) handles "already placed".
        # cup.locked is orthogonal (every tracked cup is locked) — do not skip.
        if tid in stacked:
            continue
        if cup.pos is None:
            continue
        x, y = float(cup.pos[0]), float(cup.pos[1])
        if r2 > 0.0 and any(
            (x - sx) * (x - sx) + (y - sy) * (y - sy) <= r2
            for sx, sy in excluded_xy
        ):
            continue
        if cup.color.lower() != wanted:
            continue
        return tid, (x, y)
    return None


def build_move_body(x: float, y: float, z: float, mode: str = 'absolute') -> dict:
    """The POST body for /api/robot/move — x, y, z are all required."""
    return {'x': float(x), 'y': float(y), 'z': float(z), 'mode': mode}


class PlanExecutorNode(Node):
    def __init__(self) -> None:
        super().__init__('plan_executor_node')

        self.declare_parameter('llm_output_topic', '/llm_output')
        self.declare_parameter('move_result_topic', '/move_result')
        # Stabilized exo view from digital_twin_stabilizer_node (median-filtered
        # real point_cloud_node boxes). Falls back to /digital_twin/boxes if the
        # stabilizer is not running.
        self.declare_parameter('boxes_topic', '/digital_twin/boxes_filtered')
        self.declare_parameter('stack_track_ids_topic', '/stack_track_ids')
        self.declare_parameter('stack_topic', '/stack')
        self.declare_parameter(
            "pyramid_config_url",
            "http://localhost/api/robot/config/pyramid")
        self.declare_parameter("stack_exclude_radius_m", 0.06)
        self.declare_parameter("stack_center_x", _DEFAULT_STACK_CENTER_X)
        self.declare_parameter("stack_center_y", _DEFAULT_STACK_CENTER_Y)
        self.declare_parameter("stack_degree", _DEFAULT_STACK_DEGREE)
        # Coarse move endpoint (robot base_link). pick_node owns the subsequent
        # fine pick + /api/robot/skill/pyramid call.
        self.declare_parameter(
            'api_url_move',
            'https://yarr-api-31.simplyimg.com/api/robot/move')
        self.declare_parameter('api_timeout_s', 15.0)
        # Fixed approach height so hand-eye can see the cup after the move.
        self.declare_parameter('move_z', 0.45)
        # how long to wait for perception (boxes_filtered -> _cups) before
        # failing a move when no matching cup is tracked yet (cold-start race).
        self.declare_parameter('cup_wait_s', 5.0)
        # Hand-eye fallback: when exo perception (boxes_filtered) sees no
        # cup, fall back to /hand_eye/boxes (base_link xy, what pick_node
        # uses) for the coarse move, and publish its graspable cup counts
        # (excluding the build/stack area) so the aggregator can supplement
        # cups_on_table when exo is empty.
        self.declare_parameter('handeye_fallback', True)
        self.declare_parameter('hand_eye_boxes_topic', '/hand_eye/boxes')
        self.declare_parameter('handeye_cups_topic',
                               '/vision/cups_on_table_handeye')
        # drop a hand-eye cup this long after its last marker (stale guard:
        # /hand_eye/boxes stops or misses a DELETE -> no phantom cup).
        self.declare_parameter('handeye_ttl_s', 1.5)
        # once exo has been empty this long, use hand-eye immediately
        # instead of burning the whole cup_wait_s.
        self.declare_parameter('handeye_fallback_grace_s', 0.5)
        self.declare_parameter("reserved_track_ttl_s", 45.0)
        self.declare_parameter('dry_run', True)

        llm_out = str(self.get_parameter('llm_output_topic').value)
        move_topic = str(self.get_parameter('move_result_topic').value)
        boxes_topic = str(self.get_parameter('boxes_topic').value)
        stacked_topic = str(self.get_parameter('stack_track_ids_topic').value)
        stack_topic = str(self.get_parameter('stack_topic').value)
        self._pyramid_config_url = str(
            self.get_parameter("pyramid_config_url").value)
        self._stack_exclude_radius_m = float(
            self.get_parameter("stack_exclude_radius_m").value)
        self._handeye_fallback = bool(
            self.get_parameter('handeye_fallback').value)
        handeye_boxes_topic = str(
            self.get_parameter('hand_eye_boxes_topic').value)
        handeye_cups_topic = str(
            self.get_parameter('handeye_cups_topic').value)
        self._handeye_ttl_s = float(self.get_parameter('handeye_ttl_s').value)
        self._handeye_grace_s = float(
            self.get_parameter('handeye_fallback_grace_s').value)
        self._stack_center_x = float(self.get_parameter("stack_center_x").value)
        self._stack_center_y = float(self.get_parameter("stack_center_y").value)
        self._stack_degree = float(self.get_parameter("stack_degree").value)
        self._api_url = str(self.get_parameter('api_url_move').value)
        self._timeout = float(self.get_parameter('api_timeout_s').value)
        self._move_z = float(self.get_parameter('move_z').value)
        self._cup_wait_s = float(self.get_parameter('cup_wait_s').value)
        self._reserved_track_ttl_s = float(
            self.get_parameter("reserved_track_ttl_s").value)
        self._dry_run = bool(self.get_parameter('dry_run').value)

        self._state_lock = threading.Lock()
        self._cups: dict[int, TrackedCup] = {}
        self._handeye_cups: dict[int, dict] = {}   # /hand_eye/boxes id->{xy,color}
        self._stacked_ids: set[int] = set()
        self._stack: dict[str, Any] = normalize_stack(None)
        self._stack_slot_xy: dict[str, tuple[float, float]] = default_stack_slot_xy(
            self._stack_center_x, self._stack_center_y, self._stack_degree)
        self._reserved_ids: dict[int, float] = {}
        self._plan: list[dict] = []
        self._step_idx: int = 0
        self._busy: bool = False

        self._move_pub = self.create_publisher(String, move_topic, 10)
        self.create_subscription(String, llm_out, self._on_llm_output, 10)
        self.create_subscription(
            MarkerArray, boxes_topic, self._on_boxes, 10)
        self.create_subscription(
            Int32MultiArray, stacked_topic, self._on_stack_ids, 10)
        self.create_subscription(String, stack_topic, self._on_stack, 10)
        self._handeye_pub = self.create_publisher(
            String, handeye_cups_topic, 10)
        self.create_subscription(
            MarkerArray, handeye_boxes_topic, self._on_handeye_boxes, 10)
        self.create_timer(0.5, self._publish_handeye_counts)
        self.create_timer(2.0, self._refresh_pyramid_slots)
        self._refresh_pyramid_slots()

        self.get_logger().debug(
            f'plan_executor_node: api={self._api_url} '
            f'timeout={self._timeout}s move_z={self._move_z} '
            f'dry_run={self._dry_run} move_result={move_topic}')

    def _active_reserved_ids_unlocked(self) -> set[int]:
        now = time.monotonic()
        expired = [tid for tid, until in self._reserved_ids.items() if until <= now]
        for tid in expired:
            self._reserved_ids.pop(tid, None)
        return set(self._reserved_ids)

    def _reserve_source_track(self, tid: int | None) -> None:
        if tid is None or self._reserved_track_ttl_s <= 0.0:
            return
        self._reserved_ids[int(tid)] = time.monotonic() + self._reserved_track_ttl_s

    def _refresh_pyramid_slots(self) -> None:
        if not self._pyramid_config_url:
            return
        try:
            with urllib.request.urlopen(
                self._pyramid_config_url,
                timeout=0.5,
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self.get_logger().warn(
                f"pyramid config refresh failed: {e}",
                throttle_duration_sec=5.0)
            return
        slots = data.get("slots") if isinstance(data, dict) else None
        if not isinstance(slots, dict):
            return
        slot_xy: dict[str, tuple[float, float]] = {}
        for key, val in slots.items():
            if not isinstance(val, dict):
                continue
            try:
                slot_xy[str(key)] = (float(val["x"]), float(val["y"]))
            except (KeyError, TypeError, ValueError):
                continue
        if slot_xy:
            with self._state_lock:
                self._stack_slot_xy = slot_xy

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

    def _on_stack(self, msg) -> None:
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'/stack invalid JSON: {e}')
            return
        if not isinstance(data, dict):
            self.get_logger().warn('/stack payload is not an object')
            return
        with self._state_lock:
            self._stack = normalize_stack(data)

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
        raw_steps = list(steps) if isinstance(steps, list) else []
        with self._state_lock:
            plan, skipped = drop_occupied_steps(raw_steps, self._stack)
            self._plan = plan
            self._step_idx = 0
        if skipped:
            self.get_logger().warn(
                f'adopt plan ({reason}): skipped {len(skipped)} occupied-slot step(s): '
                f'{[(s.get("step"), s.get("target_slot")) for s in skipped]}')
        self.get_logger().info(
            f'adopt plan ({reason}): {len(plan)} executable step(s)')

    # ── Step pump ────────────────────────────────────────────────────────────

    def _execute_next(self) -> None:
        skipped = []
        with self._state_lock:
            if self._busy:
                return
            while self._step_idx < len(self._plan):
                step = self._plan[self._step_idx]
                if (
                    step.get('action') == 'pyramid'
                    and stack_slot_occupied(self._stack, step.get('target_slot'))
                ):
                    skipped.append((step.get('step'), step.get('target_slot')))
                    self._step_idx += 1
                    continue
                break
            if self._step_idx >= len(self._plan):
                self.get_logger().info('plan exhausted — awaiting LLM decision')
                return
            step = self._plan[self._step_idx]
            self._busy = True
        if skipped:
            self.get_logger().warn(f'skipped occupied-slot step(s): {skipped}')
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
                self._reserve_source_track(outcome.source_tid)
                self._step_idx += 1
            self._busy = False

    # ── pyramid step: resolve color→cup, move arm above it ───────────────────

    # ── Hand-eye fallback (used when exo perception is empty) ──────────────
    def _on_handeye_boxes(self, msg) -> None:
        with self._state_lock:
            for m in msg.markers:
                if m.action == Marker.DELETEALL:
                    self._handeye_cups.clear()
                    continue
                if m.action == Marker.DELETE:
                    if m.ns in ('box_top', 'boxes', 'box_labels'):
                        self._handeye_cups.pop(m.id, None)
                    continue
                entry = self._handeye_cups.setdefault(
                    m.id, {'xy': None, 'color': 'unknown', 'seen_at': 0.0})
                entry['seen_at'] = time.monotonic()
                if m.ns == 'box_top':
                    entry['xy'] = (float(m.pose.position.x),
                                   float(m.pose.position.y))
                elif m.ns == 'box_labels':
                    col, _cls, _lk = parse_label(m.text)
                    if col != 'unknown':
                        entry['color'] = col

    def _graspable_handeye_unlocked(self) -> list:
        """Hand-eye cups OUTSIDE the build/stack area (same exclusion as
        select_cup) -> [(id, (x,y), color)]. Placed/stacked cups sit at slot
        positions and are dropped, so we never re-grab a built cup."""
        slots = tuple(self._stack_slot_xy.values())
        r2 = self._stack_exclude_radius_m ** 2
        now = time.monotonic()
        out = []
        for hid, c in self._handeye_cups.items():
            xy = c.get('xy')
            if xy is None:
                continue
            if now - c.get('seen_at', 0.0) > self._handeye_ttl_s:
                continue   # stale — /hand_eye/boxes stopped or missed DELETE
            x, y = xy
            if any((x - sx) ** 2 + (y - sy) ** 2 <= r2 for sx, sy in slots):
                continue   # inside the build area = placed cup, not graspable
            out.append((hid, xy, c.get('color') or 'unknown'))
        out.sort(key=lambda t: t[0])   # deterministic (by marker id)
        return out

    def _select_handeye_cup(self, color):
        with self._state_lock:
            grasp = self._graspable_handeye_unlocked()
        if not grasp:
            return None
        same = [(i, xy) for (i, xy, col) in grasp if col == color]
        pool = same if same else [(i, xy) for (i, xy, col) in grasp]
        hid, xy = pool[0]
        # negative id namespace -> never collides with an exo track id in
        # the reservation / source_tid space.
        return (-(int(hid) + 1), xy)

    def _publish_handeye_counts(self) -> None:
        with self._state_lock:
            grasp = self._graspable_handeye_unlocked()
        counts: dict[str, int] = {}
        for _i, _xy, col in grasp:
            counts[col] = counts.get(col, 0) + 1
        self._handeye_pub.publish(String(data=json.dumps(counts)))

    def _do_move(
        self, color: str | None, llm_slot: str | None,
    ) -> _MoveOutcome:
        if not color:
            return _MoveOutcome('fail', 'pyramid step missing color')
        api_slot = llm_to_api_slot(llm_slot)
        if api_slot is None:
            return _MoveOutcome('fail', f'unknown slot {llm_slot!r}')
        canonical = canonical_slot(llm_slot)
        # Perception (boxes_filtered -> self._cups) may not have populated
        # yet when the LLM plan arrives (_execute_next fires the first move
        # immediately on adopt). Poll up to cup_wait_s for a matching cup
        # instead of hard-failing the step (which stalls the loop). The lock
        # is released between tries so _on_boxes can fill self._cups.
        start = time.monotonic()
        deadline = start + self._cup_wait_s
        chosen = None
        waited = False
        while True:
            with self._state_lock:
                if stack_slot_occupied(self._stack, canonical):
                    return _MoveOutcome(
                        'fail',
                        f'target slot {canonical!r} already occupied')
                chosen = select_cup(
                    self._cups,
                    set(self._stacked_ids) | self._active_reserved_ids_unlocked(),
                    color,
                    tuple(self._stack_slot_xy.values()),
                    self._stack_exclude_radius_m)
                tracked, stacked = len(self._cups), len(self._stacked_ids)
            if chosen is not None:
                if waited:
                    self.get_logger().info(
                        f'cup ready after wait (tracked={tracked})')
                break
            now = time.monotonic()
            # hand-eye fallback: once exo has been empty for grace_s, take it
            # immediately instead of waiting out the full cup_wait_s.
            if self._handeye_fallback and now - start >= self._handeye_grace_s:
                he = self._select_handeye_cup(color)
                if he is not None:
                    self.get_logger().warn(
                        f'exo cups empty — hand-eye fallback cup #{he[0]} '
                        f'at ({he[1][0]:.3f},{he[1][1]:.3f})')
                    chosen = he
                    break
            if now >= deadline:
                return _MoveOutcome('fail', (
                    f'no upright {color} cup available '
                    f'(tracked={tracked}, stacked={stacked})'))
            waited = True
            time.sleep(0.1)
        tid, (x, y) = chosen
        z = self._move_z
        body = build_move_body(x, y, z)
        self.get_logger().info(
            f'move: #{tid} {color} at ({x:.3f},{y:.3f},{z:.3f}) '
            f'→ {llm_slot} (api={api_slot})')
        result, reason = self._post_move(body, source_tid=tid, llm_slot=llm_slot)
        return _MoveOutcome(
            result, reason, color=color, api_slot=api_slot, x=x, y=y,
            source_tid=tid)

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
            out['source_tid'] = outcome.source_tid
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
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
