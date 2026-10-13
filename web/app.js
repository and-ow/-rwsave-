/* .rwsave 修改器 —— 前端交互 */
'use strict';

const S = {
  saves: [],
  path: null,          // 当前存档
  units: [],           // 扁平行
  teams: [],
  summary: null,
  sel: null,           // 当前选中单位下标
  picked: new Set(),   // 批量勾选
  edits: new Map(),    // "unit|field" -> value
  fieldMeta: {},       // "unit|field" -> {label,type,ui}
  page: 0,
  pageSize: 200,
  sort: { key: 'i', dir: 1 },
  showDanger: false,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

async function api(path, opts) {
  const r = await fetch(path, opts);
  let j = null;
  try { j = await r.json(); } catch (_) { /* 非 JSON */ }
  if (!r.ok || (j && j.error)) throw new Error((j && j.error) || ('HTTP ' + r.status));
  return j;
}

let toastTimer = null;
function toast(msg, kind) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + (kind || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), kind === 'err' ? 6000 : 3000);
}

/* ---------------------------------------------------------------- 存档列表 */

async function loadSaves() {
  const d = await api('/api/saves');
  S.saves = d.saves;
  const box = $('saveList');
  if (!d.saves.length) {
    box.innerHTML = '<div class="empty">没有找到 .rwsave<br>用 --dir 指定存档目录</div>';
    return;
  }
  box.innerHTML = d.saves.map((s, i) => `
    <div class="saveitem" data-i="${i}">
      <div class="nm" title="${esc(s.name)}">${esc(s.name)}</div>
      <div class="meta">${fmtSize(s.size)} · ${esc(s.mtime)}</div>
    </div>`).join('');
  box.querySelectorAll('.saveitem').forEach((el) => {
    el.onclick = () => openSave(S.saves[+el.dataset.i].path);
  });
}

function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(2) + ' MB';
}

async function openSave(path) {
  S.path = path;
  S.picked.clear();
  S.edits.clear();
  S.sel = null;
  setStep(2);
  try {
    const d = await api('/api/save?path=' + encodeURIComponent(path));
    S.units = d.units;
    S.teams = d.teams;
    S.summary = d.summary;
    (d.pending || []).forEach((p) => {
      S.edits.set(p.unit + '|' + p.field, p.value);
    });
    document.querySelectorAll('.saveitem').forEach((el) => {
      el.classList.toggle('on', S.saves[+el.dataset.i].path === path);
    });
    const sm = d.summary;
    $('unitTitle').textContent = sm.map_path || '(未知地图)';
    $('unitCount').textContent =
      `${sm.object_count} 个对象 · ${sm.team_count} 队 · ${sm.game_time_millis != null ? (sm.game_time_millis / 1000).toFixed(1) + 's' : '—'}`;
    buildTeamFilter();
    $('filterBar').classList.remove('hidden');
    render();
    renderFieldPane(null);
    updatePending();
  } catch (e) {
    toast('打开失败：' + e.message, 'err');
  }
}

function buildTeamFilter() {
  const sel = $('fTeam');
  const seen = new Set();
  sel.innerHTML = '<option value="">全部队伍</option>';
  S.units.forEach((u) => {
    const t = u.team;
    if (t == null || seen.has(t)) return;
    seen.add(t);
    const name = (S.teams.find((x) => x.team_id === t) || {}).name || '';
    const o = document.createElement('option');
    o.value = t;
    o.textContent = `队伍 ${t}${name ? ' · ' + name : ''}`;
    sel.appendChild(o);
  });
}

/* ---------------------------------------------------------------- 过滤 / 排序 / 分页 */

