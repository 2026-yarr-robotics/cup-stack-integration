# 06-15 Revert PR 일괄 머지 안내 (2026-06-16)

06-15 server 변경 되돌리기 작업의 **6개 PR**을 일관되게 한 번에 머지하는 절차.
배경/원리 ledger는 **integration PR #4 본문** 참조.

## 대상 PR (6개)

| # | 레포 | 브랜치 | 내용 | 상태 |
|---|---|---|---|---|
| [5](https://github.com/2026-yarr-robotics/server/pull/5) | server | `fix/revert-safe-stop-recovery` | 노란불 자동복구 제거 | leaf |
| [6](https://github.com/2026-yarr-robotics/server/pull/6) | server | `fix/revert-stop-and-bringup-agent` | /stop + bringup 제거 | leaf |
| [2](https://github.com/2026-yarr-robotics/frontend/pull/2) | frontend | `fix/revert-stopall-abort` | stopAll/Abort 되돌리기 | leaf |
| [9](https://github.com/2026-yarr-robotics/ros2-cup-stack/pull/9) | ros2-cup-stack | `fix/revert-skill-api-stop` | skill_api /stop 제거 | leaf |
| [5](https://github.com/2026-yarr-robotics/cup-stack-integration/pull/5) | integration | `fix/revert-agent-done-shutdown` | 에이전트 done 자가종료 제거 | parent own-code |
| [4](https://github.com/2026-yarr-robotics/cup-stack-integration/pull/4) | integration | `chore/track-submodule-main` | 서브모듈 main 추적 | parent config |

## 묶음(함께 머지해야 일관)

- **/stop 완전 제거** = server #6 + frontend #2 + ros2 #9 → 셋이 함께 가야 한쪽만 빠진 깨진 상태(버튼 404 / orphan 호출)가 안 생김.
- **상시기동 복원** = server #6 + integration #5 → 같이 가야 "에이전트 안 뜸 / done에 자가종료" 불일치 없음.
- server #5(노란불)는 독립.

## 순서

### 1단계 — leaf 서브모듈 PR 머지 (각 서브모듈 main)
```bash
gh pr merge 5 --repo 2026-yarr-robotics/server         --merge   # 노란불
gh pr merge 6 --repo 2026-yarr-robotics/server         --merge   # /stop+bringup  (⚠️ 아래 주의)
gh pr merge 2 --repo 2026-yarr-robotics/frontend       --merge
gh pr merge 9 --repo 2026-yarr-robotics/ros2-cup-stack --merge
```
> ⚠️ **server #5 ↔ #6 충돌 주의**: 둘 다 같은 base(6f5b0db)에서 갈라져 `robot.py`/`schemas.py`/`routers/robot.py`(특히 import 목록)의 인접 영역을 건드림. 단독으로는 둘 다 CLEAN이지만 **먼저 하나 머지 후 나머지는 "Update branch"/`git merge main`로 충돌 해소** 필요할 수 있음(대개 import 줄 인접 수준). **#5 먼저 → #6** 권장.

### 2단계 — integration own-code/config PR 머지 (서브모듈 무관, 바로 가능)
```bash
gh pr merge 5 --repo 2026-yarr-robotics/cup-stack-integration --merge   # plan_executor (cup_stack_agent)
gh pr merge 4 --repo 2026-yarr-robotics/cup-stack-integration --merge   # .gitmodules branch=main
```
> #4와 #5는 서로 다른 파일(.gitmodules vs plan_executor_node.py)이라 충돌 없음.

### 3단계 — 서브모듈 포인터 bump (1단계 leaf 머지 **완료 후**)
#4가 머지되어 `branch=main`이 적용되면 한 명령으로 최신 main 팁 추종:
```bash
cd /home/ssu/cup-stack-integration
git checkout main && git pull
git submodule update --remote --merge          # server·frontend·ros2-cup-stack 등을 각 main 최신으로
git add server frontend ros2-cup-stack
git commit -m "chore(submodules): bump pointers after 06-15 reverts"
git push
```

## 검증(머지 후)
- server: `/api/robot/recover`·`/api/robot/stop` 404(제거됨), `task/stop` 정상, `send_user_command`(/user_command publish) 동작, fallen-cup tilt 유지.
- frontend: Abort 버튼이 `task/stop`로 동작(빌드 OK).
- 에이전트: done에서 자가종료 안 함(상시기동), #7 done-grace 정상.
- 유지 확인: 카메라/ws(지연↓), ros2 vel-guard(`c346b99`), outlier/unstack.
