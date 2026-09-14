'use strict';
document.addEventListener('DOMContentLoaded', function () {
  const root = $('#view-ui-automation'), dialog = $('#uiSuiteDialog');
  const rerecord = document.createElement('button');
  rerecord.type = 'button'; rerecord.id = 'uiRerecord'; rerecord.className = 'button primary'; rerecord.textContent = '重新录制为新版本'; rerecord.hidden = true;
  $('#uiSuiteForm .report-actions').append(rerecord);
  let recordedSuite = null;
  rerecord.addEventListener('click', () => { dialog.close(); window.openUiRecorder?.(recordedSuite); });
  let suites = [], files = [], editing = '', editProject = '', loadedProject = '', generation = 0, page = 1, available = false, busy = false, detailId = '';
  function fail(error) { toast(error.message || String(error), true); }
  function renderSuites() {
    const filter = $('#uiSuiteFilter').value.trim().toLowerCase();
    $('#uiSuitesBody').innerHTML = suites.filter(item => item.name.toLowerCase().includes(filter)).map(item => '<tr><td>' + esc(item.name) + '</td><td>v' + item.version + '</td><td><div class="table-actions"><button type="button" class="text-button" data-ui-edit="' + esc(item.id) + '">查看 / 编辑</button><button type="button" class="text-button" data-ui-run="' + esc(item.id) + '"' + (!available || !hasPermission('test:execute') ? ' disabled' : '') + '>运行</button><button type="button" class="text-button danger-text" data-ui-delete="' + esc(item.id) + '"' + (!hasPermission('interface:manage') ? ' disabled' : '') + '>删除</button></div></td></tr>').join('') || '<tr><td colspan="3">暂无套件，请先导入 Playwright 脚本。</td></tr>';
  }
  window.loadUiAutomation = async function () {
    if (!state.projectId) return;
    const project = state.projectId, gen = ++generation;
    if (loadedProject !== project) { page = 1; suites = []; detailId = ''; dialog.close(); $('#uiRunDetail').classList.add('hidden'); }
    loadedProject = project;
    try {
      const size = Number($('#uiRunPageSize').value);
      const data = await api('/api/ui-automation?page=' + page + '&page_size=' + size);
      if (gen !== generation || project !== state.projectId) return;
      suites = data.suites || []; const total = data.history.total; available = data.runner.available;
      $('#uiRunnerStatus').textContent = available ? '独立 UI Runner 在线 · 单任务隔离运行' : '执行环境未就绪 · 可先保存套件，配置并启动 UI Worker 后运行';
      $('#uiNewSuite').disabled = !hasPermission('interface:manage'); renderSuites();
      $('#uiRunsBody').innerHTML = data.history.items.map(run => '<tr><td>' + esc((suites.find(item => item.id === run.suite_id) || {}).name || '历史套件') + '<small class="table-subline">v' + run.suite_version + ' · ' + esc(new Date(run.created_at * 1000).toLocaleString()) + '</small></td><td>' + statusPill(run.status) + '</td><td>' + (run.result.passed ?? '—') + ' / ' + (run.result.failed ?? '—') + '</td><td><button class="text-button" type="button" data-ui-detail="' + run.id + '">详情</button>' + (['queued', 'running'].includes(run.status) && hasPermission('test:execute') ? '<button class="text-button danger-text" type="button" data-ui-stop="' + run.id + '">停止</button>' : '') + '</td></tr>').join('') || '<tr><td colspan="4">暂无运行记录</td></tr>';
      const pages = Math.max(1, Math.ceil(total / size));
      if (page > pages) { page = pages; return window.loadUiAutomation(); }
      $('#uiRunPageSummary').textContent = '共 ' + total + ' 条 · 第 ' + page + ' / ' + pages + ' 页';
      $('#uiRunPrev').disabled = page <= 1; $('#uiRunNext').disabled = page >= pages;
      if (detailId) await showDetail(detailId);
    } catch (error) { if (project === state.projectId && gen === generation) fail(error); }
  };
  function selectScript(index = 0) {
    $('#uiScriptSelect').innerHTML = files.map((file, i) => '<option value="' + i + '">' + esc(file.name) + '</option>').join('');
    $('#uiScriptSelect').value = String(index); $('#uiScriptContent').value = files[index]?.content || '';
  }
  function openEditor(item) {
    editing = item?.id || ''; editProject = state.projectId;
    const value = item?.snapshot || {}; files = (value.files || []).map(file => ({ ...file }));
    $('#uiSuiteName').value = value.name || ''; $('#uiSuiteUrl').value = value.base_url || '';
    $('#uiSuiteTimeout').value = value.timeout_seconds || 120; $('#uiSuiteParameters').value = JSON.stringify(value.parameters || {}, null, 2);
    $('#uiSuiteFiles').value = ''; $('#uiSuiteError').textContent = '';
    $('#uiSuiteDialogTitle').textContent = editing ? '套件 v' + item.version + ' · 保存将新增版本' : '导入 UI 套件';
    recordedSuite = value.recorded ? item : null;
    for (const id of ['uiSuiteName', 'uiSuiteUrl', 'uiSuiteTimeout', 'uiSuiteParameters', 'uiSuiteFiles', 'uiScriptSelect', 'uiScriptContent', 'uiSuiteExample']) $('#' + id).disabled = !!value.recorded;
    rerecord.hidden = !value.recorded; rerecord.disabled = !hasPermission('interface:manage');
    if (value.recorded) $('#uiSuiteDialogTitle').textContent = '录制套件 v' + item.version + ' · ' + value.checks + ' 个检查点 · 脚本加密保存';
    $('#uiSuiteForm [type="submit"]').disabled = !!value.recorded || !hasPermission('interface:manage'); selectScript(); dialog.showModal();
  }
  async function showDetail(id) {
    const project = state.projectId; detailId = id;
    const run = await api('/api/ui-runs/' + encodeURIComponent(id));
    if (project !== state.projectId || id !== detailId) return;
    $('#uiRunDetail').classList.remove('hidden');
    $('#uiRunDetailBody').innerHTML = '<p><b>' + esc(run.snapshot.name) + ' · v' + run.suite_version + '</b> ' + statusPill(run.status) + '</p><p>' + esc(run.result.message || '等待执行结果') + '</p><details><summary>版本与用例结果</summary><p class="muted">SHA-256：' + esc(run.snapshot.sha256) + '</p><div class="table-wrap paged-scroll" tabindex="0"><table><thead><tr><th>用例</th><th>状态</th><th>耗时 ms</th></tr></thead><tbody>' + (run.result.cases || []).map(item => '<tr><td>' + esc(item.name) + '</td><td>' + statusPill(item.status) + '</td><td>' + esc(item.duration_ms ?? '—') + '</td></tr>').join('') + '</tbody></table></div></details><div class="ui-artifacts">' + (run.result.artifacts || []).map(file => '<button type="button" class="text-button" data-ui-download="' + esc(file.name) + '" data-ui-run-id="' + id + '">' + esc(file.name) + ' · ' + fmtSize(file.size) + '</button>').join('') + '</div>';
  }
  async function mutate(button, action) {
    if (busy) return; busy = true; button.disabled = true;
    try { await action(); await window.loadUiAutomation(); } catch (error) { fail(error); }
    finally { busy = false; if (button.isConnected) button.disabled = false; }
  }
  $('#uiNewSuite').addEventListener('click', () => openEditor());
  $('#uiCloseSuite').addEventListener('click', () => dialog.close());
  $('#uiRefresh').addEventListener('click', window.loadUiAutomation);
  $('#uiSuiteFilter').addEventListener('input', renderSuites);
  $('#uiCloseDetail').addEventListener('click', () => { detailId = ''; $('#uiRunDetail').classList.add('hidden'); });
  $('#uiRunPrev').addEventListener('click', () => { page = Math.max(1, page - 1); window.loadUiAutomation(); });
  $('#uiRunNext').addEventListener('click', () => { page++; window.loadUiAutomation(); });
  $('#uiRunPageSize').addEventListener('change', () => { page = 1; window.loadUiAutomation(); });
  $('#uiScriptContent').addEventListener('input', () => { const file = files[Number($('#uiScriptSelect').value)]; if (file) file.content = $('#uiScriptContent').value; });
  $('#uiScriptSelect').addEventListener('change', () => { $('#uiScriptContent').value = files[Number($('#uiScriptSelect').value)]?.content || ''; });
  $('#uiSuiteFiles').addEventListener('change', async event => {
    const chosen = Array.from(event.target.files), project = state.projectId;
    if (chosen.length > 20 || chosen.reduce((sum, file) => sum + file.size, 0) > 500000) { $('#uiSuiteError').textContent = '最多 20 个文件，总大小 500 KB'; return; }
    const content = await Promise.all(chosen.map(async file => ({name: file.name, content: await file.text()})));
    if (project === state.projectId && dialog.open) { files = content; selectScript(); }
  });
  $('#uiSuiteExample').addEventListener('click', () => {
    files = [{name: 'smoke.spec.js', content: "const { test, expect } = require('@playwright/test');\ntest('页面内容检查', async ({ page }) => {\n  await page.setContent('<h1>烈马测试页面</h1>');\n  await expect(page.getByRole('heading')).toHaveText('烈马测试页面');\n});\n"}];
    if (!$('#uiSuiteName').value) $('#uiSuiteName').value = '页面烟测'; selectScript();
  });
  $('#uiSuiteForm').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || editProject !== state.projectId) return;
    const button = event.submitter; busy = true; button.disabled = true;
    try {
      const payload = {name: $('#uiSuiteName').value.trim(), base_url: $('#uiSuiteUrl').value.trim(), timeout_seconds: Number($('#uiSuiteTimeout').value), files, parameters: JSON.parse($('#uiSuiteParameters').value || '{}')};
      await api('/api/ui-suites' + (editing ? '/' + encodeURIComponent(editing) : ''), {method: editing ? 'PUT' : 'POST', body: payload});
      dialog.close(); await window.loadUiAutomation(); toast('UI 套件版本已保存');
    } catch (error) { $('#uiSuiteError').textContent = error.message; }
    finally { busy = false; button.disabled = false; }
  });
  root.addEventListener('click', async event => {
    const button = event.target.closest('[data-ui-edit],[data-ui-delete],[data-ui-run],[data-ui-stop],[data-ui-detail],[data-ui-download]');
    if (!button) return; const project = state.projectId;
    try {
      if (button.dataset.uiEdit) { const item = await api('/api/ui-suites/' + encodeURIComponent(button.dataset.uiEdit)); if (project === state.projectId) openEditor(item); }
      else if (button.dataset.uiDelete && confirm('删除套件全部版本？已完成的运行快照会保留。')) await mutate(button, () => api('/api/ui-suites/' + encodeURIComponent(button.dataset.uiDelete), {method: 'DELETE'}));
      else if (button.dataset.uiRun) await mutate(button, () => api('/api/ui-runs', {method: 'POST', body: {suite_id: button.dataset.uiRun}}));
      else if (button.dataset.uiStop) await mutate(button, () => api('/api/ui-runs/' + button.dataset.uiStop + '/stop', {method: 'POST'}));
      else if (button.dataset.uiDetail) await showDetail(button.dataset.uiDetail);
      else if (button.dataset.uiDownload) {
        const response = await fetch('/api/ui-runs/' + button.dataset.uiRunId + '/download?name=' + encodeURIComponent(button.dataset.uiDownload), {headers: {'X-Project-ID': project}});
        if (!response.ok) throw new Error('产物下载失败');
        const url = URL.createObjectURL(await response.blob()), link = document.createElement('a');
        link.href = url; link.download = button.dataset.uiDownload.split('/').pop(); link.click(); URL.revokeObjectURL(url);
      }
    } catch (error) { fail(error); }
  });
});
