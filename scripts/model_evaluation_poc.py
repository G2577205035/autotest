"""Run an offline EvalScope compatibility PoC against a local OpenAI mock service."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from auto_test.evaluation.backends.evalscope import (  # noqa: E402
    EvalScopeBackend,
    EvalScopeRuntimeConfig,
)
from auto_test.evaluation.contracts import EvaluationRequest  # noqa: E402


class _OpenAIHandler(BaseHTTPRequestHandler):
    server_version = "LiemaEvalScopePoC/1.0"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._write_json(
            200,
            {
                "object": "list",
                "data": [{"id": "liema-poc-model", "object": "model"}],
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(content_length) if content_length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            self._write_json(400, {"error": {"message": "invalid json"}})
            return
        prompt = json.dumps(request.get("messages") or request.get("prompt") or "", ensure_ascii=False)
        if "Translate the following" in prompt:
            answer = "你好"
        elif "1+1" in prompt or "一加一" in prompt:
            answer = "2"
        else:
            answer = "北京"
        self._write_json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.get("model") or "liema-poc-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
            },
        )


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _run_backend(
    backend: EvalScopeBackend,
    *,
    run_id: str,
    mode: str,
    task_config: dict,
    work_dir: Path,
) -> dict:
    events: list[dict] = []
    result = backend.run(
        EvaluationRequest(
            run_id=run_id,
            project_id="evalscope-poc",
            mode=mode,
            task_config=task_config,
            work_dir=work_dir,
            backend_version="1.11.1",
            secret_env={"LIEMA_EVAL_API_KEY": "EMPTY"},
        ),
        on_event=lambda event: events.append(event.to_dict()),
    )
    return {
        "status": result.status,
        "summary": result.summary,
        "artifacts": result.artifacts,
        "events": events,
        "raw": result.raw,
        "error_type": result.error_type,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        type=Path,
        default=PROJECT_ROOT / ".qa" / "evalscope-poc" / "python311" / "python.exe",
        help="安装了 evalscope[perf]==1.11.1 的隔离 Python 3.11 解释器",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "model-evaluation-poc",
    )
    args = parser.parse_args()
    python_executable = args.python.resolve()
    if not python_executable.is_file():
        parser.error(f"隔离解释器不存在：{python_executable}")

    run_root = args.output_dir.resolve() / time.strftime("%Y%m%d-%H%M%S")
    run_root.mkdir(parents=True, exist_ok=False)
    eval_dataset = run_root / "general_qa.jsonl"
    wmt_dataset_dir = run_root / "wmt24pp"
    wmt_dataset_dir.mkdir()
    wmt_dataset = wmt_dataset_dir / "test.jsonl"
    perf_dataset = run_root / "openqa.jsonl"
    _write_jsonl(eval_dataset, [{"question": "1+1 等于多少？", "answer": "2"}])
    _write_jsonl(
        wmt_dataset,
        [{"source": "Hello", "target": "你好", "language_pair": "en-zh_cn"}],
    )
    _write_jsonl(perf_dataset, [{"question": "一加一等于多少？"}, {"question": "中国首都是哪里？"}])

    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    server_thread = threading.Thread(target=server.serve_forever, name="evalscope-poc-api", daemon=True)
    server_thread.start()
    api_url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    backend = EvalScopeBackend(
        EvalScopeRuntimeConfig(
            python_executable=python_executable,
            version="1.11.1",
            source_root=SOURCE_ROOT,
            stop_grace_seconds=2,
        )
    )
    try:
        eval_result = _run_backend(
            backend,
            run_id="poc-eval",
            mode="eval",
            work_dir=run_root / "eval",
            task_config={
                "model": "liema-poc-model",
                "model_id": "liema-poc-model",
                "datasets": ["general_qa", "wmt24pp"],
                "dataset_args": {
                    "general_qa": {
                        "dataset_id": str(eval_dataset),
                        "subset_list": ["default"],
                        "default_subset": "default",
                        "eval_split": "test",
                    },
                    "wmt24pp": {
                        # EvalScope/HuggingFace encodes local paths into Windows cache names;
                        # keep this relative so the compatibility PoC also covers MAX_PATH.
                        "local_path": "../wmt24pp",
                        "subset_list": ["en-zh_cn"],
                        "default_subset": "default",
                        "eval_split": "test",
                        "metric_list": [{"bleu": {}}],
                        "primary_metric": {
                            "name": "bleu",
                            "dimensions": {"ngram": 1},
                        },
                    },
                },
                "eval_type": "openai_api",
                "api_url": api_url,
                "generation_config": {"max_tokens": 8, "stream": False},
                "eval_batch_size": 1,
                "limit": 1,
                "no_timestamp": True,
                "collect_perf": True,
            },
        )
        perf_result = _run_backend(
            backend,
            run_id="poc-perf",
            mode="perf",
            work_dir=run_root / "perf",
            task_config={
                "model": "liema-poc-model",
                "api": "openai",
                "url": api_url,
                "number": [2],
                "parallel": [1],
                "dataset": "openqa",
                "dataset_path": str(perf_dataset),
                "data_source": "local",
                "max_tokens": 8,
                "stream": False,
                "sleep_interval": 0,
                "num_workers": 1,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    summary = {
        "schema_version": "1.0",
        "python": str(python_executable),
        "evalscope_version": "1.11.1",
        "api_url": api_url,
        "eval": eval_result,
        "perf": perf_result,
    }
    summary_path = run_root / "poc_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(summary_path), "eval": eval_result["status"], "perf": perf_result["status"]}))
    return 0 if eval_result["status"] == perf_result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
