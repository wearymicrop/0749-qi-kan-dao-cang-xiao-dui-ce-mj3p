import { element } from './evidence.js';

const $ = id => document.getElementById(id);
const labels = JSON.parse(document.querySelector('#reason-labels').textContent);
const statusText = { suspended: '停刊', resumed: '已恢复', pending: '仍待确认' };
const kindText = { suspension: '停刊来函', resumption: '复刊来函' };
const effectText = { pause: '暂停出刊', terminate: '终止出版' };

const SAMPLE_BATCH = {
  rules: [
    { chain: 'CST', from_year: 2025, to_year: 2026, first_volume: 17, issues_per_year: 4, numbering: 'yearly' },
    { chain: 'GEO', from_year: 2025, to_year: 2026, first_volume: 3, issues_per_year: 3, numbering: 'continuous', first_issue: 7 },
  ],
  notices: [
    { letter_no: 'HT-2026-01', kind: 'suspension', effect: 'pause', chain: 'CST', year: 2026, volume: '18', issue_first: '3', issue_last: '4', title: '海岸观测季刊', issued_on: '2026-09-01' },
    { letter_no: 'HT-2026-02', kind: 'suspension', effect: 'terminate', chain: 'GEO', year: 2026, volume: '4', issue_first: '12', issue_last: null, title: '方志资料辑刊', issued_on: '2026-09-03' },
  ],
};
const SAMPLE_FOLLOWUP = {
  notices: [
    { letter_no: 'HF-2026-07', kind: 'resumption', chain: 'CST', year: 2026, volume: '18', issue_first: '3', issue_last: '3', title: '海岸观测季刊', issued_on: '2026-11-18' },
  ],
};

async function api(path, payload) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || '处理失败，请核对来函');
  return data;
}

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).message || '读取失败');
  return response.json();
}

function folio(slot) {
  return `${slot.year} · ${slot.volume} 卷 ${String(slot.issue).padStart(2, '0')} 期`;
}

function paintKpis(snapshot) {
  const metrics = [
    ['affected', '受影响期位'], ['suspended', '停刊'],
    ['resumed', '已恢复'], ['pending', '仍待确认'],
  ];
  $('handover-kpis').replaceChildren(...metrics.map(([key, label]) => {
    const cell = element('div', undefined, 'kpi');
    cell.dataset.metric = key;
    cell.append(element('span', label), element('strong', String(snapshot.totals[key] ?? 0)));
    return cell;
  }));
}

function paintBasis(basis) {
  const list = element('ul', undefined, 'basis-list');
  for (const item of basis) {
    const li = element('li');
    const effect = item.kind === 'resumption' ? '恢复出版' : (effectText[item.effect] || '');
    li.append(element('span', `${kindText[item.kind]} · `, 'kind'),
      document.createTextNode(`${item.letter_no}（${effect} ${item.cover}）`));
    list.append(li);
  }
  return list;
}

function paintSlots(snapshot) {
  const body = $('handover-register');
  body.replaceChildren();
  const filter = $('handover-filter').value;
  const rows = snapshot.slots.filter(slot => filter === 'all' || slot.status === filter);
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = 5;
    cell.textContent = '当前筛选没有期位。';
    return;
  }
  for (const slot of rows) {
    const tr = body.insertRow();
    tr.dataset.status = slot.status;
    tr.insertCell().textContent = slot.chain;
    tr.insertCell().textContent = folio(slot);
    tr.insertCell().append(element('span', statusText[slot.status], 'seal'));
    tr.insertCell().textContent = slot.latest_letter;
    tr.insertCell().append(paintBasis(slot.basis));
  }
}

function paintLedgerTable(rows, columns) {
  const table = element('table');
  const head = table.createTHead().insertRow();
  for (const label of columns) head.append(element('th', label));
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const value of row) tr.append(element('td', value));
  }
  return table;
}

