# platform/governance

규제 매핑표(§8.5, T23)와 GOV-01 표시(§2.1)입니다. 설계는 `docs/architecture/04-governance.md` 4절에 있습니다. 소재 판정으로 복제를 막는 것은 제안 D-03b이고, 채택 전에는 만들지 않습니다.

- `packs/`에 (국가, 산업) 단위 YAML을 둡니다(D-03a). 6개 통제의 시험과 합격선은 바꾸지 않습니다.
- 판정 입력(데이터 종류, 사업자 종류)은 사람이 확정한 값만 씁니다. AI 분류 값은 쓰지 않습니다.
- 조항 값마다 근거 상태(`confirmed`, `unverified`)를 둡니다. 원문 확인은 T23입니다. 출처, 조회 시각, 확인하지 못한 것은 [packs/SOURCES.md](packs/SOURCES.md)에 있습니다.
- 법률 판단 도구가 아닙니다. 적법성 판단은 사람이 합니다.
