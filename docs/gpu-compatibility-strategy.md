# GPU 压测最高兼容性方案

## 设计原则

平台不再按显卡名称硬编码，也不假设目标机一定是 RTX 3090。预检通过 `nvidia-smi` 获取每张卡的 compute capability、驱动版本和实时负载，再为所选 GPU 自动选择执行镜像。平台不自动安装或升级驱动、CUDA 和容器运行时。

## 支持矩阵

| GPU 代表型号 | Compute Capability | 默认执行路径 |
| --- | ---: | --- |
| QUADRO RTX 6000 / T4 | 7.5 | CUDA 11.8 通用镜像，原生 `sm_75` |
| A100 / A30 | 8.0 | CUDA 11.8 通用镜像，原生 `sm_80` |
| RTX 3090 / RTX A6000 | 8.6 | CUDA 11.8 通用镜像，原生 `sm_86` |
| RTX 4090 / RTX 6000 Ada | 8.9 | CUDA 11.8 通用镜像，原生 `sm_89` |
| B200 / GB200 | 10.0 | 优先 CUDA 12.8 Blackwell 原生镜像 |
| RTX 5090 / RTX PRO 6000 Blackwell | 12.0 | 优先 CUDA 12.8 Blackwell 原生镜像 `sm_120` |

型号中的“6000”必须结合完整名称或 compute capability 判断：QUADRO RTX 6000、RTX A6000、RTX 6000 Ada、RTX PRO 6000 Blackwell 分属不同架构，不能共用一个硬编码架构值。

## 两级镜像策略

1. 基线通用镜像：CUDA 11.8，内含 `sm_75`、`sm_80`、`sm_86`、`sm_89` 原生 cubin，并保留 `compute_89` PTX。覆盖 Turing、Ampere、Ada；在 Blackwell 原生镜像缺失时可作为 PTX 前向兼容兜底，但平台标记为“降级执行”。
2. Blackwell 原生镜像：CUDA 12.8，至少包含 `sm_100`、`sm_120` 原生 cubin和 PTX；用于 B200、RTX 5090、RTX PRO 6000 Blackwell，避免首次 PTX JIT 和非原生路径的不确定性。

项目在 `deploy/docker/gpu-burn/` 内提供 `Dockerfile.universal` 和 `Dockerfile.blackwell` 两套可复现构建定义；Blackwell 定义固定使用官方 CUDA 12.8.2 UBI9 devel/runtime 基础镜像。`Dockerfile.runtime` 仅用于封装已经构建好的二进制产物。

## 自动选择规则

- compute capability 小于 10.0：选择 CUDA 11.8 通用镜像。
- compute capability 大于等于 10.0：Blackwell 原生镜像已安装时优先使用；否则通用镜像以 PTX 模式降级运行并给出部署建议。
- 混合 GPU 或指定单卡时，只针对所选卡计算兼容性和繁忙状态。
- 镜像存在但驱动、NVIDIA Container Toolkit、GPU 卡号或负载状态不满足要求时，任务保持阻断。
- 正式烧卡前再次检查显存和利用率；业务繁忙时不允许自动停止业务容器或强制运行。

## 验证要求

- 构建阶段用 `cuobjdump` 校验每个原生 cubin 和 PTX 是否进入 fatbin。
- 新型号首次接入先做镜像识别和单卡无压枚举，再做短时单卡压测，最后才允许多卡长时测试。
- Blackwell 以 CUDA 12.8 和对应驱动分支完成专项验收；PTX 兜底不等同于原生路径验收通过。

## 官方依据

- NVIDIA GPU Compute Capability：https://developer.nvidia.com/cuda/gpus
- NVIDIA Blackwell Compatibility Guide：https://docs.nvidia.com/cuda/archive/12.8.2/blackwell-compatibility-guide/index.html
- NVIDIA CUDA 12.8 Release Notes：https://docs.nvidia.com/cuda/archive/12.8.0/cuda-toolkit-release-notes/index.html
- NVIDIA CUDA Compatibility：https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html
