import React from 'react';
import { t } from '../../../i18n';

/* ── Company profile shared rendering helpers ────────────────────────────────────────── */

export function safeStr(v: any): string {
  if (v == null) return '—';
  if (typeof v === 'string') return v;
  if (typeof v === 'number') return v.toLocaleString();
  if (typeof v === 'boolean') return v ? t('是') : t('否');
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}

export function renderCompanyVal(val: any): React.ReactNode {
  if (val == null) return <span className="jx-tr-chainNull">—</span>;
  if (typeof val === 'number') return <span className="jx-tr-companyNum">{val.toLocaleString()}</span>;
  if (typeof val === 'string') return <span>{val}</span>;
  if (typeof val === 'boolean') return <span>{val ? t('是') : t('否')}</span>;
  if (Array.isArray(val)) {
    if (val.length === 0) return <span className="jx-tr-chainNull">{t('暂无数据')}</span>;
    if (typeof val[0] !== 'object' || val[0] === null) {
      return <span>{val.map(safeStr).join('、')}</span>;
    }
    const keys = Object.keys(val[0]);
    return (
      <div className="jx-tr-chainMiniTable">
        {val.map((row: any, ri: number) => (
          <div key={ri} className="jx-tr-chainMiniRow">
            {keys.map((k) => {
              const cellVal = row?.[k];
              const display = (typeof cellVal === 'object' && cellVal !== null)
                ? safeStr(cellVal) : (cellVal ?? '—');
              return (
                <span key={k} className="jx-tr-chainMiniCell">
                  <span className="jx-tr-chainMiniKey">{k}</span>
                  <span className="jx-tr-chainMiniVal">{typeof display === 'string' ? display : safeStr(display)}</span>
                </span>
              );
            })}
          </div>
        ))}
      </div>
    );
  }
  if (typeof val === 'object') {
    return (
      <div className="jx-tr-companyInfoKV">
        {Object.entries(val).map(([k, v]: [string, any]) => (
          <div key={k} className="jx-tr-companyInfoRow">
            <span className="jx-tr-companyInfoKey">{k}</span>
            <div className="jx-tr-companyInfoVal">{renderCompanyVal(v)}</div>
          </div>
        ))}
      </div>
    );
  }
  return <span>{String(val)}</span>;
}

export function companySectionPreview(val: any): string {
  if (typeof val === 'string') return val;
  if (typeof val === 'number') return String(val);
  if (Array.isArray(val)) return t('{n} 条数据', { n: val.length });
  if (typeof val === 'object' && val !== null) {
    if (val['名称'] || val['公司名称'] || val['企业名称']) return String(val['名称'] || val['公司名称'] || val['企业名称']);
    if (val['描述'] || val['说明']) return String(val['描述'] || val['说明']).slice(0, 80);
    return Object.keys(val).slice(0, 3).join('、');
  }
  return '';
}

export function renderCompanySections(
  obj: Record<string, any>,
  setDetailModal: (modal: { title: string; body: React.ReactNode } | null) => void,
): React.ReactNode {
  return (
    <div className="jx-tr-companySections">
      {Object.entries(obj).map(([sectionKey, sectionVal]: [string, any]) => {
        const prevText = companySectionPreview(sectionVal);
        const count = Array.isArray(sectionVal) ? sectionVal.length : null;
        const openDetail = () => setDetailModal({
          title: sectionKey,
          body: <div className="jx-tr-chainDetailWrap">{renderCompanyVal(sectionVal)}</div>,
        });
        return (
          <div key={sectionKey} className="jx-tr-companySection jx-tr-companySection--clickable" onClick={openDetail} title={t('点击查看详情')}>
            <div className="jx-tr-companySectionKey">
              {sectionKey}
              {count != null && <span className="jx-tr-companySectionBadge">{count}</span>}
            </div>
            {prevText && <div className="jx-tr-companySectionVal">{prevText}</div>}
          </div>
        );
      })}
    </div>
  );
}

type SetDetailModal = (modal: { title: string; body: React.ReactNode } | null) => void;

