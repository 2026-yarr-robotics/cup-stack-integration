"""agent.launch.py — bring up the cup-stack LLM agent over the real vision pipeline.

This launches the in-repo agent nodes. The REAL perception pipeline runs in its
own (separately sourced) workspace and must already be publishing, with these
remaps so the aggregator can sit in front of goal_state_publisher:

  /digital_twin/boxes          point_cloud_node  (raw per-frame cup positions)
  /vision/cups_on_table        point_cloud_node  (-r /cups_on_table:=/vision/cups_on_table)
  /vision/stack                verifier_node     (-r /stack:=/vision/stack)
  /stack_track_ids             verifier_node

Run:
  ros2 launch agent.launch.py                 # dry-run (no robot API call)
  ros2 launch agent.launch.py dry_run:=false  # call the real pyramid API
  ros2 launch agent.launch.py with_llm:=false # skip llm_node (e.g. perception-glue smoke test)

Tuning the x,y stabilizer:
  ros2 launch agent.launch.py stabilize_method:=mean stabilize_window_s:=1.5
"""
from __future__ import annotations

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration

SCRIPTS = os.path.join(os.path.dirname(__file__), '..', 'scripts')


def _node(name: str, *params: str, condition=None) -> ExecuteProcess:
    cmd = ['python3', os.path.join(SCRIPTS, f'{name}.py')]
    if params:
        cmd += ['--ros-args', *params]
    return ExecuteProcess(cmd=cmd, name=name, output='screen', condition=condition)


def generate_launch_description() -> LaunchDescription:
    args = [
        DeclareLaunchArgument('dry_run', default_value='true'),
        DeclareLaunchArgument('with_llm', default_value='true'),
        DeclareLaunchArgument('user_command', default_value='3단 피라미드 쌓아줘'),
        DeclareLaunchArgument('stabilize_method', default_value='median'),
        DeclareLaunchArgument('stabilize_window_s', default_value='1.0'),
        DeclareLaunchArgument('stabilize_track_timeout_s', default_value='1.0'),
        DeclareLaunchArgument(
            'api_url',
            default_value='https://yarr-api-31.simplyimg.com/api/robot/skill/pyramid'),
        DeclareLaunchArgument('api_timeout_s', default_value='180.0'),
        DeclareLaunchArgument('model', default_value='qwen3.6:35b'),
        DeclareLaunchArgument('ollama_url', default_value='http://localhost:11434/api/chat'),
    ]

    dry_run = LaunchConfiguration('dry_run')
    with_llm = LaunchConfiguration('with_llm')

    nodes = [
        # aggregator: relay /vision/cups_on_table, /vision/stack -> /cups_on_table,
        # /stack for goal_state_publisher, and publish /user_command.
        _node('fake_aggregator_node',
              '-p', ['user_command:=', LaunchConfiguration('user_command')]),
        # x,y stabilizer: median/mean-filters raw /digital_twin/boxes and
        # republishes /digital_twin/boxes_filtered for plan_executor.
        _node('fake_digital_twin_node',
              '-p', ['method:=', LaunchConfiguration('stabilize_method')],
              '-p', ['window_s:=', LaunchConfiguration('stabilize_window_s')],
              '-p', ['track_timeout_s:=', LaunchConfiguration('stabilize_track_timeout_s')]),
        _node('goal_state_publisher_node'),
        _node('topic_logger_node'),
        # llm_node is optional so the perception-glue path can be smoke-tested
        # without an Ollama backend.
        _node('llm_node',
              '-p', ['model:=', LaunchConfiguration('model')],
              '-p', ['ollama_url:=', LaunchConfiguration('ollama_url')],
              condition=IfCondition(with_llm)),
        _node('plan_executor_node',
              '-p', ['api_url_pyramid:=', LaunchConfiguration('api_url')],
              '-p', ['api_timeout_s:=', LaunchConfiguration('api_timeout_s')],
              '-p', ['dry_run:=', dry_run]),
    ]
    return LaunchDescription([*args, *nodes])
