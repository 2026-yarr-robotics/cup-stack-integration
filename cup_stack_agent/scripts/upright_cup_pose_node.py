#!/usr/bin/env python3
"""
upright_cup_pose_node.py

Hand-eye 비전 노드 (cup-stack-integration v1.1).

이 노드는 hand-eye 카메라로 본 upright(똑바로 선) 컵들을 검출하고, **카메라 광학
좌표 → base_link 변환까지 직접 수행**해서 모든 컵을 `/hand_eye/boxes`
(visualization_msgs/MarkerArray, base_link frame) 로 발행한다. 즉 fake_hand_eye_node
의 실(real) 대체물이다. pick_node 는 이 토픽을 그대로 받아(좌표변환 없이) EE 최근접
컵을 골라 pyramid API 를 호출한다.

좌표 변환 (dsr_practice/stand_fallen_cup.py 검증식 재사용):
    T_base_ee  = _ee_matrix_from_tf()          # base_link<-link_6 (/tf lookup)
    T_ee_cam   = gripper2cam (npy, mm→m)
    T_base_cam = T_base_ee @ T_ee_cam
    p_base     = (T_base_cam @ [p_cam, 1])[:3] - base_offset

Output:
  /hand_eye/boxes : visualization_msgs/MarkerArray (base_link frame)
      - 매 발행마다 DELETEALL 1개로 스냅샷 초기화 후, 컵마다 2개:
        ns="box_top"    id=i  pose.position=(x,y,z)  ← base_link 좌표
        ns="box_labels" id=i  text="#i_c=<color>_upright-cup"
  /upright_cup/debug_image : Image (검출/선택 시각화)
  /fallen_cups : std_msgs/String JSON {"count": N}
      - 같은 YOLO 추론에서 fallen-cup 클래스 검출 개수. 매 프레임 발행 (0 포함 —
        구독자는 신선도(TTL)로 "fallen 없음"과 "노드 안 돎"을 구분).
      - 좌표/색 없음. world_state(cups_on_table/stack)에는 절대 불관여 — exo 와의
        이중 카운트 방지. GSP 가 decision 시점에만 payload fallen_count 로 게이트.

전제:
  - dsr 가 /tf 로 base_link<-link_6 (TF) 를 방송하고 있어야 FK 가능.
  - hand-eye 캘리브 파일(T_gripper2camera.npy) 이 calib_file 경로에 있어야 함.
"""

import json
import math
import time

import cv2
import numpy as np
import torch

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.time import Time
from tf2_ros import Buffer, TransformListener

from sensor_msgs.msg import Image, CameraInfo
from std_msgs.msg import String
from visualization_msgs.msg import Marker, MarkerArray

from ultralytics import YOLO


# cv_bridge 대체 (numpy 1.x↔2.x ABI 충돌 회피).
def imgmsg_to_cv2(msg, desired_encoding="passthrough"):
    """sensor_msgs/Image → cv2/numpy. desired_encoding은 bgr8 또는 passthrough."""
    h, w = msg.height, msg.width
    enc = msg.encoding

    if enc == "16UC1":
        arr = np.frombuffer(msg.data, dtype=np.uint16).reshape(h, w)
    elif enc == "32FC1":
        arr = np.frombuffer(msg.data, dtype=np.float32).reshape(h, w)
    elif enc == "mono8":
        arr = np.frombuffer(msg.data, dtype=np.uint8).reshape(h, w)
    elif enc in ("bgr8", "rgb8"):
        arr = np.frombuffer(msg.data, dtype=np.uint8).reshape(h, w, 3)
    elif enc in ("bgra8", "rgba8"):
        arr = np.frombuffer(msg.data, dtype=np.uint8).reshape(h, w, 4)
    else:
        raise ValueError(f"Unsupported encoding: {enc}")

    if desired_encoding == "passthrough" or desired_encoding == enc:
        return arr.copy()

    if desired_encoding == "bgr8":
        if enc == "rgb8":
            return arr[:, :, ::-1].copy()
        if enc == "bgra8":
            return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        if enc == "rgba8":
            return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
        if enc == "mono8":
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)

    raise ValueError(f"Cannot convert {enc} → {desired_encoding}")


