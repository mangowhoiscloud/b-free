"""Rebuild the learning-report figures using the team's design.html visual language."""
from pathlib import Path
from html import escape
import json

ROOT = Path(__file__).parent
OUT = ROOT / 'figures'
OUT.mkdir(exist_ok=True)
INK, MUTED, LINE = '#1b2421', '#56625d', '#d9dfdb'
BLUE, GREEN, RED, AMBER = '#24527a', '#2b7a4b', '#b0362c', '#9a6400'

def txt(x, y, text, size=20, fill=INK, anchor='start', weight=400):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{escape(text)}</text>'

def rect(x,y,w,h,fill='white',stroke=LINE,dash=''):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1.5" stroke-dasharray="{dash}"/>'

def box(x,y,w,h,title,sub='',kind='normal'):
    fill,stroke={'normal':('white',LINE),'focus':('#e3ecf4',BLUE),'ok':('#e2f1e7',GREEN),'hold':('#f6ecd6',AMBER),'block':('#f8e4e1',RED),'dim':('#eef1ee',LINE)}[kind]
    lines=sub.split('|') if sub else []
    yy=y+30 if lines else y+h/2+7
    return '<g class="node">'+rect(x,y,w,h,fill,stroke)+txt(x+16,yy,title,20,INK,weight=600)+''.join(txt(x+16,yy+28+i*24,t,16,MUTED) for i,t in enumerate(lines))+'</g>'

def arrow(x1,y1,x2,y2,label='',color=BLUE,dashed=False):
    s=f'<path d="M{x1} {y1} L{x2} {y2}" fill="none" stroke="{color}" stroke-width="2" marker-end="url(#arr-{color[1:]})"'+(' stroke-dasharray="6 5"' if dashed else '')+'/>'
    if label:
        xx=(x1+x2)/2; yy=(y1+y2)/2-12; ww=max(80,len(label)*15)
        s+=rect(xx-ww/2,yy-18,ww,24,'white','none')+txt(xx,yy,label,16,color,'middle')
    return s

def connector(points, color=BLUE):
    """One continuous orthogonal route, rounded bends, one terminal arrowhead."""
    from math import hypot
    d=f'M{points[0][0]} {points[0][1]}'
    for i,(x,y) in enumerate(points[1:-1],1):
        px,py=points[i-1]; nx,ny=points[i+1]
        a,b=hypot(x-px,y-py),hypot(nx-x,ny-y)
        r=min(8,a/2,b/2)
        ix,iy=x-(x-px)/a*r,y-(y-py)/a*r
        ox,oy=x+(nx-x)/b*r,y+(ny-y)/b*r
        d+=f' L{ix} {iy} Q{x} {y} {ox} {oy}'
    d+=f' L{points[-1][0]} {points[-1][1]}'
    return f'<path class="connector" d="{d}" fill="none" stroke="{color}" stroke-width="2" marker-end="url(#arr-{color[1:]})"/>'

def svg(body,h=540,caption=''):
    defs=''.join(f'<marker id="arr-{c[1:]}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="{c}"/></marker>' for c in [BLUE,GREEN,RED,AMBER,MUTED])
    return f'<svg role="img" aria-label="{escape(caption)}" viewBox="0 0 1120 {h}" xmlns="http://www.w3.org/2000/svg"><title>{escape(caption)}</title><defs>{defs}</defs><rect width="1120" height="{h}" fill="white"/>{body}</svg>'

