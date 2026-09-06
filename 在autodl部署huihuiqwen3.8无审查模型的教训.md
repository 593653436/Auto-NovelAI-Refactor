# 在 AutoDL 部署 Huihui-Qwen3.8-27B(无审查) 的教训总结

> 目标：把 **huihui-ai/Huihui-Qwen3.8-27B-abliterated（无审查）+ GGUF Q4_K + mmproj 视觉** 部署到 AutoDL，让 ANR 的 **识图 + tag 提取** 都由这一个模型完成（无审查、流式、可保留思考）。
>
> 结论：**一台物理 RTX 4090 48G + llama.cpp CLI（`llama-server --mmproj`）+ GGUF Q4_K 成功**；中途踩了 GPU 架构、Xet 下载、GitHub 网络、llama-cpp-python 无视觉、reasoning 档位、ffmpeg、AVIF、识图流式、并发、自启动等一堆坑。

---

## 一、最核心的坑（按踩坑顺序）

### 1. GPU 架构决定一切（vGPU / 3090 / 4090 天差地别）
| 卡 | 结果 | 原因 |
|---|---|---|
| **vGPU-32GB（虚拟卡）** | `llama.cpp` 输出**乱码**（`#4;#9` / `ugeuge`） | 虚拟化的 DeltaNet/Gated DeltaNet CUDA 路径有 bug；且 32G 装不下 FP8 29G |
| **RTX 3090 48G（安培 8.6）** | FP8 **装不下**（29G+Kv 溢出 32G）、`--no-mmap` 也崩；4-bit `Bus error` | 安培**无原生 FP8 tensor core**（FP8 需 Ada/Hopper），FP8 只能软件模拟；vLLM 也报 `MarlinFP8ScaledMMLinearKernel` 崩 |
| **RTX 4090 48G（Ada 8.9）** | **成功**（Q4_K + mmproj，GPU 15.5G，识图+提取正常） | Ada 有原生 FP8；物理卡 DeltaNet CUDA 正常 |

> 结论：**Qwen3.8(混合架构)** 在 **物理 ADA/Hopper + ≥40G 显存** 上最稳；**虚拟卡一律别碰**（乱码），**24G/32G 装不下 FP8 29G**。

### 2. 量化：FP8 29G 在 32G 显存是硬伤；无审查版没有 FP8/AWQ/GPTQ
- **FP8 权重 ≈ 29G**（27B × 1字节）本身太接近 32G，加 KV/workspace 必然 OOM（sglang `mamba_cache_size=-5`、vllm `Engine OOM`、transformers `dtype` 都卡）。
- **无审查版（huihui abliterated）只有 BF16 + GGUF**，**没有官方 FP8 / AWQ / GPTQ**（这些只存在于官方 `Qwen/Qwen3.8-27B-FP8` 等仓库）→ **想无审查只能走 GGUF 量化**（`Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf`，16G）。
- **4-bit（GGUF Q4_K ≈ 16G）** 在 ≥40G 显存宽裕，质量对"提取/识图"足够。
- **官方 FP8 权重**：`FineGrainedFP8Config` 钉死，**无法用 bitsandbytes 降到 4-bit**；想 4-bit 用 **GGUF 量化**。
- ⚠️ **第三方 AWQ（如 cyankiwi/Qwen3.8-27B-AWQ-INT4）输出乱码**（量化质量差 / compressed-tensors bug），别用。

### 3. 识图（多模态）必须用 llama.cpp CLI，且依赖 ffmpeg / 只认部分图像格式
- **`llama-cpp-python` 最新版就是 0.3.35，且 `Llama.__init__` 没有 `mmproj/vision` 参数** → 只能文本，**不能识图**。
- **识图要走 `llama.cpp CLI（llama-server --mmproj）`**（原生支持 mmproj 视觉 + Qwen3.8-VL）。
- **`--mmproj` 后端（多模态）要 ffmpeg/ffprobe 解码图片**：不装 ffmpeg 会报 `mtmd_helper_bitmap_init_from_buf: failed to decode ... ffprobe failed (is ffprobe in PATH?)` → **识图失败输出空**。`apt install ffmpeg` 解决。
- **图像格式：llama.cpp 的 ffprobe 只稳解 png/jpg/webp**；**`.avif` 不认**（会识别失败空）→ **在客户端（ANR 后端）用 Pillow 统一转 PNG 再 base64 发送**（`Image.open(x).convert("RGB").save(buf,"PNG")`；Pillow ≥11 支持 avif 解码）。
- `--mmproj` 后端要求 content 是**数组**（`[{type:text}]`），纯文本也一样——客户端 JSON 用 `content: "str"` 会 500。
- **识图（有图）响应是 SSE 流**（`data: {json}`），**前端必须按流式解析**（`choices[0].delta.content` + `delta.reasoning_content`）；若前端用 `post()+r.reply` 期望 JSON reply，会把 SSE 当 JSON parse → `Unexpected token 'd', "data: {..."`。

