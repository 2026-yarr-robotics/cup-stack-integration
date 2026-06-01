# http_client 사용 가이드

ROS 없이 동작하는 순수 Python HTTP 클라이언트.  
Ollama LLM을 통해 플랜을 받고, FastAPI 서버의 `/api/robot/skill/pyramid`를 순차 호출한다.

---

## 아키텍처

```
┌─────────────────────────────────────────────────┐
│  http_client/client.py (오케스트레이터)            │
│                                                 │
│  cold_start                                     │
│    fetch_robot_state() ──► GET /api/robot/status│
│    GoalStateBuilder.build_payload()             │
│    _llm_call()  ──► Ollama /api/chat            │
│    set_plan()                                   │
│                                                 │
│  in-flight loop (스텝마다 반복)                  │
│    execute_step() ──► POST /api/robot/skill/pyramid  (blocking)
│    fetch_robot_state() ──► GET /api/robot/status│
│    _llm_call()  ──► Ollama /api/chat            │
│    decision: continue / replan / done           │
└─────────────────────────────────────────────────┘
```

`/cups_on_table`, `/stack`은 외부 컴포넌트(`fake_aggregator_node`)가 담당하며  
이 클라이언트는 빈 dict로 초기화 후 업데이트하지 않는다.

---

## 전제 조건

| 컴포넌트 | 확인 방법 |
|---------|----------|
| FastAPI 서버 (`server/`) 실행 중 | `curl http://localhost:8000/api/robot/status` |
| Ollama 실행 중 + 모델 Pull 완료 | `ollama list` |
| ROS bringup 완료 (실제 API 호출 시) | 서버 dashboard 상태 확인 |

의존 패키지: 표준 라이브러리만 사용. 추가 설치 불필요.

---

## 빠른 시작

```bash
# test_v1.0/ 루트에서 실행
cd test_v1.0

# dry-run (API 실제 호출 없음, 기본값)
python3 http_client/client.py \
  --command "1단만 쌓아줘" \
  --fake-xy '{"L1_left":[0.28,-0.15],"L1_mid":[0.28,0.0],"L1_right":[0.28,0.15]}'

# 실제 로봇 API 호출
python3 http_client/client.py \
  --command "1단만 쌓아줘" \
  --fake-xy '{"L1_left":[0.28,-0.15],"L1_mid":[0.28,0.0],"L1_right":[0.28,0.15]}' \
  --real-api
```

---

## 인자 레퍼런스

| 인자 | 기본값 | 설명 |
|------|--------|------|
| `--command` | *(필수)* | LLM에 전달할 자연어 명령 |
| `--fake-xy` | *(필수)* | 슬롯별 픽 좌표 JSON. 아래 형식 참고 |
| `--server` | `http://localhost:8000` | FastAPI 서버 base URL |
| `--ollama-url` | `http://localhost:11434/api/chat` | Ollama 엔드포인트 |
| `--model` | `gemma4:26b` | Ollama 모델명 |
| `--llm-timeout` | `120` | LLM 호출 타임아웃 (초) |
| `--dry-run` | on | API 호출 없이 로그만 출력 |
| `--real-api` | — | `--dry-run` 해제, 실제 API 호출 |
| `--prompt-dir` | `prompts/` | 프롬프트 디렉토리 경로 (기본: 이 레포의 `prompts/`) |

### --fake-xy 형식

```json
{
  "L1_left":  [x, y],
  "L1_mid":   [x, y],
  "L1_right": [x, y],
  "L2_left":  [x, y],
  "L2_right": [x, y],
  "L3_top":   [x, y]
}
```

- 키는 `L1_left`, `L1_mid`, `L1_right`, `L2_left`, `L2_right`, `L3_top` 중 하나
- 플랜에 포함된 슬롯의 좌표만 있으면 됨 (전체 6개 불필요)
- 실험 측정값 (1단 쌓기 기준):

