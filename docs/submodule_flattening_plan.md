# 서브모듈 평탄화(flatten) & cup-stack-server 해체 계획

> 작성 브랜치: `chore/flatten-submodules` (worktree, `main` 기준)
> 최초 작성 2026-06-12 · **개정 2026-06-12** (전략 A 확정: cup-stack-server 해체)
> 목적: `cup-stack-integration` 의 중첩 서브모듈을 루트 단일 계층으로 평탄화하고,
> `cup-stack-server` 집합 레포를 해체한다. org 레포 기준 사용 레포만 루트 서브모듈로 둔다.

---

## 0. 확정된 결정 (이번 개정)

- ✅ **전략 A 채택** — `cup-stack-server` **배포 단위 해체**. 모든 leaf 서브모듈을 integration 루트로.
- ✅ **`cup-stack-server` 레포 미사용** — integration 의 서브모듈에서 제거. (leaf 들은 루트로 직접 등록)
- ✅ **`cup-stack-server/script/` → integration 루트 `script/server/` 로 이동.**
- ✅ `LLM-prompting`(archived·중복) 제거, `ros2-skill-manager` 정식 서브모듈 등록.
- ✅ **등록 검토 범위 = org 의 모든 active 레포**(아카이브 레포 제외). 전수 검토는 §3-A.
- ⚠️ **충돌 플래그**: `ros2-recode-sequence` 는 **아카이브 + 사용중**. "아카이브 제외" 규칙과
  "런타임 사용" 이 충돌 → §3-A 및 §9-7 에서 별도 결정 필요.

---

## 1. 목적 & 범위

