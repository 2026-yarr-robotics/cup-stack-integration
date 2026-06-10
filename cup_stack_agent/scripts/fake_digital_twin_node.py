"""digital_twin_stabilizer_node — publish time-stabilized cup positions.

(Formerly fake_digital_twin_node, which injected hardcoded GT coordinates.)

The real perception pipeline (depth_digital_twin/point_cloud_node) publishes the
raw per-frame cup markers on /digital_twin/boxes. The detector + cup fit already
EMA-smooth and lock each track, but the published box_top position still drifts a
few millimetres every frame, so a pick target read off a single frame is noisy.

This node sits between the real vision node and plan_executor_node: it keeps a
short sliding time window of recent positions per track id and republishes the
**median** (default; robust to YOLO/fit outliers) or mean:

  subscribe boxes_in_topic   (default /digital_twin/boxes)
  publish   boxes_out_topic  (default /digital_twin/boxes_filtered)

Only box_top (position) and box_labels (color/class text) markers are used; the
label is passed through unchanged so blue/red and upright/fallen survive. Output
stays a visualization_msgs/MarkerArray in the same `world` frame, so it is a
drop-in for any consumer of the boxes topic.

Count stabilisation (anti-flicker)
----------------------------------
The raw boxes stream stutters when the exo depth feed drops a beat, which made
every track time out at once and momentarily reported zero cups to
plan_executor (pick step aborts with "no upright cup, tracked=0"). Two guards:

  * Hold — a track keeps being republished at its last good pose for up to
    `track_timeout_s` (2.5 s) after its last fresh detection, so a brief
    dropout does not delete it. (count does not dip)
  * Confirm — a track is only published once it has been freshly seen in at
    least `confirm_frac` of the last `count_window_s` ticks, so a one-off
    spurious detection is not published. (count does not spike)

Net: when the raw count flickers within the window, the most-frequent (stable)
set of cups survives.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import Counter

import rclpy
from rclpy.node import Node
from visualization_msgs.msg import Marker, MarkerArray


def aggregate(samples: list[tuple[float, float, float, float]],
              now: float, window_s: float,
              method: str = 'median') -> tuple[float, float, float] | None:
    """Aggregate recent (t, x, y, z) samples within `window_s` of `now`.

    Returns (x, y, z) via median (robust) or mean, or None if the window is
    empty. Pure function — unit-testable without a running node.
    """
    xs, ys, zs = [], [], []
    for (t, x, y, z) in samples:
        if now - t <= window_s:
            xs.append(x)
            ys.append(y)
            zs.append(z)
    if not xs:
        return None
    if method == 'mean':
        return statistics.fmean(xs), statistics.fmean(ys), statistics.fmean(zs)
    return statistics.median(xs), statistics.median(ys), statistics.median(zs)


class DigitalTwinStabilizerNode(Node):
    def __init__(self, **kwargs) -> None:
        super().__init__('digital_twin_stabilizer_node', **kwargs)

        self.declare_parameter('boxes_in_topic', '/digital_twin/boxes')
        self.declare_parameter('boxes_out_topic', '/digital_twin/boxes_filtered')
        self.declare_parameter('method', 'median')
        self.declare_parameter('window_s', 1.0)
        # A track survives (held at its last position) for this long after its
        # last fresh observation, so a brief boxes_in dropout (e.g. the exo
        # depth stream stuttering) does NOT flush every track and momentarily
        # report zero cups to plan_executor.
        self.declare_parameter('track_timeout_s', 2.5)
        self.declare_parameter('publish_period_s', 0.1)
        # ── Count stabilisation (anti-flicker) ──────────────────────────────
        # Over the last `count_window_s`, a track must be freshly seen in at
        # least `confirm_frac` of the publish ticks before it is published
        # ("confirmed"). This suppresses spurious one-off detections (the count
        # jumping up). Once confirmed, a track keeps being published from its
        # last good pose through gaps up to `track_timeout_s` (the count not
        # dropping). Net effect: when the raw count flickers within the window,
        # the most-frequent (stable) set of cups survives.
        self.declare_parameter('count_window_s', 2.5)
        self.declare_parameter('confirm_frac', 0.5)

        self._method = str(self.get_parameter('method').value)
        self._window_s = float(self.get_parameter('window_s').value)
        self._track_timeout_s = float(
            self.get_parameter('track_timeout_s').value)
        self._count_window_s = float(
            self.get_parameter('count_window_s').value)
        self._confirm_frac = float(self.get_parameter('confirm_frac').value)

        # track id -> list[(t, x, y, z)] samples and track id -> latest label
        self._samples: dict[int, list[tuple[float, float, float, float]]] = {}
        self._labels: dict[int, str] = {}
        self._last_seen: dict[int, float] = {}
        self._frame_id = 'world'
        self._published_ids: set[int] = set()
        # Anti-flicker state.
        self._last_fresh: dict[int, float] = {}          # last fresh-aggregate t
        self._last_good: dict[int, tuple[float, float, float, str]] = {}  # x,y,z,label
        self._fresh_history: list[tuple[float, frozenset[int]]] = []      # per-tick fresh sets
        self._confirmed: set[int] = set()

        self._pub = self.create_publisher(
            MarkerArray, str(self.get_parameter('boxes_out_topic').value), 10)
        self.create_subscription(
            MarkerArray, str(self.get_parameter('boxes_in_topic').value),
            self._on_boxes, 10)
        self.create_timer(
            float(self.get_parameter('publish_period_s').value), self._publish)
        self.get_logger().info(
            f'digital_twin_stabilizer: {self._method} over {self._window_s}s; '
            f'{self.get_parameter("boxes_in_topic").value} -> '
            f'{self.get_parameter("boxes_out_topic").value}')

    # ------------------------------------------------------------------
    def _on_boxes(self, msg: MarkerArray) -> None:
        now = time.monotonic()
        for m in msg.markers:
            if m.action == Marker.DELETEALL:
                self._samples.clear()
                self._labels.clear()
                self._last_seen.clear()
                self._last_fresh.clear()
                self._last_good.clear()
                self._fresh_history.clear()
                self._confirmed.clear()
                continue
            if m.action == Marker.DELETE:
                if m.ns in ('box_top', 'boxes', 'box_labels'):
                    self._drop(m.id)
                continue
            if m.ns == 'box_top':
                if m.header.frame_id:
                    self._frame_id = m.header.frame_id
                self._samples.setdefault(m.id, []).append(
                    (now, float(m.pose.position.x),
                     float(m.pose.position.y), float(m.pose.position.z)))
                self._last_seen[m.id] = now
            elif m.ns == 'box_labels':
                self._labels[m.id] = m.text
                self._last_seen[m.id] = now

    def _drop(self, track_id: int) -> None:
        self._samples.pop(track_id, None)
        self._labels.pop(track_id, None)
        self._last_seen.pop(track_id, None)
        self._last_fresh.pop(track_id, None)
        self._last_good.pop(track_id, None)
        self._confirmed.discard(track_id)

    def _publish(self) -> None:
        now = time.monotonic()
        # Hard-expire tracks with no upstream update within the timeout, and
        # trim each track's position samples to the aggregation window.
        for tid in list(self._last_seen):
            if now - self._last_seen[tid] > self._track_timeout_s:
                self._drop(tid)
                continue
            if tid in self._samples:
                self._samples[tid] = [
                    s for s in self._samples[tid]
                    if now - s[0] <= self._window_s]

        # Tracks with a fresh aggregate this tick (need a label too).
        fresh: dict[int, tuple[float, float, float, str]] = {}
        for tid in sorted(self._samples):
            label = self._labels.get(tid)
            if label is None:
                continue  # need color/class before the executor can use it
            agg = aggregate(self._samples[tid], now, self._window_s,
                            self._method)
            if agg is None:
                continue
            x, y, z = agg
            fresh[tid] = (x, y, z, label)
            self._last_fresh[tid] = now
            self._last_good[tid] = (x, y, z, label)

        # ── Count stabilisation over the sliding window ──────────────────────
        # Record this tick's fresh set, then confirm any track seen freshly in
        # at least `confirm_frac` of the window's ticks. Confirmation is sticky
        # (cleared only on hard expiry / DELETEALL), so a confirmed track is not
        # demoted by a brief dropout — that is what keeps the most-frequent set
        # alive when the raw count flickers.
        self._fresh_history.append((now, frozenset(fresh)))
        self._fresh_history = [
            (t, s) for (t, s) in self._fresh_history
            if now - t <= self._count_window_s]
        n_ticks = len(self._fresh_history)
        presence: Counter[int] = Counter()
        for _, s in self._fresh_history:
            presence.update(s)
        need = max(1, math.ceil(self._confirm_frac * n_ticks))
        for tid, count in presence.items():
            if count >= need:
                self._confirmed.add(tid)

        # Publish confirmed tracks. Use the fresh pose if present this tick,
        # otherwise hold the last good pose — but only while within the hold
        # horizon (track_timeout_s of the last fresh observation).
        markers = MarkerArray()
        live: set[int] = set()
        for tid in sorted(self._confirmed):
            last_fresh = self._last_fresh.get(tid)
            if last_fresh is None or now - last_fresh > self._track_timeout_s:
                self._confirmed.discard(tid)
                continue
            src = fresh.get(tid) or self._last_good.get(tid)
            if src is None:
                continue
            x, y, z, label = src
            live.add(tid)
            markers.markers.append(self._top_marker(tid, x, y, z))
            markers.markers.append(self._label_marker(tid, x, y, z, label))

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
                      text: str) -> Marker:
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
        m.text = text
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
