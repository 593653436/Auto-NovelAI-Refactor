"""0.3 强机 ComfyUI 反推: PixAI Tagger v0.9 (ONNX) + Qwen3-VL (AILab_QwenVL)。

通过 ComfyUI API 调用: 上传图 -> 提交 workflow -> 轮询 history 取结果。
- 结果统一从 PreviewAny 节点的 text 字段取 (已验证: PixAI 输出 tags, QwenVL 输出描述)。
"""

from __future__ import annotations

import time

import requests

from utils.logger import logger

# 0.3 强机上的 ComfyUI (已探测: http://192.168.0.3:8188, 在线)
COMFYUI_URL = "http://192.168.0.3:8188"

_HEADERS = {"Content-Type": "application/json"}


def online(timeout: float = 3.0) -> bool:
    """检测 0.3 ComfyUI 是否在线。"""
    try:
        r = requests.get(COMFYUI_URL + "/system_stats", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _upload(image_path: str) -> str:
    """上传本地图片到 ComfyUI, 返回服务端文件名。"""
    with open(image_path, "rb") as f:
        r = requests.post(COMFYUI_URL + "/upload/image", files={"image": f}, timeout=30)
    r.raise_for_status()
    up = r.json()
    name = up.get("name")
    sub = up.get("subfolder") or ""
    return (sub + "/" + name) if sub else name


def _run(prompt: dict) -> str:
    """提交 workflow, 轮询 history, 返回 PreviewAny(节点3) 的 text 结果。"""
    r = requests.post(COMFYUI_URL + "/prompt", json={"prompt": prompt}, timeout=30)
    r.raise_for_status()
    pid = r.json().get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 提交失败: {r.text[:200]}")
    # 最多等 10 分钟 (模型首次加载可能较慢)
    for _ in range(200):
        time.sleep(3)
        try:
            h = requests.get(COMFYUI_URL + "/history/" + pid, timeout=20).json()
        except Exception:
            continue
        if pid not in h:
            continue
        v = h[pid]
        st = v.get("status", {})
        if st.get("status_str") == "error":
            for m in st.get("messages", []):
                if m[0] == "execution_error":
                    raise RuntimeError(f"ComfyUI 执行错误: {json_str(m[1])}")
        out = v.get("outputs", {})
        txt = out.get("3", {}).get("text")
        if txt is not None:
            return str(txt)
    raise RuntimeError("ComfyUI 反推超时 (10 分钟)")


def json_str(o) -> str:
    import ujson

    try:
        return ujson.dumps(o, ensure_ascii=False)[:600]
    except Exception:
        return str(o)


def pixai_tagger(image_path: str) -> str:
    """PixAI Tagger v0.9 (ONNX) 反推 -> tags 字符串。"""
    name = _upload(image_path)
    prompt = {
        "1": {"class_type": "LoadImage", "inputs": {"image": name}},
        "2": {
            "class_type": "PixAITagger|adbrasi",
            "inputs": {
                "image": ["1", 0],
                "backend": "onnx",
                "batch_size": 1,
                "threshold": 0.3,
                "character_threshold": 0.85,
                "separator": ", ",
                "exclude_tags": "",
                "include_ip": True,
                "replace_underscore": False,
                "trailing_comma": False,
                "force_download": False,
                "device": "auto",
                "fp16": True,
                "pytorch_repo_id": "pixai-labs/pixai-tagger-v0.9",
                "onnx_repo_id": "deepghs/pixai-tagger-v0.9-onnx",
                "hf_token": "",
            },
        },
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 0]}},
    }
    return _clean_text(_run(prompt))


def _clean_text(s) -> str:
    """去掉模型输出外层 [...]/引号, 让前端显示纯净文本。"""
    s = str(s or "").strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if inner.startswith("'") and inner.endswith("'"):
            inner = inner[1:-1]
        elif inner.startswith('"') and inner.endswith('"'):
            inner = inner[1:-1]
        s = inner
    return s.strip().strip('"').strip("'")


def qwen_vl(image_path: str, model: str = "Qwen3VL-8B-Instruct-Q4_K_M.gguf", preset: str = "🖼️ Simple Description", custom_prompt: str = "") -> str:
    """Qwen3-VL (AILab_QwenVL_GGUF 本地 GGUF) 反推/对话。
    - custom_prompt 非空时用它(用户自定义提示词/提问), 否则用 preset 预设模板。
    """
    name = _upload(image_path)
    prompt = {
        "1": {"class_type": "LoadImage", "inputs": {"image": name}},
        "2": {
            "class_type": "AILab_QwenVL_GGUF",
            "inputs": {
                "image": ["1", 0],
                "model_name": model,
                "preset_prompt": preset,
                "custom_prompt": custom_prompt,
                "max_tokens": 512,
                "keep_model_loaded": True,
                "seed": 1,
            },
        },
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 0]}},
    }
    return _clean_text(_run(prompt))