CSS='''
:root{--bg:#f6f7f5;--surface:#fff;--ink:#1b2421;--muted:#56625d;--line:#d9dfdb;--accent:#24527a;--ok:#2b7a4b;--hold:#9a6400;--block:#b0362c;color-scheme:light}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"IBM Plex Sans KR","Apple SD Gothic Neo",sans-serif;line-height:1.7;word-break:keep-all}
main{max-width:1200px;margin:auto;padding:24px 28px 32px}.eyebrow{font:12px "IBM Plex Mono",monospace;color:var(--accent);letter-spacing:.09em}h1{font-family:"Noto Serif KR",serif;font-size:26px;line-height:1.45;margin:8px 0 12px}p{margin:8px 0 16px;font-size:15px;color:var(--muted)}figure{margin:20px 0;border:1px solid var(--line);border-radius:6px;background:white;position:relative;overflow:hidden}svg{display:block;width:100%;height:auto;font-family:"IBM Plex Sans KR","Apple SD Gothic Neo",sans-serif}figcaption{padding:12px 18px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:16px}button,select{font:inherit;color:var(--accent);background:white;border:1px solid var(--line);border-radius:5px;padding:6px 12px;cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:3px}.legend{font-size:13px;display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}.legend span:before{content:"";display:inline-block;width:10px;height:10px;background:var(--c,#24527a);margin-right:6px}.note{border-left:3px solid var(--accent);padding:10px 16px;background:#e3ecf4;color:var(--ink);font-size:14px}.controls{display:flex;flex-wrap:wrap;gap:18px;padding:16px;background:#eef1ee;border-radius:6px;margin:16px 0}.controls label{font-size:14px}.controls input{vertical-align:middle}.result{font-size:20px;font-weight:600;color:var(--accent)}footer{font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}dialog{max-width:98vw;width:1400px;border:1px solid var(--line);border-radius:6px;padding:16px;background:#fff}dialog::backdrop{background:#1b2421bb}dialog button{float:right}dialog svg{clear:both}@media(max-width:600px){main{padding:16px 12px}h1{font-size:22px}figure{overflow:auto}figure svg{min-width:760px}.toolbar{align-items:flex-start}.controls{display:block}.controls label{display:block;margin:8px 0}}
'''

def page(slug,num,title,lede,body,caption,note,extra='',js=''):
    art=svg(body,caption=title)
    html=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=Noto+Serif+KR:wght@600;700&display=swap"><style>{CSS}</style><main><div class="eyebrow">B-FREE / ENGINEERING REPORT {num} / 2026-11-03</div><div class="toolbar"><h1>{title}</h1><button id="zoom" type="button">그림 확대</button></div><p>{lede}</p><figure>{art}<figcaption>{caption}</figcaption></figure>{extra}<div class="note">{note}</div><div class="legend"><span>구조·실행 경로</span><span style="--c:#2b7a4b">확인한 결과</span><span style="--c:#9a6400">조건·미확인</span><span style="--c:#b0362c">실패·차단</span></div><footer>팀 설계 문서의 글꼴·색상·그림 문법을 적용했습니다. 수치의 실행 조건과 원본은 해당 보고서 본문에서 확인하세요.</footer></main><dialog id="large"><button id="close" type="button">닫기</button></dialog><script>const d=document.getElementById('large');document.getElementById('zoom').onclick=()=>{{d.querySelector('svg')?.remove();d.append(document.querySelector('figure svg').cloneNode(true));d.showModal()}};document.getElementById('close').onclick=()=>d.close();{js}</script></html>'''
    (OUT/f'{slug}.html').write_text(html)
    (OUT/f'{slug}.svg').write_text(art)

