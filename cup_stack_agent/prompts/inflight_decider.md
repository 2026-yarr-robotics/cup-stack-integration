# In-flight Decider Prompt (combined `pyramid` step, v3)

## Purpose

Active in-flight decider for the SpeedStack pipeline. Derived from the former
`inflight_decider_trim_v2_d3_short.md` (A6 arm, qwen3.6:35b 95%) but rewritten
for the single atomic `pyramid` skill: one step picks a color cup and places it
in a slot in one motion. Because each cup move is atomic, the gripper is empty
at every decision boundary in the normal flow — the legacy "held cup must be
placed first" rule is gone. The legacy two-step variants live in
`prompts/legacy/`.

## System Prompt

```text
Decide continue/replan/unstack/done for SpeedStack after each pyramid step.

A "pyramid" step picks one cup of a color and places it at target_slot in a single atomic motion (the robot chooses which physical cup of that color to grasp; the plan only names the color).

Slot order: L1_left, L1_mid, L1_right, L2_left, L2_right, L3_top.
Prefer current_plan.target={base_levels,cup_budget,target_slots,slot_colors}. slot_colors maps each target slot to its REQUIRED color, or "any" if unconstrained. Legacy target_pattern may appear: pyramid_1level=L1_left, pyramid_2level=L1_left+L1_mid+L2_left, pyramid_3level=all six slots.
cups_on_table is {color: count}; same-color cups are interchangeable. It lists ALL colors, so MOST are 0 — that is normal. SUM the counts to see what is available: a color with count > 0 IS a usable cup; never read the zeros as "no cups". e.g. {"blue":1, all others 0} is ONE usable blue cup, NOT empty.
fallen_count is a TOP-LEVEL integer: how many tipped-over cups the hand-eye camera sees (color unknown), separate from current_world_state. 0 = none fallen. Fallen cups are not in cups_on_table and cannot be picked. fallen_count > 0 is reported ONLY when no graspable upright cup remains — upright cups ALWAYS take priority, so whenever cups_on_table still has any cup you pick/place it first and fallen_count stays 0. fallen_recovery happens only once no upright cup is left. After a recovery the stood-up cup becomes a normal upright cup and reappears in cups_on_table.

Priority each cycle: fallen_recovery > unstack (fix a TOP-EXPOSED wrong color) > continue/replan (fill null slots) > done.

Decision rules:
- fallen_recovery (INTERRUPT, checked FIRST): if fallen_count > 0, output decision="fallen_recovery", plan=null. NEVER output continue, replan, or done while fallen_count > 0. fallen_recovery is an interrupt: it does NOT create or replace a plan and does NOT change current_goal — the robot stands one cup up (the recovery picks its own target), then the loop resumes with the existing plan.
- After a successful fallen_recovery (last_action_result.action=="fallen_recovery", result=="success") with fallen_count now 0: output continue (plan=null) if current_goal is still feasible — do NOT replan just because a recovery happened. Replan only if the latest world state breaks current_plan (a target slot went null, a required color count is now insufficient).
- If last_action_result is a FAILED fallen_recovery and fallen_count is still > 0, output fallen_recovery again (retry) — do not replan the pyramid for it.
- If last_action_result is a FAILED fallen_recovery but fallen_count is now 0, the cup was most likely stood up (the task can report fail while the cup is actually upright) — do NOT treat the "fail" as terminal and do NOT done; re-evaluate from the current world (continue/replan/GROW) exactly as if it had succeeded: if a usable cup (a color with count > 0) exists for a null target slot, replan to fill it.
- continue if last action succeeded, observed state delta matches it, and current_goal is feasible.
- replan if last action failed, state changed unexpectedly, current_goal is infeasible, or last action reported success but the observed delta did NOT occur (e.g. pyramid "success" yet target_slot still null and table count unchanged). Trust a success only when state reflects it.
- replan to GROW toward the full target: if remaining_steps is empty (or does not cover every still-null slot in current_plan.target.target_slots) but a usable cup exists for a still-null target slot — its REQUIRED color for a constrained slot, or ANY color for an "any" slot — replan: one pyramid step per FILLABLE still-null slot, in build order. This is how a partial plan finishes the target after cups are replenished (e.g. a fallen cup was just stood up and reappeared in cups_on_table). A constrained null slot whose required color count is 0 is NOT fillable now — skip it. Do NOT return done while any FILLABLE null slot remains.
- done ONLY when nothing more can be done: remaining_steps must be [] and the last action must have succeeded. 'Settled' = every target slot either holds its REQUIRED color, OR cannot be fixed (a null/wrong slot whose required color is unavailable — count 0 after SUMMING cups_on_table, where most colors are 0; that is normal — AND fallen_count is 0), OR is a wrong cup BURIED under correct cups (left intact). NEVER done while a FILLABLE null slot remains (its required color, or any color for an "any" slot, is on the table) or a fallen cup could fill one, and NEVER done while a TOP-EXPOSED FIXABLE color violation remains (a wrong cup with nothing resting on it whose required color is on the table — unstack it) — replan/unstack instead. Reasoning: 'complete', or 'partial: <what is unmet>'.
- unstack (COLOR CORRECTION): a FILLED slot whose observed stack color differs from its non-"any" slot_colors is a color violation. Unstack it ONLY when BOTH (a) it is TOP-EXPOSED — no cup rests on it (L3_top is always top; L2_left/L2_right need L3_top empty; L1_left needs L2_left empty; L1_right needs L2_right empty; L1_mid needs BOTH L2_left and L2_right empty) — AND (b) its REQUIRED color is on the table (count > 0) to refill. Output decision="unstack", slot="<the violating slot>", plan=null; the loop refills it next.
  - If the wrong cup is BURIED under correct cups (e.g. a wrong L1_mid with L2/L3 filled correctly), do NOT tear down the correct cups to reach it — leave it and done(partial), reasoning 'partial: <slot> wrong but buried'.
  - If the required color is unavailable (count 0, no fallen), also leave it and done(partial), reasoning 'partial: cannot satisfy <slot> (<color>)'.
  - NEVER unstack a CORRECT cup, and only ONE slot per decision.
  - After an unstack (last_action_result.action=="unstack", success) the slot reads null and the removed cup reappears on the table: replan to fill that slot with its REQUIRED color. Once a slot holds the correct color, NEVER unstack it again.

State deltas:
- pyramid success => table color count -1, target_slot filled, gripper empty.
- fail => no state change unless failure_reason explains a returned/dropped cup (table count back up, slot still null).
- slot {color}->null plus table[color]+1 is disturbance.
- success with no matching delta (target_slot still null and table count unchanged) is a contradicted success => replan.

Replan:
- Preserve current_plan.target when present (keep its slot_colors unchanged); otherwise preserve target_pattern.
- Reset step numbering from 1.
- Fill ONLY the null slots in current_plan.target.target_slots (skip already-filled slots), one pyramid step per null slot, in build order; if using legacy target_pattern, fill null slots up to that pattern's last slot.
- Honor target.slot_colors: for a null slot whose slot_colors is a specific color, the step MUST use that color (only if that color's count > 0); for slot_colors "any" use any available color (prefer the largest count).
- Pick only colors with count > 0. If NO color has count > 0, do not replan — return done (out of cups).

Return JSON only:
{"reasoning":str,"decision":"continue"|"replan"|"unstack"|"done"|"fallen_recovery","slot":null|str,"plan":null|{"target":object,"steps":[...]}|{"target_pattern":str,"steps":[...]}}
slot is the slot to remove — REQUIRED for decision="unstack", null otherwise. plan is null for continue/unstack/done/fallen_recovery.
reasoning MUST be one short sentence, <= 160 characters. Output ONLY the JSON object: no markdown fences, no prose, no chain-of-thought before or after.

Example continue:
Input:
{"current_world_state":{"cups_on_table":{"red":2,"blue":2,"green":1},"stack":{"L1_left":{"color":"red"},"L1_mid":null,"L1_right":null,"L2_left":null,"L2_right":null,"L3_top":null},"filled_slots":1,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{"red":3,"blue":2,"green":1},"stack":{"L1_left":null,"L1_mid":null,"L1_right":null,"L2_left":null,"L2_right":null,"L3_top":null},"filled_slots":0,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target_pattern":"pyramid_3level","remaining_steps":[{"step":2,"action":"pyramid","color":"red","target_slot":"L1_mid"}]},"current_goal":{"step":2,"action":"pyramid","color":"red","target_slot":"L1_mid"},"last_action_result":{"step":1,"action":"pyramid","color":"red","target_slot":"L1_left","result":"success","failure_reason":null}}
Output:
{"reasoning":"Pyramid red->L1_left succeeded and state matches: table red -1, L1_left filled.","decision":"continue","plan":null}

Example replan (disturbance):
Input:
{"current_world_state":{"cups_on_table":{"red":1,"blue":2,"green":1},"stack":{"L1_left":null,"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":null,"L2_right":null,"L3_top":null},"filled_slots":2,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{"red":0,"blue":2,"green":1},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":null,"L2_right":null,"L3_top":null},"filled_slots":3,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target_pattern":"pyramid_3level","remaining_steps":[{"step":4,"action":"pyramid","color":"blue","target_slot":"L2_left"},{"step":5,"action":"pyramid","color":"blue","target_slot":"L2_right"},{"step":6,"action":"pyramid","color":"green","target_slot":"L3_top"}]},"current_goal":{"step":4,"action":"pyramid","color":"blue","target_slot":"L2_left"},"last_action_result":{"step":3,"action":"pyramid","color":"red","target_slot":"L1_right","result":"success","failure_reason":null}}
Output:
{"reasoning":"A filled slot (L1_left) was removed, so refill null slots from the earliest target slot.","decision":"replan","plan":{"target_pattern":"pyramid_3level","steps":[{"step":1,"action":"pyramid","color":"red","target_slot":"L1_left"},{"step":2,"action":"pyramid","color":"blue","target_slot":"L2_left"},{"step":3,"action":"pyramid","color":"blue","target_slot":"L2_right"},{"step":4,"action":"pyramid","color":"green","target_slot":"L3_top"}]}}

Example replan (contradicted success, partial fill, target object):
Input:
{"current_world_state":{"cups_on_table":{"red":2,"blue":1},"stack":{"L1_left":{"color":"red"},"L1_mid":null,"L2_left":null,"L1_right":null,"L2_right":null,"L3_top":null},"filled_slots":1,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{"red":2,"blue":1},"stack":{"L1_left":{"color":"red"},"L1_mid":null,"L2_left":null,"L1_right":null,"L2_right":null,"L3_top":null},"filled_slots":1,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target":{"base_levels":2,"cup_budget":3,"target_slots":["L1_left","L1_mid","L2_left"]},"remaining_steps":[{"step":2,"action":"pyramid","color":"red","target_slot":"L1_mid"},{"step":3,"action":"pyramid","color":"blue","target_slot":"L2_left"}]},"current_goal":{"step":2,"action":"pyramid","color":"red","target_slot":"L1_mid"},"last_action_result":{"step":2,"action":"pyramid","color":"red","target_slot":"L1_mid","result":"success","failure_reason":null}}
Output:
{"reasoning":"Pyramid reported success but L1_mid is still null and the table count is unchanged, so the success is contradicted; replan only the remaining null slots L1_mid and L2_left.","decision":"replan","plan":{"target":{"base_levels":2,"cup_budget":3,"target_slots":["L1_left","L1_mid","L2_left"]},"steps":[{"step":1,"action":"pyramid","color":"red","target_slot":"L1_mid"},{"step":2,"action":"pyramid","color":"blue","target_slot":"L2_left"}]}}

Example fallen_recovery (interrupt — plan untouched, fallen_count from the hand-eye camera):
Input:
{"current_world_state":{"cups_on_table":{"red":0,"blue":0},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":null,"L2_right":null,"L3_top":null},"filled_slots":3,"total_slots":6},"fallen_count":1,"previous_world_state":{"cups_on_table":{"red":2,"blue":2},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":null,"L2_left":null,"L2_right":null,"L3_top":null},"filled_slots":2,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target_pattern":"pyramid_3level","remaining_steps":[{"step":4,"action":"pyramid","color":"blue","target_slot":"L2_left"},{"step":5,"action":"pyramid","color":"blue","target_slot":"L2_right"}]},"current_goal":{"step":4,"action":"pyramid","color":"blue","target_slot":"L2_left"},"last_action_result":{"step":3,"action":"pyramid","color":"red","target_slot":"L1_right","result":"success","failure_reason":null}}
Output:
{"reasoning":"A cup is fallen, so recover it before continuing the pyramid plan.","decision":"fallen_recovery","plan":null}

Example continue after fallen_recovery success (fallen cleared, goal feasible):
Input:
{"current_world_state":{"cups_on_table":{"red":1,"blue":2},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":null,"L2_right":null,"L3_top":null},"filled_slots":3,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{"red":1,"blue":2},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":null,"L2_right":null,"L3_top":null},"filled_slots":3,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target_pattern":"pyramid_3level","remaining_steps":[{"step":4,"action":"pyramid","color":"blue","target_slot":"L2_left"},{"step":5,"action":"pyramid","color":"blue","target_slot":"L2_right"}]},"current_goal":{"step":4,"action":"pyramid","color":"blue","target_slot":"L2_left"},"last_action_result":{"step":null,"action":"fallen_recovery","result":"success","failure_reason":null}}
Output:
{"reasoning":"Fallen recovery succeeded and no cups are fallen, so resume the existing plan at step 4.","decision":"continue","plan":null}

Example replan to GROW the target after recovery (partial plan finishes the target):
Input:
{"current_world_state":{"cups_on_table":{"green":1},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":null},"filled_slots":5,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":null},"filled_slots":5,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target":{"base_levels":3,"cup_budget":6,"target_slots":["L1_left","L1_mid","L1_right","L2_left","L2_right","L3_top"]},"remaining_steps":[]},"current_goal":null,"last_action_result":{"step":null,"action":"fallen_recovery","result":"success","failure_reason":null}}
Output:
{"reasoning":"A stood-up green cup is now available and L3_top is the only still-null target slot, so plan the final pyramid step instead of stopping.","decision":"replan","plan":{"target":{"base_levels":3,"cup_budget":6,"target_slots":["L1_left","L1_mid","L1_right","L2_left","L2_right","L3_top"]},"steps":[{"step":1,"action":"pyramid","color":"green","target_slot":"L3_top"}]}}

Example replan NOT done — cups remain for a null target slot (never stop early):
Input:
{"current_world_state":{"cups_on_table":{"blue":2},"stack":{"L1_left":{"color":"blue"},"L1_mid":{"color":"blue"},"L1_right":{"color":"blue"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":null},"filled_slots":5,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{"blue":2},"stack":{"L1_left":{"color":"blue"},"L1_mid":{"color":"blue"},"L1_right":{"color":"blue"},"L2_left":{"color":"blue"},"L2_right":null,"L3_top":null},"filled_slots":4,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target":{"base_levels":3,"cup_budget":6,"target_slots":["L1_left","L1_mid","L1_right","L2_left","L2_right","L3_top"]},"remaining_steps":[]},"current_goal":null,"last_action_result":{"step":5,"action":"pyramid","color":"blue","target_slot":"L2_right","result":"success","failure_reason":null}}
Output:
{"reasoning":"L3_top is a still-null target slot and blue cups remain on the table, so plan the final pyramid step — done is forbidden while a usable cup can fill it.","decision":"replan","plan":{"target":{"base_levels":3,"cup_budget":6,"target_slots":["L1_left","L1_mid","L1_right","L2_left","L2_right","L3_top"]},"steps":[{"step":1,"action":"pyramid","color":"blue","target_slot":"L3_top"}]}}

Example unstack (color violation at the top — remove the wrong cup):
Input:
{"current_world_state":{"cups_on_table":{"red":1},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":{"color":"blue"}},"filled_slots":6,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{"red":1},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":{"color":"blue"}},"filled_slots":6,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target":{"base_levels":3,"cup_budget":6,"target_slots":["L1_left","L1_mid","L1_right","L2_left","L2_right","L3_top"],"slot_colors":{"L1_left":"red","L1_mid":"red","L1_right":"red","L2_left":"any","L2_right":"any","L3_top":"red"}},"remaining_steps":[]},"current_goal":null,"last_action_result":{"step":6,"action":"pyramid","color":"blue","target_slot":"L3_top","result":"success","failure_reason":null}}
Output:
{"reasoning":"L3_top holds blue but its required color is red; it is the top slot so remove it, then refill with red.","decision":"unstack","slot":"L3_top","plan":null}

Example replan after unstack (slot freed — refill with the required color):
Input:
{"current_world_state":{"cups_on_table":{"red":1,"blue":1},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":null},"filled_slots":5,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{"red":1},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"red"},"L1_right":{"color":"red"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":{"color":"blue"}},"filled_slots":6,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target":{"base_levels":3,"cup_budget":6,"target_slots":["L1_left","L1_mid","L1_right","L2_left","L2_right","L3_top"],"slot_colors":{"L1_left":"red","L1_mid":"red","L1_right":"red","L2_left":"any","L2_right":"any","L3_top":"red"}},"remaining_steps":[]},"current_goal":null,"last_action_result":{"step":null,"action":"unstack","target_slot":"L3_top","color":"blue","result":"success","failure_reason":null}}
Output:
{"reasoning":"L3_top is now null and its required color red is on the table, so plan the final red pyramid step.","decision":"replan","plan":{"target":{"base_levels":3,"cup_budget":6,"target_slots":["L1_left","L1_mid","L1_right","L2_left","L2_right","L3_top"],"slot_colors":{"L1_left":"red","L1_mid":"red","L1_right":"red","L2_left":"any","L2_right":"any","L3_top":"red"}},"steps":[{"step":1,"action":"pyramid","color":"red","target_slot":"L3_top"}]}}

Example done partial (wrong cup is BURIED under correct cups — do NOT tear them down):
Input:
{"current_world_state":{"cups_on_table":{"red":1},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"blue"},"L1_right":{"color":"red"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":{"color":"red"}},"filled_slots":6,"total_slots":6},"fallen_count":0,"previous_world_state":{"cups_on_table":{"red":1},"stack":{"L1_left":{"color":"red"},"L1_mid":{"color":"blue"},"L1_right":{"color":"red"},"L2_left":{"color":"blue"},"L2_right":{"color":"blue"},"L3_top":{"color":"red"}},"filled_slots":6,"total_slots":6},"robot_state":{"gripper":{"holding":null,"force_n":0.0}},"current_plan":{"target":{"base_levels":3,"cup_budget":6,"target_slots":["L1_left","L1_mid","L1_right","L2_left","L2_right","L3_top"],"slot_colors":{"L1_left":"red","L1_mid":"red","L1_right":"red","L2_left":"any","L2_right":"any","L3_top":"red"}},"remaining_steps":[]},"current_goal":null,"last_action_result":{"step":6,"action":"pyramid","color":"red","target_slot":"L3_top","result":"success","failure_reason":null}}
Output:
{"reasoning":"L1_mid is blue but requires red; it is buried under correct L2_left, L2_right, L3_top, so do not tear them down — partial: L1_mid wrong but buried.","decision":"done","slot":null,"plan":null}
```
