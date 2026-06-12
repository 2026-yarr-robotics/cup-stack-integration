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
        return json.loads(content, strict=False)
    except json.JSONDecodeError:
        pass
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
    if fence:
        return json.loads(fence.group(1), strict=False)
    start, end = content.find('{'), content.rfind('}')
    if start != -1 and end > start:
        return json.loads(content[start:end + 1], strict=False)
    raise json.JSONDecodeError('no JSON object found', content, 0)


# ── Semantic validation (spec §8) ──────────────────────────────────────────

# command alias -> canonical color. Multi-char only: 1-char Korean roots
# ('파'/'노'/'검') would false-match '파라미드'/'노력'/'검출'.
_COLOR_ALIASES = {
    'red': 'red', '레드': 'red', '빨강': 'red', '빨간': 'red',
    'orange': 'orange', '오렌지': 'orange', '주황': 'orange',
    'yellow': 'yellow', '옐로': 'yellow', '노랑': 'yellow', '노란': 'yellow',
    'green': 'green', '그린': 'green', '초록': 'green', '녹색': 'green',
    'blue': 'blue', '블루': 'blue', '파랑': 'blue', '파란': 'blue',
    'purple': 'purple', '퍼플': 'purple', '보라': 'purple',
    'white': 'white', '화이트': 'white', '하얀': 'white', '흰색': 'white',
    'black': 'black', '블랙': 'black', '검정': 'black', '검은': 'black',
    'gray': 'gray', 'grey': 'gray', '그레이': 'gray', '회색': 'gray',
}


def validate_cold_start(resp: dict, payload: dict | None = None) -> list[str]:
    """Structural + semantic checks for a cold-start planner response (§8.2).
    Partial plans (fewer steps than cup_budget) are allowed; insufficient is
    rejected when the input has cups and there is no color constraint."""
    errs: list[str] = []
    status = resp.get('status')
    if status not in ('ok', 'unsupported', 'insufficient_resources'):
        errs.append(f'bad status: {status!r}')

    total_available = None
    cups: dict = {}
    user_cmd = ''
    if isinstance(payload, dict):
        cw = payload.get('current_world_state') or {}
        raw = cw.get('cups_on_table') or {}
        if isinstance(raw, dict):
            cups = {str(k).lower(): int(v) for k, v in raw.items()
                    if isinstance(v, (int, float))
                    and not isinstance(v, bool)}     # bool is an int subclass
            total_available = sum(cups.values())
        user_cmd = str(payload.get('user_command') or '').lower()
    requested_colors = {canon for alias, canon in _COLOR_ALIASES.items()
                        if alias in user_cmd}

    if status == 'ok':
        target, plan = resp.get('target'), resp.get('plan')
        if not target or not plan:
            errs.append('status=ok requires non-null target and plan')
            return errs
        slots = target.get('target_slots') or []
        if target.get('cup_budget') != len(slots):
            errs.append('cup_budget != len(target_slots)')
        steps = plan.get('steps') or []
        budget = target.get('cup_budget', -1)
        # PARTIAL plans allowed: 1..min(cup_budget, available cups).
        if len(steps) < 1:
            errs.append('status=ok requires at least one step')
        elif len(steps) > budget:
            errs.append(f'step count {len(steps)} exceeds cup_budget {budget}')
        elif (total_available is not None
              and len(steps) > total_available):
            errs.append(f'step count {len(steps)} exceeds available cups '
                        f'{total_available}')
        errs += _check_pyramid_steps(steps)
    else:  # unsupported / insufficient_resources
        if resp.get('plan') is not None:
            errs.append(f'status={status} requires plan=null')
        if not (resp.get('error') or {}).get('code'):
            errs.append(f'status={status} requires error.code')
        # Semantic guardrail: do NOT accept insufficient when cups are
        # actually available and there is no color constraint (LLM ignored
        # e.g. blue:6). A color-constrained command may legitimately be
        # insufficient (requested color 0), so only reject when unconstrained.
        if status == 'insufficient_resources' and total_available is not None:
            if not requested_colors:
                # no color constraint: any cup means something is buildable
                if total_available > 0:
                    errs.append(
                        f'insufficient_resources but {total_available} cups '
                        'available with no color constraint')
            else:
                # color-constrained: insufficient is valid ONLY if every
                # requested color has 0 cups; reject if any has cups.
                avail = sum(cups.get(c, 0) for c in requested_colors)
                if avail > 0:
                    errs.append(
                        f'insufficient_resources but requested color(s) '
                        f'{sorted(requested_colors)} have {avail} cups')
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


def validate_fallen_recovery(resp: dict, payload: dict | None = None) -> list[str]:
    """Checks for the decision="fallen_recovery" interrupt (any mode).

    The interrupt must NOT carry a plan (it never replaces current_plan), and
    its color must exist in the payload's top-level ``fallen`` map — the LLM
    cannot invent a cup to recover.
    """
    errs: list[str] = []
    if resp.get('plan') is not None:
        errs.append('decision=fallen_recovery requires plan=null')
    rec = resp.get('fallen_recovery')
    if not isinstance(rec, dict):
        errs.append('decision=fallen_recovery requires a fallen_recovery object')
        return errs
    color = rec.get('color')
    if not color:
        errs.append('fallen_recovery missing color')
        return errs
    if isinstance(payload, dict):
        fallen = payload.get('fallen') or {}
        try:
            count = int(fallen.get(str(color), 0) or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            errs.append(
                f'fallen_recovery color {color!r} not present in fallen map')
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
                num_predict: int | None = None,
                think: bool = False) -> tuple[dict | None, float, str | None]:
    """POST one chat request. Returns (result_json, elapsed_ms, error_str)."""
    options = {'temperature': 0}
    if num_predict is not None:
        options['num_predict'] = int(num_predict)
    request = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',
             'content': json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        'stream': False,
        # Constrain decoding to syntactically valid JSON at the source so a
        # rambling string cannot emit a raw control char the parser rejects.
        # parse_model_json strict=False stays as a downstream safety net.
        'format': 'json',
        'options': options,
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
