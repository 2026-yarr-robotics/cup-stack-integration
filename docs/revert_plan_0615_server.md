# 06-15 server 변경 되돌리기 준비 계획 (revert-prep)

> 작성: 2026-06-16 · 대상: `server` 서브모듈(github `2026-yarr-robotics/server`)
> 범위: **KST 2026-06-15 하루치 server 커밋 전부**의 되돌리기 준비도 정리.
> 워크플로: 각 되돌리기는 **`fix/…` 브랜치 + worktree → push → PR(base: `main`)**.
> 서브모듈 커밋 author = `dwl21 <nggus5@gmail.com>`, 부모 포인터 bump = 체크아웃 git user.

## 0. 결론 요약

| 결정 | 대상 |
|---|---|
| **되돌림 완료 (실행됨)** | ① 노란불(safety-stop) 자동복구 `145ea68`(+merge `d0c8c98`) → **server PR #5** (`fix/revert-safe-stop-recovery`, base main) |
| **유지 (사용자 확정)** | 카메라/ws 3건 `f759cb9` · `def2301` · `96a93b3` — 코드상 지연을 *감소*시키는 수정 |
| **유지 (사용자 명시)** | ros2-cup-stack `c346b99`/`6a8159a` joint-vel-limit 가드 (alarm 1908 예방) — 포인터 24c1ae3에 이미 포함, **건드리지 않음** |
| **유지 권장 (버그픽스/페어링)** | `6f5b0db` · `6c552bb` · `5366d73`(↔`bd5fa42` 페어) |
| **요청 시 되돌림 가능 (기능, 의존성 주의)** | `1e868a2` · `bd5fa42` · `4fdf4ad` · `727426d`/`cc0b27e` |

> 🔒 **가드레일**: ros2-cup-stack joint velocity-limit re-time(`c346b99`, runtime.py +88)은
> over-limit 궤적을 *애초에 막는 예방 fix*. server 노란불 복구(반응적 무마)를 제거하는 동안
> 이 예방 가드는 **반드시 유지**. ros2-cup-stack 포인터/코드는 이번 작업에서 변경하지 않음.

핵심 주의: 노란불 복구는 **단순 `git revert` 금지**. 이후 커밋들이 `145ea68`가 도입한
헬퍼 `_run_skill_call`에 의존하므로(아래 ①) **외과적 revert**가 필요.

---

## 1. 06-15 server 커밋 인벤토리 (최신→과거)

