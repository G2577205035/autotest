"use strict";

document.addEventListener("DOMContentLoaded", function () {
  const dialog = document.querySelector("#interfaceAutomationDialog");
  const body = dialog.querySelector("[data-automation-body]");
  let scenarioId = "", projectId = "", requestId = 0, busy = false;
  const read = name => body.querySelector('[name="' + name + '"]')?.value || "";
  const error = message => { const target = body.querySelector("[data-automation-error]"); if (target) { target.textContent = message; target.classList.remove("hidden"); } else toast(message, true); };

  async function refresh() {
    const request = ++requestId, id = scenarioId, project = projectId;
    const data = await api("/api/interface-scenarios/" + encodeURIComponent(id) + "/automation");
    if (request !== requestId || project !== state.projectId || !dialog.open) return;
    const datasets = data.datasets || [], schedules = data.schedules || [], trend = data.trend || {};
    const options = '<option value="">仅使用场景默认参数</option>' + datasets.map(item => '<option value="' + esc(item.id) + '">' + esc(item.name) + '（' + Number(item.row_count) + ' 行）</option>').join("");
    body.innerHTML = '<div class="interface-form-error hidden" role="alert" data-automation-error></div>' +
      '<div class="automation-data-grid"><section><h4>参数数据集</h4><p class="muted">每行参数执行一次完整场景，分别生成结果与报告。参数名使用接口中的变量名。</p>' +
      (interfaceCanManage() ? '<form data-import-dataset><label class="field"><span>数据集名称</span><input name="datasetName" required maxlength="120" placeholder="例如：不同订单类型"></label><label class="field"><span>导入 JSON / CSV 文件</span><input type="file" accept=".json,.csv" data-dataset-file></label><label class="field"><span>格式</span><select name="datasetFormat"><option value="json">JSON 数组</option><option value="csv">CSV（首行为参数名）</option></select></label><label class="field"><span>参数内容（1～100 行）</span><textarea name="datasetContent" rows="5" required placeholder=\'[{"ORDER_TYPE":"normal"},{"ORDER_TYPE":"urgent"}]\'></textarea></label><p class="muted">密码和 Token 请使用加密环境变量，不能放入数据集。</p><button class="button secondary" type="submit">保存数据集</button></form>' : '') +
      '<div class="automation-datasets">' + (datasets.map(item => '<details><summary>' + esc(item.name) + ' · ' + Number(item.row_count) + ' 行</summary><pre>' + esc(JSON.stringify(item.rows, null, 2)) + '</pre>' + (interfaceCanManage() ? '<button type="button" class="text-button danger-text" data-delete-dataset="' + esc(item.id) + '">删除数据集</button>' : '') + '</details>').join("") || '<p class="muted">暂无数据集</p>') + '</div></section>' +
      '<section><h4>执行与定时计划</h4><label class="field"><span>使用的数据集</span><select name="runDataset">' + options + '</select></label><label class="field"><span>最大并发</span><select name="dataConcurrency">' + [1, 2, 3, 4, 5].map(n => '<option value="' + n + '"' + (n === 3 ? ' selected' : '') + '>' + n + '</option>').join("") + '</select></label>' +
      (interfaceCanExecute() ? '<button class="button primary" type="button" data-run-dataset>立即运行数据集</button>' : '') +
      (interfaceCanManage() ? '<form data-create-schedule><label class="field"><span>执行间隔（分钟）</span><input type="number" name="intervalMinutes" min="1" max="43200" value="60" required></label><p class="muted">首轮在一个间隔后执行。上轮未结束时跳过本轮，停机期间错过的周期不补跑。</p><button class="button secondary" type="submit">启用定时计划</button></form>' : '') +
      '<div class="automation-schedules">' + (schedules.map(item => '<article><strong>每 ' + (item.interval_seconds / 60) + ' 分钟 · ' + (item.enabled ? '已启用' : '已暂停') + '</strong><p>' + esc(datasets.find(d => d.id === item.dataset_id)?.name || '场景默认参数') + '</p><p>下次：' + (item.enabled ? esc(fmtTime(item.next_run_at)) : '—') + '</p>' + (item.last_error ? '<p class="muted">' + esc(item.last_error) + '</p>' : '') + (interfaceCanManage() ? '<div class="run-actions"><button type="button" class="text-button" data-toggle-schedule="' + esc(item.id) + '" data-enabled="' + (item.enabled ? 'false' : 'true') + '">' + (item.enabled ? '暂停' : '恢复') + '</button><button type="button" class="text-button danger-text" data-delete-schedule="' + esc(item.id) + '">删除</button></div>' : '') + '</article>').join("") || '<p class="muted">暂无定时计划</p>') + '</div></section></div>' +
      '<section class="automation-trend"><h4>最近 50 次完成记录</h4><p>通过率：<strong>' + (trend.success_rate == null ? '暂无数据' : trend.success_rate + '%') + '</strong> · P95 耗时：<strong>' + (trend.p95_ms == null ? '未采集' : Number(trend.p95_ms).toFixed(1) + ' ms') + '</strong></p><p class="muted">包含数据集各行和不同运行参数，仅用于观察历史；停止的任务也计入总次数。</p><div class="table-wrap paged-scroll" tabindex="0"><table><thead><tr><th>时间</th><th>运行</th><th>结果</th><th>耗时</th></tr></thead><tbody>' + ((trend.points || []).slice().reverse().map(item => '<tr><td>' + esc(fmtTime(item.created_at)) + '</td><td>' + esc(item.scenario_name) + '</td><td>' + interfaceScenarioStatus(item.status) + '</td><td>' + (item.elapsed_ms == null ? '未采集' : Number(item.elapsed_ms).toFixed(1) + ' ms') + '</td></tr>').join('') || '<tr><td colspan="4">暂无完成记录</td></tr>') + '</tbody></table></div></section>';
  }

  async function mutation(action) {
    if (busy) return;
    if (projectId !== state.projectId) { dialog.close(); return; }
    busy = true;
    body.querySelectorAll('button').forEach(button => button.disabled = true);
    try { await action(); if (dialog.open && projectId === state.projectId) await refresh(); }
    catch (e) { error(e.message); }
    finally { busy = false; body.querySelectorAll('button').forEach(button => button.disabled = false); }
  }
  document.addEventListener('click', async event => {
    const open = event.target.closest('[data-interface-automation]');
    if (!open) return;
    scenarioId = open.dataset.interfaceAutomation; projectId = state.projectId;
    dialog.querySelector('h3').textContent = (state.interfaceScenarios.find(item => item.id === scenarioId)?.name || '接口场景') + ' · 数据与计划';
    body.innerHTML = '<p>正在读取数据集、计划与历史…</p>';
    dialog.showModal();
    try { await refresh(); } catch (e) { error(e.message); }
  });
  dialog.querySelector('[data-automation-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { requestId++; });
  body.addEventListener('change', async event => {
    if (!event.target.matches('[data-dataset-file]')) return;
    const file = event.target.files[0]; if (!file) return;
    if (file.size > 1_000_000) { error('数据集不能超过 1 MB'); return; }
    const project = projectId, id = scenarioId, content = await file.text();
    if (project !== projectId || id !== scenarioId || !dialog.open) return;
    body.querySelector('[name="datasetFormat"]').value = file.name.toLowerCase().endsWith('.csv') ? 'csv' : 'json';
    body.querySelector('[name="datasetContent"]').value = content;
    if (!read('datasetName')) body.querySelector('[name="datasetName"]').value = file.name.replace(/\.(csv|json)$/i, '').slice(0, 120);
  });
  body.addEventListener('submit', event => {
    event.preventDefault();
    if (event.target.matches('[data-import-dataset]')) {
      const payload = { name: read('datasetName'), content: read('datasetContent'), format: read('datasetFormat') };
      mutation(() => api('/api/interface-scenarios/' + encodeURIComponent(scenarioId) + '/datasets', { method: 'POST', body: payload }));
    } else if (event.target.matches('[data-create-schedule]')) {
      const payload = { dataset_id: read('runDataset'), interval_seconds: Number(read('intervalMinutes')) * 60, concurrency: Number(read('dataConcurrency')) };
      mutation(() => api('/api/interface-scenarios/' + encodeURIComponent(scenarioId) + '/schedules', { method: 'POST', body: payload }));
    }
  });
  body.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.matches('[data-run-dataset]')) {
      if (!read('runDataset')) { error('立即运行前请选择一个数据集'); return; }
      const payload = { dataset_id: read('runDataset'), concurrency: Number(read('dataConcurrency')) };
      mutation(async () => { const result = await api('/api/interface-scenarios/' + encodeURIComponent(scenarioId) + '/data-execute', { method: 'POST', body: payload }); toast('已排队 ' + result.queued + ' 条数据运行，可在场景执行记录查看'); });
    } else if (button.matches('[data-toggle-schedule]')) mutation(() => api('/api/interface-schedules/' + encodeURIComponent(button.dataset.toggleSchedule), { method: 'PATCH', body: { enabled: button.dataset.enabled === 'true' } }));
    else if (button.matches('[data-delete-schedule]') && confirm('删除这个定时计划？已提交的运行会保留。')) mutation(() => api('/api/interface-schedules/' + encodeURIComponent(button.dataset.deleteSchedule), { method: 'DELETE' }));
    else if (button.matches('[data-delete-dataset]') && confirm('删除这个数据集？历史运行快照会保留。')) mutation(() => api('/api/interface-datasets/' + encodeURIComponent(button.dataset.deleteDataset), { method: 'DELETE' }));
  });
});
