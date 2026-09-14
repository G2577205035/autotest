"""Evidence-based comparisons; utilization never becomes a benchmark score."""

import json
import math
import time
import uuid

from auto_test.reporting.server_performance import summarize_samples
from auto_test.reporting.tabular import build_tabular_report


def initialize_comparisons(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS stress_comparisons (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        created_by VARCHAR(64) NOT NULL, created_at DOUBLE NOT NULL,
        result_json LONGTEXT NOT NULL, artifact_ref LONGTEXT NOT NULL)''')


def finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def comparison_result(entries, allow_mismatch=False):
    if not 2 <= len(entries) <= 6:
        raise ValueError('选择 2～6 个已结束任务进行比较')
    def signature(entry):
        options = entry.get('options') or {}
        return {key: options.get(key) for key in ('duration', 'workers', 'cpu_load', 'gpu_memory_percent', 'cpu_benchmark')} | {'modes': sorted(entry.get('modes') or [])}
    same_plan = all(signature(entry) == signature(entries[0]) for entry in entries)
    if not same_plan and not allow_mismatch:
        raise ValueError('任务参数不同；如需并列查看，请明确允许不同口径，不作排名')
    benchmarks = [entry.get('benchmark') or {} for entry in entries]
    fields = ('benchmark_version', 'block_bytes', 'duration_s', 'workers', 'warmup_s', 'python_version', 'openssl_version')
    rankable = same_plan and all(entry['status'] == 'succeeded' for entry in entries) and all(
        benchmark.get('status') == 'succeeded' and benchmark.get('benchmark_version') == 'liema-sha256-v1' and
        finite(benchmark.get('mib_per_second')) is not None and benchmark['mib_per_second'] > 0 and
        all(field in benchmark and benchmark[field] == benchmarks[0].get(field) for field in fields)
        for benchmark in benchmarks
    )
    rows = []
    for entry, benchmark in zip(entries, benchmarks):
        metrics = entry.get('metrics') or {}
        throughput = finite(benchmark.get('mib_per_second')) if benchmark.get('status') == 'succeeded' and entry['status'] == 'succeeded' else None
        baseline = benchmarks[0].get('mib_per_second') if rankable else None
        rows.append({'id': entry['id'], 'name': entry['name'], 'host': entry.get('host', ''), 'status': entry['status'], 'created_at': entry.get('created_at'),
                     'sample_count': metrics.get('sample_count', 0),
                     'cpu_avg_pct': finite((metrics.get('cpu_pct') or {}).get('avg')),
                     'cpu_peak_c': finite((metrics.get('cpu_temp_c') or {}).get('max')),
                     'memory_peak_pct': finite((metrics.get('memory_pct') or {}).get('max')),
                     'gpu_avg_pct': finite((metrics.get('gpu_pct') or {}).get('avg')),
                     'throughput_mib_s': round(throughput, 2) if throughput is not None else None,
                     'relative_index': round(throughput / baseline * 100, 2) if baseline else None,
                     'benchmark': {key: finite(value) if isinstance(value, (int, float)) else str(value)[:1000] for key, value in benchmark.items() if key in {*fields, 'mib_per_second', 'score', 'status'}}, 'parameters': signature(entry)})
    return {'rows': rows, 'comparable': same_plan, 'rankable': bool(rankable), 'baseline_id': entries[0]['id'],
            'note': '首个任务作为 100 的相对基准；仅 SHA-256 吞吐可作同参数排名，分数不代表综合硬件性能。' if rankable else '仅并列展示实测值。参数、运行状态或基准版本不一致，以及缺少基准数据时，不计算排名；使用率高低不代表处理速度。'}


class PerformanceComparisons:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def entry(self, job):
        payload = {}
        reference = job.get('artifact_dir')
        if reference:
            try:
                container = self.artifacts.resolve(reference)
                path = self.artifacts.resolve_file(self.artifacts.reference(container / 'report' / 'server_stress_report.json'), container=reference)
                if path.stat().st_size <= 5_000_000:
                    payload = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError):
                pass
        metrics = payload.get('metric_summary') or summarize_samples(self.store.get_stress_samples(job['id'], 0, 20000))
        options = job.get('options') or {}
        return {'id': job['id'], 'name': options.get('server_name') or job.get('target_name') or '未命名服务器', 'host': job.get('target_host') or options.get('host', ''),
                'created_at': job['created_at'], 'status': job['status'], 'options': options, 'modes': job.get('modes') or [],
                'metrics': metrics, 'benchmark': (payload.get('cpu_summary') or {}).get('benchmark') or {}}

    def save(self, project, actor, jobs, allow_mismatch=False):
        result = comparison_result([self.entry(job) for job in jobs], allow_mismatch)
        identifier = uuid.uuid4().hex
        directory = self.artifacts.workspace('stress-comparisons', project, identifier)
        names = build_tabular_report(directory, '服务器性能对比报告', result['note'], [
            ('任务与比较条件', ['服务器', '状态', '时长 / 进程', '基准环境'], [[row['name'], row['status'], f"{row['parameters'].get('duration') or '—'} s / {row['parameters'].get('workers')}", row['benchmark'].get('benchmark_version') or '未测量基准'] for row in result['rows']], [3, 1, 2, 3]),
            ('实测指标', ['服务器', 'CPU 均值 %', 'CPU 峰温 °C', '内存峰值 %', 'SHA-256 MiB/s', '相对指数'], [[row['name'], row['cpu_avg_pct'], row['cpu_peak_c'], row['memory_peak_pct'], row['throughput_mib_s'], row['relative_index']] for row in result['rows']], [3, 1.3, 1.3, 1.3, 1.6, 1.3]),
        ])
        (directory / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        self.artifacts.publish_tree(directory)
        with self.store._connection() as connection:
            connection.execute('INSERT INTO stress_comparisons(id,project_id,created_by,created_at,result_json,artifact_ref) VALUES(?,?,?,?,?,?)', (identifier, project, actor, time.time(), json.dumps(result, ensure_ascii=False), self.artifacts.reference(directory)))
        return {'id': identifier, 'result': result, 'files': [*names, 'comparison.json']}

    def get(self, project, identifier):
        with self.store._connection() as connection:
            row = connection.execute('SELECT * FROM stress_comparisons WHERE project_id=? AND id=?', (project, identifier)).fetchone()
        if not row:
            return None
        return {**dict(row), 'result': json.loads(row['result_json'])}

    def list(self, project):
        with self.store._connection() as connection:
            return [dict(row) for row in connection.execute('SELECT id,created_at FROM stress_comparisons WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 50', (project,)).fetchall()]
