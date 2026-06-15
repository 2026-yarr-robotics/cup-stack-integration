# 동적 폐루프 업그레이드 설계 (B+unstack)

> 상태: **계획 only** — 구현 전 리뷰용. 위험한 수정(동적 루프 + 물리 해체)이라
> 단계별로 독립 검증 가능하게 phasing 한다.

## 0. 동기 (왜)

현재 루프는 "cold-start로 plan을 만들고 step을 소진하면 done"이다. 그래서:

- **시작이 5 upright + 1 fallen**이면 cold-start가 5개로만 step을 짜고, fallen을
  세운 뒤에도 그 컵을 맨 위에 올리지 못한 채 멈춘다(부분 완성).
- 피라미드에 **색이 틀린 컵**이 올라가도 빼낼 방법이 없다.

목표: LLM을 "plan 실행기"가 아니라 **"매 결정 시점에 관측 상태를 목표와 비교해
차이를 줄이는 한 수를 고르는 컨트롤러"**로 승격한다.

## 1. 핵심 사실 (코드 그라운드 트루스)

루프 재설계가 가능한 이유 — 이미 구조가 우호적이다.

1. **plan_executor는 스스로 done을 안 정한다.** step 소진 시
   `plan_executor_node.py:589-591` 에서 `"plan exhausted — awaiting LLM decision"`
   로 멈추고 `/llm_output` 결정만 기다린다 → plan은 사실상 조언, brain은 LLM.
2. **`/stack`(verifier)이 슬롯별 색을 sticky·~10Hz로 발행** → "틀린 색"을 관측 가능.
   GSP는 idle 중 world 변화(`_on_world_change`)·`/action_result`에 자동으로
   `/llm_input`을 쏜다 → 교란 반응 배선이 이미 있음.
3. **unstack은 fallen_recovery와 같은 직접-디스패치 primitive다.**
   `/api/robot/skill/unstack {slot,x,y,nested}` 는 **동기**이고, 집을 좌표(slot 절대
   위치)·pick_z 를 **서버가 config 캐시에서 자동으로 가져온다**
   (`domains/robot.py:936-` `unstack_skill`). 즉 agent는 pick 좌표 불필요, pick_node·
   coarse move 불필요. plan_executor가 직접 호출 후 `/action_result` 직접 발행하면 됨
   (정확히 `_do_fallen_recovery_step` 패턴, `plan_executor_node.py:710-732`).
4. **복구/제거된 컵의 색은 자동 재인식.** recovery `mode:place`는 컵을 피라미드 밖
   안전구역(0.30, 0.10)에 똑바로 세우고, exo가 ~1–2초 안에 색 포함으로
   `/cups_on_table`에 반영한다. unstack도 destination에 똑바로 내려놓으므로 동일.
5. **unstack은 top-down 강제** — 서버 docstring/스키마가 명시
   (`3m → 2r/2l → 1r/1m/1l`, 호출자 책임, `schemas.py:550-551`). 우리가 지지관계
   규칙만 LLM에 주면 됨.

### 지지관계 그래프 (top-down 제거 순서)

```
L3_top(3m) ← L2_left(2l) + L2_right(2r)
L2_left(2l) ← L1_left(1l) + L1_mid(1m)
L2_right(2r) ← L1_mid(1m) + L1_right(1r)
```

- 슬롯은 **위에 점유된 슬롯이 없을 때만** 제거 가능.
- 바닥 `1m` 교정은 `3m, 2l, 2r`를 먼저 빼야 함(비쌈) → 정책 결정 필요(§5).

## 2. 행동 어휘 & 우선순위

| primitive | 트리거 | 디스패치 경로 | 신규? |
|---|---|---|---|
| `fallen_recovery` | upright 0 & fallen>0 | 직접 POST `/fallen-cup/recovery` (비동기, status 폴링) | 기존 |
| `unstack(slot)` | 슬롯 색 ≠ 목표색 & 제거 가능 | **직접 POST `/skill/unstack` (동기)** | **신규** |
| `pyramid(color,slot)` | 빈 목표 슬롯 + 맞는 색 컵 | coarse `/move` → pick_node `/skill/pyramid` | 기존 |
| `done` | gap=0 (고정점) | 루프 종료 | 의미 재정의 |

우선순위(매 틱): **fallen_recovery > unstack > pyramid > done.**
(fallen이 있으면 물리적으로 grasp 불가라 최우선 — 기존 게이트가 이미 보장.)

## 3. 데이터 흐름 (완성형)

