"""Tests for the perception-glue nodes that replaced the GT-injection fakes.

  * offline unit tests for the pure aggregate() math (always run);
  * live rclpy integration tests that spin the real nodes over DDS and assert
    the published topics (skipped automatically if ROS middleware is unavailable).
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from fake_digital_twin_node import aggregate  # noqa: E402


class AggregateTest(unittest.TestCase):
    def test_median_rejects_outlier(self):
        samples = [
            (0.0, 0.250, -0.199, 0.10),
            (0.1, 0.249, -0.201, 0.10),
            (0.2, 0.400, -0.050, 0.40),  # outlier
            (0.3, 0.250, -0.200, 0.10),
            (0.4, 0.251, -0.200, 0.10),
        ]
        x, y, z = aggregate(samples, now=0.4, window_s=1.0, method='median')
        self.assertAlmostEqual(x, 0.250, places=2)
        self.assertAlmostEqual(y, -0.200, places=2)
        self.assertAlmostEqual(z, 0.10, places=2)

    def test_mean_is_pulled_by_outlier(self):
        samples = [(0.0, 0.25, 0.0, 0.0), (0.1, 0.25, 0.0, 0.0),
                   (0.2, 0.55, 0.0, 0.0)]
        x, _y, _z = aggregate(samples, now=0.2, window_s=1.0, method='mean')
        self.assertGreater(x, 0.30)

    def test_window_drops_stale_samples(self):
        samples = [(0.0, 9.9, 9.9, 9.9), (4.9, 0.25, -0.20, 0.10),
                   (5.0, 0.25, -0.20, 0.10)]
        x, y, _z = aggregate(samples, now=5.0, window_s=1.0, method='median')
        self.assertAlmostEqual(x, 0.25, places=3)
        self.assertAlmostEqual(y, -0.20, places=3)

    def test_empty_window_returns_none(self):
        self.assertIsNone(aggregate([(0.0, 0.25, -0.20, 0.10)], now=10.0,
                                     window_s=1.0))


# ---------------------------------------------------------------------------
# Live integration (DDS). Skipped cleanly when rclpy/middleware is unavailable.
# ---------------------------------------------------------------------------
try:
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from std_msgs.msg import String
    from visualization_msgs.msg import Marker, MarkerArray

    from fake_aggregator_node import AggregatorNode
    from fake_digital_twin_node import DigitalTwinStabilizerNode
    _ROS_OK = True
except Exception:  # pragma: no cover - depends on environment
    _ROS_OK = False


def _box_top(track_id: int, x: float, y: float) -> Marker:
    m = Marker()
    m.ns = 'box_top'
    m.id = track_id
    m.action = Marker.ADD
    m.header.frame_id = 'world'
    m.pose.position.x = x
    m.pose.position.y = y
    m.pose.position.z = 0.10
    return m


def _box_label(track_id: int, text: str) -> Marker:
    m = Marker()
    m.ns = 'box_labels'
    m.id = track_id
    m.action = Marker.ADD
    m.text = text
    return m


@unittest.skipUnless(_ROS_OK, 'rclpy / ROS middleware unavailable')
class LiveNodeTest(unittest.TestCase):
    def setUp(self):
        rclpy.init()

    def tearDown(self):
        rclpy.shutdown()

    def _spin(self, executor, seconds: float, on_tick=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if on_tick is not None:
                on_tick()
            executor.spin_once(timeout_sec=0.02)

    def test_stabilizer_publishes_median_xy(self):
        # Private topics so we don't clash with a live point_cloud_node.
        stab = DigitalTwinStabilizerNode(parameter_overrides=[
            Parameter('boxes_in_topic', value='/test/boxes_in'),
            Parameter('boxes_out_topic', value='/test/boxes_out'),
            Parameter('window_s', value=1.0),
            Parameter('publish_period_s', value=0.05),
        ])
        io = Node('test_io')
        pub = io.create_publisher(MarkerArray, '/test/boxes_in', 10)
        received: list[MarkerArray] = []
        io.create_subscription(
            MarkerArray, '/test/boxes_out', received.append, 10)

        ex = SingleThreadedExecutor()
        ex.add_node(stab)
        ex.add_node(io)

        seq = [(-0.199, 0.251), (-0.201, 0.249), (-0.050, 0.400),  # outlier
               (-0.200, 0.250), (-0.198, 0.252), (-0.202, 0.248)]
        i = {'n': 0}

        def tick():
            y, x = seq[i['n'] % len(seq)]
            i['n'] += 1
            arr = MarkerArray()
            arr.markers = [_box_top(1, x, y),
                           _box_label(1, '#1_c=blue_upright-cup_0.90')]
            pub.publish(arr)

        self._spin(ex, 1.3, on_tick=tick)
        for n in (stab, io):
            ex.remove_node(n)
            n.destroy_node()

        self.assertTrue(received, 'stabilizer published nothing')
        last = received[-1]
        tops = [m for m in last.markers
                if m.ns == 'box_top' and m.action == Marker.ADD and m.id == 1]
        labels = [m for m in last.markers if m.ns == 'box_labels' and m.id == 1]
        self.assertEqual(len(tops), 1)
        self.assertEqual(len(labels), 1)
        self.assertAlmostEqual(tops[0].pose.position.x, 0.250, delta=0.01)
        self.assertAlmostEqual(tops[0].pose.position.y, -0.200, delta=0.01)
        self.assertEqual(tops[0].header.frame_id, 'world')
        self.assertIn('c=blue', labels[0].text)

    def test_aggregator_publishes_command_once(self):
        node = AggregatorNode(parameter_overrides=[
            Parameter('initial_command_delay_s', value=0.2),
            Parameter('publish_period_s', value=0.05),
            Parameter('user_command', value='3단 피라미드 쌓아줘'),
        ])
        io = Node('test_cmd_io')
        got: list[str] = []
        io.create_subscription(String, '/user_command',
                               lambda m: got.append(m.data), 10)
        ex = SingleThreadedExecutor()
        ex.add_node(node)
        ex.add_node(io)
        self._spin(ex, 0.8)
        for n in (node, io):
            ex.remove_node(n)
            n.destroy_node()

        self.assertTrue(got, 'no /user_command received')
        self.assertEqual(got[0], '3단 피라미드 쌓아줘')

    def test_aggregator_relays_world_state(self):
        # Private topics so we don't clash with a live vision pipeline.
        node = AggregatorNode(parameter_overrides=[
            Parameter('cups_in_topic', value='/test/vcups'),
            Parameter('stack_in_topic', value='/test/vstack'),
            Parameter('cups_out_topic', value='/test/cups_out'),
            Parameter('stack_out_topic', value='/test/stack_out'),
            Parameter('initial_command_delay_s', value=999.0),  # don't fire command
        ])
        io = Node('relay_io')
        cups_in = io.create_publisher(String, '/test/vcups', 10)
        stack_in = io.create_publisher(String, '/test/vstack', 10)
        cups_out: list[str] = []
        stack_out: list[str] = []
        io.create_subscription(String, '/test/cups_out',
                               lambda m: cups_out.append(m.data), 10)
        io.create_subscription(String, '/test/stack_out',
                               lambda m: stack_out.append(m.data), 10)
        ex = SingleThreadedExecutor()
        ex.add_node(node)
        ex.add_node(io)

        def tick():
            cups_in.publish(String(data='{"blue": 4, "red": 1}'))
            stack_in.publish(String(data='{"L1_left": "blue"}'))

        self._spin(ex, 0.6, on_tick=tick)
        for n in (node, io):
            ex.remove_node(n)
            n.destroy_node()

        self.assertTrue(cups_out, 'no /cups_on_table relayed')
        self.assertTrue(stack_out, 'no /stack relayed')
        self.assertEqual(cups_out[-1], '{"blue": 4, "red": 1}')
        self.assertEqual(stack_out[-1], '{"L1_left": "blue"}')


if __name__ == '__main__':
    unittest.main()
