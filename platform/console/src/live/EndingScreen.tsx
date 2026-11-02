/**
 * 마무리 장면 (FR-14, NFR-8): audit log (T24), cost by component tag (T1) and the residual
 * dependency map. The dependency map is live from the collector's static inventory (FR-2);
 * the others show "연결 안 됨" until their source exists.
 */
import { Block, DataTable, Facts, Screen } from './ScreenFrame';
import { asArray, asRecord, Fresh, LiveGate, liveReason, num, str, Tag, useLive } from './live';

interface Finding { id: string; rule: string; tier: string; tierCode: string; service: string; file: string; line: number | null; reason: string }
interface Inventory { rulesVersion: string | null; digest: string | null; files: number | null; summary: [string, number][]; findings: Finding[] }

// Evidence text is never shown: it can quote source lines. Only rule, tier, service, location and reason.
const parseInventory = (v: unknown): Inventory => {
  const o = asRecord(v);
  const toF = (x: unknown): Finding => {
    const r = asRecord(x);
    return { id: str(r.id) ?? '?', rule: str(r.rule_id) ?? '—', tier: str(r.tier) ?? '—', tierCode: str(r.tier_code) ?? '', service: str(r.service) ?? '—', file: str(r.file) ?? '—', line: num(r.line), reason: str(r.reason) ?? '' };
  };
  return {
    rulesVersion: str(o.rules_version),
    digest: str(o.input_digest),
    files: num(o.files_scanned),
    summary: Object.entries(asRecord(o.summary)).map(([k, val]) => [k, num(val) ?? 0]),
    findings: [...asArray(o.findings), ...asArray(o.unclassified)].map(toF),
  };
};

// Color only for state: code changes block a clean move (경고); the rest are plain labels.
const TIER_VARIANT: Record<string, 'warning' | 'neutral'> = { code_change: 'warning' };

export function EndingScreen() {
  const inv = useLive('collector', 'v1/inventory', 10_000, parseInventory);
  const residual = (inv.data?.findings ?? []).filter((f) => f.tierCode !== 'no_change');
  return (
    <Screen title="마무리 장면" lead="FR-14 · NFR-8: 감사 기록, 구성 요소 태그별 비용, 남은 클라우드 의존 지도를 한 화면에 둡니다. 출처가 없는 항목은 이유와 함께 '연결 안 됨'으로 둡니다.">
      <Block title="출처 상태">
        <DataTable
          rows={[
            { k: '감사 기록 (T24)', ok: false, why: '이 콘솔은 감사 기록을 읽는 출처가 없습니다. 계획별 이벤트는 CI/CD · 워크플로우 화면에 있습니다.' },
            { k: '구성 요소 태그별 비용 (T1)', ok: false, why: '이 콘솔은 태그별 비용을 읽는 출처가 없습니다. 브라우저는 AWS 자격을 쓰지 않습니다.' },
            { k: '남은 의존 지도', ok: !inv.error && inv.data !== undefined, why: inv.error ? `${liveReason(inv.error)} · ${inv.endpoint}` : '수집기 GET /v1/inventory (정적 분석, FR-2)' },
          ]}
          rowKey={(r) => r.k}
          empty="—"
          cols={[
            { key: 'k', header: '항목', weight: 2, render: (r) => r.k },
            { key: 's', header: '상태', render: (r) => <Tag variant={r.ok ? 'success' : 'neutral'} label={r.ok ? '정상' : '연결 안 됨'} /> },
            { key: 'w', header: '출처 · 이유', weight: 4, render: (r) => <span className="foot">{r.why}</span> },
          ]}
        />
      </Block>
      <Block title="남은 의존 지도" meta={<Fresh at={inv.updatedAt} />}>
        <LiveGate live={inv} title="의존 지도 연결 안 됨">
          {(d) => (
            <>
              <Facts items={d.summary.map(([k, v]) => ({ label: k, value: <span className="num">{v}건</span> }))} />
              <DataTable
                rows={residual}
                rowKey={(f) => f.id}
                empty="이전에 걸리는 의존이 남아 있지 않습니다."
                cols={[
                  { key: 't', header: '등급', render: (f) => <Tag variant={TIER_VARIANT[f.tierCode] ?? 'neutral'} label={f.tier} /> },
                  { key: 's', header: '대상', render: (f) => <span className="mono">{f.service}</span> },
                  { key: 'f', header: '위치', weight: 2, render: (f) => <span className="mono">{f.file}{f.line !== null ? `:${f.line}` : ''}</span> },
                  { key: 'r', header: '규칙 · 이유', weight: 3, render: (f) => <span className="foot"><span className="mono">{f.rule}</span> · {f.reason}</span> },
                ]}
              />
              <p className="foot">
                출처: <span className="mono">GET /svc/collector/v1/inventory</span> · 규칙 {d.rulesVersion ?? '—'} · 파일 {d.files ?? '—'}개 · 입력 digest <span className="mono">{d.digest?.slice(0, 12) ?? '—'}</span>. 근거 원문(evidence)은 소스 줄을 담을 수 있어 보이지 않습니다.
              </p>
            </>
          )}
        </LiveGate>
      </Block>
    </Screen>
  );
}
