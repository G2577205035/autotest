"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = {
  bootstrap: null, runs: [], reports: [], models: [], interfaces: [], template: null, stressJobs: [],
  interfaceModules: [], interfaceEnvironments: [], interfaceVariables: [], interfaceScenarios: [], interfaceScenarioRuns: [], selectedInterfaceScenarioIds: [], selectedInterfaceModuleId: "",
  identity: null, csrfToken: "", projectId: "", identityUsers: [], identityProjects: [], identityRoles: [], identityMembers: [], identityAudit: [], identityWorkspaceTab: "users", identityUsersPage: 1, identityProjectsPage: 1, identityRolesPage: 1, identityPageSize: 10, identityAuditPage: 1, identityAuditPageSize: 20, identityAuditTotal: 0, identityAuditTotalPages: 1,
  selectedRunId: "", monitorRunId: "", stressJobId: "", logCursor: {}, metricCursor: {}, stressMetricCursor: {}, logs: {}, metrics: {}, stressMetrics: {}, logFollow: {}, logScrollTop: {}, monitorRefreshInFlight: {},
  stressCapability: null, selectedGpuDevices: [], stressPreset: "quick", stressResourcesAutoOpened: false, expandedGpuIndex: null, gpuRenderContext: null,
  serverProfiles: [], serverSessions: [], serverSessionId: "", closingServerSessionId: "", serverSessionMetricCursor: {}, serverSessionMetrics: {},
  stressReportJobId: "", dashboardRunsSignature: "",
  evaluationBootstrap: null, evaluationRuns: [], evaluationRunsSignature: "", evaluationRunsPage: 1, evaluationRunsPageSize: 7, selectedEvaluationRunId: "", evaluationEvents: [], evaluationResults: [], evaluationReport: null, evaluationReportRunId: "", evaluationReportCasesPage: 1, evaluationReportCasesPageSize: 8, evaluationSuites: [], evaluationComparisons: [], selectedEvaluationComparisonRunIds: [], evaluationComparisonResult: null,
  pollTimer: null
};
const titles = {dashboard:"运行总览","new-run":"发起测试",monitor:"实时监控",stress:"服务器性能",reports:"报告中心",evaluation:"模型评测",models:"模型配置",interfaces:"接口中心",identity:"用户、项目与权限"};
const subtitles = {
  dashboard:"任务状态、质量指标与最近运行",
  "new-run":"创建业务自动化测试任务",
  monitor:"查看阶段进度、资源曲线与实时日志",
  stress:"服务器会话、实时状态、压测与性能报告",
  reports:"生成、管理与下载企业测试报告",
  evaluation:"翻译、报告、情报、Token、裁判复核、容量与模型对比",
  models:"安全维护报告生成模型",
  interfaces:"按项目管理接口、模块、环境、变量与不可变版本",
  identity:"管理业务项目、平台用户、项目成员角色与安全审计"
};
const stages = ["queued","preparing","stress","authentication","upload","parsing","translation","verification","ai_checks","export","reporting","completed"];
const stageNames = {queued:"排队",preparing:"准备",stress:"压力测试",authentication:"登录",upload:"上传",parsing:"解析",translation:"翻译",verification:"校验",ai_checks:"AI检查",export:"导出",reporting:"报告",completed:"完成",failed:"失败",interrupted:"中断"};
const statusNames = {authorized:"已授权待执行",queued:"排队中",preparing:"准备中",running:"运行中",scoring:"评分中",reporting:"报告中",completed:"已完成",stopped:"已停止",passed:"通过",error:"错误",succeeded:"已成功",failed:"失败",interrupted:"已中断",retrying:"重试中",draft:"草稿",published:"已发布",archived:"已归档"};
const stressPresetNames = {health:"在线体检",quick:"快速验证",standard:"标准测试",stability:"稳定性测试",custom:"自定义"};
const stressModeNames = {monitor:"监控",cpu:"CPU",gpu:"GPU"};

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, function(ch) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
  });
}
function shortId(value) { return value ? String(value).slice(0, 10) : "—"; }
function runLabel(run) { return run && (run.display_name || shortId(run.id)); }
function isLogNearBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight <= 36;
}
function updateLogFollowUi(runId) {
  const following = state.logFollow[runId] !== false;
  const status = $("#logLiveStatus");
  const button = $("#logFollowButton");
  status.textContent = following ? "LIVE" : "已暂停跟随";
  status.classList.toggle("paused", !following);
  button.classList.toggle("hidden", following);
}
function recordLogScroll(node) {
  const runId = state.monitorRunId;
  if (!runId) return;
  const previousScrollTop = state.logScrollTop[runId];
  if (previousScrollTop != null && node.scrollTop < previousScrollTop - 1) {
    state.logFollow[runId] = false;
  } else if (isLogNearBottom(node)) {
    state.logFollow[runId] = true;
  }
  state.logScrollTop[runId] = node.scrollTop;
  updateLogFollowUi(runId);
}
function followLogToBottom() {
  const runId = state.monitorRunId;
  if (!runId) return;
  state.logFollow[runId] = true;
  const consoleNode = $("#logConsole");
  consoleNode.scrollTop = consoleNode.scrollHeight;
  state.logScrollTop[runId] = consoleNode.scrollTop;
  updateLogFollowUi(runId);
}
function fmtTime(value) {
  if (!value) return "—";
  return new Date(Number(value) * 1000).toLocaleString("zh-CN", {hour12:false});
}
function runOptions(run) {
  const metadata = run && run.metadata;
  const options = metadata && typeof metadata === "object" ? metadata.options : null;
  return options && typeof options === "object" ? options : {};
}
function runCaseName(run) {
  const options = runOptions(run);
  return String(options.case_name || "未命名案件").trim();
}
function runScope(run) {
  const options = runOptions(run);
  const labels = {summary:"摘要生成",attachtranslate:"附件预翻译"};
  const raw = Array.isArray(options.analysis) ? options.analysis : String(options.analysis || "").split(",");
  const selected = raw.map(function(item){ return String(item).trim(); }).filter(Boolean);
  return selected.length ? selected.map(function(item){ return labels[item] || item; }).join(" · ") : "基础自动化";
}
function fmtDuration(run) {
  const start = Number(run && (run.started_at || run.created_at));
  const storedFinish = Number(run && run.finished_at);
  const finish = Number.isFinite(storedFinish) && storedFinish > 0
    ? storedFinish
    : (run && run.status === "running" ? Date.now() / 1000 : NaN);
  if (!Number.isFinite(start) || !Number.isFinite(finish) || finish < start) return "—";
  const total = Math.max(0, Math.round(finish - start));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return hours + " 小时 " + minutes + " 分";
  if (minutes) return minutes + " 分 " + seconds + " 秒";
  return seconds + " 秒";
}
function fmtSize(bytes) {
  if (!bytes) return "0 B";
  const units = ["B","KB","MB","GB"]; let i = 0; let n = Number(bytes);
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return n.toFixed(i ? 1 : 0) + " " + units[i];
}
function toast(message, error) {
  const node = document.createElement("div");
  node.className = "toast" + (error ? " error" : "");
  node.textContent = message;
  $("#toastArea").appendChild(node);
  setTimeout(function(){ node.remove(); }, 4200);
}
function apiErrorMessage(detail, status) {
  const fieldLabels = {
    server_name:"服务器名称", host:"服务器 IP / 主机名", port:"SSH 端口",
    user:"SSH 用户", password:"SSH 密码", modes:"测试内容", duration:"持续时间",
    workers:"CPU Workers", cpu_load:"CPU 负载", gpu_devices:"GPU"
  };
  function validationMessage(item) {
    if (!item || typeof item !== "object") return String(item || "");
    const location = Array.isArray(item.loc) ? item.loc.filter(function(part){return part !== "body";}) : [];
    const field = location.length ? location[location.length - 1] : "";
    const label = fieldLabels[field] || field || "请求参数";
    const type = String(item.type || "");
    let message = item.msg || item.message || "格式不正确";
    if (type === "missing" || type.includes("too_short") || /field required|at least 1 character/i.test(message)) message = "不能为空";
    return label + "：" + message;
  }
  if (Array.isArray(detail)) {
    const messages = detail.map(validationMessage).filter(Boolean);
    return messages.join("；") || "请求参数不正确";
  }
  if (detail && typeof detail === "object") {
    if (detail.message) return String(detail.message);
    try { return JSON.stringify(detail); } catch (_) { return "请求失败"; }
  }
  if (typeof detail === "string" && detail.trim()) return detail;
  return status === 422 ? "请求参数不正确" : "请求失败";
}
async function api(path, options) {
  const opts = Object.assign({}, options || {});
  opts.headers = Object.assign({}, opts.headers || {});
  if (state.projectId) opts.headers["X-Project-ID"] = state.projectId;
  if (state.csrfToken && !["GET","HEAD","OPTIONS"].includes(String(opts.method || "GET").toUpperCase())) {
    opts.headers["X-CSRF-Token"] = state.csrfToken;
  }
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  const type = response.headers.get("content-type") || "";
  const data = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = data && data.detail ? data.detail : (typeof data === "string" ? data : "请求失败");
    const error = new Error(apiErrorMessage(detail, response.status));
    error.status = response.status;
    if (response.status === 401 && !path.startsWith("/api/auth/")) showAuth(false, "登录已失效，请重新登录");
    throw error;
  }
  return data;
}
function statusPill(status) {
  return '<span class="status ' + esc(status) + '">' + esc(statusNames[status] || status) + "</span>";
}
function evaluationCaseStatusPill(status) {
  const labels={passed:"达标",failed:"未达标",error:"执行异常",completed:"已完成"};
  const classes={passed:"passed",failed:"evaluation-not-met",error:"error",completed:"completed"};
  return '<span class="status '+esc(classes[status]||"idle")+'">'+esc(labels[status]||status||"未知")+'</span>';
}
function openStressResourcesOnce() {
  if(state.stressResourcesAutoOpened) return;
  state.stressResourcesAutoOpened=true;
  $("#stressStepResources").open=true;
}
function activateWorkspaceTab(group, name) {
  if(group === "stress" && name !== "live") closeGpuDetailDrawer();
  $$('[data-workspace-tab="' + group + '"]').forEach(function(button){
    button.classList.toggle("active", button.dataset.workspaceTarget === name);
    button.setAttribute("aria-selected", button.dataset.workspaceTarget === name ? "true" : "false");
  });
  $$('[data-workspace-panel="' + group + '"]').forEach(function(panel){
    panel.classList.toggle("hidden", panel.dataset.workspaceName !== name);
  });
  const activePanel=$('[data-workspace-panel="' + group + '"][data-workspace-name="' + name + '"]');
  const view=activePanel && activePanel.closest(".view");
  if(view) view.scrollTo({top:0,behavior:"smooth"});
  if(group === "stress" && name === "live") refreshStressLiveWorkspace(true);
  if(group === "stress" && name === "sessions") loadServerWorkspace();
  if(group === "stress" && name === "setup") openStressResourcesOnce();
}
function gotoView(name) {
  if(name !== "stress") closeGpuDetailDrawer();
  $$(".nav-item").forEach(function(button){ button.classList.toggle("active", button.dataset.view === name); });
  $$(".view").forEach(function(view){ view.classList.toggle("active", view.id === "view-" + name); });
  $("#pageTitle").textContent = titles[name] || name;
  $("#pageSubtitle").textContent = subtitles[name] || "";
  if (name === "monitor") refreshMonitor(true);
  if (name === "new-run") loadServerWorkspace(false);
  if (name === "stress") loadServerWorkspace();
  if (name === "reports") Promise.allSettled([loadReports(),loadTemplate()]);
  if (name === "evaluation") loadEvaluationWorkspace(true);
  if (name === "models") loadModels();
  if (name === "interfaces") loadInterfaces();
  if (name === "identity") loadIdentityData();
  const activeView=$("#view-" + name);
  if(activeView) activeView.scrollTo({top:0, behavior:"smooth"});
  window.scrollTo({top:0, behavior:"smooth"});
}
async function initializeApp() {
  if (!state.identity || !state.identity.authenticated) return;
  try {
    const bootstrap = await api("/api/bootstrap");
    state.bootstrap = bootstrap;
    $(".side-status").classList.add("connected");
    $("#sideHealth").textContent = "连接正常";
    prepareRunForm();
    fillStressModuleDefaults();
    const idleSeconds=Number((bootstrap.server_sessions || {}).idle_timeout_seconds || 300);
    $("#serverSessionPolicy").textContent="空闲 " + Math.max(1,Math.round(idleSeconds/60)) + " 分钟自动断开";
    await refreshAll();
    startPolling();
  } catch (error) {
    $(".side-status").classList.remove("connected");
    $("#sideHealth").textContent = "连接失败";
    if (error.status === 401) showAuth(false, "登录已失效，请重新登录");
    else toast("本地服务加载失败：" + error.message, true);
  }
}

