// 档案索引页交互；普通结算页无这些元素，所有逻辑都以元素存在为前提。
if (document.body.dataset.runId === undefined && location.pathname === '/registry') {
  const feedback = document.querySelector('#feedback');
  const say = (text, ok = true) => {
    feedback.textContent = text;
    feedback.className = ok ? '' : 'error';
  };

  async function call(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || '请求失败');
    return data;
  }

  document.querySelectorAll('.js-publish').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const data = await call(`/api/registry/entries/${btn.dataset.id}/publish`, {});
        say(`批次 ${btn.dataset.id} 已发布：${data.state}${data.auditable ? '（可审计）' : '（证据不完整，暂缓审计）'}`);
        setTimeout(() => location.reload(), 600);
      } catch (e) {
        say(e.message, false);
      } finally {
        btn.disabled = false;
      }
    });
  });

  document.querySelectorAll('.js-publish-later').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const openAt = prompt('输入入口开放时间（UTC ISO8601，如 2026-12-01T00:00:00Z）');
      if (!openAt) return;
      try {
        const data = await call(`/api/registry/entries/${btn.dataset.id}/publish`, { open_at: openAt });
        say(`已定时，开放时间 ${data.open_at}；在此之前检索不到该批`);
        setTimeout(() => location.reload(), 600);
      } catch (e) {
        say(e.message, false);
      }
    });
  });

  const searchBtn = document.querySelector('#ticket-search');
  if (searchBtn) {
    searchBtn.addEventListener('click', async () => {
      const ticket = document.querySelector('#ticket-query').value.trim();
      const scope = document.querySelector('#scope').value;
      const url = `/api/registry/search?scope=${encodeURIComponent(scope)}${ticket ? `&ticket=${encodeURIComponent(ticket)}` : ''}`;
      const data = await (await fetch(url)).json();
      const box = document.querySelector('#search-result');
      if (!data.entries.length) {
        box.innerHTML = '<p class="hint">没有命中。注意：入口未到开放时间或证据不完整的批次不会出现在审计视野。</p>';
        return;
      }
      box.innerHTML = '<table class="ledger"><thead><tr><th>批号</th><th>票号</th><th>封存金额</th><th>复算旁证</th><th>可审计</th></tr></thead><tbody>' +
        data.entries.flatMap((e) => {
          const tix = ticket ? [ticket] : Object.keys(e.ticket_index);
          return tix.map((t) => {
            const idx = e.ticket_index[t] || {};
            return `<tr><td class="mono">${e.batch_id}</td><td>${t}</td><td>${idx.historical_amount ?? '—'}</td><td>${idx.recomputed_amount ?? '—'}</td><td>${e.auditable ? '是' : '否'}</td></tr>`;
          });
        }).join('') + '</tbody></table>';
    });
  }

  const ingestBtn = document.querySelector('#ingest-btn');
  if (ingestBtn) {
    ingestBtn.addEventListener('click', async () => {
      const filename = document.querySelector('#ingest-name').value.trim() || 'legacy.zip';
      const blob_b64 = document.querySelector('#ingest-blob').value.trim();
      if (!blob_b64) { say('请先粘贴 base64 封存内容', false); return; }
      ingestBtn.disabled = true;
      try {
        const data = await call('/api/registry/ingest', { filename, blob_b64 });
        const e = data.entry;
        say(data.created
          ? `迁入成功：${e.batch_id}，缺口 ${e.gaps.length} 项，漂移 ${e.drift.length} 票`
          : `批次 ${e.batch_id} 已存在，重复迁入未生成新索引`);
        setTimeout(() => location.reload(), 800);
      } catch (e) {
        say(e.message, false);
      } finally {
        ingestBtn.disabled = false;
      }
    });
  }
}
