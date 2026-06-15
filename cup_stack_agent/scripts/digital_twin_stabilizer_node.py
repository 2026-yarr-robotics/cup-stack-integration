"""digital_twin_stabilizer_node — publish time-stabilized cup positions.

(Formerly fake_digital_twin_node, which injected hardcoded GT coordinates.)

The real perception pipeline (depth_digital_twin/point_cloud_node) publishes the
raw per-frame cup markers on /digital_twin/boxes. The detector + cup fit already
EMA-smooth and lock each track, but the published box_top position still drifts a
few millimetres every frame, so a pick target read off a single frame is noisy.

This node sits between the real vision node and plan_executor_node: it keeps a
sticky per-track state and republishes a smoothed pose. New measurements update
that state with an EMA/momentum step; short upstream dropouts keep publishing
the last good pose instead of deleting the cup.

  subscribe boxes_in_topic   (default /digital_twin/boxes)
  publish   boxes_out_topic  (default /digital_twin/boxes_filtered)

Only box_top (position) and box_labels (color/class text) markers are used; the
label is passed through unchanged so blue/red and upright/fallen survive. Output
stays a visualization_msgs/MarkerArray in the same `world` frame, so it is a
drop-in for any consumer of the boxes topic.

Count stabilisation (anti-flicker)
----------------------------------
The raw boxes stream can stutter for seconds. Deleting tracks during that gap
empties plan_executor's cup cache, so this node treats upstream DELETE as a
dropout hint and holds the last good pose until a longer hard timeout expires.
"""
from __future__ import annotations

import math
import time

import rclpy
from rclpy.node import Node
from visualization_msgs.msg import Marker, MarkerArray


def clamp_step(step: tuple[float, float, float],
               max_step_m: float) -> tuple[float, float, float]:
    """Limit one smoothing update so a single noisy frame cannot jump a cup."""
    if max_step_m <= 0.0:
        return step
    norm = math.sqrt(step[0] * step[0] + step[1] * step[1] + step[2] * step[2])
    if norm <= max_step_m or norm == 0.0:
        return step
    scale = max_step_m / norm
    return step[0] * scale, step[1] * scale, step[2] * scale


def distance(a: tuple[float, float, float],
             b: tuple[float, float, float]) -> float:
    return math.sqrt(
        (a[0] - b[0]) * (a[0] - b[0]) +
        (a[1] - b[1]) * (a[1] - b[1]) +
        (a[2] - b[2]) * (a[2] - b[2]))


