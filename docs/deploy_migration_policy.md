# 배포 마이그레이션 방침 (post-flatten)

> 작성 2026-06-12 · 관련: 서브모듈 평탄화([`submodule_flattening_done.md`](submodule_flattening_done.md))
> 대상: 라이브 "31" 호스트(`yarr-api-31.simplyimg.com`)의 Docker 배포

## 1. 배경 — 평탄화가 배포에 미치는 영향

- 평탄화는 **integration 레포의 레이아웃**만 바꿨다. `cup-stack-server`/`server`/
  `ros2-cup-stack`/`frontend` **레포 자체는 그대로**다(서브모듈 등록만 통합 루트로 이동).
- 라이브 배포는 지금까지 **별도 체크아웃** `/home/ssu/cup-stack`(= `cup-stack-server`
  체크아웃)에서 `docker compose` + `cup-stack` tmux 세션으로 구동돼 왔다.
- 평탄화 결정에서 **"cup-stack-server 레포는 사용하지 않는다"** 가 명시됐으므로,
  배포도 `cup-stack-server` 체크아웃에서 **평탄화된 integration 체크아웃으로 이전**한다.
- `server/start.sh` 의 상대경로(`../<pkg>`)는 평탄 루트 기준으로 **이미 수정됨**
  (server PR #1). docker-compose 빌드 컨텍스트는 `server/` 레포 내부 경로라 위치 무관.

## 2. 방침 (결정)

**라이브 배포를 평탄화된 `cup-stack-integration` 체크아웃 기반으로 이전한다.**
`cup-stack-server` 집합 레포는 배포 단위에서 제외(아카이브 후보). 단, 위험을 줄이기 위해
**카나리 → 컷오버 → 구 체크아웃 보존(롤백)** 단계로 진행한다.

## 3. 마이그레이션 절차

### (A) 신규 체크아웃 준비 (배포 호스트)
```bash
cd /home/ssu
git clone --recurse-submodules \
  https://github.com/2026-yarr-robotics/cup-stack-integration.git cup-stack-flat
cd cup-stack-flat
git submodule update --init --recursive          # 대용량(depth ~470MB, doosan) 포함
```

### (B) 빌드/기동 (server 서브모듈의 docker-compose 사용)
```bash
cd /home/ssu/cup-stack-flat/server
docker compose build robot
docker compose up -d --force-recreate robot
# 카메라/비전/스킬 tmux 진입점:
cd /home/ssu/cup-stack-flat/server && ./start.sh    # ../<pkg>/install/setup.bash 소싱
```
> 비전 overlay 는 각 `<pkg>/install/setup.bash` 가 있어야 하므로, 최초 1회
> `colcon build --symlink-install` 를 각 ROS 패키지(`ros2-cup-stack`,
> `ros2-depth-point-cloude`, `vision-node`)에서 수행.

### (C) 검증 (카나리)
- `GET /api/robot/config/pyramid` 응답 정상
- `server/start.sh` tmux 창: rosbridge / cam-exo / depth_digital_twin / verifier / agent 정상
- 카메라 launch(`recode_sequence cameras_only.launch.py`) 가 depth 워크스페이스에서
  기동되는지 확인(아래 §4 병합 반영)

### (D) 컷오버 & 롤백 보존
- DNS/프록시(`yarr-api-31`)를 신규 컨테이너로 전환.
- **구 `/home/ssu/cup-stack` 체크아웃은 1~2주 보존**(즉시 롤백 경로). 문제 없으면 제거.

## 4. 동반 변경 — recode_sequence 병합 반영

`ros2-recode-sequence`(아카이브) → `ros2-depth-point-cloude` 로 `recode_sequence`
패키지 병합됨. 따라서 카메라 launch 소싱이 바뀐다:
- 구: `source ../ros2-recode-sequence/install/setup.bash`
- 신: `source ../ros2-depth-point-cloude/install/setup.bash` (한 워크스페이스에 두 패키지)

`server/start.sh` 의 `RECODE_SETUP` 가 depth 워크스페이스를 가리키도록 수정(별도 PR).
패키지명 `recode_sequence` 는 유지되어 `ros2 launch recode_sequence cameras_only.launch.py`
는 그대로 동작.

## 4-b. YOLO 가중치 sim/real 분기 ⚠️

`ros2-depth-point-cloude/vision/yolo/` 에는 **sim 전용**(`sim_exo_best.pt`,
`sim_hand_best.pt` — Isaac 렌더로 파인튜닝, vision-YOLO/data_generator v7)과
**real**(`0610-2.pt`, `speedstack3class_…a100_best.pt`, `0609_exo_best.pt` 등 —
실 카메라로 학습) 모델이 함께 들어 있다. **둘은 절대 호환되지 않는다**: sim
모델은 Isaac 렌더 분포에만 맞고 실 카메라 이미지에선 퇴행, 반대도 마찬가지다.

동일 hand/exo 모델을 쓰는 소비처는 3곳이며, **sim 오버라이드는
`yarr-isaac-playground/start_isaac.sh`(SIM_YOLO_* )에만 존재**한다. real 경로는
전부 별도 기본값(아래)을 쓰므로 sim 가중치를 집어가지 않는다 — 이 분리를 깨지
말 것.

| 소비처 | 파라미터 | SIM (start_isaac.sh) | REAL 기본값 (절대 sim 금지) |
|---|---|---|---|
| pick_node hand-eye fine pick (`upright_cup_pose_node`) | `HAND_EYE_WEIGHTS` | `$SIM_YOLO_HAND` = `sim_hand_best.pt` | `cup_stack_agent/start.sh` 기본 = real `speedstack3class_…a100_best.pt` |
| fusion detection (exo+hand) | `model_exo` / `model_hand` | launch arg `$SIM_YOLO_{EXO,HAND}` | `depth_digital_twin/config/params.yaml` = real `0610-2.pt` / `speedstack3class_…` |
| recovery `fallen_cup_detect` | `FALLEN_CUP_WEIGHTS` | `$SIM_YOLO_FALLEN` = `sim_hand_best.pt` | robot 서비스 env (real 모델 경로로 설정) |

**규칙**: 새 모델을 배포할 때 sim/real 을 섞지 말 것. sim 산출물은
`sim_*_best.pt` 접두로만, real 산출물은 real 모델 파일명으로. real 호스트의
`HAND_EYE_WEIGHTS`·`params.yaml model_*`·`FALLEN_CUP_WEIGHTS` 가 `sim_*_best.pt`
를 가리키면 안 된다(실 카메라에서 퇴행).

## 5. 위험 & 체크리스트

- [ ] 대용량 서브모듈 초기 clone/build 시간 확보(depth/doosan)
- [ ] docker volume(`robot_state` 등) 신규 체크아웃에서 동일 마운트 확인
- [ ] `restart` 가 아닌 `build + up --force-recreate` 사용(이미지/볼륨 변경 반영)
- [ ] 구 체크아웃 롤백 경로 유지 기간 합의
- [ ] 컷오버 후 `cup-stack-server` 레포 아카이브 여부 결정
- [ ] **real 호스트의 `HAND_EYE_WEIGHTS`·`params.yaml model_*`·`FALLEN_CUP_WEIGHTS`
      가 real 모델을 가리키는지 확인 (sim_*_best.pt 금지 — 4-b 참조)**

> **미결**: 컷오버 일정과 `cup-stack-server` 레포 아카이브 시점은 운영 합의 필요.