export function renderSearchCompany(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  const data = (typeof out === 'object' && out !== null ? out : {}) as any;
  const items: any[] = Array.isArray(data?.items) ? data.items : [];
  if (items.length === 0) return empty(t('未搜索到企业'));
  return (
    <div className="jx-tr-companySearchList">
      {items.map((item: any, idx: number) => {
        const name = String(item['企业名称'] || '');
        const rep = String(item['法定代表人'] || '');
        const capital = String(item['注册资金'] || '');
        const date = String(item['成立日期'] || '');
        const status = String(item['企业状态'] || '');
        const nodes: string[] = Array.isArray(item['所属产业节点']) ? item['所属产业节点'] : [];
        const quals: string[] = Array.isArray(item['企业资质']) ? item['企业资质'] : [];
        const statusCls = status.includes('存续') || status.includes('在营') ? '--active'
          : status.includes('注销') || status.includes('吊销') ? '--inactive' : '--other';
        const openDetail = () => {
          setDetailModal({
            title: name || t('企业详情'),
            body: (
              <div className="jx-tr-chainDetailWrap">
                <div className="jx-tr-companyInfoKV">
                  {[['企业ID', item['企业id']], ['法定代表人', rep], ['注册资金', capital], ['成立日期', date], ['企业状态', status], ['地址', item['地址']], ['官网', item['官网']]].map(([k, v]) =>
                    v ? <div key={k} className="jx-tr-companyInfoRow"><span className="jx-tr-companyInfoKey">{k}</span><span className="jx-tr-companyInfoVal">{String(v)}</span></div> : null
                  )}
                  {nodes.length > 0 && <div className="jx-tr-companyInfoRow"><span className="jx-tr-companyInfoKey">产业节点</span><div className="jx-tr-companyPills">{nodes.map((n, i) => <span key={i} className="jx-tr-companyPill">{n}</span>)}</div></div>}
                  {quals.length > 0 && <div className="jx-tr-companyInfoRow"><span className="jx-tr-companyInfoKey">企业资质</span><div className="jx-tr-companyPills">{quals.map((q, i) => <span key={i} className="jx-tr-companyPill jx-tr-companyPill--qual">{q}</span>)}</div></div>}
                </div>
              </div>
            ),
          });
        };
        return (
          <div key={idx} className="jx-tr-companySearchCard" onClick={openDetail} title={t('点击查看详情')}>
            <div className="jx-tr-companyName"><span className="jx-tr-companyIdx">{idx + 1}</span>{name}{status && <span className={`jx-tr-companyStatusTag jx-tr-companyStatusTag${statusCls}`}>{status}</span>}</div>
            <div className="jx-tr-companyMeta">
              {rep && <span>{t('法人: {v}', { v: rep })}</span>}
              {capital && <span>{t('注册资金: {v}', { v: capital })}</span>}
              {date && <span>{t('成立: {v}', { v: date })}</span>}
            </div>
            {(nodes.length > 0 || quals.length > 0) && (
              <div className="jx-tr-companyPills">
                {nodes.slice(0, 3).map((n, i) => <span key={`n${i}`} className="jx-tr-companyPill">{n}</span>)}
                {quals.slice(0, 3).map((q, i) => <span key={`q${i}`} className="jx-tr-companyPill jx-tr-companyPill--qual">{q}</span>)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function renderCompanyBaseInfo(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  if (!out) return empty(t('无基本信息'));
  const data = (typeof out === 'object' ? out : null) as any;
  const result = data?.result ?? data;
  if (typeof result === 'string') return <div className="jx-tr-companySectionVal">{result}</div>;
  if (typeof result === 'object' && result !== null) {
    const companyName = result['公司名称'] || result['企业名称'] || '';
    return (
      <div>
        {companyName && <div className="jx-tr-companyInfoTitle">{companyName}</div>}
        {renderCompanySections(result, setDetailModal)}
      </div>
    );
  }
  return <pre className="jx-tr-jsonBlock">{JSON.stringify(out, null, 2)}</pre>;
}

export function renderCompanyBusinessAnalysis(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  if (!out) return empty(t('无经营分析数据'));
  const data = (typeof out === 'object' ? out : null) as any;
  const result = data?.result ?? data;
  if (typeof result === 'string') return <div className="jx-tr-companySectionVal">{result}</div>;
  if (typeof result === 'object' && result !== null) {
    return renderCompanySections(result, setDetailModal);
  }
  return <pre className="jx-tr-jsonBlock">{JSON.stringify(out, null, 2)}</pre>;
}

export function renderCompanyTechInsight(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  if (!out) return empty(t('无技术洞察数据'));
  const data = (typeof out === 'object' ? out : null) as any;
  const result = data?.result ?? data;
  if (typeof result === 'string') return <div className="jx-tr-companySectionVal">{result}</div>;
  if (typeof result === 'object' && result !== null) {
    const patentKey = Object.keys(result).find(k => k.includes('专利') && k.includes('TOP'));
    const patents = patentKey ? result[patentKey] : null;
    const otherKeys = Object.keys(result).filter(k => k !== patentKey);
    return (
      <div>
        {Array.isArray(patents) && patents.length > 0 && (
          <div style={{ marginBottom: 8 }}>
            <div className="jx-tr-companySectionKey" style={{ marginBottom: 5 }}>{patentKey}</div>
            <div className="jx-tr-companyTechList">
              {patents.slice(0, 5).map((p: any, i: number) => {
                const pName = String(p['专利名称'] || p['名称'] || p.name || JSON.stringify(p));
                const cite = p['被引次数'] ?? p['引用次数'] ?? '';
                return (
                  <div key={i} className="jx-tr-companyTechItem">
                    <span className="jx-tr-companyTechRank">#{i + 1}</span>
                    <span className="jx-tr-companyTechName" title={pName}>{pName}</span>
                    {cite !== '' && <span className="jx-tr-companyTechCite">{t('引用 {cite}', { cite })}</span>}
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {otherKeys.length > 0 && renderCompanySections(
          Object.fromEntries(otherKeys.map(k => [k, result[k]])),
          setDetailModal,
        )}
      </div>
    );
  }
  return <pre className="jx-tr-jsonBlock">{JSON.stringify(out, null, 2)}</pre>;
}

export function renderCompanyFunding(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  if (!out) return empty(t('无资金穿透数据'));
  const data = (typeof out === 'object' ? out : null) as any;
  const result = data?.result ?? data;
  if (typeof result === 'string') return <div className="jx-tr-companySectionVal">{result}</div>;
  if (typeof result === 'object' && result !== null) {
    const totalCount = result['总投资企业数量'] ?? result['对外投资企业数'];
    const totalAmount = result['对外投资总金额'] ?? result['投资总金额'];
    const historyKey = Object.keys(result).find(k => k.includes('投资') && (k.includes('历史') || k.includes('记录') || k.includes('明细')));
    const history = historyKey ? result[historyKey] : null;
    const otherKeys = Object.keys(result).filter(k =>
      k !== historyKey && k !== '总投资企业数量' && k !== '对外投资企业数'
      && k !== '对外投资总金额' && k !== '投资总金额'
    );
    return (
      <div>
        {(totalCount != null || totalAmount != null) && (
          <div className="jx-tr-companyFundingSummary">
            {totalCount != null && (
              <div className="jx-tr-companyFundingStat">
                <span className="jx-tr-companyFundingNum">{totalCount}</span>
                <span className="jx-tr-companyFundingLabel">{t('投资企业')}</span>
              </div>
            )}
            {totalAmount != null && (
              <div className="jx-tr-companyFundingStat">
                <span className="jx-tr-companyFundingNum">{totalAmount}</span>
                <span className="jx-tr-companyFundingLabel">{t('投资总额')}</span>
              </div>
            )}
          </div>
        )}
        {Array.isArray(history) && history.length > 0 && (
          <div className="jx-tr-companyTimeline">
            {history.slice(0, 10).map((h: any, i: number) => {
              const date = String(h['时间'] || h['日期'] || h['投资时间'] || '');
              const desc = Object.entries(h).filter(([k]) => !['时间', '日期', '投资时间'].includes(k))
                .map(([k, v]) => `${k}: ${v}`).join(' · ');
              return (
                <div key={i} className="jx-tr-companyTimelineItem">
                  <span className="jx-tr-companyTimelineDate">{date || `#${i + 1}`}</span>
                  <span className="jx-tr-companyTimelineContent">{desc}</span>
                </div>
              );
            })}
          </div>
        )}
        {otherKeys.length > 0 && renderCompanySections(
          Object.fromEntries(otherKeys.map(k => [k, result[k]])),
          setDetailModal,
        )}
      </div>
    );
  }
  return <pre className="jx-tr-jsonBlock">{JSON.stringify(out, null, 2)}</pre>;
}

export function renderCompanyRiskWarning(out: unknown, setDetailModal: SetDetailModal): React.ReactNode {
  const empty = (msg: string) => <div className="jx-tr-empty">{msg}</div>;
  if (!out) return empty(t('无风险预警数据'));
  const data = (typeof out === 'object' ? out : null) as any;
  const result = data?.result ?? data;
  if (typeof result === 'string') return <div className="jx-tr-companySectionVal">{result}</div>;
  if (typeof result === 'object' && result !== null) {
    const RISK_KEYS = ['行政处罚', '诉讼', '案件', '失信', '被执行', '经营异常', '注销', '吊销'];
    return (
      <div className="jx-tr-companySections">
        {Object.entries(result).map(([sectionKey, sectionVal]: [string, any]) => {
          const isRisk = RISK_KEYS.some(rk => sectionKey.includes(rk));
          const isWarn = sectionKey.includes('到期') || sectionKey.includes('质押') || sectionKey.includes('预警');
          const count = Array.isArray(sectionVal) ? sectionVal.length : null;
          const prevText = companySectionPreview(sectionVal);
          const openDetail = () => setDetailModal({
            title: sectionKey,
            body: <div className="jx-tr-chainDetailWrap">{renderCompanyVal(sectionVal)}</div>,
          });
          const cls = isRisk ? 'jx-tr-companyRiskSection'
            : isWarn ? 'jx-tr-companyRiskSection jx-tr-companyRiskSection--warn'
            : 'jx-tr-companySection';
          return (
            <div key={sectionKey} className={`${cls} ${isRisk || isWarn ? 'jx-tr-companyRiskSection--clickable' : 'jx-tr-companySection--clickable'}`} onClick={openDetail} title={t('点击查看详情')}>
              <div className={isRisk ? 'jx-tr-companyRiskKey' : isWarn ? 'jx-tr-companyRiskKey' : 'jx-tr-companySectionKey'}>
                {sectionKey}
                {count != null && <span className={isRisk || isWarn ? 'jx-tr-companyRiskBadge' : 'jx-tr-companySectionBadge'}>{count}</span>}
              </div>
              {prevText && <div className={isRisk || isWarn ? 'jx-tr-companyRiskVal' : 'jx-tr-companySectionVal'}>{prevText}</div>}
            </div>
          );
        })}
      </div>
    );
  }
  return <pre className="jx-tr-jsonBlock">{JSON.stringify(out, null, 2)}</pre>;
}
