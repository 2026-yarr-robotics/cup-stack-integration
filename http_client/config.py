"""Fixed experiment configuration for the test_v1.0 integration run."""
from __future__ import annotations

import os
from pathlib import Path

# ── 실험 고정값 ────────────────────────────────────────────────────────────
COMMAND = "3단 피라미드에서 1단만 쌓아줘"

# fake_aggregator_node 측정 좌표와 동일한 값
FAKE_XY: dict[str, tuple[float, float]] = {
    "L1_left":  (0.280, -0.15),
    "L1_mid":   (0.280,  0.00),
    "L1_right": (0.280,  0.15),
}

# ── 연결 설정 ──────────────────────────────────────────────────────────────
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL      = os.getenv("LLM_MODEL",  "gemma4:26b")
PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
DRY_RUN    = os.getenv("DRY_RUN", "1") == "1"

# OnRobot RG2 max ~110 mm; 컵 파지 시 약 60-80 mm
GRIPPER_CLOSED_MM = float(os.getenv("GRIPPER_CLOSED_MM", "100.0"))
