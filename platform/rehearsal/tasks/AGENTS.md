# task를 고치는 에이전트용 규칙

- 금지: `tests/`를 에이전트 실행 환경에 두지 않습니다. 검증 때만 주입되기 때문입니다.
- 금지: `environment/`에 실제 자격 증명을 넣지 않습니다. 샌드박스에는 비밀이 없어야 하기 때문입니다.
- `task.toml`에는 `[verifier] environment_mode = "separate"`를 둡니다.
- `task.toml`의 `[agent] network_mode`는 `no-network`로 둡니다. 모델 호출은 플랫폼에서 하기 때문입니다.
- `instruction.md`는 목표, 지표, 할 수 있는 것, 할 수 없는 것, 예산, 멈춤 절을 가집니다.
- 바꾼 뒤 `make oracle`과 `make check`를 실행합니다.

`scripts/check_tasks.py`가 위 규칙 가운데 셋을 검사합니다.
