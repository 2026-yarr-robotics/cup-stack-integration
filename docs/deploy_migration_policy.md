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

## 5. 위험 & 체크리스트

- [ ] 대용량 서브모듈 초기 clone/build 시간 확보(depth/doosan)
- [ ] docker volume(`robot_state` 등) 신규 체크아웃에서 동일 마운트 확인
- [ ] `restart` 가 아닌 `build + up --force-recreate` 사용(이미지/볼륨 변경 반영)
- [ ] 구 체크아웃 롤백 경로 유지 기간 합의
- [ ] 컷오버 후 `cup-stack-server` 레포 아카이브 여부 결정

> **미결**: 컷오버 일정과 `cup-stack-server` 레포 아카이브 시점은 운영 합의 필요.
