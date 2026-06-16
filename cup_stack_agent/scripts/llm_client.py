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
        slot_colors = target.get('slot_colors')
        if slot_colors is not None:
            errs += _check_slot_colors(slot_colors, slots)
            errs += _check_steps_match_slot_colors(steps, slot_colors)
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
    if decision not in ('continue', 'replan', 'done', 'unstack'):
        errs.append(f'bad decision: {decision!r}')
    if decision in ('continue', 'done', 'unstack') and resp.get('plan') is not None:
        errs.append(f'decision={decision} requires plan=null')
    if decision == 'unstack':
        slot = resp.get('slot')
        if slot not in STACK_SLOTS:
            errs.append(
                f'decision=unstack requires a valid slot, got {slot!r}')
        else:
            cw = payload.get('current_world_state') or {}
            stack = cw.get('stack') or {}
            target = (payload.get('current_plan') or {}).get('target') or {}
            slot_colors = target.get('slot_colors')
            try:
                fallen = int(payload.get('fallen_count') or 0)
            except (TypeError, ValueError):
                fallen = 0
            # Valid unstack = a TOP-EXPOSED slot that is EITHER itself a
            # fixable color violation, OR a correct cup blocking access to a
            # FIXABLE buried violation below it (peel, top-down).
            errs += _check_unstack_removable(slot, stack)
            errs += _check_unstack_justified(
                slot, slot_colors, stack, cw.get('cups_on_table'), fallen,
                payload.get('fix_extra_colors'))
    if decision == 'replan':
        plan = resp.get('plan')
        if not plan:
            errs.append('decision=replan requires non-null plan')
        else:
            steps = plan.get('steps') or []
            errs += _check_pyramid_steps(steps)
            # Replan steps must honor the persisted per-slot color constraint.
            cur_target = (payload.get('current_plan') or {}).get('target') or {}
            sc = cur_target.get('slot_colors')
            if sc is not None:
                errs += _check_steps_match_slot_colors(steps, sc)
            # Never fill a slot that would BURY an unfixable color violation
            # below it (keep_empty) — that makes the violation permanently
            # uncorrectable. Leave those empty (done-partial) instead.
            keep_empty = set((payload.get('color_check') or {}).get('keep_empty') or [])
            buried = [s.get('target_slot') for s in steps
                      if s.get('target_slot') in keep_empty]
            if buried:
                errs.append(
                    f'decision=replan steps fill {buried} which would bury an '
                    f'unfixable color violation below — keep them empty')
    if decision == 'done':
        plan = payload.get('current_plan') or {}
        keep_empty = set((payload.get('color_check') or {}).get('keep_empty') or [])
        # Remaining steps that target a keep_empty slot are intentionally NOT
        # executable (filling them would bury an unfixable violation), so they
        # do NOT block done(partial).
        if [s for s in (plan.get('remaining_steps') or [])
                if s.get('target_slot') not in keep_empty]:
            errs.append('decision=done but remaining_steps not empty')
        lar = payload.get('last_action_result') or {}
        if lar.get('result') != 'success':
            errs.append('decision=done but last_action_result != success')
        # Semantic guard: never accept done while a null TARGET slot can still
        # be filled — a color with count > 0 remains, or a fallen cup is
        # reported. The build must GROW (replan) / recover, not stop early.
        # (Catches the model claiming "no cups remain" when cups_on_table
        # actually has cups and a target slot is null.)
        cw = payload.get('current_world_state') or {}
        stack = cw.get('stack') or {}
        target = plan.get('target') or {}
        slot_colors = target.get('slot_colors') or {}
        null_targets = [s for s in (target.get('target_slots') or [])
                        if not stack.get(s)]
        # Slots intentionally kept empty above an unfixable violation must NOT
        # block done — filling them would bury the violation permanently.
        null_targets = [s for s in null_targets if s not in keep_empty]
        cups = cw.get('cups_on_table') or {}
        total_cups = sum(int(v) for v in cups.values()
                         if isinstance(v, (int, float))
                         and not isinstance(v, bool))
        try:
            fallen = int(payload.get('fallen_count') or 0)
        except (TypeError, ValueError):
            fallen = 0
        # A null target is FILLABLE only when a cup that can fill it exists: its
        # required color for a constrained slot, or any color for an "any" slot.
        # A constrained null slot whose color is gone is NOT a reason to block
        # done (it cannot be filled) — that is a legitimate partial.
        fillable = []
        for s in null_targets:
            want = slot_colors.get(s)
            if want and want != 'any':
                if _color_available(cups, want):
                    fillable.append(f'{s}({want})')
            elif total_cups > 0:
                fillable.append(s)
        if fillable or (null_targets and fallen > 0):
            errs.append(
                f'decision=done but null target slot(s) can still be filled '
                f'(fillable={fillable}, fallen={fallen})')
        # Never accept done while a FIXABLE color violation remains — the loop
        # must unstack+refill, not stop (and, with the done->shutdown hook, a
        # wrong done here would terminate irrecoverably). An unfixable one
        # (required color gone from BOTH exo and hand-eye) is allowed as a
        # partial; fix_extra_colors lets a hand-eye-visible colour count as
        # obtainable so an exo blind-spot can't fake "unfixable".
        errs += _check_no_color_violation(
            stack, slot_colors, cups, fallen, payload.get('fix_extra_colors'))
    return errs