# 01 — ownership boundaries, not a bag of service logos.
b=rect(24,20,1072,156,'#eef1ee',LINE)+txt(44,48,'제어면 · 중앙 플랫폼 AWS EC2 / RKE2',20,BLUE,weight=600)
b+=arrow(236,112,296,112)+arrow(556,112,616,112)+arrow(860,112,916,112)
b+=box(44,72,192,80,'사람','PIN · 역할별 콘솔')+box(296,72,260,80,'API · 상태 머신','계획 · 승인 · 실행 기록','focus')+box(616,72,244,80,'규칙 엔진','게이트 · Executor')+box(916,72,156,80,'OpenBao','비밀 보관','dim')
b+=arrow(424,176,424,232,'도구 계층')+box(284,232,280,84,'환경별 도구·연결','수집 / 격리 검증 / 실행','focus')
b+=connector([(340,316),(340,336),(148,336),(148,360)])+arrow(424,316,424,360)+connector([(508,316),(508,336),(860,336),(860,360)])
b+=box(24,360,252,124,'출발지 · EKS','채팅 / CNPG / PgBouncer|S3 · Secrets Manager','normal')+box(340,360,252,124,'목적지 · RKE2','채팅 / CNPG / PgBouncer|RGW · OpenBao + ESO','normal')+box(744,360,252,124,'추가 목적지 · GKE','랜딩 존 / 통제 비교|실제 DB 컷오버 미측정','hold')
b+=arrow(276,420,340,420,'복제',GREEN)+txt(24,520,'사용자 요청: Cloudflare LB → 앱. 운영 명령: 중앙 플랫폼 → 환경별 제한된 도구.',16,MUTED)
page('01-architecture','01','플랫폼과 고객 환경은 역할이 다르다','누가 판단하고, 누가 실행하며, 실제 쓰기는 어디로 가는지부터 구분합니다.',b,'그림 1. 현재 개인 구현의 배치. 팀 정본의 SeaweedFS·Tailscale과 차이가 있습니다.','AI는 제안서를 냅니다. 게이트와 Executor는 규칙으로 판정·실행합니다. 그림의 연결은 전체 E2E 완료를 뜻하지 않습니다.')

# 02 — same-node vs cross-node source address.
b=txt(24,32,'수정 전 · 다른 노드로 갈 때',22,RED,weight=600)+txt(24,292,'수정 뒤 · 내부 대역만 출발 주소 변환',22,GREEN,weight=600)
for yy,col,labels in [(64,RED,['Cilium 내부 주소','VPC가 경로를 모름','패킷이 앱에 못 감']),(324,GREEN,['내부 대역만 SNAT','노드 IP로 통과','Pod VPC 주소 유지'])]:
    b+=arrow(248,yy+50,432,yy+50,labels[0],col)+arrow(680,yy+50,864,yy+50,labels[2],col)
    b+=box(24,yy,224,100,'노드 A · Envoy','다른 노드 앱에 연결')+box(432,yy,248,100,'VPC 네트워크',labels[1],'block' if col==RED else 'ok')+box(864,yy,232,100,'노드 B · 앱 Pod','응답 경로 필요')
b+=txt(24,220,'같은 노드에서는 정상 → 노드 2대로 확장한 뒤 발견. Cilium 밖의 드롭이라 Hubble에 안 잡힘.',18,MUTED)
b+=txt(24,488,'재시험: 600/600 HTTP 200 · 12,000 부하 요청 성공 · 노드 간 실패 0',20,GREEN,weight=600)
page('02-gateway','02','503은 앱에 도달하기 전에 발생했다','동일 노드 성공과 다른 노드 실패를 분리해야 주소 변환 문제를 찾을 수 있습니다.',b,'그림 2. aws-cni chaining을 사용하는 Source EKS에 한정된 요청 경로.','Hubble에서 드롭이 없다는 사실만으로 전체 네트워크가 정상이라고 결론낼 수 없습니다. 관찰 위치 밖에서 버려지는 구간도 확인해야 합니다.')

# 03 — honest duration ranges.
b=txt(24,36,'두 역할을 동시에 시작 · 개별 Trial 완료 시간',22,BLUE,weight=600)
for t in [0,60,120,180,240,300,360]:
 x=280+t*2;b+=f'<line x1="{x}" y1="72" x2="{x}" y2="404" stroke="{LINE}"/>'+txt(x,440,str(t)+'초',16,MUTED,'middle')
for yy,name,old,lo,hi in [(92,'버킷 정책',227,150,210),(260,'코드 수정',315,180,187)]:
 b+=txt(24,yy+22,name,20,weight=600)+txt(24,yy+54,'이전 / 개선 뒤',16,MUTED)
 b+=rect(280,yy,old*2,32,'#d9dfdb',LINE)+txt(290+old*2,yy+24,f'{old}초',16,MUTED)
 b+=rect(280,yy+48,lo*2,36,'#e3ecf4',BLUE)+rect(280+lo*2,yy+48,(hi-lo)*2,36,'#f6ecd6',AMBER)+txt(296+hi*2,yy+74,f'{lo}–{hi}초',18,BLUE)
