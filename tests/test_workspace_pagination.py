import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import bootstrap  # noqa: F401
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore
from auto_test.platform.api import create_platform_api
from auto_test.platform.artifact_storage import create_artifact_storage


class WorkspacePaginationTests(unittest.TestCase):
    def run_javascript(self, body):
        node=shutil.which('node')
        if not node: self.skipTest('Node.js is required for JavaScript behavior checks')
        source=Path(__file__).resolve().parents[1]/'src/auto_test/static/app.js'
        prelude=r'''
const fs=require('node:fs'),vm=require('node:vm');
const nodes=new Map(),listeners={};
function makeNode(){const children=new Map();return {innerHTML:'',textContent:'',dataset:{},value:'',open:false,disabled:false,scrollTop:77,setAttribute(){},setCustomValidity(value){this.validationMessage=value;},reportValidity(){},classList:{toggle(){},add(){},remove(){}},querySelector(key){if(!children.has(key))children.set(key,makeNode());return children.get(key);}};}
const document={activeElement:null,addEventListener(type,fn){(listeners[type]??=[]).push(fn);},querySelector(key){if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key);}};
const context=vm.createContext({document,console,setTimeout,clearTimeout,URLSearchParams});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext('(async()=>{const check=(ok,message)=>{if(!ok)throw Error(message);};'+process.argv[2]+'})()',context).then(()=>console.log('BEHAVIOR_OK')).catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run([node,'-e',prelude,str(source),body],text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('BEHAVIOR_OK',result.stdout)

    def test_shared_page_size_jump_validation_and_polling_preserve_input(self):
        self.run_javascript(r'''
const rows=Array.from({length:235},(_,i)=>i);
renderModels=()=>listPage('models',rows,3);
renderModels();changeListPageSize('models',20);
check(state.listPages.models===1&&listPage('models',rows,3).items.length===20,'size controls actual rows');
const pager=$('#modelsPagination'),input=pager.querySelector('[data-list-jump-input]');input.dataset.listJumpInput='models';
input.value='7';document.activeElement=input;renderModels();check(input.value==='7','poll preserves draft');
jumpListPage(input);check(listPage('models',rows,3).items[0]===120,'jump uses selected size');
for(const value of ['', '0', '-1', '1.5', '999', 'NaN', 'Infinity']){input.value=value;jumpListPage(input);check(state.listPages.models===7&&input.validationMessage,'invalid page refused: '+value);}
input.value='12';jumpListPage(input);check(listPage('models',rows,3).items.length===15,'last page');
changeListPageSize('models',3);check(listPage('models',rows,3).items.length===3,'original size remains selectable');
changeListPageSize('models',999);check(listPageSize('models')===3,'invalid size refused');
listPage('serverProfiles',rows,8);check(listPageSize('serverProfiles',8)===8,'lists independent');
state.listPages.models=7;check(listPage('models',rows,3,'changed').page===1,'filter resets');
listPage('models',[],3);check(input.disabled&&pager.querySelector('[data-list-jump]').disabled,'empty jump disabled');
''')

    def test_remote_page_size_changes_discard_previous_size_responses(self):
        self.run_javascript(r'''
const pending=[];api=url=>new Promise(resolve=>pending.push({url,resolve}));state.projectId='project';
const applied=[];renderListPager('reportJobs',{total:125,page:1,page_size:10});
loadReports=()=>loadHistoryPage('reportJobs','/api/reports','reports',data=>applied.push(data.marker));
const first=loadReports();changeListPageSize('reportJobs',50);
check(pending[1].url.endsWith('?page=1&page_size=50'),'remote size propagated');
pending[1].resolve({page:1,total:125,page_size:50,marker:'latest'});await Promise.resolve();await Promise.resolve();
pending[0].resolve({page:1,total:125,page_size:10,marker:'stale'});await first;
check(applied.join(',')==='latest','previous size cannot overwrite');
changeListPage('reportJobs',3);check(pending[2].url.endsWith('?page=3&page_size=50'),'jump requests remote page');
pending[2].resolve({page:3,total:125,page_size:50});await Promise.resolve();
evaluationSuiteDetail={id:'suite',latest_version:{id:'version'}};renderListPager('evaluationSuiteCases',{total:137,page:1,page_size:5});
changeListPageSize('evaluationSuiteCases',20);check(pending[3].url.endsWith('?page=1&page_size=20'),'drawer selected size');
pending[3].resolve({version:{version:1},total:137,page:1,page_size:20,cases:[]});await Promise.resolve();
''')

    def test_legacy_directories_and_report_sections_share_independent_pagers(self):
        self.run_javascript(r'''
