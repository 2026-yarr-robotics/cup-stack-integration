"""fake_hand_eye_node — publish the GROUND-TRUTH cup poses pick_node reads.

Fake data with real topic/message shape, the precise counterpart to
fake_digital_twin_node. That node bakes a small EXO-view error into
/digital_twin/boxes for plan_executor's coarse move; this node publishes the
TRUE cup poses on /hand_eye/boxes for pick_node's fine pick. In the loop,
plan_executor moves the arm to its perturbed pose, then pick_node grabs the
nearest cup it sees here — which is the true cup, because the EXO error is kept
small enough that each perturbed pose stays nearest to its own true cup.

  publish   /hand_eye/boxes   visualization_msgs/MarkerArray
  subscribe /action_result    std_msgs/String JSON   (disturbance sync only)

It shares MEASURED_CUPS and the return-cup disturbance with fake_digital_twin_node
so both views agree on where every cup is — only the EXO error differs. The topic
keeps its real name (not a fake_* name) so the real pick_node needs no change when
a real hand-eye vision node eventually replaces this fake.
"""
from __future__ import annotations

import json

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from visualization_msgs.msg import Marker, MarkerArray

from fake_digital_twin_node import DISTURBANCE_RETURN_POSES, MEASURED_CUPS


class FakeHandEyeNode(Node):
    def __init__(self) -> None:
        super().__init__('fake_hand_eye_node')

        self.declare_parameter('boxes_topic', '/hand_eye/boxes')
        self.declare_parameter('action_result_topic', '/action_result')
        self.declare_parameter('publish_period_s', 0.5)
        self.declare_parameter('disturbance_enabled', True)
        self.declare_parameter('disturbance_trigger_slot', 'L2_right')
        self.declare_parameter('disturbance_removed_slot', 'L2_left')

        self._cup_positions: dict[str, tuple[float, float]] = {
            slot: (x, y) for slot, (_, x, y) in MEASURED_CUPS.items()
        }
        self._disturbance_applied = False
        self._boxes_pub = self.create_publisher(
            MarkerArray, str(self.get_parameter('boxes_topic').value), 10)
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
            'fake_hand_eye_node: publishing ground-truth blue cup poses')

    def _on_action_result(self, msg: String) -> None:
        try:
            result = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'/action_result invalid JSON: {e}')
            return
        if result.get('result') != 'success':
            return
        self._maybe_apply_disturbance(result.get('target_slot'))
        self._publish()

    def _maybe_apply_disturbance(self, completed_slot: str | None) -> None:
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
        return_pose = DISTURBANCE_RETURN_POSES.get(removed_slot)
        if return_pose is None:
            self.get_logger().warn(
                f'disturbance skipped: no return pose for {removed_slot}')
            return
        self._cup_positions[removed_slot] = return_pose
        self._disturbance_applied = True
        self.get_logger().info(
            f'disturbance applied: {removed_slot} cup returned to '
            f'({return_pose[0]:.3f},{return_pose[1]:.3f})')

    def _publish(self) -> None:
        markers = MarkerArray()
        for slot, (track_id, _, _) in MEASURED_CUPS.items():
            x, y = self._cup_positions[slot]
            markers.markers.append(self._box_top_marker(track_id, x, y))
            markers.markers.append(self._label_marker(track_id, slot))
        self._boxes_pub.publish(markers)

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
    node = FakeHandEyeNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