```
GOAL(불변, cold-start 1회 컴파일)
  shape : target.target_slots
  color : target.slot_colors  (슬롯→desired color | "any")
        │ gap 진단
OBSERVED ──┐
 /stack         슬롯→색  → 색 위반 감지
 /cups_on_table {색:수}  → 채울 자원
 /fallen_cups   개수     → 복구 가능 자원
        ▲                 ┌──────── LLM(inflight_decider) ────────┐
        │                 │ 1 fallen_recovery / 2 unstack /        │
        │                 │ 3 pyramid / 4 done                     │
        │                 └───────────────┬───────────────────────┘
   perception ◀── /action_result ◀── plan_executor 디스패치 ◀┘
```

틀린 색 교정 사이클:
```
색 위반 감지(/stack vs goal) → unstack(top-down) → [world freeze]
  → 슬롯 null + 컵 색 복귀를 역방향 reflection 게이트로 확인
  → replan: 그 슬롯을 맞는 색 pyramid (없으면 fallen_recovery로 조달)
  → gap=0 → done
```

## 4. 파일별 변경 지점 (line 기준)

### prompts/cold_start_planner.md  (Phase 2)
- target에 `slot_colors` 추가: 색 제약 파싱 → 슬롯별 desired color, 무제약이면 전부
  `"any"`. (현재 색은 step에만 있고 target엔 없음 — line 70 output 스키마 확장.)

### prompts/inflight_decider.md  (Phase 1 + 2 + 3)
- **Phase 1**: done/replan 규칙 재작성(line 31-32). done = target.target_slots의 모든
  null 슬롯에 대해 쓸 컵 없음(cups 전부 0 & fallen_count 0)일 때만. remaining_steps가
  비어도 null 슬롯+자원 있으면 replan(line 43 채움 규칙 재사용). few-shot 추가.
- **Phase 2**: 관측 슬롯색 vs target.slot_colors 비교 규칙(위반 감지) 추가.
- **Phase 3**: `decision="unstack"` + 우선순위 + 지지관계(top-down/캐스케이드) +
  진동가드 규칙 + few-shot. 출력 스키마에 unstack/slot 추가(line 46-47).

### scripts/llm_client.py  (Phase 2 + 3)
- `validate_inflight` (line 143-165): decision 집합에 `'unstack'` 추가, unstack은
  `slot` 필수 + `plan=null` 검증.
- `validate_cold_start` (line 72-140): `slot_colors` 구조 검증(있으면 target_slots와
  키 일치, 값은 색 또는 "any").

### scripts/plan_executor_node.py  (Phase 3)
- `_on_llm_output` (line 515-557): `decision=='unstack'` 분기 추가 →
  `_execute_unstack(slot)`. (fallen_recovery 분기 line 522-527와 동일 형태.)
- 신규 `_execute_unstack` / `_do_unstack_step` : `_execute_fallen_recovery` /
  `_do_fallen_recovery_step`(line 700-766) 미러링. POST `/api/robot/skill/unstack`
  동기 호출, 성공 시 `/action_result {step:null, action:"unstack", slot, result}`
  직접 발행, `_plan/_step_idx` 비전진(interrupt).
- 제거 컵 destination: 신규 파라미터 `unstack_nest_xy`(기본 안전구역, recovery의
  0.30,0.10 계열과 충돌 안 나게) + nested 높이 관리. **권장: 각 컵을 개별 spot에
  `nested=1`로 내려놓아** exo가 각각 색을 보게 한다(nested 컬럼은 top 컵 색만 보임).
- 슬롯 키 매핑은 `_LLM_TO_API_SLOT`(line 71-75) 재사용.

### scripts/goal_state_publisher_node.py  (Phase 1 + 3)
- **Phase 1 done-race 가드**: `_apply_fallen_to_payload`(line 463-487)는 그대로 두되,
  "upright==0 & target에 null 슬롯 남음"인 결정에서 fresh fallen 샘플이 올 때까지
  done 결정을 보류 — 기존 `_pending_recovery` 샘플-대기 패턴(line 234-248, 347-372)
  재사용. (stale=0 게이팅으로 fallen을 못 보고 조기 done 하는 레이스 차단.)
- **Phase 3 freeze**: `_on_llm_output`(line 263-291)에서 `decision=='unstack'`도
  pyramid처럼 world freeze, unstack `/action_result`에 unfreeze(line 230-261에 분기).
- **Phase 3 역방향 reflection**: `_maybe_publish_pending_action`(line 374-403)에
  unstack용 대칭 경로 — "슬롯 null + 해당 색 table +1"이 관측될 때까지 다음 결정
  보류(아래 payload_builder 헬퍼 사용).