b+=txt(24,500,'모델 실행 99–152초의 변동은 남아 있음 · 제안 후 판정 118–152 → 27–54초',18,MUTED)
page('03-s2','03','S2: 모델 시간과 검증 대기를 나눠 본다','전체 시간을 줄였어도 변동의 원인이 모델인지, 검증 대기인지 구분해야 합니다.',b,'그림 3. 이전 단일 실행과 개선 후 관측 범위. 표본 수가 작은 관측값이며 신뢰구간이 아닙니다.','Pod 삭제 대기 30→1초와 검증 Job 조기 시작이 시간 개선에 기여했습니다. 검증기 규칙 자체의 변경은 별도 보고서에서 검토합니다.')

# 04 — one proposal, independent validators, mandatory conjunction.
b=arrow(260,252,368,252,'제안 고정')+arrow(540,204,540,176)+arrow(540,304,540,340)+connector([(740,124),(900,124),(900,212)])+connector([(740,392),(900,392),(900,308)])
b+=box(24,204,236,100,'작업 공간의 diff','마스킹 결과와 대조|원문 산출물로 결속')+box(368,204,344,100,'동일 제안서 / digest','plan_propose 이후 병렬 시작','focus')
b+=box(368,72,372,104,'정적 규칙 6개','경로 · HCL · 권한 · K8s 범위|버킷 ARN · ESO API 버전')+box(368,340,372,104,'동작 검증 Job','OBJ-01–06 / 실제 앱 쓰기·읽기|비밀 주입 / 권한 거부 / 대조')
b+=box(804,212,292,96,'둘 다 PASS','하나라도 FAIL → 합격 아님','ok')+txt(24,500,'검토 지점: 기대값이 누락되면 일부 규칙을 건너뜀 · 범위 축소나 검사 삭제가 없는지 확인',18,AMBER)
page('04-verifier','04','빨라진 판정과 바뀐 판정 규칙을 분리한다','검증을 겹쳐 돌리는 변경과 무엇을 PASS로 볼지 바꾸는 변경은 다른 리뷰입니다.',b,'그림 4. 같은 산출물에 대해 정적 규칙과 실제 동작 검증을 모두 요구하는 구조.','규칙·검증 Job 변경은 사람 검토 대기입니다. Trial PASS가 목적지 반영 승인이나 전체 보안 PASS를 대신하지 않습니다.')

# 05 — observed proportions + editable arithmetic, never a fabricated benchmark.
b=txt(24,36,'10% 가중치 ≠ 즉시 10% 표본',24,BLUE,weight=600)
b+=arrow(244,144,372,144,'쓰기 probe')+arrow(632,144,760,144,'관측 몫')+box(24,92,220,104,'입력 속도','기존 60건/s|대기 변경 120건/s')+box(372,92,260,104,'Cloudflare LB','변경 직후 옛 pool로|더 보내는 구간 관측','hold')+box(760,92,336,104,'대상 표본 100건','실제 대상 몫 3.9–9.2%|최대 보유 40초','focus')
b+=txt(24,264,'관측 비율이 일정하다고 가정한 계산: 100 ÷ (입력 속도 × 대상 비율)',20,weight=600)
for yy,rate,lo,hi,col in [(316,60,18.1,42.7,AMBER),(400,120,9.1,21.4,BLUE)]:
 b+=txt(24,yy+24,f'{rate}건/s',20,col,weight=600)+rect(220,yy,lo*16,32,'#e3ecf4',BLUE)+rect(220+lo*16,yy,(hi-lo)*16,32,'#f6ecd6',AMBER)+txt(236+hi*16,yy+24,f'{lo}–{hi}초',18,col)
