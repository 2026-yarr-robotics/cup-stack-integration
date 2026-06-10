#!/usr/bin/env bash
# upright_cup_pose_node 실행 래퍼 (real hand-eye → /hand_eye/boxes).
# fake_hand_eye_node 의 real 대체. 실행 전 fake 를 꼭 꺼야 /hand_eye/boxes 충돌 안 남.
#
#   끄기:  pkill -f 'scripts/fake_hand_eye_node.py'
#   확인:  ros2 topic info /hand_eye/boxes   # Publisher=1, node=upright_cup_pose_node
set -e

export ROS_DOMAIN_ID=21
export ROS_LOCALHOST_ONLY=0
source /opt/ros/humble/setup.bash
source /home/ssu/cup-stack-integration/vision/ros2-depth-point-cloude/install/setup.bash

YOLO=/home/ssu/cup-stack-integration/vision/ros2-depth-point-cloude/vision/yolo/speedstack3class_yolo26s_seg_1280_epoch250_3class_lightaug_geom1_redp25_sm_a100_best.pt
CALIB=/home/ssu/cup-stack-integration/cup-stack-server/fallen-cup-recovery/dsr_practice/config/T_gripper2camera.npy

cd /home/ssu/cup-stack-integration/cup_stack_agent
exec python3 scripts/upright_cup_pose_node.py --ros-args \
  -p weights_path:="$YOLO" \
  -p image_topic:=/hand/hand/color/image_raw \
  -p depth_topic:=/hand/hand/aligned_depth_to_color/image_raw \
  -p camera_info_topic:=/hand/hand/color/camera_info \
  -p calib_file:="$CALIB" \
  -p calib_scale_mm_to_m:=true \
  -p device:=cuda \
  -p imgsz:=1280 \
  -p conf:=0.35 \
  -p base_frame:=base_link \
  -p target_class_name:=upright-cup
