"""Client configuration — all defaults overridable via environment variables."""
from __future__ import annotations

import os
from pathlib import Path

SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL = os.getenv("LLM_MODEL", "gemma4:26b")
PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

# OnRobot RG2 max width ~110 mm; holding a cup is typically 60-80 mm.
# width_mm < this threshold → gripper is gripping (holding=True fed to builder).
GRIPPER_CLOSED_MM = float(os.getenv("GRIPPER_CLOSED_MM", "100.0"))
