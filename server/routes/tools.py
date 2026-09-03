"""导演工具 / 超分 / 反推 / 法术解析 / 图片筛选 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from src.director_tools import run_director
from src.upscale_images import run_upscale
from utils.gen_queue import gen_queue
from utils.jobs import jobs
from utils.logger import logger
from utils.services import pnginfo as pnginfo_service
from utils.services import selector, tagger

router = APIRouter(prefix="/api", tags=["tools"])

_DIRECTOR_KINDS = {"remove_bg", "line_art", "sketch", "colorize", "emotion", "declutter"}
_UPSCALE_KINDS = {"realcugan", "anime4k", "waifu2x"}

# 导演工具调用 NovelAI augment-image 接口 -> 走生图队列;
# 超分降噪为本地引擎 -> 不进队列, 多线程立即执行 (与生图队列互不阻塞)。


# ---------------------------------------------------------------- 导演工具


@router.post("/director")
async def director(payload: dict):
    kind = payload.get("kind", "")
    if kind not in _DIRECTOR_KINDS:
        raise HTTPException(status_code=400, detail=f"未知的导演工具: {kind}")
    try:
        task = gen_queue.submit(
            f"导演工具:{kind}",
            run_director,
            kind,
            payload.get("path"),
            payload.get("image"),
            payload.get("options") or {},
            label=f"导演工具 · {kind}",
        )
    except Exception as e:
        logger.error(f"提交导演工具任务失败: {e}")
        raise HTTPException(status_code=500, detail=f"提交任务失败: {e}")
    return {"job_id": task.id, "queued": True, "position": gen_queue.position(task.id)}


# ---------------------------------------------------------------- 超分


@router.post("/upscale")
async def upscale(payload: dict):
    kind = payload.get("kind", "")
    if kind not in _UPSCALE_KINDS:
        raise HTTPException(status_code=400, detail=f"未知的超分工具: {kind}")
    try:
        job_id = jobs.submit(
            f"超分:{kind}",
            run_upscale,
            kind,
            payload.get("path"),
            payload.get("image"),
            payload.get("options") or {},
        )
    except Exception as e:
        logger.error(f"提交超分任务失败: {e}")
        raise HTTPException(status_code=500, detail=f"提交任务失败: {e}")
    return {"job_id": job_id}


# ---------------------------------------------------------------- 法术解析


@router.post("/pnginfo")
async def get_pnginfo(payload: dict):
    info = pnginfo_service.get_pnginfo(payload.get("image_path"))
    if info is None:
        raise HTTPException(status_code=400, detail="请先上传图片")
    return info


@router.post("/pnginfo/to-generate")
async def pnginfo_to_generate(payload: dict):
    try:
        return pnginfo_service.pnginfo_to_generate(payload.get("image_path"))
    except Exception as e:
        logger.error(f"解析生成参数失败: {e}")
        raise HTTPException(status_code=400, detail=f"解析失败: {e}")


@router.post("/pnginfo/remove")
async def remove_pnginfo(payload: dict):
    try:
        message = pnginfo_service.remove_pnginfo(
            payload.get("image_path"),
            payload.get("batch_path"),
            payload.get("choices", []),
            payload.get("info", ""),
        )
        return {"message": message}
    except Exception as e:
        logger.error(f"清除元数据失败: {e}")
        raise HTTPException(status_code=400, detail=f"清除失败: {e}")


# ---------------------------------------------------------------- 反推


@router.post("/tagger")
async def run_tagger(payload: dict):
    image_path = payload.get("image_path")
    if not image_path:
        raise HTTPException(status_code=400, detail="请先上传图片")
    engine = payload.get("engine", "wd")
    # ---- 0.3 ComfyUI 反推 (PixAI Tagger v0.9 / Qwen3-VL) ----
    if engine in ("pixai", "qwen"):
        from utils.services import comfyui_tagger

        try:
            if engine == "pixai":
                out = comfyui_tagger.pixai_tagger(image_path)
            else:
                out = comfyui_tagger.qwen_vl(
                    image_path,
                    payload.get("model", "Qwen3VL-8B-Instruct-Q4_K_M.gguf"),
                    payload.get("preset", "🖼️ Simple Description"),
                )
            return {"string": out, "rating": {}, "characters": {}, "general": {}}
        except Exception as e:
            logger.error(f"0.3 反推失败: {e}")
            raise HTTPException(status_code=500, detail=f"0.3 反推失败: {e}")
    try:
        string, rating, characters, general = tagger.tagger(
            image_path,
            payload.get("model", "SmilingWolf/wd-swinv2-tagger-v3"),
            float(payload.get("general_thresh", 0.35)),
            bool(payload.get("general_mcut", False)),
            float(payload.get("character_thresh", 0.85)),
            bool(payload.get("character_mcut", False)),
        )
        return {
            "string": string,
            "rating": rating,
            "characters": characters,
            "general": general,
        }
    except Exception as e:
        logger.error(f"反推失败: {e}")
        raise HTTPException(status_code=500, detail=f"反推失败: {e}")


@router.get("/tagger/status")
async def tagger_status():
    """0.3 ComfyUI 在线状态 (反推页指示灯)。"""
    from utils.services import comfyui_tagger

    return {"online": comfyui_tagger.online()}


@router.post("/tagger/qwen-chat")
async def qwen_chat(payload: dict):
    """与 Qwen3-VL 对话: 图 + 提问 或 纯文本(tag) + 提取 -> SSE 流式回答(打字机)。"""
    image_path = payload.get("image_path")
    prompt = payload.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="请输入问题/文本")
    from utils.services import comfyui_tagger
    import ujson

    def gen():
        try:
            reply = comfyui_tagger.qwen_vl(
                image_path or None,
                payload.get("model", "Qwen3VL-8B-Instruct-Q4_K_M.gguf"),
                "🖼️ Simple Description",
                prompt,
            )
        except Exception as e:
            yield "data: " + ujson.dumps({"reply": "❌ " + str(e)}, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
            return
        # 结果流式打字机 (ComfyUI 节点整体出结果, 这里拆小块模拟流式显示)
        import time
        step = 3
        for i in range(0, len(reply), step):
            yield "data: " + ujson.dumps({"reply": reply[i:i + step]}, ensure_ascii=False) + "\n\n"
            time.sleep(0.012)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------- 图片筛选


@router.post("/selector/load")
async def selector_load(payload: dict):
    images, current = selector.load(payload.get("path", ""))
    return {"images": images, "current": current}


@router.post("/selector/next")
async def selector_next(payload: dict = None):
    images, current = selector.next_img((payload or {}).get("current"))
    return {"images": images, "current": current}


@router.post("/selector/move")
async def selector_move(payload: dict):
    images, current = selector.move(payload.get("current"), payload.get("output_path", ""))
    return {"images": images, "current": current}


@router.post("/selector/copy")
async def selector_copy(payload: dict):
    images, current = selector.copy(payload.get("current"), payload.get("output_path", ""))
    return {"images": images, "current": current}


@router.post("/selector/delete")
async def selector_delete(payload: dict):
    images, current = selector.delete(payload.get("current"))
    return {"images": images, "current": current}


@router.post("/selector/undo")
async def selector_undo():
    images, current = selector.undo()
    return {"images": images, "current": current}
