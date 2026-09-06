# 在 AutoDL 部署 Huihui-Qwen3.8-27B(无审查) 的教训总结

> 目标：把 **huihui-ai/Huihui-Qwen3.8-27B-abliterated（无审查）+ GGUF Q4_K + mmproj 视觉** 部署到 AutoDL，让 ANR 的 **识图 + tag 提取** 都由这一个模型完成（无审查、保留思考、流式）。
>
> 结论：**西部区一台 RTX 4090 48G + llama.cpp CLI（`llama-server --mmproj`）+ GGUF Q4_K 成功**；中途踩了 GPU 架构、Xet 下载、GitHub 网络、llama-cpp-python 无视觉等一堆坑。

---

## 一、最核心的坑（按踩坑顺序）

### 1. GPU 架构决定一切（vGPU / 3090 / 4090 天差地别）
| 卡 | 结果 | 原因 |
|---|---|---|
| **vGPU-32GB（虚拟卡）** | `llama.cpp` 输出**乱码**（`#4;#9` / `ugeuge`） | 虚拟化的 DeltaNet/Gated DeltaNet CUDA 路径有 bug；且 32G 装不下 FP8 29G |
| **RTX 3090 48G（安培 8.6）** | FP8 **装不下**（29G+Kv 溢出 32G）、`--no-mmap` 也崩；4-bit `Bus error` | 安培**无原生 FP8 tensor core**（FP8 需 Ada/Hopper），FP8 只能软件模拟 |
| **RTX 4090 48G（Ada 8.9）** | **成功**（Q4_K + mmproj，GPU 15.5G，识图+提取正常） | Ada 有原生 FP8；物理卡 DeltaNet CUDA 正常 |

> 结论：**Qwen3.8(混合架构)** 在 **物理 ADA/Hopper + ≥40G 显存** 上最稳；**虚拟卡一律别碰**（乱码），**24G/32G 装不下 FP8 29G**。

### 2. 量化：FP8 29G 在 32G 显存是硬伤
- **FP8 权重 ≈ 29G**（27B × 1字节）本身太接近 32G，加 KV/workspace 必然 OOM（sglang `mamba_cache_size=-5`、vllm `Engine OOM`、transformers `dtype` 都卡）。
- **4-bit（GGUF Q4_K ≈ 16G）** 在 ≥40G 显存宽裕，质量对"提取/识图"足够。
- **官方 FP8 权重**：`FineGrainedFP8Config` 钉死，**无法用 bitsandbytes 降到 4-bit**；想 4-bit 用 **GGUF 量化**（huihui abliterated-GGUF 仓库有 `Q4_K.gguf`，非 UD 版）。

### 3. 识图（多模态）必须用 llama.cpp CLI，不能用 llama-cpp-python
- **`llama-cpp-python` 最新版就是 0.3.35，且 `Llama.__init__` 没有 `mmproj/vision` 参数** → 只能文本，**不能识图**。
- **识图要走 `llama.cpp CLI（llama-server --mmproj）`**（原生支持 mmproj 视觉 + Qwen3.8-VL）。
- `--mmproj` 后端（多模态）要求 content 是**数组**（`[{type:text}]`），纯文本也一样——ANR/客户端 JSON 用 `content: "str"` 会 500。

### 4. GitHub 在 AutoDL 西部区全都不通（编不了 llama.cpp）
- `git clone github.com / gitclone / ghproxy` 在 **westb/westd/weste 全部超时**。
- **解法**：**本机（能上 GitHub，用代理 7897）clone 源码** → `tar` → **ssh/sftp 传到云端** → 云端编（Linux + CUDA + `-DGGML_MTMD=ON`）。llama.cpp 源码约 209M / tar 72M。
- 编译：`cmake -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=<86|89> -DGGML_MTMD=ON` + `cmake --build -j16`（约 30min）。

