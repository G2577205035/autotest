import unittest
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

from tests import bootstrap  # noqa: F401


STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "auto_test" / "static"


class _WorkspaceMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.tabs = defaultdict(set)
        self.panels = defaultdict(set)

    def handle_starttag(self, _tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        tab_group = values.get("data-workspace-tab")
        if tab_group:
            self.tabs[tab_group].add(values.get("data-workspace-target"))
        panel_group = values.get("data-workspace-panel")
        if panel_group:
            self.panels[panel_group].add(values.get("data-workspace-name"))


class StaticWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        cls.javascript = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        cls.stylesheet = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        cls.parser = _WorkspaceMarkupParser()
        cls.parser.feed(cls.html)

    def test_page_ids_are_unique(self):
        self.assertEqual(len(self.parser.ids), len(set(self.parser.ids)))

    def test_workspace_tabs_have_matching_panels(self):
        self.assertEqual(set(self.parser.tabs), {"stress", "reports", "interfaces"})
        self.assertEqual(dict(self.parser.tabs), dict(self.parser.panels))
        self.assertEqual(self.parser.tabs["stress"], {"sessions", "setup", "live", "history"})
        self.assertEqual(self.parser.tabs["reports"], {"create", "template", "jobs"})
        self.assertEqual(
            self.parser.tabs["interfaces"], {"assets", "scenarios", "settings", "import"}
        )

    def test_interface_center_exposes_project_assets_and_secret_safe_variables(self):
        for element_id in (
            "interfaceEditorView",
            "interfaceModuleDialog",
            "interfaceEnvironmentDialog",
            "interfaceVariableDialog",
            "interfaceVersionsDialog",
            "interfaceVariableBody",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('/api/interface-assets/workspace', self.javascript)
        self.assertIn('function interfaceCanManage()', self.javascript)
        self.assertIn('function showInterfaceVersions(id)', self.javascript)
        self.assertIn('valueState=item.is_secret?', self.javascript)
        self.assertIn('class="page-tabs workspace-tabs"', self.html)
        self.assertIn('id="interfaceProjectSwitcher"', self.html)
        self.assertIn('id="currentProjectDialog"', self.html)
        self.assertIn('id="manageCurrentProjectButton"', self.html)
        self.assertIn('class="interface-project-tree"', self.html)
        self.assertIn('data-interface-module="__ungrouped__"', self.javascript)
        self.assertNotIn('class="interface-summary stat-grid"', self.html)
        self.assertIn('.interface-catalog-layout{height:calc(100vh - 182px)', self.stylesheet)

    def test_interface_editor_is_independent_readable_and_accepts_full_urls(self):
        self.assertIn("接口工作台", self.html)
        self.assertIn('id="interfaceCatalogView"', self.html)
        self.assertIn('id="interfaceEditorView"', self.html)
        self.assertIn('id="interfaceSendButton"', self.html)
        self.assertIn('id="interfaceDebugResponse"', self.html)
        self.assertIn('data-interface-response-tab="request"', self.html)
        self.assertIn('id="interfaceAssetTargetHint"', self.html)
        self.assertIn('id="interfaceQueryRows"', self.html)
        self.assertIn('id="interfaceHeaderRows"', self.html)
        self.assertIn('id="interfaceAssetBody"', self.html)
        self.assertIn('data-interface-request-tab="query"', self.html)
        self.assertIn('function interfaceTargetUrl(item)', self.javascript)
        self.assertIn('function readInterfaceKeyValueRows(kind)', self.javascript)
        self.assertIn('async function sendInterfaceDebugRequest()', self.javascript)
        self.assertIn('api("/api/interface-debug"', self.javascript)
        self.assertIn('setInterfaceAssetError("保存失败："+error.message)', self.javascript)
        self.assertNotIn('id="interfaceAssetDialog"', self.html)
        self.assertNotIn('id="interfaceAssetDefaultPath"', self.html)
        self.assertNotIn('id="interfaceDefaultPath"', self.html)
        self.assertNotIn('$("#interfaceAssetDefaultPath")', self.javascript)
        self.assertNotIn('$("#interfaceDefaultPath")', self.javascript)
        self.assertIn('.interface-editor-view{height:100%', self.stylesheet)
        self.assertIn('.interface-debug-response{min-height:270px', self.stylesheet)

    def test_interface_editor_parses_pasted_curl_and_supports_body_encodings(self):
        self.assertIn('id="interfaceCurlPasteStatus"', self.html)
        self.assertIn('id="interfaceAssetBodyType"', self.html)
        self.assertIn('value="multipart"', self.html)
        self.assertIn('value="urlencoded"', self.html)
        self.assertIn('async function handleInterfaceCurlPaste(event)', self.javascript)
        self.assertIn('api("/api/interface-assets/parse-curl"', self.javascript)
        self.assertIn('body_type:parsedBody.type', self.javascript)
        self.assertIn('.interface-curl-paste-status[data-state="success"]', self.stylesheet)

    def test_interface_scenarios_expose_visual_steps_execution_and_reports(self):
        for element_id in (
            "interfaceScenarioCatalog",
            "interfaceScenarioEditor",
            "interfaceScenarioSteps",
            "interfaceScenarioRunDialog",
            "batchRunInterfaceScenariosButton",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("function renderInterfaceScenarios()", self.javascript)
        self.assertIn("function readInterfaceScenarioPayload()", self.javascript)
        self.assertIn("async function runInterfaceScenario(id)", self.javascript)
        self.assertIn('/api/interface-scenarios/batch-execute', self.javascript)
        self.assertIn('data-interface-scenario-run-stop', self.javascript)
        self.assertIn('/stop",{method:"POST"}', self.javascript)
        self.assertIn('/download/docx', self.javascript)
        self.assertIn('/download/pdf', self.javascript)
        self.assertIn('passed:"通过"', self.javascript)
        self.assertIn('.interface-scenario-step{', self.stylesheet)
        self.assertIn('.interface-scenario-result-summary{', self.stylesheet)

    def test_initial_page_load_defers_inactive_module_requests(self):
        self.assertIn('const jobs=[loadRuns(),loadReports()]', self.javascript)
        self.assertIn(
            'if($("#view-interfaces").classList.contains("active"))jobs.push(loadInterfaces())',
            self.javascript,
        )
        self.assertIn(
            'if (name === "new-run") loadServerWorkspace(false)', self.javascript
        )
        self.assertNotIn(
            'Promise.allSettled([loadRuns(), loadReports(), loadModels(), loadInterfaces()',
            self.javascript,
        )

    def test_model_evaluation_workspace_exposes_submit_monitor_stop_and_results(self):
        for element_id in (
            "view-evaluation",
            "evaluationForm",
            "evaluationRunKind",
            "evaluationModelProfile",
            "evaluationRunsBody",
            "evaluationProgressBar",
            "evaluationEventsList",
            "evaluationResultsBody",
            "stopEvaluationButton",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('data-view="evaluation"', self.html)
        self.assertIn('async function loadEvaluationWorkspace(reset)', self.javascript)
        self.assertIn('api("/api/model-evaluation/bootstrap")', self.javascript)
        self.assertIn('api("/api/model-evaluation/runs",{method:"POST"', self.javascript)
        self.assertIn('function refreshSelectedEvaluationRun()', self.javascript)
        self.assertIn('downloadEvaluationArtifact("performance")', self.javascript)
        self.assertIn('hasPermission("evaluation:operate")', self.javascript)
        self.assertIn('.evaluation-metric-grid{display:grid;grid-template-columns:repeat(5', self.stylesheet)
        self.assertIn('.evaluation-detail-grid{display:grid;grid-template-columns:', self.stylesheet)

    def test_business_project_switchers_hide_internal_project_keys(self):
        self.assertIn('function businessProjectLabel(project)', self.javascript)
        self.assertIn('esc(businessProjectLabel(project))+"</option>"', self.javascript)
        self.assertIn('<small>业务项目</small>', self.html)
        self.assertNotIn('id="interfaceProjectKey"', self.html)
        self.assertNotIn('$("#interfaceProjectKey")', self.javascript)

    def test_new_run_form_collects_one_time_credentials_and_server_assets(self):
        self.assertIn('id="runUsername"', self.html)
        self.assertIn('id="runPassword"', self.html)
        self.assertIn('id="runServerProfile"', self.html)
        self.assertIn('id="runGpuServerProfile"', self.html)
        self.assertIn('id="runAdministratorUsername"', self.html)
        self.assertIn('id="runAdministratorPassword"', self.html)
        self.assertNotIn('id="runAccount"', self.html)
        self.assertIn('if (!username) throw new Error("请填写登录用户名")', self.javascript)
        self.assertIn('if (!password) throw new Error("请填写登录密码")', self.javascript)
        self.assertIn('function populateRunServerProfiles()', self.javascript)
        self.assertIn('function syncRunServerProfileFields(clearSecrets)', self.javascript)
        self.assertIn('function reconfigureRun(runId)', self.javascript)
        self.assertNotIn('/restart', self.javascript)
        self.assertNotIn('$("#runHost").value = (state.bootstrap', self.javascript)
        self.assertNotIn('$("#runSshUser").value = env.app_user || "root"', self.javascript)
        self.assertIn('function defaultRunCaseName(date)', self.javascript)
        self.assertIn('$("#runCaseName").value=defaultRunCaseName()', self.javascript)
        self.assertIn('$("#runTranslate").value=defaultTranslateName()', self.javascript)
        self.assertIn('if (!translateName) throw new Error("请填写翻译语种")', self.javascript)

    def test_new_run_form_explains_required_fields_and_conditional_sources(self):
        self.assertIn('class="form-required-note"', self.html)
        self.assertIn('选择客户本地测试文件夹', self.html)
        self.assertNotIn('id="runUploadPathField"', self.html)
        self.assertNotIn('id="runUploadPath"', self.html)
        self.assertIn('class="advanced-options run-advanced-options"', self.html)
        self.assertIn('class="advanced-state-closed">展开设置</span>', self.html)
        self.assertIn('if (!files.length)', self.javascript)
        self.assertIn('form.append("files", file, file.webkitRelativePath || file.name)', self.javascript)
        self.assertIn('平台暂存后由 Worker 通过 SSH/SFTP 直传到业务服务器', self.javascript)
        self.assertNotIn('$("#runUploadPath")', self.javascript)
        self.assertIn('if (!caseName) throw new Error("请填写案件名称")', self.javascript)
        self.assertIn('#view-new-run .run-advanced-options>summary', self.stylesheet)

    def test_business_monitor_gpu_card_uses_average_and_peak(self):
        self.assertIn("GPU 均值 / 峰值", self.html)
        self.assertNotIn("GPU 当前 / 峰值", self.html)
        self.assertIn('updateAveragePeakMetric("gpuNow", gpu, "%")', self.javascript)

    def test_business_monitor_displays_trusted_cpu_package_temperatures(self):
        self.assertIn("CPU 封装温度 当前 / 峰值", self.html)
        self.assertIn('id="businessTempReadings"', self.html)
        self.assertIn('id="businessTempSource"', self.html)
        self.assertIn('function renderBusinessCpuTemperatureReadings(metrics)', self.javascript)
        self.assertIn('renderBusinessCpuTemperatureReadings(metrics);', self.javascript)
        self.assertIn('renderCpuTemperatureCard(metrics,"businessTempReadings","businessTempSource"', self.javascript)
        self.assertIn('.business-monitor-metrics{grid-template-columns:repeat(5', self.stylesheet)

    def test_business_monitor_displays_backend_translation_average_speed(self):
        self.assertIn("翻译平均速度", self.html)
        self.assertIn('id="translationSpeedNow"', self.html)
        self.assertIn('id="translationSpeedProgress"', self.html)
        self.assertIn('renderTranslationSpeed(run.translation_speed || {});', self.javascript)
        self.assertIn('Number(speed.items_per_minute).toFixed(2)+" 个/分钟"', self.javascript)
        self.assertIn('统计区间新增 ', self.javascript)

    def test_stage_timeline_places_latest_event_at_bottom_and_follows_it(self):
        self.assertIn('const timelineNode = $("#eventTimeline");', self.javascript)
        self.assertIn('.slice(-14).map(function(item)', self.javascript)
        self.assertNotIn('.slice(-14).reverse().map(function(item)', self.javascript)
        self.assertIn('timelineNode.scrollTop = timelineNode.scrollHeight;', self.javascript)

    def test_monitor_polling_deduplicates_events_and_prevents_overlap(self):
        self.assertIn('function mergeRowsById(current, incoming, limit)', self.javascript)
        self.assertIn('if (state.monitorRefreshInFlight[runId]) return;', self.javascript)
        self.assertIn('state.logs[runId] = mergeRowsById(', self.javascript)
        self.assertIn('state.metrics[runId] = mergeRowsById(', self.javascript)
        self.assertIn('delete state.monitorRefreshInFlight[runId];', self.javascript)

    def test_desktop_platform_brand_remains_above_module_title_hierarchy(self):
        self.assertIn('.brand{min-height:76px;gap:10px;padding:4px 0 18px}', self.stylesheet)
        self.assertIn('.brand-mark{width:44px;height:44px;flex-basis:44px}', self.stylesheet)
        self.assertIn('.brand>strong{font-size:18px}', self.stylesheet)
        self.assertIn('.page-heading h1{font-size:17px}', self.stylesheet)
        self.assertIn('.page-heading p{font-size:13px}', self.stylesheet)

    def test_server_temperature_card_is_cpu_only_and_displays_sensor_source(self):
        self.assertIn("CPU 各传感器 当前 / 峰值", self.html)
        self.assertNotIn("最热封装/核心", self.html)
        self.assertIn('id="stressTempReadings"', self.html)
        self.assertIn('id="stressTempSource"', self.html)
        self.assertIn('function cpuTemperatureSeries(metrics)', self.javascript)
        self.assertIn('data.cpu_temp_status !== "available"', self.javascript)
        self.assertIn('function renderCpuTemperatureReadings(metrics)', self.javascript)
        self.assertIn('当前会话仍在使用旧版采集，请重启服务并重新连接', self.javascript)
        self.assertIn('current.toFixed(1)+\' °C / \'+peak.toFixed(1)', self.javascript)
        self.assertNotIn('key === "cpu_temp_c" || key === "gpu_temp_c"', self.javascript)

    def test_desktop_typography_has_readable_1080p_baseline_and_cache_version(self):
        self.assertIn('styles.css?v=20260902.1', self.html)
        self.assertIn('app.js?v=20260902.1', self.html)
        self.assertIn('@media(min-width:981px)', self.stylesheet)
        self.assertIn('body{font-size:16px;line-height:1.6}', self.stylesheet)
        self.assertIn('table{font-size:15px}', self.stylesheet)
        self.assertIn('.field small{font-size:13px}', self.stylesheet)
        self.assertIn('.page-tabs{overflow-y:hidden}', self.stylesheet)
        self.assertIn('.stress-form .stress-step{background:#0b1926}', self.stylesheet)
        self.assertIn('.capability-result h4{color:#e5eef7}', self.stylesheet)

    def test_report_run_candidates_use_lightweight_run_directory_flag(self):
        self.assertIn(
            'Boolean(run.has_run_dir || run.run_dir)',
            self.javascript,
        )

    def test_model_security_guidance_is_a_readable_structured_panel(self):
        self.assertIn('class="security-note-head"', self.html)
        self.assertIn('API Key 安全保护', self.html)
        self.assertIn('class="security-flow"', self.html)
        self.assertIn('id="modelCount"', self.html)
        self.assertIn('.security-note h3{margin:0;color:#fff;font-size:22px', self.stylesheet)
        self.assertIn('.security-flow strong{color:#edf6ff;font-size:15px', self.stylesheet)
        self.assertIn('$("#modelCount").textContent = state.models.length + " 项"', self.javascript)

    def test_report_workspace_removes_legacy_width_caps_and_fills_canvas(self):
        self.assertNotIn('grid-template-columns:minmax(0,1040px)', self.stylesheet)
        self.assertNotIn('[data-workspace-name="create"]{max-width:1040px}', self.stylesheet)
        self.assertNotIn('[data-workspace-name="template"]{max-width:1180px}', self.stylesheet)
        self.assertIn('#view-reports .report-layout{display:grid;grid-template-columns:minmax(0,1fr)', self.stylesheet)
        self.assertIn('grid-template-columns:repeat(auto-fit,minmax(430px,1fr))', self.stylesheet)

    def test_server_performance_workspace_cannot_fall_back_to_light_cards(self):
        self.assertIn('#view-stress #stressForm .stress-step,#view-stress #stressForm .stress-step[open]', self.stylesheet)
        self.assertIn('background:#0a1926!important', self.stylesheet)
        self.assertIn('#view-stress #stressForm .stress-step-content', self.stylesheet)
        self.assertIn('background:transparent!important', self.stylesheet)
        self.assertIn('#view-stress .stress-status-grid .capability-result', self.stylesheet)
        self.assertIn('.capability-result h4{color:#eef6fd!important}', self.stylesheet)

    def test_stateful_components_are_protected_from_legacy_light_specificity(self):
        self.assertIn('#view-new-run .advanced-options .choice-box', self.stylesheet)
        self.assertIn('background:#0a1825!important', self.stylesheet)
        self.assertNotIn('.upload-zone.path-mode', self.stylesheet)
        self.assertIn('#view-stress .gpu-device-card.selected', self.stylesheet)
        self.assertIn('#view-stress .gpu-empty-state.blocked', self.stylesheet)
        self.assertIn('.toast.error{color:#ffb4b8!important', self.stylesheet)

    def test_formal_login_replaces_the_retired_page_key_gate(self):
        self.assertIn('id="authOverlay"', self.html)
        self.assertIn('id="loginForm"', self.html)
        self.assertIn('id="setupForm"', self.html)
        self.assertNotIn('id="apiKeyInput"', self.html)
        self.assertNotIn('id="connectButton"', self.html)
        self.assertIn('.auth-overlay', self.stylesheet)
        self.assertNotIn('X-API-Key', self.javascript)
        self.assertNotIn('liema_api_key', self.javascript)
        self.assertNotIn('页面与模型密钥隔离', self.javascript)
        self.assertIn('X-CSRF-Token', self.javascript)
        self.assertIn('initializeAuth();', self.javascript)

    def test_login_portal_is_a_restrained_full_screen_split_layout(self):
        self.assertIn('class="auth-portal"', self.html)
        self.assertIn('class="auth-overview"', self.html)
        self.assertIn('class="auth-entry"', self.html)
        self.assertIn('class="auth-platform-scope"', self.html)
        self.assertIn('class="auth-scene"', self.html)
        self.assertNotIn('安全账户入口', self.html)
        self.assertNotIn('访问安全由平台统一保护', self.html)
        self.assertNotIn('质量工作流', self.html)
        self.assertIn('grid-template-columns:58% 42%', self.stylesheet)
        self.assertIn('.auth-entry{color-scheme:light', self.stylesheet)
        self.assertIn('<h1 id="authTitle">登录</h1>', self.html)
        self.assertNotIn('ALL-IN-ONE QUALITY ENGINEERING', self.html)
        self.assertNotIn('auth-overview-kicker', self.html)
        self.assertIn('.auth-intro{display:none', self.stylesheet)
        self.assertIn('$("#authIntro").textContent=message || "";', self.javascript)
        self.assertIn('$("#authOverlay").dataset.mode=setupRequired?"setup":"login";', self.javascript)

    def test_identity_workspace_exposes_project_roles_and_audit(self):
        self.assertIn('id="view-identity"', self.html)
        self.assertIn('id="projectSwitcher"', self.html)
        self.assertIn('id="identityUsersBody"', self.html)
        self.assertIn('id="identityMembersBody"', self.html)
        self.assertIn('id="identityAuditBody"', self.html)
        self.assertIn('id="identityAuditPrevButton"', self.html)
        self.assertIn('id="identityAuditNextButton"', self.html)
        self.assertIn('id="identityAuditPageSummary"', self.html)
        self.assertIn('project_admin:"项目管理员"', self.javascript)
        self.assertIn('/api/identity/audit-events?page=', self.javascript)
        self.assertIn('identityAuditPageSize: 20', self.javascript)
        self.assertIn('await switchProject(project.id,"dashboard")', self.javascript)
        self.assertIn('用户与项目', self.html)
        self.assertIn('method:"PATCH",body:payload', self.javascript)
        self.assertIn('烈马是平台名称', self.html)
        self.assertNotIn('id="setupProjectKey" value="LIEMA"', self.html)
        self.assertNotIn('id="setupProjectName" value="烈马测试项目"', self.html)

    def test_dashboard_removes_demo_readiness_and_shows_operational_run_fields(self):
        self.assertNotIn('上线准备度', self.html)
        self.assertNotIn('readinessScore', self.html)
        self.assertNotIn('renderReadiness', self.javascript)
        self.assertIn('class="panel active-panel dashboard-current-panel"', self.html)
        for heading in ("测试案件", "目标环境", "测试内容", "开始时间", "耗时", "执行结果", "操作"):
            self.assertIn(f"<th>{heading}</th>", self.html)
        self.assertIn('options.case_name || "未命名案件"', self.javascript)
        self.assertIn('options.host || "未记录目标服务器"', self.javascript)
        self.assertIn('function fmtDuration(run)', self.javascript)
        self.assertIn('class="recent-run-result"', self.javascript)
        self.assertIn('.classList.toggle("is-empty", !active)', self.javascript)
        self.assertIn('state.dashboardRunsSignature !== recentSignature', self.javascript)
        self.assertIn('#view-dashboard>.dashboard-current-panel{min-height:154px}', self.stylesheet)

    def test_stress_recovery_uses_versioned_authorization_and_visible_actions(self):
        self.assertIn('authorized:"已授权待执行"', self.javascript)
        self.assertIn('["authorized","queued","running"].includes(job.status)', self.javascript)
        self.assertIn('重新配置授权', self.javascript)
        self.assertIn('.status.authorized{color:#8dccff!important', self.stylesheet)
        self.assertIn('th:last-child,#view-stress [data-workspace-name="history"] td:last-child{position:sticky', self.stylesheet)

    def test_server_performance_is_session_first_and_reports_are_selectable(self):
        self.assertIn('data-workspace-target="sessions">服务器会话', self.html)
        self.assertIn('id="serverProfileList"', self.html)
        self.assertIn('id="serverProfileDrawer"', self.html)
        self.assertIn('id="serverProfileSearch"', self.html)
        self.assertIn('class="server-asset-table"', self.html)
        self.assertIn('id="serverSaveConfig"', self.html)
        self.assertIn('id="serverSavePassword"', self.html)
        self.assertIn('id="serverSessionSelect"', self.html)
        self.assertIn('id="stopServerSessionButton"', self.html)
        self.assertIn('id="stressReportJobSelect"', self.html)
        self.assertIn('data-session-open=', self.javascript)
        self.assertIn('/api/server-sessions/', self.javascript)
        self.assertIn('class="text-button stress-download"', self.javascript)
        self.assertIn('function openServerProfileDrawer()', self.javascript)
        self.assertIn('serverProfileStatusFilter', self.javascript)
        self.assertIn('.server-profile-drawer.open', self.stylesheet)
        self.assertIn('white-space:normal!important', self.stylesheet)

    def test_reported_interactions_have_visible_pending_states_and_dense_gpu_layout(self):
        self.assertIn('closingServerSessionId', self.javascript)
        self.assertIn('正在关闭 SSH 会话与实时采集', self.javascript)
        self.assertIn('模型连通测试已开始', self.javascript)
        self.assertIn('当前数值变化较小，采集仍在持续', self.javascript)
        self.assertNotIn('strategy.recommended_preset === "health") {\n    applyStressPreset("custom")', self.javascript)
        self.assertIn('.server-profile-form .server-save-options .switch-row>input[type="checkbox"]', self.stylesheet)
        self.assertIn('grid-template-columns:repeat(auto-fit,minmax(370px,1fr))', self.stylesheet)
        self.assertIn('.session-live-badge.closing', self.stylesheet)
        self.assertIn('.model-action-feedback.pending', self.stylesheet)

    def test_stress_resource_step_only_auto_opens_on_first_setup_visit(self):
        self.assertIn('stressResourcesAutoOpened: false', self.javascript)
        self.assertIn('function openStressResourcesOnce()', self.javascript)
        self.assertIn('if(group === "stress" && name === "setup") openStressResourcesOnce();', self.javascript)
        self.assertEqual(self.javascript.count('$("#stressStepResources").open=true;'), 1)
        render_capability = self.javascript.split('function renderCapability(report)', 1)[1].split('async function submitStress', 1)[0]
        self.assertNotIn('$("#stressStepResources").open=true;', render_capability)

    def test_live_session_selector_status_and_action_share_one_control_baseline(self):
        self.assertIn('gap:18px;align-items:end;margin-bottom:16px', self.stylesheet)
        self.assertIn('.server-live-toolbar .server-live-actions{align-self:end;flex-wrap:nowrap}', self.stylesheet)
        self.assertIn('flex:0 0 112px;width:112px;min-height:46px;height:46px', self.stylesheet)
        self.assertIn('display:inline-flex;align-items:center;justify-content:center', self.stylesheet)
        self.assertIn('padding:0 8px;font-size:14px;line-height:1;white-space:nowrap', self.stylesheet)

    def test_stress_setup_can_switch_the_active_ssh_session_in_place(self):
        self.assertIn('id="stressSessionSelect"', self.html)
        self.assertNotIn('id="stressActiveSessionSummary"', self.html)
        self.assertIn('["serverSessionSelect","stressSessionSelect"].forEach', self.javascript)
        self.assertIn('function syncServerSessionSelectors(sessionId)', self.javascript)
        self.assertIn('openServerSession(this.value,"setup")', self.javascript)
        self.assertIn('activateWorkspaceTab("stress",workspaceName || "live")', self.javascript)
        self.assertIn('if(state.serverSessionId!==sessionId)', self.javascript)
        self.assertIn('.stress-session-switcher{width:min(380px,40vw);min-width:320px;margin:0}', self.stylesheet)

    def test_starting_stress_opens_a_live_task_monitor_and_keeps_polling_it(self):
        self.assertIn('id="stressLiveTaskPanel"', self.html)
        self.assertIn('id="stressLiveTaskStatus"', self.html)
        self.assertIn('id="stressLiveTaskProgressBar"', self.html)
        self.assertIn('id="stressLiveStopButton"', self.html)
        self.assertIn('function renderStressTask(job)', self.javascript)
        self.assertIn('function refreshStressLiveWorkspace(reset)', self.javascript)
        self.assertIn('if(group === "stress" && name === "live") refreshStressLiveWorkspace(true);', self.javascript)
        self.assertIn('renderStressTask(job);\n  renderCpuTemperatureReadings(metrics);\n  if(!metrics.length)', self.javascript)
        self.assertIn('if(isActiveStressJob(job)) await refreshStress(false);', self.javascript)
        self.assertIn('else if(job) renderStressTask(job);', self.javascript)
        self.assertIn('stopStressJob(state.stressJobId)', self.javascript)
        self.assertIn('.stress-live-task[data-status="running"]{border-color:#3175a8}', self.stylesheet)
        self.assertIn('.stress-live-task-meta{display:grid;grid-template-columns:1.2fr 1fr 1fr .7fr', self.stylesheet)

    def test_gpu_live_cards_open_details_in_a_right_side_drawer(self):
        self.assertIn('data-gpu-expand=', self.javascript)
        self.assertIn('function toggleGpuLiveDetails(index)', self.javascript)
        self.assertIn('function renderGpuDetailDrawer(metrics,index,status,gpuRows)', self.javascript)
        self.assertIn('function closeGpuDetailDrawer()', self.javascript)
        self.assertIn('class="gpu-card-footer"', self.javascript)
        self.assertIn('指标详情', self.javascript)
        self.assertIn('在右侧查看 GPU ', self.javascript)
        self.assertIn('id="gpuDetailDrawer"', self.html)
        self.assertIn('id="closeGpuDetailDrawer"', self.html)
        self.assertIn('gpu-status-', self.javascript)
        self.assertIn('次采样</small>', self.javascript)
        self.assertNotIn('点 · 读数稳定', self.javascript)
        self.assertNotIn('class="gpu-live-details"', self.javascript)
        self.assertNotIn('class="gpu-live-charts"', self.javascript)
        self.assertIn('#view-stress .gpu-live-card.selected{border-color:#419cff', self.stylesheet)
        self.assertIn('.gpu-detail-drawer.open{visibility:visible;transform:translateX(0)}', self.stylesheet)
        self.assertIn('.gpu-drawer-detail-grid{display:grid;grid-template-columns:repeat(2', self.stylesheet)
        self.assertIn('.gpu-drawer-charts canvas{width:100%;height:150px}', self.stylesheet)
        self.assertIn('border:1px solid #337a67', self.stylesheet)
        self.assertNotIn('data-stress-workspace=', self.html)
        self.assertNotIn('data-current-view=', self.html)
        self.assertNotIn('neutral light canvas', self.stylesheet)
        self.assertIn('#view-stress .gpu-live-card{min-height:0;padding:0', self.stylesheet)
        self.assertIn('.gpu-card-footer{display:flex;align-items:center;justify-content:flex-end', self.stylesheet)
        self.assertIn('.gpu-card-expand{width:auto;min-height:30px', self.stylesheet)
        self.assertIn('.gpu-card-expand[aria-expanded="true"] svg{transform:rotate(180deg)}', self.stylesheet)


if __name__ == "__main__":
    unittest.main()