b+=f'<line x1="860" y1="292" x2="860" y2="452" stroke="{RED}" stroke-dasharray="6 5"/>'+txt(860,480,'40초 한계',16,RED,'middle')
extra='<div class="controls"><label>입력 속도 <select id="rate"><option value="60">60건/s</option><option value="120" selected>120건/s</option></select></label><label>대상 비율 <input id="share" type="range" min="3.9" max="10" step="0.1" value="3.9"><output id="sharevalue"></output></label><output class="result" id="seconds"></output></div><p>계산 도구입니다. 가중치 전파가 일정하거나 독립이라고 검증한 모델이 아니며, 실제 재시험이 필요합니다.</p>'
js="function calc(){let s=+document.getElementById('share').value,r=+document.getElementById('rate').value;document.getElementById('sharevalue').textContent=s.toFixed(1)+'%';document.getElementById('seconds').textContent=(100/(r*s/100)).toFixed(1)+'초 / 100건'};document.getElementById('share').oninput=calc;document.getElementById('rate').onchange=calc;calc();"
page('05-shift','05','오류가 0이어도 표본이 모자라면 멈춘다','연결 풀 예열은 지연 문제를 줄였지만, 엣지 가중치 전파의 표본 문제는 남았습니다.',b,'그림 5. 비율 3.9–9.2%를 대입한 산술 범위. dd85cef는 미배포·재시험 대기입니다.','판정 기준 100건·오류율 1%·p95 1,500ms는 유지합니다. 120건/s는 사용자 부하에 더해지는 합성 쓰기이므로 다음 부하 시험에 함께 계산해야 합니다.',extra,js)

# 06 — compare experiment conditions without claiming equal exposure duration.
b=txt(24,36,'같은 목표 50쓰기 + 150읽기/s, 읽기의 비용은 다르다',22,BLUE,weight=600)
for x in [0,50,100,150]:
 xx=328+x*4.2;b+=f'<line x1="{xx}" y1="76" x2="{xx}" y2="424" stroke="{LINE}"/>'+txt(xx,456,str(x),16,MUTED,'middle')
for yy,name,w,r,kind in [(100,'1노드 · WS · 300초',4.23,4.6,'block'),(224,'2노드 · WS · 60초',38.7,45.4,'block'),(348,'2노드 · HTTP · 300초',50,149.75,'ok')]:
 b+=txt(24,yy+20,name,18,weight=600)+rect(328,yy-8,w*4.2,24,'#e3ecf4',BLUE)+txt(340+w*4.2,yy+10,f'쓰기 {w}',16,BLUE)+rect(328,yy+32,r*4.2,24,'#e2f1e7',GREEN)+txt(340+r*4.2,yy+50,f'읽기 {r}',16,GREEN)
b+=txt(24,508,'축: 달성 요청/초 · HTTP 실행도 읽기 77건 실패, 쓰기 39번의 503 시도가 있었음',18,AMBER)
page('06-load','06','부하 생성기와 대상 서비스의 병목을 구분한다','처리량·지연·오류·DB 무결성은 각각 다른 질문에 답합니다.',b,'그림 6. 실행 조건이 다른 관측값 비교. WebSocket은 읽기마다 새 연결을 만드는 부하였습니다.','15,000쓰기 유실·중복 0은 재시도 후 DB 대조 결과입니다. 모든 요청 시도가 성공했다거나 WebSocket 목표를 충족했다는 뜻은 아닙니다.')

