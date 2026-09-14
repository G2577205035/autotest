"""Versioned SHA-256 throughput benchmark, separate from load utilization."""

import base64
import json
import math
import shlex
import time
import uuid


BENCHMARK_SCRIPT = r'''
import argparse, hashlib, json, multiprocessing as mp, os, platform, ssl, time

def measure(duration, barrier, output):
    data = bytes(range(256)) * 4096
    warmup = time.perf_counter() + 1
    while time.perf_counter() < warmup:
        hashlib.sha256(data).digest()
    barrier.wait(timeout=30)
    started, count = time.perf_counter(), 0
    while time.perf_counter() - started < duration:
        hashlib.sha256(data).digest()
        count += 1
    output.put({'mib': count, 'elapsed_s': time.perf_counter() - started})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=int, default=60)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()
    if not 10 <= args.duration <= 300 or not 1 <= args.workers <= 64:
        raise ValueError('benchmark bounds')
    queue, barrier = mp.Queue(), mp.Barrier(args.workers + 1)
    processes = [mp.Process(target=measure, args=(args.duration, barrier, queue)) for _ in range(args.workers)]
    try:
        for process in processes: process.start()
        barrier.wait(timeout=30)
        rows = [queue.get(timeout=args.duration + 15) for _ in processes]
        for process in processes: process.join(5)
        if any(process.exitcode != 0 for process in processes): raise RuntimeError('worker failed')
        elapsed = max(row['elapsed_s'] for row in rows)
        rate = sum(row['mib'] for row in rows) / elapsed
        print(json.dumps({'benchmark_version': 'liema-sha256-v1', 'status': 'succeeded',
            'algorithm': 'SHA-256', 'block_bytes': 1048576, 'duration_s': args.duration,
            'workers': args.workers, 'warmup_s': 1, 'elapsed_s': elapsed,
            'mib_per_second': rate, 'score': rate, 'reference_mib_per_second': 100,
            'python_version': platform.python_version(), 'openssl_version': ssl.OPENSSL_VERSION,
            'cpu_count': os.cpu_count(), 'architecture': platform.machine()}), flush=True)
    finally:
        for process in processes:
            if process.is_alive(): process.terminate()
        for process in processes:
            if process.pid: process.join(3)

if __name__ == '__main__': main()
'''


def validate_benchmark(result, duration, workers):
    if result.get('benchmark_version') != 'liema-sha256-v1' or result.get('status') != 'succeeded':
        raise ValueError('基准未完成')
    if result.get('duration_s') != duration or result.get('workers') != workers or result.get('block_bytes') != 1048576:
        raise ValueError('基准参数不匹配')
    for field in ('mib_per_second', 'elapsed_s', 'score'):
        if type(result.get(field)) not in (int, float) or not math.isfinite(result[field]) or result[field] <= 0:
            raise ValueError('基准结果无效')
    if result['elapsed_s'] < duration or result['elapsed_s'] > duration + 30 or not math.isclose(result['score'], result['mib_per_second']):
        raise ValueError('基准时长或分数无效')
    return result


def run_cpu_benchmark(ssh, *, duration=60, workers=1, guard_callback):
    if not 10 <= duration <= 300 or not 1 <= workers <= 64:
        raise ValueError('CPU 基准要求 10～300 秒、1～64 个进程')
    directory = '/tmp/liema-bench-' + uuid.uuid4().hex
    encoded = base64.b64encode(BENCHMARK_SCRIPT.encode()).decode('ascii')
    pid = ''
    try:
        # setsid creates an owned process group so stop also reaches child workers.
        command = f"umask 077; if mkdir {directory} && printf %s {shlex.quote(encoded)} | base64 -d > {directory}/bench.py; then setsid python3 {directory}/bench.py --duration {duration} --workers {workers} > {directory}/result.json 2> {directory}/error.txt < /dev/null & echo $!; fi"
        output, _ = ssh.exec(command, timeout=15)
        pid = output.strip().splitlines()[-1] if output.strip() else ''
        if not pid.isdigit():
            raise RuntimeError('CPU 基准启动失败，需要 python3、setsid 和 base64')
        deadline = time.monotonic() + duration + 45
        while time.monotonic() < deadline:
            guard_callback()
            output, _ = ssh.exec(f"head -c 10000 {directory}/result.json", timeout=10)
            if output.strip():
                try:
                    result = json.loads(output)
                except json.JSONDecodeError:
                    pass  # A concurrent write may not yet have completed.
                else:
                    return validate_benchmark(result, duration, workers)
            alive, _ = ssh.exec(f"kill -0 {pid} 2>/dev/null && echo running", timeout=10)
            if 'running' not in alive:
                raise RuntimeError('CPU 基准提前结束，未得到有效结果')
            time.sleep(1)
        raise RuntimeError('CPU 基准超过运行时限')
    finally:
        if pid.isdigit():
            # Avoid signaling a PID that has already been recycled for another task.
            ssh.exec(f"if tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null | grep -Fq {shlex.quote(directory + '/bench.py')}; then kill -TERM -- -{pid} 2>/dev/null; fi", timeout=10)
        ssh.exec(f"rm -f {directory}/bench.py {directory}/result.json {directory}/error.txt; rmdir {directory} 2>/dev/null", timeout=10)