def validate_fallen_recovery(resp: dict, payload: dict | None = None) -> list[str]:
    """Checks for the decision="fallen_recovery" interrupt (any mode).

    The interrupt must NOT carry a plan (it never replaces current_plan), and
    the payload's top-level ``fallen_count`` must be positive — the LLM
    cannot invent a cup to recover. The hand-eye vision reports a bare count
    (no color), so the interrupt carries no target object: the recovery task
    stands the nearest fallen cup it sees.
    """
    errs: list[str] = []
    if resp.get('plan') is not None:
        errs.append('decision=fallen_recovery requires plan=null')
    if isinstance(payload, dict):
        try:
            count = int(payload.get('fallen_count') or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            errs.append(
                'decision=fallen_recovery with fallen_count=0 in the payload')
    return errs


# Which slots rest ON a given slot — a slot is unstack-removable only when
# every slot it supports is already empty (top-down teardown, mirrors
# server/domains/robot.py UNSTACK_SEQUENCE and docs/dynamic_loop_plan.md §1).
_UNSTACK_SUPPORTS: dict[str, tuple[str, ...]] = {
    'L1_left': ('L2_left',),
    'L1_mid': ('L2_left', 'L2_right'),
    'L1_right': ('L2_right',),
    'L2_left': ('L3_top',),
    'L2_right': ('L3_top',),
    'L3_top': (),
}


def _slot_occupied(stack: Any, slot: str) -> bool:
    """True when ``slot`` holds a cup in a normalized payload stack
    (``{slot: {"color": str}|null}``; tolerates a bare color string)."""
    if not isinstance(stack, dict):
        return False
    val = stack.get(slot)
    if val is None:
        return False
    if isinstance(val, str):
        return val.strip().lower() not in ('', 'none', 'null', 'empty')
    if isinstance(val, dict):
        return bool(val.get('color'))
    return bool(val)


def _observed_color(stack: Any, slot: str) -> str | None:
    """The color a normalized payload stack reports for ``slot`` (``{slot:
    {"color": c}|null}``; tolerates a bare color string), or None when empty
    or color-unknown."""
    if not isinstance(stack, dict):
        return None
    val = stack.get(slot)
    if isinstance(val, dict):
        c = val.get('color')
        return c if isinstance(c, str) else None
    if isinstance(val, str):
        v = val.strip().lower()
        return None if v in ('', 'none', 'null', 'empty') else val
    return None


def _color_available(cups: Any, color: str) -> bool:
    """True when cups_on_table has at least one cup of ``color``."""
    if not isinstance(cups, dict):
        return False
    try:
        return int(cups.get(color) or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_exposed(stack: Any, slot: str) -> bool:
    """True when no occupied slot rests on ``slot`` — it is the top of its
    column and can be unstacked without disturbing a cup above it."""
    return not any(_slot_occupied(stack, up)
                   for up in _UNSTACK_SUPPORTS.get(slot, ()))


def _check_no_color_violation(
    stack: Any, slot_colors: Any, cups: Any, fallen: int, extra: Any = None,
) -> list[str]:
    """A filled slot whose color differs from its non-"any" slot_colors is a
    color violation. done is blocked for any FIXABLE violation: TOP-EXPOSED
    (its required color is on the table / a fallen cup supplies it — unstack
    directly) OR BURIED but reachable (its required color is held by a cup
    above it that a top-down peel would free, or is on the table). Only a
    violation whose required color cannot be obtained at all (not on the table,
    no fallen cup, not among the cups above it) is a legitimate partial that
    done may leave intact."""
    if not isinstance(slot_colors, dict):
        return []
    bad = []
    for slot, want in slot_colors.items():
        if not want or want == 'any':
            continue
        got = _observed_color(stack, slot)
        if got is None or got == want:
            continue
        if _is_exposed(stack, slot):
            # top-exposed: fixable iff the required color is on the table now
            # (or a fallen cup could supply it, or the hand-eye sees it).
            if _color_available(cups, want) or fallen > 0 or (extra and want in extra):
                bad.append(f'{slot}:{got}!={want}')
        elif _required_color_obtainable(
                want, stack, _above(slot), cups, fallen, extra):
            # buried but reachable: a top-down peel frees the cups above it,
            # one of which (or the table) supplies the required color.
            bad.append(f'{slot}:{got}!={want}(buried-fixable)')
    if bad:
        return [f'decision=done but fixable color violation(s) {bad} — '
                f'unstack/peel and refill first']
    return []


def _above(slot: str) -> set[str]:
    """Transitive set of slots resting on ``slot`` — every one must be empty
    before ``slot`` is exposed. A top-down teardown removes them outer-first."""
    out: set[str] = set()
    for up in _UNSTACK_SUPPORTS.get(slot, ()):
        out.add(up)
        out |= _above(up)
    return out


def _is_color_violation(slot: str, slot_colors: Any, stack: Any) -> bool:
    """True when ``slot`` holds a cup whose color differs from its non-"any"
    required color."""
    if not isinstance(slot_colors, dict):
        return False
    want = slot_colors.get(slot)
    got = _observed_color(stack, slot)
    return bool(want and want != 'any' and got is not None and got != want)


def _required_color_obtainable(
    want: str, stack: Any, blockers: Any, cups: Any, fallen: int,
    extra: Any = None,
) -> bool:
    """``want`` can refill a slot: it is on the (exo) table, a fallen cup could
    supply it, the hand-eye sees it (``extra`` — a colour the exo camera missed
    but the eye-in-hand view has, picked via plan_executor's hand-eye fallback),
    or one of ``blockers`` (cups currently above the slot) holds it and a
    teardown would free it back onto the table. ``extra`` defaults to None so
    callers that don't supply it behave exactly as before (exo-only)."""
    if _color_available(cups, want) or fallen > 0:
        return True
    if extra and want in extra:
        return True
    return any(_observed_color(stack, b) == want for b in (blockers or ()))


def _blocks_fixable_violation(
    slot: str, slot_colors: Any, stack: Any, cups: Any, fallen: int,
    extra: Any = None,
) -> bool:
    """True when top-exposed ``slot`` is a correct cup sitting above a FIXABLE
    buried color violation — removing it (peel) clears access so the violation
    below can eventually be reached and refilled with its required color."""
    if not isinstance(slot_colors, dict):
        return False
    for s, want in slot_colors.items():
        if not want or want == 'any':
            continue
        if slot not in _above(s):
            continue  # slot does not rest above s
        if not _is_color_violation(s, slot_colors, stack):
            continue
        if _required_color_obtainable(want, stack, _above(s), cups, fallen, extra):
            return True
    return False


def _check_unstack_justified(
    slot: str, slot_colors: Any, stack: Any, cups: Any, fallen: int,
    extra: Any = None,
) -> list[str]:
    """A top-exposed unstack is justified when the slot is ITSELF a fixable
    color violation (its required color is obtainable to refill it), OR it is a
    correct cup blocking access to a FIXABLE buried violation below it (peel,
    top-down — it is replaced during the later refill). Otherwise removing it
    just churns correct work, so reject it."""
    if not isinstance(slot_colors, dict):
        return []
    if _is_color_violation(slot, slot_colors, stack):
        want = slot_colors.get(slot)
        if _required_color_obtainable(want, stack, _above(slot), cups, fallen, extra):
            return []
        return [f'decision=unstack slot {slot!r} requires color {want!r} to '
                f'refill but none is available — leave it (done partial)']
    if _blocks_fixable_violation(slot, slot_colors, stack, cups, fallen, extra):
        return []  # peel: clears access to a fixable buried violation below
    got = _observed_color(stack, slot)
    return [f'decision=unstack slot {slot!r} is not a color violation '
            f'(holds {got!r}) and clears no fixable buried violation below — '
            f'do not unstack it']


def compute_color_check(stack: Any, slot_colors: Any, cups: Any,
                        fallen: int = 0, fix_extra: Any = None) -> dict | None:
    """Precompute the color-violation facts a no-CoT decider otherwise has to
    derive by multi-hop. For each constrained slot holding the wrong color, list
    the colors of the cups TRANSITIVELY ABOVE it and whether it is FIXABLE (its
    required color is on the table, supplied by a fallen cup, or held by one of
    those cups above — which a top-down peel frees). Injected into /llm_input so
    the fast decider reads the relationship off instead of inferring it (the
    model otherwise anchors on "buried -> done partial" and never inspects the
    cup above). Returns {"violations": [...]} or None when there is no color
    violation. Pure/deterministic — mirrors validate_inflight's own checks."""
    if not isinstance(slot_colors, dict) or not isinstance(stack, dict):
        return None
    try:
        fallen = int(fallen or 0)
    except (TypeError, ValueError):
        fallen = 0
    viols = []
    keep_empty: set[str] = set()
    for slot, want in slot_colors.items():
        if not want or want == 'any':
            continue
        if not _is_color_violation(slot, slot_colors, stack):
            continue
        ab = _above(slot)
        fixable = _required_color_obtainable(want, stack, ab, cups, fallen,
                                             fix_extra)
        viols.append({
            'slot': slot,
            'holds': _observed_color(stack, slot),
            'requires': want,
            'exposed': _is_exposed(stack, slot),
            'cups_above': {b: _observed_color(stack, b)
                           for b in sorted(ab) if _slot_occupied(stack, b)},
            'fixable': fixable,
        })
        # An UNFIXABLE wrong cup must not be BURIED — keep every still-empty slot
        # above it empty so it stays top-exposed and correctable if its required
        # color reappears later.
        if not fixable:
            keep_empty |= {b for b in ab if not _slot_occupied(stack, b)}
    if not viols:
        return None
    out = {'violations': viols}
    if keep_empty:
        out['keep_empty'] = sorted(keep_empty)
    return out


def _check_unstack_removable(slot: str, stack: Any) -> list[str]:
    """An unstack target must be FILLED and TOP-EXPOSED (nothing resting on it).

    A still-buried slot is never unstacked DIRECTLY — peel the cups above it
    first (one top-exposed cup per cycle), then it becomes removable. Also
    guards an already-empty slot."""
    errs: list[str] = []
    if not _slot_occupied(stack, slot):
        errs.append(f'decision=unstack but slot {slot!r} is already empty')
        return errs
    blockers = [s for s in _UNSTACK_SUPPORTS.get(slot, ())
                if _slot_occupied(stack, s)]
    if blockers:
        errs.append(
            f'decision=unstack slot {slot!r} is buried under {sorted(blockers)} '
            f'— peel the cup(s) above it first (unstack the top-exposed one)')
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


def _check_slot_colors(slot_colors: Any, target_slots: list) -> list[str]:
    """target.slot_colors (when present) must map EXACTLY the target_slots to a
    known color or "any". (Optional field — callers only invoke this when it is
    present, so absence is not an error.)"""
    errs: list[str] = []
    if not isinstance(slot_colors, dict):
        errs.append('slot_colors must be an object {slot: color|"any"}')
        return errs
    valid = set(_COLOR_ALIASES.values()) | {'any'}
    if set(slot_colors) != set(target_slots):
        errs.append(
            f'slot_colors keys {sorted(slot_colors)} must match target_slots '
            f'{sorted(target_slots)}')
    for slot, color in slot_colors.items():
        if color not in valid:
            errs.append(
                f'slot_colors[{slot!r}]={color!r} is not a known color or "any"')
    return errs


def _check_steps_match_slot_colors(steps: Any, slot_colors: Any) -> list[str]:
    """A step filling a CONSTRAINED slot (slot_colors[slot] != "any") must use
    that exact color. "any" slots are unconstrained; a constrained slot with NO
    step is fine (it may be unfillable for now) — only a wrong-color step errs."""
    errs: list[str] = []
    if not isinstance(slot_colors, dict) or not isinstance(steps, list):
        return errs
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        want = slot_colors.get(s.get('target_slot'))
        if want and want != 'any' and s.get('color') != want:
            errs.append(
                f"step {i + 1}: slot {s.get('target_slot')!r} requires color "
                f"{want!r} but step uses {s.get('color')!r}")
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
