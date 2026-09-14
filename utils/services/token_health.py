"""Token 可用性状态: 以 **token 本身(打码字符串)** 为标识, 而不是槽位下标。

为什么不用下标: 在设置里删除/新增/重排 API 后, 下标会整体位移, 会造成
  · "停用标记留在槽位上" —— 原本被停用的 API 前移一位后变成启用
  · 额度历史串台 —— 同一下标先后属于不同 API, 数据被混在一起
改成以 token 标识后, 这些状态始终跟着 API 走: 删掉就消失, 重新加回来自动匹配。

- 判定模式 `env.skip_exhausted_mode`: off(默认, 只提示不阻止) | usage | anlas | both
- **真正阻止生图**只有两种: ① 用户手动停用 ② 用户显式开启的 skip 模式
- 自动检测出的问题(订阅失效/电量用尽/点数用尽)只记 warn, 不阻止
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
_records: dict[str, dict] = {}  # token标识 -> {usable, reason, warn, anlas, remains, index, ...}


def _load_persisted_manual() -> set[str]:
    """手动停用的 token 标识 (settings.json; 旧版存的是数字下标, 直接忽略)。"""
    try:
        raw = getattr(env, "manual_disabled_tokens", None) or []
        return {str(x) for x in raw if isinstance(x, str) and x.strip()}
    except Exception:  # noqa: BLE001
        return set()


_manual: set[str] = _load_persisted_manual()


def _save_persisted_manual() -> None:
    try:
        with _lock:
            items = sorted(_manual)
        env.update({"manual_disabled_tokens": items})
    except Exception as e:  # noqa: BLE001
        logger.debug(f"保存手动停用列表失败: {e}")


def is_manual_disabled(key) -> bool:
    if not key:
        return False
    with _lock:
        return str(key) in _manual


def set_manual_disabled(key, disabled: bool) -> dict:
    """手动停用/启用某个 Token 的生图 (采样不受影响)。key 为 token 标识。"""
    k = str(key or "").strip()
    if not k:
        return {"token": None, "manual_disabled": False}
    with _lock:
        if disabled:
            _manual.add(k)
        else:
            _manual.discard(k)
        rec = dict(_records.get(k) or {})
    _save_persisted_manual()
    # 立即按新状态重判一次, 队列与面板无需等下一轮采样
    update(k, rec.get("anlas"), rec.get("remains"), "manual", rec.get("active"), rec.get("index"))
    logger.warning(f"{k} 已{'手动停用' if disabled else '手动启用'}生图 (采样继续)")
    return {"token": k, "manual_disabled": disabled}


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
                r.get("token"),
                r.get("anlas"),
                r.get("remains"),
                "mode-change",
                r.get("active"),  # 必须带上, 否则订阅状态会被覆盖成 None
                r.get("index"),
            )
        except Exception:  # noqa: BLE001
            pass
    return m


def _block_reason(anlas, remains, active, key) -> str:
    """真正**阻止生图**的原因。默认只认"手动停用"(用户亲手按的)。

    自动检测出的问题一律只提示不阻止 —— 用户需要看到真实的报错。
    唯一的例外: 用户显式选择了 skip_exhausted_mode (opt-in, 默认 off)。
    """
    if is_manual_disabled(key):
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


def update(key, anlas, remains, source: str = "sample", active=None, index=None) -> dict | None:
    """记录一次判定结果。key 为 token 标识 (打码字符串)。"""
    k = str(key or "").strip()
    if not k:
        return None
    block = _block_reason(anlas, remains, active, k)
    rec = {
        "token": k,
        "index": index,
        "usable": not block,          # 只有"手动停用"或用户显式开启的跳过模式才会 False
        "reason": block,
        "warn": "" if block else _warning(anlas, remains, active),  # 仅提示, 不阻止生图
        "anlas": anlas,
        "remains": remains,
        "active": active,
        "manual_disabled": is_manual_disabled(k),
        "checked_at": time.time(),
        "source": source,
    }
    with _lock:
        old = _records.get(k)
        _records[k] = rec
    # 只在真正发生状态变化时打日志 (首次记录不算"恢复", 否则开机就会误报一轮)
    usable = rec["usable"]
    reason = rec["reason"]
    if old is None:
        if not usable:
            logger.warning(f"{k} 已停用生图: {reason}")
        else:
            logger.debug(f"{k} 状态已记录 (可用)")
    elif bool(old.get("usable")) != usable:
        if usable:
            logger.info(f"{k} 已恢复参与生图")
        else:
            logger.warning(f"{k} 已停用生图: {reason}")
    return rec


def update_many(tokens: list[dict], source: str = "sample") -> None:
    """按 inquire_anlas_all() 的返回批量刷新 (以返回里的 token 字段为标识)。"""
    for t in tokens or []:
        try:
            update(t.get("token"), t.get("anlas"), t.get("remains"), source, t.get("active"), t.get("index"))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"刷新 Token 健康状态失败: {e}")


def mark_failed(key, error: str = "") -> None:
    """生图失败后的处理: 先按上次读数重判, 再后台复查一次真实额度。"""
    k = str(key or "")
    with _lock:
        rec = dict(_records.get(k) or {})
    if rec:
        update(k, rec.get("anlas"), rec.get("remains"), "after-fail", rec.get("active"), rec.get("index"))
    recheck_async(k, error)


def recheck_async(key, error: str = "") -> None:
    """后台复查某个 Token 的真实额度 (纯查询, 不消耗额度)。"""
    k = str(key or "")

    def _run():
        try:
            from utils.generator import inquire_anlas_all

            target = [t for t in inquire_anlas_all() if str(t.get("token") or "") == k]
            if target:
                update(k, target[0].get("anlas"), target[0].get("remains"), "after-fail",
                       target[0].get("active"), target[0].get("index"))
            else:
                logger.debug(f"{k} 复查无结果 (可能已被移除)")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{k} 复查额度失败: {e}")

    threading.Thread(target=_run, daemon=True, name="token-recheck").start()
    if error:
        logger.debug(f"{k} 生成失败, 已触发额度复查: {error[:120]}")


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


def is_usable(key) -> bool:
    """该 Token 当前是否可用于生图。key 为 token 标识。"""
    k = str(key or "").strip()
    if not k:
        return True
    with _lock:
        rec = _records.get(k)
    if not rec:
        return not is_manual_disabled(k)
    return not _block_reason(rec.get("anlas"), rec.get("remains"), rec.get("active"), k)


def reason(key) -> str:
    k = str(key or "").strip()
    if not k:
        return ""
    with _lock:
        rec = _records.get(k)
    if not rec:
        return "手动停用" if is_manual_disabled(k) else ""
    return _block_reason(rec.get("anlas"), rec.get("remains"), rec.get("active"), k)


def snapshot() -> list[dict]:
    """全部记录 (按当前下标排序, 便于前端按顺序显示)。"""
    with _lock:
        out = [dict(v) for v in _records.values()]

    def _sort_key(r):
        idx = r.get("index")
        return (0, idx) if isinstance(idx, int) else (1, str(r.get("token") or ""))

    return sorted(out, key=_sort_key)