### 5. hf-mirror 下载：Xet 401 & 限速
- **hf-mirror 的 `resolve` 会 302 重定向到 `cas-bridge.xethub.hf.co`（Xet 存储）**——huggingface_hub/curl 走 Xet 常报 **401**，或限速到几百 KB/s。
- **用 aria2c 多线程 `-x16 -s16 -k2M -m 0 -o <file>`**（跟随签名 URL）能下；**GGUF repo 快（325MB/s）**，safetensors repo 慢（5MB/s）。
- **分片名**：官方 FP8/GGUF 用 **`layers-N.safetensors`**（逐层，不是 `model-N`）——用 `curl api | grep '\.safetensors'` 抓**全部**，别只抓 `model-*`。
- **别下出 `.1` 变体**（aria2c 重试会生成 `layers-0.1.safetensors` 等重复层，占 20G）——按 index 只留必要的，删除 `*.1.safetensors`。

### 6. 保留思考 & 不乱码的关键参数
- **物理卡 + 最新 llama.cpp** 后，`llama-server --mmproj -m Q4_K.gguf -ngl 99 -fa on -c N --jinja` → **不乱码 + 保留思考**（`--jinja` 走 GGUF 内嵌 qwen3.8 模板，思考由模型自然输出）。
- **llama-cpp-python server**：`--chat_format qwen`（不是 `llama3`）+ `--n_batch`（不是 `--batch_size`）+ `--n_gpu_layers 99 --flash_attn on`。
- 参考：llama.cpp issue/discussion **#27164**——RTX 3090 物理卡 + 更新 llama.cpp + FA + libggml-cuda.so 一起更新后，Qwen3.8 就正常；**旧版 CUDA 对 DeltaNet 层有 bug**。

### 7. AutoDL 公网映射（ANR 接入）
- 实例内监听 **6006 / 6008**，系统映射到公网（注意**主机名不同**，极易搞混）：
  - `6006` → `https://u37677-1yy1-664a775e.weste.seetacloud.com:8443`（**一个 u**）
  - `6008` → `https://uu37677-1yy1-664a775e.weste.seetacloud.com:8443`（**两个 u**）
- ANR 侧请求（httpx）要 **`verify=False`**（自签 https）。

### 8. 存数据盘/共享盘
- 模型/编译产物放 **`/autodl-fs/data`**（AutoDL 大容量数据盘，**跨实例共享**、换实例不丢）比 `/root/autodl-tmp`（50G 实例盘）稳。
- `/root/autodl-tmp` 只有 50G，**装不下 54G 的 BF16 safetensors**，别往那放。

---

## 二、最终可复现方案（本次成功配置）

### 云端（weste，RTX 4090 48G，compute 8.9）
1. 模型：`/autodl-fs/data/27b-gguf/Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf`（16G）+ `mmproj-model-bf16.gguf`（889M）。
2. llama.cpp：本机 clone + 传云端编译（`-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 -DGGML_MTMD=ON`）。
3. 启动**识图+提取合一**（6006）：
```bash
cd /root/llama.cpp_src/build/bin
LD_LIBRARY_PATH=$PWD:/usr/local/cuda/lib64:$LD_LIBRARY_PATH \
./llama-server -m /autodl-fs/data/27b-gguf/Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf \
  --mmproj /autodl-fs/data/27b-gguf/mmproj-model-bf16.gguf \
  --host 0.0.0.0 --port 6006 -ngl 99 -fa on -c 16384 --jinja
```
4. 公网入口：`https://u37677-1yy1-664a775e.weste.seetacloud.com:8443`（6006）。

### ANR（`server/routes/tools.py` 的 `qwen_chat`）
- **有图**（识图）/ **无图**（提取）都指向上面公网 URL；
- content 用**数组**（多模态后端）；`httpx ... verify=False`；流式 SSE 转发。

---

## 三、一句话教训
> **Qwen3.8(混合 DeltaNet) 想跑通：选物理 Ada/Hopper ≥40G 显存 + 最新 llama.cpp CLI(--mmproj) + GGUF 4-bit；避开 vGPU(乱码) 与 24/32G(装不下 FP8)；GitHub 不通就本机 clone 传云端编；hf-mirror 走 Xet 用 aria2c 硬啃 + 只抓 `*.safetensors`。**

（本次提交：ANR `e8dc317` 等——合一识图/提取到云端 Qwen3.8。）
