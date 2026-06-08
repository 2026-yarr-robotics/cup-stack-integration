"""topic_logger_node -- write experiment topic snapshots to JSONL files.

This node is observation-only. It subscribes to the fixed experiment topics and
does not publish anything back into the pipeline.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32MultiArray, String
from visualization_msgs.msg import MarkerArray


STRING_TOPICS = [
    '/user_command',
    '/cups_on_table',
    '/stack',
    '/robot_state',
    '/llm_input',
    '/llm_output',
    '/action_result',
]


class TopicLoggerNode(Node):
    def __init__(self) -> None:
        super().__init__('topic_logger_node')
        self.declare_parameter('log_dir', 'logs/manual')
        self.declare_parameter('dedupe', True)

        self._log_dir = Path(str(self.get_parameter('log_dir').value))
        self._dedupe = bool(self.get_parameter('dedupe').value)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._last_payload_by_topic: dict[str, str] = {}

        for topic in STRING_TOPICS:
            self.create_subscription(
                String,
                topic,
                lambda msg, topic=topic: self._log_string(topic, msg),
                10,
            )
        self.create_subscription(
            Int32MultiArray,
            '/stack_track_ids',
            self._log_stack_track_ids,
            10,
        )
        self.create_subscription(
            MarkerArray,
            '/digital_twin/boxes_filtered',
            self._log_boxes,
            10,
        )

        self.get_logger().info(
            f'topic_logger_node: writing topic logs to {self._log_dir}')

    def _log_string(self, topic: str, msg: String) -> None:
        try:
            payload: Any = json.loads(msg.data)
        except json.JSONDecodeError:
            payload = msg.data
        self._write(topic, payload)

    def _log_stack_track_ids(self, msg: Int32MultiArray) -> None:
        self._write('/stack_track_ids', [int(x) for x in msg.data])

    def _log_boxes(self, msg: MarkerArray) -> None:
        markers = []
        for marker in msg.markers:
            markers.append({
                'ns': marker.ns,
                'id': int(marker.id),
                'action': int(marker.action),
                'text': marker.text,
                'position': {
                    'x': float(marker.pose.position.x),
                    'y': float(marker.pose.position.y),
                    'z': float(marker.pose.position.z),
                },
            })
        self._write('/digital_twin/boxes_filtered', markers)

    def _write(self, topic: str, payload: Any) -> None:
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        if self._dedupe and self._last_payload_by_topic.get(topic) == encoded_payload:
            return
        self._last_payload_by_topic[topic] = encoded_payload

        record = {
            'time': time.time(),
            'iso_time': datetime.now(timezone.utc).isoformat(),
            'topic': topic,
            'data': payload,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        self._append(self._log_dir / 'topics.jsonl', line)
        self._append(self._log_dir / f'{self._topic_name(topic)}.jsonl', line)

    @staticmethod
    def _append(path: Path, line: str) -> None:
        with path.open('a', encoding='utf-8') as f:
            f.write(line)
            f.write('\n')

    @staticmethod
    def _topic_name(topic: str) -> str:
        stripped = topic.strip('/')
        if not stripped:
            return 'root'
        return stripped.replace('/', '__')


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = TopicLoggerNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
