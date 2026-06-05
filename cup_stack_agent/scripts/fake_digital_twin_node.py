"""fake_digital_twin_node — publish the EXO-view cup poses plan_executor reads.

This is fake data with real topic/message shape. It replaces the digital twin
for the fixed experiment while keeping plan_executor_node unchanged:

  publish /digital_twin/boxes    visualization_msgs/MarkerArray
  publish /stack_track_ids       std_msgs/Int32MultiArray
  subscribe /action_result       std_msgs/String JSON

v1.1 coarse→fine pick: plan_executor uses the imprecise EXO view to move the arm
roughly above a cup; pick_node then refines with its hand-eye view. So the poses
published here are the GROUND TRUTH (see MEASURED_CUPS) plus a small, fixed,
per-cup error (`exo_xy_error_m`, default 0.02 m). The matching ground-truth poses
that pick_node consumes are published by fake_pick_view_node on /hand_eye/boxes.

The error stays well under half the 0.10 m cup spacing so each perturbed pose is
still nearest to its own true cup — pick_node picks the nearest cup, so a larger
error would make it grab the wrong one.

Measured (ground-truth) blue cup pick positions for the experiment:
  L1_left  -> track id 1, x=0.250, y=-0.20
  L1_mid   -> track id 2, x=0.250, y=0.00
  L1_right -> track id 3, x=0.250, y=0.20
  L2_left  -> track id 4, x=0.350, y=-0.20
  L2_right -> track id 5, x=0.350, y=0.00
  L3_top   -> track id 6, x=0.350, y=0.20

Disturbance experiment:
  after L2_right succeeds, track id 4 (L2_left) is removed from the stack and
  reappears at the L1_left table position x=0.250, y=-0.20.
"""
from __future__ import annotations

import json
import math

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32MultiArray, String
from visualization_msgs.msg import Marker, MarkerArray


MEASURED_CUPS: dict[str, tuple[int, float, float]] = {
    'L1_left': (1, 0.250, -0.20),
    'L1_mid': (2, 0.250, 0.00),
    'L1_right': (3, 0.250, 0.20),
    'L2_left': (4, 0.350, -0.20),
    'L2_right': (5, 0.350, 0.00),
    'L3_top': (6, 0.350, 0.20),
}

DISTURBANCE_RETURN_POSES: dict[str, tuple[float, float]] = {
    'L2_left': (0.250, -0.20),
}


def exo_offset(track_id: int, error_m: float) -> tuple[float, float]:
    """Fixed, deterministic per-cup XY error for the EXO view.

    Magnitude is exactly `error_m`; the direction is spread around a circle by
    track id so cups err in different directions but identically every tick (no
    randomness, so the experiment replays the same way each run).
    """
    angle = track_id * (2.0 * math.pi / len(MEASURED_CUPS))
    return error_m * math.cos(angle), error_m * math.sin(angle)


class FakeDigitalTwinNode(Node):
    def __init__(self) -> None:
        super().__init__('fake_digital_twin_node')

        self.declare_parameter('boxes_topic', '/digital_twin/boxes')
        self.declare_parameter('stack_track_ids_topic', '/stack_track_ids')
        self.declare_parameter('action_result_topic', '/action_result')
        self.declare_parameter('publish_period_s', 0.5)
        # Per-cup XY error baked into the EXO view plan_executor reads. Kept well
        # under half the 0.10 m cup spacing so each perturbed pose stays nearest
        # to its own true cup (pick_node grabs the nearest one).
        self.declare_parameter('exo_xy_error_m', 0.02)
        self.declare_parameter('disturbance_enabled', True)
        self.declare_parameter('disturbance_trigger_slot', 'L2_right')
        self.declare_parameter('disturbance_removed_slot', 'L2_left')

        self._stacked_ids: set[int] = set()
        self._exo_error = float(self.get_parameter('exo_xy_error_m').value)
        self._cup_positions: dict[str, tuple[float, float]] = {
            slot: (x, y) for slot, (_, x, y) in MEASURED_CUPS.items()
        }
        self._disturbance_applied = False
        self._boxes_pub = self.create_publisher(
            MarkerArray,
            str(self.get_parameter('boxes_topic').value),
            10,
        )
        self._stack_ids_pub = self.create_publisher(
            Int32MultiArray,
            str(self.get_parameter('stack_track_ids_topic').value),
            10,
        )
        self.create_subscription(
            String,
            str(self.get_parameter('action_result_topic').value),
            self._on_action_result,
            10,
        )
        self.create_timer(
            float(self.get_parameter('publish_period_s').value),
            self._publish,
        )
        self.get_logger().info(
            'fake_digital_twin_node: publishing measured blue cup poses')

    def _on_action_result(self, msg: String) -> None:
        try:
            result = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'/action_result invalid JSON: {e}')
            return
        if result.get('result') != 'success':
            return
        target_slot = result.get('target_slot')
        measured = MEASURED_CUPS.get(target_slot)
        if measured is None:
            return
        track_id, _, _ = measured
        self._stacked_ids.add(track_id)
        self._maybe_apply_disturbance(target_slot)
        self._publish()

    def _maybe_apply_disturbance(self, completed_slot: str) -> None:
        if self._disturbance_applied:
            return
        if not bool(self.get_parameter('disturbance_enabled').value):
            return
        trigger_slot = str(
            self.get_parameter('disturbance_trigger_slot').value)
        if completed_slot != trigger_slot:
            return
        removed_slot = str(
            self.get_parameter('disturbance_removed_slot').value)
        measured = MEASURED_CUPS.get(removed_slot)
        return_pose = DISTURBANCE_RETURN_POSES.get(removed_slot)
        if measured is None or return_pose is None:
            self.get_logger().warn(
                f'disturbance skipped: no measured return pose for '
                f'{removed_slot}')
            return
        track_id, _, _ = measured
        self._stacked_ids.discard(track_id)
        self._cup_positions[removed_slot] = return_pose
        self._disturbance_applied = True
        self.get_logger().info(
            f'disturbance applied: track id {track_id} returned to '
            f'({return_pose[0]:.3f},{return_pose[1]:.3f})')

    def _publish(self) -> None:
        markers = MarkerArray()
        for slot, (track_id, _, _) in MEASURED_CUPS.items():
            x, y = self._cup_positions[slot]
            dx, dy = exo_offset(track_id, self._exo_error)
            markers.markers.append(
                self._box_top_marker(track_id, x + dx, y + dy))
            markers.markers.append(self._label_marker(track_id, slot))
        self._boxes_pub.publish(markers)
        self._stack_ids_pub.publish(
            Int32MultiArray(data=sorted(self._stacked_ids)))

    def _box_top_marker(self, track_id: int, x: float, y: float) -> Marker:
        marker = Marker()
        marker.ns = 'box_top'
        marker.id = track_id
        marker.action = Marker.ADD
        marker.pose.position.x = x
        marker.pose.position.y = y
        marker.pose.position.z = 0.0
        return marker

    def _label_marker(self, track_id: int, slot: str) -> Marker:
        marker = Marker()
        marker.ns = 'box_labels'
        marker.id = track_id
        marker.action = Marker.ADD
        marker.text = f'#{track_id}_slot={slot}_c=blue_upright-cup'
        return marker


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = FakeDigitalTwinNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
