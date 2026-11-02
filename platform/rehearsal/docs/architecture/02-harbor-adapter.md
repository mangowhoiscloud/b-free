# 02. Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 환경 어댑터와 외부형 에이전트

소유 코드: `src/bravo_rehearsal/harbor_env.py`, `src/bravo_rehearsal/harbor_agent.py`, `src/bravo_rehearsal/harbor_run.py`, `src/bravo_rehearsal/harbor_task.py`, `src/bravo_rehearsal/tool_layer.py`
확인 명령: `make test T=tests/test_tool_layer.py`

## 어댑터: 리허설 네임스페이스 하나 = Harbor 환경 하나

Harbor 내장 K8s backend(gke, openshift, ack)는 NetworkPolicy를 만들지 않습니다. 그래서 `import_path`로 커스텀 환경을 붙입니다.

| 메서드 | 할 일 |
|---|---|
| `type()` | `"rke2-rehearsal"`를 돌려줍니다 |
| `capabilities` | canary 시험을 통과한 정책만 True로 선언합니다 |
| `start()` | 네임스페이스, Quota, NetworkPolicy, 작업 Pod를 만들고 준비를 기다립니다 |
| `exec()` | K8s exec stream으로 명령을 실행합니다. 종료 코드와 timeout을 지킵니다 |
| 파일 전송 4종 | exec 위 tar 스트림으로 올리고 내립니다 |
| `stop()` | 네임스페이스를 지우거나 보존합니다 |

정책 선언 규칙:
- no-network와 IP · CIDR 허용 목록만 선언합니다.
- 호스트 이름 허용 목록은 선언하지 않습니다. 그것을 요구하는 task는 Harbor가 거부합니다.
- 작업 Pod는 non-root, Pod Security restricted입니다. 서비스 계정 토큰은 자동 마운트하지 않습니다.
- `exec`의 `user` 인자는 쓰지 않습니다. restricted Pod에서는 `su`가 막히기 때문입니다.

## 외부형 에이전트

- Claude Agent SDK 루프를 `BaseAgent.run()` 안에서 돌립니다. 모델 키는 플랫폼에만 있습니다.
- SDK 내장 Bash, Read, Write 도구는 끕니다. MCP 도구만 엽니다.
- `rehearsal.exec` 도구는 구조화한 작업만 받습니다. `tool_layer.authorize()`가 허용한 작업만 셸 인용한 명령으로 `environment.exec()`에 넘깁니다. 네임스페이스는 에이전트가 아니라 러너가 정합니다.
- 커스텀 에이전트는 ATIF 기본값이 꺼져 있습니다. 궤적은 어댑터가 직접 씁니다.

## 함정

- `preflight()`는 `harbor run` CLI에서만 불립니다. `Trial.create()` 경로에서는 `start()`에서 canary를 돌립니다.
- 리허설 노드는 목적지 운영 Pod와 섞지 않습니다. 노드 라벨과 taint로 나눕니다.
- 컨테이너는 보안 경계가 아닙니다. 노드 분리와 자격 증명 부재가 함께 있어야 합니다.

근거: `research/sandbox-research/08-harbor-k8s-fit.md` 3절과 7절, `research/sandbox-research/07-harbor-papers.md` 2.4절