# 07 — current code protocol, commit boundary is highlighted.
b=txt(24,32,'현재 코드의 프로토콜 요약 · 실서비스 전체 경로는 아직 미측정',22,AMBER,weight=600)
st=[(24,92,'① 준비','지연 · epoch'),(300,92,'② 쓰기 제한','PAUSE · 펜싱'),(576,92,'③ 대조','barrier · 해시·영수증'),(852,92,'④ 확정점','복제 방향·슬롯 처리'),(852,312,'⑤ 쓰기 개방','LOGIN · 대상 전환'),(576,312,'⑥ 성공 확인','새 DB 업무 쓰기 ACK')]
for a,c in zip(st[:3],st[1:4]):b+=arrow(a[0]+244,a[1]+48,c[0],c[1]+48)
b+=arrow(974,188,974,312,'확정 뒤')+arrow(852,360,820,360)
for x,y,t,s in st:b+=box(x,y,244,96,t,s,'hold' if '확정점' in t else 'normal')
b+=box(24,312,464,96,'응답이 불명확하면 SAFE_HOLD','타임아웃만으로 재실행·쓰기 재개하지 않음','block')
b+=txt(24,484,'확정 전: 이번 실행이 바꾼 효과만 복구 · 확정 뒤: 실패 시 SAFE_HOLD, 자동 재개 없음',18,MUTED)
page('07-cutover','07','트래픽 전환과 쓰기 DB 전환은 별도다','SHIFT 동안 목적지 앱도 출발지 DB에 쓸 수 있습니다. DB 소유권은 컷오버 프로토콜로 옮깁니다.',b,'그림 7. cutover.py의 현재 코드 경로를 단순화했습니다. 구현 검토와 실제 E2E 성공을 구분합니다.','되돌리기도 역방향 데이터 도착·대조를 거칩니다. 마지막 LB 이동만 성공했다고 데이터 failback 전체가 성공한 것은 아닙니다.')

# 08 — linear timeline with unknown segment.
b=txt(24,36,'S3 종료 3:40 고정 · 컷오버 시간은 아직 모름',22,AMBER,weight=600)
for sec in [120,140,160,180,200,220]:
 x=240+(sec-120)*8;b+=f'<line x1="{x}" y1="84" x2="{x}" y2="428" stroke="{LINE}"/>'+txt(x,464,f'{sec//60}:{sec%60:02}',16,MUTED,'middle')
for yy,start,lab in [(124,140,'2:20 시작'),(292,161,'2:41 시작')]:
 x=240+(start-120)*8;end=start+54;remain=220-end
 b+=txt(24,yy+26,lab,20,weight=600)+rect(x,yy,80,48,'#eef1ee',LINE)+txt(x+40,yy+30,'클릭',16,MUTED,'middle')+rect(x+80,yy,352,48,'#e3ecf4',BLUE)+txt(x+256,yy+30,'SHIFT 44초',20,BLUE,'middle')+rect(x+432,yy,remain*8,48,'#f6ecd6',AMBER)
 b+=txt(1040,yy+84,f'남음 약 {remain}초 → 컷오버·영수증·WS 재접속',18,AMBER,'end')
extra='<div class="controls"><label>S3 시작 초 <input id="start" type="range" min="140" max="161" value="140"><output id="startvalue"></output></label><output id="remaining" class="result"></output></div><p>클릭 10초와 SHIFT 44초를 둔 각본 계산입니다. 미측정 컷오버를 0초로 채우지 않습니다.</p>'
js="function budget(){let t=+document.getElementById('start').value;document.getElementById('startvalue').textContent=Math.floor(t/60)+':'+String(t%60).padStart(2,'0');document.getElementById('remaining').textContent='남은 예산 '+(220-t-54)+'초'};document.getElementById('start').oninput=budget;budget();"
page('08-demo-budget','08','각본은 평균이 아니라 늦은 경로를 견뎌야 한다','S2 재대조 PASS 시각 2:15–2:41이 S3 시작 가능 시각을 결정합니다.',b,'그림 8. 발표 경과 시간. 초기 각본의 약 25초·5초 여유를 10+44초 가정으로 계산하면 26초·5초입니다.','온프렘 게이트 사전 실행과 GKE 무대 실연은 팀 결정 대기입니다. 컷오버 실측 후에 S2 시작 시각과 구간 배분을 정합니다.',extra,js)