- **평탄화 + 해체**: 2~3단 중첩 + cup-stack-server 집합 계층을 걷어내고, 루트에 leaf 레포를 평평하게 둔다.
- **사용 레포만 등록**: [org 레포 목록](https://github.com/orgs/2026-yarr-robotics/repositories) 기준.
- **제거 후보 식별**: 아카이브 서브모듈, 미사용 코드·문서, stale 브랜치.
- 이 문서는 **계획**이다. 실제 `git rm`/`submodule add`/경로 수정은 승인 후 별도 단계.

---

## 2. 현재 구조 (as-is)

```
cup-stack-integration/                                   [repo @ main]
├── cup_stack_agent/                                     ← 자체 코드 (서브모듈 아님)
├── tools/
│   ├── run_skill_manager.sh
│   └── ros2-skill-manager/                              ← ros2-skill-manager.git clone (.gitignore, 서브모듈 아님)
├── docs/
├── vision_rviz.sh
├── build/ install/ log/                                 ← colcon 산출물(.gitignore)
├── cup-stack-server/                          [submodule → cup-stack-server.git]  ⚠️ 해체 대상
│   ├── CLAUDE.md, pull.sh, .gitmodules
│   ├── script/  (build_pyramid*.sh, cycle_*.sh, unstack*.sh)   ← script/server/ 로 이동
│   ├── docs/    (*.docx, integration/*.md)
│   ├── cup_stack/ros2/                                  ← root:root 잔재(빌드 마운트 추정)
│   ├── server/                                [submodule → server.git]   (docker-compose)
│   ├── ros2-cup-stack/                        [submodule → ros2-cup-stack.git]  (colcon ws)
│   │   └── ros2/src/doosan-robot2/            [submodule → doosan-robot2.git @ yarr-integration]
│   ├── frontend/                              [submodule → frontend.git]
│   ├── fallen-cup-recovery/                   [submodule → fallen-cup-recovery.git @ released]
│   └── LLM-prompting/                         [submodule → LLM-prompting.git] ⚠️ ARCHIVED·제거
└── vision/
    ├── ros2-depth-point-cloude/               [submodule]
    ├── vision-node/                           [submodule]
    └── ros2-recode-sequence/                  [submodule] ⚠️ ARCHIVED(but 사용중·frozen)
```

> `yarr-robust-speed-stack` 서브모듈은 직전 작업에서 제거 완료.

---

## 2-bis. 의존성 트리 & 코드 사용 범위 (결정 근거)

### 런타임 의존성 트리 (closed loop)

```
user command
   │
   ▼
cup_stack_agent  ───────────────────────────────────────────────┐  (이 레포 자체 코드)
  ├─HTTP─▶ server (REST: /api/robot/skill/pyramid|unstack, /move) │
  │          └─rosbridge─▶ ros2-cup-stack (cup_stack skill, MoveIt)
  │                           └─colcon src─▶ doosan-robot2 (M0609 드라이버)
  ├─sub◀── ros2-depth-point-cloude (depth_digital_twin: /digital_twin/boxes, detection)
  ├─sub◀── vision-node (verifier_node: /stack 슬롯 판정)
  ├─launch─ ros2-recode-sequence (recode_sequence: cameras_only.launch.py — 카메라 bringup)
  └─prompts─ cup_stack_agent/prompts/*  (← 구 LLM-prompting, 이관 완료)

frontend ──REST/ws──▶ server                         (대시보드)
fallen-cup-recovery ──▶ ros2-cup-stack / server      (복구 skill, @released)
ros2-skill-manager ──REST──▶ server                  (오퍼레이터 GUI, 별도 실행·메타 launch 미포함)

[offline] vision-YOLO ──.pt 모델──▶ ros2-depth-point-cloude / vision-node   (학습 산출물만 소비)
[absorbed] hand_pick ──▶ cup_stack_agent (pick_node/upright_cup_pose_node 로 inline)
```

### 코드 사용 범위 요약

| 레포 | 사용 면(surface) | 결합 깊이 | 런타임? |
|---|---|---|---|
| `server` | REST/rosbridge 게이트웨이 | docker-compose · `../vision` 역참조 | ✅ |
| `ros2-cup-stack` | cup_stack skill, MoveIt 실행 | colcon ws + doosan-robot2 src | ✅ |
| `doosan-robot2` | 로봇 드라이버 | ros2-cup-stack `src/` bound | ✅ |
| `ros2-depth-point-cloude` | `/digital_twin/boxes` 등 토픽 | install/setup.bash 소싱 | ✅ |
| `vision-node` | `/stack` verifier | install/setup.bash 소싱 | ✅ |
| `ros2-recode-sequence` | `cameras_only.launch.py` | install/setup.bash 소싱 | ✅ (but archived → 병합) |
| `frontend` | 대시보드 UI | server compose | ✅(보조) |
| `fallen-cup-recovery` | 복구 skill | ros2-cup-stack/server | ✅ |
| `ros2-skill-manager` | 오퍼레이터 GUI | server REST(별도 실행) | ✅(도구) |
| `LLM-prompting` | (런타임 미사용) | 프롬프트 cup_stack_agent 로 이관 | ❌ |
| `hand_pick` | (런타임 미사용) | cup_stack_agent 로 흡수 | ❌ |
| `vision-YOLO` | 모델 학습(오프라인) | 산출물 .pt 만 소비 | ❌(런타임) |
| Isaac 계열 | 별도 통합/실험 | 무관 | ❌ |

**정리 원칙(이 트리에서 도출):**
1. **런타임 토픽/REST/colcon 으로 직접 엮인 것만** 서브모듈로 둔다 → server, ros2-cup-stack(+doosan-robot2), ros2-depth-point-cloude, vision-node, fallen-cup-recovery, ros2-skill-manager.
2. **이관(병합)으로 트리에서 사라질 수 있는 것** → LLM-prompting·hand_pick(이미 cup_stack_agent 흡수), ros2-recode-sequence(→depth 로 흡수).
3. **오프라인/병렬트랙** → vision-YOLO·Isaac 계열은 서브모듈 제외.

---

## 3. Org 레포 전수 검토 & 서브모듈 결정

### 3-A. 모든 active 레포 등록 검토 (아카이브 제외)

> 규칙: **아카이브 레포는 등록 대상에서 제외.** 아래는 org 의 active 레포 전수.
> (참고용으로 archived 레포도 하단에 분리 표기)

| 레포 | 상태 | 결정 | 위치(to-be) | 근거 |
|---|---|---|---|---|
| `cup-stack-integration` | active | **self** | (루트) | 이 통합 레포 자신 |
| `cup-stack-server` | active | **제거(해체)** | — | 집합 레포 미사용. leaf 를 루트로 직접 등록 |
| `server` | active | 루트 등록 | `server/` | FastAPI/rosbridge, docker-compose |
| `ros2-cup-stack` | active | 루트 등록 | `ros2-cup-stack/` | cup_stack ROS pkg, pyramid/unstack skill |
| `frontend` | active | 루트 등록 | `frontend/` | React 대시보드 |
| `fallen-cup-recovery` | active | 루트 등록 | `fallen-cup-recovery/` @released | 쓰러진 컵 복구 |
| `doosan-robot2` | active | **중첩 유지** | `ros2-cup-stack/ros2/src/doosan-robot2` @yarr-integration | colcon `src/` bound — 루트로 못 뺌 |
| `ros2-depth-point-cloude` | active | 루트 등록 | `ros2-depth-point-cloude/` | perception detection+3D boxes |
| `vision-node` | active | 루트 등록 | `vision-node/` | `/stack` verifier |
| `ros2-skill-manager` | active | **신규 등록** | `tools/ros2-skill-manager/` | 오퍼레이터 GUI. ignore clone → 승격 |
| `hand_pick` | active | **병합 권장**(미등록) | → `cup_stack_agent` | 기능 inline 흡수됨, 트리 참조 0건 (§3-B) |
| `vision-YOLO` | active | **등록 보류** | — | 모델 학습 레포(런타임 코드 아님). in-tree 필요 시 `training/` 로 등록 |
| `cup-stack-integration-isaac` | active | **제외** | — | 병렬 통합 레포. 서브모듈로 중첩 부적합(형제 프로젝트) |
| `yarr-isaac-playground` | active | **제외** | — | Isaac sandbox. 별도 트랙 |
| **archived (등록 제외)** | | | | |
| `ros2-recode-sequence` | archived | **병합 후 제거** | → `ros2-depth-point-cloude` 로 코드 이관 | 사용중이나 아카이브. 코드 이관으로 충돌 해소 (§3-B) |
| `LLM-prompting` | archived | **병합완료·제거** | (이미 `cup_stack_agent/prompts`) | 프롬프트 이관 완료 (§3-B) |
| `yarr-robust-speed-stack(-v2)`, `test-yarr-cup-stack` | archived | 대상 외 | — | dead |

### 3-B. 서브모듈 병합 가능성 검토 (코드 이관 → 서브모듈 소거)

> 목표: 독립 유지가치가 낮은 서브모듈은 **코드를 active 레포로 이관**하고 서브모듈 자체를 없앤다.

| 서브모듈 | 병합 가능? | 이관 대상 | 작업 내용 | 효과 |
|---|---|---|---|---|
| `ros2-recode-sequence`(archived) | ✅ **권장** | `ros2-depth-point-cloude` | `recode_sequence` 패키지(`cameras_only.launch.py`, `cameras.yaml`, playback/sequence 노드)를 depth 레포 colcon ws 의 2번째 pkg 로 이동. **패키지명 `recode_sequence` 유지** → `ros2 launch recode_sequence cameras_only.launch.py` 그대로 동작 | 아카이브 서브모듈 제거 + §0 충돌 해소 |
| `LLM-prompting`(archived) | ✅ **완료** | `cup_stack_agent` | 런타임 프롬프트는 이미 `cup_stack_agent/prompts/*`. 잔여(executor/benchmarks/legacy)는 미사용 | 서브모듈 단순 제거 |
| `hand_pick` | ✅ 가능 | `cup_stack_agent` | hand-eye pick 은 이미 `pick_node.py`/`upright_cup_pose_node.py` 로 inline. 추가 이관 불필요 | 등록 안 하고 종료 |
| `vision-node` ↔ `ros2-depth-point-cloude` | △ 가능하나 보류 | (단일 `vision` 레포) | 두 perception 패키지를 한 레포로 통합 가능하나 **독립 개발/릴리스 결합** 비용. 당장은 분리 유지 | 차후 검토 |
| `server` / `ros2-cup-stack` / `frontend` | ❌ | — | 배포 컴포넌트(서로 다른 런타임·언어·compose). 병합 부적합 | 분리 유지 |
| `doosan-robot2` | ❌ | — | upstream fork mirror. 병합 시 upstream sync 단절 | 분리 유지 |

**병합 후 최종 vision 관련 서브모듈**: `ros2-depth-point-cloude`(+recode_sequence 패키지 흡수), `vision-node` 2개만 남음. `ros2-recode-sequence` 서브모듈은 소멸.

---

## 4. 핵심 제약 (해체 시 반드시 동반 수정)

1. **`doosan-robot2` 중첩 유지** — `ros2-cup-stack/ros2/src/doosan-robot2`. colcon `src/` 결합. 루트로 빼면 빌드 깨짐. **유일하게 평탄화 제외.**
2. **`server/start.sh` 상대경로 전면 재계산** — 현재 `$SCRIPT_DIR/../../vision/ros2-recode-sequence` 는 (server 가 `cup-stack-server/server/` 라서) integration 루트 vision/ 을 가리킴. 해체 후 server 가 `integration/server/` 로 올라가고 vision/ 이 루트로 풀리면:
   - `../../vision/ros2-recode-sequence` → **`../ros2-recode-sequence`**
   - cup_stack/ros2-cup-stack setup.bash 소싱 경로(`../../...`)도 한 단계 줄여 재계산
   - 대상: `server/start.sh`(100,174,216 등), `server/stop.sh`, `vision_rviz.sh`
3. **`pull.sh` 폐기** — `cd ./server && git pull ...` 식 수동 동기화. 루트 서브모듈화 후 `git submodule update --remote` 로 대체.
4. **`ros2-recode-sequence` archived** — frozen 유지(사용중). 제거 금지.
5. **배포 단위 이관** — 라이브 Docker 는 별도 체크아웃 `/home/ssu/cup-stack`(= cup-stack-server). integration 에서 해체해도 그 배포가 자동으로 바뀌지 않음 → **배포 측 마이그레이션은 별도 후속**(§9-1).

---

## 5. 목표 구조 (to-be, 전략 A)

```
cup-stack-integration/
├── cup_stack_agent/                          [own code]
├── script/
│   ├── run_skill_manager.sh                  (← tools/ 에서 통합, 선택)
│   └── server/                               ★ cup-stack-server/script/* 이동
│       ├── build_pyramid.sh  build_pyramid_nested.sh
│       ├── cycle_grid.sh     cycle_nested.sh
│       └── unstack.sh        unstack_grid.sh
├── server/                                   [submodule]
├── ros2-cup-stack/                           [submodule]
│   └── ros2/src/doosan-robot2/               [submodule, 중첩 유지]
├── frontend/                                 [submodule]
├── fallen-cup-recovery/                      [submodule @released]
├── ros2-depth-point-cloude/                  [submodule] (+recode_sequence 패키지 흡수)
├── vision-node/                              [submodule]
├── tools/ros2-skill-manager/                 [submodule 신규]
└── docs/
```

해체/병합으로 사라지는 것: `cup-stack-server/`(집합 계층), `vision/`(계층),
`LLM-prompting`(서브모듈, 흡수완료), `ros2-recode-sequence`(서브모듈, depth 로 병합),
`pull.sh`, `cup_stack/ros2/`(잔재). cup-stack-server 의 `docs/`·`CLAUDE.md` 처리는 §7.

> 최종 루트 서브모듈(8): `server`, `ros2-cup-stack`(+doosan-robot2 중첩), `frontend`,
> `fallen-cup-recovery`, `ros2-depth-point-cloude`(+recode_sequence), `vision-node`,
> `tools/ros2-skill-manager`.

---

## 6. 마이그레이션 단계 (전략 A)

> worktree(`chore/flatten-submodules`)에서. 서브모듈 내부 커밋은 `dwl21 <nggus5@gmail.com>`, 최상위는 기본 author.
> ⚠️ 서브모듈을 부모(cup-stack-server)에서 떼어 조부모(integration)로 옮기는 작업이라
> **`.git/modules` 경로와 `.gitmodules` 양쪽을 모두 손봐야 한다.** 가장 안전한 방식은
> "deinit → 상위에서 add 재등록" 이다.

**(0) 사전 보존**
```bash
# cup-stack-server/script 와 docs 를 작업트리로 복사해 둠(이동 소스)
```

**(1) cup-stack-server 의 script → 루트 script/server**
```bash
mkdir -p script/server
git mv cup-stack-server/script/* script/server/     # cup-stack-server 가 아직 서브모듈이면 일반 cp 후 add
```
> cup-stack-server 가 서브모듈이라 `git mv` 가 경계를 못 넘으면: `cp` → integration 에 `git add script/server`, 원본은 (3)에서 통째 제거.

**(2) leaf 서브모듈을 루트로 재등록** (각각: 현 포인터 SHA 기록 → deinit)
```bash
# 현재 포인터 기록
git -C cup-stack-server submodule status   # server/ros2-cup-stack/frontend/fallen-cup-recovery SHA
# integration 에 루트 서브모듈로 추가 (기록한 SHA 로 checkout)
git submodule add https://github.com/2026-yarr-robotics/server.git server
git submodule add https://github.com/2026-yarr-robotics/ros2-cup-stack.git ros2-cup-stack
git submodule add https://github.com/2026-yarr-robotics/frontend.git frontend
git submodule add -b released https://github.com/2026-yarr-robotics/fallen-cup-recovery.git fallen-cup-recovery
# 각 서브모듈을 기록한 SHA 로 set
# ros2-cup-stack 의 doosan-robot2 는 그 안의 .gitmodules 로 자동 따라옴(submodule update --init --recursive)
```

**(3) vision/ 계층 해체** — 동일 방식으로 루트 재등록
```bash
git submodule add https://github.com/2026-yarr-robotics/ros2-depth-point-cloude.git ros2-depth-point-cloude
git submodule add https://github.com/2026-yarr-robotics/vision-node.git vision-node
git submodule add https://github.com/2026-yarr-robotics/ros2-recode-sequence.git ros2-recode-sequence
```

**(4) cup-stack-server · vision/ · LLM-prompting 제거**
```bash
git submodule deinit -f cup-stack-server && git rm -f cup-stack-server && rm -rf .git/modules/cup-stack-server
git submodule deinit -f vision/ros2-depth-point-cloude vision/vision-node vision/ros2-recode-sequence
git rm -f vision/ros2-depth-point-cloude vision/vision-node vision/ros2-recode-sequence
rmdir vision 2>/dev/null
# LLM-prompting 는 cup-stack-server 제거로 함께 사라짐
```

**(5) ros2-skill-manager 정식 등록**
```bash
git rm -r --cached tools/ros2-skill-manager 2>/dev/null || true   # ignore clone 해제
# .gitignore 의 /tools/ros2-skill-manager/ 라인 삭제
git submodule add https://github.com/2026-yarr-robotics/ros2-skill-manager.git tools/ros2-skill-manager
```

**(6) 경로 수정 (핵심)**
- `server/start.sh`, `server/stop.sh` — `../../vision/X` → `../X`, cup_stack/ros2-cup-stack 소싱 경로 한 단계 축소
- `vision_rviz.sh`, `tools/run_skill_manager.sh` — vision/·tools 경로 점검
- 루트 `CLAUDE.md` — 구조도/경로 서술 전면 갱신
- `pull.sh` 삭제(또는 `git submodule update --remote` wrapper 로 대체)

**(7) 검증**
```bash
git submodule status --recursive
python3 -m py_compile cup_stack_agent/scripts/*.py
for f in server/start.sh server/stop.sh script/server/*.sh tools/run_skill_manager.sh vision_rviz.sh; do bash -n "$f"; done
grep -rn "cup-stack-server\|\.\./\.\./vision" --include='*.sh' --include='*.md' .   # 잔존 참조 0 확인
```

---

## 7. 제거 후보 (코드 · 문서)

| 후보 | 사유 | 권장 |
|---|---|---|
| `cup-stack-server` 서브모듈 | 집합 레포 해체 | **제거** |
| `LLM-prompting` 서브모듈 | archived + 프롬프트 이관됨 | **제거** (§6-1 선결확인) |
| `cup-stack-server/pull.sh` | 수동 submodule pull, submodule update 로 대체 | **삭제** |
| `cup-stack-server/cup_stack/ros2/` | root:root 잔재(빌드 마운트 추정), 트리 비결합 | **삭제 후보**(확인 후) |
| `cup-stack-server/docs/integration/yarr-robust-speed-stack-integration.md` | 이미 제거된 dead 레포 통합 문서 | **삭제** |
| `cup-stack-server/docs/*.docx`, `integration/*.md`(나머지) | 프로젝트 문서·통합 노트 | integration `docs/` 로 **이관** 또는 보존 결정 |
| `cup-stack-server/CLAUDE.md` | 해체되면 무주공산 | 유용분은 루트 CLAUDE.md 로 흡수 |
| `server/start.sh.bak_disable_dt_panel` | `.bak` 잔재 | **삭제 후보** |
| 루트 `build/ install/ log/` | colcon 산출물 | 이미 ignore, 디스크 정리만 |

---

## 8. 브랜치 정리 후보 (stale)

| 레포 | 정리 후보 | 비고 |
|---|---|---|
| `cup-stack-integration` | `tmp`, `vision-integration` | `tmp`=WIP, `vision-integration`=main 병합 확인 |
| `server` | `worktree-fix-joint-ee-risks`, `worktree-swagger-nginx-route` | worktree 잔재 |
| `ros2-cup-stack` | `worktree-add-pyramid-unstack-loop` | worktree 잔재 |
| `doosan-robot2` | `auto-sync-jazzy-*`(6), `feature/*`, `fix/issue-345/346`, `foxy-devel`, `master` | fork 노이즈. 사용=`yarr-integration`. fork mirror 정책이면 보존 |
| `LLM-prompting` | `feat/llm-demo-and-noreplan-prompt` | 레포 archived |
| `ros2-skill-manager` | `feat/settled-gate` | merge 확인 |
| 다수 | `isaac` | Isaac 트랙 — **삭제 금지** |

> 삭제 전 main 병합 여부 개별 확인. `git push origin --delete <branch>`.

---

## 9. 리스크 & 미결정 사항

1. **배포 마이그레이션** — 라이브 Docker 가 `/home/ssu/cup-stack`(cup-stack-server) 에서 구동. integration 해체 후 배포를 어디서 띄울지(integration 직접 vs server 단독 체크아웃) 결정 필요. server 의 docker-compose 빌드 컨텍스트가 형제 경로 가정 시 영향.
2. **`LLM-prompting` 제거 확정** — `cup_stack_agent/prompts/*` 완전 대체 확인.
3. **`hand_pick` 처리** — inline 흡수로 제외 vs 정식 편입.
4. **cup-stack-server `docs/` 처리** — integration `docs/` 이관 vs 폐기.
5. **`doosan-robot2` 브랜치 정책** — fork mirror 보존 vs `yarr-integration`+`humble` 만.
6. **포인터 분기** — server/ros2-cup-stack/frontend 를 다른 곳(예: 배포 체크아웃)에서도 참조 시 SHA 동기화 주체 명확화.

---

## 10. 실행 체크리스트

- [ ] script → `script/server/` 이동(§6-1)
- [ ] leaf 4종 루트 재등록 + 포인터 SHA 보존(§6-2)
- [ ] vision/ 3종 루트 재등록(§6-3)
- [ ] cup-stack-server·vision/·LLM-prompting 제거(§6-4)
- [ ] ros2-skill-manager 서브모듈 등록(§6-5)
- [ ] **경로 수정**: start.sh/stop.sh/vision_rviz.sh/CLAUDE.md, pull.sh 삭제(§6-6)
- [ ] docs 이관/정리, .bak·cup_stack 잔재 정리(§7)
- [ ] stale 브랜치 개별 병합확인 후 삭제(§8)
- [ ] `submodule status --recursive` + py_compile + `bash -n` + 잔존참조 grep(§6-7)
- [ ] 배포 마이그레이션 방침 확정(§9-1)

---

## 11. 최종 결정 요약 (TL;DR)

의존성 트리(§2-bis)와 코드 사용 범위로부터 도출한 최종 결정:

**(A) cup-stack-server 해체** — 집합 레포 미사용. leaf 를 integration 루트로 직접 등록.
`cup-stack-server/script/` → `script/server/`, `pull.sh`·`cup_stack/ros2/` 잔재 제거.

**(B) 최종 루트 서브모듈 8개** (런타임 직접 결합만):
`server` · `ros2-cup-stack`(→`ros2/src/doosan-robot2` 중첩) · `frontend` ·
`fallen-cup-recovery`@released · `ros2-depth-point-cloude`(+recode_sequence 흡수) ·
`vision-node` · `tools/ros2-skill-manager`(신규).

**(C) 병합으로 소거**:
- `ros2-recode-sequence`(archived) → `ros2-depth-point-cloude` 로 `recode_sequence` 패키지 이관(패키지명 유지). 아카이브 충돌 해소.
- `LLM-prompting`(archived) → `cup_stack_agent/prompts` 로 이관 완료, 서브모듈 제거.
- `hand_pick` → `cup_stack_agent` 로 inline 흡수 완료, 미등록.

**(D) 제외**: `vision-YOLO`(오프라인 학습), `cup-stack-integration-isaac`·`yarr-isaac-playground`(병렬 트랙), 모든 archived 레포.

**(E) 동반 필수**: `server/start.sh|stop.sh` 의 `../../vision/*`→`../*` 경로 재계산, 루트 `CLAUDE.md` 갱신, stale 브랜치 정리(§8).

**(F) 남은 결정**(§9): 배포 마이그레이션 위치, doosan-robot2 fork 브랜치 정책.
```