```bash
--fake-xy '{"L1_left":[0.280,-0.15],"L1_mid":[0.280,0.0],"L1_right":[0.280,0.15]}'
```

---

## 환경 변수

인자 대신 환경 변수로 기본값을 오버라이드할 수 있다.

| 변수 | 대응 인자 | 기본값 |
|------|----------|--------|
| `SERVER_URL` | `--server` | `http://localhost:8000` |
| `OLLAMA_URL` | `--ollama-url` | `http://localhost:11434/api/chat` |
| `LLM_MODEL` | `--model` | `gemma4:26b` |
| `DRY_RUN` | `--dry-run` / `--real-api` | `1` (dry-run on) |
| `GRIPPER_CLOSED_MM` | — | `100.0` mm (이 값 미만이면 그리퍼가 컵을 잡은 것으로 판단) |

```bash
SERVER_URL=http://192.168.1.31:8000 LLM_MODEL=qwen3.6:35b \
  python3 http_client/client.py \
  --command "1단만 쌓아줘" \
  --fake-xy '{"L1_left":[0.28,-0.15],"L1_mid":[0.28,0.0],"L1_right":[0.28,0.15]}' \
  --real-api
```

---

## 실행 흐름 상세

### 1. cold_start

1. `GET /api/robot/status` → `gripper.width_mm` 조회
   - `width_mm < GRIPPER_CLOSED_MM` → `holding=True` (이전 픽 색상을 GoalStateBuilder가 채움)
   - `width_mm`가 None이거나 임계값 이상 → `holding=None`
2. `GoalStateBuilder`로 cold_start 페이로드 조립 (`mode="cold_start"`)
3. Ollama 호출 → `status=ok` + `plan.steps[]` 수신
4. 플랜 채택

### 2. in-flight 루프

스텝마다 다음을 반복한다.

```
execute_step()   # POST /api/robot/skill/pyramid {x, y, slot}
                 # timeout=None — 로봇 동작 완료까지 blocking
                 # 서버에 큐 없음: 응답 받은 뒤 다음 요청 전송 필수
↓
fetch_robot_state()   # GET /api/robot/status
↓
GoalStateBuilder.build_payload()  # mode="in_flight"
↓
Ollama 호출
↓
decision:
  continue → 다음 스텝
  replan   → 새 플랜 채택 후 다음 스텝
  done     → 루프 종료
```

### 슬롯 → API 키 변환

| LLM 슬롯 | API `slot` 값 |
|---------|--------------|
| `L1_left` | `1l` |
| `L1_mid` | `1m` |
| `L1_right` | `1r` |
| `L2_left` | `2l` |
| `L2_right` | `2r` |
| `L3_top` | `3m` |

---

## 서버 큐 동작 주의

FastAPI `POST /api/robot/skill/pyramid`는 내부적으로 skill_api_node에 직접 HTTP를 쏘며 **요청 큐가 없다**.  
동시 요청이 오면 skill_api_node에 동시 도달하므로, 이 클라이언트는 **반드시 응답을 받은 뒤 다음 요청을 보내야 한다**.  
`execute_step(timeout=None)`의 blocking HTTP 호출이 이를 자동으로 보장한다.

---

## 미구현 / 외부 위임 항목

| 항목 | 현황 |
|------|------|
| `cups_on_table` / `stack` 갱신 | `fake_aggregator_node`가 담당. 이 클라이언트는 항상 `{}` |
| 컵 색상 → 실제 좌표 해석 | `fake_xy` (슬롯 기반 고정 좌표). 실제 환경은 `plan_executor_node` + digital twin |
| stacked ID 추적 | `fake_digital_twin_node` + `plan_executor_node`가 담당 |

---

## 테스트

```bash
# test_v1.0/ 루트에서
python3 -m unittest discover -s tests -v
```

`test_sequential_pyramid.py`: 실제 HTTP 없이 3개 스텝(L1_left → L1_mid → L1_right) 순서와 횟수를 검증한다.
