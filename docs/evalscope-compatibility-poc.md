# EvalScope 1.11.1 隔离兼容性 PoC

> 验证日期：2026-09-01  
> 结论：通过，可作为烈马模型评测第一版的锁定候选；生产镜像仍需在工作日 4 完成离线依赖清单、许可证和哈希收口。

## 1. 固定环境

- 主项目继续使用现有 Python 3.12 环境，不直接安装或导入 EvalScope。
- PoC 使用项目 `.qa/` 下独立的 Python 3.11.9 embeddable 环境。
- EvalScope 固定为 `evalscope[perf]==1.11.1`，不跟随 `main` 或浮动版本。
- 平台通过 `EvaluationBackend` 启动绝对子脚本路径；凭据仅通过 `LIEMA_EVAL_*` 子进程环境变量注入。
- 子进程默认设置 HuggingFace/Transformers 离线模式；未配置时缓存限制在每次运行的短临时路径，正式环境可显式挂载受控缓存，Windows 因而同时避开 `MAX_PATH`。

隔离环境的最小安装命令为：

```powershell
python -m pip install "evalscope[perf]==1.11.1"
```

## 2. 实际验收结果

使用 `scripts/model_evaluation_poc.py` 启动仅监听随机本机端口的 OpenAI 兼容假服务，并通过平台自己的隔离适配器执行：

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| EvalScope 导入与版本 | 通过 | Python 3.11.9 可导入 EvalScope 1.11.1 |
| 标准评测 | 通过 | `general_qa` 本地 JSONL 完成预测、评分、报告和性能采集 |
| WMT | 通过 | `wmt24pp` 本地 `en-zh_cn` 样本以 BLEU-only 离线运行，BLEU-1 为 100% |
| 性能压测 | 通过 | `perf` 的 `openqa` 本地数据执行 2 请求、并发 1，生成明细数据库、百分位和 HTML |
| Mock 与停止 | 通过 | 隔离 Mock 可运行，协作停止后子进程进入 `stopped` |
| 结果兼容 | 通过 | JSON/JSONL/CSV 未知字段保留，稳定映射为平台结果信封 |
| 凭据保护 | 通过 | 请求快照只含环境变量名；PoC 产物扫描未发现注入值 |
| 完全离线 | 通过 | 测试数据和模型服务均为本地，子进程强制 Hub/Transformers offline |

最终重复运行命令：

```powershell
..\.venv\Scripts\python.exe scripts\model_evaluation_poc.py
```

本次最终运行输出位于 `runtime/model-evaluation-poc/20260901-162222/`（运行目录不提交）。标准评测映射到 11 个文件，性能压测映射到 11 个文件，两个入口均返回 `completed`。

## 3. 已确认的兼容处理

1. Windows embeddable Python 忽略普通 `PYTHONPATH`，不能依赖 `python -m auto_test...`；适配器改为执行已校验的绝对子脚本路径。
2. EvalScope/HuggingFace 会把本地数据路径编码到缓存文件名；深目录在 Windows 会触发 `WinError 206`。适配器支持独立 `cache_root`，未配置时 Windows 使用短临时缓存。
3. EvalScope 1.11.1 的 `BenchmarkMeta` 会在字符串主指标转换时丢失维度，WMT 的四个 BLEU identity 因而无法选出唯一主指标。隔离入口接受结构化 `MetricSelector` JSON，并在子进程边界恢复类型。
4. `general_qa` 的 BLEU 还依赖 NLTK `punkt_tab`。PoC 在缺少该资源时确认 EvalScope 会记录指标错误并继续输出 Rouge；正式离线包必须预取该资源。
5. WMT 的 BERTScore/COMET 需要额外模型和 `unbabel-comet`。工作日 1 仅用 BLEU 验证 WMT 数据、调用、评分和报告链；完整翻译指标按工作日 3 的许可、哈希和离线模型流程接入。
6. EvalScope 冷启动明显重于现有 Worker（本机首次导入约一分钟），因此继续坚持隔离子进程；若正式并发或内存验收不满足，再无损切换为独立 Evaluation Worker 镜像。

## 4. 复验规则

以下变化必须重跑本 PoC：EvalScope/Python 版本升级、子进程参数映射变化、Windows/Linux 基础镜像变化、WMT/性能插件升级、缓存布局变化或凭据注入方式变化。正式模型与完整 COMET/BERTScore 不属于本地假服务结论，必须另行标记实机验收。