const rows=Array.from({length:55},(_,i)=>({id:String(i)}));
state.identityUsersPage=1;renderIdentityUsers=()=>identityPage(rows,'identityUsersPage');
renderIdentityUsers();changeListPageSize('identityUsers',20);changeListPage('identityUsers',3);
check(renderIdentityUsers().items[0].id==='40'&&state.identityUsersPage===3,'legacy directory uses chosen page size');
const section={title:'测试方法',headers:['条目'],rows:Array.from({length:35},(_,i)=>['指标'+i])};
state.evaluationReport={system_run_id:'run-a',sections:[section,section],intro_sections:[section]};
renderEvaluationReportSections(state.evaluationReport);renderEvaluationReportSections(state.evaluationReport,true);
changeListPage('evaluationSection_metrics_1',2);changeListPageSize('evaluationSection_metrics_0',20);changeListPage('evaluationSection_metrics_0',2);
check($('#evaluationSection_metrics_0 tbody').innerHTML.includes('指标20'),'metric table page size');
check(state.listPages.evaluationSection_metrics_1===2,'other section page preserved');
check(state.listPages.evaluationSection_intro_0===1,'intro isolated from metric table');
state.evaluationReport.system_run_id='run-b';renderEvaluationReportSections(state.evaluationReport);
check(state.listPages.evaluationSection_metrics_0===1&&listPageSize('evaluationSection_metrics_0')===20,'new report resets page and retains size');
state.evaluationComparisonResult={comparable:true,rows:Array.from({length:10},(_,i)=>({run_id:'run-'+i,model_name:'模型'+i})),ranking:[]};
renderEvaluationComparison();changeListPageSize('evaluationComparisonResults',100);
check(state.listMeta.evaluationComparisonResults.total===10&&$('#evaluationComparisonResult').innerHTML.includes('模型9'),'all comparison rows retained with shared controls');
''')

    def test_audit_size_and_refresh_races_do_not_restore_old_pages(self):
        self.run_javascript(r'''
state.identity={user:{id:'admin',is_superuser:true}};state.projectId='a';
const pending=[];api=url=>new Promise(resolve=>pending.push({url,resolve}));
renderListPager('identityAudit',{total:130,page:1,page_size:20});
const first=loadIdentityAudit(2);changeListPageSize('identityAudit',50);
check(pending[1].url.endsWith('page=1&page_size=50'),'audit size request');
pending[1].resolve({events:[],page:1,page_size:50,total:130,total_pages:3});await Promise.resolve();await Promise.resolve();
pending[0].resolve({events:[{actor_username:'stale'}],page:2,page_size:20,total:130,total_pages:7});await first;
check(state.identityAuditPage===1&&state.identityAuditPageSize===50&&state.identityAudit.length===0,'stale audit response ignored');
const another=loadIdentityAudit(3);state.projectId='b';pending[2].resolve({events:[{actor_username:'wrong-project'}],page:3,page_size:50,total:130});await another;
check(state.identityAuditPage===1,'project switch drops in-flight audit response');
''')

    def test_evaluation_result_size_and_ordinals_remain_consistent(self):
        self.run_javascript(r'''
