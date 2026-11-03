# 시각화 기준

1순위: 팀 `docs/design.html`과 개인 보존 스펙의 시각화 문법. Noto Serif KR 제목, IBM Plex Sans KR 본문, IBM Plex Mono 수치·식별자. bg #f6f7f5, ink #1b2421, accent #24527a, PASS #2b7a4b, BLOCK #b0362c, HOLD #9a6400.

본문은 Notion 원문으로 읽고 도식은 독립 HTML 첨부로 확대한다. 색만으로 상태를 전달하지 않는다. 단위·범례·측정 조건·미확인 경계를 그림과 본문에 함께 쓴다. 시간 차트는 실제 비례 축을 사용한다. 계산 도구는 관측과 구분한다. 모바일 그림은 가로 스크롤을 허용한다.

재생성: `python3 docs/reports/build_figures.py`. 출력은 figures/*.html 및 *.svg. 외부 스크립트는 없고 Google Fonts만 선택적으로 로드한다. 팀 스펙 파일은 수정하지 않는다.