def cv2_to_imgmsg(image, encoding="bgr8"):
    """cv2/numpy → sensor_msgs/Image. bgr8/rgb8/mono8 지원."""
    msg = Image()
    h, w = image.shape[:2]
    msg.height = h
    msg.width = w
    msg.encoding = encoding
    msg.is_bigendian = 0
    if encoding in ("bgr8", "rgb8"):
        msg.step = w * 3
    elif encoding == "mono8":
        msg.step = w
    else:
        raise ValueError(f"Unsupported encoding: {encoding}")
    msg.data = image.tobytes()
    return msg


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ["true", "1", "yes", "y"]
    return bool(value)


# link_6(EE) 프레임명. base_link<-link_6 FK 는 MoveItPy 대신 /tf 로 얻는다.
EE_LINK = "link_6"


def quat_to_matrix(x, y, z, w):
    """단위 quaternion(xyzw) -> 3x3 회전행렬."""
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n == 0.0:
        return np.eye(3)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ], dtype=float)


# HSV 기반 단순 색 분류. mask 영역 평균 BGR → 알려진 컵 색 토큰.
# plan_executor/pick_node 의 _KNOWN_COLORS 와 어휘 일치.
def classify_color_bgr(mean_bgr):
    b, g, r = [float(c) for c in mean_bgr]
    px = np.uint8([[[b, g, r]]])
    hsv = cv2.cvtColor(px, cv2.COLOR_BGR2HSV)[0, 0]
    h, s, v = int(hsv[0]), int(hsv[1]), int(hsv[2])
    if v < 50:
        return "black"
    if s < 40:
        return "white" if v > 180 else "gray"
    # OpenCV Hue: 0-179
    if h < 10 or h >= 170:
        return "red"
    if h < 25:
        return "orange"
    if h < 35:
        return "yellow"
    if h < 85:
        return "green"
    if h < 130:
        return "blue"
    if h < 160:
        return "purple"
    return "red"


