from fastapi import HTTPException, Request, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from auto_test.monitoring.performance_comparison import PerformanceComparisons


class ComparisonInput(BaseModel):
    job_ids: list[str] = Field(min_length=2, max_length=6)
    allow_mismatch: bool = False


def register_performance_api(router, store, artifacts, visible):
    service = PerformanceComparisons(store, artifacts)
    def project(request):
        return request.state.identity['current_project']['id']
    def job(request, identifier):
        item = store.get_stress_job(identifier)
        if not item or not visible(item, request):
            raise HTTPException(404, '任务不存在')
        if item['status'] not in {'succeeded', 'failed', 'interrupted', 'stopped'}:
            raise HTTPException(409, '任务尚未结束')
        return item

    @router.get('/stress-comparisons')
    def workspace(request: Request):
        return {'comparisons': service.list(project(request))}

    @router.post('/stress-comparisons', status_code=201)
    def create(payload: ComparisonInput, request: Request):
        if len(set(payload.job_ids)) != len(payload.job_ids):
            raise HTTPException(400, '不能重复选择同一个任务')
        jobs = [job(request, identifier) for identifier in payload.job_ids]
        try:
            return service.save(project(request), request.state.identity['user']['id'], jobs, payload.allow_mismatch)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/stress-comparisons/{identifier}')
    def detail(identifier: str, request: Request):
        item = service.get(project(request), identifier)
        if not item:
            raise HTTPException(404, '对比报告不存在')
        return {'id': identifier, 'result': item['result']}

    @router.get('/stress-comparisons/{identifier}/download/{format_name}')
    def download(identifier: str, format_name: str, request: Request):
        if format_name not in {'docx', 'pdf', 'json'}:
            raise HTTPException(400, '格式无效')
        item = service.get(project(request), identifier)
        if not item:
            raise HTTPException(404, '对比报告不存在')
        try:
            root = artifacts.resolve(item['artifact_ref'])
            filename = 'comparison.json' if format_name == 'json' else 'report.' + format_name
            path = artifacts.resolve_file(artifacts.reference(root / filename), container=item['artifact_ref'])
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, '报告产物不可用') from exc
        return FileResponse(path, filename='server-comparison.' + format_name, headers={'Cache-Control': 'no-store'})

    @router.get('/stress-trends')
    def trend(request: Request, job_id: str = Query(min_length=1, max_length=64)):
        selected = job(request, job_id)
        # Use the repository's project predicate, then require the exact target.
        context = request.state.identity
        legacy = bool(context['user'].get('is_superuser')) and (not context.get('legacy_project_id') or context.get('legacy_project_id') == project(request))
        candidates = store.page_project_history('stress', project(request), include_legacy=legacy, page=1, page_size=100)
        items = candidates.get('items') or candidates.get('jobs') or []
        entries = [service.entry(item) for item in items if item.get('target_host') == selected.get('target_host') and item['status'] in {'succeeded', 'failed', 'interrupted', 'stopped'}][:30]
        return {'host': selected.get('target_host'), 'points': [{'id': item['id'], 'created_at': item['created_at'], 'status': item['status'], 'cpu_avg_pct': (item['metrics'].get('cpu_pct') or {}).get('avg'), 'cpu_peak_c': (item['metrics'].get('cpu_temp_c') or {}).get('max'), 'parameters': {key: item['options'].get(key) for key in ('duration', 'workers', 'cpu_load')}, 'benchmark_mib_s': item['benchmark'].get('mib_per_second') if item['status'] == 'succeeded' else None} for item in reversed(entries)], 'note': '从项目最近 100 条任务中取同目标最多 30 条已结束记录；不同参数仅显示趋势，不进行排名。'}
