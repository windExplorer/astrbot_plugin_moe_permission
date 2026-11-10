"""模型（提供商）引用的一致性检查：AstrBot 删除 / 禁用模型后，插件里残留的引用。

背景与取舍
----------
AstrBot 用「提供商 id」标识一个模型，插件把它持久化在等级路由、可切换名单和
对象专属模型里。AstrBot 后台**删除**模型时配置里就没了；**禁用**时不加载实例
（``manager.load_provider`` 里 ``enable=false`` 直接 skip），两种情况下
``context.get_all_providers()`` 都看不到它。此外还有「配置里在、但加载失败」的
暂时态。

所以**绝不能在启动时自动清理**：禁用是可逆的、加载是有时序的，自动清理会在
用户临时禁用模型时把配好的等级路由抹掉。本模块只做三件事：

1. 把库里所有模型引用收集起来（纯函数，方便自检）；
2. 按「已加载 / 已禁用 / 未加载 / 已删除 / 未知」分类；
3. 给写入侧提供「不能新增悬空引用」的判定。

真正删数据只由用户在控制台**主动点击**后走 ``Store.prune_provider_refs``，
且只清用户明确勾选的那些 id。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

# policy 表里 effect 直接存提供商 id 的 feature（model_choice 是废弃的旧版，一并纳入）
MODEL_FEATURES = ("model", "model_choice")
LEVEL_FIELDS = ("provider_id", "fallback_provider_id")

STATUS_AVAILABLE = "available"
STATUS_DISABLED = "disabled"       # 配置里在，enable=false
STATUS_UNAVAILABLE = "unavailable"  # 配置里在且 enable=true，但没加载成功 / 还没加载完
STATUS_DELETED = "deleted"         # 配置里已经没有这个 id
STATUS_UNKNOWN = "unknown"         # 读不到配置，无法区分（此时禁止判定为 deleted）

# 需要提示的失效状态（available 不在其中）
STALE_STATUSES = (STATUS_DISABLED, STATUS_UNAVAILABLE, STATUS_DELETED, STATUS_UNKNOWN)

STATUS_TEXT = {
    STATUS_DISABLED: "已禁用",
    STATUS_UNAVAILABLE: "未加载",
    STATUS_DELETED: "已删除",
    STATUS_UNKNOWN: "状态未知",
}


def parse_switch_providers(raw: Any) -> list[str]:
    """等级 ``switch_providers`` 列的 JSON 文本 / list → 去空去重保序的 id 列表。

    与 ``store.parse_provider_list`` 同语义，但这里不 import store（那个模块
    依赖 aiosqlite，本模块要能脱离环境直接跑自检）。
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple, set)):
        seq = list(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        # 与 store.parse_provider_list 同语义：JSON 解不开时按「手工填的单个裸 id」处理，
        # 否则老库里手填的那份名单会被当成没有引用、报告漏报。
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            seq = [text]
        else:
            seq = list(parsed) if isinstance(parsed, (list, tuple)) else [parsed]
    else:
        return []
    out: list[str] = []
    for item in seq:
        pid = str(item or "").strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def dump_switch_providers(ids: Iterable[str]) -> str:
    """写回等级列的 JSON 文本（与 store.dump_provider_list 同格式）。"""
    seen: list[str] = []
    for pid in ids:
        text = str(pid or "").strip()
        if text and text not in seen:
            seen.append(text)
    return json.dumps(seen, ensure_ascii=False)


def _level_ref(level: Mapping[str, Any], field: str) -> dict[str, Any]:
    kind = str(level.get("kind") or "user")
    name = str(level.get("name") or "")
    where = "好友等级" if kind == "user" else "群聊等级"
    field_text = {
        "provider_id": "主模型",
        "fallback_provider_id": "备用模型",
        "switch_providers": "可切换名单",
    }.get(field, field)
    return {
        "where": "level",
        "level_id": int(level.get("id") or 0),
        "level_kind": kind,
        "field": field,
        "label": f"{where}「{name}」的{field_text}",
    }


def _policy_ref(row: Mapping[str, Any]) -> dict[str, Any]:
    scope_type = str(row.get("scope_type") or "")
    scope_id = str(row.get("scope_id") or "")
    where = {
        "user": "好友专属模型",
        "group": "群专属模型",
        "member": "群成员专属模型",
        "level": "等级专属模型",
        "global": "全局专属模型",
    }.get(scope_type, "专属模型")
    return {
        "where": "policy",
        "scope_type": scope_type,
        "scope_id": scope_id,
        "feature": str(row.get("feature") or ""),
        "scene": str(row.get("scene") or ""),
        "label": f"{where}（{scope_id}）",
    }


def collect_refs(
    levels: Iterable[Mapping[str, Any]],
    policies: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """收集所有持久化的模型引用：``{provider_id: [引用位置, ...]}``。

    覆盖等级的主 / 备模型、可切换名单，以及 ``policy`` 里 feature 为
    ``model`` / ``model_choice`` 的行。**不碰** ``usage_log`` / ``audit_log``：
    那是历史数据，模型删了也要留着对账。
    """
    out: dict[str, list[dict[str, Any]]] = {}

    def add(pid: str, ref: dict[str, Any]) -> None:
        out.setdefault(pid, []).append(ref)

    for level in levels or []:
        for field in LEVEL_FIELDS:
            pid = str(level.get(field) or "").strip()
            if pid:
                add(pid, _level_ref(level, field))
        for pid in parse_switch_providers(level.get("switch_providers")):
            add(pid, _level_ref(level, "switch_providers"))

    for row in policies or []:
        if str(row.get("feature") or "") not in MODEL_FEATURES:
            continue
        pid = str(row.get("effect") or "").strip()
        # inherit / allow / deny 这类枚举值不该被当成 id（历史脏数据兜底）
        if pid and pid not in ("inherit", "allow", "deny"):
            add(pid, _policy_ref(row))
    return out


def classify(
    pid: str,
    available: Iterable[str],
    configured: Mapping[str, Mapping[str, Any]] | None,
) -> str:
    """给一个 id 判状态。

    ``configured`` 传 ``None`` 表示「读不到 AstrBot 配置」——这时**不能**把
    未加载的 id 判成已删除，只能报 unknown，否则一次 API 取不到就可能导致
    用户误点清理、把配置删光。
    """
    if pid in set(available or ()):
        return STATUS_AVAILABLE
    if configured is None:
        return STATUS_UNKNOWN
    cfg = configured.get(pid)
    if cfg is None:
        return STATUS_DELETED
    return STATUS_DISABLED if not bool(cfg.get("enable", True)) else STATUS_UNAVAILABLE


def build_report(
    refs: Mapping[str, list[dict[str, Any]]],
    available: Iterable[str],
    configured: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """生成控制台用的失效引用报告。

    返回 ``{available, configured_known, stale: [...], counts, total_refs}``；
    ``stale`` 按「已删除 → 已禁用 → 未加载 → 未知」排序，每项带引用位置列表。
    """
    alive = sorted(set(available or ()))
    stale: list[dict[str, Any]] = []
    counts = {s: 0 for s in STALE_STATUSES}
    for pid, places in refs.items():
        status = classify(pid, alive, configured)
        if status == STATUS_AVAILABLE:
            continue
        counts[status] += 1
        stale.append(
            {
                "provider_id": pid,
                "status": status,
                "status_text": STATUS_TEXT.get(status, status),
                "refs": places,
                "ref_count": len(places),
            }
        )
    order = {s: i for i, s in enumerate(STALE_STATUSES)}
    stale.sort(key=lambda it: (order.get(it["status"], 99), -it["ref_count"], it["provider_id"]))
    return {
        "available": alive,
        "configured_known": configured is not None,
        "stale": stale,
        "counts": counts,
        "total": len(stale),
        "total_refs": sum(it["ref_count"] for it in stale),
    }


def stale_ids(report: Mapping[str, Any]) -> set[str]:
    """从报告里取出所有失效 id（供 /levels 标字段、行为侧过滤复用）。"""
    return {str(it.get("provider_id") or "") for it in (report.get("stale") or []) if it.get("provider_id")}


def stale_level_fields(level: Mapping[str, Any], stale: Iterable[str]) -> list[str]:
    """一个等级里哪些字段引用了失效 id（前端标红用）。"""
    dead = set(stale or ())
    bad = [f for f in LEVEL_FIELDS if str(level.get(f) or "").strip() in dead]
    if any(pid in dead for pid in parse_switch_providers(level.get("switch_providers"))):
        bad.append("switch_providers")
    return bad


def validate_new_ids(
    new_values: Mapping[str, Any],
    old_values: Mapping[str, Any] | None,
    known: Iterable[str],
) -> list[str]:
    """写入侧校验：返回「本次新增的、当前未加载的 id」列表。

    关键：用户只是编辑等级的其它字段时，主 / 备模型字段会原样回传。若那个模型
    恰好被临时禁用，直接拒绝会让他**连等级名字都改不了**。所以只拦「值被改动
    且新值不在已加载集合」的情况；原值原样保留。
    """
    alive = set(known or ())
    if not alive:  # 拿不到列表时放行，避免核心 API 异常把整个控制台写操作锁死
        return []
    old = old_values or {}
    bad: list[str] = []
    for field, raw in new_values.items():
        new = str(raw or "").strip()
        if not new or new == str(old.get(field) or "").strip():
            continue
        if new in alive:
            continue
        if new not in bad:
            bad.append(new)
    return bad


def filter_known(ids: Iterable[str], known: Iterable[str]) -> list[str]:
    """名单里只保留已加载的 id；``known`` 为空时原样保留（拿不到列表不静默丢数据）。"""
    seq = [str(p or "").strip() for p in (ids or ())]
    seq = [p for p in seq if p]
    alive = set(known or ())
    return seq if not alive else [p for p in seq if p in alive]
