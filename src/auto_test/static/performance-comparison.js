'use strict';
document.addEventListener('DOMContentLoaded', function () {
  const dialog = $('#performanceComparisonDialog'), result = $('#performanceComparisonResult');
  let selected = [], project = '', generation = 0, busy = false, comparisonId = '';
  const error = e => { $('#performanceComparisonError').textContent = e.message || String(e); };
  const value = x => x === null || x === undefined ? '—' : esc(x);
  const valid = gen => gen === generation && dialog.open && state.projectId === project;
  function table(headers, rows) { return '<div class="table-wrap paged-scroll" tabindex="0"><table><thead><tr>' + headers.map(x => '<th>' + esc(x) + '</th>').join('') + '</tr></thead><tbody>' + rows.map(row => '<tr>' + row.map(x => '<td>' + value(x) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>'; }
  function render(data) {
    comparisonId = data.id;
    const content = data.result;
    result.innerHTML = '<h4>服务器性能对比</h4><p>' + esc(content.note) + '</p>' + table(['服务器', '状态', 'CPU 均值 %', 'CPU 峰温 °C', '内存峰值 %', 'SHA-256 MiB/s', '相对指数'], content.rows.map(row => [row.name, statusNames[row.status] || row.status, row.cpu_avg_pct, row.cpu_peak_c, row.memory_peak_pct, row.throughput_mib_s, row.relative_index])) + '<div class="report-actions">' + ['docx', 'pdf', 'json'].map(format => '<button class="button secondary" type="button" data-comparison-download="' + format + '">下载 ' + format.toUpperCase() + '</button>').join('') + '</div>';
  }
  async function loadSaved() {
    const gen = generation, data = await api('/api/stress-comparisons');
    if (!valid(gen)) return;
    $('#savedPerformanceComparison').innerHTML = '<option value="">选择已保存报告</option>' + data.comparisons.map(item => '<option value="' + esc(item.id) + '">' + esc(new Date(item.created_at * 1000).toLocaleString()) + ' · ' + esc(item.id.slice(0, 8)) + '</option>').join('');
  }
  $('#openPerformanceComparison').addEventListener('click', async () => {
    project = state.projectId; selected = []; comparisonId = ''; const gen = ++generation;
    result.textContent = ''; $('#performanceComparisonError').textContent = ''; $('#performanceRunOptions').textContent = '正在读取任务…'; dialog.showModal();
    $('#createPerformanceComparison').disabled = !hasPermission('report:manage');
    try {
      const data = await api('/api/stress-jobs?page=1&page_size=100');
      if (!valid(gen)) return;
      const jobs = (data.jobs || []).filter(item => ['succeeded', 'failed', 'interrupted', 'stopped'].includes(item.status));
      $('#performanceRunOptions').innerHTML = jobs.map(job => '<label class="switch-row"><input type="checkbox" value="' + esc(job.id) + '" data-compare-run><span><b>' + esc(job.target_name || job.target_host || '服务器') + '</b><small>' + esc(new Date(job.created_at * 1000).toLocaleString()) + ' · ' + esc(statusNames[job.status] || job.status) + ' · ' + (job.options.duration || '—') + ' 秒</small></span></label>').join('') || '<p class="muted">暂无已结束任务。</p>';
      await loadSaved();
    } catch (e) { if (valid(gen)) error(e); }
  });
  $('#closePerformanceComparison').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { generation++; });
  $('#performanceRunOptions').addEventListener('change', event => {
    const input = event.target.closest('[data-compare-run]'); if (!input) return;
    if (input.checked && selected.length >= 6) { input.checked = false; error(new Error('最多选择 6 个任务')); return; }
    selected = input.checked ? [...selected, input.value] : selected.filter(id => id !== input.value);
  });
  async function action(button, work) {
    if (busy || state.projectId !== project) return; busy = true; button.disabled = true;
    const gen = generation; $('#performanceComparisonError').textContent = '';
    try { await work(gen); } catch (e) { if (valid(gen)) error(e); }
    finally { busy = false; button.disabled = false; }
  }
  $('#createPerformanceComparison').addEventListener('click', event => action(event.target, async gen => {
    if (selected.length < 2) throw new Error('请先选择 2～6 个任务');
    const data = await api('/api/stress-comparisons', {method: 'POST', body: {job_ids: selected, allow_mismatch: $('#performanceAllowMismatch').checked}});
    if (valid(gen)) { render(data); await loadSaved(); }
  }));
  $('#savedPerformanceComparison').addEventListener('change', event => action(event.target, async gen => {
    if (!event.target.value) return;
    const data = await api('/api/stress-comparisons/' + encodeURIComponent(event.target.value)); if (valid(gen)) render(data);
  }));
  $('#showPerformanceTrend').addEventListener('click', event => action(event.target, async gen => {
    if (!selected.length) throw new Error('请先选择一个已结束任务');
    const data = await api('/api/stress-trends?job_id=' + encodeURIComponent(selected[0]));
    if (valid(gen)) result.innerHTML = '<h4>服务器历史趋势</h4><p>' + esc(data.note) + '</p>' + table(['时间', '状态', 'CPU 均值 %', 'CPU 峰温 °C', 'SHA-256 MiB/s', '时长 / 进程'], data.points.map(point => [new Date(point.created_at * 1000).toLocaleString(), statusNames[point.status] || point.status, point.cpu_avg_pct, point.cpu_peak_c, point.benchmark_mib_s, (point.parameters.duration ?? '—') + ' s / ' + (point.parameters.workers ?? '—')]));
  }));
  result.addEventListener('click', event => {
    const button = event.target.closest('[data-comparison-download]'); if (!button) return;
    action(button, async () => {
      const format = button.dataset.comparisonDownload;
      const response = await fetch('/api/stress-comparisons/' + comparisonId + '/download/' + format, {headers: {'X-Project-ID': project}});
      if (!response.ok) throw new Error('下载失败');
      const url = URL.createObjectURL(await response.blob()), link = document.createElement('a');
      link.href = url; link.download = 'server-comparison.' + format; link.click(); URL.revokeObjectURL(url);
    });
  });
  $('#stressCpuBenchmark').addEventListener('change', event => {
    if (!event.target.checked) return;
    applyStressPreset('custom');
    $$('input[name="stressMode"]').forEach(input => { input.checked = input.value === 'cpu' || input.value === 'monitor'; });
    $('#stressCpuLoad').value = '100'; $('#stressWorkers').value = '1'; $('#stressDuration').value = '60';
    $('#stressPresetHint').textContent = 'SHA-256 基准：CPU 满负载测量吞吐，GPU 仅监控。'; updateStressStartState();
  });
});
