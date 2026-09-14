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
    "off": "不停用 (默认: 检测到的异常只提示, 不阻止生图)",
    "usage": "自动停用【无额度】的 (用量电池为 0)",
    "anlas": "自动停用【无点数】的 (Anlas 为 0)",
    "both": "自动停用【无额度】或【无点数】的",
}

_lock = threading.RLock()
# index -> {usable, reason, remains, anlas, checked_at, source}
_records: dict[int, dict] = {}
_index_by_token: dict[str, int] = {}
# 手动停用的 Token 序号: 采样照常进行, 只是不参与生图 (面板/队列弹窗可切换)
# 持久化在 settings.json 的 manual_disabled_tokens —— 重启后仍然生效
def _load_persisted_manual() -> set[int]:
    try:
        raw = getattr(env, "manual_disabled_tokens", None) or []
        return {int(x) for x in raw}
    except Exception:  # noqa: BLE001
        return set()


_manual: set[int] = _load_persisted_manual()


def _save_persisted_manual() -> None:
    try:
        with _lock:
            items = sorted(_manual)
        env.update({"manual_disabled_tokens": items})
    except Exception as e:  # noqa: BLE001
        logger.debug(f"保存手动停用列表失败: {e}")


def is_manual_disabled(index: int) -> bool:
    with _lock:
        return int(index) in _manual


def set_manual_disabled(index: int, disabled: bool) -> dict:
    """手动停用/启用某个 Token 的生图 (采样不受影响)。"""
    idx = int(index)
    with _lock:
        if disabled:
            _manual.add(idx)
        else:
            _manual.discard(idx)
        rec = dict(_records.get(idx) or {})
    _save_persisted_manual()
    # 立即按新状态重判一次, 队列与面板无需等下一轮采样
    update(idx, rec.get("anlas"), rec.get("remains"), "manual", rec.get("active"))
    logger.warning(f"Token#{idx} 已{'手动停用' if disabled else '手动启用'}生图 (采样继续)")
    return {"index": idx, "manual_disabled": disabled}


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
            update(
                int(r.get("index", 0)),
                r.get("anlas"),
                r.get("remains"),
                "mode-change",
                r.get("active"),  # 必须带上, 否则订阅状态会被覆盖成 None
            )
        except Exception:  # noqa: BLE001
            pass
    return m


def _block_reason(anlas, remains, active, index) -> str:
    """真正**阻止生图**的原因。默认只认"手动停用"(用户亲手按的)。

    自动检测出的问题 (订阅失效 / 电量耗尽 / 点数耗尽) 一律**只提示不阻止** ——
    用户需要看到真实的报错, 而不是被悄悄跳过。
    唯一的例外: 用户在面板显式选择了 skip_exhausted_mode (opt-in, 默认 off)。
    """
    if index is not None and is_manual_disabled(index):
        return "手动停用"
    m = mode()
    if m == "off":
        return ""
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
        return "无额度 (用量电池为 0)"
    if m == "anlas" and no_anlas:
        return "无点数 (Anlas 为 0)"
    if m == "both":
        if no_usage and no_anlas:
            return "无额度且无点数"
        if no_usage:
            return "无额度 (用量电池为 0)"
        if no_anlas:
            return "无点数 (Anlas 为 0)"
    return ""


def _warning(anlas, remains, active) -> str:
    """仅用于**提示**(不阻止生图): 订阅失效 / 电量用尽 / 点数用尽。"""
    if active is False:
        return "订阅已失效 (无法生图)"
    try:
        a = int(anlas)
    except (TypeError, ValueError):
        a = -1
    try:
        r = int(remains)
    except (TypeError, ValueError):
        r = -1
    if r == 0:
        return "电量用尽 (用量电池为 0)"
    if a == 0:
        return "点数用尽 (Anlas 为 0)"
    return ""


def update(index: int, anlas, remains, source: str = "sample", active=None) -> dict:
    """记录一次判定结果。"""
    block = _block_reason(anlas, remains, active, index)
    rec = {
        "index": int(index),
        "usable": not block,          # 只有"手动停用"或用户显式开启的跳过模式才会 False
        "reason": block,
        "warn": "" if block else _warning(anlas, remains, active),  # 仅提示, 不阻止生图
        "anlas": anlas,
        "remains": remains,
        "active": active,
        "manual_disabled": is_manual_disabled(index),
        "checked_at": time.time(),
        "source": source,
    }
    with _lock:
        old = _records.get(int(index))
        _records[int(index)] = rec
    # 只在真正发生状态变化时打日志 (首次记录不算"恢复", 否则开机就会误报一轮)
    usable = rec["usable"]
    reason = rec["reason"]
    if old is None:
        if not usable:
            logger.warning(f"Token#{index} 已停用生图: {reason}")
        else:
            logger.debug(f"Token#{index} 状态已记录 (可用)")
    elif bool(old.get("usable")) != usable:
        if usable:
            logger.info(f"Token#{index} 已恢复参与生图")
        else:
            logger.warning(f"Token#{index} 已停用生图: {reason}")
    return rec


def update_many(tokens: list[dict], source: str = "sample") -> None:
    """按 inquire_anlas_all() 的返回批量刷新。"""
    for t in tokens or []:
        try:
            update(int(t.get("index", 0)), t.get("anlas"), t.get("remains"), source, t.get("active"))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"刷新 Token 健康状态失败: {e}")


def mark_failed(index: int, error: str = "") -> None:
    """生图失败后的处理: 先按上次读数重判, 再后台复查一次真实额度。"""
    with _lock:
        rec = _records.get(int(index))
    if rec:
        update(index, rec.get("anlas"), rec.get("remains"), "after-fail", rec.get("active"))
    recheck_async(int(index), error)


def recheck_async(index: int, error: str = "") -> None:
    """后台复查某个 Token 的真实额度 (纯查询, 不消耗额度)。"""

    def _run():
        try:
            from utils.generator import inquire_anlas_all

            tokens = inquire_anlas_all()
            target = [t for t in tokens if int(t.get("index", -1)) == int(index)]
            if target:
                update(index, target[0].get("anlas"), target[0].get("remains"), "after-fail", target[0].get("active"))
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
    """该 Token 当前是否可用于生图 (模式为 off 时恒可用; 订阅失效恒不可用)。

    注意: 必须用**当前模式**重新判定记录里的原始数值 —— 模式切换后旧结论立即失效,
    否则会出现"改了模式但不生效, 要等下一次采样"的问题。
    """
    with _lock:
        rec = _records.get(int(index))
    if not rec:
        return not is_manual_disabled(index)  # 尚无数据时不拦, 但手动停用仍生效
    return not _block_reason(rec.get("anlas"), rec.get("remains"), rec.get("active"), index)


def reason(index: int) -> str:
    with _lock:
        rec = _records.get(int(index))
    if not rec:
        return "手动停用" if is_manual_disabled(index) else ""
    return _block_reason(rec.get("anlas"), rec.get("remains"), rec.get("active"), index)


def snapshot() -> list[dict]:
    with _lock:
        return [dict(v) for _, v in sorted(_records.items())]