function visible() {
  const q = $('q').value.trim().toLowerCase();
  const team = $('fTeam').value;
  const kind = $('fKind').value;
  const hp = $('fHp').value;
  let rows = S.units.filter((u) => {
    if (team !== '' && String(u.team) !== team) return false;
    if (kind !== '' && String(u.kind) !== kind) return false;
    if (q) {
      const hay = (u.type_name || '') + ' ' + u.object_id + ' ' + (u.kind_name || '');
      if (!hay.toLowerCase().includes(q)) return false;
    }
    if (hp) {
      const r = u.hp_ratio;
      if (r == null) return false;
      if (hp === 'hurt' && r >= 0.5) return false;
      if (hp === 'near' && r >= 0.2) return false;
      if (hp === 'full' && r < 0.999) return false;
    }
    return true;
  });
  const k = S.sort.key, dir = S.sort.dir;
  rows.sort((a, b) => {
    let x = a[k], y = b[k];
    if (x == null) x = dir;
    if (y == null) y = dir;
    if (typeof x === 'string' || typeof y === 'string') {
      return String(x).localeCompare(String(y)) * dir;
    }
    return ((x < y) ? -1 : (x > y) ? 1 : 0) * dir;
  });
  return rows;
}

function render() {
  const rows = visible();
  const ps = S.pageSize;
  const pages = Math.max(1, Math.ceil(rows.length / ps));
  if (S.page >= pages) S.page = pages - 1;
  const page = rows.slice(S.page * ps, S.page * ps + ps);

  // 预先统计每个单位被改的字段数，避免每行都遍历一遍 edits
  const pendCount = {};
  S.edits.forEach((_v, k) => {
    const u = +k.split('|')[0];
    pendCount[u] = (pendCount[u] || 0) + 1;
  });

  const body = $('unitBody');
  if (!page.length) {
    body.innerHTML = '';
    $('unitEmpty').classList.remove('hidden');
    $('unitEmpty').textContent = S.units.length ? '没有符合筛选条件的单位' : '该存档没有对象';
  } else {
    $('unitEmpty').classList.add('hidden');
    body.innerHTML = page.map((u) => {
      const r = u.hp_ratio;
      const cls = r == null ? '' : r < 0.2 ? 'low' : r < 0.5 ? 'mid' : '';
      const pct = r == null ? 0 : Math.max(0, Math.min(100, r * 100));
      const pend = pendCount[u.i] || 0;
      const flags = [];
      if (u.is_dead) flags.push('<span class="tag dead">死</span>');
      return `<tr data-i="${u.i}" class="${S.sel === u.i ? 'on' : ''}">
        <td class="c-pick"><input type="checkbox" data-pick="${u.i}" ${S.picked.has(u.i) ? 'checked' : ''}></td>
        <td class="num">${u.i}</td>
        <td title="${esc(u.type_name)}">${esc(u.type_name || '?')}</td>
        <td class="c-team num">${u.team == null ? '<span class="dot">·</span>' : u.team}</td>
        <td class="c-hp num">${fmtNum(u.hp)} / ${fmtNum(u.max_hp)}</td>
        <td class="c-bar"><span class="hpbar ${cls}"><i style="width:${pct}%"></i></span></td>
        <td class="num">${fmtNum(u.pos_x)}</td>
        <td class="num">${fmtNum(u.pos_y)}</td>
        <td class="num">${fmtNum(u.rotation, 2)}</td>
        <td class="num">${u.kill_count == null ? '·' : u.kill_count}</td>
        <td class="c-flag">${flags.join('') || '<span class="dot">·</span>'}</td>
        <td class="c-edit">${pend ? '<span class="tag edit">' + pend + '</span>' : ''}</td>
      </tr>`;
    }).join('');
  }

  body.querySelectorAll('tr').forEach((tr) => {
    tr.onclick = (ev) => {
      if (ev.target.matches('input[type=checkbox]')) return;
      select(+tr.dataset.i);
    };
  });
  body.querySelectorAll('input[data-pick]').forEach((cb) => {
    cb.onclick = (ev) => {
      ev.stopPropagation();
      const i = +cb.dataset.pick;
      if (cb.checked) S.picked.add(i); else S.picked.delete(i);
      updateBulkBar();
    };
  });

  $('pgInfo').textContent = `${rows.length} 个单位 · 第 ${S.page + 1}/${pages} 页`;
  $('pager').classList.toggle('hidden', rows.length === 0);
  $('pgPrev').disabled = S.page === 0;
  $('pgNext').disabled = S.page >= pages - 1;

  document.querySelectorAll('thead th[data-sort]').forEach((th) => {
    th.classList.toggle('sorted', th.dataset.sort === S.sort.key);
  });
  updateBulkBar();
}