class UprightCupPoseNode(Node):
    """hand-eye 카메라 → base_link 변환까지 떠안고 /hand_eye/boxes 를 내는 비전 노드.

    fake_hand_eye_node 와 동일한 토픽/메시지 형식(base_link MarkerArray, box_top +
    box_labels)을 발행하므로 pick_node 입장에선 fake/real 구분 없이 동일하게 동작.
    """

    def __init__(self):
        super().__init__("upright_cup_pose_node")

        # ── Parameters ───────────────────────────────────────
        self.declare_parameter("weights_path", "")
        self.declare_parameter("image_topic", "/camera/camera/color/image_raw")
        self.declare_parameter(
            "depth_topic", "/camera/camera/aligned_depth_to_color/image_raw"
        )
        self.declare_parameter(
            "camera_info_topic", "/camera/camera/color/camera_info"
        )

        self.declare_parameter("boxes_topic", "/hand_eye/boxes")
        self.declare_parameter("debug_image_topic", "/upright_cup/debug_image")

        self.declare_parameter("imgsz", 640)
        self.declare_parameter("conf", 0.25)
        self.declare_parameter("iou", 0.45)
        self.declare_parameter("device", "cpu")
        self.declare_parameter("half", False)

        self.declare_parameter("target_class_name", "upright-cup")
        self.declare_parameter("min_mask_area", 300.0)

        # ── fallen-cup count (같은 추론, 별도 채널) ──────────
        # 같은 YOLO 프레임에서 fallen-cup 클래스 검출 개수만 세서
        # /fallen_cups 에 {"count": N} 으로 매 프레임(0 포함) 발행한다.
        # goal_state_publisher 가 decision 시점(집을 upright 이 하나도 없을
        # 때)에만 payload 의 fallen_count 로 게이트해 싣는다. /hand_eye/boxes
        # (pick 좌표 경로)와 world_state 에는 절대 관여하지 않는다 — count 만.
        self.declare_parameter("fallen_class_name", "fallen-cup")
        self.declare_parameter("fallen_count_topic", "/fallen_cups")

        # -- pick point method ----------------------------
        # A standing cup seen from above shows its rim circle; if the seg
        # mask also catches the side wall and elongates, the moments centroid
        # drifts off the rim center. We re-detect the "circle part" instead.
        #   top_hole  : top-face donut hole (dark central hole) center -- precise
        #               pick into the cup mouth / through-hole
        #   inscribed : distance-transform max = largest inscribed circle (robust default)
        #   hough     : HoughCircles rim detection on the image
        #   centroid  : original moments centroid (unchanged)
        self.declare_parameter("pick_point_method", "inscribed")
        # hough tuning (radius ratio is relative to contour bbox short side).
        self.declare_parameter("hough_dp", 1.2)
        self.declare_parameter("hough_param1", 100.0)
        self.declare_parameter("hough_param2", 25.0)
        self.declare_parameter("hough_min_radius_ratio", 0.25)
        self.declare_parameter("hough_max_radius_ratio", 0.75)
        # top_hole tuning. brightness threshold uses Otsu (lighting-adaptive)
        # by default; dark_percentile is a safety cap when Otsu misbehaves.
        self.declare_parameter("top_hole_face_ratio", 0.95)
        self.declare_parameter("top_hole_min_circularity", 0.45)
        self.declare_parameter("top_hole_dark_percentile", 35.0)
        self.declare_parameter("top_hole_min_area_frac", 0.01)
        self.declare_parameter("top_hole_max_area_frac", 0.7)

        # ── 좌표 변환 (camera → base_link) ──────────────────
        self.declare_parameter("base_frame", "base_link")
        # 캘리브 파일. 비우면 pick_node share 의 T_gripper2camera.npy 사용.
        self.declare_parameter("calib_file", "")
        self.declare_parameter("calib_scale_mm_to_m", True)
        self.declare_parameter("base_offset_x", 0.0)
        self.declare_parameter("base_offset_y", 0.0)
        self.declare_parameter("base_offset_z", 0.080)

        # 색 분류: 비우면 mask 평균색 자동 분류. 값 지정 시 모든 컵에 고정 색.
        self.declare_parameter("cup_color", "")

        self.weights_path = str(self.get_parameter("weights_path").value)
        self.image_topic = str(self.get_parameter("image_topic").value)
        self.depth_topic = str(self.get_parameter("depth_topic").value)
        self.camera_info_topic = str(self.get_parameter("camera_info_topic").value)

        self.boxes_topic = str(self.get_parameter("boxes_topic").value)
        self.debug_image_topic = str(self.get_parameter("debug_image_topic").value)

        self.imgsz = int(self.get_parameter("imgsz").value)
        self.conf = float(self.get_parameter("conf").value)
        self.iou = float(self.get_parameter("iou").value)
        self.device = str(self.get_parameter("device").value)
        self.half = as_bool(self.get_parameter("half").value)

        self.target_class_name = str(self.get_parameter("target_class_name").value)
        self.min_mask_area = float(self.get_parameter("min_mask_area").value)
        self.fallen_class_name = str(
            self.get_parameter("fallen_class_name").value)
        self.fallen_count_topic = str(
            self.get_parameter("fallen_count_topic").value)
        self.pick_point_method = str(
            self.get_parameter("pick_point_method").value).strip().lower()
        if self.pick_point_method not in (
                "top_hole", "inscribed", "hough", "centroid"):
            self.get_logger().warn(
                f"unknown pick_point_method '{self.pick_point_method}', "
                f"falling back to 'inscribed'")
            self.pick_point_method = "inscribed"
        self.hough_dp = float(self.get_parameter("hough_dp").value)
        self.hough_param1 = float(self.get_parameter("hough_param1").value)
        self.hough_param2 = float(self.get_parameter("hough_param2").value)
        self.hough_min_radius_ratio = float(
            self.get_parameter("hough_min_radius_ratio").value)
        self.hough_max_radius_ratio = float(
            self.get_parameter("hough_max_radius_ratio").value)
        self.top_hole_face_ratio = float(
            self.get_parameter("top_hole_face_ratio").value)
        self.top_hole_min_circularity = float(
            self.get_parameter("top_hole_min_circularity").value)
        self.top_hole_dark_percentile = float(
            self.get_parameter("top_hole_dark_percentile").value)
        self.top_hole_min_area_frac = float(
            self.get_parameter("top_hole_min_area_frac").value)
        self.top_hole_max_area_frac = float(
            self.get_parameter("top_hole_max_area_frac").value)

        self.base_frame = str(self.get_parameter("base_frame").value)
        calib_file = str(self.get_parameter("calib_file").value)
        self.calib_scale = as_bool(
            self.get_parameter("calib_scale_mm_to_m").value)
        self.base_offset = np.array([
            float(self.get_parameter("base_offset_x").value),
            float(self.get_parameter("base_offset_y").value),
            float(self.get_parameter("base_offset_z").value),
        ], dtype=float)
        self.cup_color = str(self.get_parameter("cup_color").value).strip().lower()

        if self.weights_path == "":
            raise RuntimeError("weights_path is empty.")

        if self.device != "cpu" and not torch.cuda.is_available():
            self.get_logger().warn("CUDA is not available. Falling back to CPU.")
            self.device = "cpu"
            self.half = False
        if self.device == "cpu":
            self.half = False

        # ── 캘리브 로드 (T_ee_cam) ──────────────────────────
        if calib_file == "":
            from ament_index_python.packages import get_package_share_directory
            from pathlib import Path
            calib_file = str(
                Path(get_package_share_directory("pick_node"))
                / "config" / "T_gripper2camera.npy"
            )
        self.gripper2cam = np.load(calib_file).astype(float)
        if self.calib_scale:
            self.gripper2cam[:3, 3] /= 1000.0  # mm → m
        self.get_logger().info(f"Hand-Eye 캘리브 로드: {calib_file}")

        # ── link_6 FK: /tf lookup (MoveItPy 대신) ────────────
        # 두 번째 MoveItPy planning-scene-monitor 충돌을 피하려 TF 로 base_link<-
        # link_6 FK 를 읽는다. dsr 가 이 변환을 /tf 로 방송하므로 read-only FK 로 충분.
        self.ee_frame = EE_LINK
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self, spin_thread=True)
        self.get_logger().info(
            f"link_6 FK source: /tf ({self.base_frame} <- {self.ee_frame})")

        # ── 카메라 내부 파라미터 / depth ────────────────────
        self.last_depth_m = None
        self.fx = self.fy = self.cx = self.cy = None

        self.get_logger().info(f"Loading YOLO model: {self.weights_path}")
        self.model = YOLO(self.weights_path)
        try:
            self.model.fuse()
        except Exception as e:
            self.get_logger().warn(f"model.fuse() skipped: {e}")

        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
        )

        self.image_sub = self.create_subscription(
            Image, self.image_topic, self.image_callback, qos)
        self.depth_sub = self.create_subscription(
            Image, self.depth_topic, self.depth_callback, qos)
        self.info_sub = self.create_subscription(
            CameraInfo, self.camera_info_topic, self.camera_info_callback, qos)

        self.boxes_pub = self.create_publisher(
            MarkerArray, self.boxes_topic, 10)
        self.fallen_pub = self.create_publisher(
            String, self.fallen_count_topic, 10)
        self.debug_pub = self.create_publisher(
            Image, self.debug_image_topic, 10)

        self.get_logger().info("upright_cup_pose_node started.")
        self.get_logger().info(f"  image_topic : {self.image_topic}")
        self.get_logger().info(f"  boxes_topic : {self.boxes_topic} ({self.base_frame})")
        self.get_logger().info(f"  target_class: '{self.target_class_name}'")
        self.get_logger().info(
            f"  fallen count: '{self.fallen_class_name}' -> "
            f"{self.fallen_count_topic}")
        self.get_logger().info(f"  model classes: {getattr(self.model, 'names', None)}")

    # ── Depth / camera info ──────────────────────────────────
    def depth_callback(self, msg: Image):
        try:
            depth = imgmsg_to_cv2(msg, desired_encoding="passthrough")
        except Exception as e:
            self.get_logger().warn(f"depth conversion failed: {e}")
            return
        if msg.encoding == "16UC1":
            self.last_depth_m = depth.astype(np.float32) * 0.001
        elif msg.encoding == "32FC1":
            self.last_depth_m = depth.astype(np.float32)
        else:
            self.get_logger().warn(f"Unsupported depth encoding: {msg.encoding}")

    def camera_info_callback(self, msg: CameraInfo):
        self.fx = float(msg.k[0])
        self.fy = float(msg.k[4])
        self.cx = float(msg.k[2])
        self.cy = float(msg.k[5])

    def get_depth_at_pixel(self, u, v, window=7):
        if self.last_depth_m is None:
            return None
        h, w = self.last_depth_m.shape[:2]
        u = int(round(u))
        v = int(round(v))
        if u < 0 or u >= w or v < 0 or v >= h:
            return None
        r = window // 2
        x0, x1 = max(0, u - r), min(w, u + r + 1)
        y0, y1 = max(0, v - r), min(h, v + r + 1)
        patch = self.last_depth_m[y0:y1, x0:x1]
        valid = patch[np.isfinite(patch)]
        valid = valid[valid > 0.05]
        if valid.size == 0:
            return None
        return float(np.median(valid))

    def deproject_pixel_to_3d(self, u, v, z):
        if None in (self.fx, self.fy, self.cx, self.cy):
            return None
        x = (u - self.cx) * z / self.fx
        y = (v - self.cy) * z / self.fy
        return x, y, z

    # ── YOLO mask extraction ─────────────────────────────────
    def extract_detections(self, result, frame_bgr, image_h, image_w):
        detections = []
        if result.masks is None or result.masks.data is None:
            return detections
        masks = result.masks.data.detach().cpu().numpy()
        boxes = result.boxes
        confs = clss = None
        if boxes is not None:
            if boxes.conf is not None:
                confs = boxes.conf.detach().cpu().numpy()
            if boxes.cls is not None:
                clss = boxes.cls.detach().cpu().numpy()

        for i, mask in enumerate(masks):
            if mask.shape[:2] != (image_h, image_w):
                mask = cv2.resize(
                    mask, (image_w, image_h), interpolation=cv2.INTER_NEAREST)
            binary = (mask > 0.5).astype(np.uint8) * 255
            contours, _ = cv2.findContours(
                binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if len(contours) == 0:
                continue
            contour = max(contours, key=cv2.contourArea)
            area = float(cv2.contourArea(contour))
            if area < self.min_mask_area:
                continue
            M = cv2.moments(contour)
            if abs(M["m00"]) < 1e-6:
                continue
            cx = float(M["m10"] / M["m00"])
            cy = float(M["m01"] / M["m00"])
            centroid = np.array([cx, cy], dtype=np.float32)

            # re-detect the rim "circle" / top-hole center from the mask.
            center, radius = self.compute_pick_point(
                frame_bgr, binary, contour, centroid)

            conf = float(confs[i]) if confs is not None and i < len(confs) else 1.0
            cls_id = int(clss[i]) if clss is not None and i < len(clss) else -1
            detections.append({
                "mask": binary,
                "contour": contour,
                "area": area,
                "center": center,        # pick point (circle / hole center)
                "centroid": centroid,    # original moments centroid (debug compare)
                "pick_radius": radius,   # detected circle radius (px) or None
                "conf": conf,
                "cls_id": cls_id,
                "cls_name": self._class_id_to_name(cls_id),
            })
        return detections

    # -- pick point: "circle" / top-hole center of the mask ----
    def compute_pick_point(self, frame_bgr, binary, contour, centroid):
        """Return pick point (u,v) and circle radius (px, None if absent) by
        the selected method. Every method falls back to the moments centroid.
        """
        method = self.pick_point_method
        if method == "centroid":
            return centroid, None
        if method == "top_hole":
            res = self._top_hole(frame_bgr, binary, centroid)
            if res is not None:
                return res
            # hole detection failed -> fall back to inscribed circle
            return self._inscribed_circle(binary, centroid)
        if method == "hough":
            res = self._hough_circle(frame_bgr, contour, centroid)
            if res is not None:
                return res
            # hough failed -> fall back to inscribed circle
            return self._inscribed_circle(binary, centroid)
        # default: inscribed
        return self._inscribed_circle(binary, centroid)

    def _inscribed_circle(self, binary, centroid):
        """distance-transform max = center of the largest inscribed circle;
        ignores the elongated tail (side wall) and locks onto the round top."""
        dist = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
        _, max_val, _, max_loc = cv2.minMaxLoc(dist)
        if max_val <= 0:
            return centroid, None
        center = np.array([float(max_loc[0]), float(max_loc[1])], dtype=np.float32)
        return center, float(max_val)

    def _top_hole(self, frame_bgr, binary, centroid):
        """Detect the center of the top-face donut hole (dark central hole).
        Returns None on failure (caller falls back to inscribed).

        Lighting-robust design: (1) restrict the search to the TOP FACE
        (inscribed-circle disk) so side-wall shadows cannot be mistaken for
        the hole; (2) auto-threshold brightness with Otsu (relative rim/hole
        split, lighting-adaptive), capped by dark_percentile; (3) score hole
        candidates by circularity + centrality + area; (4) use the moments
        centroid (not minEnclosingCircle) for the center."""
        # 1) top-face region = inscribed-circle disk
        dist = cv2.distanceTransform(binary, cv2.DIST_L2, 5)
        _, insc_r, _, insc_loc = cv2.minMaxLoc(dist)
        if insc_r < 4:
            return None
        face_center = np.array([float(insc_loc[0]), float(insc_loc[1])], np.float32)
        face_r = max(3.0, insc_r * self.top_hole_face_ratio)
        face = np.zeros_like(binary)
        cv2.circle(face, (int(face_center[0]), int(face_center[1])),
                   int(face_r), 255, -1)
        face = cv2.bitwise_and(face, binary)
        face_area = float(np.count_nonzero(face))
        if face_area < 30:
            return None

        # 2) brightness (HSV V) of top-face pixels -> Otsu (lighting-adaptive)
        v = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)[:, :, 2]
        vals = v[face > 0]
        if vals.size < 30:
            return None
        otsu_thr, _ = cv2.threshold(
            vals.reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # guard if Otsu runs too high and marks most of the face as "dark".
        cap = float(np.percentile(vals, self.top_hole_dark_percentile))
        thr = min(float(otsu_thr), cap)

        dark = ((v <= thr) & (face > 0)).astype(np.uint8) * 255
        dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

        cnts, _ = cv2.findContours(
            dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return None

        # 3) score candidate holes: circularity + centrality + area
        min_a = self.top_hole_min_area_frac * face_area
        max_a = self.top_hole_max_area_frac * face_area
        best = None
        for c in cnts:
            a = float(cv2.contourArea(c))
            if a < max(min_a, 30.0) or a > max_a:
                continue
            (cu, cv_), cr = cv2.minEnclosingCircle(c)
            if cr < 2:
                continue
            circularity = a / (math.pi * cr * cr)
            if circularity < self.top_hole_min_circularity:
                continue
            Mh = cv2.moments(c)
            if abs(Mh["m00"]) < 1e-6:
                continue
            hx = Mh["m10"] / Mh["m00"]
            hy = Mh["m01"] / Mh["m00"]
            dist_c = math.hypot(hx - face_center[0], hy - face_center[1])
            if dist_c > face_r:                 # reject centers outside the top face
                continue
            # higher score = rounder, more central, larger.
            score = circularity - 0.004 * dist_c + 0.0006 * a
            if best is None or score > best[0]:
                best = (score, hx, hy, cr)
        if best is None:
            return None
        center = np.array([best[1], best[2]], dtype=np.float32)
        return center, float(best[3])

    def _hough_circle(self, frame_bgr, contour, centroid):
        """Detect the rim circle directly with HoughCircles inside the contour
        bbox ROI. Returns None on failure (caller falls back to inscribed)."""
        x, y, w, h = cv2.boundingRect(contour)
        if min(w, h) < 4:
            return None
        pad = int(0.15 * max(w, h))
        H, W = frame_bgr.shape[:2]
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
        roi = frame_bgr[y0:y1, x0:x1]
        if roi.size == 0:
            return None
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.medianBlur(gray, 5)

        half_short = max(2.0, min(w, h) / 2.0)
        min_r = max(1, int(self.hough_min_radius_ratio * half_short))
        max_r = max(min_r + 1, int(self.hough_max_radius_ratio * half_short))
        circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=self.hough_dp,
            minDist=half_short,
            param1=self.hough_param1, param2=self.hough_param2,
            minRadius=min_r, maxRadius=max_r)
        if circles is None:
            return None
        circles = np.asarray(circles, dtype=np.float32).reshape(-1, 3)
        # among circles whose center lies inside the contour, take the largest.
        best = None
        for cu, cv_, cr in circles:
            gu, gv = float(cu) + x0, float(cv_) + y0
            if cv2.pointPolygonTest(contour, (gu, gv), False) < 0:
                continue
            if best is None or cr > best[2]:
                best = (gu, gv, float(cr))
        if best is None:
            return None
        return np.array([best[0], best[1]], dtype=np.float32), best[2]

    def _class_id_to_name(self, cls_id):
        if cls_id is None or cls_id < 0:
            return None
        names = getattr(self.model, "names", None)
        if names is None:
            return None
        if isinstance(names, dict):
            return names.get(cls_id)
        try:
            return names[cls_id]
        except (IndexError, KeyError, TypeError):
            return None

    def filter_target_detections(self, detections):
        if not self.target_class_name:
            return detections
        return [d for d in detections if d.get("cls_name") == self.target_class_name]

    # ── 색 분류 ───────────────────────────────────────────────
    def detect_color(self, frame_bgr, det):
        if self.cup_color:
            return self.cup_color
        mask = det["mask"] > 0
        if not np.any(mask):
            return "unknown"
        mean_bgr = frame_bgr[mask].mean(axis=0)
        return classify_color_bgr(mean_bgr)

    # ── 좌표 변환 (camera optical → base_link) ────────────────
    def _ee_matrix_from_tf(self):
        """base_frame <- link_6 4x4 (TF 최신). 미수신/실패 시 None."""
        try:
            tf = self.tf_buffer.lookup_transform(
                self.base_frame, self.ee_frame, Time())
        except Exception as e:
            self.get_logger().warn(
                f"link_6 TF lookup 실패(FK 불가): {e}",
                throttle_duration_sec=2.0)
            return None
        t = tf.transform.translation
        q = tf.transform.rotation
        T = np.eye(4)
        T[:3, :3] = quat_to_matrix(q.x, q.y, q.z, q.w)
        T[:3, 3] = [t.x, t.y, t.z]
        return T

    def cam_to_base(self, p_cam):
        """p_cam (camera optical frame, m) → base_link (m). 실패 시 None."""
        T_base_ee = self._ee_matrix_from_tf()
        if T_base_ee is None:
            return None
        T_base_cam = T_base_ee @ self.gripper2cam
        p_base = (T_base_cam @ np.append(np.asarray(p_cam, dtype=float), 1.0))[:3]
        p_base = p_base - self.base_offset
        return p_base

    # ── Main callback ─────────────────────────────────────────
    def image_callback(self, msg: Image):
        try:
            frame_bgr = imgmsg_to_cv2(msg, desired_encoding="bgr8")
        except Exception as e:
            self.get_logger().error(f"image conversion failed: {e}")
            return

        h, w = frame_bgr.shape[:2]
        debug = frame_bgr.copy()
        start = time.time()

        try:
            with torch.inference_mode():
                results = self.model.predict(
                    source=frame_bgr,
                    imgsz=self.imgsz,
                    conf=self.conf,
                    iou=self.iou,
                    device=self.device,
                    half=self.half,
                    verbose=False,
                    retina_masks=True,
                )
        except Exception as e:
            self.get_logger().error(f"YOLO inference failed: {e}")
            return

        detections = self.extract_detections(results[0], frame_bgr, h, w)
        targets = self.filter_target_detections(detections)

        # fallen-cup 개수 — 같은 추론 결과에서 세서 매 프레임(0 포함) 발행.
        # 0 도 발행해야 구독자가 "fallen 없음"과 "노드 안 돎"을 신선도(TTL)로
        # 구분한다. 좌표/색은 싣지 않는다 (count 만 — world 불관여).
        fallen_n = sum(
            1 for d in detections
            if d.get("cls_name") == self.fallen_class_name)
        self.fallen_pub.publish(
            String(data=json.dumps({"count": int(fallen_n)})))

        cups = []  # [{"xy_base":(x,y), "z_base":z, "color":str, "center":(u,v)}]
        for det in targets:
            u, v = det["center"]
            z = self.get_depth_at_pixel(u, v, window=7)
            if z is None:
                continue
            p_cam = self.deproject_pixel_to_3d(u, v, z)
            if p_cam is None:
                continue
            p_base = self.cam_to_base(p_cam)
            if p_base is None:
                continue
            color = self.detect_color(frame_bgr, det)
            cups.append({
                "xy_base": (float(p_base[0]), float(p_base[1])),
                "z_base": float(p_base[2]),
                "color": color,
                "center": (float(u), float(v)),
            })

        self.publish_boxes(cups)

        # ── debug 시각화 ──
        for det in targets:
            cv2.drawContours(debug, [det["contour"]], -1, (0, 200, 255), 1)
            # detected circle (inscribed/hough/top_hole) -- green outline
            if det.get("pick_radius"):
                c = det["center"]
                cv2.circle(debug, (int(c[0]), int(c[1])),
                           int(det["pick_radius"]), (0, 255, 0), 2)
            # original centroid (gray) vs final pick point (red)
            ctr = det.get("centroid")
            if ctr is not None:
                cv2.circle(debug, (int(ctr[0]), int(ctr[1])), 3, (160, 160, 160), -1)
            c = det["center"]
            cv2.circle(debug, (int(c[0]), int(c[1])), 4, (0, 0, 255), -1)
        cv2.putText(
            debug,
            f"upright cups={len(targets)} published={len(cups)} "
            f"fallen={fallen_n} pick={self.pick_point_method}",
            (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        self.publish_debug(debug, msg.header)

        elapsed = (time.time() - start) * 1000.0
        self.get_logger().info(
            f"upright cups={len(targets)} published(base)={len(cups)} "
            f"fallen={fallen_n} time={elapsed:.1f} ms")

    # ── Publish ───────────────────────────────────────────────
    def publish_boxes(self, cups):
        """모든 컵을 base_link MarkerArray 로 발행 (fake_hand_eye 형식)."""
        markers = MarkerArray()

        # 스냅샷 초기화: 구독자(_boxes dict)가 이전 프레임 잔재를 안 들고 있게.
        clear = Marker()
        clear.action = Marker.DELETEALL
        clear.ns = "box_top"
        markers.markers.append(clear)

        now = self.get_clock().now().to_msg()
        for i, cup in enumerate(cups):
            x, y = cup["xy_base"]
            z = cup["z_base"]
            color = cup["color"]

            top = Marker()
            top.header.frame_id = self.base_frame
            top.header.stamp = now
            top.ns = "box_top"
            top.id = i
            top.action = Marker.ADD
            top.type = Marker.SPHERE
            top.pose.position.x = x
            top.pose.position.y = y
            top.pose.position.z = z
            top.pose.orientation.w = 1.0
            markers.markers.append(top)

            label = Marker()
            label.header.frame_id = self.base_frame
            label.header.stamp = now
            label.ns = "box_labels"
            label.id = i
            label.action = Marker.ADD
            label.type = Marker.TEXT_VIEW_FACING
            label.pose.position.x = x
            label.pose.position.y = y
            label.pose.position.z = z
            label.pose.orientation.w = 1.0
            label.text = f"#{i}_c={color}_upright-cup"
            markers.markers.append(label)

        self.boxes_pub.publish(markers)

    def publish_debug(self, image_bgr, header):
        try:
            out = cv2_to_imgmsg(image_bgr, encoding="bgr8")
            out.header = header
            self.debug_pub.publish(out)
        except Exception as e:
            self.get_logger().warn(f"debug image publish failed: {e}")


def main(args=None):
    rclpy.init(args=args)
    node = UprightCupPoseNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == "__main__":
    main()
