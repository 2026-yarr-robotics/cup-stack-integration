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

## 고정 실험값 (`config.py`)

```python
COMMAND = "3단 피라미드에서 1단만 쌓아줘"

FAKE_XY = {
    "L1_left":  (0.280, -0.15),
    "L1_mid":   (0.280,  0.00),
    "L1_right": (0.280,  0.15),
}
```

`fake_aggregator_node` / `fake_digital_twin_node`의 측정 좌표와 동일한 값.  
실험 조건이 바뀔 때는 `config.py`만 수정한다.

---

## 전제 조건

| 컴포넌트 | 확인 방법 |
|---------|----------|
| FastAPI 서버 (`server/`) 실행 중 | `curl http://localhost:8000/api/robot/status` |
| Ollama 실행 중 + 모델 Pull 완료 | `ollama list` |
| ROS bringup 완료 (실제 API 호출 시) | 서버 dashboard 상태 확인 |

의존 패키지: 표준 라이브러리만 사용. 추가 설치 불필요.

---

## 실행

```bash
# test_v1.0/ 루트에서 실행

# dry-run (기본값, API 미호출)
python3 http_client/client.py

# 실제 로봇 API 호출
DRY_RUN=0 python3 http_client/client.py
```

---

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `SERVER_URL` | `http://localhost:8000` | FastAPI 서버 base URL |
| `OLLAMA_URL` | `http://localhost:11434/api/chat` | Ollama 엔드포인트 |
| `LLM_MODEL` | `gemma4:26b` | Ollama 모델명 |
| `DRY_RUN` | `1` | `0`으로 설정 시 실제 API 호출 |
| `GRIPPER_CLOSED_MM` | `100.0` | 이 값 미만이면 그리퍼가 컵을 잡은 것으로 판단 |

```bash
SERVER_URL=http://192.168.1.31:8000 LLM_MODEL=qwen3.6:35b DRY_RUN=0 \
  python3 http_client/client.py
```

---

## 실행 흐름 상세

### 1. cold_start

1. `GET /api/robot/status` → `gripper.width_mm` 조회
   - `width_mm < GRIPPER_CLOSED_MM` → `holding=True`
   - `width_mm`가 None이거나 임계값 이상 → `holding=None`
2. `GoalStateBuilder`로 cold_start 페이로드 조립
3. Ollama 호출 → `status=ok` + `plan.steps[]` 수신
4. 플랜 채택

### 2. in-flight 루프

스텝마다 다음을 반복한다.

```
execute_step()        # POST /api/robot/skill/pyramid {x, y, slot}
                      # timeout=None — 로봇 동작 완료까지 blocking
↓
fetch_robot_state()   # GET /api/robot/status
↓
_llm_call()           # in_flight payload → Ollama
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
`execute_step(timeout=None)`의 blocking HTTP 호출이 자연스러운 순차 실행을 보장한다.

---

## 미구현 / 외부 위임 항목

| 항목 | 현황 |
|------|------|
| `cups_on_table` / `stack` 갱신 | `fake_aggregator_node`가 담당. 이 클라이언트는 항상 `{}` |
| 컵 색상 → 실제 좌표 해석 | `FAKE_XY` (슬롯 기반 고정 좌표). 실제 환경은 `plan_executor_node` + digital twin |
| stacked ID 추적 | `fake_digital_twin_node` + `plan_executor_node`가 담당 |

---

## 테스트

```bash
# test_v1.0/ 루트에서
python3 -m unittest discover -s tests -v
```

`test_sequential_pyramid.py`: 실제 HTTP 없이 3개 스텝(L1_left → L1_mid → L1_right) 순서와 횟수를 검증한다.