function hasPermission(permission) {
  const identity=state.identity || {};
  return Boolean(identity.user && identity.user.is_superuser) || (identity.permissions || []).includes(permission);
}
function roleLabel(role) {
  const labels={project_admin:"项目管理员",tester:"测试执行者",viewer:"只读成员"};
  return labels[role] || (state.identity && state.identity.user && state.identity.user.is_superuser ? "平台管理员" : "未分配角色");
}
function businessProjectLabel(project) {
  return String((project && project.name) || (project && project.project_key) || "未命名业务项目");
}
function showAuth(setupRequired,message) {
  clearInterval(state.pollTimer);
  state.pollTimer=null;
  state.identity=null;state.csrfToken="";state.projectId="";
  $("#appShell").classList.add("app-locked");
  $("#authOverlay").classList.remove("hidden");
  $("#authOverlay").dataset.mode=setupRequired?"setup":"login";
  $("#setupForm").classList.toggle("hidden",!setupRequired);
  $("#loginForm").classList.toggle("hidden",Boolean(setupRequired));
  $("#authTitle").textContent=setupRequired?"初始化烈马测试平台":"登录";
  $("#authIntro").textContent=message || "";
  $("#authIntro").classList.toggle("is-notice",Boolean(message));
  setTimeout(function(){const target=setupRequired?$("#setupUsername"):$("#loginUsername");if(target)target.focus();},50);
}
function syncIdentityChrome() {
  const identity=state.identity || {};
  const user=identity.user || {};
  const projects=identity.projects || [];
  $("#currentUserName").textContent=user.display_name || user.username || "—";
  $("#currentUserRole").textContent=user.is_superuser?"平台管理员":roleLabel(identity.project_role);
  $("#userAvatar").textContent=String(user.display_name || user.username || "—").slice(0,1).toUpperCase();
  const projectOptions=projects.map(function(project){return '<option value="'+esc(project.id)+'">'+esc(businessProjectLabel(project))+"</option>";}).join("");
  ["projectSwitcher","interfaceProjectSwitcher"].forEach(function(id){const node=$("#"+id);if(!node)return;node.innerHTML=projectOptions;node.value=state.projectId;node.disabled=projects.length<2;});
  $("#identityNavItem").classList.toggle("hidden",!user.is_superuser);
  $$('[data-view="models"]').forEach(function(node){node.classList.toggle("hidden",!hasPermission("platform:manage"));});
  $$('[data-view="evaluation"]').forEach(function(node){node.classList.toggle("hidden",!hasPermission("evaluation:view"));});
  $$('[data-view="new-run"],[data-goto="new-run"]').forEach(function(node){node.classList.toggle("permission-hidden",!hasPermission("test:execute"));});
  syncInterfacePermissions();
}
async function completeAuthentication(identity) {
  state.identity=Object.assign({authenticated:true},identity || {});
  state.csrfToken=String(state.identity.csrf_token || "");
  state.projectId=String((state.identity.current_project || {}).id || "");
  if(!state.projectId) {showAuth(false,"当前账号尚未加入可用项目，请联系平台管理员");return;}
  $("#authOverlay").classList.add("hidden");
  $("#appShell").classList.remove("app-locked");
  syncIdentityChrome();
  await initializeApp();
}
async function initializeAuth() {
  try {
    const status=await api("/api/auth/status");
    if(status.authenticated) await completeAuthentication(status);
    else showAuth(Boolean(status.setup_required));
  } catch(error) {
    showAuth(false,"无法连接平台服务："+error.message);
  }
}
async function submitLogin(event) {
  event.preventDefault();
  const errorNode=$("#loginError");errorNode.textContent="";
  try {
    const identity=await api("/api/auth/login",{method:"POST",body:{username:$("#loginUsername").value.trim(),password:$("#loginPassword").value}});
    $("#loginPassword").value="";
    await completeAuthentication(identity);
  } catch(error) {errorNode.textContent=error.message;}
}
async function submitSetup(event) {
  event.preventDefault();
  const errorNode=$("#setupError");errorNode.textContent="";
  if($("#setupPassword").value!==$("#setupPasswordConfirm").value){errorNode.textContent="两次输入的密码不一致";return;}
  try {
    const identity=await api("/api/auth/setup",{method:"POST",body:{username:$("#setupUsername").value.trim(),display_name:$("#setupDisplayName").value.trim(),password:$("#setupPassword").value,project_key:$("#setupProjectKey").value.trim(),project_name:$("#setupProjectName").value.trim()}});
    $("#setupPassword").value="";$("#setupPasswordConfirm").value="";
    await completeAuthentication(identity);
  } catch(error) {errorNode.textContent=error.message;}
}
async function logout() {
  try {await api("/api/auth/logout",{method:"POST"});} catch(_) {}
  showAuth(false,"已安全退出平台");
}
async function switchProject(projectId,returnView) {
  if(!projectId || projectId===state.projectId)return;
  state.projectId=projectId;
  try {
    const identity=await api("/api/auth/status");
    state.bootstrap=null;state.runs=[];state.reports=[];state.stressJobs=[];state.serverProfiles=[];state.serverSessions=[];state.serverSessionId="";state.selectedRunId="";state.monitorRunId="";state.stressJobId="";state.stressReportJobId="";state.dashboardRunsSignature="";state.logCursor={};state.metricCursor={};state.stressMetricCursor={};state.logs={};state.metrics={};state.stressMetrics={};state.interfaces=[];state.interfaceModules=[];state.interfaceEnvironments=[];state.interfaceVariables=[];state.interfaceScenarios=[];state.interfaceScenarioRuns=[];state.selectedInterfaceScenarioIds=[];state.selectedInterfaceModuleId="";state.evaluationBootstrap=null;state.evaluationRuns=[];state.evaluationRunsSignature="";state.evaluationRunsPage=1;state.selectedEvaluationRunId="";state.evaluationEvents=[];state.evaluationResults=[];state.evaluationReport=null;state.evaluationReportRunId="";state.evaluationReportCasesPage=1;
    await completeAuthentication(identity);
    gotoView(returnView || "dashboard");
    toast("已切换到项目："+(identity.current_project || {}).name);
  } catch(error) {toast("项目切换失败："+error.message,true);}
}
function prepareRunForm() {
  ["runUsername","runPassword","runAdministratorUsername","runAdministratorPassword","runHost","runSshUser","runSshPassword","runGpuHost","runGpuSshUser","runGpuSshPassword"].forEach(function(id){$("#"+id).value="";});
  $("#runFiles").value="";
  $("#runServerProfile").value="manual";
  $("#runGpuServerProfile").value="";
  $("#runCaseName").value=defaultRunCaseName();
  $("#runTranslate").value=defaultTranslateName();
  fillRunAdvancedDefaults();
  syncRunServerProfileFields(false);
  syncUploadModeUi();
}
function defaultRunCaseName(date) {
  const current=date || new Date();
  const pad=function(value){return String(value).padStart(2,"0");};
  return "测试"+current.getFullYear()+pad(current.getMonth()+1)+pad(current.getDate());
}
function defaultTranslateName() {
  return String((state.bootstrap && state.bootstrap.defaults && state.bootstrap.defaults.translate_name) || "").trim();
}
function setChecked(id, value) {
  const node = $("#" + id);
  if (node) node.checked = Boolean(value);
}
function fillRunAdvancedDefaults() {
  if (!state.bootstrap) return;
  const monitor = state.bootstrap.monitor || {};
  const upload = state.bootstrap.upload || {};
  const exp = state.bootstrap.export || {};
  const ai = state.bootstrap.ai_checks || {};
  const stress = state.bootstrap.stress || {};
  $("#runUploadMode").value = upload.mode || "auto";
  $("#runUploadSizeLimit").value = upload.size_limit_mb || 1024;
  $("#runMonitorModules").value = (monitor.containers || state.bootstrap.monitor_containers || []).join(",");
  setChecked("runMonitorPerf", monitor.enable_perf !== false);
  setChecked("runMonitorLogs", monitor.enable_log_monitor !== false);
  const exportTypes = String(exp.types || "tran,original").split(",").map(function(item){ return item.trim(); });
  $$('input[name="runExportType"]').forEach(function(input){ input.checked = exportTypes.includes(input.value); });
  setChecked("runAiChecks", ai.enable !== false);
  setChecked("runAiSimpleTranslation", ai.simple_translation_enable !== false);
  setChecked("runAiXiaoyiTranslation", ai.xiaoyi_translation_enable !== false);
  setChecked("runAiXiaoyiSummary", ai.xiaoyi_summary_enable !== false);
  $("#runAiSamplePath").value = ai.sample_path || "";
  $("#runAiMaxFileIds").value = ai.max_file_ids || 1;
  setChecked("runStressEnable", Boolean(stress.enable));
  $("#runStressWorkers").value = stress.workers || 0;
  $("#runStressCpuLoad").value = stress.cpu_load || 80;
  $("#runStressDuration").value = stress.duration || 30;
}
function populateRunServerProfiles() {
  const currentApp=$("#runServerProfile").value || "manual";
  const currentGpu=$("#runGpuServerProfile").value || "";
  const options=state.serverProfiles.map(function(profile){
    const credential=profile.credential_saved ? "已保存凭据" : "每次授权";
    return '<option value="'+esc(profile.id)+'">'+esc(profile.name)+' · '+esc(profile.host)+' · '+credential+'</option>';
  }).join("");
  $("#runServerProfile").innerHTML='<option value="manual">手动填写服务器</option>'+options;
  $("#runGpuServerProfile").innerHTML='<option value="">与业务服务器同机</option><option value="manual">手动填写独立 GPU 服务器</option>'+options;
  $("#runServerProfile").value=state.serverProfiles.some(function(item){return item.id===currentApp;}) ? currentApp : "manual";
  $("#runGpuServerProfile").value=state.serverProfiles.some(function(item){return item.id===currentGpu;}) ? currentGpu : (currentGpu==="manual" ? "manual" : "");
  syncRunServerProfileFields(false);
}
function syncRunServerProfileFields(clearSecrets) {
  const appValue=$("#runServerProfile").value;
  const appProfile=state.serverProfiles.find(function(item){return item.id===appValue;});
  if(appProfile) {
    $("#runHost").value=appProfile.host || "";
    $("#runSshUser").value=appProfile.user || "";
  } else if(clearSecrets) {
    $("#runHost").value="";
    $("#runSshUser").value="";
  }
  $("#runHost").readOnly=Boolean(appProfile);
  $("#runSshUser").readOnly=Boolean(appProfile);
  $("#runSshPassword").placeholder=appProfile && appProfile.credential_saved ? "已加密保存，无需填写" : "请输入一次性 SSH 密码";

  const gpuValue=$("#runGpuServerProfile").value;
  const gpuProfile=state.serverProfiles.find(function(item){return item.id===gpuValue;});
  const separate=Boolean(gpuValue);
  ["runGpuHostField","runGpuSshUserField","runGpuSshPasswordField"].forEach(function(id){$("#"+id).classList.toggle("hidden",!separate);});
  $("#runGpuHost").required=separate;
  if(gpuProfile) {
    $("#runGpuHost").value=gpuProfile.host || "";
    $("#runGpuSshUser").value=gpuProfile.user || "";
  } else if(clearSecrets || !separate) {
    $("#runGpuHost").value="";
    $("#runGpuSshUser").value="";
  }
  $("#runGpuHost").readOnly=Boolean(gpuProfile);
  $("#runGpuSshUser").readOnly=Boolean(gpuProfile);
  $("#runGpuSshPassword").placeholder=gpuProfile && gpuProfile.credential_saved ? "已加密保存，无需填写" : "留空则复用业务服务器 SSH 密码";
  if(clearSecrets) {
    $("#runSshPassword").value="";
    $("#runGpuSshPassword").value="";
  }
  syncRunSummary();
}
function syncRunSummary() {
  const account = $("#runUsername").value.trim() || "未填写";
  const host = $("#runHost").value.trim() || "未填写";
  const gpuHost = $("#runGpuHost").value.trim();
  const serverMode = $("#runUploadMode").value === "server";
  const files = Array.from($("#runFiles").files || []);
  const relativePath = files.length ? String(files[0].webkitRelativePath || "") : "";
  const folderName = relativePath ? relativePath.split("/")[0] : "";
  const fileSummary = files.length
    ? (folderName ? folderName+" · " : "")+files.length+" 个文件"
    : "尚未选择客户本地测试文件夹";
  const path = serverMode && files.length ? "服务器直传 · "+fileSummary : fileSummary;
  const analysis = $$('input[name="analysis"]:checked').map(function(input){ return input.nextElementSibling.textContent; }).join("、") || "未选择";
  $("#runSummaryAccount").textContent = account;
  $("#runSummaryHost").textContent = gpuHost ? host + " / GPU " + gpuHost : host;
  $("#runSummaryPath").textContent = path;
  $("#runSummaryAnalysis").textContent = analysis;
}
function syncUploadModeUi() {
  const serverMode = $("#runUploadMode").value === "server";
  const files = Array.from($("#runFiles").files || []);
  const bytes = files.reduce(function(sum,file){return sum+file.size;},0);
  const selectedText = files.length ? files.length+" 个文件 · "+fmtSize(bytes) : "尚未选择客户本地测试文件夹";
  if (serverMode) {
    $("#uploadZoneTitle").innerHTML = '选择客户本地测试文件夹 <b class="required-mark" title="必填">*</b>';
    $("#uploadZoneDescription").textContent = "从当前客户电脑选择目录；平台暂存后由 Worker 通过 SSH/SFTP 直传到业务服务器。";
    $("#fileSelectionText").textContent = selectedText;
    $("#runSubmitHint").textContent = "客户本地目录不会作为服务器路径保存；只保留文件夹结构和任务输入快照。";
  } else {
    $("#uploadZoneTitle").innerHTML = '选择客户本地测试文件夹 <b class="required-mark" title="必填">*</b>';
    $("#uploadZoneDescription").textContent = "从当前客户电脑选择目录并保留文件夹结构；任务开始前形成安全输入快照。";
    $("#fileSelectionText").textContent = selectedText;
    $("#runSubmitHint").textContent = "提交后进入全局串行队列，可安全关闭页面。";
  }
  syncRunSummary();
}
async function refreshAll() {
  const jobs=[loadRuns(),loadReports()];
  if($("#view-new-run").classList.contains("active"))jobs.push(loadServerWorkspace(false));
  if($("#view-models").classList.contains("active"))jobs.push(loadModels());
  if($("#view-evaluation").classList.contains("active"))jobs.push(loadEvaluationWorkspace(false));
  if($("#view-interfaces").classList.contains("active"))jobs.push(loadInterfaces());
  if($("#view-reports").classList.contains("active"))jobs.push(loadTemplate());
  if($("#view-stress").classList.contains("active")){jobs.push(loadStressJobs());jobs.push(loadServerWorkspace());}
  if($("#view-identity").classList.contains("active"))jobs.push(loadIdentityData());
  const results = await Promise.allSettled(jobs);
  const rejected = results.find(function(result){ return result.status === "rejected"; });
  if (rejected && rejected.reason && rejected.reason.status === 401) showAuth(false,"登录已失效，请重新登录");
}
async function loadRuns() {
  const data = await api("/api/runs?limit=100");
  state.runs = data.runs || [];
  state.selectedRunId = data.active_run_id || state.selectedRunId || (state.runs[0] && state.runs[0].id) || "";
  if (!state.monitorRunId) state.monitorRunId = state.selectedRunId;
  renderDashboard(data.active_run_id);
  populateRunSelects();
}
function renderDashboard(activeId) {
  const runs = state.runs;
  const terminal = runs.filter(function(run){ return ["succeeded","failed","interrupted"].includes(run.status); });
  const success = terminal.filter(function(run){ return run.status === "succeeded"; }).length;
  $("#statRuns").textContent = String(runs.length);
  $("#statSuccess").textContent = terminal.length ? Math.round(success * 100 / terminal.length) + "%" : "—";
  $("#statActive").textContent = activeId ? "1" : "0";
  $("#statReports").textContent = String(state.reports.filter(function(job){ return job.status === "succeeded"; }).length);
  const active = runs.find(function(run){ return run.id === activeId; }) || runs.find(function(run){ return run.status === "queued"; });
  $(".dashboard-current-panel").classList.toggle("is-empty", !active);
  $("#activeRunEmpty").classList.toggle("hidden", Boolean(active));
  $("#activeRunDetails").classList.toggle("hidden", !active);
  const pill = $("#activeStatus");
  if (active) {
    pill.className = "status " + active.status;
    pill.textContent = statusNames[active.status] || active.status;
    $("#activeRunName").textContent = runLabel(active) + " · " + (stageNames[active.stage] || active.stage);
    $("#activeRunMessage").textContent = active.message || "";
    $("#activeProgressBar").style.width = active.progress + "%";
    $("#activeProgressText").textContent = active.progress + "%";
    const current = Math.max(0, stages.indexOf(active.stage));
    $("#stageTrack").innerHTML = stages.slice(0, -1).map(function(stage, index){
      const cls = index < current ? "done" : (index === current ? "current" : "");
      return '<span class="' + cls + '">' + esc(stageNames[stage]) + "</span>";
    }).join("");
  } else {
    pill.className = "status idle"; pill.textContent = "暂无任务";
  }
  const recentRuns = runs.slice(0, 10);
  const recentSignature = JSON.stringify(recentRuns.map(function(run){
    const options = runOptions(run);
    return [run.id,run.status,run.stage,run.progress,run.message,run.started_at,run.finished_at,options.case_name,options.host,options.gpu_host,options.username,options.analysis,options.translate_name];
  }));
  if (state.dashboardRunsSignature !== recentSignature) {
    state.dashboardRunsSignature = recentSignature;
    $("#recentRunsBody").innerHTML = recentRuns.map(function(run){
      const options = runOptions(run);
      const host = String(options.host || "未记录目标服务器").trim();
      const account = String(options.username || "未记录测试账号").trim();
      const gpuHost = String(options.gpu_host || "").trim();
      const targetNote = "测试账号 " + account + (gpuHost && gpuHost !== host ? " · GPU " + gpuHost : "");
      const language = String(options.translate_name || "").trim();
      const resultDetail = run.message || ((stageNames[run.stage] || run.stage || "等待执行") + (run.status === "running" || run.status === "queued" ? " · " + run.progress + "%" : ""));
      return '<tr><td><strong class="recent-run-primary">' + esc(runCaseName(run)) + '</strong><small class="recent-run-secondary mono">任务 ' + esc(shortId(run.id)) + '</small></td>'
        + '<td><strong class="recent-run-primary mono">' + esc(host) + '</strong><small class="recent-run-secondary">' + esc(targetNote) + '</small></td>'
        + '<td><strong class="recent-run-primary">' + esc(runScope(run)) + '</strong><small class="recent-run-secondary">' + esc(language ? "翻译语种 " + language : "未记录翻译语种") + '</small></td>'
        + '<td><time>' + fmtTime(run.started_at || run.created_at) + '</time></td><td class="mono">' + esc(fmtDuration(run)) + '</td>'
        + '<td><div class="recent-run-result">' + statusPill(run.status) + '<span>' + esc(resultDetail) + '</span></div></td>'
        + '<td class="run-actions">' + runActionButtons(run) + "</td></tr>";
    }).join("") || '<tr><td colspan="7" class="recent-runs-empty">暂无运行记录</td></tr>';
  }
}
function populateRunSelects() {
  const options = state.runs.map(function(run){
    return '<option value="' + esc(run.id) + '">' + esc(runLabel(run)) + " · " + esc(statusNames[run.status] || run.status) + "</option>";
  }).join("");
  $("#monitorRunSelect").innerHTML = options || '<option value="">暂无任务</option>';
  $("#reportRunSelect").innerHTML = state.runs.filter(function(run){ return Boolean(run.has_run_dir || run.run_dir); }).map(function(run){
    return '<option value="' + esc(run.id) + '">' + esc(runLabel(run)) + "</option>";
  }).join("") || '<option value="">暂无可生成报告的任务</option>';
  if (state.monitorRunId) $("#monitorRunSelect").value = state.monitorRunId;
}
function runActionButtons(run) {
  const id = esc(run.id);
  const buttons = ['<button class="text-button run-open" data-id="' + id + '">查看</button>'];
  if (run.status === "queued") {
    buttons.push('<button class="text-button run-execute" data-id="' + id + '">执行</button>');
    buttons.push('<button class="text-button danger run-stop" data-id="' + id + '">取消</button>');
  } else if (run.status === "running") {
    buttons.push('<button class="text-button danger run-stop" data-id="' + id + '">停止</button>');
  } else if (["succeeded","failed","interrupted"].includes(run.status)) {
    buttons.push('<button class="text-button run-restart" data-id="' + id + '">重新配置</button>');
  }
  return buttons.join("");
}
async function submitRun(event) {
  event.preventDefault();
  const button = $("#runSubmitButton");
  button.disabled = true;
  try {
    let uploadId = "";
    const uploadMode = $("#runUploadMode").value;
    const files = Array.from($("#runFiles").files || []);
    const username = $("#runUsername").value.trim();
    const password = $("#runPassword").value;
    const host = $("#runHost").value.trim();
    const caseName = $("#runCaseName").value.trim();
    const translateName = $("#runTranslate").value.trim() || defaultTranslateName();
    const administratorUsername=$("#runAdministratorUsername").value.trim();
    const administratorPassword=$("#runAdministratorPassword").value;
    const appProfileValue=$("#runServerProfile").value;
    const gpuProfileValue=$("#runGpuServerProfile").value;
    const gpuHost=$("#runGpuHost").value.trim();
    if (!username) throw new Error("请填写登录用户名");
    if (!password) throw new Error("请填写登录密码");
    if (!host) throw new Error("请填写业务服务器地址");
    if (gpuProfileValue && !gpuHost) throw new Error("请选择或填写独立 GPU 服务器地址");
    if (!caseName) throw new Error("请填写案件名称");
    if (!translateName) throw new Error("请填写翻译语种");
    if(Boolean(administratorUsername)!==Boolean(administratorPassword)) throw new Error("业务管理员账号和密码必须同时填写");
    if (!files.length) {
      throw new Error("请从客户电脑选择测试文件夹");
    }
    if (files.length) {
      button.textContent = "正在上传 " + files.length + " 个文件…";
      const form = new FormData();
      files.forEach(function(file){ form.append("files", file, file.webkitRelativePath || file.name); });
      const uploaded = await api("/api/uploads", {method:"POST", body:form});
      uploadId = uploaded.upload_id;
      $("#runSubmitHint").textContent = "输入快照：" + uploaded.file_count + " 个文件，" + fmtSize(uploaded.total_bytes);
    }
    button.textContent = "正在创建任务…";
    const analysis = $$('input[name="analysis"]:checked').map(function(input){ return input.value; });
    const payload = {
      username:username,
      password:password,
      administrator_username:administratorUsername,
      administrator_password:administratorPassword,
      upload_id:uploadId,
      server_profile_id:appProfileValue === "manual" ? "" : appProfileValue,
      host:host,
      ssh_user:$("#runSshUser").value.trim(),
      ssh_password:$("#runSshPassword").value,
      gpu_server_profile_id:gpuProfileValue && gpuProfileValue !== "manual" ? gpuProfileValue : "",
      gpu_host:gpuHost,
      gpu_ssh_user:$("#runGpuSshUser").value.trim(),
      gpu_ssh_password:$("#runGpuSshPassword").value,
      case_name:caseName,
      translate_name:translateName,
      analysis:analysis,
      monitor_modules:$("#runMonitorModules").value.split(",").map(function(item){ return item.trim(); }).filter(Boolean),
      monitor_enable_perf:$("#runMonitorPerf").checked,
      monitor_enable_log:$("#runMonitorLogs").checked,
      upload_mode:uploadMode,
      upload_size_limit_mb:Number($("#runUploadSizeLimit").value || 1024),
      export_enable:$$('input[name="runExportType"]:checked').length > 0,
      export_types:$$('input[name="runExportType"]:checked').map(function(input){ return input.value; }),
      ai_checks_enable:$("#runAiChecks").checked,
      ai_simple_translation_enable:$("#runAiSimpleTranslation").checked,
      ai_xiaoyi_translation_enable:$("#runAiXiaoyiTranslation").checked,
      ai_xiaoyi_summary_enable:$("#runAiXiaoyiSummary").checked,
      ai_sample_path:$("#runAiSamplePath").value.trim(),
      ai_max_file_ids:Number($("#runAiMaxFileIds").value || 1),
      stress_enable:$("#runStressEnable").checked,
      stress_workers:Number($("#runStressWorkers").value || 0),
      stress_cpu_load:Number($("#runStressCpuLoad").value || 80),
      stress_duration:Number($("#runStressDuration").value || 30)
    };
    const result = await api("/api/run", {method:"POST", body:payload});
    state.monitorRunId = result.run_id;
    ["runPassword","runSshPassword","runGpuSshPassword","runAdministratorPassword"].forEach(function(id){$("#"+id).value="";});
    toast("任务已进入队列：" + (result.task && result.task.display_name ? result.task.display_name : shortId(result.run_id)));
    await loadRuns();
    gotoView("monitor");
  } catch (error) {
    toast("任务创建失败：" + error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "上传并发起测试";
  }
}
function reconfigureRun(runId) {
  const run=state.runs.find(function(item){return item.id===runId;});
  if(!run) {toast("未找到原任务",true);return;}
  const options=runOptions(run);
  prepareRunForm();
  $("#runUsername").value=options.username || "";
  $("#runCaseName").value=options.case_name || defaultRunCaseName();
  $("#runTranslate").value=options.translate_name || defaultTranslateName();

  const appProfileId=String(options.server_profile_id || "");
  $("#runServerProfile").value=state.serverProfiles.some(function(item){return item.id===appProfileId;}) ? appProfileId : "manual";
  const gpuProfileId=String(options.gpu_server_profile_id || "");
  $("#runGpuServerProfile").value=state.serverProfiles.some(function(item){return item.id===gpuProfileId;}) ? gpuProfileId : (options.gpu_host ? "manual" : "");
  syncRunServerProfileFields(false);
  if($("#runServerProfile").value==="manual") {
    $("#runHost").value=options.host || "";
    $("#runSshUser").value=options.ssh_user || "";
  }
  if($("#runGpuServerProfile").value==="manual") {
    $("#runGpuHost").value=options.gpu_host || "";
    $("#runGpuSshUser").value=options.gpu_ssh_user || "";
  }
  const analysis=Array.isArray(options.analysis) ? options.analysis : String(options.analysis || "").split(",");
  $$('input[name="analysis"]').forEach(function(input){input.checked=analysis.includes(input.value);});
  $("#runUploadMode").value=options.upload_mode || $("#runUploadMode").value;
  $("#runUploadSizeLimit").value=options.upload_size_limit_mb || $("#runUploadSizeLimit").value;
  $("#runMonitorModules").value=Array.isArray(options.monitor_modules) ? options.monitor_modules.join(",") : (options.monitor_modules || $("#runMonitorModules").value);
  setChecked("runMonitorPerf",options.monitor_enable_perf !== false);
  setChecked("runMonitorLogs",options.monitor_enable_log !== false);
  setChecked("runAiChecks",options.ai_checks_enable !== false);
  setChecked("runAiSimpleTranslation",options.ai_simple_translation_enable !== false);
  setChecked("runAiXiaoyiTranslation",options.ai_xiaoyi_translation_enable !== false);
  setChecked("runAiXiaoyiSummary",options.ai_xiaoyi_summary_enable !== false);
  setChecked("runStressEnable",Boolean(options.stress_enable));
  $("#runStressWorkers").value=options.stress_workers || 0;
  $("#runStressCpuLoad").value=options.stress_cpu_load || 80;
  $("#runStressDuration").value=options.stress_duration || 30;
  syncUploadModeUi();
  syncRunSummary();
  gotoView("new-run");
  $("#runPassword").focus();
  toast("已恢复非敏感配置，请重新填写本次授权密码");
}
async function runAction(runId, action) {
  const labels = {stop:"停止", restart:"重跑", execute:"执行"};
  try {
    const result = await api("/api/runs/" + encodeURIComponent(runId) + "/" + action, {method:"POST"});
    if (result.id) {
      state.monitorRunId = result.id;
      toast((labels[action] || "操作") + "成功：" + (result.display_name || shortId(result.id)));
    } else {
      toast((labels[action] || "操作") + "成功");
    }
    await loadRuns();
    if (action !== "stop") gotoView("monitor");
  } catch (error) {
    toast((labels[action] || "操作") + "失败：" + error.message, true);
  }
}
function mergeRowsById(current, incoming, limit) {
  const seen = new Set();
  return (current || []).concat(incoming || []).filter(function(item){
    const key = item && item.id != null
      ? "id:" + item.id
      : "row:" + JSON.stringify([item && item.created_at, item && item.level, item && item.stage, item && item.message, item && item.source]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(-limit);
}
async function refreshMonitor(reset) {
  const runId = $("#monitorRunSelect").value || state.monitorRunId;
  if (!runId) return;
  if (state.monitorRunId !== runId || reset) {
    state.monitorRunId = runId;
    state.logFollow[runId] = true;
    if (reset && !state.logs[runId]) { state.logCursor[runId] = 0; state.metricCursor[runId] = 0; }
  }
  if (state.monitorRefreshInFlight[runId]) return;
  state.monitorRefreshInFlight[runId] = true;
  try {
    const run = await api("/api/runs/" + encodeURIComponent(runId));
    const logAfter = state.logCursor[runId] || 0;
    const metricAfter = state.metricCursor[runId] || 0;
    const results = await Promise.all([
      api("/api/runs/" + encodeURIComponent(runId) + "/logs?after_id=" + logAfter + "&limit=2000"),
      api("/api/runs/" + encodeURIComponent(runId) + "/metrics?after_id=" + metricAfter + "&limit=20000")
    ]);
    state.logs[runId] = mergeRowsById(state.logs[runId], results[0].events, 500);
    state.metrics[runId] = mergeRowsById(state.metrics[runId], results[1].metrics, 600);
    state.logCursor[runId] = results[0].last_id || logAfter;
    state.metricCursor[runId] = results[1].last_id || metricAfter;
    renderMonitor(run);
  } catch (error) {
    if (error.status !== 404) console.warn(error);
  } finally {
    delete state.monitorRefreshInFlight[runId];
  }
}
function renderMonitor(run) {
  $("#monitorSubtitle").textContent = runLabel(run) + " · " + (stageNames[run.stage] || run.stage) + " · " + (run.message || "");
  $("#monitorProgress").textContent = run.progress + "%";
  $("#monitorProgressBar").style.width = run.progress + "%";
  const logs = state.logs[run.id] || [];
  const timelineNode = $("#eventTimeline");
  timelineNode.innerHTML = logs.filter(function(item){ return item.stage || item.progress != null; }).slice(-14).map(function(item){
    const time = new Date(Number(item.created_at) * 1000).toLocaleTimeString("zh-CN", {hour12:false});
    return '<div class="event"><span>' + esc(time) + '</span><i></i><div><b>' + esc(stageNames[item.stage] || item.stage || item.level) + "</b><span>" + esc(item.message) + "</span></div></div>";
  }).join("") || '<p class="muted">等待阶段事件…</p>';
  timelineNode.scrollTop = timelineNode.scrollHeight;
  const consoleNode = $("#logConsole");
  const followLogs = state.logFollow[run.id] !== false;
  const previousScrollTop = consoleNode.scrollTop;
  consoleNode.textContent = logs.map(function(item){
    const time = new Date(Number(item.created_at) * 1000).toLocaleTimeString("zh-CN", {hour12:false});
    return time + " [" + item.level + "] " + item.message;
  }).join("\n") || "等待任务日志…";
  consoleNode.scrollTop = followLogs ? consoleNode.scrollHeight : previousScrollTop;
  state.logScrollTop[run.id] = consoleNode.scrollTop;
  updateLogFollowUi(run.id);
  renderTranslationSpeed(run.translation_speed || {});
  const metrics = state.metrics[run.id] || [];
  const cpu = values(metrics, "cpu_pct");
  const gpu = values(metrics, "gpu_pct");
  const mem = values(metrics, "mem_used_gb");
  updateMetric("cpuNow", cpu, "%"); updateAveragePeakMetric("gpuNow", gpu, "%"); updateMetric("memNow", mem, " GB");
  renderBusinessCpuTemperatureReadings(metrics);
  drawChart($("#cpuChart"), cpu, "#4fb3ff", 100);
  drawChart($("#gpuChart"), gpu, "#f6b94f", 100);
  drawChart($("#memChart"), mem, "#8f9dff", null);
}
function formatMonitorDuration(seconds) {
  const total=Math.max(0,Math.round(Number(seconds) || 0));
  const hours=Math.floor(total/3600);
  const minutes=Math.floor((total%3600)/60);
  const secs=total%60;
  if(hours) return hours+"小时"+minutes+"分"+secs+"秒";
  if(minutes) return minutes+"分"+secs+"秒";
  return secs+"秒";
}
function renderTranslationSpeed(speed) {
  const value=$("#translationSpeedNow");
  const currentNode=$("#translationSpeedCurrent");
  const detail=$("#translationSpeedDetail");
  const progress=$("#translationSpeedProgress");
  if(!value || !currentNode || !detail || !progress) return;
  const current=Number(speed.current);
  const total=Number(speed.total);
  const hasProgress=Number.isFinite(current) && Number.isFinite(total) && total>0;
  progress.style.width=hasProgress ? Math.max(0,Math.min(100,current*100/total)).toFixed(2)+"%" : "0%";
  currentNode.textContent=hasProgress ? "当前 "+current+" / "+total : "等待翻译进度";
  if(speed.state === "available" && Number.isFinite(Number(speed.items_per_minute))) {
    value.textContent=Number(speed.items_per_minute).toFixed(2)+" 个/分钟";
    detail.textContent="统计区间新增 "+Number(speed.translated_count || 0)+" 个 · "+formatMonitorDuration(speed.elapsed_seconds);
    return;
  }
  if(speed.state === "measuring") {
    value.textContent="采集中";
    detail.textContent="至少需要 2 个不同时间的进度采样点";
    return;
  }
  value.textContent="—";
  detail.textContent="按实际进度事件时间戳统计";
}
function values(metrics, key) {
  return metrics.map(function(row){
    const data = row.data || {}; let value = data[key];
    if ((value === "" || value == null) && key === "gpu_pct") {
      const cards = Object.keys(data).filter(function(name){ return /^gpu\d+_pct$/.test(name); }).map(function(name){ return Number(data[name]); }).filter(Number.isFinite);
      value = cards.length ? cards.reduce(function(a,b){ return a+b; },0) / cards.length : null;
    }
    if (value === "" || value == null) return NaN;
    return Number(value);
  }).filter(Number.isFinite);
}
function cpuTemperatureSeries(metrics) {
  const sensors=new Map();
  metrics.forEach(function(row){
    const data=row.data || {};
    if(data.cpu_temp_status !== "available" || !["lm-sensors","sysfs"].includes(data.cpu_temp_source)) return;
    let readings=Array.isArray(data.cpu_temp_readings) ? data.cpu_temp_readings : [];
    if(!readings.length && data.cpu_temp_label && data.cpu_temp_c !== "" && data.cpu_temp_c != null) {
      readings=[{id:data.cpu_temp_label,label:data.cpu_temp_label,chip:"",value:data.cpu_temp_c}];
    }
    readings.forEach(function(reading){
      const value=Number(reading.value);
      if(!Number.isFinite(value)) return;
      const id=String(reading.id || ((reading.chip || "")+"|"+(reading.label || "CPU")));
      if(!sensors.has(id)) sensors.set(id,{id:id,label:String(reading.label || "CPU"),chip:String(reading.chip || ""),source:data.cpu_temp_source,values:[]});
      sensors.get(id).values.push(value);
    });
  });
  return Array.from(sensors.values());
}
function updateMetric(id, list, unit) {
  $("#" + id).textContent = list.length ? list[list.length-1].toFixed(1) + unit + " / " + Math.max.apply(null,list).toFixed(1) + unit : "—";
}
function renderCpuTemperatureReadings(metrics) {
  renderCpuTemperatureCard(metrics,"stressTempReadings","stressTempSource","当前会话仍在使用旧版采集，请重启服务并重新连接");
}
function renderBusinessCpuTemperatureReadings(metrics) {
  renderCpuTemperatureCard(metrics,"businessTempReadings","businessTempSource","历史任务使用旧版温度口径，请重新执行后查看封装温度");
}
function renderCpuTemperatureCard(metrics,readingsId,sourceId,legacyMessage) {
  const container=$("#"+readingsId);
  const node=$("#"+sourceId);
  if(!container || !node) return;
  const sensors=cpuTemperatureSeries(metrics);
  if(sensors.length) {
    container.innerHTML=sensors.map(function(sensor){
      const current=sensor.values[sensor.values.length-1];
      const peak=Math.max.apply(null,sensor.values);
      return '<div class="cpu-temperature-reading"><div><b>'+esc(sensor.label)+'</b><small>'+esc(sensor.chip || (sensor.source === "sysfs" ? "Linux sysfs" : "lm-sensors"))+'</small></div><strong>'+current.toFixed(1)+' °C / '+peak.toFixed(1)+' °C</strong></div>';
    }).join("");
    const sources=Array.from(new Set(sensors.map(function(sensor){return sensor.source === "sysfs" ? "Linux sysfs" : sensor.source;})));
    node.textContent="来源："+sources.join(" / ");
    const latestTrusted=metrics.slice().reverse().map(function(row){return row.data || {};}).find(function(data){return data.cpu_temp_status === "available" && data.cpu_temp_details;});
    node.title=latestTrusted ? latestTrusted.cpu_temp_details : node.textContent;
    node.classList.remove("unavailable");
    return;
  }
  container.innerHTML='<b class="cpu-temperature-empty">—</b>';
  let latest=null;
  for(let index=metrics.length-1;index>=0;index--) {
    const data=metrics[index].data || {};
    if(Object.prototype.hasOwnProperty.call(data,"cpu_temp_c") || data.cpu_temp_status) {latest=data;break;}
  }
  if(!latest) {
    node.textContent="等待 CPU 温度采样";
    node.title="";
    node.classList.remove("unavailable");
    return;
  }
  const legacyValue=latest.cpu_temp_c !== "" && latest.cpu_temp_c != null && Number.isFinite(Number(latest.cpu_temp_c));
  if(legacyValue && !latest.cpu_temp_status) {
    node.textContent=legacyMessage;
  } else {
    node.textContent=latest.cpu_temp_details || "CPU 温度不可用";
  }
  node.title=latest.cpu_temp_details || node.textContent;
  node.classList.add("unavailable");
}
function updateAveragePeakMetric(id, list, unit) {
  if (!list.length) {
    $("#" + id).textContent = "—";
    return;
  }
  const average = list.reduce(function(sum,value){ return sum + value; },0) / list.length;
  $("#" + id).textContent = average.toFixed(1) + unit + " / " + Math.max.apply(null,list).toFixed(1) + unit;
}
function drawChart(canvas, list, color, fixedMax, adaptiveScale) {
  const rect = canvas.getBoundingClientRect(); const ratio = window.devicePixelRatio || 1;
  const width = Math.max(160, rect.width); const height = Math.max(54,Math.min(95,rect.height || 95));
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio); ctx.clearRect(0,0,width,height);
  ctx.strokeStyle = "rgba(126,157,184,.24)"; ctx.lineWidth = 1;
  [.25,.5,.75].forEach(function(position){const y=Math.round(height*position);ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();});
  if (list.length < 2) return;
  const data = list.slice(-120);
  let max = fixedMax || Math.max.apply(null,data.concat([1])); let min = 0;
  if(adaptiveScale && data.length) {
    const dataMin=Math.min.apply(null,data); const dataMax=Math.max.apply(null,data);
    const padding=Math.max(.5,(dataMax-dataMin)*.2);
    min=Math.max(0,dataMin-padding);
    max=Math.min(100,dataMax+padding);
    if(max-min<1) {min=Math.max(0,dataMin-.5);max=Math.min(100,dataMax+.5);}
  }
  ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2;
  data.forEach(function(value,index){
    const x = index * width / Math.max(1,data.length-1);
    const y = height - 10 - (value-min)/(max-min || 1)*(height-20);
    if (!index) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  });
  ctx.stroke();
  ctx.lineTo(width,height); ctx.lineTo(0,height); ctx.closePath();
  const gradient = ctx.createLinearGradient(0,0,0,height); gradient.addColorStop(0,color+"35"); gradient.addColorStop(1,color+"00");
  ctx.fillStyle = gradient; ctx.fill();
}
function metricStats(list) {
  if (!list.length) return {current:null, average:null, peak:null};
  return {
    current:list[list.length - 1],
    average:list.reduce(function(sum,value){ return sum + value; },0) / list.length,
    peak:Math.max.apply(null,list)
  };
}
function formatGpuNumber(value, unit) {
  return Number.isFinite(Number(value)) && value !== "" && value != null ? Number(value).toFixed(1) + (unit || "") : "N/A";
}
function formatGpuTriplet(list, unit) {
  const stats=metricStats(list);
  if (stats.current == null) return "N/A";
  return [stats.current,stats.average,stats.peak].map(function(value){return formatGpuNumber(value,unit);}).join(" / ");
}
function latestMetricData(metrics, predicate) {
  for (let index=metrics.length-1; index>=0; index-=1) {
    const data=metrics[index].data || {};
    if (!predicate || predicate(data)) return data;
  }
  return {};
}
function gpuIndexes(metrics) {
  const indexes={};
  metrics.forEach(function(row){
    Object.keys(row.data || {}).forEach(function(key){
      const match=key.match(/^gpu(\d+)_pct$/);
      if(match) indexes[Number(match[1])]=true;
    });
  });
  return Object.keys(indexes).map(Number).sort(function(a,b){return a-b;});
}
function gpuMemoryPercentValues(metrics, index) {
  const prefix=index == null ? "gpu_" : "gpu" + index + "_";
  return metrics.map(function(row){
    const data=row.data || {};
    const direct=data[prefix + "mem_pct"];
    if(direct !== "" && direct != null && Number.isFinite(Number(direct))) return Number(direct);
    const used=Number(data[prefix + "mem_mb"]); const total=Number(data[prefix + "mem_total_mb"]);
    return Number.isFinite(used) && total>0 ? used/total*100 : NaN;
  }).filter(Number.isFinite);
}
function gpuMetricIconMarkup(kind) {
  const paths={
    utilization:'<path d="M5 16a7 7 0 1 1 14 0"/><path d="m12 13 4-4"/><path d="M7 17h10"/>',
    memory:'<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 9h6v6H9zM9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/>',
    temperature:'<path d="M10 14.8V5a2 2 0 1 1 4 0v9.8a4 4 0 1 1-4 0Z"/><path d="M12 11v6"/>'
  };
  return '<span class="gpu-metric-icon '+esc(kind)+'" aria-hidden="true"><svg viewBox="0 0 24 24">'+(paths[kind] || paths.utilization)+'</svg></span>';
}
function gpuStatMarkup(label, list, unit, currentOverride, kind) {
  const stats=metricStats(list);
  const current=currentOverride || formatGpuNumber(stats.current,unit);
  return '<div class="gpu-live-stat">'+gpuMetricIconMarkup(kind)+'<div><span>'+esc(label)+'</span><strong>'+esc(current)+'</strong><small>均值 '+esc(formatGpuNumber(stats.average,unit))+' · 峰值 '+esc(formatGpuNumber(stats.peak,unit))+'</small></div></div>';
}
function gpuTrendMarkup(label, list) {
  const recent=list.slice(-120);
  return '<span><b>'+esc(label)+'</b><small>最近 '+recent.length+' 次采样</small></span>';
}
function formatGpuMemoryCapacity(usedMib,totalMib) {
  const used=Number(usedMib); const total=Number(totalMib);
  if(!Number.isFinite(used)) return "N/A";
  return (used/1024).toFixed(1)+(Number.isFinite(total) && total>0 ? " / "+(total/1024).toFixed(1) : "")+" GiB";
}
function gpuDetailMarkup(label,current,list,unit,note) {
  const stats=metricStats(list || []);
  const summary=note || (stats.current == null ? "当前未采集" : "平均 "+formatGpuNumber(stats.average,unit)+" · 峰值 "+formatGpuNumber(stats.peak,unit));
  return '<div class="gpu-detail-stat"><span>'+esc(label)+'</span><strong>'+esc(current)+'</strong><small>'+esc(summary)+'</small></div>';
}
function gpuSampleTime(rows) {
  const latestRow=rows[rows.length-1];
  if(!latestRow || latestRow.created_at == null) return "";
  const numeric=Number(latestRow.created_at);
  const date=Number.isFinite(numeric) ? new Date(numeric < 1e12 ? numeric*1000 : numeric) : new Date(latestRow.created_at);
  if(Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN",{hour12:false,hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
function renderStressGpu(job, metrics) {
  state.gpuRenderContext={job:job,metrics:metrics};
  const indexes=gpuIndexes(metrics);
  const statusNode=$("#stressGpuSamplingStatus");
  const messageNode=$("#stressGpuSamplingMessage");
  const cardsNode=$("#stressGpuCards");
  const gpuRows=metrics.filter(function(row){
    const data=row.data || {};
    return data.gpu_sampling_status || Object.keys(data).some(function(key){return /^gpu\d+_pct$/.test(key);});
  });
  const latest=latestMetricData(gpuRows);
  const hasEnhancedMetrics=gpuRows.some(function(row){
    return Object.keys(row.data || {}).some(function(key){return /^gpu\d+_(mem_total_mb|power_w|sm_clock_mhz|fan_pct|pstate)$/.test(key);});
  });
  let status=latest.gpu_sampling_status || (indexes.length ? (hasEnhancedMetrics ? "available" : "partial") : "waiting");
  if(!indexes.length && ["succeeded","failed","interrupted"].includes(String(job.status || ""))) status="unavailable";
  const statusMeta={
    available:["采集正常","available"],
    partial:["基础采集正常 · 部分字段 N/A","partial"],
    unavailable:["GPU 采集不可用","unavailable"],
    waiting:["等待 GPU 采样","waiting"]
  }[status] || ["采集状态未知","waiting"];
  statusNode.className="gpu-sampling-state " + statusMeta[1];
  statusNode.innerHTML='<i></i><span>'+esc(statusMeta[0]+(gpuRows.length ? " · "+gpuRows.length+" 次采样" : ""))+'</span>';
  const workspace=cardsNode.closest(".gpu-monitor-workspace");
  if(workspace) workspace.dataset.gpuStatus=status;
  messageNode.className="gpu-sampling-message " + status;
  const sampleTime=gpuSampleTime(gpuRows);
  const gpuChanged=indexes.some(function(index){
    return [values(metrics,"gpu"+index+"_pct"),gpuMemoryPercentValues(metrics,index)].some(function(list){
      return list.length > 1 && Math.max.apply(null,list)-Math.min.apply(null,list) >= .1;
    });
  });
  messageNode.textContent=latest.gpu_sampling_message || (
    status === "waiting" ? "任务开始后，这里会显示总体指标和每张显卡的独立曲线。" :
    status === "unavailable" ? "当前任务没有获得可用 GPU 数据，请检查 nvidia-smi、驱动和 SSH 连接。" :
    status === "partial" ? "基础利用率、显存和温度可用；此任务未采集或不支持部分扩展字段。" : "GPU 指标正在持续采集中。"
  );
  if(gpuRows.length && !latest.gpu_sampling_message) {
    messageNode.textContent+=" 最近更新 "+(sampleTime || "刚刚")+"；"+(gpuChanged ? "采样期间检测到指标变化。" : "当前数值变化较小，采集仍在持续。");
  }

  $("#stressGpuOverviewUtil").textContent=formatGpuTriplet(values(metrics,"gpu_pct"),"%");
  $("#stressGpuOverviewMemory").textContent=formatGpuTriplet(gpuMemoryPercentValues(metrics,null),"%");
  $("#stressGpuOverviewTemp").textContent=formatGpuTriplet(values(metrics,"gpu_temp_c")," °C");
  $("#stressGpuOverviewPower").textContent=formatGpuTriplet(values(metrics,"gpu_power_w")," W");

  if(!indexes.length) {
    state.expandedGpuIndex=null;
    cardsNode.innerHTML='<div class="gpu-monitor-empty">'+esc(status === "unavailable" ? "本次任务未采集到 GPU 指标" : "尚无 GPU 采样数据")+'</div>';
    renderGpuDetailDrawer(metrics,null,status,gpuRows);
    return;
  }
  if(state.expandedGpuIndex != null && !indexes.includes(state.expandedGpuIndex)) state.expandedGpuIndex=null;
  cardsNode.innerHTML=indexes.map(function(index){
    const prefix="gpu"+index+"_";
    const cardLatest=latestMetricData(metrics,function(data){return data[prefix+"pct"] != null || data[prefix+"name"];});
    const utilization=values(metrics,prefix+"pct");
    const memory=gpuMemoryPercentValues(metrics,index);
    const memoryUsed=values(metrics,prefix+"mem_mb");
    const temperature=values(metrics,prefix+"temp_c");
    const cardStatus=status === "available" ? "正常" : status === "unavailable" ? "采集中断" : "部分 N/A";
    const selected=state.expandedGpuIndex===index;
    return '<article class="gpu-live-card gpu-status-'+esc(status)+(selected ? " selected" : "")+'" data-live-gpu-card="'+index+'">'
      +'<div class="gpu-live-card-head"><div><b>GPU '+index+'</b><span>'+esc(cardLatest[prefix+"name"] || "未识别型号")+'</span></div><div class="gpu-card-head-actions"><span class="gpu-health '+esc(status)+'"><i></i><span>'+esc(cardStatus)+'</span></span></div></div>'
      +'<div class="gpu-live-stats gpu-live-primary">'
      +gpuStatMarkup("使用率",utilization,"%",null,"utilization")
      +gpuStatMarkup(memory.length ? "显存占用" : "显存已用",memory.length ? memory : memoryUsed,memory.length ? "%" : " MiB",null,"memory")
      +gpuStatMarkup("温度",temperature," °C",null,"temperature")
      +'</div>'
      +'<div class="gpu-card-footer"><button type="button" class="gpu-card-expand" data-gpu-expand="'+index+'" aria-expanded="'+(selected ? "true" : "false")+'" aria-label="'+(selected ? "关闭 GPU "+index+" 指标详情" : "在右侧查看 GPU "+index+" 指标详情")+'"><span>'+(selected ? "收起详情" : "指标详情")+'</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button></div>'
      +'</article>';
  }).join("");
  renderGpuDetailDrawer(metrics,state.expandedGpuIndex,status,gpuRows);
}
function renderGpuDetailDrawer(metrics,index,status,gpuRows) {
  const drawer=$("#gpuDetailDrawer");
  if(index == null) {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden","true");
    delete drawer.dataset.gpuIndex;
    return;
  }
  const prefix="gpu"+index+"_";
  const cardLatest=latestMetricData(metrics,function(data){return data[prefix+"pct"] != null || data[prefix+"name"];});
  const utilization=values(metrics,prefix+"pct");
  const memory=gpuMemoryPercentValues(metrics,index);
  const memoryUsed=values(metrics,prefix+"mem_mb");
  const temperature=values(metrics,prefix+"temp_c");
  const power=values(metrics,prefix+"power_w");
  const clock=values(metrics,prefix+"sm_clock_mhz");
  const fan=values(metrics,prefix+"fan_pct");
  const memoryCapacity=formatGpuMemoryCapacity(cardLatest[prefix+"mem_mb"],cardLatest[prefix+"mem_total_mb"]);
  const powerCurrent=formatGpuNumber(metricStats(power).current," W") + (cardLatest[prefix+"power_limit_w"] == null ? "" : " / "+formatGpuNumber(cardLatest[prefix+"power_limit_w"]," W"));
  const cardStatus=status === "available" ? "正常" : status === "unavailable" ? "采集中断" : "部分 N/A";
  const content=$("#gpuDetailDrawerContent");
  const changedGpu=drawer.dataset.gpuIndex!==String(index);
  const previousScroll=changedGpu ? 0 : content.scrollTop;
  $("#gpuDetailDrawerTitle").textContent="GPU "+index+" 指标详情";
  $("#gpuDetailDrawerModel").textContent=cardLatest[prefix+"name"] || "未识别型号";
  const statusNode=$("#gpuDetailDrawerStatus");
  statusNode.className="gpu-health "+status;
  statusNode.innerHTML='<i></i><span>'+esc(cardStatus)+'</span>';
  content.innerHTML='<div class="gpu-drawer-sample-meta"><span>最近更新 '+esc(gpuSampleTime(gpuRows) || "刚刚")+'</span><span>'+esc((gpuRows || []).length+" 次采样")+'</span></div>'
    +'<section class="gpu-drawer-section"><h4>核心状态</h4><div class="gpu-drawer-primary">'
    +gpuStatMarkup("使用率",utilization,"%",null,"utilization")
    +gpuStatMarkup(memory.length ? "显存占用" : "显存已用",memory.length ? memory : memoryUsed,memory.length ? "%" : " MiB",null,"memory")
    +gpuStatMarkup("温度",temperature," °C",null,"temperature")
    +'</div></section>'
    +'<section class="gpu-drawer-section"><h4>扩展指标</h4><div class="gpu-drawer-detail-grid">'
    +gpuDetailMarkup("显存容量",memoryCapacity,[],"","当前已用 / 总容量")
    +gpuDetailMarkup("功耗 / 上限",powerCurrent,power," W")
    +gpuDetailMarkup("SM 频率",formatGpuNumber(metricStats(clock).current," MHz"),clock," MHz")
    +gpuDetailMarkup("风扇",formatGpuNumber(metricStats(fan).current,"%"),fan,"%")
    +gpuDetailMarkup("性能状态",cardLatest[prefix+"pstate"] || "N/A",[],"","当前 P-State")
    +'</div></section>'
    +'<section class="gpu-drawer-section"><h4>趋势</h4><div class="gpu-drawer-charts"><article>'+gpuTrendMarkup("使用率趋势",utilization)+'<canvas data-gpu-drawer-chart="util"></canvas></article><article>'+gpuTrendMarkup("显存占比趋势",memory)+'<canvas data-gpu-drawer-chart="memory"></canvas></article></div></section>';
  drawer.dataset.gpuIndex=String(index);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden","false");
  content.scrollTop=previousScroll;
  content.querySelectorAll("canvas[data-gpu-drawer-chart]").forEach(function(canvas){
    const isMemory=canvas.dataset.gpuDrawerChart==="memory";
    drawChart(canvas,isMemory ? memory : utilization,isMemory ? "#8f9dff" : "#f6b94f",isMemory ? null : 100,isMemory);
  });
}
function toggleGpuLiveDetails(index) {
  state.expandedGpuIndex=state.expandedGpuIndex===index ? null : index;
  const context=state.gpuRenderContext;
  if(context) renderStressGpu(context.job,context.metrics);
}
function closeGpuDetailDrawer() {
  if(state.expandedGpuIndex == null) return;
  state.expandedGpuIndex=null;
  const context=state.gpuRenderContext;
  if(context) renderStressGpu(context.job,context.metrics);
}
function fillStressModuleDefaults() {
  if (!state.bootstrap) return;
  if (!$("#stressModules").value && state.bootstrap.monitor_containers) {
    $("#stressModules").value = state.bootstrap.monitor_containers.join(",");
  }
}
const stressPresets = {
  quick:{duration:60,modes:["monitor","cpu","gpu"],hint:"快速验证：60 秒，同时验证 CPU 与检测为空闲的 GPU。"},
  standard:{duration:300,modes:["monitor","cpu","gpu"],hint:"标准测试：5 分钟，采集 CPU、GPU、内存与磁盘指标。"},
  stability:{duration:1800,modes:["monitor","cpu","gpu"],hint:"稳定性测试：30 分钟，请在维护窗口执行。"},
  custom:{duration:null,modes:null,hint:"自定义方案：按需选择 CPU/GPU 负载、持续时间与安全阈值。"}
};
function updateStressStartState() {
  const modes = $$('input[name="stressMode"]:checked').map(function(input){return input.value;});
  const loadModes=modes.filter(function(mode){return mode === "cpu" || mode === "gpu";});
  const needsGpu = modes.includes("gpu");
  const ready = Boolean(state.serverSessionId) && Boolean(state.stressCapability) && loadModes.length > 0 && (!needsGpu || state.selectedGpuDevices.length > 0);
  $("#stressSubmitButton").disabled = !ready;
}
function syncStressGpuSelection() {
  state.selectedGpuDevices.sort(function(a,b){return a-b;});
  $("#stressGpuDevices").value = state.selectedGpuDevices.join(",");
  const available = ((state.stressCapability || {}).gpu || {}).devices || [];
  const selectableCount = available.filter(function(device){return device.stress_selectable || device.selectable;}).length;
  $("#gpuSelectionSummary").textContent = state.selectedGpuDevices.length
    ? "已选择 " + state.selectedGpuDevices.length + " 张：GPU " + state.selectedGpuDevices.join("、")
    : (available.length ? "当前有 " + selectableCount + " 张 GPU 可进行满载测试" : "检测后展示 CPU、GPU、内存与磁盘，并推荐安全方案。");
  $$("[data-gpu-index]").forEach(function(card){
    const selected=state.selectedGpuDevices.includes(Number(card.dataset.gpuIndex));
    card.classList.toggle("selected",selected);
    card.setAttribute("aria-pressed",selected ? "true" : "false");
    const marker=$(".gpu-select-marker",card);
    if(marker) marker.textContent=selected ? "✓ 已选择" : (card.disabled ? "当前不可压测" : "选择此卡");
  });
  updateStressStartState();
}
function resetStressDiscovery(message) {
  state.stressCapability = null;
  state.selectedGpuDevices = [];
  $("#stressGpuDevices").value = "";
  $("#gpuDevicePicker").innerHTML = '<div class="gpu-empty-state">' + esc(message || "服务器信息已变化，请重新检测") + '</div>';
  $("#gpuSelectionSummary").textContent = "检测后展示 CPU、GPU、内存与磁盘，并推荐安全方案。";
  $("#capabilityResult").className = "capability-result";
  $("#capabilityResult").innerHTML = '<div class="empty-result-note"><strong>等待环境检测</strong><p>完成检测后，这里会给出操作系统兼容性、CPU/GPU 执行能力、内存/磁盘采集状态和安全建议。</p></div>';
  $("#resourceCpuOverview").textContent = "待检测";
  $("#resourceGpuOverview").textContent = "待检测";
  $("#resourceMemoryOverview").textContent = "待检测";
  $("#resourceDiskOverview").textContent = "待检测";
  $("#stressStrategyBanner").className = "stress-strategy-banner hidden";
  $("#stressStrategyBanner").innerHTML = "";
  updateStressStartState();
}
function applyStressPreset(name) {
  const preset=stressPresets[name] || stressPresets.quick;
  state.stressPreset=name in stressPresets ? name : "quick";
  $$("[data-stress-preset]").forEach(function(card){card.classList.toggle("active",card.dataset.stressPreset===state.stressPreset);});
  if(preset.duration != null) {
    $("#stressDuration").value=String(preset.duration);
  }
  if(Array.isArray(preset.modes)) {
    $$('input[name="stressMode"]').forEach(function(input){input.checked=preset.modes.includes(input.value);});
  }
  $("#stressPresetHint").textContent=preset.hint;
  $("#stressSubmitButton").textContent="启动压测";
  $("#stressSubmitHint").textContent="压测复用当前会话，并持续监控 CPU、GPU、内存与磁盘安全阈值。";
  syncStressGpuSelection();
  updateStressStartState();
}
function renderGpuDevices(devices) {
  const picker=$("#gpuDevicePicker");
  if(!devices.length) {
    state.selectedGpuDevices=[];
    picker.innerHTML='<div class="gpu-empty-state blocked">未发现可识别的 NVIDIA GPU</div>';
    syncStressGpuSelection();
    return;
  }
  const preserved=state.selectedGpuDevices.filter(function(index){
    const device=devices.find(function(item){return item.index===index;}); return device && (device.stress_selectable || device.selectable);
  });
  const firstStressReady=devices.find(function(device){return device.stress_selectable || device.selectable;});
  state.selectedGpuDevices=preserved.length ? preserved : (firstStressReady ? [firstStressReady.index] : []);
  const statusLabels={ready:"空闲",degraded:"兼容模式",busy:"业务占用",blocked:"环境不完整"};
  picker.innerHTML=devices.map(function(device){
    const total=Number(device.memory_total_mib || 0); const used=Number(device.memory_used_mib || 0);
    const usedPercent=total ? Math.min(100,Math.round(used/total*100)) : 0;
    const selected=state.selectedGpuDevices.includes(Number(device.index));
    const classes="gpu-device-card "+esc(device.status || "blocked")+(selected ? " selected" : "");
    const stressSelectable=device.stress_selectable || device.selectable;
    const occupancyLabel=device.occupancy_label || statusLabels[device.status] || device.status;
    return '<button type="button" class="'+classes+'" data-gpu-index="'+esc(device.index)+'" aria-pressed="'+(selected ? "true" : "false")+'" '+(stressSelectable ? "" : "disabled")+'>'+ 
      '<span class="gpu-card-top"><b>GPU '+esc(device.index)+'</b><i>'+esc(occupancyLabel)+'</i></span>'+ 
      '<strong class="gpu-name">'+esc(device.name || "NVIDIA GPU")+'</strong>'+ 
      '<span class="gpu-specs"><span>显存 '+(used/1024).toFixed(1)+' / '+(total/1024).toFixed(1)+' GiB</span><span>利用率 '+esc(device.utilization_percent)+'%</span><span>温度 '+esc(device.temperature_c)+'℃</span><span>CC '+esc(device.compute_capability)+' · '+esc(device.architecture_family)+'</span></span>'+ 
      '<span class="gpu-memory-bar"><i style="width:'+usedPercent+'%"></i></span>'+ 
      '<small>'+esc(device.reason || "等待平台匹配执行器")+'</small><span class="gpu-select-marker">'+(selected ? "✓ 已选择" : (stressSelectable ? "选择此卡" : "当前不可压测"))+'</span></button>';
  }).join("");
  syncStressGpuSelection();
}
function toggleStressGpu(index) {
  const devices=(((state.stressCapability || {}).gpu || {}).devices || []);
  const device=devices.find(function(item){return item.index===index;});
  if(!device || !(device.stress_selectable || device.selectable)) return;
  const position=state.selectedGpuDevices.indexOf(index);
  if(position>=0) state.selectedGpuDevices.splice(position,1); else state.selectedGpuDevices.push(index);
  syncStressGpuSelection();
}

function serverProfilePayload() {
  return {
    name:$("#stressServerName").value.trim(),
    host:$("#stressHost").value.trim(),
    port:Number($("#stressPort").value || 22),
    user:$("#stressUser").value.trim(),
    password:$("#stressPassword").value,
    save_password:$("#serverSavePassword").checked
  };
}
function openServerProfileDrawer() {
  const drawer=$("#serverProfileDrawer");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden","false");
  document.body.classList.add("server-drawer-open");
}
function closeServerProfileDrawer(force) {
  const drawer=$("#serverProfileDrawer");
  if(!force && $("#connectServerButton").disabled) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden","true");
  document.body.classList.remove("server-drawer-open");
}
function resetServerProfileForm(openDrawer) {
  $("#serverProfileId").value="";
  $("#stressServerName").value="";
  $("#stressHost").value="";
  $("#stressPort").value="22";
  $("#stressUser").value="";
  $("#stressPassword").value="";
  $("#serverSaveConfig").checked=true;
  $("#serverSavePassword").checked=false;
  $("#serverProfileFormTitle").textContent="添加服务器";
  syncServerSaveOptions();
  if(openDrawer !== false) {
    openServerProfileDrawer();
    window.setTimeout(function(){$("#stressServerName").focus();},80);
  }
}
function syncServerSaveOptions() {
  const saveConfig=$("#serverSaveConfig").checked;
  $("#serverSavePassword").disabled=!saveConfig;
  $("#saveServerProfileButton").disabled=!saveConfig;
  if(!saveConfig) $("#serverSavePassword").checked=false;
}
function editServerProfile(profileId) {
  const profile=state.serverProfiles.find(function(item){return item.id===profileId;});
  if(!profile) return;
  $("#serverProfileId").value=profile.id;
  $("#stressServerName").value=profile.name || "";
  $("#stressHost").value=profile.host || "";
  $("#stressPort").value=String(profile.port || 22);
  $("#stressUser").value=profile.user || "";
  $("#stressPassword").value="";
  $("#serverSaveConfig").checked=true;
  $("#serverSavePassword").checked=Boolean(profile.credential_saved);
  $("#serverProfileFormTitle").textContent="编辑服务器配置";
  syncServerSaveOptions();
  openServerProfileDrawer();
}
async function saveServerProfile() {
  if(!$("#serverSaveConfig").checked) throw new Error("请先启用“保存服务器配置”");
  const payload=serverProfilePayload();
  if(!payload.name) throw new Error("请填写服务器名称");
  if(!payload.host) throw new Error("请填写服务器 IP / 主机名");
  if(!payload.user) throw new Error("请填写 SSH 用户");
  const profileId=$("#serverProfileId").value;
  const profile=await api(profileId ? "/api/server-profiles/"+encodeURIComponent(profileId) : "/api/server-profiles",{method:profileId ? "PUT" : "POST",body:payload});
  $("#serverProfileId").value=profile.id;
  $("#stressPassword").value="";
  await loadServerWorkspace(false);
  editServerProfile(profile.id);
  return profile;
}
async function connectServerSession(event) {
  if(event) event.preventDefault();
  const button=$("#connectServerButton"); button.disabled=true;
  try {
    const payload=serverProfilePayload();
    if(!payload.name) throw new Error("请填写服务器名称");
    if(!payload.host) throw new Error("请填写服务器 IP / 主机名");
    if(!payload.user) throw new Error("请填写 SSH 用户");
    let profileId=$("#serverProfileId").value;
    const currentPassword=payload.password;
    if($("#serverSaveConfig").checked) {
      const profile=await saveServerProfile();
      profileId=profile.id;
    }
    $("#serverConnectionHint").textContent="正在建立 SSH 会话并执行只读环境检测…";
    const session=await api("/api/server-sessions",{method:"POST",body:profileId ? {profile_id:profileId,password:currentPassword} : {server_name:payload.name,host:payload.host,port:payload.port,user:payload.user,password:currentPassword}});
    state.serverSessionId=session.id;
    state.serverSessionMetricCursor[session.id]=0;
    state.serverSessionMetrics[session.id]=[];
    $("#stressPassword").value="";
    applyServerSession(session);
    await loadServerWorkspace(false);
    closeServerProfileDrawer(true);
    toast("服务器会话已连接，实时监控已启动");
    activateWorkspaceTab("stress","live");
  } catch(error) {
    toast("连接服务器失败："+error.message,true);
  } finally {
    button.disabled=false;
    $("#serverConnectionHint").textContent="连接会执行只读环境检测，并立即启动 CPU、GPU、内存和磁盘采集。";
  }
}
async function connectSavedServer(profileId) {
  const profile=state.serverProfiles.find(function(item){return item.id===profileId;});
  if(!profile) return;
  if(!profile.credential_saved) {
    editServerProfile(profileId);
    window.setTimeout(function(){$("#stressPassword").focus();},120);
    toast("该服务器未保存密码，请输入 SSH 密码后连接");
    return;
  }
  try {
    const session=await api("/api/server-sessions",{method:"POST",body:{profile_id:profileId}});
    state.serverSessionId=session.id;
    state.serverSessionMetricCursor[session.id]=0;
    state.serverSessionMetrics[session.id]=[];
    applyServerSession(session);
    await loadServerWorkspace(false);
    activateWorkspaceTab("stress","live");
    toast("已使用加密凭据连接 "+profile.name);
  } catch(error) {toast("连接服务器失败："+error.message,true);}
}
async function deleteServerProfile(profileId) {
  const profile=state.serverProfiles.find(function(item){return item.id===profileId;});
  if(!profile || !window.confirm("确认删除服务器配置“"+profile.name+"”？")) return;
  try {
    await api("/api/server-profiles/"+encodeURIComponent(profileId),{method:"DELETE"});
    if($("#serverProfileId").value===profileId) resetServerProfileForm(false);
    await loadServerWorkspace(false);
    toast("服务器配置已删除");
  } catch(error) {toast("删除失败："+error.message,true);}
}
function renderServerProfiles() {
  populateRunServerProfiles();
  const sessionsByProfile={};
  state.serverSessions.forEach(function(session){if(session.profile_id)sessionsByProfile[session.profile_id]=session;});
  const query=$("#serverProfileSearch").value.trim().toLowerCase();
  const statusFilter=$("#serverProfileStatusFilter").value;
  const profiles=state.serverProfiles.filter(function(profile){
    const session=sessionsByProfile[profile.id];
    const searchable=[profile.name,profile.host,profile.user].join(" ").toLowerCase();
    return (!query || searchable.includes(query)) && (statusFilter === "all" || (statusFilter === "connected" ? Boolean(session) : !session));
  });
  const connectedCount=Object.keys(sessionsByProfile).length;
  const credentialCount=state.serverProfiles.filter(function(profile){return profile.credential_saved;}).length;
  $("#serverAssetTotal").textContent=String(state.serverProfiles.length);
  $("#serverAssetConnected").textContent=String(connectedCount);
  $("#serverAssetCredential").textContent=String(credentialCount);
  $("#serverAssetAuthorization").textContent=String(state.serverProfiles.length-credentialCount);
  $("#serverProfileCount").textContent=String(profiles.length);
  $("#serverProfileList").innerHTML=profiles.map(function(profile){
    const session=sessionsByProfile[profile.id];
    const lastUsed=profile.last_used_at ? fmtTime(profile.last_used_at) : "尚未连接";
    return '<tr class="'+(session ? "connected" : "")+'"><td><div class="server-cell"><i></i><div><strong>'+esc(profile.name)+'</strong><small>'+esc(profile.host)+':'+esc(profile.port)+'</small></div></div></td><td><div class="server-account"><strong>'+esc(profile.user)+'</strong><small>SSH 用户</small></div></td><td><span class="credential-policy '+(profile.credential_saved ? "saved" : "temporary")+'">'+(profile.credential_saved ? "加密保存" : "每次输入")+'</span></td><td><span class="server-session-state '+(session ? "connected" : "disconnected")+'"><i></i>'+(session ? "实时监控中" : "未连接")+'</span></td><td><time>'+esc(lastUsed)+'</time></td><td><div class="server-table-actions">'+(session ? '<button class="primary" type="button" data-session-open="'+esc(session.id)+'">查看监控</button>' : '<button class="primary" type="button" data-server-connect="'+esc(profile.id)+'">连接</button>')+'<button type="button" data-server-edit="'+esc(profile.id)+'">编辑</button><button class="danger" type="button" data-server-delete="'+esc(profile.id)+'" aria-label="删除 '+esc(profile.name)+'">删除</button></div></td></tr>';
  }).join("");
  const empty=$("#serverProfileEmpty");
  empty.classList.toggle("hidden",profiles.length>0);
  const emptyTitle=empty.querySelector("strong");
  const emptyDescription=empty.querySelector("p");
  if(state.serverProfiles.length && !profiles.length) {
    emptyTitle.textContent="没有匹配的服务器";
    emptyDescription.textContent="请调整搜索条件或状态筛选。";
  } else {
    emptyTitle.textContent="还没有服务器资产";
    emptyDescription.textContent="添加第一台服务器后，可从列表直接连接并查看实时状态。";
  }
}
function populateServerSessionSelect() {
  const options=state.serverSessions.map(function(session){
    return '<option value="'+esc(session.id)+'">'+esc(session.name || session.host)+' · '+esc(session.host)+'</option>';
  }).join("") || '<option value="">暂无活动会话</option>';
  ["serverSessionSelect","stressSessionSelect"].forEach(function(id){$("#"+id).innerHTML=options;});
  syncServerSessionSelectors(state.serverSessionId);
}
function syncServerSessionSelectors(sessionId) {
  ["serverSessionSelect","stressSessionSelect"].forEach(function(id){
    const select=$("#"+id);
    if(select) select.value=sessionId || "";
  });
}
async function loadServerWorkspace(includeDetail) {
  try {
    const results=await Promise.all([api("/api/server-profiles"),api("/api/server-sessions")]);
    state.serverProfiles=results[0].profiles || [];
    state.serverSessions=results[1].sessions || [];
    if(state.serverSessionId && !state.serverSessions.some(function(item){return item.id===state.serverSessionId;})) state.serverSessionId="";
    if(!state.serverSessionId && state.serverSessions[0]) state.serverSessionId=state.serverSessions[0].id;
    renderServerProfiles();
    populateServerSessionSelect();
    if(state.serverSessionId && includeDetail !== false) await refreshServerSession(false);
    else if(!state.serverSessionId) renderEmptyServerSession();
  } catch(error) {console.warn(error);}
}
function renderEmptyServerSession() {
  state.stressCapability=null;
  state.selectedGpuDevices=[];
  $("#serverSessionSummary").innerHTML='<div class="empty-result-note"><strong>暂无活动会话</strong><p>从服务器列表连接，或填写连接信息创建临时会话。</p></div>';
  $("#serverLiveMessage").textContent="选择一个已连接会话查看实时指标。";
  $("#serverLiveBadge").innerHTML='<i></i>未连接';
  $("#serverLiveBadge").className="session-live-badge";
  $("#stopServerSessionButton").disabled=true;
  syncServerSessionSelectors("");
  $("#serverSessionInsight").classList.add("hidden");
  resetStressDiscovery("连接服务器后展示 GPU 与压测能力");
  renderServerSessionMetrics({id:"",status:"closed"});
  renderStressTask(null);
}
function applyServerSession(session) {
  if(state.closingServerSessionId===session.id) return;
  state.serverSessionId=session.id;
  const existingIndex=state.serverSessions.findIndex(function(item){return item.id===session.id;});
  if(existingIndex>=0) state.serverSessions[existingIndex]=Object.assign({},state.serverSessions[existingIndex],session); else state.serverSessions.unshift(session);
  syncServerSessionSelectors(session.id);
  $("#serverLiveMessage").textContent=(session.name || session.host)+" · "+session.user+"@"+session.host+":"+session.port;
  $("#serverLiveBadge").innerHTML='<i></i>SSH 已连接';
  $("#serverLiveBadge").className="session-live-badge connected";
  $("#stopServerSessionButton").disabled=false;
  $("#serverSessionInsight").classList.remove("hidden");
  $("#serverSessionSummary").innerHTML='<div class="session-summary-head"><span class="session-live-badge connected"><i></i>实时采集中</span><strong>'+esc(session.name || session.host)+'</strong></div><dl><div><dt>目标</dt><dd>'+esc(session.user)+'@'+esc(session.host)+':'+esc(session.port)+'</dd></div><div><dt>采样</dt><dd>'+esc(session.sample_count || 0)+' 次</dd></div><div><dt>自动回收</dt><dd>空闲 '+Math.max(1,Math.ceil(Number(session.idle_seconds_remaining || 0)/60))+' 分钟</dd></div></dl><p>压测会复用此 SSH 连接；关闭会话前需先停止正在运行的压测。</p>';
  if(session.capability) renderCapability(session.capability);
  renderServerSessionMetrics(session);
}
async function openServerSession(sessionId, workspaceName) {
  if(!sessionId) return;
  if(state.serverSessionId!==sessionId) {
    state.stressCapability=null;
    state.selectedGpuDevices=[];
    $("#stressGpuDevices").value="";
  }
  state.serverSessionId=sessionId;
  syncServerSessionSelectors(sessionId);
  state.serverSessionMetricCursor[sessionId]=0;
  state.serverSessionMetrics[sessionId]=[];
  await refreshServerSession(true);
  activateWorkspaceTab("stress",workspaceName || "live");
  if(workspaceName === "setup") {
    const session=state.serverSessions.find(function(item){return item.id===sessionId;});
    toast("压测会话已切换为 "+(session ? session.name || session.host : "所选服务器"));
  }
}
async function refreshServerSession(reset) {
  const sessionId=$("#serverSessionSelect").value || state.serverSessionId;
  if(!sessionId) {renderEmptyServerSession();return;}
  if(state.closingServerSessionId===sessionId) return;
  if(reset || state.serverSessionId!==sessionId) {
    state.serverSessionId=sessionId;
    state.serverSessionMetricCursor[sessionId]=0;
    state.serverSessionMetrics[sessionId]=[];
  }
  try {
    const session=await api("/api/server-sessions/"+encodeURIComponent(sessionId)+"/heartbeat",{method:"POST"});
    const after=state.serverSessionMetricCursor[sessionId] || 0;
    const data=await api("/api/server-sessions/"+encodeURIComponent(sessionId)+"/metrics?after_id="+after+"&limit=20000");
    state.serverSessionMetrics[sessionId]=(state.serverSessionMetrics[sessionId] || []).concat(data.metrics || []).slice(-600);
    state.serverSessionMetricCursor[sessionId]=data.last_id || after;
    applyServerSession(session);
  } catch(error) {
    if(error.status===404) {
      state.serverSessionId="";
      await loadServerWorkspace(false);
      toast("服务器会话已关闭或空闲超时",true);
    } else console.warn(error);
  }
}
function renderServerSessionMetrics(session) {
  const metrics=state.serverSessionMetrics[session.id] || [];
  const cpu=values(metrics,"cpu_pct");
  const memory=metrics.map(function(row){const data=row.data || {};const used=Number(data.mem_used_gb);const total=Number(data.mem_total_gb);return total>0 ? used/total*100 : NaN;}).filter(Number.isFinite);
  const disk=values(metrics,"disk_util_pct");
  updateMetric("stressCpuNow",cpu,"%"); updateMetric("stressMemoryNow",memory,"%"); updateMetric("stressDiskNow",disk,"%");
  renderCpuTemperatureReadings(metrics);
  drawChart($("#stressCpuChart"),cpu,"#4fb3ff",100); drawChart($("#stressMemoryChart"),memory,"#8f9dff",100); drawChart($("#stressDiskChart"),disk,"#d28cff",100);
  renderStressGpu({status:session.status === "connected" ? "running" : "interrupted"},metrics);
}
async function stopServerSession() {
  const sessionId=state.serverSessionId;
  if(!sessionId || state.closingServerSessionId===sessionId) return;
  const button=$("#stopServerSessionButton");
  state.closingServerSessionId=sessionId;
  button.disabled=true;
  button.classList.add("is-busy");
  button.textContent="正在关闭…";
  $("#serverLiveBadge").innerHTML='<i></i>正在断开';
  $("#serverLiveBadge").className="session-live-badge closing";
  toast("正在关闭 SSH 会话与实时采集…");
  try {
    await api("/api/server-sessions/"+encodeURIComponent(sessionId)+"/stop",{method:"POST"});
    delete state.serverSessionMetrics[sessionId]; delete state.serverSessionMetricCursor[sessionId];
    state.serverSessionId="";
    await loadServerWorkspace(false);
    toast("SSH 会话与实时采集已关闭");
    activateWorkspaceTab("stress","sessions");
  } catch(error) {
    toast("关闭会话失败："+error.message,true);
    state.closingServerSessionId="";
    await refreshServerSession(false);
  } finally {
    state.closingServerSessionId="";
    button.classList.remove("is-busy");
    button.textContent="关闭连接";
  }
}

function stressJobMatchesCurrentSession(job) {
  if(!job || !state.serverSessionId) return false;
  const session=state.serverSessions.find(function(item){return item.id===state.serverSessionId;});
  if(!session) return false;
  const options=job.options || {};
  if(options.session_id===session.id) return true;
  return Boolean(job.target_host) && job.target_host===session.host;
}
function isActiveStressJob(job) {
  return Boolean(job) && ["authorized","queued","running"].includes(job.status);
}
function selectStressJobForCurrentSession() {
  const current=state.stressJobs.find(function(job){return job.id===state.stressJobId;});
  if(stressJobMatchesCurrentSession(current)) return current;
  const matching=state.stressJobs.filter(stressJobMatchesCurrentSession);
  const selected=matching.find(isActiveStressJob) || matching[0] || null;
  state.stressJobId=selected ? selected.id : "";
  const select=$("#stressJobSelect");
  if(select) select.value=state.stressJobId;
  return selected;
}
function fmtStressSeconds(value) {
  const total=Math.max(0,Math.round(Number(value) || 0));
  const hours=Math.floor(total/3600);
  const minutes=Math.floor((total%3600)/60);
  const seconds=total%60;
  if(hours) return hours+" 小时 "+minutes+" 分";
  if(minutes) return minutes+" 分 "+seconds+" 秒";
  return seconds+" 秒";
}
function stressTaskRuntime(job) {
  const options=(job && job.options) || {};
  const duration=Math.max(1,Number(options.duration) || 60);
  const started=Number(job && job.started_at);
  const finished=Number(job && job.finished_at);
  const end=Number.isFinite(finished) && finished>0 ? finished : Date.now()/1000;
  const elapsed=Number.isFinite(started) && started>0 ? Math.max(0,end-started) : 0;
  let progress=0;
  if(job && job.status==="running") progress=Math.min(99,Math.round(elapsed/duration*100));
  else if(job && job.status==="succeeded") progress=100;
  else if(job && ["failed","interrupted"].includes(job.status) && elapsed) progress=Math.min(100,Math.round(elapsed/duration*100));
  return {duration:duration,elapsed:elapsed,progress:progress};
}
function renderStressTask(job) {
  const panel=$("#stressLiveTaskPanel");
  const stopButton=$("#stressLiveStopButton");
  if(!job) {
    const session=state.serverSessions.find(function(item){return item.id===state.serverSessionId;});
    panel.dataset.status="idle";
    $("#stressLiveTaskTitle").textContent="当前没有压测任务";
    $("#stressLiveTaskMessage").textContent="实时监控仍在采集服务器状态；启动压测后将在这里显示执行状态。";
    $("#stressLiveTaskStatus").className="status stress-idle";
    $("#stressLiveTaskStatus").textContent="待启动";
    $("#stressLiveTaskPlan").textContent="—";
    $("#stressLiveTaskTarget").textContent=session ? session.name || session.host : "—";
    $("#stressLiveTaskTiming").textContent="—";
    $("#stressLiveTaskProgressValue").textContent="等待启动";
    $("#stressLiveTaskProgressBar").style.width="0%";
    stopButton.classList.add("hidden");
    stopButton.disabled=true;
    return;
  }
  const options=job.options || {};
  const preset=stressPresetNames[options.test_preset] || "自定义";
  const modes=(job.modes || options.modes || []).map(function(mode){return stressModeNames[mode] || mode;}).join(" + ") || "监控";
  const runtime=stressTaskRuntime(job);
  const pending=["authorized","queued"].includes(job.status);
  const active=pending || job.status==="running";
  let timing="等待执行器领取";
  if(job.status==="running") timing=fmtStressSeconds(runtime.elapsed)+" / "+fmtStressSeconds(runtime.duration);
  else if(job.started_at) timing=fmtStressSeconds(runtime.elapsed)+"（计划 "+fmtStressSeconds(runtime.duration)+"）";
  panel.dataset.status=job.status || "idle";
  $("#stressLiveTaskTitle").textContent=(job.target_name || job.target_host || "未命名服务器")+" · "+preset;
  $("#stressLiveTaskMessage").textContent=job.message || job.error || "正在等待任务状态";
  $("#stressLiveTaskStatus").className="status "+(job.status || "stress-idle");
  $("#stressLiveTaskStatus").textContent=statusNames[job.status] || job.status || "未知";
  $("#stressLiveTaskPlan").textContent=preset+" · "+modes;
  $("#stressLiveTaskTarget").textContent=job.target_name || job.target_host || "—";
  $("#stressLiveTaskTiming").textContent=timing;
  $("#stressLiveTaskProgressValue").textContent=pending ? "等待执行" : (job.status==="running" ? runtime.progress+"%" : (job.status==="succeeded" ? "100%" : "已结束"));
  $("#stressLiveTaskProgressBar").style.width=runtime.progress+"%";
  stopButton.classList.toggle("hidden",!active);
  stopButton.disabled=Boolean(job.stop_requested);
  stopButton.textContent=job.stop_requested ? "正在停止…" : (pending ? "取消任务" : "安全停止");
}
async function refreshStressLiveWorkspace(reset) {
  await refreshServerSession(reset);
  await loadStressJobs();
  const job=selectStressJobForCurrentSession();
  if(isActiveStressJob(job)) await refreshStress(reset);
  else if(job) renderStressTask(job);
  else renderStressTask(null);
}

async function loadStressJobs() {
  try {
    const data = await api("/api/stress-jobs?limit=100");
    state.stressJobs = data.jobs || [];
    if (state.stressJobId && !state.stressJobs.some(function(job){return job.id===state.stressJobId;})) state.stressJobId="";
    if (!state.stressJobId && state.stressJobs[0]) {
      const matching=state.stressJobs.filter(stressJobMatchesCurrentSession);
      const preferred=matching.find(isActiveStressJob) || matching[0] || state.stressJobs[0];
      state.stressJobId=preferred.id;
    }
    populateStressSelect();
    populateStressReportSelect();
    renderStressJobsTable();
  } catch (error) { console.warn(error); }
}
function populateStressSelect() {
  $("#stressJobSelect").innerHTML = state.stressJobs.map(function(job){
    const name=job.target_name || job.target_host || "未命名服务器";
    return '<option value="' + esc(job.id) + '">' + esc(name) + " · " + esc(statusNames[job.status] || job.status) + " · " + fmtTime(job.created_at) + "</option>";
  }).join("") || '<option value="">暂无压测任务</option>';
  if (state.stressJobId) $("#stressJobSelect").value = state.stressJobId;
}
function populateStressReportSelect() {
  const reportJobs=state.stressJobs.filter(function(job){return Boolean(job.report_path);});
  if(state.stressReportJobId && !reportJobs.some(function(job){return job.id===state.stressReportJobId;})) state.stressReportJobId="";
  if(!state.stressReportJobId && reportJobs[0]) state.stressReportJobId=reportJobs[0].id;
  $("#stressReportJobSelect").innerHTML=reportJobs.map(function(job){
    return '<option value="'+esc(job.id)+'">'+esc(job.target_name || job.target_host || "未命名服务器")+' · '+fmtTime(job.created_at)+' · '+shortId(job.id)+'</option>';
  }).join("") || '<option value="">暂无可下载报告</option>';
  $("#stressReportJobSelect").value=state.stressReportJobId;
  $("#downloadStressDocxButton").disabled=!state.stressReportJobId;
  $("#downloadStressPdfButton").disabled=!state.stressReportJobId;
}
function renderStressJobsTable() {
  $("#stressJobsBody").innerHTML = state.stressJobs.map(function(job){
    const message=String(job.message || job.error || "");
    const credentialIssue=/凭据|重新授权|SSH/.test(message);
    const pending=["authorized","queued"].includes(job.status);
    const taskAction = ["authorized","queued","running"].includes(job.status)
      ? '<button type="button" class="text-button danger stress-stop" data-id="' + esc(job.id) + '">' + (pending ? "取消" : "停止") + '</button>'
      : ["interrupted","failed"].includes(job.status)
      ? '<button type="button" class="text-button stress-retry" data-id="' + esc(job.id) + '">' + (credentialIssue ? "重新配置授权" : "重新提交") + '</button>'
      : "—";
    const reportAction=job.report_path ? '<button type="button" class="text-button stress-download" data-id="'+esc(job.id)+'" data-format="docx">DOCX</button><button type="button" class="text-button stress-download" data-id="'+esc(job.id)+'" data-format="pdf">PDF</button>' : '';
    const action='<div class="stress-row-actions">'+taskAction+reportAction+'</div>';
    const options=job.options || {};
    const plan=stressPresetNames[options.test_preset] || "自定义";
    return '<tr><td class="target-cell"><strong>'+esc(job.target_name || job.target_host || "未命名服务器")+'</strong><small>'+esc(job.target_host || "—")+' · '+shortId(job.id)+'</small></td><td class="plan-cell"><b>'+esc(plan)+'</b><small>'+esc((job.modes || []).map(function(mode){return stressModeNames[mode] || mode;}).join(" + "))+'</small></td><td>'+statusPill(job.status)+'</td><td>'+fmtTime(job.started_at || job.created_at)+'</td><td>'+esc(job.message || job.error || "—")+'</td><td>'+action+'</td></tr>';
  }).join("") || '<tr><td colspan="6">暂无服务器性能测试记录</td></tr>';
}
function retryStressJob(jobId) {
  const job=state.stressJobs.find(function(item){return item.id === jobId;});
  if(!job) { toast("没有找到原任务配置",true); return; }
  state.stressJobId=jobId;
  const options=job.options || {};
  applyStressPreset(options.test_preset in stressPresets ? options.test_preset : "custom");
  const matchingProfile=state.serverProfiles.find(function(profile){return profile.host===(options.host || job.target_host) && profile.user===(options.user || "") && Number(profile.port || 22)===Number(options.port || 22);});
  if(matchingProfile) editServerProfile(matchingProfile.id); else {
    resetServerProfileForm();
    $("#stressServerName").value=options.server_name || job.target_name || "";
    $("#stressHost").value=options.host || job.target_host || "";
    $("#stressPort").value=String(options.port || 22);
    $("#stressUser").value=options.user || "";
  }
  $("#stressPassword").value="";
  $("#stressModules").value=(job.modules || options.modules || []).join(",");
  $("#stressGpuSource").value=options.gpu_burn_source || "";
  $("#stressDuration").value=String(options.duration || 60);
  $("#stressWorkers").value=String(options.workers == null ? 0 : options.workers);
  $("#stressCpuLoad").value=String(options.cpu_load || 80);
  $("#stressGpuMemoryPercent").value=String(options.gpu_memory_percent || 90);
  $("#stressSafetyEnabled").checked=options.safety_enabled !== false;
  $("#stressCpuTempLimit").value=String(options.cpu_temp_limit || 90);
  $("#stressGpuTempLimit").value=String(options.gpu_temp_limit || 85);
  $("#stressMemoryLimit").value=String(options.memory_usage_limit || 95);
  $("#stressDiskLimit").value=String(options.disk_usage_limit || 95);
  const modes=job.modes || options.modes || ["monitor"];
  $$('input[name="stressMode"]').forEach(function(input){input.checked=modes.includes(input.value);});
  const metrics=options.metric_dimensions || ["cpu","gpu","memory","disk"];
  $$('input[name="stressMetric"]').forEach(function(input){input.checked=metrics.includes(input.value);});
  state.serverSessionId="";
  resetStressDiscovery("已带入原任务配置，请重新连接服务器会话");
  gotoView("stress");
  activateWorkspaceTab("stress","sessions");
  $("#stressStepResources").open=false;
  $("#stressStepPlan").open=false;
  setTimeout(function(){const password=$("#stressPassword");password.focus();password.scrollIntoView({block:"center",behavior:"smooth"});},100);
  toast("原任务配置已恢复，请重新连接服务器会话");
}
function stressPayload() {
  const session=state.serverSessions.find(function(item){return item.id===state.serverSessionId;}) || {};
  return {
    target:"server",
    session_id:state.serverSessionId,
    server_name:session.name || "",
    host:session.host || "",
    port:Number(session.port || 22),
    user:session.user || "",
    password:"",
    modes:$$('input[name="stressMode"]:checked').map(function(input){ return input.value; }),
    modules:$("#stressModules").value.split(",").map(function(item){ return item.trim(); }).filter(Boolean),
    duration:Number($("#stressDuration").value || 60),
    workers:Number($("#stressWorkers").value || 0),
    cpu_load:Number($("#stressCpuLoad").value || 80),
    gpu_devices:state.selectedGpuDevices.join(","),
    gpu_burn_source:$("#stressGpuSource").value.trim(),
    test_preset:state.stressPreset,
    gpu_memory_percent:Number($("#stressGpuMemoryPercent").value || 90),
    safety_enabled:$("#stressSafetyEnabled").checked,
    cpu_temp_limit:Number($("#stressCpuTempLimit").value || 90),
    gpu_temp_limit:Number($("#stressGpuTempLimit").value || 85),
    memory_usage_limit:Number($("#stressMemoryLimit").value || 95),
    disk_usage_limit:Number($("#stressDiskLimit").value || 95),
    metric_dimensions:$$('input[name="stressMetric"]:checked').map(function(input){return input.value;})
  };
}
function renderCapability(report) {
  state.stressCapability=report;
  const box = $("#capabilityResult");
  const labels = {ready:"可直接执行",degraded:"可降级执行",blocked:"暂不可执行"};
  const modeLabels = {monitor:"在线体检",cpu:"CPU 压测",gpu:"GPU 满载测试"};
  const host = report.host || {}; const gpu = report.gpu || {}; const caps = report.capabilities || {};
  const devices=gpu.devices || [];
  const strategy=gpu.test_strategy || {
    recommended_preset:devices.some(function(device){return device.stress_selectable || device.selectable;}) ? "quick" : "health",
    detected_count:devices.length,
    full_stress_count:devices.filter(function(device){return device.stress_selectable || device.selectable;}).length,
    online_health_count:devices.length,
    maintenance_required:Boolean(devices.length) && !devices.some(function(device){return device.stress_selectable || device.selectable;}),
    message:"平台已根据当前占用生成安全建议"
  };
  if(!devices.length) {
    applyStressPreset(state.stressPreset === "custom" ? "custom" : state.stressPreset);
    $$('input[name="stressMode"]').forEach(function(input){input.checked=input.value === "monitor" || input.value === "cpu";});
    $("#stressPresetHint").textContent="未检测到 NVIDIA GPU，将执行 CPU 负载并监控内存与磁盘。";
  } else if(strategy.recommended_preset === "health") {
    applyStressPreset(state.stressPreset === "custom" ? "custom" : state.stressPreset);
    $$('input[name="stressMode"]').forEach(function(input){
      input.checked=input.value === "monitor" || (input.value === "cpu" && (!caps.cpu || caps.cpu.status !== "blocked"));
    });
    $("#stressPresetHint").textContent="GPU 当前承载业务，实时监控保持可用；GPU 满载压测已禁止选择。";
  }
  const requested = (report.requested_modes || Object.keys(caps)).filter(function(mode){return caps[mode];});
  const rows = requested.map(function(mode){
    const item=caps[mode]; const needs=(item.prerequisites || []).join("；");
    return "<li><b>"+esc(modeLabels[mode] || mode.toUpperCase())+"：</b>"+esc(labels[item.status] || item.status)+"，"+esc(item.message || "")+(needs ? "<br><span>处理建议："+esc(needs)+"</span>" : "")+"</li>";
  }).join("");
  const warnings = (report.warnings || []).map(function(item){return "<li><b>提示：</b>"+esc(item)+"</li>";}).join("");
  const support = report.support || {}; const runtime = report.runtime || {};
  const memoryGiB = host.memory_kb ? (host.memory_kb / 1024 / 1024).toFixed(1) : "未知";
  const tmpMiB = host.tmp_available_kb ? Math.round(host.tmp_available_kb / 1024) : "未知";
  const cpuEngine=(report.cpu || {}).stress_engine || "仅监控";
  const diskReady=Boolean((report.tools || {}).iostat);
  $("#resourceCpuOverview").textContent=(host.cpu_logical || "未知")+" 线程 · "+cpuEngine;
  $("#resourceGpuOverview").textContent=(gpu.count || 0)+" 张"+(gpu.name ? " · "+gpu.name : "");
  $("#resourceMemoryOverview").textContent=memoryGiB+" GiB";
  $("#resourceDiskOverview").textContent=tmpMiB+" MiB 可用 · "+(diskReady ? "I/O 可采集" : "基础采集");
  const resultTone=strategy.recommended_preset === "health" && devices.length
    ? "attention"
    : (!devices.length || Number(strategy.full_stress_count || 0) > 0 ? "ready" : (report.overall || "ready"));
  const summaryTitle="环境检测完成 · "+(host.hostname || host.os_pretty_name || "目标服务器");
  box.className = "capability-result " + resultTone;
  box.innerHTML = '<div class="result-summary"><span class="result-icon">'+(resultTone === "attention" ? "!" : "✓")+'</span><div><h4>'+esc(summaryTitle)+'</h4><p>已完成 CPU、GPU、内存与磁盘能力检查。'+esc(strategy.message || "")+'</p></div></div>'+ 
    '<div class="result-facts"><span>'+esc(host.os_pretty_name || host.os_id || "未知系统")+'</span><span>'+esc(support.label || "待验证")+'</span><span>安全阈值可用</span></div>'+ 
    '<div class="result-readable-grid"><div><b>CPU</b><span>'+esc(String(host.cpu_logical || "未知"))+' 线程</span></div><div><b>GPU</b><span>'+esc(String(gpu.count || 0))+' 张 / '+esc(String(strategy.full_stress_count || 0))+' 张可满载</span></div><div><b>内存</b><span>'+esc(memoryGiB)+' GiB</span></div><div><b>磁盘</b><span>'+esc(String(tmpMiB))+' MiB 临时空间</span></div></div>'+ 
    '<details class="result-details"><summary>技术详情与兼容性依据</summary><p>账号：'+esc(host.user || "未知")+(host.is_root ? "（root）" : "（普通用户）")+'；临时目录：'+(runtime.temporary_directory_writable ? "可写" : "不可写")+'</p>'+ 
    '<p>CPU 引擎：'+esc((report.cpu || {}).stress_engine || "none")+'；GPU 引擎：'+esc(gpu.stress_engine || "none")+'；计算能力：'+esc(Object.values(gpu.selected_compute_capabilities || gpu.compute_capabilities || {}).join(",") || "未知")+'；镜像模式：'+esc(gpu.container_compatibility_mode || "none")+'；DCGM：'+(gpu.dcgm && gpu.dcgm.installed ? "已安装" : "未安装（不影响测试）")+'</p><ul>'+rows+warnings+'</ul></details>';
  const banner=$("#stressStrategyBanner");
  banner.className="stress-strategy-banner "+(strategy.recommended_preset === "health" ? "health" : "ready");
  banner.innerHTML='<strong>'+(strategy.recommended_preset === "health" ? "实时监控可用 · GPU 压测受限" : "推荐：快速验证")+'</strong><span>'+esc(strategy.message || "")+'</span>'+
    (strategy.maintenance_required ? '<small>如需满载测试，请在维护窗口腾退业务；平台不会自动停止容器。</small>' : '');
  renderGpuDevices(devices);
  updateStressStartState();
}
async function submitStress(event) {
  event.preventDefault();
  const button = $("#stressSubmitButton");
  button.disabled = true;
  try {
    const payload = stressPayload();
    if (!payload.session_id) throw new Error("请先连接服务器会话");
    if (!payload.modes.length) throw new Error("至少选择一种压测模式");
    if (!payload.modes.some(function(mode){return mode === "cpu" || mode === "gpu";})) throw new Error("请至少选择一种 CPU 或 GPU 负载");
    if (!state.stressCapability) throw new Error("当前会话尚未完成环境检测");
    if (payload.modes.includes("gpu") && !state.selectedGpuDevices.length) throw new Error("请选择至少一张可用 GPU");
    const job = await api("/api/stress-jobs", {method:"POST", body:payload});
    state.stressJobId = job.id;
    state.stressMetricCursor[job.id] = 0;
    state.stressMetrics[job.id] = [];
    toast("会话内压测已进入队列：" + shortId(job.id));
    await loadStressJobs();
    await refreshServerSession(false);
    renderStressTask(job);
    activateWorkspaceTab("stress","live");
  } catch (error) {
    toast("压测启动失败：" + error.message, true);
  } finally {
    button.disabled = false;
  }
}
async function stopStressJob(jobId) {
  const liveButton=$("#stressLiveStopButton");
  if(liveButton && state.stressJobId===jobId) {
    liveButton.disabled=true;
    liveButton.classList.add("is-busy");
    liveButton.textContent="正在停止…";
  }
  try {
    const job = await api("/api/stress-jobs/" + encodeURIComponent(jobId) + "/stop", {method:"POST"});
    toast(job.status === "interrupted" ? "压测任务已取消" : "已请求安全停止压测任务");
    await loadStressJobs();
    if (state.stressJobId === jobId) await refreshStress(true);
  } catch (error) {
    if(liveButton) {
      liveButton.disabled=false;
      liveButton.textContent="安全停止";
    }
    toast("停止压测失败：" + error.message, true);
  }
  finally {
    if(liveButton) liveButton.classList.remove("is-busy");
  }
}
async function refreshStress(reset) {
  const jobId = $("#stressJobSelect").value || state.stressJobId;
  if (!jobId) return;
  if (state.stressJobId !== jobId || reset) {
    state.stressJobId = jobId;
    if (reset && !state.stressMetrics[jobId]) state.stressMetricCursor[jobId] = 0;
  }
  try {
    const job = await api("/api/stress-jobs/" + encodeURIComponent(jobId));
    const after = state.stressMetricCursor[jobId] || 0;
    const data = await api("/api/stress-jobs/" + encodeURIComponent(jobId) + "/metrics?after_id=" + after + "&limit=20000");
    state.stressMetrics[jobId] = (state.stressMetrics[jobId] || []).concat(data.metrics || []).slice(-600);
    state.stressMetricCursor[jobId] = data.last_id || after;
    renderStress(job);
    await loadStressJobs();
  } catch (error) {
    if (error.status !== 404) console.warn(error);
  }
}
function renderStress(job) {
  const metrics = state.stressMetrics[job.id] || [];
  renderStressTask(job);
  renderCpuTemperatureReadings(metrics);
  if(!metrics.length) {
    updateMetric("stressCpuNow", [], "%"); updateMetric("stressMemoryNow", [], "%"); updateMetric("stressDiskNow", [], "%");
    return;
  }
  const cpu = values(metrics, "cpu_pct");
  const memory = metrics.map(function(row){const data=row.data || {};const used=Number(data.mem_used_gb);const total=Number(data.mem_total_gb);return total>0 ? used/total*100 : NaN;}).filter(Number.isFinite);
  const disk = values(metrics, "disk_util_pct");
  updateMetric("stressCpuNow", cpu, "%");
  updateMetric("stressMemoryNow", memory, "%");
  updateMetric("stressDiskNow", disk, "%");
  drawChart($("#stressCpuChart"), cpu, "#4fb3ff", 100);
  drawChart($("#stressMemoryChart"), memory, "#8f9dff", 100);
  drawChart($("#stressDiskChart"), disk, "#d28cff", 100);
  renderStressGpu(job, metrics);
}
async function downloadStressReport(format, selectedJobId) {
  const jobId = selectedJobId || $("#stressReportJobSelect").value || state.stressReportJobId;
  if (!jobId) { toast("请选择压测任务", true); return; }
  format = format === "pdf" ? "pdf" : "docx";
  try {
    const response = await fetch("/api/stress-jobs/" + encodeURIComponent(jobId) + "/download/" + format,{headers:{"X-Project-ID":state.projectId}});
    if (!response.ok) { const data=await response.json(); throw new Error(data.detail || "报告未生成"); }
    const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement("a");
    link.href = url; link.download = "server_performance_report." + format; document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  } catch (error) { toast("压测报告下载失败：" + error.message, true); }
}
async function loadReports() {
  try {
    const data = await api("/api/reports?limit=100");
    state.reports = data.reports || [];
    $("#statReports").textContent = String(state.reports.filter(function(x){ return x.status === "succeeded"; }).length);
    $("#reportJobsBody").innerHTML = state.reports.map(function(job){
      const files = job.status === "succeeded" ? '<button class="text-button report-download" data-id="' + job.id + '" data-format="docx">DOCX</button><button class="text-button report-download" data-id="' + job.id + '" data-format="pdf">PDF</button>' : esc(job.message || "—");
      return "<tr><td class=\"mono\">" + shortId(job.id) + "</td><td class=\"mono\">" + shortId(job.run_id) + "</td><td>v" + job.template_version + "</td><td>" + statusPill(job.status) + "</td><td>" + job.attempts + "/" + job.max_attempts + "</td><td>" + files + "</td></tr>";
    }).join("") || '<tr><td colspan="6">暂无报告任务</td></tr>';
  } catch (error) { console.warn(error); }
}
async function loadTemplate() {
  try {
    state.template = await api("/api/report-template");
    $("#templateName").value = state.template.name;
    renderTemplate();
  } catch (error) { console.warn(error); }
}
function renderTemplate() {
  const sections = (state.template && state.template.sections) || [];
  $("#templateSections").innerHTML = sections.map(function(section){
    return '<div class="section-card" data-key="' + esc(section.key) + '"><div class="section-card-head"><input type="text" class="section-title" value="' + esc(section.title) + '"><select class="section-mode"><option value="auto"' + (section.mode==="auto"?" selected":"") + '>自动</option><option value="mixed"' + (section.mode==="mixed"?" selected":"") + '>固定+自定义</option><option value="custom"' + (section.mode==="custom"?" selected":"") + '>自定义</option></select><input type="checkbox" class="section-enabled"' + (section.enabled ? " checked" : "") + '></div><textarea class="section-fixed" aria-label="固定表达">' + esc(section.fixed_text || "") + "</textarea></div>";
  }).join("");
}
async function saveTemplate() {
  const sections = $$(".section-card").map(function(card){
    return {key:card.dataset.key,title:$(".section-title",card).value.trim(),mode:$(".section-mode",card).value,enabled:$(".section-enabled",card).checked,fixed_text:$(".section-fixed",card).value.trim()};
  });
  try {
    state.template = await api("/api/report-template",{method:"PUT",body:{name:$("#templateName").value.trim(),sections:sections}});
    renderTemplate(); toast("报告模板已保存为 v" + state.template.version);
  } catch (error) { toast("模板保存失败：" + error.message,true); }
}
async function submitReport(event) {
  event.preventDefault();
  const runId = $("#reportRunSelect").value;
  if (!runId) { toast("请选择已产生运行目录的任务",true); return; }
  const payload = {
    title:$("#reportTitle").value.trim(), prepared_by:$("#reportPreparedBy").value.trim(),
    environment_notes:$("#reportEnvironment").value.trim(),
    custom_sections:{overview:$("#reportOverview").value.trim()},
    conclusion:$("#reportConclusion").value.trim(),
    use_model_conclusion:$("#useModelConclusion").checked
  };
  try {
    const job = await api("/api/runs/" + encodeURIComponent(runId) + "/reports",{method:"POST",body:payload});
    toast("报告任务已提交：" + shortId(job.id)); await loadReports();
  } catch (error) { toast("报告提交失败：" + error.message,true); }
}
async function downloadReport(jobId, format) {
  try {
    const response = await fetch("/api/reports/" + encodeURIComponent(jobId) + "/download/" + format,{headers:{"X-Project-ID":state.projectId}});
    if (!response.ok) { const data=await response.json(); throw new Error(data.detail||"下载失败"); }
    const blob = await response.blob(); const url = URL.createObjectURL(blob); const link=document.createElement("a");
    link.href=url; link.download="xiaoyi_enterprise_test_report."+format; document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  } catch (error) { toast("下载失败：" + error.message,true); }
}
const evaluationKindLabels={mock:"Mock 流程验证",mock_full:"完整进阶流程验证",foundation:"基础能力与鲁棒性",translation:"中英双向翻译",wmt_translation:"WMT2024++ 标准翻译",report_writing:"报告写作能力",intelligence:"情报生产能力",standard_benchmark:"标准 Benchmark",concurrency:"并发阶梯",deep_performance:"深度性能与容量",custom:"项目自定义测试集"};
const evaluationPlanLabels={quick:"快速体检",standard:"标准评测",deep:"深度评测"};
const evaluationPhaseLabels={queued:"排队",preparing:"准备",running:"执行",scoring:"规则评分",reporting:"报告",completed:"完成",failed:"失败",stopped:"已停止",stop_requested:"安全停止中"};
function evaluationActive(run){return Boolean(run&&["queued","preparing","running","scoring","reporting","stop_requested"].includes(run.status));}
function evaluationTerminal(run){return Boolean(run&&["completed","failed","stopped"].includes(run.status));}
function evaluationSnapshot(run){return run&&run.snapshot&&typeof run.snapshot==="object"?run.snapshot:{};}
function evaluationNumber(value,digits){const number=Number(value);return Number.isFinite(number)?number.toFixed(digits==null?1:digits):"—";}
function evaluationRunLabel(run){
  const numeric=Number(run&&run.created_at);const date=Number.isFinite(numeric)?new Date(numeric<1e12?numeric*1000:numeric):null;
  if(!date||Number.isNaN(date.getTime()))return "评测任务";
  const pad=function(value){return String(value).padStart(2,"0");};
  return "评测任务 "+date.getFullYear()+pad(date.getMonth()+1)+pad(date.getDate())+"-"+pad(date.getHours())+pad(date.getMinutes())+pad(date.getSeconds());
}
function evaluationCaseInfo(run,caseId,index){
  const task=evaluationSnapshot(run).task_config||{};const cases=Array.isArray(task.cases)?task.cases:[];
  const matched=cases.find(function(item){return String(item.id||item.case_id||item.case_key||"")===String(caseId||"");})||{};
  const payload=matched.payload||{};const key=matched.case_key||matched.category||"";
  const name=payload.name||payload.question||matched.name||("用例 "+String(index+1));
  return {name:name,description:key||String(matched.category||"")};
}
function showEvaluationWorkspace(name){
  activateWorkspaceTab("evaluation",name);
  if(name==="detail"||name==="report")requestAnimationFrame(function(){const tabs=$("#evaluationWorkspaceTabs");if(tabs)tabs.scrollIntoView({block:"start",behavior:"smooth"});});
}
function evaluationKindInfo(kind){
  const catalog=(state.evaluationBootstrap&&state.evaluationBootstrap.run_kinds)||[];
  return catalog.find(function(item){return item.id===kind;})||{id:kind,label:evaluationKindLabels[kind]||kind,description:""};
}
function syncEvaluationForm(){
  const kind=$("#evaluationRunKind").value;
  const info=evaluationKindInfo(kind);
  const mock=["mock","mock_full"].includes(kind);
  const requiresEvalScope=["standard_benchmark","wmt_translation","concurrency","deep_performance"].includes(kind);
  const runtime=(state.evaluationBootstrap&&state.evaluationBootstrap.runtime)||{};
  $("#evaluationRunKindHint").textContent=info.description||"选择评测范围。";
  $("#evaluationModelProfile").disabled=mock;
  if(mock) $("#evaluationModelProfile").value="";
  $("#evaluationCustomSuiteField").classList.toggle("hidden",kind!=="custom");
  $("#evaluationSubmitNote").textContent=mock?"Mock 运行不产生真实模型负载，可验收完整进阶流程。":requiresEvalScope?(runtime.evalscope_configured?"将使用锁定的 EvalScope "+runtime.evalscope_version+" 隔离运行；深度方案会记录固定 RPS、突发、持续和恢复观察配置。":"尚未配置 EvalScope Python 3.11 运行时，WMT、标准和性能评测暂不可提交。"):("专项能力逐条受控调用；规则、裁判和人工复核状态分开保存。最大输出 "+$("#evaluationMaxTokens").value+" Tokens。");
  const canOperate=hasPermission("evaluation:operate");
  const missingCustom=kind==="custom"&&!$("#evaluationSuiteVersion").value;
  $("#evaluationSubmitButton").disabled=!canOperate||(requiresEvalScope&&!runtime.evalscope_configured)||missingCustom;
  if(!canOperate) $("#evaluationSubmitNote").textContent="当前项目角色可以查看评测结果，但不能发起或停止评测。";
}
function populateEvaluationModels(){
  const models=(state.evaluationBootstrap&&state.evaluationBootstrap.profiles)||[];
  const current=$("#evaluationModelProfile").value;
  $("#evaluationModelProfile").innerHTML='<option value="">请选择模型配置</option>'+models.map(function(model){return '<option value="'+esc(model.id)+'">'+esc(model.name+" · "+model.model_name)+(model.has_api_key?"":" · 无鉴权")+'</option>';}).join("");
  if(models.some(function(model){return model.id===current;})) $("#evaluationModelProfile").value=current;
  else {const active=models.find(function(model){return model.is_active;});if(active)$("#evaluationModelProfile").value=active.id;}
  const judgeCurrent=$("#evaluationJudgeProfile").value;
  $("#evaluationJudgeProfile").innerHTML='<option value="">不使用裁判模型</option>'+models.map(function(model){return '<option value="'+esc(model.id)+'">'+esc(model.name+" · "+model.model_name)+'</option>';}).join("");
  if(models.some(function(model){return model.id===judgeCurrent;}))$("#evaluationJudgeProfile").value=judgeCurrent;
  const sessions=(state.evaluationBootstrap&&state.evaluationBootstrap.server_sessions)||[];
  $("#evaluationServerSession").innerHTML='<option value="">不关联服务器资源</option>'+sessions.map(function(session){return '<option value="'+esc(session.id)+'">'+esc(session.name||session.host||"活动服务器")+'</option>';}).join("");
}
function populateEvaluationSuites(){
  const suites=state.evaluationSuites||[];
  const current=$("#evaluationSuiteVersion").value;
  const projectSuites=suites.filter(function(item){return item.project_id&&item.latest_version;});
  $("#evaluationSuiteVersion").innerHTML='<option value="">请选择已发布的项目测试集</option>'+projectSuites.map(function(item){return '<option value="'+esc(item.latest_version.id)+'">'+esc(item.name+" · v"+item.latest_version.version+" · "+item.latest_version.case_count+" 条")+'</option>';}).join("");
  if(projectSuites.some(function(item){return item.latest_version.id===current;}))$("#evaluationSuiteVersion").value=current;
}
function evaluationSuiteSourceLabel(suite){return suite.project_id?"项目测试集":((suite.source==="evalscope_standard")?"EvalScope 标准集":"平台内置集");}
function renderEvaluationSuites(){
  const suites=state.evaluationSuites||[];
  $("#evaluationSuiteList").innerHTML=suites.map(function(suite){
    const latest=suite.latest_version||{};const isProject=Boolean(suite.project_id);
    const meta=latest.id?("v"+latest.version+" · "+latest.case_count+" 条 · "+shortId(latest.content_sha256)):"尚未发布版本";
    const actions=isProject?(hasPermission("evaluation:manage")?'<label class="evaluation-suite-upload"><input type="file" data-evaluation-suite-file accept=".json,.jsonl,.csv,.zip,.txt,.md,.docx,.pdf"><span>选择素材</span></label><button class="button secondary" type="button" data-evaluation-suite-import="'+esc(suite.id)+'">校验并发布新版本</button>':'<span class="muted">仅管理员可发布版本</span>'):(hasPermission("evaluation:manage")?'<button class="button secondary" type="button" data-evaluation-suite-clone="'+esc(suite.id)+'">复制到当前项目</button>':'');
    return '<article class="evaluation-suite-card"><header><div><span>'+esc(evaluationSuiteSourceLabel(suite))+'</span><strong>'+esc(suite.name)+'</strong></div>'+statusPill(suite.status)+'</header><p>'+esc(suite.description||"暂无说明")+'</p><dl><div><dt>能力分类</dt><dd>'+esc(evaluationKindLabels[suite.category]||suite.category||"通用")+'</dd></div><div><dt>最新版本</dt><dd>'+esc(meta)+'</dd></div></dl><footer>'+actions+'</footer></article>';
  }).join("")||'<div class="evaluation-empty-state"><strong>暂无测试集</strong><p>创建项目测试集并导入已授权、已脱敏的素材。</p></div>';
}
async function refreshEvaluationSuites(){
  try{const data=await api("/api/model-evaluation/suites");state.evaluationSuites=data.suites||[];populateEvaluationSuites();renderEvaluationSuites();renderEvaluationRuns();syncEvaluationForm();}catch(error){toast("测试集加载失败："+error.message,true);}
}
async function createEvaluationSuite(event){
  event.preventDefault();const errorNode=$("#evaluationSuiteFormError");errorNode.textContent="";
  try{await api("/api/model-evaluation/suites",{method:"POST",body:{name:$("#evaluationSuiteName").value.trim(),category:$("#evaluationSuiteCategory").value,description:$("#evaluationSuiteDescription").value.trim()}});event.currentTarget.reset();toast("项目测试集已创建，请选择素材发布首个版本");await refreshEvaluationSuites();}catch(error){errorNode.textContent=error.message;}
}
async function cloneEvaluationSuite(suiteId){
  const suite=state.evaluationSuites.find(function(item){return item.id===suiteId;});if(!suite)return;
  const name=window.prompt("复制后的项目测试集名称",suite.name+"（项目副本）");if(name===null)return;
  try{await api("/api/model-evaluation/suites/"+encodeURIComponent(suiteId)+"/clone",{method:"POST",body:{name:name.trim()}});toast("已复制为当前项目的可编辑测试集");await refreshEvaluationSuites();}catch(error){toast("复制测试集失败："+error.message,true);}
}
async function importEvaluationSuite(suiteId,button){
  const card=button.closest(".evaluation-suite-card");const input=card&&card.querySelector("[data-evaluation-suite-file]");const file=input&&input.files&&input.files[0];if(!file){toast("请先选择要导入的素材文件",true);return;}
  const form=new FormData();form.append("file",file);button.disabled=true;button.textContent="正在校验并发布…";
  try{const result=await api("/api/model-evaluation/suites/"+encodeURIComponent(suiteId)+"/import",{method:"POST",body:form});toast("新版本已发布，共 "+String((result.validation||{}).total||0)+" 条有效用例");await refreshEvaluationSuites();}catch(error){toast("素材导入失败："+error.message,true);}finally{if(button.isConnected){button.disabled=false;button.textContent="校验并发布新版本";}}
}
function comparisonRankFor(result,runId){const item=(result.ranking||[]).find(function(row){return row.run_id===runId;});return item?String(item.rank):"—";}
function renderEvaluationComparison(){
  const terminal=state.evaluationRuns.filter(evaluationTerminal);const selected=new Set(state.selectedEvaluationComparisonRunIds||[]);
  $("#evaluationComparisonRunList").innerHTML=terminal.map(function(run){const snapshot=evaluationSnapshot(run),model=snapshot.model||{},summary=run.summary||{};return '<label class="evaluation-comparison-run"><input type="checkbox" data-evaluation-comparison-run="'+esc(run.id)+'" '+(selected.has(run.id)?"checked":"")+'><span><strong>'+esc(model.name||model.model_name||"未记录模型")+'</strong><small>'+esc(evaluationKindLabels[snapshot.run_kind]||snapshot.run_kind||run.backend)+' · '+esc(evaluationPlanLabels[snapshot.plan]||snapshot.plan||"—")+' · '+esc(fmtTime(run.created_at))+'</small></span><b>'+esc(summary.quality_score==null?"—":evaluationNumber(summary.quality_score,1)+" 分")+'</b></label>';}).join("")||'<p class="muted">当前项目还没有可对比的已结束运行。</p>';
  $("#createEvaluationComparisonButton").disabled=selected.size<2||selected.size>10||!hasPermission("evaluation:operate");
  const result=state.evaluationComparisonResult||(((state.evaluationComparisons[0]||{}).config||{}).result)||null;
  if(!result){$("#evaluationComparisonResult").innerHTML='<p class="muted">请选择 2～10 个已结束运行。</p>';return;}
  const notice='<div class="evaluation-comparison-notice '+(result.comparable?"good":"warning")+'"><strong>'+(result.comparable?"口径一致，可生成排名":"口径不一致，仅并列展示")+'</strong><span>'+esc(result.notice||"")+(result.mismatches&&result.mismatches.length?" 差异："+esc(result.mismatches.join("、")):"")+'</span></div>';
  const rows=(result.rows||[]).map(function(row){return '<tr><td>'+comparisonRankFor(result,row.run_id)+'</td><td><strong>'+esc(row.model_name)+'</strong><small class="table-subline">'+esc(shortId(row.run_id))+'</small></td><td>'+esc(row.quality_score==null?"—":evaluationNumber(row.quality_score,1))+'</td><td>'+esc(row.success_rate==null?"—":evaluationNumber(row.success_rate,1)+"%")+'</td><td>'+esc(row.latency_p95_ms==null?"—":evaluationNumber(row.latency_p95_ms,1)+" ms")+'</td><td>'+esc(row.output_tokens_per_second==null?"—":evaluationNumber(row.output_tokens_per_second,2))+'</td><td>'+esc(((row.capacity||{}).max_stable_concurrency)==null?"—":(row.capacity||{}).max_stable_concurrency)+'</td></tr>';}).join("");
  $("#evaluationComparisonResult").innerHTML=notice+'<div class="table-wrap"><table><thead><tr><th>排名</th><th>模型 / 运行</th><th>质量分</th><th>成功率</th><th>P95</th><th>输出 Token/s</th><th>稳定并发</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
}
async function createEvaluationComparison(allowMismatch){
  try{const data=await api("/api/model-evaluation/comparisons",{method:"POST",body:{run_ids:state.selectedEvaluationComparisonRunIds,allow_mismatch:Boolean(allowMismatch)}});state.evaluationComparisonResult=data.result;state.evaluationComparisons.unshift(data.comparison);renderEvaluationComparison();toast(data.result.comparable?"模型对比已生成":"已按确认要求并列展示不同口径运行");}catch(error){if(error.status===409&&!allowMismatch&&window.confirm(error.message+"\n\n是否确认仅并列展示，不进行排名？")){return createEvaluationComparison(true);}toast("模型对比失败："+error.message,true);}
}
function closeEvaluationReview(){const dialog=$("#evaluationReviewDialog");if(dialog.open)dialog.close();}
function openEvaluationReview(resultId,name){
  const item=state.evaluationResults.find(function(result){return result.id===resultId;});const score=(item&&item.score)||{};const manual=score.manual_review||{};
  $("#evaluationReviewResultId").value=resultId;$("#evaluationReviewTitle").textContent=(name||"当前用例")+" · 人工复核";$("#evaluationReviewScore").value=Math.round(Number(manual.score==null?(score.score==null ? 0.8 : score.score):manual.score)*100);$("#evaluationReviewComment").value=manual.comment||"";$("#evaluationReviewError").textContent="";const dialog=$("#evaluationReviewDialog");if(typeof dialog.showModal==="function")dialog.showModal();else dialog.setAttribute("open","");
}
async function submitEvaluationReview(event){
  event.preventDefault();const errorNode=$("#evaluationReviewError");errorNode.textContent="";const resultId=$("#evaluationReviewResultId").value;
  try{await api("/api/model-evaluation/runs/"+encodeURIComponent(state.selectedEvaluationRunId)+"/results/"+encodeURIComponent(resultId)+"/reviews",{method:"POST",body:{score:Number($("#evaluationReviewScore").value),rubric:{},comment:$("#evaluationReviewComment").value.trim()}});closeEvaluationReview();toast("人工复核已保存，综合分和报告将使用复核结果");state.evaluationReport=null;state.evaluationReportRunId="";await refreshSelectedEvaluationRun();}catch(error){errorNode.textContent=error.message;}
}
function renderEvaluationRuns(){
  $("#evaluationSuiteCount").textContent=String(((state.evaluationBootstrap||{}).suites||[]).length)+" 个";
  $("#evaluationRunCount").textContent=String(state.evaluationRuns.length)+" 次";
  const runtime=(state.evaluationBootstrap&&state.evaluationBootstrap.runtime)||{};
  $("#evaluationRuntimeState").textContent=runtime.evalscope_configured?("EvalScope "+runtime.evalscope_version):"Mock / 原生可用";
  $("#evaluationRuntimeHint").textContent=runtime.evalscope_configured?("EvalScope 已配置为隔离子进程，队列后端为 "+(runtime.queue_backend||"local")+"。"):("EvalScope 尚未配置；Mock 与原生基础评测可用，标准 Benchmark 和并发阶梯保持禁用。队列后端为 "+(runtime.queue_backend||"local")+"。");
  const total=state.evaluationRuns.length;const totalPages=Math.max(1,Math.ceil(total/state.evaluationRunsPageSize));state.evaluationRunsPage=Math.min(Math.max(1,state.evaluationRunsPage),totalPages);const start=(state.evaluationRunsPage-1)*state.evaluationRunsPageSize;const pageRuns=state.evaluationRuns.slice(start,start+state.evaluationRunsPageSize);const end=Math.min(total,start+pageRuns.length);
  $("#evaluationRunsPageSummary").textContent=total?("共 "+total+" 条 · 当前 "+(start+1)+"–"+end+" 条"):"共 0 条";$("#evaluationRunsPageIndicator").textContent="第 "+state.evaluationRunsPage+" / "+totalPages+" 页";$("#evaluationRunsPrevButton").disabled=state.evaluationRunsPage<=1;$("#evaluationRunsNextButton").disabled=state.evaluationRunsPage>=totalPages;
  const signature=JSON.stringify({page:state.evaluationRunsPage,selected:state.selectedEvaluationRunId,canOperate:hasPermission("evaluation:operate"),canManage:hasPermission("evaluation:manage"),runs:state.evaluationRuns.map(function(run){return [run.id,run.status,run.phase,run.progress,run.artifact_ref,run.created_at];})});
  if(signature===state.evaluationRunsSignature)return;
  state.evaluationRunsSignature=signature;
  $("#evaluationRunsBody").innerHTML=pageRuns.map(function(run){
    const snapshot=evaluationSnapshot(run);const model=snapshot.model||{};const selected=run.id===state.selectedEvaluationRunId;
    const actions='<div class="evaluation-run-actions"><button class="text-button" type="button" data-evaluation-open="'+esc(run.id)+'">查看详情</button>'+(evaluationTerminal(run)?'<button class="text-button" type="button" data-evaluation-report="'+esc(run.id)+'">评测报告</button>':"")+(evaluationActive(run)&&hasPermission("evaluation:operate")?'<button class="text-button danger" type="button" data-evaluation-stop="'+esc(run.id)+'">停止</button>':"")+(evaluationTerminal(run)&&hasPermission("evaluation:manage")?'<button class="text-button danger" type="button" data-evaluation-delete="'+esc(run.id)+'">删除</button>':"")+'</div>';
    return '<tr'+(selected?' class="selected"':"")+'><td><div class="evaluation-run-name"><strong>'+esc(evaluationKindLabels[snapshot.run_kind]||snapshot.run_kind||run.backend)+'</strong><small>'+esc((model.name||model.model_name||"未记录模型")+" · "+evaluationRunLabel(run).replace("评测任务 ",""))+'</small></div></td><td>'+esc(evaluationPlanLabels[snapshot.plan]||snapshot.plan||"—")+'</td><td>'+statusPill(run.status)+'</td><td><div class="evaluation-run-progress"><i><b style="width:'+Math.max(0,Math.min(Number(run.progress)||0,100))+'%"></b></i><span>'+esc(run.progress||0)+'%</span></div></td><td>'+esc(fmtTime(run.created_at))+'</td><td>'+actions+'</td></tr>';
  }).join("")||'<tr><td class="evaluation-empty-row" colspan="6">当前项目还没有模型评测运行。</td></tr>';
}
function setEvaluationRunsPage(page){const totalPages=Math.max(1,Math.ceil(state.evaluationRuns.length/state.evaluationRunsPageSize));const next=Math.min(Math.max(1,Number(page)||1),totalPages);if(next===state.evaluationRunsPage)return;state.evaluationRunsPage=next;state.evaluationRunsSignature="";renderEvaluationRuns();}
function readableEvaluationEventMessage(item){
  const message=String((item&&item.message)||(item&&item.event_type)||"阶段事件");
  if(!message.includes("�"))return message;
  const phase=String((item&&item.phase)||(item&&item.status)||"");
  const eventType=String((item&&item.event_type)||"");
  if(/^EvalScope\b/i.test(message)){
    if(eventType==="failed"||phase==="failed")return "EvalScope 子进程执行失败";
    if(eventType==="completed"||phase==="completed")return "EvalScope 子进程执行完成";
    if(eventType==="stopped"||phase==="stopped")return "EvalScope 子进程已停止";
    if(phase==="running"||phase==="preparing")return "EvalScope 评测已启动";
    return "EvalScope 运行阶段更新";
  }
  return "阶段事件（历史记录编码异常）";
}
function renderEvaluationDetail(run,events,results,total){
  if(!run){$("#evaluationDetailTitle").textContent="选择一条评测查看运行监控";$("#evaluationDetailMessage").textContent="提交后可在这里查看 Token 来源、吞吐、P95/P99、错误分类与用例明细。";$("#evaluationPhaseLabel").textContent="尚未选择运行";$("#evaluationProgressLabel").textContent="0%";$("#evaluationProgressBar").style.width="0%";$("#stopEvaluationButton").classList.add("hidden");$("#deleteEvaluationButton").classList.add("hidden");$("#openEvaluationReportButton").disabled=true;$("#downloadEvaluationSummaryButton").disabled=true;$("#downloadEvaluationPerformanceButton").disabled=true;$("#evaluationScoringConfidence").textContent="—";$("#evaluationReviewState").textContent="规则 / 裁判 / 人工";$("#evaluationCapacity").textContent="—";$("#evaluationCapacityState").textContent="并发 / 拐点";$("#evaluationEventsList").innerHTML='<p class="muted">尚未选择运行。</p>';$("#evaluationResultsBody").innerHTML='<tr><td class="evaluation-empty-row" colspan="8">请先从“评测运行”中选择任务。</td></tr>';return;}
  const snapshot=evaluationSnapshot(run);const summary=Object.assign({},run.summary||{});const latestLiveEvent=[...(events||[])].reverse().find(function(item){const data=item.data||{};return data.success_rate!=null||data.p95_ms!=null||data.output_tokens_per_second!=null;});
  if(latestLiveEvent){const live=latestLiveEvent.data||{};if(summary.success_rate==null&&live.success_rate!=null)summary.success_rate=live.success_rate;summary.performance=Object.assign({},summary.performance||{});if(summary.performance.latency_p95_ms==null&&live.p95_ms!=null)summary.performance.latency_p95_ms=live.p95_ms;if(summary.performance.output_tokens_per_second==null&&live.output_tokens_per_second!=null)summary.performance.output_tokens_per_second=live.output_tokens_per_second;}
  const performance=summary.performance||{};const token=summary.token_usage||{};const robustness=summary.robustness||{};const scoring=summary.scoring||{};const capacity=summary.capacity||{};
  $("#evaluationDetailTitle").textContent=(evaluationKindLabels[snapshot.run_kind]||snapshot.run_kind||"模型评测")+" · "+evaluationRunLabel(run);
  $("#evaluationDetailMessage").textContent=readableEvaluationEventMessage({message:run.message||run.error||"评测运行已创建",phase:run.phase,status:run.status});
  $("#evaluationPhaseLabel").textContent=evaluationPhaseLabels[run.phase]||evaluationPhaseLabels[run.status]||run.phase||run.status;
  $("#evaluationProgressLabel").textContent=String(run.progress||0)+"%";$("#evaluationProgressBar").style.width=Math.max(0,Math.min(Number(run.progress)||0,100))+"%";
  $("#stopEvaluationButton").classList.toggle("hidden",!evaluationActive(run)||!hasPermission("evaluation:operate"));
  $("#deleteEvaluationButton").classList.toggle("hidden",!evaluationTerminal(run)||!hasPermission("evaluation:manage"));
  $("#openEvaluationReportButton").disabled=!evaluationTerminal(run);
  $("#downloadEvaluationSummaryButton").disabled=!run.artifact_ref;$("#downloadEvaluationPerformanceButton").disabled=!run.artifact_ref;
  $("#evaluationSuccessRate").textContent=summary.success_rate==null?"—":evaluationNumber(summary.success_rate,1)+"%";
  $("#evaluationQualityScore").textContent="质量分 "+(summary.quality_score==null?"—":evaluationNumber(summary.quality_score,1));
  const sources=Object.keys(token.source_counts||{});const sourceLabels={api_usage:"API 精确",local_tokenizer:"本地 Tokenizer",estimated:"估算"};
  $("#evaluationTokenSource").textContent=sources.length?sources.map(function(item){return sourceLabels[item]||item;}).join(" / "):"—";
  $("#evaluationTokenCount").textContent="总量 "+(token.total_tokens==null?"—":token.total_tokens);
  $("#evaluationLatency").textContent=(performance.latency_p95_ms==null?"—":evaluationNumber(performance.latency_p95_ms,1))+" / "+(performance.latency_p99_ms==null?"—":evaluationNumber(performance.latency_p99_ms,1))+" ms";
  $("#evaluationThroughput").textContent=performance.output_tokens_per_second==null?"—":evaluationNumber(performance.output_tokens_per_second,2);
  const robustnessApplicable=["foundation","mock","mock_full","custom"].includes(snapshot.run_kind)||robustness.average_retention!=null;
  $("#evaluationRobustness").textContent=robustness.average_retention==null?(robustnessApplicable?"等待数据":"不适用"):evaluationNumber(robustness.average_retention,1)+"% / "+evaluationNumber(robustness.worst_retention,1)+"%";
  $("#evaluationScoringConfidence").textContent=scoring.confidence==null?"—":evaluationNumber(scoring.confidence,1)+"%";
  $("#evaluationReviewState").textContent="人工已复核 "+String(scoring.manual_reviewed_cases||0)+" / 待复核 "+String(scoring.pending_manual_review_cases||0);
  const capacityApplicable=["concurrency","deep_performance"].includes(snapshot.run_kind)||capacity.max_stable_rps!=null||capacity.max_stable_concurrency!=null;
  $("#evaluationCapacity").textContent=capacity.max_stable_rps!=null?(evaluationNumber(capacity.max_stable_rps,2)+" RPS"):(capacity.max_stable_concurrency==null?(capacityApplicable?"等待数据":"不适用"):("并发 "+capacity.max_stable_concurrency));
  $("#evaluationCapacityState").textContent=(capacity.capacity_knee&&capacity.capacity_knee.concurrency!=null)?("拐点并发 "+capacity.capacity_knee.concurrency):(capacity.conclusion||(capacityApplicable?"等待性能阶梯":"当前类型不计算容量"));
  $("#evaluationEventCount").textContent=events.length+" 条";
  $("#evaluationEventsList").innerHTML=events.map(function(item){return '<article class="evaluation-event"><i></i><div><strong>'+esc(readableEvaluationEventMessage(item))+'</strong><small>'+esc(evaluationPhaseLabels[item.phase]||item.phase||"事件")+' · '+esc(fmtTime(item.created_at))+(item.progress==null?"":" · "+esc(item.progress)+"%")+'</small></div></article>';}).join("")||'<p class="muted">暂无阶段事件。</p>';
  $("#evaluationResultCount").textContent=String(total||0)+" 条";
  const scoreSourceLabels={deterministic_rules:"规则",deterministic_rules_pending_review:"规则·待复核",rules_and_independent_judge:"规则+裁判",rules_and_independent_judge_pending_review:"规则+裁判·待复核",rules_judge_and_manual_review:"规则+裁判+人工",mock:"Mock"};
  $("#evaluationResultsBody").innerHTML=results.map(function(item,index){const metrics=item.metrics||{};const score=item.score||{};const info=evaluationCaseInfo(run,item.case_id,index);const totalTokens=metrics.total_tokens==null?((Number(metrics.input_tokens)||0)+(Number(metrics.output_tokens)||0)):metrics.total_tokens;const reviewStatus=(score.manual_review||{}).status||"not_required";const manual=reviewStatus==="completed";const review=hasPermission("evaluation:manage")&&evaluationTerminal(run)?'<button class="text-button" type="button" data-evaluation-review="'+esc(item.id)+'" data-evaluation-review-name="'+esc(info.name)+'">'+(manual?"重新复核":"复核")+'</button>':(manual?"已复核":reviewStatus==="pending"?"待复核":"无需复核");return '<tr><td><div class="evaluation-result-name"><strong title="'+esc(info.name)+'">'+esc(info.name)+'</strong><small>'+esc(info.description||("第 "+String(index+1)+" 条用例"))+'</small></div></td><td>'+evaluationCaseStatusPill(item.status)+'</td><td>'+esc(totalTokens||"—")+'<small class="table-subline">'+esc(metrics.token_source||"—")+'</small></td><td>'+esc(metrics.ttft_ms==null?"—":evaluationNumber(metrics.ttft_ms,1)+" ms")+'</td><td>'+esc(metrics.latency_ms==null?"—":evaluationNumber(metrics.latency_ms,1)+" ms")+'</td><td><span class="evaluation-result-score">'+esc(score.score==null?"—":evaluationNumber(Number(score.score)*100,1)+"%")+'</span></td><td><span class="evaluation-score-source">'+esc(scoreSourceLabels[score.scoring_source]||score.scoring_source||"规则")+'</span></td><td>'+review+'</td></tr>';}).join("")||'<tr><td class="evaluation-empty-row" colspan="8">当前运行尚无用例级明细；性能阶梯请查看顶部汇总与 CSV。</td></tr>';
}
function renderEvaluationReport(report){
  const selected=state.evaluationRuns.find(function(item){return item.id===state.selectedEvaluationRunId;});
  const available=Boolean(report);
  $("#downloadEvaluationReportDocxButton").disabled=!available;$("#downloadEvaluationReportPdfButton").disabled=!available;
  $("#downloadEvaluationSummaryButton").disabled=!selected||!selected.artifact_ref;$("#downloadEvaluationPerformanceButton").disabled=!selected||!selected.artifact_ref;
  if(!report){state.evaluationReportCasesPage=1;$("#evaluationReportTitle").textContent="选择一条已结束的评测查看报告";$("#evaluationReportMeta").textContent="报告会把指标翻译成可阅读的结论，并说明下一步怎么处理。";const conclusion=$("#evaluationReportConclusion");conclusion.className="evaluation-report-conclusion info";conclusion.innerHTML="<span>结论</span><strong>尚未生成报告</strong><p>请先选择一条已完成、失败或停止的评测任务。</p>";$("#evaluationReportOverview").innerHTML="";$("#evaluationReportMetrics").innerHTML="";$("#evaluationReportFindings").innerHTML='<p class="muted">暂无内容。</p>';$("#evaluationReportRecommendations").innerHTML="";$("#evaluationReportCaseSummary").textContent="暂无用例数据";$("#evaluationReportCasesBody").innerHTML='<tr><td class="evaluation-empty-row" colspan="5">尚未生成报告。</td></tr>';$("#evaluationReportCasesPageSummary").textContent="共 0 条";$("#evaluationReportCasesPageIndicator").textContent="第 1 / 1 页";$("#evaluationReportCasesPrevButton").disabled=true;$("#evaluationReportCasesNextButton").disabled=true;return;}
  $("#evaluationReportTitle").textContent=report.title+" · "+report.report_number;
  $("#evaluationReportMeta").textContent=report.run_kind_text+" · "+report.model_name+" · 生成于 "+report.generated_at;
  const conclusion=$("#evaluationReportConclusion");conclusion.className="evaluation-report-conclusion "+esc(report.conclusion.level||"info");conclusion.innerHTML='<span>一句话结论</span><strong>'+esc(report.conclusion.title)+'</strong><p>'+esc(report.conclusion.summary)+'</p>';
  $("#evaluationReportOverview").innerHTML=[["被测模型",report.model_name],["评测类型",report.run_kind_text],["测试集",report.suite_name+" · v"+report.suite_version],["运行方案",report.plan_text],["任务状态",report.status_text]].map(function(item){return '<div><span>'+esc(item[0])+'</span><strong>'+esc(item[1])+'</strong></div>';}).join("");
  $("#evaluationReportMetrics").innerHTML=(report.metrics||[]).map(function(item){return '<article><span>'+esc(item.assessment)+'</span><strong>'+esc(item.value)+'</strong><h5>'+esc(item.label)+'</h5><p>'+esc(item.explanation)+'</p></article>';}).join("");
  $("#evaluationReportFindings").innerHTML=(report.findings||[]).map(function(item){return '<article class="'+esc(item.level||"info")+'"><i></i><div><strong>'+esc(item.title)+'</strong><p>'+esc(item.detail)+'</p></div></article>';}).join("")||'<p class="muted">暂无内容。</p>';
  $("#evaluationReportRecommendations").innerHTML=(report.recommendations||[]).map(function(item){return '<li>'+esc(item)+'</li>';}).join("");
  const summary=report.case_summary||{};$("#evaluationReportCaseSummary").textContent="共 "+(summary.total||0)+" 条 · 达标 "+(summary.passed||0)+" · 未达标 "+(summary.failed||0)+" · 执行异常 "+(summary.error||0);
  const cases=report.cases||[];const total=cases.length;const totalPages=Math.max(1,Math.ceil(total/state.evaluationReportCasesPageSize));state.evaluationReportCasesPage=Math.min(Math.max(1,state.evaluationReportCasesPage),totalPages);const start=(state.evaluationReportCasesPage-1)*state.evaluationReportCasesPageSize;const pageCases=cases.slice(start,start+state.evaluationReportCasesPageSize);const end=Math.min(total,start+pageCases.length);
  $("#evaluationReportCasesPageSummary").textContent=total?("共 "+total+" 条 · 当前 "+(start+1)+"–"+end+" 条"):"共 0 条";$("#evaluationReportCasesPageIndicator").textContent="第 "+state.evaluationReportCasesPage+" / "+totalPages+" 页";$("#evaluationReportCasesPrevButton").disabled=state.evaluationReportCasesPage<=1;$("#evaluationReportCasesNextButton").disabled=state.evaluationReportCasesPage>=totalPages;
  $("#evaluationReportCasesBody").innerHTML=pageCases.map(function(item){return '<tr><td><strong>'+esc(item.name)+'</strong></td><td>'+esc(item.category)+'</td><td>'+evaluationCaseStatusPill(item.status)+'</td><td>'+esc(item.score==null?"—":evaluationNumber(item.score,1)+"%")+'</td><td>'+esc(item.latency_ms==null?"—":evaluationNumber(item.latency_ms,1)+" ms")+'</td></tr>';}).join("")||'<tr><td class="evaluation-empty-row" colspan="5">当前运行没有用例级明细，请查看报告结论与核心指标。</td></tr>';
}
function setEvaluationReportCasesPage(page){const totalPages=Math.max(1,Math.ceil((((state.evaluationReport||{}).cases)||[]).length/state.evaluationReportCasesPageSize));const next=Math.min(Math.max(1,Number(page)||1),totalPages);if(next===state.evaluationReportCasesPage)return;state.evaluationReportCasesPage=next;renderEvaluationReport(state.evaluationReport);}
async function loadEvaluationReport(force){
  const runId=state.selectedEvaluationRunId;const run=state.evaluationRuns.find(function(item){return item.id===runId;});
  if(!runId||!evaluationTerminal(run)){state.evaluationReport=null;state.evaluationReportRunId="";renderEvaluationReport(null);if(runId)toast("任务结束后才能生成最终评测报告",true);return;}
  if(!force&&state.evaluationReport&&state.evaluationReportRunId===runId){renderEvaluationReport(state.evaluationReport);return;}
  $("#evaluationReportTitle").textContent="正在生成可阅读报告…";$("#evaluationReportMeta").textContent="正在整理结论、指标解释、异常和建议。";
  try{const payload=await api("/api/model-evaluation/runs/"+encodeURIComponent(runId)+"/report");if(state.evaluationReportRunId!==runId)state.evaluationReportCasesPage=1;state.evaluationReport=payload.report;state.evaluationReportRunId=runId;renderEvaluationReport(state.evaluationReport);}catch(error){renderEvaluationReport(null);toast("评测报告生成失败："+error.message,true);}
}
function openEvaluationReport(runId){if(runId)state.selectedEvaluationRunId=runId;showEvaluationWorkspace("report");loadEvaluationReport(true);}
async function refreshSelectedEvaluationRun(){
  const runId=state.selectedEvaluationRunId;if(!runId){renderEvaluationDetail(null,[],[],0);return;}
  try {const payloads=await Promise.all([api("/api/model-evaluation/runs/"+encodeURIComponent(runId)),api("/api/model-evaluation/runs/"+encodeURIComponent(runId)+"/events?limit=1000"),api("/api/model-evaluation/runs/"+encodeURIComponent(runId)+"/results?page_size=200")]);const run=payloads[0];state.evaluationEvents=payloads[1].events||[];state.evaluationResults=payloads[2].results||[];const index=state.evaluationRuns.findIndex(function(item){return item.id===run.id;});if(index>=0)state.evaluationRuns[index]=Object.assign({},state.evaluationRuns[index],run);renderEvaluationRuns();renderEvaluationDetail(run,state.evaluationEvents,state.evaluationResults,payloads[2].total||0);}catch(error){toast("评测详情加载失败："+error.message,true);}
}
async function loadEvaluationWorkspace(reset){
  try {if(reset||!state.evaluationBootstrap){if(reset)state.evaluationRunsPage=1;state.evaluationBootstrap=await api("/api/model-evaluation/bootstrap");state.evaluationRuns=state.evaluationBootstrap.runs||[];state.evaluationSuites=state.evaluationBootstrap.suites||[];state.evaluationComparisons=state.evaluationBootstrap.comparisons||[];populateEvaluationModels();populateEvaluationSuites();renderEvaluationSuites();renderEvaluationComparison();syncEvaluationForm();}else{const data=await api("/api/model-evaluation/runs?limit=500");state.evaluationRuns=data.runs||[];}if(!state.evaluationRuns.some(function(run){return run.id===state.selectedEvaluationRunId;}))state.selectedEvaluationRunId=(state.evaluationRuns[0]||{}).id||"";renderEvaluationRuns();renderEvaluationComparison();await refreshSelectedEvaluationRun();}catch(error){toast("模型评测加载失败："+error.message,true);}
}
async function submitEvaluation(event){
  event.preventDefault();$("#evaluationFormError").textContent="";const kind=$("#evaluationRunKind").value;const modelId=$("#evaluationModelProfile").value;if(!["mock","mock_full"].includes(kind)&&!modelId){$("#evaluationFormError").textContent="请选择已配置的被测模型";return;}if($("#evaluationJudgeProfile").value&&$("#evaluationJudgeProfile").value===modelId){$("#evaluationFormError").textContent="裁判模型必须独立于被测模型";return;}const button=$("#evaluationSubmitButton");button.disabled=true;button.textContent="正在提交…";try{const run=await api("/api/model-evaluation/runs",{method:"POST",body:{run_kind:kind,plan:$("#evaluationPlan").value,model_profile_id:modelId,suite_version_id:$("#evaluationSuiteVersion").value,judge_model_profile_id:$("#evaluationJudgeProfile").value,server_session_id:$("#evaluationServerSession").value,manual_review_percent:Number($("#evaluationManualReviewPercent").value),stream:$("#evaluationStream").checked,max_tokens:Number($("#evaluationMaxTokens").value),timeout_seconds:Number($("#evaluationTimeout").value)}});state.evaluationRunsPage=1;state.selectedEvaluationRunId=run.id;showEvaluationWorkspace("detail");toast(evaluationRunLabel(run)+"已排队");await loadEvaluationWorkspace(false);}catch(error){$("#evaluationFormError").textContent=error.message;}finally{button.textContent="提交评测任务";syncEvaluationForm();}}
async function stopEvaluationRun(runId){try{await api("/api/model-evaluation/runs/"+encodeURIComponent(runId)+"/stop",{method:"POST"});toast("已请求安全停止模型评测");state.selectedEvaluationRunId=runId;await loadEvaluationWorkspace(false);}catch(error){toast("停止评测失败："+error.message,true);}}
async function deleteEvaluationRun(runId){const run=state.evaluationRuns.find(function(item){return item.id===runId;});if(!run)return;if(!window.confirm("确认删除“"+evaluationRunLabel(run)+"”？\n\n该任务的阶段事件、结果明细和评测产物会一并删除，且无法恢复。"))return;try{await api("/api/model-evaluation/runs/"+encodeURIComponent(runId),{method:"DELETE"});toast(evaluationRunLabel(run)+"已删除");if(state.selectedEvaluationRunId===runId)state.selectedEvaluationRunId="";showEvaluationWorkspace("runs");await loadEvaluationWorkspace(false);}catch(error){toast("删除评测失败："+error.message,true);}}
async function downloadEvaluationReport(format){const runId=state.selectedEvaluationRunId;if(!runId){toast("请先选择评测运行",true);return;}try{const response=await fetch("/api/model-evaluation/runs/"+encodeURIComponent(runId)+"/report/"+format,{headers:{"X-Project-ID":state.projectId}});if(!response.ok){const data=await response.json();throw new Error(data.detail||"报告下载失败");}const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download="model_evaluation_report."+format;document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);}catch(error){toast("评测报告下载失败："+error.message,true);}}
async function downloadEvaluationArtifact(kind){const runId=state.selectedEvaluationRunId;if(!runId){toast("请先选择评测运行",true);return;}try{const response=await fetch("/api/model-evaluation/runs/"+encodeURIComponent(runId)+"/artifacts/"+kind,{headers:{"X-Project-ID":state.projectId}});if(!response.ok){const data=await response.json();throw new Error(data.detail||"下载失败");}const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download=kind==="performance"?"model_evaluation_performance.csv":"model_evaluation_summary.json";document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);}catch(error){toast("下载评测产物失败："+error.message,true);}}
async function loadModels() {
  try {
    const data = await api("/api/model-profiles"); state.models = data.profiles || [];
    $("#modelCount").textContent = state.models.length + " 项";
    $("#modelList").innerHTML = state.models.map(function(model){
      return '<div class="model-item"><div class="model-item-head"><strong>' + esc(model.name) + "</strong>" + (model.is_active?'<span class="status succeeded">已启用</span>':'<span class="status idle">未启用</span>') + '</div><p>' + esc(model.model_name) + "<br>" + esc(model.base_url) + "<br>Key: " + (model.has_api_key?"••••••••":"未配置") + '</p><div class="model-actions"><button data-model-edit="' + model.id + '">编辑</button><button data-model-test="' + model.id + '">连通测试</button>' + (!model.is_active?'<button data-model-activate="'+model.id+'">启用</button>':"") + "</div></div>";
    }).join("") || '<div class="model-empty"><span class="model-empty-icon" aria-hidden="true">＋</span><strong>尚未配置模型</strong><p>保存模型后，可在这里进行编辑、连通测试与启用切换。模型不是任务主流程的必选项。</p></div>';
  } catch (error) { console.warn(error); }
}
async function submitModel(event) {
  event.preventDefault(); $("#modelFormError").textContent="";
  const payload = {id:$("#modelId").value||null,name:$("#modelName").value.trim(),provider:$("#modelProvider").value,base_url:$("#modelBaseUrl").value.trim(),model_name:$("#modelModelName").value.trim(),api_key:$("#modelApiKey").value.trim(),temperature:Number($("#modelTemperature").value),max_tokens:Number($("#modelMaxTokens").value),system_prompt:$("#modelPrompt").value.trim(),is_active:$("#modelActive").checked};
  try { await api("/api/model-profiles",{method:"POST",body:payload}); toast("模型配置已保存"); $("#modelApiKey").value=""; await loadModels(); }
  catch(error){ $("#modelFormError").textContent=error.message; }
}
function editModel(id) {
  const model = state.models.find(function(item){ return item.id===id; }); if(!model)return;
  $("#modelId").value=model.id; $("#modelName").value=model.name; $("#modelProvider").value=model.provider; $("#modelBaseUrl").value=model.base_url; $("#modelModelName").value=model.model_name; $("#modelApiKey").value=""; $("#modelTemperature").value=model.temperature; $("#modelMaxTokens").value=model.max_tokens; $("#modelPrompt").value=model.system_prompt||""; $("#modelActive").checked=Boolean(model.is_active); window.scrollTo({top:0,behavior:"smooth"});
}
async function modelAction(id,action,sourceButton) {
  const card=sourceButton && sourceButton.closest(".model-item");
  let feedback=card && card.querySelector(".model-action-feedback");
  if(card && !feedback) {
    feedback=document.createElement("div");
    feedback.className="model-action-feedback";
    feedback.setAttribute("role","status");
    feedback.setAttribute("aria-live","polite");
    card.appendChild(feedback);
  }
  if(sourceButton) {sourceButton.disabled=true;sourceButton.classList.add("is-busy");sourceButton.textContent=action==="test"?"测试中…":"启用中…";}
  if(feedback) {feedback.className="model-action-feedback pending";feedback.textContent=action==="test"?"正在请求模型并等待响应，最长约 30 秒…":"正在切换启用模型…";}
  toast(action==="test"?"模型连通测试已开始，请稍候…":"正在启用模型…");
  try {
    const result=await api("/api/model-profiles/"+encodeURIComponent(id)+"/"+action,{method:"POST"});
    if(feedback) {feedback.className="model-action-feedback success";feedback.textContent=action==="test"?"连接成功："+result.message:"模型已启用";}
    toast(action==="test"?"模型响应："+result.message:"模型已启用");
    if(action!=="test") await loadModels();
  }
  catch(error){
    if(feedback) {feedback.className="model-action-feedback error";feedback.textContent=(action==="test"?"连通测试失败：":"启用失败：")+error.message;}
    toast((action==="test"?"连通测试失败：":"启用失败：")+error.message,true);
  } finally {
    if(sourceButton && sourceButton.isConnected) {sourceButton.disabled=false;sourceButton.classList.remove("is-busy");sourceButton.textContent=action==="test"?"重新测试":"启用";}
  }
}
async function loadInterfaces() {
  try {
    const data=await api("/api/interface-assets/workspace");
    state.interfaces=data.assets||[];
    state.interfaceModules=data.modules||[];
    state.interfaceEnvironments=data.environments||[];
    state.interfaceVariables=data.variables||[];
    state.interfaceScenarios=data.scenarios||[];
    state.interfaceScenarioRuns=data.scenario_runs||[];
    state.selectedInterfaceScenarioIds=state.selectedInterfaceScenarioIds.filter(function(id){return state.interfaceScenarios.some(function(item){return item.id===id;});});
    if(state.selectedInterfaceModuleId && !state.interfaceModules.some(function(item){return item.id===state.selectedInterfaceModuleId;})) state.selectedInterfaceModuleId="";
    renderInterfaceWorkspace();
  } catch(error){ console.warn(error);if($("#view-interfaces").classList.contains("active"))toast("接口资产加载失败："+error.message,true); }
}
function interfaceCanManage(){return hasPermission("interface:manage");}
function interfaceCanExecute(){return hasPermission("test:execute");}
function projectCanManage(){return hasPermission("project:members");}
function openInterfaceDialog(id){const dialog=$("#"+id);if(!dialog)return;if(typeof dialog.showModal==="function")dialog.showModal();else dialog.classList.add("open");}
function closeInterfaceDialog(id){const dialog=$("#"+id);if(!dialog)return;if(dialog.open)dialog.close();dialog.classList.remove("open");}
function interfaceSelectOptions(items,emptyLabel){return '<option value="">'+esc(emptyLabel)+'</option>'+items.map(function(item){return '<option value="'+esc(item.id)+'">'+esc(item.name)+'</option>';}).join("");}
function syncInterfacePermissions(){
  const canManage=interfaceCanManage();
  $$(".interface-manage").forEach(function(node){node.classList.toggle("permission-hidden",!canManage);});
  $$(".interface-execute").forEach(function(node){node.classList.toggle("permission-hidden",!interfaceCanExecute());});
  $$(".project-manage").forEach(function(node){node.classList.toggle("permission-hidden",!projectCanManage());});
  $("#interfaceImportTab").classList.toggle("permission-hidden",!canManage);
  const importPanel=$('[data-workspace-panel="interfaces"][data-workspace-name="import"]');
  if(!canManage && importPanel && !importPanel.classList.contains("hidden"))activateWorkspaceTab("interfaces","assets");
}
function syncInterfaceOptions(){
  const moduleOptions=interfaceSelectOptions(state.interfaceModules,"未分组");
  ["interfaceAssetModule","interfaceImportModule"].forEach(function(id){const node=$("#"+id);const current=node.value;node.innerHTML=moduleOptions;node.value=current;});
  const environmentOptions=interfaceSelectOptions(state.interfaceEnvironments,"不绑定环境");
  ["interfaceAssetEnvironment","interfaceImportEnvironment"].forEach(function(id){const node=$("#"+id);const current=node.value;node.innerHTML=environmentOptions;node.value=current;});
  const scenarioEnvironment=$("#interfaceScenarioEnvironment");if(scenarioEnvironment){const current=scenarioEnvironment.value;scenarioEnvironment.innerHTML=interfaceSelectOptions(state.interfaceEnvironments,"跟随各接口");scenarioEnvironment.value=current;}
  const variableEnvironment=$("#interfaceVariableEnvironment");
  const variableCurrent=variableEnvironment.value;
  variableEnvironment.innerHTML=interfaceSelectOptions(state.interfaceEnvironments,"项目通用");
  variableEnvironment.value=variableCurrent;
  const filter=$("#interfaceEnvironmentFilter");const filterValue=filter.value;
  filter.innerHTML=interfaceSelectOptions(state.interfaceEnvironments,"全部环境");filter.value=filterValue;
}
function renderInterfaceModules(){
  const project=(state.identity&&state.identity.current_project)||((state.identity&&state.identity.projects)||[]).find(function(item){return item.id===state.projectId;})||{};
  const allCount=state.interfaces.length;
  const ungroupedCount=state.interfaces.filter(function(item){return !item.module_id;}).length;
  const keyword=$("#interfaceModuleSearch").value.trim().toLowerCase();
  $("#interfaceProjectName").textContent=businessProjectLabel(project);
  $("#interfaceBreadcrumbProject").textContent=project.name||"当前项目";
  const rows=[
    '<article class="interface-module-item '+(!state.selectedInterfaceModuleId?'active':'')+'"><button class="interface-module-select" type="button" data-interface-module=""><i class="interface-folder-icon all" aria-hidden="true"></i><span><strong>全部接口</strong><small>当前项目的全部接口资产</small></span><em>'+allCount+'</em></button></article>',
    '<article class="interface-module-item '+(state.selectedInterfaceModuleId==="__ungrouped__"?'active':'')+'"><button class="interface-module-select" type="button" data-interface-module="__ungrouped__"><i class="interface-folder-icon" aria-hidden="true"></i><span><strong>未分组接口</strong><small>尚未归属业务模块</small></span><em>'+ungroupedCount+'</em></button></article>'
  ];
  const modules=state.interfaceModules.filter(function(module){return !keyword || [module.name,module.description].some(function(value){return String(value||"").toLowerCase().includes(keyword);});});
  modules.forEach(function(module){
    const actions=interfaceCanManage()?'<span class="interface-tree-actions"><button type="button" data-interface-module-edit="'+esc(module.id)+'">编辑</button><button class="danger-text" type="button" data-interface-module-delete="'+esc(module.id)+'">删除</button></span>':'';
    rows.push('<article class="interface-module-item '+(state.selectedInterfaceModuleId===module.id?'active':'')+'"><button class="interface-module-select" type="button" data-interface-module="'+esc(module.id)+'"><i class="interface-folder-icon" aria-hidden="true"></i><span><strong>'+esc(module.name)+'</strong><small>'+esc(module.description||"业务接口模块")+'</small></span><em>'+Number(module.asset_count||0)+'</em></button>'+actions+'</article>');
  });
  if(!state.interfaceModules.length)rows.push('<div class="interface-module-empty"><strong>还没有业务模块</strong><span>先新建模块，再把接口归入对应业务域。</span></div>');
  else if(keyword&&!modules.length)rows.push('<div class="interface-module-empty compact"><span>没有匹配的模块</span></div>');
  $("#interfaceModuleList").innerHTML=rows.join("");
}
function renderInterfaceAssets(){
  const keyword=$("#interfaceAssetSearch").value.trim().toLowerCase();
  const environmentId=$("#interfaceEnvironmentFilter").value;
  const module=state.interfaceModules.find(function(item){return item.id===state.selectedInterfaceModuleId;});
  $("#interfaceAssetPanelTitle").textContent=state.selectedInterfaceModuleId==="__ungrouped__"?"未分组接口":(module?module.name:"全部接口");
  const assets=state.interfaces.filter(function(item){
    if(state.selectedInterfaceModuleId==="__ungrouped__" && item.module_id)return false;
    if(state.selectedInterfaceModuleId && state.selectedInterfaceModuleId!=="__ungrouped__" && item.module_id!==state.selectedInterfaceModuleId)return false;
    if(environmentId && item.environment_id!==environmentId)return false;
    if(!keyword)return true;
    return [item.name,item.path,item.description,item.module_name,item.environment_name].some(function(value){return String(value||"").toLowerCase().includes(keyword);});
  });
  $("#interfaceVisibleCount").textContent=assets.length+" 个接口";
  $("#interfaceBody").innerHTML=assets.map(function(item){
    const target=interfaceTargetUrl(item);
    let actions=interfaceCanExecute()?'<button class="text-button interface-debug-action" type="button" data-interface-asset-open="'+esc(item.id)+'">调试</button>':'';
    actions+='<button class="text-button" type="button" data-interface-versions="'+esc(item.id)+'">版本</button>';
    if(interfaceCanManage()){
      actions+='<button class="text-button" type="button" data-interface-asset-edit="'+esc(item.id)+'">编辑</button>';
      if(item.status!=="published")actions+='<button class="text-button" type="button" data-interface-asset-publish="'+esc(item.id)+'">发布</button>';
      actions+='<button class="text-button danger-text" type="button" data-interface-asset-delete="'+esc(item.id)+'">删除</button>';
    }
    return '<tr><td><strong>'+esc(item.name)+'</strong><small class="table-subline">'+esc(item.description||"暂无接口说明")+'</small></td><td>'+esc(item.module_name||"未分组")+'</td><td><div class="interface-request-cell"><span class="interface-method '+String(item.method||"get").toLowerCase()+'">'+esc(item.method)+'</span><code class="interface-path">'+esc(target||item.path)+'</code></div></td><td>'+esc(item.environment_name||"独立地址")+'</td><td>v'+Number(item.current_version||1)+'<small class="table-subline">共 '+Number(item.version_count||0)+' 版</small></td><td>'+statusPill(item.status)+'</td><td><span class="interface-table-actions">'+actions+'</span></td></tr>';
  }).join("")||'<tr><td colspan="7" class="interface-empty"><div class="interface-catalog-empty"><i aria-hidden="true">HTTP</i><strong>'+(state.interfaces.length?"没有符合筛选条件的接口":"还没有接口")+'</strong><span>'+(state.interfaces.length?"调整模块、环境或搜索条件后再试。":"新建一个 HTTP 接口，维护地址、参数、请求头、请求体与版本。")+'</span>'+(interfaceCanManage()&&!state.interfaces.length?'<button class="button primary" type="button" data-interface-empty-create>＋ 新建第一个接口</button>':'')+'</div></td></tr>';
}
function renderInterfaceEnvironments(){
  $("#interfaceEnvironmentList").innerHTML=state.interfaceEnvironments.map(function(item){
    const actions=interfaceCanManage()?'<span class="interface-row-actions"><button type="button" data-interface-environment-edit="'+esc(item.id)+'">编辑</button><button class="danger-text" type="button" data-interface-environment-delete="'+esc(item.id)+'">删除</button></span>':'';
    return '<article class="interface-environment-card"><div><span class="account-state active"><i></i>'+(item.is_default?'默认环境':'可用环境')+'</span><h4>'+esc(item.name)+'</h4><code>'+esc(item.base_url||"未配置 Base URL")+'</code><p>'+esc(item.description||"暂无环境说明")+'</p></div><dl><div><dt>环境变量</dt><dd>'+Number(item.variable_count||0)+'</dd></div><div><dt>关联接口</dt><dd>'+Number(item.asset_count||0)+'</dd></div></dl>'+actions+'</article>';
  }).join("")||'<div class="interface-empty-state"><strong>尚未配置运行环境</strong><span>创建环境后可集中维护 Base URL 与环境变量。</span></div>';
}
function renderInterfaceVariables(){
  $("#interfaceVariableBody").innerHTML=state.interfaceVariables.map(function(item){
    const actions=interfaceCanManage()?'<span class="interface-table-actions"><button class="text-button" type="button" data-interface-variable-edit="'+esc(item.id)+'">编辑</button><button class="text-button danger-text" type="button" data-interface-variable-delete="'+esc(item.id)+'">删除</button></span>':'';
    const valueState=item.is_secret?(item.has_secret?'密文已保存':'尚未设置'):esc(item.value||"空值");
    return '<tr><td><code>'+esc(item.variable_key)+'</code></td><td>'+esc(item.environment_name||"项目通用")+'</td><td><span class="role-badge '+(item.is_secret?'platform':'standard')+'">'+(item.is_secret?'密钥':'普通变量')+'</span></td><td>'+valueState+'</td><td>'+esc(item.description||"—")+'</td><td>'+actions+'</td></tr>';
  }).join("")||'<tr><td colspan="6" class="interface-empty">暂无变量</td></tr>';
}
function interfaceScenarioStatus(status){
  const labels={queued:"排队中",succeeded:"通过",passed:"通过",failed:"未通过",interrupted:"已停止",running:"执行中",skipped:"已跳过"};
  return '<span class="interface-scenario-status '+esc(status||"unknown")+'"><i></i>'+esc(labels[status]||"尚未执行")+'</span>';
}
function renderInterfaceScenarios(){
  const selected=new Set(state.selectedInterfaceScenarioIds);
  $("#interfaceScenarioCount").textContent=state.interfaceScenarios.length;
  $("#interfaceScenarioSelectedCount").textContent=selected.size;
  $("#batchRunInterfaceScenariosButton").disabled=!selected.size||!interfaceCanExecute();
  $("#interfaceScenarioSelectAll").checked=Boolean(state.interfaceScenarios.length&&selected.size===state.interfaceScenarios.length);
  $("#interfaceScenarioSelectAll").indeterminate=Boolean(selected.size&&selected.size<state.interfaceScenarios.length);
  $("#interfaceScenarioBody").innerHTML=state.interfaceScenarios.map(function(item){
    let actions=interfaceCanExecute()?'<button class="text-button interface-debug-action" type="button" data-interface-scenario-run="'+esc(item.id)+'">运行</button>':'';
    actions+='<button class="text-button" type="button" data-interface-scenario-history="'+esc(item.id)+'">结果</button>';
    if(interfaceCanManage())actions+='<button class="text-button" type="button" data-interface-scenario-edit="'+esc(item.id)+'">编辑</button><button class="text-button danger-text" type="button" data-interface-scenario-delete="'+esc(item.id)+'">删除</button>';
    return '<tr><td><input type="checkbox" data-interface-scenario-select="'+esc(item.id)+'" '+(selected.has(item.id)?'checked':'')+' aria-label="选择 '+esc(item.name)+'"></td><td><strong>'+esc(item.name)+'</strong><small class="table-subline">'+esc(item.description||"暂无场景说明")+'</small></td><td>'+esc(item.environment_name||"跟随接口")+'</td><td>'+Number(item.step_count||0)+' 步<small class="table-subline">'+Object.keys(item.parameters||{}).length+' 个用例参数</small></td><td>'+(item.last_run_status?interfaceScenarioStatus(item.last_run_status)+'<small class="table-subline">'+esc(fmtTime(item.last_run_at))+'</small>':'<span class="muted">尚未执行</span>')+'</td><td><span class="interface-table-actions">'+actions+'</span></td></tr>';
  }).join("")||'<tr><td colspan="6" class="interface-empty"><div class="interface-catalog-empty"><i>FLOW</i><strong>还没有接口场景</strong><span>创建场景后即可编排接口步骤、提取变量、配置断言并生成执行报告。</span>'+(interfaceCanManage()?'<button class="button primary" type="button" data-interface-scenario-empty-create>＋ 新建第一个场景</button>':'')+'</div></td></tr>';
}
function renderInterfaceScenarioRuns(){
  $("#interfaceScenarioRunCount").textContent=state.interfaceScenarioRuns.length+" 条";
  $("#interfaceScenarioRunBody").innerHTML=state.interfaceScenarioRuns.map(function(run){
    const summary=run.summary||{};
    const reports=(run.docx_path?'<a class="text-button" href="/api/interface-scenario-runs/'+encodeURIComponent(run.id)+'/download/docx">DOCX</a>':'')+(run.pdf_path?'<a class="text-button" href="/api/interface-scenario-runs/'+encodeURIComponent(run.id)+'/download/pdf">PDF</a>':'');
    return '<tr><td>'+esc(fmtTime(run.created_at))+'</td><td><button class="text-button" type="button" data-interface-scenario-run-detail="'+esc(run.id)+'">'+esc(run.scenario_name)+'</button></td><td>'+interfaceScenarioStatus(run.status)+'</td><td>'+Number(summary.passed_steps||0)+' / '+Number(summary.total_steps||0)+' 通过</td><td>'+Number(summary.elapsed_ms||0).toFixed(1)+' ms</td><td><span class="interface-table-actions">'+(reports||'<span class="muted">未生成</span>')+'</span></td></tr>';
  }).join("")||'<tr><td colspan="6" class="interface-empty">暂无场景执行记录</td></tr>';
}
function interfaceScenarioEnvironmentOptions(selected){
  selected=selected||"";
  return '<option value="" '+(!selected?'selected':'')+'>跟随场景 / 接口</option>'+state.interfaceEnvironments.map(function(item){return '<option value="'+esc(item.id)+'" '+(item.id===selected?'selected':'')+'>'+esc(item.name)+'</option>';}).join("");
}
function interfaceScenarioAssetOptions(selected){return '<option value="">请选择接口</option>'+state.interfaces.map(function(item){return '<option value="'+esc(item.id)+'" '+(item.id===selected?'selected':'')+'>'+esc(item.method)+' · '+esc(item.name)+' · '+esc(interfaceTargetUrl(item))+'</option>';}).join("");}
function interfaceScenarioExtractionRow(item){
  item=item||{};
  const source=item.source||"json";
  return '<div class="interface-scenario-rule-row extraction"><input data-scenario-extraction-name value="'+esc(item.name||"")+'" placeholder="变量名，例如 ORDER_ID"><select data-scenario-rule-source><option value="json" '+(source==="json"?'selected':'')+'>JSON 路径</option><option value="header" '+(source==="header"?'selected':'')+'>响应头</option><option value="body" '+(source==="body"?'selected':'')+'>正文正则</option><option value="status" '+(source==="status"?'selected':'')+'>状态码</option><option value="elapsed_ms" '+(source==="elapsed_ms"?'selected':'')+'>响应耗时</option></select><input data-scenario-rule-expression value="'+esc(item.expression||"")+'" placeholder="$.data.id / Header 名称 / 正则"><label class="interface-scenario-required"><input data-scenario-extraction-required type="checkbox" '+(item.required===false?'':'checked')+'><span>必需</span></label><button type="button" data-scenario-rule-delete aria-label="删除提取规则">×</button></div>';
}
function interfaceScenarioAssertionRow(item){
  item=item||{};const source=item.source||"status",operator=item.operator||"between";const expected=item.expected==null?"":(typeof item.expected==="string"?item.expected:JSON.stringify(item.expected));
  const operators=[['equals','等于'],['not_equals','不等于'],['contains','包含'],['not_contains','不包含'],['exists','存在'],['not_exists','不存在'],['greater_than','大于'],['greater_or_equal','大于等于'],['less_than','小于'],['less_or_equal','小于等于'],['matches','匹配正则'],['between','区间内']];
  return '<div class="interface-scenario-rule-row assertion"><select data-scenario-rule-source><option value="status" '+(source==="status"?'selected':'')+'>状态码</option><option value="json" '+(source==="json"?'selected':'')+'>JSON 路径</option><option value="header" '+(source==="header"?'selected':'')+'>响应头</option><option value="body" '+(source==="body"?'selected':'')+'>响应正文</option><option value="elapsed_ms" '+(source==="elapsed_ms"?'selected':'')+'>响应耗时</option></select><input data-scenario-rule-expression value="'+esc(item.expression||"")+'" placeholder="JSON 路径或 Header 名称"><select data-scenario-assertion-operator>'+operators.map(function(entry){return '<option value="'+entry[0]+'" '+(operator===entry[0]?'selected':'')+'>'+entry[1]+'</option>';}).join("")+'</select><input data-scenario-assertion-expected value="'+esc(expected)+'" placeholder="期望值；区间如 [200,299]"><button type="button" data-scenario-rule-delete aria-label="删除断言">×</button></div>';
}
function interfaceScenarioStepCard(step,index){
  step=step||{};const extractions=Array.isArray(step.extractions)?step.extractions:[],assertions=Array.isArray(step.assertions)?step.assertions:[];
  return '<article class="interface-scenario-step" data-interface-scenario-step data-step-id="'+esc(step.id||"")+'"><header><span>'+(index+1)+'</span><div><strong>'+(esc(step.name)||"未命名步骤")+'</strong><small>按当前顺序执行</small></div><div class="interface-scenario-step-actions"><button type="button" data-scenario-step-move="up" aria-label="上移">↑</button><button type="button" data-scenario-step-move="down" aria-label="下移">↓</button><button class="danger-text" type="button" data-scenario-step-delete>删除</button></div></header><div class="interface-scenario-step-meta"><label class="field"><span>接口 *</span><select data-scenario-step-asset>'+interfaceScenarioAssetOptions(step.asset_id||"")+'</select></label><label class="field"><span>步骤名称</span><input data-scenario-step-name maxlength="120" value="'+esc(step.name||"")+'" placeholder="默认使用接口名称"></label><label class="field"><span>环境覆盖</span><select data-scenario-step-environment>'+interfaceScenarioEnvironmentOptions(step.environment_id||"")+'</select></label><label class="switch-row"><input data-scenario-step-continue type="checkbox" '+(step.continue_on_failure?'checked':'')+'><span><b>失败后继续</b><small>默认遇到失败即停止后续步骤。</small></span></label></div><div class="interface-scenario-rules"><section><header><div><strong>变量提取</strong><small>保存响应值，供后续步骤使用</small></div><button type="button" data-scenario-rule-add="extraction">＋ 添加提取</button></header><div data-scenario-extractions>'+extractions.map(interfaceScenarioExtractionRow).join("")+'</div></section><section><header><div><strong>响应断言</strong><small>未配置时默认校验 HTTP 200～399</small></div><button type="button" data-scenario-rule-add="assertion">＋ 添加断言</button></header><div data-scenario-assertions>'+assertions.map(interfaceScenarioAssertionRow).join("")+'</div></section></div></article>';
}
function renderInterfaceScenarioSteps(steps){
  const items=Array.isArray(steps)?steps:[];
  $("#interfaceScenarioSteps").innerHTML=items.map(interfaceScenarioStepCard).join("")||'<div class="interface-scenario-step-empty"><strong>尚未添加步骤</strong><span>添加第一个接口步骤后，再配置提取变量和断言。</span></div>';
}
function closeInterfaceScenarioEditor(){$("#interfaceScenarioEditor").classList.add("hidden");$("#interfaceScenarioCatalog").classList.remove("hidden");setInterfaceScenarioError("");}
function setInterfaceScenarioError(message){const node=$("#interfaceScenarioError");node.textContent=message||"";node.classList.toggle("hidden",!message);}
function openInterfaceScenarioEditor(id){
  const item=state.interfaceScenarios.find(function(scenario){return scenario.id===id;});
  $("#interfaceScenarioForm").reset();$("#interfaceScenarioId").value=item?item.id:"";$("#interfaceScenarioName").value=item?item.name:"";$("#interfaceScenarioEnvironment").value=item?item.environment_id:"";$("#interfaceScenarioDescription").value=item?item.description:"";$("#interfaceScenarioParameters").value=JSON.stringify((item&&item.parameters)||{},null,2);$("#interfaceScenarioEditorTitle").textContent=item?item.name:"新建场景";renderInterfaceScenarioSteps(item?item.steps:[]);setInterfaceScenarioError("");$("#interfaceScenarioCatalog").classList.add("hidden");$("#interfaceScenarioEditor").classList.remove("hidden");$("#interfaceScenarioEditor").scrollTop=0;
}
function addInterfaceScenarioStep(){
  const steps=$$("[data-interface-scenario-step]",$("#interfaceScenarioSteps")).map(readInterfaceScenarioStepLoose);steps.push({asset_id:"",name:"",environment_id:"",continue_on_failure:false,extractions:[],assertions:[]});renderInterfaceScenarioSteps(steps);const cards=$$("[data-interface-scenario-step]",$("#interfaceScenarioSteps"));if(cards.length)cards[cards.length-1].scrollIntoView({behavior:"smooth",block:"center"});
}
function readInterfaceScenarioStepLoose(card){
  if(!card||!card.matches)return {};
  return {id:card.dataset.stepId||"",asset_id:card.querySelector("[data-scenario-step-asset]").value,name:card.querySelector("[data-scenario-step-name]").value.trim(),environment_id:card.querySelector("[data-scenario-step-environment]").value,enabled:true,continue_on_failure:card.querySelector("[data-scenario-step-continue]").checked,extractions:$$("[data-scenario-extractions] .interface-scenario-rule-row",card).map(function(row){return {name:row.querySelector("[data-scenario-extraction-name]").value.trim(),source:row.querySelector("[data-scenario-rule-source]").value,expression:row.querySelector("[data-scenario-rule-expression]").value.trim(),required:row.querySelector("[data-scenario-extraction-required]").checked};}),assertions:$$("[data-scenario-assertions] .interface-scenario-rule-row",card).map(function(row){return {source:row.querySelector("[data-scenario-rule-source]").value,expression:row.querySelector("[data-scenario-rule-expression]").value.trim(),operator:row.querySelector("[data-scenario-assertion-operator]").value,expected:row.querySelector("[data-scenario-assertion-expected]").value};})};
}
function parseInterfaceScenarioExpected(value){const text=String(value||"").trim();if(!text)return "";try{return JSON.parse(text);}catch(_error){return text;}}
function readInterfaceScenarioPayload(){
  let parameters;try{parameters=JSON.parse($("#interfaceScenarioParameters").value.trim()||"{}");}catch(error){throw new Error("用例参数不是合法 JSON："+error.message);}if(!parameters||Array.isArray(parameters)||typeof parameters!=="object")throw new Error("用例参数必须是 JSON 对象");
  const steps=$$("[data-interface-scenario-step]",$("#interfaceScenarioSteps")).map(function(card,index){const step=readInterfaceScenarioStepLoose(card);if(!step.asset_id)throw new Error("第 "+(index+1)+" 个步骤尚未选择接口");step.extractions.forEach(function(item){if(!item.name)throw new Error("第 "+(index+1)+" 个步骤存在未填写变量名的提取规则");});step.assertions.forEach(function(item){item.expected=parseInterfaceScenarioExpected(item.expected);});return step;});if(!steps.length)throw new Error("场景至少需要一个接口步骤");
  return {name:$("#interfaceScenarioName").value.trim(),description:$("#interfaceScenarioDescription").value.trim(),environment_id:$("#interfaceScenarioEnvironment").value,parameters:parameters,steps:steps};
}
async function saveInterfaceScenario(runAfter){
  const id=$("#interfaceScenarioId").value;setInterfaceScenarioError("");let payload;try{payload=readInterfaceScenarioPayload();if(!payload.name)throw new Error("场景名称不能为空");}catch(error){setInterfaceScenarioError(error.message);return null;}
  const runButton=$("#saveAndRunInterfaceScenarioButton");if(runAfter){runButton.disabled=true;runButton.textContent="保存中…";}
  try{const item=await api(id?"/api/interface-scenarios/"+encodeURIComponent(id):"/api/interface-scenarios",{method:id?"PUT":"POST",body:payload});toast(id?"场景已更新":"场景已创建");await loadInterfaces();openInterfaceScenarioEditor(item.id);if(runAfter)await runInterfaceScenario(item.id);return item;}catch(error){setInterfaceScenarioError("保存失败："+error.message);toast("场景保存失败："+error.message,true);return null;}finally{if(runAfter){runButton.disabled=false;runButton.textContent="保存并运行";}}
}
async function submitInterfaceScenario(event){event.preventDefault();await saveInterfaceScenario(false);}
async function deleteInterfaceScenario(id){if(!window.confirm("确认删除这个接口场景？历史执行记录和报告仍会保留。"))return;try{await api("/api/interface-scenarios/"+encodeURIComponent(id),{method:"DELETE"});toast("接口场景已删除");await loadInterfaces();}catch(error){toast("场景删除失败："+error.message,true);}}
function renderInterfaceScenarioRunDetail(run){
  const result=run.result||{},summary=run.summary||{},steps=result.steps||[],statusLabels={queued:"排队中",running:"执行中",succeeded:"通过",failed:"未通过",interrupted:"已停止"};$("#interfaceScenarioRunTitle").textContent=(run.scenario_name||((result.scenario||{}).name)||"接口场景")+" · "+(statusLabels[run.status]||run.status||"未知状态");
  const reports=(run.docx_path?'<a class="button secondary" href="/api/interface-scenario-runs/'+encodeURIComponent(run.id)+'/download/docx">下载 DOCX</a>':'')+(run.pdf_path?'<a class="button secondary" href="/api/interface-scenario-runs/'+encodeURIComponent(run.id)+'/download/pdf">下载 PDF</a>':'');
  $("#interfaceScenarioRunDetail").innerHTML='<div class="interface-scenario-result-summary"><article><span>最终结果</span>'+interfaceScenarioStatus(run.status)+'</article><article><span>通过步骤</span><strong>'+Number(summary.passed_steps||0)+' / '+Number(summary.total_steps||0)+'</strong></article><article><span>失败 / 跳过</span><strong>'+Number(summary.failed_steps||0)+' / '+Number(summary.skipped_steps||0)+'</strong></article><article><span>总耗时</span><strong>'+Number(summary.elapsed_ms||0).toFixed(1)+' ms</strong></article></div>'+(result.error?'<div class="interface-form-error">'+esc(result.error)+'</div>':'')+'<div class="interface-scenario-result-steps">'+steps.map(function(step){const response=((step.exchange||{}).response)||{};const extracts=(step.extractions||[]).map(function(item){return '<li class="'+(item.success?'passed':'failed')+'"><b>'+(item.success?'通过':'失败')+'</b><span>'+esc(item.name||"")+' · '+esc(item.value||item.message||"—")+'</span></li>';}).join("");const assertions=(step.assertions||[]).map(function(item){return '<li class="'+(item.passed?'passed':'failed')+'"><b>'+(item.passed?'通过':'失败')+'</b><span>'+esc(item.source||"")+' '+esc(item.expression||"")+' · 期望 '+esc(typeof item.expected==="string"?item.expected:JSON.stringify(item.expected))+' · 实际 '+esc(typeof item.actual==="string"?item.actual:JSON.stringify(item.actual))+'</span></li>';}).join("");return '<article class="interface-scenario-result-step '+esc(step.status||"")+'"><header><span>'+Number(step.index||0)+'</span><div><strong>'+esc(step.name||"未命名步骤")+'</strong><small>'+esc(((step.asset||{}).method)||"")+' '+esc(((step.asset||{}).path)||"")+'</small></div>'+interfaceScenarioStatus(step.status)+'</header><div class="interface-scenario-result-meta"><span>HTTP '+esc(response.status_code==null?"—":response.status_code)+'</span><span>'+Number(step.elapsed_ms||0).toFixed(1)+' ms</span><span>接口 v'+Number(((step.asset||{}).version)||1)+'</span></div>'+(step.message?'<p class="interface-scenario-result-error">'+esc(step.message)+'</p>':'')+(extracts?'<section><h5>变量提取</h5><ul>'+extracts+'</ul></section>':'')+(assertions?'<section><h5>断言结果</h5><ul>'+assertions+'</ul></section>':'')+(response.body!=null?'<details><summary>查看响应摘要</summary><pre>'+esc(prettyInterfaceResponseBody(response.body,response.content_type))+'</pre></details>':'')+'</article>';}).join("")+'</div><div class="interface-scenario-report-actions">'+(!interfaceScenarioTerminal(run.status)?'<button class="button danger" type="button" data-interface-scenario-run-stop="'+esc(run.id)+'">停止执行</button>':'')+reports+'</div>';openInterfaceDialog("interfaceScenarioRunDialog");
}
async function showInterfaceScenarioRun(id){$("#interfaceScenarioRunTitle").textContent="正在加载执行结果…";$("#interfaceScenarioRunDetail").innerHTML='<div class="interface-empty-state"><span>正在读取场景快照与步骤结果…</span></div>';openInterfaceDialog("interfaceScenarioRunDialog");try{const run=await api("/api/interface-scenario-runs/"+encodeURIComponent(id));renderInterfaceScenarioRunDetail(run);}catch(error){$("#interfaceScenarioRunDetail").innerHTML='<div class="interface-form-error">'+esc(error.message)+'</div>';}}
async function stopInterfaceScenarioRun(id){try{const run=await api("/api/interface-scenario-runs/"+encodeURIComponent(id)+"/stop",{method:"POST"});renderInterfaceScenarioRunDetail(run);toast(run.status==="interrupted"?"已停止排队任务":"已发送停止请求");}catch(error){toast("停止失败："+error.message,true);}}
function interfaceScenarioTerminal(status){return ["succeeded","failed","interrupted"].includes(status);}
function interfaceScenarioWait(ms){return new Promise(function(resolve){setTimeout(resolve,ms);});}
async function verifyInterfaceWorker(checks){if(checks!==12)return;const health=await api("/health");if(health.task_execution_mode==="external"&&health.task_queue&&health.task_queue.worker_available===false)throw new Error("独立 Worker 尚未连接，任务已安全保留在队列中；请启动 python -m auto_test.worker");}
async function waitInterfaceScenarioRun(run){let current=run,checks=0;while(current&&!interfaceScenarioTerminal(current.status)){renderInterfaceScenarioRunDetail(current);await interfaceScenarioWait(750);current=await api("/api/interface-scenario-runs/"+encodeURIComponent(current.id));checks+=1;await verifyInterfaceWorker(checks);}return current;}
async function waitInterfaceScenarioBatch(batchId){let current={batch_id:batchId,runs:[]},checks=0;while(true){current=await api("/api/interface-scenario-batches/"+encodeURIComponent(batchId));if((current.runs||[]).length&&(current.runs||[]).every(function(run){return interfaceScenarioTerminal(run.status);}))return current;await interfaceScenarioWait(900);checks+=1;await verifyInterfaceWorker(checks);}}
async function runInterfaceScenario(id){const scenario=state.interfaceScenarios.find(function(item){return item.id===id;});$("#interfaceScenarioRunTitle").textContent=(scenario?scenario.name:"接口场景")+" · 排队中";$("#interfaceScenarioRunDetail").innerHTML='<div class="interface-empty-state"><strong>场景已进入后台队列</strong><span>页面请求已经释放，Worker 将执行请求、变量提取、断言和报告生成。</span></div>';openInterfaceDialog("interfaceScenarioRunDialog");try{const queued=await api("/api/interface-scenarios/"+encodeURIComponent(id)+"/execute",{method:"POST",body:{parameters:{}}});const run=await waitInterfaceScenarioRun(queued);await loadInterfaces();renderInterfaceScenarioRunDetail(run);toast(run.status==="succeeded"?"接口场景执行通过":run.status==="interrupted"?"接口场景已停止":"接口场景执行未通过",run.status!=="succeeded");return run;}catch(error){$("#interfaceScenarioRunDetail").innerHTML='<div class="interface-form-error">'+esc(error.message)+'</div>';toast("场景执行失败："+error.message,true);return null;}}
async function batchRunInterfaceScenarios(){const ids=state.selectedInterfaceScenarioIds.slice();if(!ids.length)return;const button=$("#batchRunInterfaceScenariosButton");button.disabled=true;button.textContent="提交队列中…";try{const queued=await api("/api/interface-scenarios/batch-execute",{method:"POST",body:{scenario_ids:ids,parameters:{},concurrency:Math.min(3,ids.length)}});toast("已提交 "+(queued.runs||[]).length+" 个场景，后台正在执行");button.textContent="后台执行中…";const result=await waitInterfaceScenarioBatch(queued.batch_id);await loadInterfaces();const succeeded=(result.runs||[]).filter(function(run){return run.status==="succeeded";}).length;toast("批量执行完成："+succeeded+" / "+(result.runs||[]).length+" 个场景通过",succeeded!==(result.runs||[]).length);if((result.runs||[]).length)await showInterfaceScenarioRun(result.runs[0].id);}catch(error){toast("批量执行失败："+error.message,true);}finally{button.disabled=!state.selectedInterfaceScenarioIds.length;button.textContent="批量运行";}}
function renderInterfaceWorkspace(){
  $("#interfaceAssetCount").textContent=state.interfaces.length;
  $("#interfaceModuleCount").textContent=state.interfaceModules.length;
  $("#interfaceEnvironmentCount").textContent=state.interfaceEnvironments.length;
  $("#interfacePublishedCount").textContent=state.interfaces.filter(function(item){return item.status==="published";}).length;
  syncInterfaceOptions();syncInterfacePermissions();renderInterfaceModules();renderInterfaceAssets();renderInterfaceEnvironments();renderInterfaceVariables();renderInterfaceScenarios();renderInterfaceScenarioRuns();
}
function selectInterfaceModule(id){state.selectedInterfaceModuleId=String(id||"");closeInterfaceEditor();renderInterfaceModules();renderInterfaceAssets();}
function openCurrentProjectEditor(){
  const project=((state.identity&&state.identity.projects)||[]).find(function(item){return item.id===state.projectId;})||(state.identity&&state.identity.current_project)||{};
  if(!project.id){toast("当前业务项目不存在",true);return;}
  $("#currentProjectForm").reset();$("#currentProjectId").value=project.id;$("#currentProjectKey").value=project.project_key||"";$("#currentProjectName").value=project.name||"";$("#currentProjectDescription").value=project.description||"";openInterfaceDialog("currentProjectDialog");
}
async function submitCurrentProject(event){
  event.preventDefault();const projectId=$("#currentProjectId").value;const payload={name:$("#currentProjectName").value.trim(),description:$("#currentProjectDescription").value.trim()};
  try{await api("/api/identity/projects/"+encodeURIComponent(projectId),{method:"PATCH",body:payload});const identity=await api("/api/auth/status");state.identity=identity;state.csrfToken=identity.csrf_token||state.csrfToken;syncIdentityChrome();closeInterfaceDialog("currentProjectDialog");renderInterfaceWorkspace();toast("业务项目已更新");}
  catch(error){toast("项目更新失败："+error.message,true);}
}
function openInterfaceModuleEditor(id){
  const item=state.interfaceModules.find(function(module){return module.id===id;});
  $("#interfaceModuleForm").reset();$("#interfaceModuleId").value=item?item.id:"";$("#interfaceModuleName").value=item?item.name:"";$("#interfaceModuleDescription").value=item?item.description:"";$("#interfaceModuleOrder").value=item?Number(item.sort_order||0):state.interfaceModules.length*10;$("#interfaceModuleDialogTitle").textContent=item?"编辑模块":"新建模块";openInterfaceDialog("interfaceModuleDialog");
}
async function submitInterfaceModule(event){
  event.preventDefault();const id=$("#interfaceModuleId").value;const payload={name:$("#interfaceModuleName").value.trim(),description:$("#interfaceModuleDescription").value.trim(),sort_order:Number($("#interfaceModuleOrder").value||0)};
  try{await api(id?"/api/interface-modules/"+encodeURIComponent(id):"/api/interface-modules",{method:id?"PUT":"POST",body:payload});closeInterfaceDialog("interfaceModuleDialog");toast(id?"模块已更新":"模块已创建");await loadInterfaces();}catch(error){toast("模块保存失败："+error.message,true);}
}
async function deleteInterfaceModule(id){if(!window.confirm("确认删除这个接口模块？"))return;try{await api("/api/interface-modules/"+encodeURIComponent(id),{method:"DELETE"});if(state.selectedInterfaceModuleId===id)state.selectedInterfaceModuleId="";toast("模块已删除");await loadInterfaces();}catch(error){toast("模块删除失败："+error.message,true);}}
function openInterfaceEnvironmentEditor(id){
  const item=state.interfaceEnvironments.find(function(environment){return environment.id===id;});
  $("#interfaceEnvironmentForm").reset();$("#interfaceEnvironmentId").value=item?item.id:"";$("#interfaceEnvironmentName").value=item?item.name:"";$("#interfaceEnvironmentBaseUrl").value=item?item.base_url:"";$("#interfaceEnvironmentDescription").value=item?item.description:"";$("#interfaceEnvironmentDefault").checked=Boolean(item&&item.is_default);$("#interfaceEnvironmentDialogTitle").textContent=item?"编辑环境":"新建环境";openInterfaceDialog("interfaceEnvironmentDialog");
}
async function submitInterfaceEnvironment(event){
  event.preventDefault();const id=$("#interfaceEnvironmentId").value;const payload={name:$("#interfaceEnvironmentName").value.trim(),base_url:$("#interfaceEnvironmentBaseUrl").value.trim(),description:$("#interfaceEnvironmentDescription").value.trim(),is_default:$("#interfaceEnvironmentDefault").checked};
  try{await api(id?"/api/interface-environments/"+encodeURIComponent(id):"/api/interface-environments",{method:id?"PUT":"POST",body:payload});closeInterfaceDialog("interfaceEnvironmentDialog");toast(id?"环境已更新":"环境已创建");await loadInterfaces();}catch(error){toast("环境保存失败："+error.message,true);}
}
async function deleteInterfaceEnvironment(id){if(!window.confirm("确认删除这个运行环境？"))return;try{await api("/api/interface-environments/"+encodeURIComponent(id),{method:"DELETE"});toast("环境已删除");await loadInterfaces();}catch(error){toast("环境删除失败："+error.message,true);}}
function syncInterfaceVariableSecret(){const secret=$("#interfaceVariableSecret").checked;$("#interfaceVariableValue").type=secret?"password":"text";$("#interfaceVariableValueLabel").textContent=secret?"密钥值":"变量值";}
function openInterfaceVariableEditor(id){
  const item=state.interfaceVariables.find(function(variable){return variable.id===id;});
  $("#interfaceVariableForm").reset();$("#interfaceVariableId").value=item?item.id:"";$("#interfaceVariableKey").value=item?item.variable_key:"";$("#interfaceVariableEnvironment").value=item?item.environment_id:"";$("#interfaceVariableSecret").checked=Boolean(item&&item.is_secret);$("#interfaceVariableValue").value=item&& !item.is_secret?item.value:"";$("#interfaceVariableValue").placeholder=item&&item.is_secret&&item.has_secret?"留空保留已保存密钥":"请输入变量值";$("#interfaceVariableDescription").value=item?item.description:"";$("#interfaceVariableDialogTitle").textContent=item?"编辑变量":"新建变量";syncInterfaceVariableSecret();openInterfaceDialog("interfaceVariableDialog");
}
async function submitInterfaceVariable(event){
  event.preventDefault();const id=$("#interfaceVariableId").value;const payload={environment_id:$("#interfaceVariableEnvironment").value,key:$("#interfaceVariableKey").value.trim(),value:$("#interfaceVariableValue").value,is_secret:$("#interfaceVariableSecret").checked,description:$("#interfaceVariableDescription").value.trim()};
  try{await api(id?"/api/interface-variables/"+encodeURIComponent(id):"/api/interface-variables",{method:id?"PUT":"POST",body:payload});$("#interfaceVariableValue").value="";closeInterfaceDialog("interfaceVariableDialog");toast(id?"变量已更新":"变量已创建");await loadInterfaces();}catch(error){toast("变量保存失败："+error.message,true);}
}
async function deleteInterfaceVariable(id){if(!window.confirm("确认删除这个变量？已保存密钥也会一并删除。"))return;try{await api("/api/interface-variables/"+encodeURIComponent(id),{method:"DELETE"});toast("变量已删除");await loadInterfaces();}catch(error){toast("变量删除失败："+error.message,true);}}
function interfaceTargetUrl(item){
  const path=String((item&&item.path)||"").trim();
  if(/^https?:\/\//i.test(path))return path;
  return String((item&&item.base_url)||"").replace(/\/$/,"")+path;
}
function interfaceKeyValueRow(kind,key,value){
  return '<div class="interface-kv-row"><input data-interface-row-key="'+kind+'" value="'+esc(key||"")+'" placeholder="参数名"><input data-interface-row-value="'+kind+'" value="'+esc(value==null?"":(typeof value==="string"?value:JSON.stringify(value)))+'" placeholder="参数值"><button type="button" data-interface-row-delete aria-label="删除此行">×</button></div>';
}
function renderInterfaceKeyValueRows(kind,values){
  const rows=values&&typeof values==="object"&&!Array.isArray(values)?Object.entries(values):[];
  $(kind==="query"?"#interfaceQueryRows":"#interfaceHeaderRows").innerHTML=(rows.length?rows:[["",""]]).map(function(entry){return interfaceKeyValueRow(kind,entry[0],entry[1]);}).join("");
  updateInterfaceRequestCounts();
}
function addInterfaceKeyValueRow(kind){
  const container=$(kind==="query"?"#interfaceQueryRows":"#interfaceHeaderRows");
  container.insertAdjacentHTML("beforeend",interfaceKeyValueRow(kind,"",""));
  const inputs=container.querySelectorAll('[data-interface-row-key="'+kind+'"]');if(inputs.length)inputs[inputs.length-1].focus();
}
function readInterfaceKeyValueRows(kind){
  const result={};
  $$('.interface-kv-row').filter(function(row){return row.querySelector('[data-interface-row-key="'+kind+'"]');}).forEach(function(row){
    const key=row.querySelector('[data-interface-row-key="'+kind+'"]').value.trim();const value=row.querySelector('[data-interface-row-value="'+kind+'"]').value;
    if(!key)return;if(Object.prototype.hasOwnProperty.call(result,key))throw new Error((kind==="query"?"Query 参数":"请求头")+'存在重复名称：'+key);result[key]=value;
  });
  return result;
}
function updateInterfaceRequestCounts(){
  $("#interfaceQueryCount").textContent=$$('#interfaceQueryRows [data-interface-row-key="query"]').filter(function(input){return input.value.trim();}).length;
  $("#interfaceHeaderCount").textContent=$$('#interfaceHeaderRows [data-interface-row-key="headers"]').filter(function(input){return input.value.trim();}).length;
}
function activateInterfaceRequestTab(name){
  $$('[data-interface-request-tab]').forEach(function(button){const active=button.dataset.interfaceRequestTab===name;button.classList.toggle("active",active);button.setAttribute("aria-selected",String(active));});
  $$('[data-interface-request-pane]').forEach(function(pane){pane.classList.toggle("active",pane.dataset.interfaceRequestPane===name);});
}
function activateInterfaceResponseTab(name){
  $$('[data-interface-response-tab]').forEach(function(button){const active=button.dataset.interfaceResponseTab===name;button.classList.toggle("active",active);button.setAttribute("aria-selected",String(active));});
  $$('[data-interface-response-pane]').forEach(function(pane){pane.classList.toggle("active",pane.dataset.interfaceResponsePane===name);});
}
function setInterfaceAssetError(message){const node=$("#interfaceAssetError");node.textContent=message||"";node.classList.toggle("hidden",!message);}
function syncInterfaceTargetHint(){
  const environment=state.interfaceEnvironments.find(function(item){return item.id===$("#interfaceAssetEnvironment").value;});
  $("#interfaceAssetTargetHint").textContent=environment&&environment.base_url?"当前环境 Base URL："+environment.base_url+"。可填写 / 开头的相对路径，也可直接填写完整 URL。":"可直接填写完整 http(s) URL；绑定带 Base URL 的环境后也可填写以 / 开头的相对路径。";
}
function setInterfaceCurlPasteStatus(message,stateName){
  const node=$("#interfaceCurlPasteStatus");if(!node)return;node.textContent=message||"粘贴 cURL，自动拆解方法、参数、请求头和请求体";node.dataset.state=stateName||"idle";
}
function syncInterfaceBodyEditor(){
  const type=$("#interfaceAssetBodyType").value||"none",body=$("#interfaceAssetBody");
  const labels={none:"请求体",json:"JSON 请求体",multipart:"表单字段（JSON 对象）",urlencoded:"表单字段（JSON 对象）",raw:"原始请求体"};
  const hints={none:"当前请求不发送请求体。",json:"请输入合法 JSON；可使用 {{VARIABLE_NAME}} 引用项目变量或环境变量。",multipart:"每个对象字段会作为 multipart/form-data 文本字段发送；不会读取 @file 本机路径。",urlencoded:"每个对象字段会按 application/x-www-form-urlencoded 编码发送。",raw:"按原始文本发送；Content-Type 可在请求头中设置。"};
  $("#interfaceAssetBodyLabel").textContent=labels[type]||labels.none;$("#interfaceAssetBodyHint").textContent=hints[type]||hints.none;body.disabled=type==="none";
  body.placeholder=type==="raw"?"原始请求文本":type==="none"?"无请求体":'{"text":"hello"}';
  if(type!=="none"&&type!=="raw"&&!body.value.trim())body.value="{}";
}
function readInterfaceRequestBody(){
  const type=$("#interfaceAssetBodyType").value||"none",text=$("#interfaceAssetBody").value;
  if(type==="none")return {type:type,body:null};
  if(type==="raw")return {type:type,body:text};
  const body=JSON.parse(text.trim()||"{}");
  if((type==="multipart"||type==="urlencoded")&&(!body||Array.isArray(body)||typeof body!=="object"))throw new Error("表单请求体必须是 JSON 对象");
  return {type:type,body:body};
}
function interfaceNameFromTarget(target){
  try{const parsed=new URL(target);const parts=parsed.pathname.split("/").filter(Boolean);return decodeURIComponent(parts[parts.length-1]||parsed.hostname||"新建接口");}catch(_error){return "";}
}
function applyParsedCurlToInterface(parsed){
  $("#interfaceAssetMethod").value=parsed.method||"GET";$("#interfaceAssetPath").value=parsed.target||"";$("#interfaceDebugTimeout").value=Number(parsed.timeout_seconds||30);renderInterfaceKeyValueRows("query",parsed.query||{});renderInterfaceKeyValueRows("headers",parsed.headers||{});
  const bodyType=parsed.body_type||"none";$("#interfaceAssetBodyType").value=bodyType;$("#interfaceAssetBody").value=bodyType==="raw"?String(parsed.body||""):bodyType==="none"?"":JSON.stringify(parsed.body||{},null,2);syncInterfaceBodyEditor();syncInterfaceMethodBadge();
  if(!$("#interfaceAssetName").value.trim())$("#interfaceAssetName").value=interfaceNameFromTarget(parsed.target||"");
  const queryCount=Object.keys(parsed.query||{}).length,headerCount=Object.keys(parsed.headers||{}).length,bodyCount=parsed.body&&typeof parsed.body==="object"&&!Array.isArray(parsed.body)?Object.keys(parsed.body).length:(parsed.body==null?0:1);const details=[parsed.method||"GET",queryCount+" 个 Query",headerCount+" 个请求头",bodyCount+" 个请求体字段"];
  const warnings=Array.isArray(parsed.warnings)?parsed.warnings:[];setInterfaceCurlPasteStatus("已识别："+details.join(" · ")+(warnings.length?"；"+warnings.join("；"):""),warnings.length?"warning":"success");activateInterfaceRequestTab(queryCount?"query":headerCount?"headers":bodyType!=="none"?"body":"query");setInterfaceAssetError("");toast("cURL 已自动填充");
}
async function handleInterfaceCurlPaste(event){
  const text=event.clipboardData&&event.clipboardData.getData("text");if(!/^\s*(?:\$\s*)?curl(?:\.exe)?\s/i.test(String(text||"")))return;
  event.preventDefault();setInterfaceAssetError("");setInterfaceCurlPasteStatus("正在识别 cURL…","loading");
  try{const parsed=await api("/api/interface-assets/parse-curl",{method:"POST",body:{content:text}});applyParsedCurlToInterface(parsed);}catch(error){setInterfaceCurlPasteStatus("cURL 识别失败","error");setInterfaceAssetError("cURL 识别失败："+error.message);}
}
function closeInterfaceEditor(){
  const catalog=$("#interfaceCatalogView"),editor=$("#interfaceEditorView");if(!catalog||!editor)return;
  editor.classList.add("hidden");catalog.classList.remove("hidden");setInterfaceAssetError("");
}
function resetInterfaceDebugResponse(){
  $("#interfaceDebugResponse").dataset.state="empty";$("#interfaceResponseMeta").innerHTML="<span>等待发送</span>";
  $("#interfaceResponseEmpty").classList.remove("hidden");$("#interfaceResponseEmpty").querySelector("strong").textContent="发送请求后在这里查看响应";$("#interfaceResponseEmpty").querySelector("span").textContent="状态码、耗时、大小、响应体和实际请求会集中展示。";
  $("#interfaceResponseBody").textContent="";$("#interfaceResponseBody").classList.add("hidden");$("#interfaceResponseHeaders").textContent="";$("#interfaceActualRequest").textContent="";activateInterfaceResponseTab("body");
}
function syncInterfaceMethodBadge(){const method=$("#interfaceAssetMethod").value||"GET";const badge=$("#interfaceEditorMethodBadge");badge.textContent=method;badge.className=method.toLowerCase();}
function openInterfaceAssetEditor(id){
  const item=state.interfaces.find(function(asset){return asset.id===id;});const defaultEnvironment=state.interfaceEnvironments.find(function(environment){return environment.is_default;});
  const selectedModule=state.selectedInterfaceModuleId==="__ungrouped__"?"":state.selectedInterfaceModuleId;const request=item&&item.request&&typeof item.request==="object"?item.request:{};
  const hasBody=Object.prototype.hasOwnProperty.call(request,"body")&&request.body!==null;const bodyType=request.body_type||(hasBody?"json":"none");const requestBody=hasBody?request.body:null;
  $("#interfaceAssetForm").reset();$("#interfaceAssetId").value=item?item.id:"";$("#interfaceAssetName").value=item?item.name:"";$("#interfaceAssetMethod").value=item?item.method:"GET";$("#interfaceAssetModule").value=item?item.module_id:selectedModule;$("#interfaceAssetEnvironment").value=item?item.environment_id:(defaultEnvironment?defaultEnvironment.id:"");$("#interfaceAssetPath").value=item?item.path:"";$("#interfaceAssetDescription").value=item?item.description:"";$("#interfaceAssetBodyType").value=bodyType;$("#interfaceAssetBody").value=bodyType==="raw"?String(requestBody||""):bodyType==="none"?"":JSON.stringify(requestBody||{},null,2);$("#interfaceAssetPublish").checked=false;$("#interfaceAssetDialogTitle").textContent=item?item.name:"新建接口";renderInterfaceKeyValueRows("query",request.query);renderInterfaceKeyValueRows("headers",request.headers);activateInterfaceRequestTab("query");setInterfaceAssetError("");setInterfaceCurlPasteStatus();syncInterfaceTargetHint();syncInterfaceBodyEditor();syncInterfaceMethodBadge();resetInterfaceDebugResponse();$("#interfaceCatalogView").classList.add("hidden");$("#interfaceEditorView").classList.remove("hidden");$("#interfaceEditorView").scrollTop=0;
}
async function submitInterfaceAsset(event){
  event.preventDefault();const id=$("#interfaceAssetId").value;setInterfaceAssetError("");let requestDefinition={};
  try{const parsedBody=readInterfaceRequestBody();requestDefinition={headers:readInterfaceKeyValueRows("headers"),query:readInterfaceKeyValueRows("query"),body_type:parsedBody.type,body:parsedBody.body};}catch(error){activateInterfaceRequestTab(String(error.message).includes("请求头")?"headers":String(error.message).includes("Query")?"query":"body");setInterfaceAssetError("请求定义无效："+error.message);return;}
  const payload={module_id:$("#interfaceAssetModule").value,environment_id:$("#interfaceAssetEnvironment").value,name:$("#interfaceAssetName").value.trim(),description:$("#interfaceAssetDescription").value.trim(),method:$("#interfaceAssetMethod").value,path:$("#interfaceAssetPath").value.trim(),request:requestDefinition,publish:$("#interfaceAssetPublish").checked};
  try{const item=await api(id?"/api/interface-assets/"+encodeURIComponent(id):"/api/interface-assets",{method:id?"PUT":"POST",body:payload});toast("接口已保存为 v"+item.current_version+(item.status==="published"?"，并已发布":" 草稿"));await loadInterfaces();openInterfaceAssetEditor(item.id);}catch(error){setInterfaceAssetError("保存失败："+error.message);toast("接口保存失败："+error.message,true);}
}
function prettyInterfaceResponseBody(body,contentType){
  const value=String(body||"");if(!value)return "（响应体为空）";
  if(String(contentType||"").toLowerCase().includes("json")||/^[\s]*[\[{]/.test(value)){try{return JSON.stringify(JSON.parse(value),null,2);}catch(_error){return value;}}
  return value;
}
function renderInterfaceDebugResult(result){
  const response=result.response||{},request=result.request||{},status=Number(response.status_code||0),successful=status>=200&&status<400;
  $("#interfaceDebugResponse").dataset.state=successful?"success":"error";
  $("#interfaceResponseMeta").innerHTML='<span class="interface-response-status '+(successful?'success':'error')+'">'+status+' '+esc(response.reason||"")+'</span><span>'+Number(response.elapsed_ms||0).toFixed(1)+' ms</span><span>'+esc(fmtSize(Number(response.size_bytes||0)))+'</span>'+(response.truncated?'<span class="warning">响应仅展示前 2 MB</span>':'');
  $("#interfaceResponseEmpty").classList.add("hidden");$("#interfaceResponseBody").classList.remove("hidden");$("#interfaceResponseBody").textContent=prettyInterfaceResponseBody(response.body,response.content_type);$("#interfaceResponseHeaders").textContent=JSON.stringify(response.headers||{},null,2);$("#interfaceActualRequest").textContent=(request.method||"")+" "+(request.url||"")+"\n\n"+JSON.stringify(request.headers||{},null,2);activateInterfaceResponseTab("body");
}
async function sendInterfaceDebugRequest(){
  setInterfaceAssetError("");let body=null,bodyType="none",headers={},query={};
  try{const parsedBody=readInterfaceRequestBody();body=parsedBody.body;bodyType=parsedBody.type;headers=readInterfaceKeyValueRows("headers");query=readInterfaceKeyValueRows("query");}catch(error){activateInterfaceRequestTab(String(error.message).includes("请求头")?"headers":String(error.message).includes("Query")?"query":"body");setInterfaceAssetError("请求定义无效："+error.message);return;}
  const target=$("#interfaceAssetPath").value.trim();if(!target){setInterfaceAssetError("请求地址不能为空");$("#interfaceAssetPath").focus();return;}
  const button=$("#interfaceSendButton"),responseNode=$("#interfaceDebugResponse");button.disabled=true;button.textContent="发送中…";responseNode.dataset.state="loading";$("#interfaceResponseMeta").innerHTML="<span>正在等待响应…</span>";$("#interfaceResponseEmpty").classList.remove("hidden");$("#interfaceResponseEmpty").querySelector("strong").textContent="请求已发送";$("#interfaceResponseEmpty").querySelector("span").textContent="正在等待目标服务返回结果。";$("#interfaceResponseBody").classList.add("hidden");
  try{const result=await api("/api/interface-debug",{method:"POST",body:{asset_id:$("#interfaceAssetId").value,method:$("#interfaceAssetMethod").value,target:target,environment_id:$("#interfaceAssetEnvironment").value,headers:headers,query:query,body_type:bodyType,body:body,timeout_seconds:Number($("#interfaceDebugTimeout").value||30)}});renderInterfaceDebugResult(result);}catch(error){responseNode.dataset.state="error";$("#interfaceResponseMeta").innerHTML='<span class="interface-response-status error">请求失败</span>';$("#interfaceResponseEmpty").classList.remove("hidden");$("#interfaceResponseEmpty").querySelector("strong").textContent="请求没有成功完成";$("#interfaceResponseEmpty").querySelector("span").textContent=error.message;toast("接口请求失败："+error.message,true);}finally{button.disabled=false;button.textContent="发送";}
}
async function publishInterfaceAsset(id){try{const item=await api("/api/interface-assets/"+encodeURIComponent(id)+"/publish",{method:"POST"});toast("已发布 "+item.name+" v"+item.current_version);await loadInterfaces();}catch(error){toast("接口发布失败："+error.message,true);}}
async function deleteInterfaceAsset(id){if(!window.confirm("确认删除这个接口及全部历史版本？"))return;try{await api("/api/interface-assets/"+encodeURIComponent(id),{method:"DELETE"});toast("接口资产已删除");await loadInterfaces();}catch(error){toast("接口删除失败："+error.message,true);}}
async function showInterfaceVersions(id){
  const asset=state.interfaces.find(function(item){return item.id===id;});$("#interfaceVersionsTitle").textContent=(asset?asset.name:"")+" · 版本历史";$("#interfaceVersionList").innerHTML='<div class="interface-empty-state"><span>正在加载版本…</span></div>';openInterfaceDialog("interfaceVersionsDialog");
  try{const data=await api("/api/interface-assets/"+encodeURIComponent(id)+"/versions");$("#interfaceVersionList").innerHTML=(data.versions||[]).map(function(version){const definition=version.definition||{};return '<article><div><span>v'+Number(version.version)+'</span>'+statusPill(version.status)+'<time>'+esc(fmtTime(version.created_at))+'</time></div><dl><div><dt>请求</dt><dd><span class="step-chip">'+esc(definition.method||"GET")+'</span> <code>'+esc(definition.path||"/")+'</code></dd></div><div><dt>环境</dt><dd>'+esc((state.interfaceEnvironments.find(function(item){return item.id===definition.environment_id;})||{}).name||"未绑定")+'</dd></div><div><dt>说明</dt><dd>'+esc(definition.description||"—")+'</dd></div></dl></article>';}).join("")||'<div class="interface-empty-state"><span>暂无版本记录</span></div>';}catch(error){$("#interfaceVersionList").innerHTML='<div class="interface-empty-state"><span>'+esc(error.message)+'</span></div>';}
}
async function submitInterface(event) {
  event.preventDefault();
  const payload={logical_name:$("#interfaceName").value.trim(),module_id:$("#interfaceImportModule").value,environment_id:$("#interfaceImportEnvironment").value,content:$("#interfaceContent").value.trim(),publish:$("#interfacePublish").checked};
  try { const result=await api("/api/interface-specs/import",{method:"POST",body:payload}); toast("已导入 "+result.count+" 个接口版本"+(result.runtime_effect?"，并已发布":""));$("#interfaceContent").value="";$("#interfacePublish").checked=false;await loadInterfaces();activateWorkspaceTab("interfaces","assets"); }
  catch(error){ toast("接口导入失败："+error.message,true); }
}
async function loadIdentityMembers(projectId) {
  const selected=projectId || $("#membershipProjectSelect").value || state.projectId;
  if(!selected){state.identityMembers=[];renderIdentityMembers();return;}
  try {const data=await api("/api/identity/projects/"+encodeURIComponent(selected)+"/members");state.identityMembers=data.members || [];renderIdentityMembers();}
  catch(error){state.identityMembers=[];renderIdentityMembers();toast("成员列表加载失败："+error.message,true);}
}
function renderIdentityMembers() {
  const roleNames={viewer:"只读成员",tester:"测试执行者",project_admin:"项目管理员"};
  $("#identityMemberCount").textContent=state.identityMembers.length+" 人";
  $("#identityMembersBody").innerHTML=state.identityMembers.map(function(member){
    return '<tr><td><strong>'+esc(member.display_name)+'</strong><small class="table-subline">'+esc(member.username)+'</small></td><td><span class="role-badge '+esc(member.role)+'">'+esc(roleNames[member.role] || member.role)+'</span></td><td><span class="account-state '+(member.is_active?"active":"disabled")+'"><i></i>'+(member.is_active?"正常":"已停用")+'</span></td><td><button class="text-button danger-text" data-member-remove="'+esc(member.user_id)+'">移除</button></td></tr>';
  }).join("") || '<tr><td colspan="4">当前项目尚未配置成员</td></tr>';
}
function identityPage(items,pageKey) {
  const total=items.length,totalPages=Math.max(1,Math.ceil(total/state.identityPageSize));
  state[pageKey]=Math.min(Math.max(1,Number(state[pageKey] || 1)),totalPages);
  const start=(state[pageKey]-1)*state.identityPageSize;
  return {items:items.slice(start,start+state.identityPageSize),total:total,totalPages:totalPages,page:state[pageKey],start:total?start+1:0,end:Math.min(total,start+state.identityPageSize)};
}
function renderIdentityPager(prefix,pageInfo) {
  $("#"+prefix+"PageSummary").textContent=pageInfo.total?"共 "+pageInfo.total+" 条 · 当前 "+pageInfo.start+"–"+pageInfo.end+" 条":"共 0 条";
  $("#"+prefix+"PageIndicator").textContent="第 "+pageInfo.page+" / "+pageInfo.totalPages+" 页";
  $("#"+prefix+"PrevButton").disabled=pageInfo.page<=1;
  $("#"+prefix+"NextButton").disabled=pageInfo.page>=pageInfo.totalPages;
}
function renderIdentityUsers() {
  const keyword=$("#identityUserSearch").value.trim().toLowerCase(),role=$("#identityUserRoleFilter").value,status=$("#identityUserStatusFilter").value;
  const filtered=state.identityUsers.filter(function(user){
    const matchesKeyword=!keyword||(String(user.username||"")+" "+String(user.display_name||"")).toLowerCase().includes(keyword);
    const matchesRole=role==="all"||(role==="platform"?user.is_superuser:!user.is_superuser);
    const matchesStatus=status==="all"||(status==="active"?user.is_active:!user.is_active);
    return matchesKeyword&&matchesRole&&matchesStatus;
  });
  const page=identityPage(filtered,"identityUsersPage");
  $("#identityUsersBody").innerHTML=page.items.map(function(user){
    const self=state.identity && state.identity.user && state.identity.user.id===user.id;
    return '<tr><td><strong>'+esc(user.display_name)+'</strong><small class="table-subline">'+esc(user.username)+(self?" · 当前账号":"")+'</small></td><td><span class="role-badge '+(user.is_superuser?"platform":"standard")+'">'+(user.is_superuser?"平台管理员":"普通用户")+'</span></td><td><span class="account-state '+(user.is_active?"active":"disabled")+'"><i></i>'+(user.is_active?"正常":"已停用")+'</span></td><td>'+esc(fmtTime(user.last_login_at))+'</td><td>'+esc(fmtTime(user.created_at))+'</td><td><div class="identity-row-actions"><button class="text-button" data-user-edit="'+esc(user.id)+'">编辑</button><button class="text-button '+(user.is_active?"danger-text":"")+'" data-user-toggle="'+esc(user.id)+'" data-next-active="'+(user.is_active?"false":"true")+'">'+(user.is_active?"停用":"启用")+'</button></div></td></tr>';
  }).join("") || '<tr><td colspan="6">没有符合条件的平台用户</td></tr>';
  renderIdentityPager("identityUsers",page);
}
function renderIdentityProjects() {
  const keyword=$("#identityProjectSearch").value.trim().toLowerCase(),status=$("#identityProjectStatusFilter").value;
  const filtered=state.identityProjects.filter(function(project){
    const matchesKeyword=!keyword||(String(project.name||"")+" "+String(project.project_key||"")+" "+String(project.description||"")).toLowerCase().includes(keyword);
    const matchesStatus=status==="all"||(status==="active"?project.is_active:!project.is_active);
    return matchesKeyword&&matchesStatus;
  });
  const page=identityPage(filtered,"identityProjectsPage");
  $("#identityProjectsBody").innerHTML=page.items.map(function(project){
    const current=project.id===state.projectId;
    return '<tr><td><strong>'+esc(project.name)+'</strong><small class="table-subline">'+esc(project.description||"未填写项目说明")+(current?" · 当前项目":"")+'</small></td><td><code>'+esc(project.project_key)+'</code></td><td><span class="account-state '+(project.is_active?"active":"disabled")+'"><i></i>'+(project.is_active?"正常":"已停用")+'</span></td><td>'+esc(fmtTime(project.updated_at))+'</td><td>'+esc(fmtTime(project.created_at))+'</td><td><div class="identity-row-actions"><button class="text-button" data-project-members="'+esc(project.id)+'">成员管理</button><button class="text-button" data-project-edit="'+esc(project.id)+'">编辑</button></div></td></tr>';
  }).join("") || '<tr><td colspan="6">没有符合条件的项目空间</td></tr>';
  renderIdentityPager("identityProjects",page);
}
function renderIdentityRoles() {
  const permissionLabels={"project:view":"查看项目","test:execute":"执行测试","report:manage":"管理报告","server:operate":"操作服务器","project:members":"管理成员","interface:manage":"管理接口","evaluation:view":"查看评测","evaluation:manage":"管理评测","evaluation:operate":"执行评测"};
  const roleBoundaries={viewer:"适合浏览结果和评测内容，不可发起或修改任务",tester:"适合测试执行人员，可运行任务并管理报告",project_admin:"项目内最高权限，可维护成员、接口和评测资产"};
  const keyword=$("#identityRoleSearch").value.trim().toLowerCase();
  const filtered=state.identityRoles.filter(function(role){return !keyword||(String(role.label||"")+" "+String(role.key||"")+" "+(role.permissions||[]).join(" ")).toLowerCase().includes(keyword);});
  const page=identityPage(filtered,"identityRolesPage");
  $("#identityRoleMatrix").innerHTML=page.items.map(function(role){return '<tr><td><strong>'+esc(role.label)+'</strong></td><td><code>'+esc(role.key)+'</code></td><td><div class="identity-permission-list">'+role.permissions.map(function(permission){return '<span>'+esc(permissionLabels[permission]||permission)+'</span>';}).join("")+'</div></td><td><span class="identity-role-boundary">'+esc(roleBoundaries[role.key]||"按平台固定权限执行")+'</span></td></tr>';}).join("") || '<tr><td colspan="4">没有符合条件的固定角色</td></tr>';
  renderIdentityPager("identityRoles",page);
}
function renderIdentityAdmin() {
  const users=state.identityUsers,projects=state.identityProjects;
  renderIdentityUsers();renderIdentityProjects();renderIdentityRoles();
  const currentMembershipProject=$("#membershipProjectSelect").value || state.projectId;
  $("#membershipProjectSelect").innerHTML=projects.map(function(project){return '<option value="'+esc(project.id)+'"'+(project.is_active?'':' disabled')+'>'+esc(project.name)+(project.is_active?'':'（已停用）')+'</option>';}).join("");
  $("#membershipProjectSelect").value=projects.some(function(project){return project.id===currentMembershipProject&&project.is_active;})?currentMembershipProject:(projects.find(function(project){return project.is_active;}) || {}).id || "";
  $("#membershipUserSelect").innerHTML='<option value="" disabled selected>请选择平台用户</option>'+users.filter(function(user){return user.is_active;}).map(function(user){return '<option value="'+esc(user.id)+'">'+esc(user.display_name)+" · "+esc(user.username)+'</option>';}).join("");
  renderIdentityAudit();
}
function applyIdentityAuditData(data) {
  state.identityAudit=data.events || [];
  state.identityAuditPage=Number(data.page || 1);
  state.identityAuditPageSize=Number(data.page_size || 20);
  state.identityAuditTotal=Number(data.total || 0);
  state.identityAuditTotalPages=Math.max(1,Number(data.total_pages || 1));
}
function renderIdentityAudit() {
  $("#identityAuditBody").innerHTML=state.identityAudit.map(function(event){return '<tr><td>'+esc(fmtTime(event.created_at))+'</td><td>'+esc(event.actor_username || "系统 / 未认证")+'</td><td>'+esc(event.project_name || "—")+'</td><td><code>'+esc(event.action)+'</code></td><td><span class="audit-outcome '+esc(event.outcome)+'">'+(event.outcome==="success"?"成功":event.outcome==="denied"?"拒绝":"失败")+'</span></td><td>'+esc(event.target_type?event.target_type+" · "+shortId(event.target_id):"—")+'</td></tr>';}).join("") || '<tr><td colspan="6">暂无审计记录</td></tr>';
  const start=state.identityAuditTotal ? (state.identityAuditPage-1)*state.identityAuditPageSize+1 : 0;
  const end=Math.min(state.identityAuditTotal,state.identityAuditPage*state.identityAuditPageSize);
  $("#identityAuditPageSummary").textContent=state.identityAuditTotal ? "共 "+state.identityAuditTotal+" 条 · 当前 "+start+"–"+end+" 条" : "共 0 条";
  $("#identityAuditPageIndicator").textContent="第 "+state.identityAuditPage+" / "+state.identityAuditTotalPages+" 页";
  $("#identityAuditPrevButton").disabled=state.identityAuditPage<=1;
  $("#identityAuditNextButton").disabled=state.identityAuditPage>=state.identityAuditTotalPages;
}
async function loadIdentityAudit(page) {
  if(!state.identity || !state.identity.user || !state.identity.user.is_superuser)return;
  const requested=Math.max(1,Number(page || state.identityAuditPage || 1));
  try {
    const data=await api("/api/identity/audit-events?page="+requested+"&page_size="+state.identityAuditPageSize);
    applyIdentityAuditData(data);renderIdentityAudit();
  } catch(error){toast("审计记录加载失败："+error.message,true);}
}
async function loadIdentityData(auditPage) {
  if(!state.identity || !state.identity.user || !state.identity.user.is_superuser)return;
  try {
    const page=Math.max(1,Number(auditPage || state.identityAuditPage || 1));
    const results=await Promise.all([api("/api/identity/users"),api("/api/identity/projects"),api("/api/identity/roles"),api("/api/identity/audit-events?page="+page+"&page_size="+state.identityAuditPageSize)]);
    state.identityUsers=results[0].users || [];state.identityProjects=results[1].projects || [];state.identityRoles=results[2].roles || [];applyIdentityAuditData(results[3]);
    const signedInUser=state.identityUsers.find(function(user){return user.id===state.identity.user.id;});
    if(signedInUser)state.identity.user=Object.assign({},state.identity.user,signedInUser);
    state.identity.projects=(state.identity.projects||[]).map(function(project){return state.identityProjects.find(function(fresh){return fresh.id===project.id;})||project;});
    const currentProject=state.identityProjects.find(function(project){return project.id===state.projectId;});if(currentProject)state.identity.current_project=currentProject;
    syncIdentityChrome();
    renderIdentityAdmin();
    await loadIdentityMembers();
  } catch(error){toast("用户与权限数据加载失败："+error.message,true);}
}
function activateIdentityTab(tab) {
  const target=["users","projects","roles","audit"].includes(tab)?tab:"users";
  state.identityWorkspaceTab=target;
  $$("[data-identity-tab]").forEach(function(button){const active=button.dataset.identityTab===target;button.classList.toggle("active",active);button.setAttribute("aria-selected",String(active));});
  $$("[data-identity-panel]").forEach(function(panel){panel.classList.toggle("active",panel.dataset.identityPanel===target);});
  $("#newIdentityUserButton").classList.toggle("hidden",target!=="users");
  $("#newIdentityProjectButton").classList.toggle("hidden",target!=="projects");
  if(target==="audit")loadIdentityAudit(state.identityAuditPage);
}
function showIdentityDrawer(id) {
  const dialog=$("#"+id);if(!dialog)return;
  if(typeof dialog.showModal==="function"){if(!dialog.open)dialog.showModal();}else dialog.classList.add("open");
}
function closeIdentityDrawer(id) {
  const dialog=$("#"+id);if(!dialog)return;
  if(dialog.open)dialog.close();dialog.classList.remove("open");
}
function openIdentityUserDrawer(userId) {
  const user=state.identityUsers.find(function(item){return item.id===userId;});
  $("#createUserForm").reset();$("#identityUserId").value=user?user.id:"";$("#newUsername").value=user?user.username:"";$("#newUsername").readOnly=Boolean(user);
  $("#newUserDisplayName").value=user?user.display_name:"";$("#newUserPassword").required=!user;$("#newUserSuperuser").checked=Boolean(user&&user.is_superuser);$("#newUserActive").checked=user?Boolean(user.is_active):true;$("#newUserActive").disabled=Boolean(user&&state.identity&&state.identity.user&&user.id===state.identity.user.id);
  $("#identityUserDrawerTitle").textContent=user?"编辑用户":"新增用户";$("#identityUserDrawerHint").textContent=user?"可修改姓名、密码、平台角色和账号状态。":"创建后可按项目分配角色和操作范围。";
  $("#identityUserPasswordLabel").textContent=user?"重置密码（可选）":"初始密码";$("#identityUserPasswordHint").textContent=user?"不填写则保留当前密码；填写后旧登录会话会失效。":"密码只会以哈希形式保存。";
  $("#identityUserActiveHint").textContent=$("#newUserActive").disabled?"不能在当前会话中停用自己的账号。":"关闭后会立即撤销该用户的全部登录会话。";
  $("#newUserPassword").placeholder=user?"不修改请留空":"至少 12 位，并包含三类字符";$("#identityUserActiveRow").classList.toggle("hidden",!user);$("#identityUserSubmitButton").textContent=user?"保存修改":"创建用户";
  showIdentityDrawer("identityUserDrawer");setTimeout(function(){$("#newUsername").focus();},0);
}
function openIdentityProjectDrawer(projectId) {
  const project=state.identityProjects.find(function(item){return item.id===projectId;});
  $("#createProjectForm").reset();$("#identityProjectId").value=project?project.id:"";$("#newProjectKey").value=project?project.project_key:"";$("#newProjectKey").readOnly=Boolean(project);
  $("#newProjectName").value=project?project.name:"";$("#newProjectDescription").value=project?project.description||"":"";$("#newProjectActive").checked=project?Boolean(project.is_active):true;$("#newProjectActive").disabled=Boolean(project&&project.id===state.projectId);
  $("#identityProjectDrawerTitle").textContent=project?"编辑项目":"新增项目";$("#identityProjectActiveRow").classList.toggle("hidden",!project);$("#identityProjectSubmitButton").textContent=project?"保存修改":"创建项目";
  $("#identityProjectActiveHint").textContent=$("#newProjectActive").disabled?"请先切换到其他项目，再停用当前项目。":"停用后该项目不再进入日常项目选择。";
  showIdentityDrawer("identityProjectDrawer");setTimeout(function(){$("#newProjectName").focus();},0);
}
async function openIdentityMembershipDrawer(projectId) {
  if(projectId)$("#membershipProjectSelect").value=projectId;
  showIdentityDrawer("identityMembershipDrawer");
  await loadIdentityMembers($("#membershipProjectSelect").value);
}
async function createIdentityUser(event) {
  event.preventDefault();
  try {
    const userId=$("#identityUserId").value,payload={display_name:$("#newUserDisplayName").value.trim(),is_superuser:$("#newUserSuperuser").checked};
    if(userId){payload.is_active=$("#newUserActive").checked;if($("#newUserPassword").value)payload.password=$("#newUserPassword").value;}
    else {payload.username=$("#newUsername").value.trim();payload.password=$("#newUserPassword").value;}
    await api(userId?"/api/identity/users/"+encodeURIComponent(userId):"/api/identity/users",{method:userId?"PATCH":"POST",body:payload});
    event.currentTarget.reset();closeIdentityDrawer("identityUserDrawer");toast(userId?"用户信息已更新":"平台用户已创建");await loadIdentityData();
  } catch(error){toast(($("#identityUserId").value?"更新":"创建")+"用户失败："+error.message,true);}
}
async function createIdentityProject(event) {
  event.preventDefault();
  try {
    const projectId=$("#identityProjectId").value,payload={name:$("#newProjectName").value.trim(),description:$("#newProjectDescription").value.trim()};
    if(projectId)payload.is_active=$("#newProjectActive").checked;else payload.project_key=$("#newProjectKey").value.trim();
    const project=await api(projectId?"/api/identity/projects/"+encodeURIComponent(projectId):"/api/identity/projects",{method:projectId?"PATCH":"POST",body:payload});
    event.currentTarget.reset();closeIdentityDrawer("identityProjectDrawer");
    if(projectId){toast("项目信息已更新");await loadIdentityData();}else await switchProject(project.id,"dashboard");
  } catch(error){toast(($("#identityProjectId").value?"更新":"创建")+"项目失败："+error.message,true);}
}
async function saveMembership(event) {
  event.preventDefault();
  const projectId=$("#membershipProjectSelect").value;
  try {
    await api("/api/identity/projects/"+encodeURIComponent(projectId)+"/members",{method:"PUT",body:{user_id:$("#membershipUserSelect").value,role:$("#membershipRoleSelect").value}});
    toast("项目成员权限已保存");await loadIdentityMembers(projectId);
  } catch(error){toast("保存成员权限失败："+error.message,true);}
}
async function removeMembership(userId) {
  const projectId=$("#membershipProjectSelect").value;
  try {await api("/api/identity/projects/"+encodeURIComponent(projectId)+"/members",{method:"PUT",body:{user_id:userId,role:null}});toast("成员已从项目移除");await loadIdentityMembers(projectId);}
  catch(error){toast("移除成员失败："+error.message,true);}
}
async function toggleIdentityUser(userId,active) {
  try {await api("/api/identity/users/"+encodeURIComponent(userId),{method:"PATCH",body:{is_active:active}});toast(active?"用户已启用":"用户已停用并撤销会话");await loadIdentityData();}
  catch(error){toast("更新用户失败："+error.message,true);}
}
function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer=setInterval(async function(){
    try {
      await loadRuns();
      if($("#view-monitor").classList.contains("active")) await refreshMonitor(false);
      if($("#view-stress").classList.contains("active")) {
        await refreshServerSession(false);
        await loadStressJobs();
        const livePanel=$("#stressLiveTaskPanel");
        if(livePanel && !livePanel.classList.contains("hidden")) {
          const job=selectStressJobForCurrentSession();
          if(isActiveStressJob(job)) await refreshStress(false);
          else if(job) renderStressTask(job);
          else renderStressTask(null);
        }
      }
      if($("#view-reports").classList.contains("active")) await loadReports();
      if($("#view-evaluation").classList.contains("active")) await loadEvaluationWorkspace(false);
    }
    catch(error){ console.warn(error); }
  },2500);
}
function bind() {
  $("#refreshButton").addEventListener("click",refreshAll);
  $("#mainNav").addEventListener("click",function(event){const button=event.target.closest("[data-view]");if(button)gotoView(button.dataset.view);});
  document.body.addEventListener("click",function(event){
    const workspaceTab=event.target.closest("[data-workspace-tab]");
    if(workspaceTab){activateWorkspaceTab(workspaceTab.dataset.workspaceTab,workspaceTab.dataset.workspaceTarget);if(workspaceTab.dataset.workspaceTab==="evaluation"&&workspaceTab.dataset.workspaceTarget==="report")loadEvaluationReport(false);if(workspaceTab.dataset.workspaceTab==="evaluation"&&workspaceTab.dataset.workspaceTarget==="suites")refreshEvaluationSuites();if(workspaceTab.dataset.workspaceTab==="evaluation"&&workspaceTab.dataset.workspaceTarget==="comparison")renderEvaluationComparison();}
    const interfaceWorkspaceTarget=event.target.closest("[data-interface-workspace-target]");if(interfaceWorkspaceTarget)activateWorkspaceTab("interfaces",interfaceWorkspaceTarget.dataset.interfaceWorkspaceTarget);
    const goto=event.target.closest("[data-goto]");if(goto)gotoView(goto.dataset.goto);
    const run=event.target.closest(".run-open");if(run){state.monitorRunId=run.dataset.id;populateRunSelects();gotoView("monitor");}
    const runStop=event.target.closest(".run-stop");if(runStop)runAction(runStop.dataset.id,"stop");
    const runRestart=event.target.closest(".run-restart");if(runRestart)reconfigureRun(runRestart.dataset.id);
    const runExecute=event.target.closest(".run-execute");if(runExecute)runAction(runExecute.dataset.id,"execute");
    const download=event.target.closest(".report-download");if(download)downloadReport(download.dataset.id,download.dataset.format);
    const edit=event.target.closest("[data-model-edit]");if(edit)editModel(edit.dataset.modelEdit);
    const test=event.target.closest("[data-model-test]");if(test)modelAction(test.dataset.modelTest,"test",test);
    const activate=event.target.closest("[data-model-activate]");if(activate)modelAction(activate.dataset.modelActivate,"activate",activate);
    const evaluationOpen=event.target.closest("[data-evaluation-open]");if(evaluationOpen){state.selectedEvaluationRunId=evaluationOpen.dataset.evaluationOpen;showEvaluationWorkspace("detail");refreshSelectedEvaluationRun();}
    const evaluationReport=event.target.closest("[data-evaluation-report]");if(evaluationReport)openEvaluationReport(evaluationReport.dataset.evaluationReport);
    const evaluationStop=event.target.closest("[data-evaluation-stop]");if(evaluationStop)stopEvaluationRun(evaluationStop.dataset.evaluationStop);
    const evaluationDelete=event.target.closest("[data-evaluation-delete]");if(evaluationDelete)deleteEvaluationRun(evaluationDelete.dataset.evaluationDelete);
    const evaluationClone=event.target.closest("[data-evaluation-suite-clone]");if(evaluationClone)cloneEvaluationSuite(evaluationClone.dataset.evaluationSuiteClone);
    const evaluationImport=event.target.closest("[data-evaluation-suite-import]");if(evaluationImport)importEvaluationSuite(evaluationImport.dataset.evaluationSuiteImport,evaluationImport);
    const evaluationReview=event.target.closest("[data-evaluation-review]");if(evaluationReview)openEvaluationReview(evaluationReview.dataset.evaluationReview,evaluationReview.dataset.evaluationReviewName);
    const evaluationReviewClose=event.target.closest("[data-evaluation-review-close]");if(evaluationReviewClose)closeEvaluationReview();
    const stressStop=event.target.closest(".stress-stop");if(stressStop)stopStressJob(stressStop.dataset.id);
    const stressRetry=event.target.closest(".stress-retry");if(stressRetry)retryStressJob(stressRetry.dataset.id);
    const stressDownload=event.target.closest(".stress-download");if(stressDownload)downloadStressReport(stressDownload.dataset.format,stressDownload.dataset.id);
    const gpuExpand=event.target.closest("[data-gpu-expand]");if(gpuExpand)toggleGpuLiveDetails(Number(gpuExpand.dataset.gpuExpand));
    const gpuDrawerClose=event.target.closest("#closeGpuDetailDrawer");if(gpuDrawerClose)closeGpuDetailDrawer();
    const gpuCard=event.target.closest("[data-gpu-index]");if(gpuCard && !gpuExpand)toggleStressGpu(Number(gpuCard.dataset.gpuIndex));
    const stressPreset=event.target.closest("[data-stress-preset]");if(stressPreset)applyStressPreset(stressPreset.dataset.stressPreset);
    const serverNew=event.target.closest("[data-server-new]");if(serverNew)resetServerProfileForm();
    const serverEdit=event.target.closest("[data-server-edit]");if(serverEdit)editServerProfile(serverEdit.dataset.serverEdit);
    const serverConnect=event.target.closest("[data-server-connect]");if(serverConnect)connectSavedServer(serverConnect.dataset.serverConnect);
    const serverDelete=event.target.closest("[data-server-delete]");if(serverDelete)deleteServerProfile(serverDelete.dataset.serverDelete);
    const sessionOpen=event.target.closest("[data-session-open]");if(sessionOpen)openServerSession(sessionOpen.dataset.sessionOpen);
    const identityTab=event.target.closest("[data-identity-tab]");if(identityTab)activateIdentityTab(identityTab.dataset.identityTab);
    const identityOpen=event.target.closest("[data-identity-open]");if(identityOpen){if(identityOpen.dataset.identityOpen==="user")openIdentityUserDrawer("");if(identityOpen.dataset.identityOpen==="project")openIdentityProjectDrawer("");}
    const identityClose=event.target.closest("[data-identity-close]");if(identityClose)closeIdentityDrawer(identityClose.dataset.identityClose);
    const userEdit=event.target.closest("[data-user-edit]");if(userEdit)openIdentityUserDrawer(userEdit.dataset.userEdit);
    const userToggle=event.target.closest("[data-user-toggle]");if(userToggle)toggleIdentityUser(userToggle.dataset.userToggle,userToggle.dataset.nextActive==="true");
    const projectEdit=event.target.closest("[data-project-edit]");if(projectEdit)openIdentityProjectDrawer(projectEdit.dataset.projectEdit);
    const projectMembers=event.target.closest("[data-project-members]");if(projectMembers)openIdentityMembershipDrawer(projectMembers.dataset.projectMembers);
    const memberRemove=event.target.closest("[data-member-remove]");if(memberRemove)removeMembership(memberRemove.dataset.memberRemove);
    const interfaceModule=event.target.closest("[data-interface-module]");if(interfaceModule)selectInterfaceModule(interfaceModule.dataset.interfaceModule);
    const interfaceModuleEdit=event.target.closest("[data-interface-module-edit]");if(interfaceModuleEdit)openInterfaceModuleEditor(interfaceModuleEdit.dataset.interfaceModuleEdit);
    const interfaceModuleDelete=event.target.closest("[data-interface-module-delete]");if(interfaceModuleDelete)deleteInterfaceModule(interfaceModuleDelete.dataset.interfaceModuleDelete);
    const interfaceEnvironmentEdit=event.target.closest("[data-interface-environment-edit]");if(interfaceEnvironmentEdit)openInterfaceEnvironmentEditor(interfaceEnvironmentEdit.dataset.interfaceEnvironmentEdit);
    const interfaceEnvironmentDelete=event.target.closest("[data-interface-environment-delete]");if(interfaceEnvironmentDelete)deleteInterfaceEnvironment(interfaceEnvironmentDelete.dataset.interfaceEnvironmentDelete);
    const interfaceVariableEdit=event.target.closest("[data-interface-variable-edit]");if(interfaceVariableEdit)openInterfaceVariableEditor(interfaceVariableEdit.dataset.interfaceVariableEdit);
    const interfaceVariableDelete=event.target.closest("[data-interface-variable-delete]");if(interfaceVariableDelete)deleteInterfaceVariable(interfaceVariableDelete.dataset.interfaceVariableDelete);
    const interfaceAssetOpen=event.target.closest("[data-interface-asset-open]");if(interfaceAssetOpen)openInterfaceAssetEditor(interfaceAssetOpen.dataset.interfaceAssetOpen);
    const interfaceAssetEdit=event.target.closest("[data-interface-asset-edit]");if(interfaceAssetEdit)openInterfaceAssetEditor(interfaceAssetEdit.dataset.interfaceAssetEdit);
    const interfaceAssetPublish=event.target.closest("[data-interface-asset-publish]");if(interfaceAssetPublish)publishInterfaceAsset(interfaceAssetPublish.dataset.interfaceAssetPublish);
    const interfaceAssetDelete=event.target.closest("[data-interface-asset-delete]");if(interfaceAssetDelete)deleteInterfaceAsset(interfaceAssetDelete.dataset.interfaceAssetDelete);
    const interfaceVersions=event.target.closest("[data-interface-versions]");if(interfaceVersions)showInterfaceVersions(interfaceVersions.dataset.interfaceVersions);
    const interfaceEmptyCreate=event.target.closest("[data-interface-empty-create]");if(interfaceEmptyCreate)openInterfaceAssetEditor("");
    const interfaceScenarioEmptyCreate=event.target.closest("[data-interface-scenario-empty-create]");if(interfaceScenarioEmptyCreate)openInterfaceScenarioEditor("");
    const interfaceScenarioEdit=event.target.closest("[data-interface-scenario-edit]");if(interfaceScenarioEdit)openInterfaceScenarioEditor(interfaceScenarioEdit.dataset.interfaceScenarioEdit);
    const interfaceScenarioRun=event.target.closest("[data-interface-scenario-run]");if(interfaceScenarioRun)runInterfaceScenario(interfaceScenarioRun.dataset.interfaceScenarioRun);
    const interfaceScenarioDelete=event.target.closest("[data-interface-scenario-delete]");if(interfaceScenarioDelete)deleteInterfaceScenario(interfaceScenarioDelete.dataset.interfaceScenarioDelete);
    const interfaceScenarioHistory=event.target.closest("[data-interface-scenario-history]");if(interfaceScenarioHistory){const latest=state.interfaceScenarioRuns.find(function(run){return run.scenario_id===interfaceScenarioHistory.dataset.interfaceScenarioHistory;});if(latest)showInterfaceScenarioRun(latest.id);else toast("这个场景还没有执行记录");}
    const interfaceScenarioRunDetail=event.target.closest("[data-interface-scenario-run-detail]");if(interfaceScenarioRunDetail)showInterfaceScenarioRun(interfaceScenarioRunDetail.dataset.interfaceScenarioRunDetail);
    const interfaceScenarioRunStop=event.target.closest("[data-interface-scenario-run-stop]");if(interfaceScenarioRunStop)stopInterfaceScenarioRun(interfaceScenarioRunStop.dataset.interfaceScenarioRunStop);
    const scenarioStepMove=event.target.closest("[data-scenario-step-move]");if(scenarioStepMove){const card=scenarioStepMove.closest("[data-interface-scenario-step]");const steps=$$("[data-interface-scenario-step]",$("#interfaceScenarioSteps")).map(readInterfaceScenarioStepLoose);const index=$$("[data-interface-scenario-step]",$("#interfaceScenarioSteps")).indexOf(card);const target=scenarioStepMove.dataset.scenarioStepMove==="up"?index-1:index+1;if(target>=0&&target<steps.length){const moved=steps.splice(index,1)[0];steps.splice(target,0,moved);renderInterfaceScenarioSteps(steps);}}
    const scenarioStepDelete=event.target.closest("[data-scenario-step-delete]");if(scenarioStepDelete){const card=scenarioStepDelete.closest("[data-interface-scenario-step]");if(card){card.remove();const steps=$$("[data-interface-scenario-step]",$("#interfaceScenarioSteps")).map(readInterfaceScenarioStepLoose);renderInterfaceScenarioSteps(steps);}}
    const scenarioRuleAdd=event.target.closest("[data-scenario-rule-add]");if(scenarioRuleAdd){const card=scenarioRuleAdd.closest("[data-interface-scenario-step]");const type=scenarioRuleAdd.dataset.scenarioRuleAdd;const container=card.querySelector(type==="extraction"?"[data-scenario-extractions]":"[data-scenario-assertions]");container.insertAdjacentHTML("beforeend",type==="extraction"?interfaceScenarioExtractionRow({}):interfaceScenarioAssertionRow({}));}
    const scenarioRuleDelete=event.target.closest("[data-scenario-rule-delete]");if(scenarioRuleDelete){const row=scenarioRuleDelete.closest(".interface-scenario-rule-row");if(row)row.remove();}
    const interfaceRequestTab=event.target.closest("[data-interface-request-tab]");if(interfaceRequestTab)activateInterfaceRequestTab(interfaceRequestTab.dataset.interfaceRequestTab);
    const interfaceResponseTab=event.target.closest("[data-interface-response-tab]");if(interfaceResponseTab)activateInterfaceResponseTab(interfaceResponseTab.dataset.interfaceResponseTab);
    const interfaceRowAdd=event.target.closest("[data-interface-row-add]");if(interfaceRowAdd)addInterfaceKeyValueRow(interfaceRowAdd.dataset.interfaceRowAdd);
    const interfaceRowDelete=event.target.closest("[data-interface-row-delete]");if(interfaceRowDelete){const row=interfaceRowDelete.closest(".interface-kv-row");if(row)row.remove();updateInterfaceRequestCounts();}
    const interfaceDialogClose=event.target.closest("[data-interface-dialog-close]");if(interfaceDialogClose)closeInterfaceDialog(interfaceDialogClose.dataset.interfaceDialogClose);
  });
  document.body.addEventListener("change",function(event){
    const comparisonRun=event.target.closest("[data-evaluation-comparison-run]");if(comparisonRun){const id=comparisonRun.dataset.evaluationComparisonRun;state.selectedEvaluationComparisonRunIds=comparisonRun.checked?Array.from(new Set(state.selectedEvaluationComparisonRunIds.concat(id))):state.selectedEvaluationComparisonRunIds.filter(function(item){return item!==id;});state.evaluationComparisonResult=null;renderEvaluationComparison();}
    if(event.target.matches("#evaluationSuiteVersion"))syncEvaluationForm();
    const selected=event.target.closest("[data-interface-scenario-select]");if(selected){const id=selected.dataset.interfaceScenarioSelect;state.selectedInterfaceScenarioIds=selected.checked?Array.from(new Set(state.selectedInterfaceScenarioIds.concat(id))):state.selectedInterfaceScenarioIds.filter(function(item){return item!==id;});renderInterfaceScenarios();}
    if(event.target.matches("#interfaceScenarioSelectAll")){state.selectedInterfaceScenarioIds=event.target.checked?state.interfaceScenarios.map(function(item){return item.id;}):[];renderInterfaceScenarios();}
    if(event.target.matches("[data-scenario-step-asset]")){const card=event.target.closest("[data-interface-scenario-step]");const asset=state.interfaces.find(function(item){return item.id===event.target.value;});const name=card&&card.querySelector("[data-scenario-step-name]");if(name&&!name.value.trim()&&asset)name.value=asset.name;const title=card&&card.querySelector("header strong");if(title)title.textContent=name.value.trim()||(asset&&asset.name)||"未命名步骤";}
    if(event.target.matches("[data-scenario-step-name]")){const card=event.target.closest("[data-interface-scenario-step]");const title=card&&card.querySelector("header strong");if(title)title.textContent=event.target.value.trim()||"未命名步骤";}
  });
  $("#loginForm").addEventListener("submit",submitLogin);
  $("#setupForm").addEventListener("submit",submitSetup);
  $("#logoutButton").addEventListener("click",logout);
  $("#projectSwitcher").addEventListener("change",function(){switchProject(this.value);});
  $("#interfaceProjectSwitcher").addEventListener("change",function(){switchProject(this.value,"interfaces");});
  $("#runServerProfile").addEventListener("change",function(){syncRunServerProfileFields(true);});
  $("#runGpuServerProfile").addEventListener("change",function(){syncRunServerProfileFields(true);});
  ["runUsername","runHost","runGpuHost"].forEach(function(id){ $("#" + id).addEventListener("input",syncRunSummary); });
  $("#runUploadMode").addEventListener("change",syncUploadModeUi);
  $$('input[name="analysis"]').forEach(function(input){ input.addEventListener("change",syncRunSummary); });
  $("#runFiles").addEventListener("change",function(){
    const files=Array.from(this.files||[]);const bytes=files.reduce(function(sum,file){return sum+file.size;},0);
    $("#fileSelectionText").textContent=files.length?files.length+" 个文件 · "+fmtSize(bytes):"尚未选择客户本地测试文件夹";
    syncRunSummary();
  });
  $("#runForm").addEventListener("submit",submitRun);
  $("#monitorRunSelect").addEventListener("change",function(){state.monitorRunId=this.value;refreshMonitor(true);});
  $("#logConsole").addEventListener("scroll",function(){recordLogScroll(this);});
  $("#logFollowButton").addEventListener("click",followLogToBottom);
  $$('input[name="stressMode"]').forEach(function(input){input.addEventListener("change",updateStressStartState);});
  $("#stressDuration").addEventListener("input",function(){if(state.stressPreset!=="custom")applyStressPreset("custom");});
  $("#serverProfileForm").addEventListener("submit",connectServerSession);
  $("#newServerProfileButton").addEventListener("click",resetServerProfileForm);
  $("#closeServerProfileDrawer").addEventListener("click",closeServerProfileDrawer);
  $("#serverProfileDrawerBackdrop").addEventListener("click",closeServerProfileDrawer);
  $("#serverProfileSearch").addEventListener("input",renderServerProfiles);
  $("#serverProfileStatusFilter").addEventListener("change",renderServerProfiles);
  $("#serverSaveConfig").addEventListener("change",syncServerSaveOptions);
  $("#saveServerProfileButton").addEventListener("click",async function(){try{await saveServerProfile();closeServerProfileDrawer();toast("服务器配置已保存");}catch(error){toast("保存失败："+error.message,true);}});
  $("#serverSessionSelect").addEventListener("change",function(){openServerSession(this.value);});
  $("#stressSessionSelect").addEventListener("change",function(){openServerSession(this.value,"setup");});
  $("#stopServerSessionButton").addEventListener("click",stopServerSession);
  $("#stressLiveStopButton").addEventListener("click",function(){if(state.stressJobId)stopStressJob(state.stressJobId);});
  $("#stressForm").addEventListener("submit",submitStress);
  $("#stressJobSelect").addEventListener("change",function(){state.stressJobId=this.value;refreshStress(true);});
  $("#stressReportJobSelect").addEventListener("change",function(){state.stressReportJobId=this.value;});
  $("#downloadStressDocxButton").addEventListener("click",function(){downloadStressReport("docx");});
  $("#downloadStressPdfButton").addEventListener("click",function(){downloadStressReport("pdf");});
  $("#saveTemplateButton").addEventListener("click",saveTemplate);
  $("#reportForm").addEventListener("submit",submitReport);
  $("#refreshReportsButton").addEventListener("click",loadReports);
  $("#modelForm").addEventListener("submit",submitModel);
  $("#evaluationForm").addEventListener("submit",submitEvaluation);
  $("#evaluationSuiteForm").addEventListener("submit",createEvaluationSuite);
  $("#evaluationReviewForm").addEventListener("submit",submitEvaluationReview);
  $("#evaluationRunKind").addEventListener("change",syncEvaluationForm);
  $("#evaluationJudgeProfile").addEventListener("change",syncEvaluationForm);
  $("#evaluationMaxTokens").addEventListener("input",syncEvaluationForm);
  $("#refreshEvaluationButton").addEventListener("click",function(){loadEvaluationWorkspace(false);});
  $("#evaluationRunsPrevButton").addEventListener("click",function(){setEvaluationRunsPage(state.evaluationRunsPage-1);});
  $("#evaluationRunsNextButton").addEventListener("click",function(){setEvaluationRunsPage(state.evaluationRunsPage+1);});
  $("#evaluationReportCasesPrevButton").addEventListener("click",function(){setEvaluationReportCasesPage(state.evaluationReportCasesPage-1);});
  $("#evaluationReportCasesNextButton").addEventListener("click",function(){setEvaluationReportCasesPage(state.evaluationReportCasesPage+1);});
  $("#refreshEvaluationSuitesButton").addEventListener("click",refreshEvaluationSuites);
  $("#createEvaluationComparisonButton").addEventListener("click",function(){createEvaluationComparison(false);});
  $("#stopEvaluationButton").addEventListener("click",function(){if(state.selectedEvaluationRunId)stopEvaluationRun(state.selectedEvaluationRunId);});
  $("#deleteEvaluationButton").addEventListener("click",function(){if(state.selectedEvaluationRunId)deleteEvaluationRun(state.selectedEvaluationRunId);});
  $("#openEvaluationReportButton").addEventListener("click",function(){openEvaluationReport(state.selectedEvaluationRunId);});
  $("#downloadEvaluationReportDocxButton").addEventListener("click",function(){downloadEvaluationReport("docx");});
  $("#downloadEvaluationReportPdfButton").addEventListener("click",function(){downloadEvaluationReport("pdf");});
  $("#downloadEvaluationSummaryButton").addEventListener("click",function(){downloadEvaluationArtifact("summary");});
  $("#downloadEvaluationPerformanceButton").addEventListener("click",function(){downloadEvaluationArtifact("performance");});
  $("#interfaceForm").addEventListener("submit",submitInterface);
  $("#refreshInterfacesButton").addEventListener("click",loadInterfaces);
  $("#newInterfaceModuleButton").addEventListener("click",function(){openInterfaceModuleEditor("");});
  $("#manageCurrentProjectButton").addEventListener("click",openCurrentProjectEditor);
  $("#newInterfaceEnvironmentButton").addEventListener("click",function(){openInterfaceEnvironmentEditor("");});
  $("#newInterfaceVariableButton").addEventListener("click",function(){openInterfaceVariableEditor("");});
  $("#newInterfaceAssetButton").addEventListener("click",function(){openInterfaceAssetEditor("");});
  $("#newInterfaceScenarioButton").addEventListener("click",function(){openInterfaceScenarioEditor("");});
  $("#batchRunInterfaceScenariosButton").addEventListener("click",batchRunInterfaceScenarios);
  $("#interfaceModuleForm").addEventListener("submit",submitInterfaceModule);
  $("#currentProjectForm").addEventListener("submit",submitCurrentProject);
  $("#interfaceEnvironmentForm").addEventListener("submit",submitInterfaceEnvironment);
  $("#interfaceVariableForm").addEventListener("submit",submitInterfaceVariable);
  $("#interfaceAssetForm").addEventListener("submit",submitInterfaceAsset);
  $("#interfaceScenarioForm").addEventListener("submit",submitInterfaceScenario);
  $("#closeInterfaceEditorButton").addEventListener("click",closeInterfaceEditor);
  $("#closeInterfaceScenarioEditorButton").addEventListener("click",closeInterfaceScenarioEditor);
  $("#addInterfaceScenarioStepButton").addEventListener("click",addInterfaceScenarioStep);
  $("#saveAndRunInterfaceScenarioButton").addEventListener("click",function(){saveInterfaceScenario(true);});
  $("#interfaceSendButton").addEventListener("click",sendInterfaceDebugRequest);
  $("#interfaceAssetMethod").addEventListener("change",syncInterfaceMethodBadge);
  $("#interfaceAssetEnvironment").addEventListener("change",syncInterfaceTargetHint);
  $("#interfaceAssetPath").addEventListener("paste",handleInterfaceCurlPaste);
  $("#interfaceAssetBodyType").addEventListener("change",syncInterfaceBodyEditor);
  $("#interfaceAssetForm").addEventListener("input",function(event){if(event.target.matches("[data-interface-row-key]"))updateInterfaceRequestCounts();if(!$("#interfaceAssetError").classList.contains("hidden"))setInterfaceAssetError("");});
  $("#interfaceVariableSecret").addEventListener("change",syncInterfaceVariableSecret);
  $("#interfaceAssetSearch").addEventListener("input",renderInterfaceAssets);
  $("#interfaceModuleSearch").addEventListener("input",renderInterfaceModules);
  $("#interfaceEnvironmentFilter").addEventListener("change",renderInterfaceAssets);
  $("#createUserForm").addEventListener("submit",createIdentityUser);
  $("#createProjectForm").addEventListener("submit",createIdentityProject);
  $("#membershipForm").addEventListener("submit",saveMembership);
  $("#membershipProjectSelect").addEventListener("change",function(){loadIdentityMembers(this.value);});
  $("#refreshAuditButton").addEventListener("click",function(){loadIdentityAudit(1);});
  $("#identityAuditPrevButton").addEventListener("click",function(){loadIdentityAudit(state.identityAuditPage-1);});
  $("#identityAuditNextButton").addEventListener("click",function(){loadIdentityAudit(state.identityAuditPage+1);});
  $("#identityUserSearch").addEventListener("input",function(){state.identityUsersPage=1;renderIdentityUsers();});
  $("#identityUserRoleFilter").addEventListener("change",function(){state.identityUsersPage=1;renderIdentityUsers();});
  $("#identityUserStatusFilter").addEventListener("change",function(){state.identityUsersPage=1;renderIdentityUsers();});
  $("#resetIdentityUserFilters").addEventListener("click",function(){$("#identityUserSearch").value="";$("#identityUserRoleFilter").value="all";$("#identityUserStatusFilter").value="all";state.identityUsersPage=1;renderIdentityUsers();});
  $("#identityUsersPrevButton").addEventListener("click",function(){state.identityUsersPage-=1;renderIdentityUsers();});
  $("#identityUsersNextButton").addEventListener("click",function(){state.identityUsersPage+=1;renderIdentityUsers();});
  $("#identityProjectSearch").addEventListener("input",function(){state.identityProjectsPage=1;renderIdentityProjects();});
  $("#identityProjectStatusFilter").addEventListener("change",function(){state.identityProjectsPage=1;renderIdentityProjects();});
  $("#resetIdentityProjectFilters").addEventListener("click",function(){$("#identityProjectSearch").value="";$("#identityProjectStatusFilter").value="all";state.identityProjectsPage=1;renderIdentityProjects();});
  $("#identityProjectsPrevButton").addEventListener("click",function(){state.identityProjectsPage-=1;renderIdentityProjects();});
  $("#identityProjectsNextButton").addEventListener("click",function(){state.identityProjectsPage+=1;renderIdentityProjects();});
  $("#identityRoleSearch").addEventListener("input",function(){state.identityRolesPage=1;renderIdentityRoles();});
  $("#resetIdentityRoleFilters").addEventListener("click",function(){$("#identityRoleSearch").value="";state.identityRolesPage=1;renderIdentityRoles();});
  $("#identityRolesPrevButton").addEventListener("click",function(){state.identityRolesPage-=1;renderIdentityRoles();});
  $("#identityRolesNextButton").addEventListener("click",function(){state.identityRolesPage+=1;renderIdentityRoles();});
  document.addEventListener("keydown",function(event){if(event.key==="Escape"){closeServerProfileDrawer();closeGpuDetailDrawer();["interfaceModuleDialog","currentProjectDialog","interfaceEnvironmentDialog","interfaceVariableDialog","interfaceVersionsDialog","interfaceScenarioRunDialog"].forEach(closeInterfaceDialog);["identityUserDrawer","identityProjectDrawer","identityMembershipDrawer"].forEach(closeIdentityDrawer);}});
  window.addEventListener("resize",function(){const run=state.runs.find(function(x){return x.id===state.monitorRunId;});if(run)renderMonitor(run);const session=state.serverSessions.find(function(x){return x.id===state.serverSessionId;});if(session)renderServerSessionMetrics(session);});
}
document.addEventListener("DOMContentLoaded",function(){
  bind();
  syncServerSaveOptions();
  syncUploadModeUi();
  applyStressPreset("quick");
  initializeAuth();
});
