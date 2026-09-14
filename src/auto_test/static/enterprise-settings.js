"use strict";

document.addEventListener('DOMContentLoaded', function () {
  const dialog = document.querySelector('#enterpriseSettingsDialog');
  const body = dialog.querySelector('[data-enterprise-body]');
  let mode = '', profileId = '', generation = 0, busy = false;
  async function showBindings() {
    const gen = ++generation;
    const [data, users] = await Promise.all([api('/api/identity/oidc-bindings'), api('/api/identity/users')]);
    if (gen !== generation || !dialog.open) return;
    body.innerHTML = '<p class="muted">将企业身份的稳定主体标识绑定到已有平台账号。项目角色仍由平台管理；删除绑定会撤销该账号的现有登录。</p><form data-bind-identity><label class="field"><span>平台账号</span><select name="bindingUser">' + (users.users || []).map(user => '<option value="' + esc(user.id) + '">' + esc(user.display_name + ' · ' + user.username) + '</option>').join('') + '</select></label><label class="field"><span>身份签发者（Issuer）</span><input type="url" name="bindingIssuer" required maxlength="500" value="' + esc(data.issuer || '') + '" placeholder="https://login.example.com"></label><label class="field"><span>企业身份主体（Subject）</span><input name="bindingSubject" required maxlength="255" placeholder="由企业身份服务提供的稳定标识"></label><button class="button primary" type="submit">绑定企业账号</button></form><div class="table-wrap paged-scroll" tabindex="0"><table><thead><tr><th>平台账号</th><th>签发者 / 主体</th><th>操作</th></tr></thead><tbody>' + ((data.bindings || []).map(item => '<tr><td>' + esc(item.username) + '</td><td>' + esc(item.issuer) + '<small class="table-subline">' + esc(item.subject) + '</small></td><td><button class="text-button danger-text" type="button" data-unbind-identity="' + esc(item.id) + '">解除绑定</button></td></tr>').join('') || '<tr><td colspan="3">暂无企业身份绑定</td></tr>') + '</tbody></table></div><p class="form-error" role="alert" data-enterprise-error></p>';
  }
  async function showAccess() {
    const gen = ++generation;
    const [access, projects] = await Promise.all([api('/api/model-profiles/' + encodeURIComponent(profileId) + '/access'), api('/api/identity/projects')]);
    if (gen !== generation || !dialog.open) return;
    body.innerHTML = '<form data-save-model-access><label class="switch-row"><input name="modelRestricted" type="checkbox"' + (access.restricted ? ' checked' : '') + '><span><b>仅允许指定项目使用</b><small>关闭时所有项目可用；开启后未勾选任何项目则禁止业务调用。</small></span></label><div class="enterprise-project-options">' + (projects.projects || []).map(project => '<label class="switch-row"><input type="checkbox" name="modelAllowedProject" value="' + esc(project.id) + '"' + ((access.project_ids || []).includes(project.id) ? ' checked' : '') + '><span>' + esc(project.name) + '</span></label>').join('') + '</div><p class="muted">限制适用于被测模型、裁判模型和报告分析；排队任务领取时会再次检查授权。</p><button class="button primary" type="submit">保存项目授权</button><p class="form-error" role="alert" data-enterprise-error></p></form>';
  }
  const fail = e => { const error = body.querySelector('[data-enterprise-error]'); if (error) error.textContent = e.message; else { body.textContent = '读取失败：' + e.message; } };
  async function mutate(action) {
    if (busy) return;
    busy = true; body.querySelectorAll('button').forEach(button => button.disabled = true);
    try { await action(); if (mode === 'bindings') await showBindings(); else { dialog.close(); toast('模型项目授权已保存'); } }
    catch (e) { fail(e); }
    finally { busy = false; body.querySelectorAll('button').forEach(button => button.disabled = false); }
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-enterprise-bindings],[data-model-access]');
    if (!button || !hasPermission('platform:manage')) return;
    mode = button.matches('[data-enterprise-bindings]') ? 'bindings' : 'access';
    profileId = button.dataset.modelAccess || '';
    dialog.querySelector('h3').textContent = mode === 'bindings' ? '企业账号绑定' : '模型项目授权';
    body.innerHTML = '<p>正在读取配置…</p>'; dialog.showModal();
    try { if (mode === 'bindings') await showBindings(); else await showAccess(); } catch (e) { fail(e); }
  });
  dialog.querySelector('[data-enterprise-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => generation++);
  body.addEventListener('submit', event => {
    event.preventDefault();
    const form = event.target;
    if (form.matches('[data-bind-identity]')) {
      const payload = { user_id: form.elements.bindingUser.value, issuer: form.elements.bindingIssuer.value.trim(), subject: form.elements.bindingSubject.value.trim() };
      mutate(() => api('/api/identity/oidc-bindings', { method: 'POST', body: payload }));
    } else if (form.matches('[data-save-model-access]')) {
      const payload = { restricted: form.elements.modelRestricted.checked, project_ids: Array.from(form.querySelectorAll('[name="modelAllowedProject"]:checked')).map(item => item.value) };
      mutate(() => api('/api/model-profiles/' + encodeURIComponent(profileId) + '/access', { method: 'PUT', body: payload }));
    }
  });
  body.addEventListener('click', event => {
    const button = event.target.closest('[data-unbind-identity]');
    if (button && confirm('解除此企业身份绑定并撤销该账号现有登录？')) mutate(() => api('/api/identity/oidc-bindings/' + encodeURIComponent(button.dataset.unbindIdentity), { method: 'DELETE' }));
  });
});
