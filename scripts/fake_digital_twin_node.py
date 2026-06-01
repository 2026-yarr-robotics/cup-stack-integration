"""fake_digital_twin_node — publish measured cup poses on real executor topics.

This is fake data with real topic/message shape. It replaces the digital twin
for the fixed experiment while keeping plan_executor_node unchanged:

  publish /digital_twin/boxes    visualization_msgs/MarkerArray
  publish /stack_track_ids       std_msgs/Int32MultiArray
  subscribe /action_result       std_msgs/String JSON

Measured blue cup pick positions for the experiment:
  L1_left  -> track id 1, x=0.250, y=-0.20
  L1_mid   -> track id 2, x=0.250, y=0.00
  L1_right -> track id 3, x=0.250, y=0.20
  L2_left  -> track id 4, x=0.350, y=-0.20
  L2_right -> track id 5, x=0.350, y=0.00
  L3_top   -> track id 6, x=0.350, y=0.20
"""
from __future__ import annotations

import json

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


class FakeDigitalTwinNode(Node):
    def __init__(self) -> None:
        super().__init__('fake_digital_twin_node')

        self.declare_parameter('boxes_topic', '/digital_twin/boxes')
        self.declare_parameter('stack_track_ids_topic', '/stack_track_ids')
        self.declare_parameter('action_result_topic', '/action_result')
        self.declare_parameter('publish_period_s', 0.5)

        self._stacked_ids: set[int] = set()
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
        self._publish()

    def _publish(self) -> None:
        markers = MarkerArray()
        for slot, (track_id, x, y) in MEASURED_CUPS.items():
            markers.markers.append(self._box_top_marker(track_id, x, y))
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