function fmtNum(v, d) {
  if (v == null) return '·';
  const n = Number(v);
  if (!isFinite(n)) return '·';
  if (d != null) return n.toFixed(d);
  return Math.abs(n) >= 1000 ? n.toFixed(0) : (Math.round(n * 100) / 100).toString();
}

/* ---------------------------------------------------------------- 字段面板 */

async function select(i) {
  S.sel = i;
  setStep(3);
  render();
  $('fieldPane').innerHTML = '<div class="empty">加载中…</div>';
  try {
    const u = await api(`/api/unit?path=${encodeURIComponent(S.path)}&index=${i}`);
    renderFieldPane(u);
  } catch (e) {
    $('fieldPane').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function renderFieldPane(u) {
  if (!u) {
    $('unitMeta').innerHTML = '';
    $('fieldPane').innerHTML = '<div class="empty">在左侧列表点一个单位</div>';
    return;
  }
  const warn = [];
  if (u.duplicate_offsets && Object.keys(u.duplicate_offsets).length)
    warn.push('检测到重复字节偏移（已阻止写该字段）');
  if (u.rejected && u.rejected.length)
    warn.push(`${u.rejected.length} 个字段未通过回读校验，不可编辑`);
  $('unitMeta').innerHTML = `
    <div><b>#${u.index}</b> ${esc(u.type_name || '?')}
      &nbsp;<span class="muted">${esc(u.kind_name || '')}</span></div>
    <div>objectId=${u.object_id} &nbsp; offset=0x${(u.offset || 0).toString(16).toUpperCase()}
      &nbsp; 长度=${u.length ?? '?'} B</div>
    ${warn.length ? '<div class="warnline">⚠ ' + warn.map(esc).join('<br>⚠ ') + '</div>' : ''}`;

  let html = '';
  u.groups.forEach((g) => {
    const fields = g.fields.filter((f) => S.showDanger || !f.danger);
    if (!fields.length) return;
    html += `<div class="fgroup"><h4>${esc(g.group)}</h4>`;
    fields.forEach((f) => {
      const key = u.index + '|' + f.name;
      const changed = S.edits.has(key);
      S.fieldMeta[key] = f;
      if (f.locked) {
        html += `<div class="frow locked">
          <div class="flabel"><span>${esc(f.label)}</span></div>
          <div class="unit">只读</div>
          <div class="lockmsg">🔒 ${esc(f.lock_reason || '')}</div></div>`;
        return;
      }
      let ctrl;
      if (f.ui === 'b') {
        const on = changed ? !!S.edits.get(key) : !!f.value;
        ctrl = `<span class="sw"><input type="checkbox" data-f="${esc(f.name)}" data-t="b" ${on ? 'checked' : ''}><i></i></span>`;
      } else {
        const val = changed ? S.edits.get(key) : (f.value == null ? '' : f.value);
        ctrl = `<div class="finput">
            <input type="text" data-f="${esc(f.name)}" data-t="${f.ui}" value="${esc(val)}"
                   class="${changed ? 'changed' : ''}">
            <span class="unit">${f.ui === 'i' ? 'int' : 'f32'}</span>
          </div>`;
      }
      html += `<div class="frow ${changed ? 'changed' : ''}" data-row="${esc(f.name)}">
        <div class="flabel">
          <span>${esc(f.label)}</span>
          ${f.danger ? '<span class="fdanger" title="语义未完全确认，修改可能影响游戏行为">⚠</span>' : ''}
          <span class="fkey">${esc(f.name)}</span>
        </div>${ctrl}</div>`;
    });
    html += '</div>';
  });

  const pane = $('fieldPane');
  pane.innerHTML = html || '<div class="empty">该对象没有可编辑字段</div>';
  pane.querySelectorAll('input[data-f]').forEach((el) => {
    const f = el.dataset.f;
    if (el.dataset.t === 'b') el.onchange = () => stage(u.index, f, el.checked);
    else {
      el.onchange = () => stage(u.index, f, el.value);
      el.onkeydown = (e) => { if (e.key === 'Enter') el.blur(); };
    }
  });
}

async function stage(unit, field, value) {
  try {
    await api('/api/edit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: S.path, unit, field, value }),
    });
    S.edits.set(unit + '|' + field, value);
    updatePending();
    render();
    // 就地刷新该行样式，避免整面板重载
    const row = $('fieldPane').querySelector(`[data-row="${CSS.escape(field)}"]`);
    if (row) {
      row.classList.add('changed');
      const inp = row.querySelector('input:not([data-t="b"])');
      if (inp) inp.classList.add('changed');
    }
  } catch (e) {
    toast(e.message, 'err');
    if (S.sel != null) select(S.sel);
  }
}

/* ---------------------------------------------------------------- 批量 */

function updateBulkBar() {
  const n = S.picked.size;
  $('bulkBar').classList.toggle('hidden', n === 0);
  $('selCount').textContent = n;
  if (n && !$('bulkField').options.length) bulkFill();
}

async function bulkFill(i) {
  const idx = i != null ? i : (S.units.find((u) => S.picked.has(u.i)) || {}).i;
  if (idx == null) return;
  const sel = $('bulkField');
  if (sel.dataset.unit == idx && sel.options.length) return;
  try {
    const u = await api(`/api/unit?path=${encodeURIComponent(S.path)}&index=${idx}`);
    const prev = sel.value;
    sel.innerHTML = '';
    u.groups.forEach((g) => g.fields.forEach((f) => {
      if (f.locked) return;
      const o = document.createElement('option');
      o.value = f.name;
      o.textContent = g.group + ' · ' + f.label;
      o.dataset.ui = f.ui;
      o.dataset.type = f.type;
      sel.appendChild(o);
    }));
    sel.dataset.unit = idx;
    if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  } catch (_) { /* 忽略 */ }
}

async function applyBulk() {
  const field = $('bulkField').value;
  const raw = $('bulkVal').value;
  if (!field || raw === '') { toast('请选字段并填新值', 'err'); return; }
  const opt = $('bulkField').selectedOptions[0];
  const value = opt.dataset.ui === 'b' ? /^(1|true|on|是)$/i.test(raw) : raw;
  const targets = [...S.picked];
  try {
    const r = await api('/api/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: S.path, field, value, units: targets }),
    });
    targets.forEach((u) => S.edits.set(u + '|' + field, value));
    toast(`已应用 ${r.applied} 处修改${r.error_count ? '，' + r.error_count + ' 处失败' : ''}`,
          r.error_count ? 'err' : 'ok');
    $('bulkVal').value = '';
    updatePending();
    render();
    if (S.sel != null) select(S.sel);
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ---------------------------------------------------------------- 保存 */

function updatePending() {
  const n = S.edits.size;
  $('pendingBadge').textContent = `${n} 处待保存`;
  $('pendingBadge').classList.toggle('hidden', n === 0);
  $('btnSave').classList.toggle('hidden', n === 0);
  $('btnReset').classList.toggle('hidden', n === 0);
  if (n) setStep(4);
}

function openSaveModal() {
  const cnt = S.edits.size;
  const units = new Set([...S.edits.keys()].map((k) => +k.split('|')[0]));
  const byField = {};
  S.edits.forEach((v, k) => {
    const f = k.split('|')[1];
    byField[f] = (byField[f] || 0) + 1;
  });
  $('savePreview').innerHTML = [
    `<div>将修改 <b>${units.size}</b> 个单位，共 <b>${cnt}</b> 个字段：</div>`,
    ...Object.entries(byField)
      .sort((a, b) => b[1] - a[1])
      .map(([f, n]) => `<div>· ${esc(f)} × ${n}</div>`),
  ].join('');
  $('modal').classList.remove('hidden');
  setStep(4);
}

async function confirmSave() {
  const body = { path: S.path };
  const o = $('outDir').value.trim();
  const nm = $('outName').value.trim();
  if (o) body.out_dir = o;
  if (nm) body.name = nm;
  $('btnConfirmSave').disabled = true;
  try {
    const r = await api('/api/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    $('modal').classList.add('hidden');
    toast(`已保存 ${r.name}（${r.edits} 处修改，自检通过）`, 'ok');
    S.edits.clear();
    updatePending();
    render();
    if (S.sel != null) select(S.sel);
    loadSaves();
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
  } finally {
    $('btnConfirmSave').disabled = false;
  }
}

async function resetAll() {
  if (!confirm('确定放弃全部待保存的修改？')) return;
  try {
    await api('/api/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: S.path }),
    });
    S.edits.clear();
    updatePending();
    render();
    if (S.sel != null) select(S.sel);
    toast('已放弃全部修改', 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ---------------------------------------------------------------- 杂项 */

function setStep(n) {
  document.querySelectorAll('.step').forEach((el) => {
    el.classList.toggle('on', +el.dataset.step <= n);
  });
}

/* ---------------------------------------------------------------- 绑定 */

function init() {
  loadSaves().catch((e) => toast(e.message, 'err'));

  $('btnRescan').onclick = () => loadSaves();
  $('q').oninput = () => { S.page = 0; render(); };
  $('fTeam').onchange = $('fKind').onchange = $('fHp').onchange = () => { S.page = 0; render(); };
  $('btnClearFilter').onclick = () => {
    $('q').value = ''; $('fTeam').value = ''; $('fKind').value = ''; $('fHp').value = '';
    S.page = 0; render();
  };

  document.querySelectorAll('thead th[data-sort]').forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.sort;
      S.sort = { key: k, dir: S.sort.key === k ? -S.sort.dir : 1 };
      S.page = 0; render();
    };
  });

  $('pickAll').onchange = (e) => {
    const rows = visible().slice(S.page * S.pageSize, (S.page + 1) * S.pageSize);
    rows.forEach((u) => { if (e.target.checked) S.picked.add(u.i); else S.picked.delete(u.i); });
    render();
  };
  $('pgPrev').onclick = () => { if (S.page > 0) { S.page--; render(); } };
  $('pgNext').onclick = () => { S.page++; render(); };
  $('pgSize').onchange = (e) => { S.pageSize = +e.target.value; S.page = 0; render(); };

  $('bulkField').onchange = () => {
    const opt = $('bulkField').selectedOptions[0];
    const ui = opt && opt.dataset.ui;
    $('bulkVal').placeholder = ui === 'b' ? 'true / false' : (ui === 'i' ? '整数' : '数值');
  };
  $('btnBulk').onclick = applyBulk;
  $('btnSelNone').onclick = () => { S.picked.clear(); render(); };

  $('showDanger').onchange = (e) => {
    S.showDanger = e.target.checked;
    if (S.sel != null) select(S.sel);
  };

  $('btnSave').onclick = openSaveModal;
  $('btnCancelSave').onclick = () => $('modal').classList.add('hidden');
  $('btnConfirmSave').onclick = confirmSave;
  $('btnReset').onclick = resetAll;

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $('modal').classList.add('hidden');
  });
}

document.addEventListener('DOMContentLoaded', init);