async function paintLedger(tab) {
  const ledger = await getJson('/api/handover/ledger');
  const body = $('handover-ledger-body');
  body.replaceChildren();
  if (tab === 'inbox') {
    if (!ledger.inbox.length) { body.textContent = '台账暂无来函。'; return; }
    body.append(paintLedgerTable(
      ledger.inbox.map(item => [item.seq, item.batch_id.slice(0, 8), JSON.stringify(item.raw)]),
      ['台账序号', '批次', '原件原文']));
  } else if (tab === 'accepted') {
    if (!ledger.notices.length) { body.textContent = '尚无采信来函。'; return; }
    body.append(paintLedgerTable(
      ledger.notices.map(n => [n.letter_no, kindText[n.kind], n.chain, `${n.year}/${n.cover.volume}卷`,
        `${n.cover.first}–${n.cover.last}期`]),
      ['来函编号', '类型', '订阅链', '年/卷', '覆盖期位']));
  } else if (tab === 'quarantine') {
    if (!ledger.quarantine.length) { body.textContent = '没有隔离来函。'; return; }
    body.append(paintLedgerTable(
      ledger.quarantine.map(q => [q.inbox_seq, labels[q.code] || q.code, q.fields.join('、') || '—', JSON.stringify(q.raw)]),
      ['台账序号', '隔离原因', '字段', '原件原文']));
  } else {
    if (!ledger.replays.length) { body.textContent = '没有重放记录。'; return; }
    body.append(paintLedgerTable(
      ledger.replays.map(r => [r.inbox_seq, r.letter_no, r.original_seq]),
      ['台账序号', '重放来函', '原台账序号']));
  }
}

async function paintVersions() {
  const listing = await getJson('/api/handover/versions');
  const line = $('handover-versions');
  if (!listing.versions.length) { line.textContent = '尚未发布版本。'; return; }
  line.textContent = `已发布 ${listing.versions.length} 版 · 当前版摘要 ${listing.current.digest.slice(0, 12)} · `
    + listing.versions.map(v => `v${v.version}（${v.created_at}）`).join(' · ');
}

async function refresh(snapshot) {
  const current = snapshot || await getJson('/api/handover/current').catch(() => null);
  if (!current) {
    $('handover-kpis').replaceChildren();
    $('handover-register').replaceChildren();
    $('handover-versions').textContent = '尚未形成交班清单，请先送达首批来函（须随附订购规则）。';
    return;
  }
  paintKpis(current);
  paintSlots(current);
  await paintVersions();
  await paintLedger($('handover-ledger-tab').value);
}

async function deliver() {
  const feedback = $('handover-feedback');
  feedback.textContent = '正在处理来函…';
  try {
    const text = $('handover-input').value.trim();
    if (!text) throw new Error('请粘贴一批来函 JSON');
    let payload;
    try { payload = JSON.parse(text); } catch { throw new Error('JSON 格式有误，请检查后重试'); }
    const result = await api('/api/handover/batches', payload);
    feedback.textContent = `批次 ${result.batch_id.slice(0, 8)} 已入账：采信 ${result.accepted} 件、`
      + `隔离 ${result.quarantined} 件、重放 ${result.replayed} 件；清单第 ${result.version} 版`
      + (result.reused ? '（内容未变，复用现有版本）' : '已发布');
    await refresh(result.snapshot);
  } catch (error) {
    feedback.textContent = error.message;
  }
}

export function bootHandover() {
  $('handover-sample-first').addEventListener('click', () => {
    $('handover-input').value = JSON.stringify(SAMPLE_BATCH, null, 2);
  });
  $('handover-sample-followup').addEventListener('click', () => {
    $('handover-input').value = JSON.stringify(SAMPLE_FOLLOWUP, null, 2);
  });
  $('handover-deliver').addEventListener('click', deliver);
  $('handover-filter').addEventListener('change', () => refresh());
  $('handover-ledger-tab').addEventListener('change', event => paintLedger(event.target.value));
  refresh();
}