### 4. GitHub 在 AutoDL 这几个区全都不通（编不了 llama.cpp）
- `git clone github.com / gitclone / ghproxy` 在 **west 各区分区全部超时**。
- **解法**：**本机（能上 GitHub，用代理 7897）clone 源码** → `tar` → **ssh/sftp 传到云端** → 云端编（Linux + CUDA + `-DGGML_MTMD=ON`）。llama.cpp 源码约 209M / tar 72M。
- 编译：`cmake -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=<86|89> -DGGML_MTMD=ON` + `cmake --build -j16`（约 30min）。
- ⚠️ **必须 `-DGGML_MTMD=ON`** 才带视觉/mmproj（否则只有文本）。

### 5. hf-mirror 下载：Xet 401 & 限速
- **hf-mirror 的 `resolve` 会 302 重定向到 `cas-bridge.xethub.hf.co`（Xet 存储）**——huggingface_hub/curl 走 Xet 常报 **401**，或限速到几百 KB/s。
- **用 aria2c 多线程 `-x16 -s16 -k2M -m 0 -o <file>`**（跟随签名 URL）能下；**GGUF repo 快（325MB/s）**，safetensors repo 慢（5MB/s）。`-s32` 会报错（上限 16），用 `-x16 -s16`。
- **分片名**：官方 FP8/GGUF 用 **`layers-N.safetensors`**（逐层，不是 `model-N`）——用 `curl api | grep '\.safetensors'` 抓**全部**，别只抓 `model-*`。
- **别下出 `.1` 变体**（aria2c 重试会生成 `layers-0.1.safetensors` 等重复层，占 20G）——按 index 只留必要的，删除 `*.1.safetensors`。

### 6. 保留思考 & 不乱码的关键参数（含 reasoning 档位坑）
- **物理卡 + 最新 llama.cpp** 后，`llama-server --mmproj -m Q4_K.gguf -ngl 99 -fa on -c N --jinja` → **不乱码 + 可保留思考**（`--jinja` 走 GGUF 内嵌 qwen3.8 模板）。
- ⚠️ **`--reasoning-effort` 只支持 `xhigh`（默认）/ `medium` / `low`**。设成 `high`/其它会抛 Jinja `Unexpected reasoning effort high` → **500 → 响应空**（看起来"没出字"）。**max档=xhigh**。
- **思考会吃掉大量 token**：`max_tokens` 要给够（如 16000），且 **`-c`（context）要 > max_tokens**（初版 `-c 16384` + max 16000 很紧，建议 **`-c 32768`**）。`max_tokens` 太小（如几百）会被思考吃满 → `finish_reason=length` → **正文(content)为空**。
- **前端必须同时读 `reasoning_content`（思考）+ `content`（正文）**；只读 `content` 时思考不显示、正文空时就显示"空"。
- **llama-cpp-python server**：`--chat_format qwen`（不是 `llama3`）+ `--n_batch`（不是 `--batch_size`）+ `--n_gpu_layers 99 --flash_attn on`。
- 参考：llama.cpp issue/discussion **#27164**——RTX 3090 物理卡 + 更新 llama.cpp + FA + libggml-cuda.so 一起更新后，Qwen3.8 就正常；**旧版 CUDA 对 DeltaNet 层有 bug**。

### 7. AutoDL 公网映射（ANR 接入）
- 实例内端口映射到公网（注意**不同端口的公网域名不同，极易搞混**；此处用占位符）：
  - 端口 `6006` → 公网 `https://<AUTODL端口6006公网域名>:8443`
  - 端口 `6008` → 公网 `https://<AUTODL端口6008公网域名>:8443`
- ANR 侧请求（httpx）要 **`verify=False`**（自签 https）。
- ⚠️ **公网入口/实例域名属敏感信息，别写进仓库文档/聊天记录**。

### 8. 存数据盘/共享盘
- 模型/编译产物放 **`/autodl-fs/data`**（AutoDL 大容量数据盘，**跨实例共享**、换实例不丢）比 `/root/autodl-tmp`（50G 实例盘）稳。
- `/root/autodl-tmp` 只有 50G，**装不下 54G 的 BF16 safetensors**，别往那放。

