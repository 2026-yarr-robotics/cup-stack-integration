"""HTTP helpers: fetch robot_state from server, execute one pyramid step.

execute_step() is intentionally blocking (timeout=None by default) so the
caller naturally waits for the robot motion to finish before sending the
next request.  The server has no pyramid queue — concurrent calls would
both hit skill_api_node simultaneously — so the client must stay sequential.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from plan_executor_node import build_pyramid_body, llm_to_api_slot  # noqa: E402

import config  # noqa: E402

_LLM_SLOTS: frozenset[str] = frozenset(
    {"L1_left", "L1_mid", "L1_right", "L2_left", "L2_right", "L3_top"}
)


def parse_fake_xy(json_text: str) -> dict[str, tuple[float, float]]:
    """Parse --fake-xy JSON string → slot → (x, y).

    Accepts: '{"L1_left":[0.28,-0.15], "L1_mid":[0.28,0.0], ...}'
    Validates that every key is a known LLM slot and every value is [x, y].
    """
    if not json_text.strip():
        return {}
    raw = json.loads(json_text)
    if not isinstance(raw, dict):
        raise ValueError("--fake-xy must be a JSON object")
    result: dict[str, tuple[float, float]] = {}
    for slot, xy in raw.items():
        if slot not in _LLM_SLOTS:
            raise ValueError(f"unknown LLM slot {slot!r}")
        if (
            not isinstance(xy, (list, tuple))
            or len(xy) != 2
            or not all(isinstance(v, (int, float)) for v in xy)
        ):
            raise ValueError(f"fake xy for {slot!r} must be [x, y]")
        result[slot] = (float(xy[0]), float(xy[1]))
    return result


def fetch_robot_state(server_url: str, timeout: float = 5.0) -> dict[str, Any]:
    """GET /api/robot/status → robot_state dict for GoalStateBuilder.

    Mapping:
      gripper.width_mm < GRIPPER_CLOSED_MM  →  holding=True
        (GoalStateBuilder fills the color from last successful pick)
      width_mm is None or >= threshold      →  holding=None (empty)

    Falls back to an empty gripper on any network/parse error so the
    LLM pipeline can still run when the robot server is not reachable.
    """
    url = f"{server_url}/api/robot/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"[WARN] fetch_robot_state failed ({exc}); using empty gripper")
        return {"gripper": {"holding": None, "force_n": 0.0}}

    width_mm = (data.get("gripper") or {}).get("width_mm")
    holding: bool | None = (
        True if (width_mm is not None and width_mm < config.GRIPPER_CLOSED_MM) else None
    )
    return {"gripper": {"holding": holding, "force_n": 0.0}}


def execute_step(
    step: dict[str, Any],
    fake_xy: dict[str, tuple[float, float]],
    api_url: str,
    timeout: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Execute one pyramid plan step and return an action_result dict.

    timeout=None → no socket timeout; the call blocks until skill_api_node
    returns, which is after the robot motion completes.  This is intentional:
    the caller must not send the next step until this one finishes.
    """
    action_result: dict[str, Any] = {
        "step": step.get("step"),
        "action": step.get("action"),
        "color": step.get("color"),
        "target_slot": step.get("target_slot"),
        "result": "fail",
        "failure_reason": None,
    }

    if step.get("action") != "pyramid":
        action_result["failure_reason"] = f"unknown action {step.get('action')!r}"
        return action_result

    color = step.get("color")
    llm_slot = step.get("target_slot")
    if not color:
        action_result["failure_reason"] = "pyramid step missing color"
        return action_result
    api_slot = llm_to_api_slot(llm_slot)
    if api_slot is None:
        action_result["failure_reason"] = f"unknown slot {llm_slot!r}"
        return action_result
    xy = fake_xy.get(llm_slot)
    if xy is None:
        action_result["failure_reason"] = f"no fake xy configured for slot {llm_slot!r}"
        return action_result
    body = build_pyramid_body(xy[0], xy[1], api_slot)

    if dry_run:
        print(f"  [dry-run] POST {api_url} {body}")
        action_result["result"] = "success"
        return action_result

    print(f"  [exec] POST {api_url} {body}")
    ok, detail = _http_post_json(api_url, body, timeout)
    if ok:
        action_result["result"] = "success"
    else:
        action_result["failure_reason"] = detail
    return action_result


def _http_post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float | None,
) -> tuple[bool, str]:
    """Returns (ok, detail). detail is empty string on success."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode()[:200]}"
    except urllib.error.URLError as exc:
        return False, f"network: {exc.reason}"
    except Exception as exc:
        return False, f"transport: {exc}"

    try:
        parsed = json.loads(body)
    except ValueError:
        return False, f"non-JSON response: {body[:200]}"

    if isinstance(parsed, dict) and parsed.get("success") is False:
        detail = str(parsed.get("detail") or parsed.get("message") or "success=false")
        return False, detail
    return True, ""
