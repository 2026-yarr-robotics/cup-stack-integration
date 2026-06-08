"""aggregator_node — relay real vision world-state to the goal-state publisher.

(Formerly fake_aggregator_node, which injected a hardcoded {cups_on_table, stack}
world state.)

The real vision pipeline computes the world state, but publishes it on namespaced
topics so this aggregator can sit between vision and goal_state_publisher_node:

  subscribe cups_in_topic   (default /vision/cups_on_table)  <- point_cloud_node
  subscribe stack_in_topic  (default /vision/stack)          <- verifier_node
  publish   cups_out_topic  (default /cups_on_table)         -> goal_state_publisher
  publish   stack_out_topic (default /stack)                 -> goal_state_publisher
  publish   user_command_topic (default /user_command)

Today it relays the vision values straight through (counts/occupancy carry no
geometric jitter to filter — that correction applies to x,y positions, handled by
digital_twin_stabilizer_node). The relay is the single seam where world-state
refinement (e.g. temporal debouncing of flickering counts) would be added.

It also publishes /user_command — the one world-state input perception cannot
produce — once, after an initial delay.
"""
from __future__ import annotations

import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class AggregatorNode(Node):
    def __init__(self, **kwargs) -> None:
        super().__init__('aggregator_node', **kwargs)

        self.declare_parameter('cups_in_topic', '/vision/cups_on_table')
        self.declare_parameter('stack_in_topic', '/vision/stack')
        self.declare_parameter('cups_out_topic', '/cups_on_table')
        self.declare_parameter('stack_out_topic', '/stack')
        self.declare_parameter('user_command_topic', '/user_command')
        self.declare_parameter('user_command', '3단 피라미드 쌓아줘')
        self.declare_parameter('initial_command_delay_s', 2.0)
        self.declare_parameter('publish_period_s', 0.5)

        self._cups_pub = self.create_publisher(
            String, str(self.get_parameter('cups_out_topic').value), 10)
        self._stack_pub = self.create_publisher(
            String, str(self.get_parameter('stack_out_topic').value), 10)
        self._command_pub = self.create_publisher(
            String, str(self.get_parameter('user_command_topic').value), 10)

        self.create_subscription(
            String, str(self.get_parameter('cups_in_topic').value),
            self._relay_cups, 10)
        self.create_subscription(
            String, str(self.get_parameter('stack_in_topic').value),
            self._relay_stack, 10)

        self._command_published = False
        self._started_at = time.monotonic()
        self.create_timer(
            float(self.get_parameter('publish_period_s').value),
            self._tick_command)
        self.get_logger().info(
            'aggregator_node: relaying '
            f'{self.get_parameter("cups_in_topic").value} -> '
            f'{self.get_parameter("cups_out_topic").value}, '
            f'{self.get_parameter("stack_in_topic").value} -> '
            f'{self.get_parameter("stack_out_topic").value}')

    # ------------------------------------------------------------------
    # Refinement seam: counts/occupancy are relayed as-is today. Temporal
    # debouncing of the world state would go here.
    def _relay_cups(self, msg: String) -> None:
        self._cups_pub.publish(msg)

    def _relay_stack(self, msg: String) -> None:
        self._stack_pub.publish(msg)

    def _tick_command(self) -> None:
        if self._command_published:
            return
        delay = float(self.get_parameter('initial_command_delay_s').value)
        if time.monotonic() - self._started_at < delay:
            return
        command = str(self.get_parameter('user_command').value)
        self._command_pub.publish(String(data=command))
        self._command_published = True
        self.get_logger().info(f'/user_command published: {command!r}')


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = AggregatorNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