# 09 — separate identity, secret, evidence paths.
b=txt(24,36,'신원 / 실행 비밀 / 관측 결과는 서로 다른 경로',22,BLUE,weight=600)
for y,labels,subs,kind in [(92,['팀원 이메일','Cloudflare Access','API viewer'],['PIN으로 본인 확인','검증된 JWT (iss, sub)','첫 로그인 역할 부여'],'focus'),(244,['OpenBao','Kubernetes Secret','실행 서비스'],['플랫폼에서 보관','필요한 키만 전달','모델 컨텍스트와 분리'],'normal'),(396,['환경·서비스','수집기 · 관측','콘솔'],['상태 / 사용량 / 메트릭','시각·출처·누락 표시','연결됐다는 범위만 표시'],'hold')]:
 b+=arrow(300,y+40,420,y+40)+arrow(700,y+40,820,y+40)
 for x,t,s in zip([24,420,820],labels,subs):b+=box(x,y,276,88,t,s,kind)
page('09-operations','09','로그인 가능, 키 연결, 완전한 관측은 각각 확인한다','팀 리뷰는 조회 권한으로 열고, 클라우드 실행 키는 플랫폼에서 관리합니다.',b,'그림 9. 이번 공유 작업에서 확인한 접근·비밀·관측 경계.','비용은 누락 출처가 있는 추정치입니다. API 키가 저장됐다는 사실만으로 모델 사용 한도와 리허설 성공이 보장되지는 않습니다.')

# 10 — dependency path, evidence missing at transitions.
b=txt(24,36,'P0 완료를 막는 것은 단계 사이의 실제 연결',22,BLUE,weight=600)
b+=arrow(236,144,300,144)+arrow(536,144,600,144)+arrow(836,144,900,144)
b+=box(24,96,212,96,'기반 환경','세 고객 환경 연결','ok')+box(300,96,236,96,'리허설·검증','부분 실측 / 규칙 리뷰','hold')+box(600,96,236,96,'승인·DB 전환','콘솔 전체 E2E 없음','hold')+box(900,96,196,96,'복구·인수','실제 재시험 필요','hold')
b+=rect(24,260,1072,208,'#eef1ee',LINE)+txt(48,296,'팀 저장소로 옮기기 전에 맞출 계약',22,weight=600)
b+=box(48,328,304,100,'API · 상태 · 승인','요청 / 이벤트 / 권한 경계')+box(408,328,304,100,'도구 · 통제 · 영수증','스키마 / 판정 / 원본 증거')+box(768,328,304,100,'환경 어댑터','RGW ↔ SeaweedFS|Cloudflare ↔ Tailscale')
b+=txt(24,508,'담당 이름보다 먼저: 산출물 → 읽는 사람 → 다음 결정 → 완료 증거',18,MUTED)
page('10-roadmap','10','개인 구현은 팀 계약에 맞춰 선택해서 합친다','기능 수보다 실제 인수 경로와 그 증거를 기준으로 다음 일을 정합니다.',b,'그림 10. 의존 순서도이며 날짜 축이 아닙니다. 담당·기한은 팀 회의록에서 확정합니다.','구현부 이전은 보류한 채 공유합니다. 개인 저장소의 완료를 팀 작업의 완료로 계산하지 않습니다.')

(ROOT/'visual-style.md').write_text('''# 시각화 기준

1순위: 팀 `docs/design.html`과 개인 보존 스펙의 시각화 문법. Noto Serif KR 제목, IBM Plex Sans KR 본문, IBM Plex Mono 수치·식별자. bg #f6f7f5, ink #1b2421, accent #24527a, PASS #2b7a4b, BLOCK #b0362c, HOLD #9a6400.

본문은 Notion 원문으로 읽고 도식은 독립 HTML 첨부로 확대한다. 색만으로 상태를 전달하지 않는다. 단위·범례·측정 조건·미확인 경계를 그림과 본문에 함께 쓴다. 시간 차트는 실제 비례 축을 사용한다. 계산 도구는 관측과 구분한다. 모바일 그림은 가로 스크롤을 허용한다.

재생성: `python3 docs/reports/build_figures.py`. 출력은 figures/*.html 및 *.svg. 외부 스크립트는 없고 Google Fonts만 선택적으로 로드한다. 팀 스펙 파일은 수정하지 않는다.
''')
print(json.dumps({'figures':len(list(OUT.glob('*.html'))),'output':str(OUT)}))
