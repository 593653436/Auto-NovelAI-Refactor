"""Token 可用性状态: 额度(用量电池) 或 点数(Anlas) 耗尽的 Token 可临时停用。

需求: 某个 Token 的额度/点数用完后, 不要一直拿它去生图然后失败丢弃,
而是把它 **暂停**, 直到额度恢复才重新参与生图 (由生图队列读取本模块状态)。

- 判定模式 `env.skip_exhausted_mode`: off(不停用) | usage(仅无额度) | anlas(仅无点数) | both(任一)
- 状态来源:
  1. 后台采样线程每次采样后刷新 (每 anlas_sample_interval 秒)
  2. 生图任务失败后对该 Token 立即复查一次 (纯查询接口, 不消耗额度)
  3. 前端手动"检查额度"时刷新
- 本模块只做判定与记录, 不做网络请求; 查询统一走 utils.generator.inquire_anlas_all()
"""

from __future__ import annotations

import threading
import time

from utils.config import env
from utils.logger import logger

MODES = ("off", "usage", "anlas", "both")
MODE_LABELS = {
    "off": "不停用 (额度用完也照常排队, 失败后丢弃)",
    "usage": "停用【无额度】的 (用量电池为 0)",
    "anlas": "停用【无点数】的 (Anlas 为 0)",
    "both": "停用【无额度】或【无点数】的",
}

_lock = threading.RLock()
# index -> {usable, reason, remains, anlas, checked_at, source}
_records: dict[int, dict] = {}
_index_by_token: dict[str, int] = {}


def mode() -> str:
    m = str(getattr(env, "skip_exhausted_mode", "off") or "off").lower()
    return m if m in MODES else "off"


def set_mode(m: str) -> str:
    m = str(m or "off").lower()
    if m not in MODES:
        m = "off"
    env.update({"skip_exhausted_mode": m})
    # 立即按新模式重判已有记录: 状态与日志不需要等下一轮采样
    with _lock:
        recs = [dict(r) for r in _records.values()]
    for r in recs:
        try:
            update(int(r.get("index", 0)), r.get("anlas"), r.get("remains"), "mode-change")
        except Exception:  # noqa: BLE001
            pass
    return m


def _evaluate(anlas, remains) -> tuple[bool, str]:
    """按当前模式判定该 Token 是否可用。返回 (usable, reason)。"""
    m = mode()
    if m == "off":
        return True, ""
    try:
        a = int(anlas)
    except (TypeError, ValueError):
        a = -1
    try:
        r = int(remains)
    except (TypeError, ValueError):
        r = -1
    no_anlas = a == 0
    no_usage = r == 0
    if m == "usage" and no_usage:
        return False, "无额度 (用量电池为 0)"
    if m == "anlas" and no_anlas:
        return False, "无点数 (Anlas 为 0)"
    if m == "both":
        if no_usage and no_anlas:
            return False, "无额度且无点数"
        if no_usage:
            return False, "无额度 (用量电池为 0)"
        if no_anlas:
            return False, "无点数 (Anlas 为 0)"
    return True, ""


def update(index: int, anlas, remains, source: str = "sample") -> dict:
    """记录一次判定结果。"""
    usable, reason = _evaluate(anlas, remains)
    rec = {
        "index": int(index),
        "usable": usable,
        "reason": reason,
        "anlas": anlas,
        "remains": remains,
        "checked_at": time.time(),
        "source": source,
    }
    with _lock:
        old = _records.get(int(index))
        _records[int(index)] = rec
    # 只在真正发生状态变化时打日志 (首次记录不算"恢复", 否则开机就会误报一轮)
    if old is None:
        if not usable:
            logger.warning(f"Token#{index} 暂停生图: {reason} (恢复后自动启用)")
        else:
            logger.debug(f"Token#{index} 状态已记录 (可用)")
    elif old.get("usable") != usable:
        if usable:
            logger.info(f"Token#{index} 额度已恢复, 重新参与生图")
        else:
            logger.warning(f"Token#{index} 暂停生图: {reason} (恢复后自动启用)")
    return rec


def update_many(tokens: list[dict], source: str = "sample") -> None:
    """按 inquire_anlas_all() 的返回批量刷新。"""
    for t in tokens or []:
        try:
            update(int(t.get("index", 0)), t.get("anlas"), t.get("remains"), source)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"刷新 Token 健康状态失败: {e}")


def mark_failed(index: int, error: str = "") -> None:
    """生图失败后的处理: 先按上次读数重判, 再后台复查一次真实额度。"""
    with _lock:
        rec = _records.get(int(index))
    if rec:
        update(index, rec.get("anlas"), rec.get("remains"), "after-fail")
    recheck_async(int(index), error)


def recheck_async(index: int, error: str = "") -> None:
    """后台复查某个 Token 的真实额度 (纯查询, 不消耗额度)。"""

    def _run():
        try:
            from utils.generator import inquire_anlas_all

            tokens = inquire_anlas_all()
            target = [t for t in tokens if int(t.get("index", -1)) == int(index)]
            if target:
                update(index, target[0].get("anlas"), target[0].get("remains"), "after-fail")
            else:
                logger.debug(f"复查 Token#{index} 无结果 (可能已被移除)")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"复查 Token#{index} 额度失败: {e}")

    threading.Thread(target=_run, daemon=True, name=f"token-recheck-{index}").start()
    if error:
        logger.debug(f"Token#{index} 生成失败, 已触发额度复查: {error[:120]}")


def check_all(source: str = "manual") -> list[dict]:
    """实时查询全部 Token 并刷新状态 (纯查询接口, 不消耗额度)。"""
    try:
        from utils.generator import inquire_anlas_all

        tokens = inquire_anlas_all()
        update_many(tokens, source)
        return tokens
    except Exception as e:  # noqa: BLE001
        logger.debug(f"检查 Token 额度失败: {e}")
        return []


def is_usable(index: int) -> bool:
    """该 Token 当前是否可用于生图 (模式为 off 时恒可用)。

    注意: 必须用**当前模式**重新判定记录里的原始数值 —— 模式切换后旧结论立即失效,
    否则会出现"改了模式但不生效, 要等下一次采样"的问题。
    """
    m = mode()
    if m == "off":
        return True
    with _lock:
        rec = _records.get(int(index))
    if not rec:
        return True  # 尚无数据时不拦, 避免误停
    usable, _ = _evaluate(rec.get("anlas"), rec.get("remains"))
    return usable


def reason(index: int) -> str:
    m = mode()
    if m == "off":
        return ""
    with _lock:
        rec = _records.get(int(index))
    if not rec:
        return ""
    usable, why = _evaluate(rec.get("anlas"), rec.get("remains"))
    return "" if usable else why


def snapshot() -> list[dict]:
    with _lock:
        return [dict(v) for _, v in sorted(_records.items())]