### scripts/payload_builder.py  (Phase 2 + 3)
- `action_result_reflected`(line 143-179): 현재 pyramid만 검증(비-pyramid는 즉시
  True). unstack용 역방향 판정 추가 — `result.action=='unstack'`이면 슬롯이 null이
  됐고 table[color]가 증가했는지 확인. **진동 방지의 핵심.**
- `on_action_result`(line 268-300): `action=='unstack'`은 fallen_recovery처럼 plan
  비전진(line 273-278 가드에 추가).
- `set_plan`/`build_payload`: target.slot_colors 보존(Phase 2).

## 5. 정책 결정 (✅ 2026-06-15 확정)

1. **unstack 트리거 출처** → ✅ **둘 다.** 색 제약 위반 + 교란으로 멀쩡한 슬롯이
   바뀐 경우 모두 교정(관측 슬롯색 ≠ goal.slot_colors 면 교정 대상).
2. **교정 공격성(캐스케이드)** → ✅ **top-down 캐스케이드 허용하되 "맞는 색 교체 컵
   확보 가능"일 때만 시작.** 확보 불가하면 partial done.
3. **제거 컵 destination + 진동가드** → ✅ **개별 spot `nested=1`**(색 재인식 우선) +
   "맞는 색 교체 컵이 인벤토리/복구로 확보 불가하면 unstack 안 함" + 슬롯당 시도 cap.

> 작업 브랜치: `loop` (top-level integration repo; cup_stack_agent은 own code라
> 서브모듈 커밋 불필요, 커밋 작성자는 checkout의 git user).

### #7 교란 트리거 결정 (✅ 2026-06-15)

- **노이즈 필터는 verifier가 이미 처리** (release_off 5s + color vote, #10) →
  GSP에 별도 디바운스 불필요. `/stack`이 슬롯을 null로 보고하면 그건 이미 ~5초
  확정된 교란이다. #7의 실제 작업은 `publish_on_world_change`를 켜되 **실제
  world delta일 때만** publish (지금은 프레임마다 flood하는 구조 → 직전 발행
  world와 다를 때만 쏘도록 게이팅).
- **done 후 자동종료(`_shutdown_agent`)와 충돌 → (a) done 후 10초 grace.**
  done이어도 즉시 안 끄고 10초간 살아서 교란을 감시, 그 사이 교란이 루프를
  다시 깨우면(`publish_on_world_change` 재트리거) 종료 타이머 취소. 10초 무사
  통과 시 종료.

## 6. Phasing (위험도 순)

- **Phase 1 (저위험, 색·unstack 무관)**: 부분플랜→전체목표 수렴 + done-race 가드.
  원래 요구(5 upright+1 fallen→6칸)를 이것만으로 달성. unstack 없음.
- **Phase 2 (중위험)**: target에 slot_colors 승격 + 위반 *감지*(교정은 아직 안 함).
  무제약 명령은 전부 "any"라 기존 동작 100% 보존.
- **Phase 3 (고위험)**: unstack 교정 primitive + freeze/역방향 reflection +
  진동/캐스케이드 가드. 동적 루프 완성.

각 Phase는 독립 머지·검증 가능. Phase 1만으로도 사용자 원래 시나리오 해결.

## 7. 테스트 매트릭스

| # | 시나리오 | Phase | 기대 |
|---|---|---|---|
| 1 | 5 upright + 1 fallen, "3단" | 1 | 5개 쌓고 fallen 복구→6번째 올림→done |
| 2 | upright만 충분, 무제약 | 1-3 | 기존과 동일(회귀 없음, unstack 0회) |
| 3 | 색 제약 + 틀린 색 슬롯(맨 위) | 3 | unstack→맞는 색 재배치→done |
| 4 | 색 제약 + 바닥 색 위반 | 3 | 정책대로 캐스케이드 or partial done |
| 5 | 같은 위반 상태 반복 입력 | 3 | 동일 결정 수렴(무한 unstack 진동 없음) |
| 6 | unstack 후 stale /stack | 3 | 역방향 reflection 게이트가 재-unstack 차단 |

명령: `python3 -m unittest discover -s tests -v`, `python3 -m py_compile scripts/*.py`,
`bash -n start.sh`.

## 8. 위험 요약

- **진동**(unstack↔place 무한): 역방향 reflection 게이트 + "맞는 색 확보 시에만
  unstack" + 시도 cap 으로 차단.
- **done-race**: Phase 1 가드(fresh fallen 샘플 대기).
- **캐스케이드 비용**: 정책 §5.2로 한정.
- **nested 색 가림**: 개별 spot nested=1 로 회피.
- **무제약 명령 회귀**: slot_colors 전부 "any" → unstack 트리거 자체가 안 됨(테스트 #2).
