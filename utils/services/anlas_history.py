"""额度采样历史: 定时记录各 Token 的剩余点数 / 用量, 用于统计恢复速度。

NovelAI 的额度恢复规则未公开 (黑箱), 这里周期性调用**纯查询接口**
`/user/subscription` (不消耗任何额度、不生成图片) 采样落盘,
前端据此画折线图并估算"每小时恢复多少额度"。

- 采样间隔由 settings.json 的 `anlas_sample_interval` 控制 (秒, 默认 1800 = 半小时)
- 数据落盘 `outputs/anlas_history.json`, 只保留最近 MAX_SAMPLES 条
- 后端常驻线程采样, 与浏览器是否打开无关 (页面关着也在记录)
"""

from __future__ import annotations

import json
import threading
import time

from utils.config import BASE_DIR, env
from utils.logger import logger

HISTORY_FILE = BASE_DIR / "outputs" / "anlas_history.json"
GEN_FILE = BASE_DIR / "outputs" / "anlas_gen_events.json"  # 每次成功生成的记账 (供反推单张电量消耗)
MAX_SAMPLES = 4320  # 半小时一次 ≈ 90 天
MAX_GEN_EVENTS = 20000
DEFAULT_INTERVAL = 1800
MIN_INTERVAL = 60
_lock = threading.RLock()
_started = False


def record_generation(anlas=None, remains=None, note: str = "") -> None:
    """记录一次成功生成 (时刻 + 哪个 Token + 当时余额)。

    单张电量消耗小于 1% 时无法从整数读数直接看出, 必须靠"张数 + 时间段电量变化"
    反推 (ΔB = 纯回血速率×时长 − 单张成本×张数), 因此需要这份生成流水。
    """
    try:
        from utils.tokens import current_token, get_tokens, mask_token

        tok = current_token()
        tokens = get_tokens()
        idx = tokens.index(tok) if tok and tok in tokens else -1
        rec = {
            "t": time.time(),
            "index": idx,
            "token": mask_token(tok) if tok else None,
            "anlas": anlas,
            "remains": remains,
        }
        if note:
            rec["note"] = note
        with _lock:
            data = []
            try:
                if GEN_FILE.exists():
                    data = json.loads(GEN_FILE.read_text(encoding="utf-8")) or []
                    if not isinstance(data, list):
                        data = []
            except Exception:
                data = []
            data.append(rec)
            if len(data) > MAX_GEN_EVENTS:
                data = data[-MAX_GEN_EVENTS:]
            GEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            GEN_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"记录生成流水失败 (不影响生成): {e}")


def generation_events(hours: float | None = None) -> list[dict]:
    """生成流水 (最近 hours 小时内)。"""
    try:
        with _lock:
            data = json.loads(GEN_FILE.read_text(encoding="utf-8")) if GEN_FILE.exists() else []
    except Exception as e:
        logger.debug(f"读取生成流水失败: {e}")
        data = []
    if not isinstance(data, list):
        data = []
    if hours:
        cutoff = time.time() - float(hours) * 3600
        data = [r for r in data if float(r.get("t") or 0) >= cutoff]
    return data


def interval_seconds() -> int:
    """当前采样间隔 (秒)。"""
    try:
        return max(MIN_INTERVAL, int(getattr(env, "anlas_sample_interval", DEFAULT_INTERVAL) or DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL


def _load() -> list[dict]:
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.debug(f"读取额度历史失败: {e}")
    return []


def samples(hours: float | None = None) -> list[dict]:
    """全部 (或最近 hours 小时内) 的采样记录, 按时间升序。"""
    with _lock:
        data = _load()
    if hours:
        cutoff = time.time() - float(hours) * 3600
        data = [s for s in data if float(s.get("t") or 0) >= cutoff]
    return data


def take_sample(reason: str = "auto") -> dict | None:
    """采一次样并落盘 (纯查询接口, 不消耗额度)。失败返回 None。"""
    from utils.generator import inquire_anlas_all

    try:
        tokens = inquire_anlas_all()
    except Exception as e:
        logger.debug(f"额度采样失败: {e}")
        return None
    if not tokens:
        return None

    # 顺带刷新 Token 可用性状态 (额度/点数耗尽的可被停用, 见 token_health)
    try:
        from utils.services import token_health

        token_health.update_many(tokens, "sample")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"同步 Token 健康状态失败: {e}")

    sample = {"t": time.time(), "reason": reason, "tokens": tokens}
    with _lock:
        data = _load()
        data.append(sample)
        if len(data) > MAX_SAMPLES:
            data = data[-MAX_SAMPLES:]
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"写入额度历史失败: {e}")
            return None
    return sample


def _slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    """最小二乘拟合斜率, 换算成 "每小时变化量"; 点数不足返回 None。"""
    n = len(points)
    if n < 2:
        return None
    mean_t = sum(p[0] for p in points) / n
    mean_v = sum(p[1] for p in points) / n
    var = sum((p[0] - mean_t) ** 2 for p in points)
    if var <= 0:
        return None
    cov = sum((p[0] - mean_t) * (p[1] - mean_v) for p in points)
    return cov / var * 3600.0


