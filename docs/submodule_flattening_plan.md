# 서브모듈 평탄화(flatten) & 정리 계획

> 작성 브랜치: `chore/flatten-submodules` (worktree, `main` 기준)
> 작성일: 2026-06-12
> 목적: `cup-stack-integration` 의 중첩 서브모듈을 루트로 정리하고, org 레포 기준으로
> (1) 실제 사용하는 레포만 서브모듈로 등록, (2) 미사용 코드·문서·브랜치를 제거 후보로 식별.

---

## 1. 목적 & 범위

- **평탄화**: 현재 2~3단계로 중첩된 서브모듈을 `cup-stack-integration` 루트(또는 의미 있는 단일 계층)로 끌어올린다.
- **사용 레포 재등록**: [org 레포 목록](https://github.com/orgs/2026-yarr-robotics/repositories) 기준으로 *실제 런타임/빌드에 쓰이는* 레포만 서브모듈로 둔다.
- **제거 후보 식별**: 아카이브된 레포 서브모듈, 미사용 코드·문서, stale 브랜치를 골라낸다.
- 이 문서는 **계획만** 담는다. 실제 `git rm`/`git submodule add` 실행은 승인 후 별도 단계.

---

## 2. 현재 구조 (as-is, 재귀 서브모듈 트리)

```
cup-stack-integration/                                   [repo @ main]
├── cup_stack_agent/                                     ← 이 레포 자체 코드 (서브모듈 아님)
├── tools/
│   ├── run_skill_manager.sh                             ← wrapper (tracked)
│   └── ros2-skill-manager/                              ← ros2-skill-manager.git clone (★ .gitignore 처리, 서브모듈 아님)
├── docs/                                                ← 비어 있음 (.gitkeep)
├── vision_rviz.sh
├── build/  install/  log/                               ← colcon 산출물 (.gitignore, 미추적)
├── cup-stack-server/                          [submodule → cup-stack-server.git]
│   ├── CLAUDE.md, docs/, script/, pull.sh, cup_stack/   ← 자체 파일
│   ├── server/                                [submodule → server.git]   (docker-compose 포함)
│   ├── ros2-cup-stack/                        [submodule → ros2-cup-stack.git]  (colcon ws)
│   │   └── ros2/src/doosan-robot2/            [submodule → doosan-robot2.git @ yarr-integration]
│   ├── frontend/                              [submodule → frontend.git]
│   ├── fallen-cup-recovery/                   [submodule → fallen-cup-recovery.git @ released]
│   └── LLM-prompting/                         [submodule → LLM-prompting.git] ⚠️ ARCHIVED
└── vision/
    ├── ros2-depth-point-cloude/               [submodule → ros2-depth-point-cloude.git]
    ├── vision-node/                           [submodule → vision-node.git]
    └── ros2-recode-sequence/                  [submodule → ros2-recode-sequence.git] ⚠️ ARCHIVED(but 사용중)
```

> 참고: `yarr-robust-speed-stack` 서브모듈은 직전 작업에서 이미 제거됨(미사용 dead scaffold).

---

## 3. Org 레포 인벤토리 & 분류

| 레포 | 상태 | 현재 서브모듈? | 사용 판정 | 근거 |
|---|---|---|---|---|
| `cup-stack-server` | active | ✅ (root) | **사용 (deploy unit)** | 라이브 배포 단위. server+ros2-cup-stack+frontend 집합체 |
| `server` | active | ✅ (중첩) | **사용 (runtime)** | FastAPI/rosbridge, `docker-compose.yml` 보유 |
| `ros2-cup-stack` | active | ✅ (중첩) | **사용 (runtime)** | cup_stack ROS pkg, pyramid/unstack skill, colcon ws |
| `frontend` | active | ✅ (중첩) | **사용** | React 대시보드 (compose가 빌드) |
| `fallen-cup-recovery` | active | ✅ (중첩, `released`) | **사용** | 쓰러진 컵 복구 통합 |
| `doosan-robot2` | active | ✅ (3단 중첩, `yarr-integration`) | **사용** | Doosan 드라이버. **colcon `ros2/src/` 에 bound** |
| `ros2-depth-point-cloude` | active | ✅ (vision/) | **사용 (perception)** | detection + 3D boxes |
| `vision-node` | active | ✅ (vision/) | **사용** | `/stack` 슬롯 verifier |
| `ros2-recode-sequence` | **archived** | ✅ (vision/) | **사용중(frozen)** | `server/start.sh:174` 가 `cameras_only.launch.py` 소싱 |
| `ros2-skill-manager` | active | ❌ (.gitignore clone) | **사용** | 오퍼레이터 GUI. `tools/run_skill_manager.sh` 가 실행 → **서브모듈 등록 후보** |
| `LLM-prompting` | **archived** | ✅ (중첩) | **미사용(중복)** | 프롬프트는 `cup_stack_agent/prompts/*` 가 런타임. 본체는 연구 레포 → **제거 후보** |
| `hand_pick` | active | ❌ | **미사용(추정)** | 트리 내 `hand_pick` 참조 0건. hand-eye pick 은 `cup_stack_agent/{pick_node,upright_cup_pose_node}.py` 에 inline |
| `vision-YOLO` | active | ❌ | **참조용(모델 학습)** | YOLO seg 학습 파이프라인. 코드가 아니라 모델 산출물 출처. 문서 URL 참조만 → 서브모듈 불필요 |
| `cup-stack-integration-isaac` | active | ❌ | **별도 통합** | Isaac sim 변형. 형제 레포 (서브모듈 아님) |
| `yarr-isaac-playground` | active | ❌ | **별도 sandbox** | Isaac 실험장 |
| `yarr-robust-speed-stack` | archived | ❌(제거됨) | 미사용 | dead meta repo |
| `yarr-robust-speed-stack-v2` | archived | ❌ | 미사용 | 구 통합본 |
| `test-yarr-cup-stack` | archived | ❌ | 미사용 | 테스트 잔재 |

### 3-1. 추가 서브모듈로 등록할 목록 (사용자 추가 조사 요청 답)

- **`ros2-skill-manager`** → **등록 권장**. 현재 `tools/ros2-skill-manager/` 에 clone 으로만 존재하고 `.gitignore` 됨. 오퍼레이터 GUI 로 실제 사용 중이므로 `tools/ros2-skill-manager` 경로의 정식 서브모듈로 승격.
- **`hand_pick`** → **보류/조사**. 기능이 `cup_stack_agent` 에 inline 되어 현재 트리에서 직접 참조 없음. "원본 prototype 을 흡수했는가"를 확인 후, 통합 의도가 있으면 등록·아니면 제외.
- **`vision-YOLO`** → **등록 불필요**. 런타임 ROS 패키지가 아니라 모델 학습 레포. 모델(.pt) 산출물만 소비하므로 URL 참조로 충분.
- **`doosan-robot2`** → 이미 서브모듈(중첩). 신규 등록 아님. 평탄화 제약 대상(아래 §4).
- Isaac 계열(`cup-stack-integration-isaac`, `yarr-isaac-playground`) → 이 레포의 서브모듈로 부적합. 별도 통합 라인.

---

## 4. 핵심 제약 — "전부 루트로"가 그대로는 안 되는 이유

평탄화 대상마다 **빌드/배포 레이아웃 결합도**가 다르다. 무지성 full-flatten 은 워크스페이스를 깨뜨린다.

1. **`doosan-robot2` 는 colcon `src/` 에 묶임** — `ros2-cup-stack/ros2/src/doosan-robot2`. 루트로 빼면 `ros2-cup-stack` 의 colcon 빌드가 드라이버를 못 찾는다. → **중첩 유지 필수.**
2. **`server`/`frontend` 는 `cup-stack-server` 배포 단위에 묶임** — `server/docker-compose.yml` 이 형제 경로를 빌드 컨텍스트로 참조. 라이브 배포(`/home/ssu/cup-stack` = cup-stack-server 체크아웃)가 이 구조를 가정. → **cup-stack-server 안에 유지 권장.**
3. **`cup-stack-server/server/start.sh` 가 `../../vision/` 를 역참조** (`start.sh:100,174`) — 즉 cup-stack-server 는 이미 "cup-stack-integration 안에서 vision/ 을 형제로 둔다"고 가정. vision/ 을 루트로 올리면(`vision/ros2-recode-sequence` → `ros2-recode-sequence`) 이 상대경로(`../../vision/...`)를 **전부 수정**해야 한다.
4. **`ros2-recode-sequence` 는 archived 지만 런타임 사용중** — frozen 으로 두되 제거하면 카메라 bringup 이 깨진다.

---

## 5. 목표 구조 (to-be) — 2가지 전략

### 전략 A — 완전 평탄화(사용자 직설 목표)
모든 leaf 레포를 루트 단일 계층으로:
```
cup-stack-integration/
├── cup_stack_agent/
├── server/  ros2-cup-stack/  frontend/  fallen-cup-recovery/
├── doosan-robot2/            (← ros2-cup-stack 의 colcon src 로 symlink/overlay 필요)
├── ros2-depth-point-cloude/  vision-node/  ros2-recode-sequence/
├── ros2-skill-manager/
└── docs/
```
- 장점: 트리가 평평, 한눈에 모든 컴포넌트.
- 단점/비용: `cup-stack-server` 집합 레포를 이 통합에서 **해체** → 배포 단위와 어긋남. docker-compose 빌드 컨텍스트, colcon `src/`, `start.sh ../../vision` 경로를 **모두 재작성**. 동일 leaf 를 cup-stack-server 와 integration 양쪽이 가리켜 **포인터 분기** 위험.

### 전략 B — 실용적 하이브리드(권장)
"평탄화"를 *결합도 없는 계층 제거*로 한정:
```
cup-stack-integration/
├── cup_stack_agent/                          [own code]
├── cup-stack-server/                         [submodule, 배포 단위 그대로]
│   └── ... server/ ros2-cup-stack/ frontend/ fallen-cup-recovery/ (+doosan-robot2 중첩 유지)
├── ros2-depth-point-cloude/                  [submodule]  ← vision/ 계층 제거
├── vision-node/                              [submodule]  ← vision/ 계층 제거
├── ros2-recode-sequence/                     [submodule, frozen]  ← vision/ 계층 제거
├── tools/ros2-skill-manager/                 [submodule 신규 등록]  ← .gitignore clone 승격
└── docs/
```
- `vision/` 한 계층만 루트로 흡수(결합 없음, 단 `../../vision/` 경로 4곳 수정).
- 빌드/배포 결합 서브모듈(`server`,`ros2-cup-stack`,`frontend`,`doosan-robot2`)은 제자리 유지.
- `LLM-prompting`(archived·중복) 제거, `ros2-skill-manager` 정식 등록.

> **권장: 전략 B.** 사용자의 "루트로" 의도는 충족하되(중첩 계층 제거) 워크스페이스/배포를 깨지 않음. 전략 A 가 정말 필요하면 §8 의 결정사항부터 합의.

---

## 6. 마이그레이션 단계 (전략 B 기준)

> 모두 worktree(`chore/flatten-submodules`)에서 수행. 서브모듈 내부 커밋은 `dwl21 <nggus5@gmail.com>`, 최상위는 기본 author.

**(1) `LLM-prompting` 서브모듈 제거** — cup-stack-server 내부
```bash
cd cup-stack-server
git submodule deinit -f LLM-prompting
git rm -f LLM-prompting
rm -rf .git/modules/LLM-prompting
# commit(dwl21) → push → 상위에서 cup-stack-server 포인터 bump
```
*선결*: `cup_stack_agent/prompts/*` 가 LLM-prompting 의 프롬프트를 완전히 대체하는지 최종 확인.

**(2) `vision/` 계층 평탄화** — 각 서브모듈을 `vision/<x>` → `<x>` 로 이동
```bash
# 예: ros2-depth-point-cloude
git mv vision/ros2-depth-point-cloude ros2-depth-point-cloude   # .gitmodules path 자동 갱신은 git mv가 처리(또는 수동)
# vision-node, ros2-recode-sequence 동일
# .gitmodules 의 path/section 정리 후:
git submodule sync
```
*동반 수정(필수)*: `cup-stack-server/server/start.sh` 의 `../../vision/ros2-recode-sequence` → `../../ros2-recode-sequence` 등 상대경로 4곳, `vision_rviz.sh`, CLAUDE.md 의 vision/ 경로 서술.

**(3) `ros2-skill-manager` 정식 서브모듈 등록**
```bash
# 기존 ignore clone 백업/제거 후
git rm -r --cached tools/ros2-skill-manager 2>/dev/null || true
# .gitignore 의 /tools/ros2-skill-manager/ 라인 제거
git submodule add https://github.com/2026-yarr-robotics/ros2-skill-manager.git tools/ros2-skill-manager
```

**(4) 상위 포인터 bump & 검증**
```bash
git submodule status --recursive
python3 -m py_compile cup_stack_agent/scripts/*.py
bash -n cup-stack-server/server/start.sh tools/run_skill_manager.sh
```

---

## 7. 제거 후보 (코드 · 문서)

| 후보 | 위치 | 사유 | 권장 |
|---|---|---|---|
| `LLM-prompting` 서브모듈 | cup-stack-server/LLM-prompting | archived + 프롬프트가 `cup_stack_agent/prompts` 로 이관됨 | **제거** (단 §6-1 선결확인) |
| `LLM-prompting/legacy/**` | (위 서브모듈 내부) | colab 노트북·프레임 PNG·옛 리포트 등 dead weight | 제거 시 함께 사라짐 (보존 원하면 upstream 에서 정리) |
| `cup-stack-server/docs/integration/yarr-robust-speed-stack-integration.md` | 문서 | 이미 제거된 dead 레포 통합 계획 | **제거 후보** |
| `cup-stack-server/server/start.sh.bak_disable_dt_panel` | 백업 파일 | `.bak` 잔재 | **제거 후보** |
| 루트 `build/` `install/` `log/` | 산출물 | colcon 산출물 | 이미 `.gitignore`, 추적 안 됨 → 조치 불필요(디스크 정리만) |
| `docs/.gitkeep` | placeholder | docs 채워지면 불필요 | 본 계획 문서 추가로 해소 |
| `hand_pick` 레포 | (외부) | inline 으로 흡수, 미참조 | 레포 자체 아카이브 여부는 org 차원 결정 |

> 트리 전반 추가 정밀 스캔(미참조 launch/script/문서)은 평탄화 확정 후 별도 패스로.

---

## 8. 브랜치 정리 후보 (stale)

| 레포 | 정리 후보 브랜치 | 비고 |
|---|---|---|
| `cup-stack-integration` | `tmp`, `vision-integration` | `tmp` = 현재 WIP(merge 후 삭제), `vision-integration` = main 병합 여부 확인 |
| `server` | `worktree-fix-joint-ee-risks`, `worktree-swagger-nginx-route` | worktree 잔재 브랜치 |
| `ros2-cup-stack` | `worktree-add-pyramid-unstack-loop` | worktree 잔재 |
| `doosan-robot2` | `auto-sync-jazzy-*`(6개), `feature/*`, `fix/issue-345/346`, `foxy-devel`, `master` 등 | upstream fork 노이즈. **사용은 `yarr-integration`** 만. 단 fork mirror 정책이면 보존 |
| `LLM-prompting` | `feat/llm-demo-and-noreplan-prompt` | 레포 자체 archived |
| `ros2-skill-manager` | `feat/settled-gate` | merge 여부 확인 |
| 다수 | `isaac` | Isaac 통합 라인 — 삭제 말 것(별도 트랙) |

> 브랜치 삭제는 각 레포에서 `git push origin --delete <branch>`. **삭제 전 main 병합/머지 여부 개별 확인 필수.**

---

## 9. 리스크 & 미결정 사항 (사용자 결정 필요)

1. **전략 A vs B** — `cup-stack-server` 집합 레포를 이 통합에서 해체할 것인가? (배포 단위/포인터 분기 이슈) → **권장 B.**
2. **`LLM-prompting` 제거 확정** — `cup_stack_agent/prompts/*` 가 완전 대체임을 확인했는가?
3. **`hand_pick` 처리** — 흡수 완료로 보고 제외 vs 정식 서브모듈 편입.
4. **`doosan-robot2` 다수 브랜치** — fork mirror 로 upstream 브랜치 보존할지, `yarr-integration`+`humble` 만 남길지.
5. **`ros2-recode-sequence` archived** — frozen 사용 유지가 맞나(후속 레포로 대체 계획 없는지).

---

## 10. 실행 체크리스트 (확정 후)

- [ ] 전략(A/B) 확정 — §5/§9-1
- [ ] LLM-prompting 대체 확인 → 제거(§6-1)
- [ ] vision/ 평탄화 + `../../vision` 경로 수정(§6-2)
- [ ] ros2-skill-manager 서브모듈 등록(§6-3)
- [ ] 제거 후보 문서/백업 파일 정리(§7)
- [ ] stale 브랜치 개별 병합확인 후 삭제(§8)
- [ ] `git submodule status --recursive` + py_compile + `bash -n` 검증(§6-4)
- [ ] cup-stack-server 내부 커밋(dwl21) → push → 상위 포인터 bump → push
```