### 9. 多会话并发
- llama-server 默认 **`--parallel 1`（单槽）** → 多会话**排队（串行）**，一个做完下一个才动；xhigh 思考很长（每会话想 1000+ token），多会话排起来**等待很明显**。
- context（`-c`）和 KV 缓存是**共享池**：多会话分同一份 context（如 32768），会话越多每个分到越少。
- **要真并行**：`--parallel 2~4`（开多槽）——但 **KV 显存 = 模型 + context×槽数**（48G 大概只容 2 槽且 context 变小），且建议**思考降 medium/low**（并行才不崩/不挤）。
- 结论：**偶尔 2-3 会话**保持 `--parallel 1` 排队即可；**要并行**就 `--parallel 2 -c 16384` + medium。

### 10. 服务自启动（AutoDL 无 systemd / crontab）
- AutoDL 容器**没有 systemd**（PID1 非）、也**没有 crontab**（常见自启动通道都不可用）。
- 可靠做法：写 `/etc/rc.local`（`chmod +x`，内部 `nohup bash /root/start_qwen38.sh` 并把日志重定向）；或 **AutoDL 控制台的"开机自启动"脚本**（**若控制台有该选项**，填 `/root/start_qwen38.sh`）。
- 启动脚本建议：`nohup bash /root/start_qwen38.sh > /root/qwen38.log 2>&1 &`，日志可 `tail -f`。**注意早启动可能撞 GPU 未就绪**（可加 sleep 或健康检查重试）。

---

## 二、最终可复现方案（本次成功配置）

### 云端（物理 RTX 4090 48G，compute 8.9）
1. 模型：`/autodl-fs/data/27b-gguf/Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf`（16G）+ `mmproj-model-bf16.gguf`（889M）。
2. llama.cpp：本机 clone + 传云端编译（`-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 -DGGML_MTMD=ON`）。
3. **装 ffmpeg**（识图解码必需）：`apt install -y ffmpeg`。
4. 启动**识图+提取合一**（端口 6006）：
```bash
cd /root/llama.cpp_src/build/bin
LD_LIBRARY_PATH=$PWD:/usr/local/cuda/lib64:$LD_LIBRARY_PATH \
./llama-server -m /autodl-fs/data/27b-gguf/Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf \
  --mmproj /autodl-fs/data/27b-gguf/mmproj-model-bf16.gguf \
  --host 0.0.0.0 --port 6006 -ngl 99 -fa on -c 32768 --jinja \
  --reasoning-effort xhigh   # xhigh(max)/medium/low; 用 low 最快出正文
```
5. 公网入口：AutoDL 控制台为端口 6006 生成的公网地址（**不在此写实值**）。
6. 自启动：`/etc/rc.local`（或 AutoDL 开机脚本）→ `nohup bash /root/start_qwen38.sh`。

### ANR（`server/routes/tools.py` 的 `qwen_chat` + `web/js/views/pnginfo.js`）
- **有图（识图）/ 无图（提取）都指向上面公网 URL**；content 用**数组**（多模态后端）；`httpx ... verify=False`。
- **有图：后端用 Pillow 统一 `convert("RGB")` 转 PNG**（兼容 avif/webp/jpg）→ base64 发送。
- **识图/提取走 SSE 流式**（`streamChat`），**前端读 `delta.reasoning_content`（可折叠思考）+ `delta.content`（正文）**，只读 content 会"空"。
- `max_tokens` 给够（如 16000），配合云端 `-c 32768`。
- 识图/提取共用 6006（一个模型全包），不再分 6006 识图 + 6008 提取。

---

## 三、一句话教训
> **Qwen3.8(混合 DeltaNet) 想跑通：选物理 Ada/Hopper ≥40G 显存 + 最新 llama.cpp CLI(--mmproj)+ 装 ffmpeg + GGUF 4-bit；reasoning-effort 只认 xhigh/medium/low(设 high 直接 500 空)；context 要 > max_tokens；前端读 reasoning+content；识图用 Pillow 转 png(avif 不认)；避开 vGPU(乱码) 与 24/32G(装不下 FP8)；GitHub 不通就本机 clone 传云端编；hf-mirror 走 Xet 用 aria2c 硬啃 + 只抓 `*.safetensors`；公网入口别落盘；无 systemd/crontab 用 rc.local/AutoDL 开机脚本。**

（本次 ANR 相关提交：`e8dc317`、`6e2f7f2`、`dc82966` 及后续识图流式/avif/思考档位等。）
