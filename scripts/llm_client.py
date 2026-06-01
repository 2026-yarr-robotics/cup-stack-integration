"""Pure (ROS-free) LLM client for the cup-stacking planner/decider.

Wraps an Ollama chat call plus prompt loading, JSON extraction, and the
semantic post-validation from `docs/pipeline_io_spec_comprehensive.md` §8.
Kept free of `rclpy` so it can be exercised offline (and mocked in tests).

Routing mirrors the benchmark harness in the LLM-prompting repo: the *caller*
selects the prompt by mode — cold_start vs in_flight — rather than asking the
model to choose. Temperature is pinned to 0 for reproducibility.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

DEFAULT_OLLAMA_URL = 'http://localhost:11434/api/chat'
DEFAULT_MODEL = 'gemma4:26b'  # fastest model that passed the full suite

STACK_SLOTS = [
    'L1_left', 'L1_mid', 'L1_right', 'L2_left', 'L2_right', 'L3_top',
]


# ── Prompt loading / JSON extraction (mirrors run_labeled_scenarios.py) ─────

def load_system_prompt(text: str) -> str:
    """Extract the fenced system prompt from a vendored prompt markdown."""
    match = re.search(
        r'## System Prompt\s*```(?:text)?\n(.*?)\n```', text, re.DOTALL)
    if not match:
        match = re.search(r'```(?:text)?\n(.*?)\n```', text, re.DOTALL)
    if not match:
        raise ValueError('no fenced system prompt found')
    return match.group(1).strip()


def parse_model_json(content: str) -> dict:
    """Parse model output as JSON, tolerating fences / surrounding prose."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    start, end = content.find('{'), content.rfind('}')
    if start != -1 and end > start:
        return json.loads(content[start:end + 1])
    raise json.JSONDecodeError('no JSON object found', content, 0)


# ── Semantic validation (spec §8) ──────────────────────────────────────────

def validate_cold_start(resp: dict) -> list[str]:
    """Structural + semantic checks for a cold-start planner response (§8.2)."""
    errs: list[str] = []
    status = resp.get('status')
    if status not in ('ok', 'unsupported', 'insufficient_resources'):
        errs.append(f'bad status: {status!r}')
    if status == 'ok':
        target, plan = resp.get('target'), resp.get('plan')
        if not target or not plan:
            errs.append('status=ok requires non-null target and plan')
            return errs
        slots = target.get('target_slots') or []
        if target.get('cup_budget') != len(slots):
            errs.append('cup_budget != len(target_slots)')
        steps = plan.get('steps') or []
        if len(steps) != target.get('cup_budget', -1):
            errs.append('step count != cup_budget')
        errs += _check_pyramid_steps(steps)
    else:  # unsupported / insufficient_resources
        if resp.get('plan') is not None:
            errs.append(f'status={status} requires plan=null')
        if not (resp.get('error') or {}).get('code'):
            errs.append(f'status={status} requires error.code')
    return errs


def validate_inflight(resp: dict, payload: dict) -> list[str]:
    """Structural + semantic checks for an in-flight decision (§8.3)."""
    errs: list[str] = []
    decision = resp.get('decision')
    if decision not in ('continue', 'replan', 'done'):
        errs.append(f'bad decision: {decision!r}')
    if decision in ('continue', 'done') and resp.get('plan') is not None:
        errs.append(f'decision={decision} requires plan=null')
    if decision == 'replan':
        plan = resp.get('plan')
        if not plan:
            errs.append('decision=replan requires non-null plan')
        else:
            steps = plan.get('steps') or []
            errs += _check_pyramid_steps(steps)
    if decision == 'done':
        plan = payload.get('current_plan') or {}
        if (plan.get('remaining_steps') or []):
            errs.append('decision=done but remaining_steps not empty')
        lar = payload.get('last_action_result') or {}
        if lar.get('result') != 'success':
            errs.append('decision=done but last_action_result != success')
    return errs


def _check_pyramid_steps(steps: list) -> list[str]:
    """Each step is one atomic `pyramid` action with color + target_slot."""
    errs: list[str] = []
    for i, s in enumerate(steps):
        if s.get('action') != 'pyramid':
            errs.append(
                f'step {i + 1}: expected pyramid, got {s.get("action")}')
        if not s.get('color'):
            errs.append(f'step {i + 1}: pyramid step missing color')
        if not s.get('target_slot'):
            errs.append(f'step {i + 1}: pyramid step missing target_slot')
    return errs


# ── Ollama call ─────────────────────────────────────────────────────────────

def call_ollama(model: str, system_prompt: str, payload: dict, *,
                ollama_url: str = DEFAULT_OLLAMA_URL,
                timeout_seconds: int = 120,
                think: bool = False) -> tuple[dict | None, float, str | None]:
    """POST one chat request. Returns (result_json, elapsed_ms, error_str)."""
    request = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',
             'content': json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        'stream': False,
        'options': {'temperature': 0},
        'think': think,
    }
    data = json.dumps(request).encode('utf-8')
    req = urllib.request.Request(
        ollama_url, data=data, headers={'Content-Type': 'application/json'})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            result = json.loads(response.read())
        return result, (time.time() - start) * 1000, None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        return None, (time.time() - start) * 1000, f'HTTP {exc.code}: {body[:200]}'
    except Exception as exc:  # noqa: BLE001 — surface any transport failure
        return None, (time.time() - start) * 1000, f'{type(exc).__name__}: {exc}'