| # | 커밋 (merge) | 요약 | 분류 | 되돌리기 방법 | 충돌/위험 | 권장 |
|---|---|---|---|---|---|---|
| 12 | `145ea68` (`d0c8c98`) | safe-stop 노란불 자동복구 | feat | **외과적**(아래 ①) | 中–高 (드리프트+의존) | **되돌림(실행)** |
| 11 | `f759cb9` (`107e516`) | exo/cam payload 640w/q35 축소 | perf | param flip 또는 `git revert` | 低 | **유지** |
| 10 | `def2301` (`802ac3b`) | camera keepalive teardown 방지 | fix | `git revert` | 低 | **유지** |
| 9 | `96a93b3` (`9f3c884`) | 서버측 WS keepalive 비활성 | fix | `git revert`(clean) | 低 | **유지** |
| 8 | `4fdf4ad` | unstack_all skill(`/skill/unstack_all`) | feat | `git revert` | 中 (#5가 의존) | 요청 시 |
| 7 | `5366d73` | start.sh: cup_stack_agent 자동기동 중지 | chore | `git revert` | 低 | 유지(#4 페어) |
| 6 | `727426d` (`cc0b27e`) | outlier_cup_recovery TASK 등록 | feat | `git revert` | 中 (교차레포) | 요청 시 |
| 5 | `6c552bb` | unstack에 home-skip+grip-twist 전달 | fix | `git revert` | 低 (#8 위에 얹힘) | 유지 |
| 4 | `bd5fa42` | host bringup agent로 cup_stack_agent 기동 | feat | `git revert` | 中 (#7 페어) | 요청 시 |
| 3 | `1e868a2` | `POST /api/robot/stop` (skill 중단+HOME) | feat | `git revert` | 中 (FE `/stop` 호출 확인) | 요청 시 |
| 2 | `b8506f4` | start.sh: VISION_MODE=standalone 기본 | chore | env flip 또는 `git revert` | 低 | 유지 |
| 1 | `6f5b0db` | stop: rosapi/rosbridge orphan kill | fix | `git revert` | 低 | 유지(버그픽스) |

merge 커밋(`d0c8c98` 등)은 feature 커밋 외 추가 내용 없음 — feature 커밋 diff만 되돌리면 됨.

---

## ① 노란불 자동복구 `145ea68` — 외과적 되돌리기 (실행 대상)

### 왜 단순 revert 불가
- `145ea68`가 헬퍼 **`_run_skill_call`** 을 도입하고 기존 skill POST 4곳을
  `run_in_executor(None,_call)` → `self._run_skill_call(_call)` 로 전환.
- 이후 `4fdf4ad`(unstack_all) 등 **새 skill도 `_run_skill_call`을 호출** (현재 호출부
  4곳: 도메인 L1105/1557/1595/1631). `git revert`는 `_run_skill_call` 정의를 지워
  **later skill을 조용히 깨뜨림** (충돌로도 안 잡힘).
- routers/tests는 이후 `1e868a2`/`bd5fa42`/`4fdf4ad`가 인접 영역을 건드려
  reverse-apply 충돌.

### 의존성 확인 결과
- `recover_safe_stop` / `/api/robot/recover` 참조는 **server 내부에만** 존재.
  `cup_stack_agent` · `frontend` 어디서도 호출하지 않음 → 외부 비파괴.
- recovery 전용 심볼(`_ROBOT_STATE_NAMES`, `_RECOVER_CONTROL`, `_ROBOT_STATE_RUNNING`,
  `_get_robot_state`, `RECOVER_*`, `MOVE_RECOVER_*`)은 recovery 밖에서 미사용 → 안전 제거.
- pre-feature(parent `f10e964`)엔 `ROBOT_STATE_*` 상수 자체가 없었음 → 블록 통째 제거.

### 편집 명세
**`server/domains/robot.py`**
1. 상수 블록 **L86–129** (`# ── Safety-stop … ─` ~ `MOVE_RECOVER_VEL_SCALE`) 삭제.
   `MOVE_VEL`/`MOVE_ACC`(L130–131)는 `_build_move_req` 전용이면 함께 삭제(인라인 복원).
2. `_get_robot_state` (L566–585) 삭제.
3. `recover_safe_stop` (L587–671) 삭제.
4. `_run_skill_call` (L673–699) → **메서드 유지**, 본문을 pass-through로 축소:
   `loop = asyncio.get_running_loop(); return await loop.run_in_executor(None, call)`.
5. `_build_move_req` (L794–824) 삭제, `move_to` (L825–885)를 pre-feature 인라인
   단일 호출판으로 복원(재시도 루프/감속 재시도/`recovered` 제거). `_validate_target` 유지.

**`server/routers/robot.py`** — `RecoverResponse` import(L28) + `POST /recover`(L197–207) 삭제.
**`server/schemas.py`** — `MoveResponse.recovered`(L228) + `RecoverResponse`(L238–) 삭제
  (`git apply -R`로 clean revert 가능).
**`tests/test_robot_router.py`** — `145ea68` 추가 테스트클래스 3개 삭제:
  `TestMoveSafetyStopRecovery`, `TestRecoverEndpoint`, `TestSkillSafetyStopRecovery`.

### 검증
- `pytest` (recovery 테스트 제거 후 전체 green) · `python -m py_compile` · `ruff`(있으면).
- grep로 `_run_skill_call` 호출부 4곳 잔존 확인, `recover`/`RecoverResponse` 잔재 0 확인.

### 게시
- 브랜치 `fix/revert-safe-stop-recovery` (server) → push → PR base `main`.
- 머지 후 부모 레포에서 server 포인터 bump (`fix/…` 브랜치 + PR base `main`).

---

## ② 카메라/ws 3건 — 유지 (확정), 되돌릴 경우 절차만 기록

> ⚠️ 이들은 지연을 *유발*이 아니라 *감소*시키는 수정. 되돌리면 800w/q50 큰 페이로드 +
> keepalive_ping AssertionError로 인한 ~1s 주기 카메라 끊김 재발.

- `f759cb9` `server/services/camera.py`: `target_width 640→800`, `jpeg_quality 35→50` 되돌리기(param flip) 또는 `git revert`.
- `def2301` `server/services/camera.py`: 클라 `ping_interval=None`+recv 10s 재접속 → `git revert`.
- `96a93b3` `server/entrypoints/{handineye,handtoeye,robot}.py`: `ws_ping_interval=None` 3곳 → `git revert`(clean).

---

## ③ 기능/기타 커밋 — 요청 시 되돌리기 (의존성 메모)

- `1e868a2 POST /stop`: 되돌리기 전 frontend `/api/robot/stop` 호출 여부 확인(있으면 FE도 함께).
- `bd5fa42`(bringup agent) ↔ `5366d73`(start.sh 자동기동 중지)는 **페어** — 함께 되돌려야 일관.
- `4fdf4ad`(unstack_all) ← `6c552bb`가 그 위에 얹힘 — 되돌리면 `6c552bb`부터 역순.
- `727426d`(outlier TASK 등록)는 ros2-cup-stack `df6484d`(launch wrapper) + outlier-cup-recovery
  레포에 걸침 — 교차레포, server 단독 되돌리면 dangling launch 참조 주의.

---

## ④ 실행 순서

1. **(지금)** ① 노란불 복구 외과적 되돌리기 → server `fix/revert-safe-stop-recovery` → PR.
2. PR 머지 후 부모 포인터 bump PR.
3. ②·③은 사용자 확정 후 동일 워크플로(개별 `fix/…` 브랜치 + PR)로 진행.
