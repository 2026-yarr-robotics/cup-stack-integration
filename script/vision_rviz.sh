#!/usr/bin/env bash
# vision_rviz.sh — standalone RViz viewer for the VISION / perception pipeline.
#
# Attaches RViz2 to the ALREADY-RUNNING vision topics so you can visually
# inspect cup detection, 3D boxes, the digital-twin point cloud, debug images,
# and the /stack slot verifier — independently of full system bring-up.
#
# This script is a VIEWER ONLY. It does NOT start cameras or detection nodes —
# bring those up with server/start.sh (or the live system)
# first, then run this to watch their output. (Set LAUNCH_PIPELINE=true to also
# launch the detection pipeline from here; off by default — see below.)
#
# Topics produced by the running pipeline (for reference):
#   /exo/exo/*            RealSense exo (eye-to-hand) color/depth
#   /digital_twin/boxes   3D object boxes (depth_digital_twin)
#   /vision/cups_on_table cups detected on the table
#   /vision/stack         judged stack slots (cup_stacking_verify)
#   /stack_track_ids      track ids backing /vision/stack
#
# Usage:
#   ./vision_rviz.sh                 # detection + 3D boxes view (digital_twin.rviz)
#   VIEW=verify ./vision_rviz.sh     # stack-slot verifier view (cup_verify.rviz)
#   VIEW=fusion ./vision_rviz.sh     # depth fusion view (fusion.rviz)
#   ROS_DOMAIN_ID=21 ./vision_rviz.sh    # override domain (default 21, matches start.sh)
#   LAUNCH_PIPELINE=true ./vision_rviz.sh # ALSO launch detection (normally start.sh's job)

set -e

# Resolve our real path (robust to being run via a symlink), then derive the
# integration repo root that holds the canonical vision/ submodules.
SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
ROOT_DIR="$SCRIPT_DIR"

ROS_SETUP="/opt/ros/humble/setup.bash"

# Keep all ROS nodes on one domain so this viewer sees the running topics.
# Mirror start.sh's default of 21; overridable.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-21}"

# Which RViz config to open.
VIEW="${VIEW:-digital_twin}"

# ── sanity ────────────────────────────────────────────────
if [[ ! -f "$ROS_SETUP" ]]; then
    echo "[ERROR] ROS 2 Humble not found at $ROS_SETUP" >&2
    exit 1
fi

# Canonical vision workspaces (vision/<pkg> — NOT the yarr-robust-speed-stack
# duplicates). Sourcing these resolves the custom message types and the rviz
# configs shipped in each package's share/.
DEPTH_DT_SETUP="$ROOT_DIR/ros2-depth-point-cloude/install/setup.bash"
VISION_NODE_SETUP="$ROOT_DIR/vision-node/install/setup.bash"
RECODE_SETUP="$ROOT_DIR/ros2-recode-sequence/install/setup.bash"

# shellcheck disable=SC1090
source "$ROS_SETUP"
for ws in "$RECODE_SETUP" "$DEPTH_DT_SETUP" "$VISION_NODE_SETUP"; do
    if [[ -f "$ws" ]]; then
        # shellcheck disable=SC1090
        source "$ws"
    else
        echo "[WARN] vision workspace not built (missing $ws). Run start.sh once to colcon build." >&2
    fi
done

# Locate the .rviz config shipped by the vision packages.
DT_SHARE="$ROOT_DIR/ros2-depth-point-cloude/install/depth_digital_twin/share/depth_digital_twin/rviz"
VERIFY_SHARE="$ROOT_DIR/vision-node/install/cup_stacking_verify/share/cup_stacking_verify/rviz"
case "$VIEW" in
    digital_twin|boxes|detect) RVIZ_CFG="$DT_SHARE/digital_twin.rviz" ;;
    fusion)                    RVIZ_CFG="$DT_SHARE/fusion.rviz" ;;
    verify|stack|slots)        RVIZ_CFG="$VERIFY_SHARE/cup_verify.rviz" ;;
    *)
        echo "[ERROR] unknown VIEW='$VIEW' (use digital_twin | fusion | verify)" >&2
        exit 1
        ;;
esac

# Optional: also launch the detection pipeline (normally start.sh owns this).
# Off by default so this stays a pure viewer that attaches to running topics.
if [[ "${LAUNCH_PIPELINE:-false}" == "true" ]]; then
    echo "[INFO] LAUNCH_PIPELINE=true → launching depth_digital_twin (camera_ns:=exo) in background..."
    ros2 launch depth_digital_twin digital_twin.launch.py camera_ns:=exo rviz:=false &
fi

echo "[INFO] ROS_DOMAIN_ID=$ROS_DOMAIN_ID  VIEW=$VIEW"
if [[ -f "$RVIZ_CFG" ]]; then
    echo "[INFO] rviz2 -d $RVIZ_CFG"
    exec rviz2 -d "$RVIZ_CFG"
else
    echo "[WARN] rviz config not found ($RVIZ_CFG); launching plain rviz2." >&2
    echo "[WARN] Build the vision workspaces (start.sh / colcon build) to get the shipped config." >&2
    exec rviz2
fi