state.projectId='a';state.selectedEvaluationRunId='run';state.evaluationResultsRunId='run';
state.evaluationRuns=[];let rendered=null;renderEvaluationRuns=()=>{};renderEvaluationDetail=(run,events,rows,total)=>rendered={rows,total};
renderListPager('evaluationResults',{total:125,page:1,page_size:8});state.listPageSizes.evaluationResults=50;state.listPages.evaluationResults=3;
const urls=[];api=async url=>{urls.push(url);if(url.includes('/results?'))return {total:125,results:Array.from({length:25},(_,i)=>({case_id:'case-'+(100+i)}))};if(url.includes('/events'))return {events:[]};return {id:'run'};};
await refreshSelectedEvaluationRun();
check(urls.some(url=>url.endsWith('page=3&page_size=50')),'results selected size reaches API');
check(rendered.rows.length===25&&state.listMeta.evaluationResults.total_pages===3,'results page count');
state.selectedEvaluationRunId='new-run';await refreshSelectedEvaluationRun();
check(state.listPages.evaluationResults===1&&listPageSize('evaluationResults')===50,'new run resets page with size retained');
''')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = PlatformStore(self.root/'platform.db', recover_jobs=False)
        self.a = self.store.create_project('A', '项目 A')['id']
        self.b = self.store.create_project('B', '项目 B')['id']

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, kind):
        if kind == 'reports':
            seed=self.store.create_report_job('run',1,{'_project_id':self.a})
            table='report_jobs'
        elif kind == 'stress':
            seed=self.store.create_stress_job({'_project_id':self.a,'host':'example.test'})
            table='stress_jobs'
        else:
            seed=self.store.create_interface_scenario_run(self.a,{'name':'合成场景','steps':[]})
            table='interface_scenario_runs'
        with self.store._connection() as connection:
            sample=dict(connection.execute(f'SELECT * FROM {table} WHERE id=?',(seed['id'],)).fetchone())
            connection.execute(f'DELETE FROM {table} WHERE id=?',(seed['id'],))
            for i in range(625):
                row=dict(sample,id=f'{kind}-{i:04d}',created_at=100 if i<25 else 200,status='succeeded')
                project=self.a if i<25 else self.b
                if kind=='scenarios': row['project_id']=project
                else: row['options_json']=json.dumps({'_project_id':project})
                if kind=='reports': row['idempotency_key']=row['id']
                cols=list(row)
                connection.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",tuple(row.values()))

    def test_all_report_histories_filter_before_limit_and_use_stable_pages(self):
        for kind in ('reports','stress','scenarios'):
            with self.subTest(kind=kind):
                self.seed(kind)
                first=self.store.page_project_history(kind,self.a,page=1,page_size=10)
                second=self.store.page_project_history(kind,self.a,page=2,page_size=10)
                last=self.store.page_project_history(kind,self.a,page=999,page_size=10)
                self.assertEqual(first['total'],25)
                self.assertEqual(first['succeeded'],25)
                self.assertEqual([x['id'] for x in second['items']],[f'{kind}-{i:04d}' for i in range(14,4,-1)])
                self.assertFalse({x['id'] for x in first['items']}&{x['id'] for x in second['items']})
                self.assertEqual((last['page'],last['total_pages'],len(last['items'])),(3,3,5))
                empty=self.store.page_project_history(kind,'missing',page=9)
                self.assertEqual((empty['page'],empty['total'],empty['items']),(1,0,[]))
                if kind=='scenarios':
                    self.assertNotIn('result',first['items'][0])
                    self.assertNotIn('scenario_snapshot',first['items'][0])

    def test_report_api_keeps_legacy_data_in_initial_admin_project(self):
        self.seed('reports')
        self.store.create_report_job('legacy',1,{})
        router,*_=create_platform_api(TaskStore(self.root/'tasks.db'),platform_store=self.store,
            artifact_storage=create_artifact_storage(self.root,{'backend':'local','root':'artifacts'}))
        endpoint=next(r.endpoint for r in router.routes if r.path=='/api/reports' and 'GET' in r.methods)
        def request(project,admin):
            return SimpleNamespace(state=SimpleNamespace(identity={'user':{'is_superuser':admin},'current_project':{'id':project},'legacy_project_id':self.a}))
        admin=asyncio.run(endpoint(request(self.a,True),limit=100,page=1,page_size=10))
        member=asyncio.run(endpoint(request(self.a,False),limit=100,page=1,page_size=10))
        other=asyncio.run(endpoint(request(self.b,True),limit=100,page=1,page_size=10))
        self.assertEqual((admin['total'],member['total'],other['total']),(26,25,600))
        self.assertEqual(len(admin['reports']),10)
        self.assertNotIn('items',admin)

    def test_javascript_pagers_handle_filter_empty_shrink_and_stale_requests(self):
        node=shutil.which('node')
        if not node: self.skipTest('Node.js is required for JavaScript behavior checks')
        source=Path(__file__).resolve().parents[1]/'src/auto_test/static/app.js'
        script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function fakeNode(){const children=new Map();return {innerHTML:'',dataset:{},setAttribute(){},open:false,value:'',querySelector(key){if(!children.has(key))children.set(key,fakeNode());return children.get(key);}};}
const document={addEventListener(){},querySelector(selector){if(!nodes.has(selector))nodes.set(selector,fakeNode());return nodes.get(selector);}};
const context=vm.createContext({document,console,setTimeout,clearTimeout,URLSearchParams});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext(`(async()=>{
 const check=(ok,message)=>{if(!ok)throw Error(message);};
 const rows=Array.from({length:23},(_,i)=>i);
 listPage('models',rows,3,'a');state.listPages.models=3;
 check(listPage('models',rows,3,'a').items[0]===6,'second page preserves state');
 check(listPage('models',rows,3,'b').items[0]===0,'filter resets page');
 state.listPages.models=99;check(listPage('models',[1,2],3).page===1,'shrink clamps');
 check(listPage('models',[],3).items.length===0,'empty page');
 check(state.listMeta.models.total_pages===1,'empty has one page');
 const pending=[];api=()=>new Promise(resolve=>pending.push(resolve));state.projectId='a';
 const applied=[];const first=loadHistoryPage('reportJobs','/api/reports','reports',data=>applied.push(data.marker));
 state.listPages.reportJobs=2;const second=loadHistoryPage('reportJobs','/api/reports','reports',data=>applied.push(data.marker));
 pending[1]({page:2,total:30,page_size:10,marker:'new'});await second;
 pending[0]({page:1,total:30,page_size:10,marker:'old'});await first;
 check(applied.join(',')==='new','stale page response ignored');
 const third=loadHistoryPage('reportJobs','/api/reports','reports',data=>applied.push('wrong-project'));
 state.projectId='b';pending[2]({page:1,total:1});await third;
 check(applied.join(',')==='new','old project response ignored');
})()`,context).then(()=>console.log('PAGINATION_BEHAVIOR_OK')).catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run([node,'-e',script,str(source)],text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('PAGINATION_BEHAVIOR_OK',result.stdout)

    def test_suite_drawer_discards_stale_pages_and_escapes_case_content(self):
        node = shutil.which('node')
        if not node: self.skipTest('Node.js is required')
        source = Path(__file__).resolve().parents[1]/'src/auto_test/static/app.js'
        script = r'''