def estimate_cost_per_image(stats: list[dict], events: list[dict]) -> None:
    """给统计补上「窗口内生成张数」与「单张电量消耗估计」(原地修改 stats)。

    原理: Δ电量 = 纯回血速率 × 时长 − 单张成本 × 张数
      → 单张成本 = (纯回血速率 × 时长 − 电量净变化) / 张数
    其中纯回血速率用接口推算值 (3600 / timeUntilNextPercent)。

    注意: 同一账号若被他人共用 (拼车), 别人的消耗也会计入 → 估计值偏高, 是**上限**。
    """
    by_token: dict[int, int] = {}
    for e in events or []:
        by_token[int(e.get("index", -1))] = by_token.get(int(e.get("index", -1)), 0) + 1

    for s in stats:
        n = by_token.get(int(s.get("index", -2)), 0)
        s["generated"] = n
        s["battery_delta"] = None
        s["cost_per_image"] = None
        pts = s.get("points") or []
        if n <= 0 or len(pts) < 2:
            continue
        span_h = float(s.get("span_hours") or 0)
        rate = s.get("predicted_per_hour") or s.get("measured_per_hour")
        if span_h <= 0 or not rate:
            continue
        db = float(pts[-1][1]) - float(pts[0][1])
        s["battery_delta"] = round(db, 1)
        s["cost_per_image"] = round((float(rate) * span_h - db) / n, 4)


def compute_stats(data: list[dict]) -> list[dict]:
    """按 Token 汇总: 当前值 / 实测恢复速度 (%/小时) / 接口预测速度 / 点数变化。

    配置里的每个 Token 都会出现在结果里 —— 新添加的 Token 即使还没有采样点也会列出
    (否则前端下拉/统计会漏掉它)。
    """
    from utils.tokens import get_tokens, mask_token

    by_token: dict[int, list[dict]] = {}
    for s in data:
        for t in s.get("tokens") or []:
            idx = int(t.get("index") or 0)
            by_token.setdefault(idx, []).append({"t": float(s.get("t") or 0), "v": t})

    configured = get_tokens()
    for i in range(len(configured)):
        by_token.setdefault(i, [])

    stats: list[dict] = []
    for idx in sorted(by_token):
        rows = sorted(by_token[idx], key=lambda r: r["t"])
        masked = mask_token(configured[idx]) if idx < len(configured) else None
        if not rows:
            # 还没有采样点 (例如刚添加的 Token)
            stats.append({
                "index": idx,
                "token": masked,
                "samples": 0,
                "span_hours": 0.0,
                "remains": None,
                "anlas": None,
                "measured_per_hour": None,
                "predicted_per_hour": None,
                "next_percent_in": None,
                "anlas_delta": None,
                "points": [],
                "anlas_points": [],
            })
            continue
        usage_pts = [(r["t"], float(r["v"]["remains"])) for r in rows if float(r["v"].get("remains", -1)) >= 0]
        anlas_pts = [(r["t"], float(r["v"]["anlas"])) for r in rows if float(r["v"].get("anlas", -1)) >= 0]
        latest = rows[-1]["v"]
        span_h = (rows[-1]["t"] - rows[0]["t"]) / 3600.0 if len(rows) > 1 else 0.0

        measured = _slope_per_hour(usage_pts) if len(usage_pts) >= 2 else None
        pred = None
        secs = latest.get("next_percent_in")
        try:
            secs = float(secs)
            if secs > 0:
                pred = 3600.0 / secs
        except (TypeError, ValueError):
            pred = None

        stats.append({
            "index": idx,
            "token": latest.get("token") or masked,
            "samples": len(rows),
            "span_hours": round(span_h, 2),
            "remains": latest.get("remains"),
            "anlas": latest.get("anlas"),
            "measured_per_hour": round(measured, 3) if measured is not None else None,
            "predicted_per_hour": round(pred, 3) if pred is not None else None,
            "next_percent_in": latest.get("next_percent_in"),
            "anlas_delta": round(anlas_pts[-1][1] - anlas_pts[0][1], 1) if len(anlas_pts) >= 2 else None,
            "points": [[int(r["t"]), r["v"].get("remains")] for r in rows],
            "anlas_points": [[int(r["t"]), r["v"].get("anlas")] for r in rows],
        })
    return stats


def start_scheduler() -> None:
    """启动后台采样线程 (幂等)。"""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def _loop() -> None:
        time.sleep(8)  # 等启动预热完成, 避免与缓存预热抢代理
        while True:
            try:
                if not getattr(env, "skip_inquire_anlas", False):
                    s = take_sample("auto")
                    if s:
                        logger.debug(f"额度采样完成 (共 {len(s.get('tokens') or [])} 个 Token)")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"额度采样异常: {e}")
            time.sleep(interval_seconds())

    threading.Thread(target=_loop, daemon=True, name="anlas-sampler").start()
