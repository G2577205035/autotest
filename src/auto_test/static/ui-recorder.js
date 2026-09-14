'use strict';
document.addEventListener('DOMContentLoaded', function () {
  const dialog = $('#uiRecorderDialog'), frame = $('#uiRecorderFrame');
  let recording = null, project = '', suiteId = '', generation = 0, busy = false, timer = null, polling = false;
  const active = item => item && ['queued', 'starting', 'recording', 'saving'].includes(item.status);
  const valid = gen => dialog.open && gen === generation && project === state.projectId && hasPermission('interface:manage');
  const error = message => { $('#uiRecorderError').textContent = message || ''; };
  let nativeSize = false, ownsFullscreen = false, awaitingSave = false;
  function checkHelp(open) {
    $('#uiRecorderCheckGuide').hidden = !open;
    $('#uiRecorderCheckHelp').setAttribute('aria-expanded', String(open));
  }
  function syncScale() {
    frame.contentWindow?.postMessage({type: 'liema-recorder-scale', mode: nativeSize ? 'native' : 'fit'}, location.origin);
    $('#uiRecorderScale').textContent = nativeSize ? '适应窗口' : '原始大小';
    $('#uiRecorderScale').setAttribute('aria-pressed', String(nativeSize));
    $('#uiRecorderScale').title = nativeSize ? '缩放显示完整的浏览器与代码窗口' : '按原始大小显示，可滚动查看完整画面';
  }
  frame.addEventListener('load', syncScale);
  window.addEventListener('message', event => {
    if (event.origin === location.origin && event.source === frame.contentWindow && event.data?.type === 'liema-recorder-ready') syncScale();
  });
  function environmentNotice(result) {
    $('#uiRecorderEnvironment').textContent = result.message;
    $('#uiRecorderEnvironmentTitle').textContent = result.available ? '录制服务已就绪' : result.configured ? '录制服务尚未启动或缺少组件' : '首次使用，请先配置录制服务';
    $('#uiRecorderNotice').classList.toggle('needs-setup', !result.available);
  }
  function disconnect() { frame.removeAttribute('src'); frame.hidden = true; }
  function close() {
    if (ownsFullscreen && document.fullscreenElement === document.documentElement) document.exitFullscreen().catch(() => {});
    generation++; clearInterval(timer); timer = null; recording = null; awaitingSave = false; disconnect(); dialog.close();
  }
  async function request(path, method = 'GET', body) {
    // Capture the project when opening; never let a delayed response switch scope.
    const response = await fetch('/api/ui-recordings' + path, {method, credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Project-ID': project, 'X-CSRF-Token': state.csrfToken},
      body: body ? JSON.stringify(body) : undefined});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '录制请求失败，请检查填写内容');
    return data;
  }
  function render(item) {
    const needsHelp = awaitingSave && item.status === 'recording';
    recording = item;
    const running = active(item), ready = item.status === 'recording';
    dialog.classList.toggle('is-recording', running);
    $('#uiRecorderForm').classList.add('hidden'); $('#uiRecorderResume').classList.add('hidden');
    $('#uiRecorderLive').classList.remove('hidden');
    $('#uiRecorderState').textContent = item.message;
    $('#uiRecorderSave').disabled = !ready || busy;
    $('#uiRecorderReconnect').disabled = !ready;
    $('#uiRecorderScale').disabled = !ready;
    $('#uiRecorderFullscreen').disabled = !running;
    $('#uiRecorderCheckHelp').disabled = !ready;
    if (needsHelp) checkHelp(true);
    if (ready || !running) awaitingSave = false;
    $('#uiRecorderCancel').textContent = running ? '取消本次录制' : '关闭';
    $('#uiRecorderDeadline').textContent = running ? '剩余 ' + Math.max(0, Math.ceil((item.expires_at * 1000 - Date.now()) / 60000)) + ' 分钟 · 断开页面 90 秒后自动回收' : '';
    if (ready && !frame.getAttribute('src')) { frame.hidden = false; frame.src = '/ui-recorder/' + encodeURIComponent(item.id) + '/desktop'; }
    if (!running) {
      checkHelp(false);
      if (ownsFullscreen && document.fullscreenElement === document.documentElement) document.exitFullscreen().catch(() => {});
      clearInterval(timer); timer = null; disconnect();
      if (item.status === 'saved') {
        $('#uiRecorderState').textContent = '已保存“' + item.name + '” v' + item.suite_version + '，包含 ' + item.checks + ' 个检查点。关闭后可在套件列表运行。';
        window.loadUiAutomation?.();
      }
    }
  }
  async function poll() {
    if (polling || busy || !recording || !dialog.open) return;
    if (project !== state.projectId || !hasPermission('interface:manage')) { close(); return; }
    const gen = generation, id = recording.id;
    polling = true;
    try {
      const item = await request('/' + id + '/heartbeat', 'POST');
      if (valid(gen) && recording?.id === id) { error(''); render(item); }
    } catch (e) { if (valid(gen)) { disconnect(); error(e.message + '；可重连窗口，未恢复的会话将自动回收。'); } }
    finally { polling = false; }
  }
  function watch(item) { render(item); clearInterval(timer); if (active(item)) timer = setInterval(poll, 3000); }
  window.openUiRecorder = async function (suite = null) {
    if (!hasPermission('interface:manage')) return;
    close(); project = state.projectId; suiteId = suite?.id || ''; const gen = ++generation;
    nativeSize = false; syncScale(); checkHelp(false);
    $('#uiRecorderTitle').textContent = suiteId ? '重新录制 · 保存为新版本' : '录制浏览器测试';
    $('#uiRecordingName').value = suite?.snapshot?.name || ''; $('#uiRecordingUrl').value = '';
    $('#uiRecorderForm').classList.remove('hidden'); $('#uiRecorderLive').classList.add('hidden');
    $('#uiRecorderResume').classList.add('hidden'); dialog.classList.remove('is-recording');
    $('#uiRecorderEnvironment').textContent = '正在检查录制环境…'; $('#uiRecorderStart').disabled = true; error('');
    $('#uiRecorderEnvironmentTitle').textContent = '正在检查录制服务'; $('#uiRecorderNotice').classList.remove('needs-setup');
    dialog.showModal();
    try {
      const result = await request(''); if (!valid(gen)) return;
      environmentNotice(result);
      $('#uiRecorderStart').disabled = !result.available;
      if (result.items.length) {
        $('#uiRecorderForm').classList.add('hidden');
        const resume = $('#uiRecorderResume'); resume.classList.remove('hidden');
        resume.replaceChildren(); const text = document.createElement('p'); text.textContent = '已有录制“' + result.items[0].name + '”，可以继续操作或取消后新建。';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'button primary'; button.textContent = '继续已有录制';
        button.addEventListener('click', () => { watch(result.items[0]); poll(); }); resume.append(text, button);
      }
    } catch (e) { if (valid(gen)) error(e.message); }
  };
  $('#uiRecordNew').addEventListener('click', () => window.openUiRecorder());
  $('#uiRecorderClose').addEventListener('click', () => close());
  // Closing the window allows refresh/reconnect; only explicit Cancel discards.
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  $('#uiRecorderForm').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || project !== state.projectId) return;
    const gen = generation; busy = true; $('#uiRecorderStart').disabled = true; error('');
    try {
      const item = await request('', 'POST', {name: $('#uiRecordingName').value.trim(), target_url: $('#uiRecordingUrl').value.trim(), suite_id: suiteId});
      if (valid(gen)) { $('#uiRecordingUrl').value = ''; watch(item); }
    } catch (e) { if (valid(gen)) error(e.message); }
    finally { busy = false; if (valid(gen)) $('#uiRecorderStart').disabled = false; }
  });
  $('#uiRecorderSave').addEventListener('click', async () => {
    if (busy || !recording || project !== state.projectId) return;
    const gen = generation, id = recording.id; busy = true; $('#uiRecorderSave').disabled = true; error('');
    try { const item = await request('/' + id + '/save', 'POST'); if (valid(gen) && recording?.id === id) { awaitingSave = true; render(item); } }
    catch (e) { if (valid(gen)) error(e.message); }
    finally { busy = false; if (valid(gen) && recording) render(recording); }
  });
  $('#uiRecorderReconnect').addEventListener('click', () => { if (recording && project === state.projectId) { disconnect(); render(recording); } });
  $('#uiRecorderCheckHelp').addEventListener('click', () => checkHelp($('#uiRecorderCheckGuide').hidden));
  $('#uiRecorderCheckClose').addEventListener('click', () => { checkHelp(false); $('#uiRecorderCheckHelp').focus(); });
  $('#uiRecorderScale').addEventListener('click', () => { nativeSize = !nativeSize; syncScale(); });
  $('#uiRecorderFullscreen').addEventListener('click', async () => {
    const gen = generation;
    try {
      if (ownsFullscreen && document.fullscreenElement === document.documentElement) await document.exitFullscreen();
      else {
        ownsFullscreen = true;
        await document.documentElement.requestFullscreen();
        if (!valid(gen) && document.fullscreenElement === document.documentElement) await document.exitFullscreen();
      }
    } catch (_) { ownsFullscreen = false; if (valid(gen)) error('浏览器未允许页面全屏，可按 F11 放大浏览器，或选择“原始大小”查看。'); }
  });
  document.addEventListener('fullscreenchange', () => {
    const expanded = ownsFullscreen && document.fullscreenElement === document.documentElement;
    dialog.classList.toggle('is-fullscreen', expanded);
    $('#uiRecorderFullscreen').textContent = expanded ? '退出全屏' : '全屏';
    if (!expanded) ownsFullscreen = false;
  });
  $('#uiRecorderCancel').addEventListener('click', async () => {
    if (!active(recording)) { close(); return; }
    if (busy || project !== state.projectId) return;
    const gen = generation; busy = true;
    try { const item = await request('/' + recording.id + '/cancel', 'POST'); if (valid(gen)) render(item); }
    catch (e) { if (valid(gen)) error(e.message); }
    finally { busy = false; }
  });
  // Global project/logout controls remain usable outside a modal on some hosts.
  setInterval(() => {
    if (dialog.open && (project !== state.projectId || !hasPermission('interface:manage'))) close();
    if (environmentDialog.open && environmentProject !== state.projectId) closeEnvironment();
    $('#uiRecordNew').disabled = !hasPermission('interface:manage');
    $('#uiEnvironmentOpen').disabled = !state.projectId;
  }, 1000);

  const environmentDialog = $('#uiEnvironmentDialog');
  let environmentProject = '', environmentGeneration = 0, environmentBusy = false, environmentDirty = false;
  let environmentState = null, environmentRequest = 0;
  const environmentFields = {runner_image: 'uiEnvironmentRunnerImage', recorder_image: 'uiEnvironmentRecorderImage', network: 'uiEnvironmentNetwork'};
  function environmentValid(gen) { return environmentDialog.open && gen === environmentGeneration && environmentProject === state.projectId; }
  function closeEnvironment() { environmentGeneration++; environmentDialog.close(); }
  function renderEnvironment(data, fill) {
    environmentState = data;
    const serverManaged = data.deployment_mode === 'container';
    $('#uiEnvironmentSummary').textContent = data.available ? '录制服务已就绪，可以开始录制' : data.configured ? '配置已填写，请检查组件并启动录制服务' : '按下方状态完成首次配置';
    $('#uiEnvironmentScope').textContent = data.scope;
    const labels = {ready: '已就绪', configured: '待服务验证', missing: '待完成'};
    $('#uiEnvironmentChecks').innerHTML = (data.checks || []).map(check => '<li data-status="' + esc(check.status) + '"><div><strong>' + esc(check.label) + '</strong><span>' + esc(labels[check.status] || '待检查') + '</span></div><p>' + esc(check.detail) + '</p></li>').join('');
    $('#uiEnvironmentPermission').textContent = !data.can_edit ? '当前账号可查看状态。运行环境由平台管理员统一配置，请联系平台管理员完成首次设置。' : data.worker_online ? '录制服务正在运行。修改前请先在 UI Worker 终端按 Ctrl+C 停止，保存后重新启动。' : '平台管理员可在这里保存本机配置。镜像和网络需事先准备好。';
    if (serverManaged) $('#uiEnvironmentPermission').textContent = '录制服务由服务器统一管理，你的电脑无需安装 Docker 或 Playwright。新增测试网站时，请管理员将网站及其接口地址加入服务器测试网络的允许列表。';
    $('#uiEnvironmentGuide').hidden = serverManaged;
    $('#uiEnvironmentGuideOpen').hidden = serverManaged;
    $('#uiEnvironmentForm').classList.toggle('hidden', !data.can_edit);
    if (data.can_edit) for (const [key, id] of Object.entries(environmentFields)) {
      const input = $('#' + id), managed = (data.managed_fields || []).includes(key);
      if (fill || managed) input.value = data.configuration?.[key] || '';
      input.readOnly = managed || data.worker_online; input.dataset.managed = String(managed);
      input.title = managed ? '由启动环境变量指定，请先移除对应环境变量后再在页面修改' : '';
    }
    $('#uiEnvironmentSave').disabled = !data.can_edit || data.worker_online || environmentBusy || serverManaged;
    if (data.configuration_error) $('#uiEnvironmentError').textContent = data.configuration_error;
    if (dialog.open && project === state.projectId && !active(recording)) {
      environmentNotice(data); $('#uiRecorderStart').disabled = !data.available || busy;
    }
  }
  async function refreshEnvironment(fill = false) {
    if (environmentBusy) return;
    const gen = environmentGeneration, requestId = ++environmentRequest;
    $('#uiEnvironmentRefresh').disabled = true; $('#uiEnvironmentError').textContent = '';
    try {
      const data = await api('/api/ui-recordings/environment');
      if (environmentValid(gen) && requestId === environmentRequest) renderEnvironment(data, fill || !environmentDirty);
    } catch (e) { if (environmentValid(gen) && requestId === environmentRequest) $('#uiEnvironmentError').textContent = e.message; }
    finally { if (environmentValid(gen) && requestId === environmentRequest) $('#uiEnvironmentRefresh').disabled = false; }
  }
  function openEnvironment() {
    environmentProject = state.projectId; environmentGeneration++; environmentDirty = false; environmentState = null;
    $('#uiEnvironmentForm').classList.add('hidden'); $('#uiEnvironmentChecks').replaceChildren();
    $('#uiEnvironmentSummary').textContent = '正在读取运行状态…'; $('#uiEnvironmentPermission').textContent = '';
    $('#uiEnvironmentFeedback').textContent = ''; $('#uiEnvironmentError').textContent = '';
    $('#uiEnvironmentGuide').open = false; environmentDialog.showModal(); refreshEnvironment(true);
  }
  $('#uiEnvironmentOpen').addEventListener('click', openEnvironment);
  $('#uiRecorderEnvironmentOpen').addEventListener('click', openEnvironment);
  $('#uiEnvironmentClose').addEventListener('click', closeEnvironment);
  environmentDialog.addEventListener('cancel', event => { event.preventDefault(); closeEnvironment(); });
  $('#uiEnvironmentRefresh').addEventListener('click', () => refreshEnvironment());
  $('#uiEnvironmentGuideOpen').addEventListener('click', () => {
    $('#uiEnvironmentGuide').open = true;
    $('#uiEnvironmentGuide summary').scrollIntoView({block: 'start'});
    $('#uiEnvironmentGuide summary').focus();
  });
  $('#uiEnvironmentForm').addEventListener('input', () => { environmentDirty = true; });
  $('#uiEnvironmentForm').addEventListener('submit', async event => {
    event.preventDefault(); if (environmentBusy || environmentProject !== state.projectId || !environmentState?.can_edit || environmentState.worker_online) return;
    const gen = environmentGeneration; environmentBusy = true; environmentRequest++;
    $('#uiEnvironmentSave').disabled = true; $('#uiEnvironmentRefresh').disabled = true;
    $('#uiEnvironmentError').textContent = ''; $('#uiEnvironmentFeedback').textContent = '';
    try {
      const body = Object.fromEntries(Object.entries(environmentFields).map(([key, id]) => [key, $('#' + id).value.trim()]));
      const data = await api('/api/ui-recordings/environment', {method: 'PUT', body});
      if (environmentValid(gen)) {
        environmentDirty = false; renderEnvironment(data, true);
        $('#uiEnvironmentFeedback').textContent = '配置已保存。请按准备步骤启动录制服务，再点击“刷新状态”。';
        $('#uiEnvironmentGuide').open = true;
      }
    } catch (e) { if (environmentValid(gen)) $('#uiEnvironmentError').textContent = e.message; }
    finally {
      environmentBusy = false;
      if (environmentValid(gen)) {
        $('#uiEnvironmentSave').disabled = !environmentState?.can_edit || environmentState.worker_online;
        $('#uiEnvironmentRefresh').disabled = false;
      } else if (environmentDialog.open) refreshEnvironment(true);
    }
  });
  environmentDialog.addEventListener('click', async event => {
    const button = event.target.closest('[data-ui-copy-command]'); if (!button) return;
    const node = $('#' + button.dataset.uiCopyCommand), text = node.textContent;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else {
        const area = document.createElement('textarea'); area.value = text; area.style.position = 'fixed'; area.style.opacity = '0'; environmentDialog.append(area); area.select();
        const copied = document.execCommand('copy'); area.remove(); if (!copied) throw new Error('copy unavailable');
      }
      $('#uiEnvironmentFeedback').textContent = '命令已复制，请在本机项目目录的终端执行。';
    } catch (_) {
      const range = document.createRange(); range.selectNodeContents(node); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      $('#uiEnvironmentFeedback').textContent = '浏览器未允许自动复制，已选中命令，请按 Ctrl+C 复制。';
    }
  });
});