class DigitalTwinStabilizerNode(Node):
    def __init__(self, **kwargs) -> None:
        super().__init__('digital_twin_stabilizer_node', **kwargs)

        self.declare_parameter('boxes_in_topic', '/digital_twin/boxes')
        self.declare_parameter('boxes_out_topic', '/digital_twin/boxes_filtered')
        # Kept for launch compatibility; smoothing is stateful, not a sliding
        # median window anymore.
        self.declare_parameter('method', 'ema')
        self.declare_parameter('window_s', 1.0)
        # Hard timeout: only after this much silence do we DELETE a track.
        self.declare_parameter('track_timeout_s', 30.0)
        # A track not measured by upstream within this window is still PUBLISHED
        # (coasted from memory for pick stability / RViz) but its label is tagged
        # '_coast' so a consumer can tell it is a stale ghost, not a freshly seen
        # graspable cup (plan_executor skips coasting tracks in coarse pick — F2).
        self.declare_parameter('fresh_ttl_s', 1.5)
        self.declare_parameter('publish_period_s', 0.1)
        self.declare_parameter('smooth_alpha', 0.25)
        self.declare_parameter('momentum_beta', 0.7)
        self.declare_parameter('max_step_m', 0.015)
        self.declare_parameter('merge_distance_m', 0.055)
        # Deprecated compatibility parameters. They no longer affect output.
        self.declare_parameter('count_window_s', 2.5)
        self.declare_parameter('confirm_frac', 0.5)

        self._method = str(self.get_parameter('method').value)
        self._track_timeout_s = float(
            self.get_parameter('track_timeout_s').value)
        self._fresh_ttl_s = float(self.get_parameter('fresh_ttl_s').value)
        self._alpha = float(self.get_parameter('smooth_alpha').value)
        self._beta = float(self.get_parameter('momentum_beta').value)
        self._max_step_m = float(self.get_parameter('max_step_m').value)
        self._merge_distance_m = float(
            self.get_parameter('merge_distance_m').value)

        # Pending raw position updates are consumed once per publish tick.
        self._pending_pos: dict[int, tuple[float, float, float]] = {}
        # Raw detector track id -> canonical stabilized id. This prevents one
        # physical cup from being held as many cups when upstream reassigns ids.
        self._aliases: dict[int, int] = {}
        self._labels: dict[int, str] = {}
        self._last_seen: dict[int, float] = {}
        self._smoothed: dict[int, tuple[float, float, float]] = {}
        self._momentum: dict[int, tuple[float, float, float]] = {}
        self._frame_id = 'world'
        self._published_ids: set[int] = set()

        self._pub = self.create_publisher(
            MarkerArray, str(self.get_parameter('boxes_out_topic').value), 10)
        self.create_subscription(
            MarkerArray, str(self.get_parameter('boxes_in_topic').value),
            self._on_boxes, 10)
        self.create_timer(
            float(self.get_parameter('publish_period_s').value), self._publish)
        self.get_logger().info(
            f'digital_twin_stabilizer: {self._method} '
            f'alpha={self._alpha:.2f} beta={self._beta:.2f} '
            f'max_step={self._max_step_m:.3f}m '
            f'merge={self._merge_distance_m:.3f}m '
            f'timeout={self._track_timeout_s:.1f}s; '
            f'{self.get_parameter("boxes_in_topic").value} -> '
            f'{self.get_parameter("boxes_out_topic").value}')

    # ------------------------------------------------------------------
    def _on_boxes(self, msg: MarkerArray) -> None:
        now = time.monotonic()
        for m in msg.markers:
            if m.action == Marker.DELETEALL:
                # Treat upstream full clears as perception dropouts. Existing
                # smoothed tracks will be held until the hard timeout.
                continue
            if m.action == Marker.DELETE:
                # Do not propagate raw flicker deletes into the executor cache.
                # A real disappearance is handled by the hard timeout.
                continue
            if m.ns == 'box_top':
                if m.header.frame_id:
                    self._frame_id = m.header.frame_id
                pos = (
                    float(m.pose.position.x),
                    float(m.pose.position.y),
                    float(m.pose.position.z))
                tid = self._canonical_id(m.id, pos)
                self._pending_pos[tid] = pos
                self._last_seen[tid] = now
                if tid != m.id:
                    self._move_label(m.id, tid)
            elif m.ns == 'box_labels':
                tid = self._aliases.get(m.id, m.id)
                self._labels[tid] = m.text
                self._last_seen[tid] = now

    def _canonical_id(self, raw_id: int,
                      pos: tuple[float, float, float]) -> int:
        mapped = self._aliases.get(raw_id)
        if mapped is not None:
            return mapped
        best_id = raw_id
        best_dist = self._merge_distance_m
        candidates = set(self._smoothed) | set(self._pending_pos)
        for tid in candidates:
            ref = self._pending_pos.get(tid) or self._smoothed.get(tid)
            if ref is None:
                continue
            d = distance(pos, ref)
            if d <= best_dist:
                best_id = tid
                best_dist = d
        self._aliases[raw_id] = best_id
        return best_id

    def _move_label(self, raw_id: int, canonical_id: int) -> None:
        label = self._labels.pop(raw_id, None)
        if label is not None:
            self._labels[canonical_id] = label
        self._last_seen.pop(raw_id, None)
        self._pending_pos.pop(raw_id, None)

    def _drop(self, track_id: int) -> None:
        self._pending_pos.pop(track_id, None)
        for raw_id, canonical_id in list(self._aliases.items()):
            if raw_id == track_id or canonical_id == track_id:
                self._aliases.pop(raw_id, None)
        self._labels.pop(track_id, None)
        self._last_seen.pop(track_id, None)
        self._smoothed.pop(track_id, None)
        self._momentum.pop(track_id, None)

    def _publish(self) -> None:
        now = time.monotonic()
        for tid, measurement in list(self._pending_pos.items()):
            if tid not in self._smoothed:
                self._smoothed[tid] = measurement
                self._momentum[tid] = (0.0, 0.0, 0.0)
                self._pending_pos.pop(tid, None)
                continue
            prev = self._smoothed[tid]
            old_m = self._momentum.get(tid, (0.0, 0.0, 0.0))
            residual = (
                measurement[0] - prev[0],
                measurement[1] - prev[1],
                measurement[2] - prev[2])
            momentum = (
                self._beta * old_m[0] + (1.0 - self._beta) * residual[0],
                self._beta * old_m[1] + (1.0 - self._beta) * residual[1],
                self._beta * old_m[2] + (1.0 - self._beta) * residual[2])
            step = clamp_step(
                (self._alpha * momentum[0],
                 self._alpha * momentum[1],
                 self._alpha * momentum[2]),
                self._max_step_m)
            self._momentum[tid] = momentum
            self._smoothed[tid] = (
                prev[0] + step[0],
                prev[1] + step[1],
                prev[2] + step[2])
            self._pending_pos.pop(tid, None)

        markers = MarkerArray()
        live: set[int] = set()
        for tid in sorted(self._smoothed):
            last_seen = self._last_seen.get(tid)
            if last_seen is None or now - last_seen > self._track_timeout_s:
                self._drop(tid)
                continue
            label = self._labels.get(tid)
            if label is None:
                continue
            x, y, z = self._smoothed[tid]
            fresh = (now - last_seen) <= self._fresh_ttl_s
            live.add(tid)
            markers.markers.append(self._top_marker(tid, x, y, z))
            markers.markers.append(
                self._label_marker(tid, x, y, z, label, fresh))

        for tid in self._published_ids - live:
            markers.markers.append(self._delete_marker('box_top', tid))
            markers.markers.append(self._delete_marker('box_labels', tid))
        self._published_ids = live

        if markers.markers:
            self._pub.publish(markers)

    def _stamp(self, marker: Marker) -> None:
        marker.header.frame_id = self._frame_id
        marker.header.stamp = self.get_clock().now().to_msg()

    def _top_marker(self, tid: int, x: float, y: float, z: float) -> Marker:
        m = Marker()
        self._stamp(m)
        m.ns = 'box_top'
        m.id = tid
        m.type = Marker.SPHERE
        m.action = Marker.ADD
        m.pose.position.x = x
        m.pose.position.y = y
        m.pose.position.z = z
        m.pose.orientation.w = 1.0
        m.scale.x = m.scale.y = m.scale.z = 0.025
        m.color.g = 1.0
        m.color.a = 1.0
        return m

    def _label_marker(self, tid: int, x: float, y: float, z: float,
                      text: str, fresh: bool = True) -> Marker:
        m = Marker()
        self._stamp(m)
        m.ns = 'box_labels'
        m.id = tid
        m.type = Marker.TEXT_VIEW_FACING
        m.action = Marker.ADD
        m.pose.position.x = x
        m.pose.position.y = y
        m.pose.position.z = z + 0.05
        m.pose.orientation.w = 1.0
        m.scale.z = 0.025
        m.color.r = m.color.g = m.color.b = m.color.a = 1.0
        # Tag a coasted (upstream not currently seeing it) track so consumers can
        # skip the stale ghost; a fresh track keeps the upstream label verbatim.
        m.text = text if fresh else f'{text}_coast'
        return m

    def _delete_marker(self, ns: str, tid: int) -> Marker:
        m = Marker()
        m.ns = ns
        m.id = tid
        m.action = Marker.DELETE
        return m


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = DigitalTwinStabilizerNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