const fs=require('node:fs'),vm=require('node:vm');
const nodes=new Map();
function fakeNode(){const children=new Map();return {innerHTML:'',dataset:{},setAttribute(){},open:false,value:'',querySelector(key){if(!children.has(key))children.set(key,fakeNode());return children.get(key);}};}
const document={addEventListener(){},querySelector(selector){if(!nodes.has(selector))nodes.set(selector,fakeNode());return nodes.get(selector);}};
const context=vm.createContext({document,console,setTimeout,clearTimeout,URLSearchParams});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext(`(async()=>{
 const check=(ok,message)=>{if(!ok)throw Error(message);};
 const pending=[];api=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));state.projectId='a';
 evaluationSuiteDetail={id:'suite',latest_version:{id:'version'}};
 const response=(name,page)=>({version:{version:1},total:11,page,page_size:5,total_pages:3,cases:[{payload:{name,prompt:'<img src=x onerror=alert(1)>',expected:0}}]});
 const first=loadEvaluationSuiteCases();state.listPages.evaluationSuiteCases=2;const second=loadEvaluationSuiteCases();
 pending[1].resolve(response('new-page',2));await second;
 pending[0].resolve(response('stale-page',1));await first;
 check($('#evaluationSuiteCases').innerHTML.includes('new-page'),'new page retained');
 check(!$('#evaluationSuiteCases').innerHTML.includes('stale-page'),'stale page ignored');
 check(!$('#evaluationSuiteCases').innerHTML.includes('<img'),'HTML payload escaped');
 check($('#evaluationSuiteCases').innerHTML.includes('&lt;img'),'payload shown literally');
 check($('#evaluationSuiteCases').innerHTML.includes('<pre>0</pre>'),'zero expectation preserved');
 const third=loadEvaluationSuiteCases();state.projectId='b';pending[2].resolve(response('wrong-project',1));await third;
 check(!$('#evaluationSuiteCases').innerHTML.includes('wrong-project'),'project change ignored');
 const fourth=loadEvaluationSuiteCases();closeEvaluationSuiteDrawer();pending[3].resolve(response('after-close',1));await fourth;
 check(!$('#evaluationSuiteCases').innerHTML.includes('after-close'),'closed request ignored');
 evaluationSuiteDetail={id:'suite',latest_version:{id:'version'}};
 const failed=loadEvaluationSuiteCases();pending[4].reject(Error('<failed>'));await failed;
 check($('#evaluationSuiteCases').innerHTML.includes('data-evaluation-suite-retry'),'visible retry');
 check($('#evaluationSuiteCases').innerHTML.includes('&lt;failed&gt;'),'error escaped');
 const retried=loadEvaluationSuiteCases();pending[5].resolve(response('retry-success',1));await retried;
 check($('#evaluationSuiteCases').innerHTML.includes('retry-success'),'retry succeeds');
})()`,context).then(()=>console.log('SUITE_DRAWER_OK')).catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([node, '-e', script, str(source)], text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('SUITE_DRAWER_OK', result.stdout)
