"""控制台后端路由（AstrBot 插件 Page 的桥接 API）。

路由前缀 ``/astrbot_plugin_moe_permission``（AstrBot 约定：带插件名、不带 /page），
前端通过 ``window.AstrBotPluginPage.apiGet/apiPost`` 调用，由 Dashboard 转发。

⚠️ 返回信封必须遵循 AstrBot 的桥接约定，不能自创格式。Dashboard 侧的处理是
（见 ``dashboard/src/views/PluginPagePage.vue`` 的 ``handleBridgeRequest``）：

```js
if (response.data?.status === "error") throw new Error(response.data.message);
sendBridgeResponse(requestId, true, response.data?.data ?? response.data);
```

也就是说：
  - 成功 → ``{"status": "ok", "data": <payload>}``（Dashboard 只把 ``data`` 转发给前端）
  - 失败 → ``{"status": "error", "message": "..."}``（Dashboard 直接抛错，前端拿到 rejected promise）

M0 阶段已实现：健康检查、配置读写、总览统计、好友/群缓存列表、额度增删改查、权限规则读写、明细查询。
好友/群同步（``/sync``）依赖协议端调用，在 M1 随权限内核一起接入。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger

try:  # quart 是 AstrBot 的运行期依赖
    from quart import request
except Exception:  # pragma: no cover - 仅本地静态检查时缺失
    request = None  # type: ignore

try:  # 与 main.py 同样的双形态导入（包内正常加载 / 平铺调试）
    from . import model_card
    from . import provider_refs as provider_refs_mod
    from .gate import effect_in_scene
    from .store import MODEL_CHOICE_FEATURE, MODEL_FEATURE
    from .store import SCHEMA_VERSION as STORE_SCHEMA_VERSION
    from .store import member_scope_id, parse_member_scope_id, parse_provider_list
    from .sync import sync_group_members
except ImportError:  # pragma: no cover - 本地平铺调试
    import model_card  # type: ignore
    import provider_refs as provider_refs_mod  # type: ignore
    from gate import effect_in_scene  # type: ignore
    from store import MODEL_CHOICE_FEATURE, MODEL_FEATURE  # type: ignore
    from store import SCHEMA_VERSION as STORE_SCHEMA_VERSION  # type: ignore
    from store import member_scope_id, parse_member_scope_id, parse_provider_list  # type: ignore
    from sync import sync_group_members  # type: ignore

ROUTE_PREFIX = "/astrbot_plugin_moe_permission"

# 时间范围简写 → 秒数
_RANGE_SECONDS = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #
def ok(data: Any = None) -> dict:
    """成功信封（AstrBot 桥接约定）。"""
    return {"status": "ok", "data": data}


def err(message: str) -> dict:
    """失败信封（AstrBot 桥接约定：Dashboard 会据此抛错）。"""
    return {"status": "error", "message": message}


def _q(name: str, default: str = "") -> str:
    """读取查询参数（quart request 不可用时返回默认值）。"""
    try:
        if request is None:
            return default
        return (request.args.get(name) or default).strip()
    except Exception:
        return default


def _qi(name: str, default: int, lo: int, hi: int) -> int:
    """读取并夹紧的整型查询参数。"""
    try:
        return max(lo, min(hi, int(_q(name) or default)))
    except Exception:
        return default


def _scene_arg(default: str = "private") -> str:
    """读取「场景」参数（``private`` / ``group``）。

    好友与好友等级这两层权限是分场景的，控制台按当前场景读写；
    群聊页的规则（群专属 / 群等级）本来只在群里生效，传什么场景都取到同一条。
    """
    got = _q("scene", default) or default
    return got if got in ("private", "group") else default


async def _payload() -> dict:
    """读取 JSON 请求体。"""
    try:
        if request is None:
            return {}
        return await request.get_json(silent=True) or {}
    except Exception:
        return {}


def _load_schema() -> dict[str, Any]:
    """读取 ``_conf_schema.json``（配置项的唯一来源：默认值 / 类型 / 可选项）。"""
    try:
        path = Path(__file__).resolve().parent / "_conf_schema.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[UserGateway] 读取 _conf_schema.json 失败: {e}")
        return {}


def _range_bounds(range_key: str, from_raw: str, to_raw: str) -> tuple[int, int]:
    """解析统计时间范围，返回 ``(from_ts, to_ts)``（epoch 秒）。"""
    now = int(time.time())
    if from_raw:
        try:
            return int(float(from_raw)), int(float(to_raw)) if to_raw else now
        except Exception:
            pass
    span = _RANGE_SECONDS.get(range_key or "7d", _RANGE_SECONDS["7d"])
    return now - span, now


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是", "开")


# 列表页展示额度时的周期优先级：同一个对象可能同时配了日/月/累计额度，
# 列表上只展示一条，按 day > month > total 取最紧凑的那个。
_PERIOD_PREF = ("day", "month", "total")

# 档位的中文名（与 gate.py 的文案保持一致；消息类型的文案在前端）
LAYER_LABELS: dict[str, str] = {
    "user": "好友专属",
    "user_level": "好友等级",
    "group": "群专属",
    "group_level": "群等级",
    "global": "全局默认",
}


def _by_scope(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """把限额行归并成 ``{scope_id: {period: row}}``。"""
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        sid = str(row.get("scope_id") or "")
        if sid:
            out.setdefault(sid, {})[str(row.get("period"))] = row
    return out


def _levels_index(rows: list[dict]) -> dict[str, dict[int, dict]]:
    """``{kind: {level_id: level_row}}``，便于给列表行标等级名。"""
    out: dict[str, dict[int, dict]] = {"user": {}, "group": {}}
    for row in rows:
        try:
            out.setdefault(str(row.get("kind") or "user"), {})[int(row["id"])] = row
        except Exception:
            continue
    return out


def _effective_from_chain(chain: list[dict]) -> dict[str, Any]:
    """从 ``plugin.quota_chain()`` 的结果里挑出「生效档位」并压成一行展示数据。

    生效规则来自闸门内核（命中最具体的一层即止），这里只做展示层的取值：
    在该层内按 day > month > total 取第一个真正配了上限的周期。

    Returns:
        ``{layer, layer_label, period, limit, used, mode}``；没配额度时 ``layer`` 为空串。
    """
    for item in chain or []:
        if not item.get("effective"):
            continue
        limits = item.get("limits") or {}
        usage = item.get("usage") or {}
        for period in _PERIOD_PREF:
            row = limits.get(period)
            if not row:
                continue
            limit = int(row.get("limit_tokens") or 0)
            if limit <= 0:
                continue
            layer = str(item.get("layer") or "")
            return {
                "layer": layer,
                "layer_label": LAYER_LABELS.get(layer, str(item.get("label") or layer)),
                "scope_type": str(item.get("scope_type") or ""),
                "scope_id": str(item.get("scope_id") or ""),
                "period": period,
                "limit": limit,
                "used": int(usage.get(period) or 0),
                "mode": str(row.get("mode") or "enforce"),
                "exceeded": bool(item.get("exceeded")),
            }
    return {
        "layer": "",
        "layer_label": "",
        "scope_type": "",
        "scope_id": "",
        "period": "",
        "limit": 0,
        "used": 0,
        "mode": "",
        "exceeded": False,
    }


def _fmt_group(row: dict, ctx: dict[str, Any]) -> dict:
    """把群缓存行补上等级、权限、生效额度、今日用量与「最后回复」。"""
    gid = str(row.get("group_id") or "")
    lv_id = (ctx["subject_level"].get("group") or {}).get(gid)
    lv = (ctx["levels"].get("group") or {}).get(int(lv_id)) if lv_id else None
    q = _effective_from_chain(ctx["plugin"].quota_chain("group", gid))
    bm = ctx["last_bot"].get(gid) or {}
    return {
        **row,
        "avatar_id": gid,
        "effect": effect_in_scene(ctx["policy"].get(gid), ctx["scene"]) or "inherit",
        "effect_command": effect_in_scene(ctx["cmd_master"].get(gid), ctx["scene"]) or "inherit",
        "level_id": int(lv_id) if lv_id else None,
        "level_name": (lv or {}).get("name") or "",
        "quota": q,
        "quota_limit": q["limit"] or None,
        "quota_used": q["used"] if q["limit"] else None,
        "quota_mode": q["mode"] or None,
        "quota_layer": q["layer"],
        "today_tokens": int(ctx["today"].get(gid, 0)),
        "member_cached": int((ctx.get("member_counts") or {}).get(gid, 0)),
        "last_bot_ts": int(bm.get("ts") or 0),
        "last_bot_kind": str(bm.get("kind") or ""),
        "last_bot_command": str(bm.get("command") or ""),
        "last_bot_preview": str(bm.get("preview") or ""),
    }


def _fmt_friend(row: dict, ctx: dict[str, Any]) -> dict:
    """把好友缓存行补上等级、权限、生效额度、今日用量与「最后回复」。"""
    uin = str(row.get("uin") or "")
    # 等级分场景（v8 / v1.2.2）：私聊用「私聊等级」（好友等级那一套），
    # 群聊用「群聊等级」（限额页里 kind=group 的那套）。群聊没配 = 走群的档位，
    # 所以**不回落**到私聊等级（v1.2.2 的语义）。
    base_id = (ctx["subject_level"].get("user") or {}).get(uin)
    group_id = (ctx.get("subject_level_group") or {}).get(uin)
    scene_is_group = ctx["scene"] == "group"
    lv_id = group_id if scene_is_group else base_id
    lv = None
    if lv_id:
        lid = int(lv_id)
        # 名字按对应 kind 的索引查；查不到再试另一个索引 —— level_id 全局唯一，
        # 这样既支持「群聊等级填群聊等级」，也兼容 v1.2.x 时代填好友等级的旧数据。
        user_lv = ctx["levels"].get("user") or {}
        group_lv = ctx["levels"].get("group") or {}
        first, second = (group_lv, user_lv) if scene_is_group else (user_lv, group_lv)
        lv = first.get(lid) or second.get(lid)
    q = _effective_from_chain(ctx["plugin"].quota_chain("user", uin))
    bm = ctx["last_bot"].get(uin) or {}
    pol = ctx["policy"].get(uin) or {}
    cm = ctx["cmd_master"].get(uin) or {}
    return {
        **row,
        "display_name": (row.get("remark") or "").strip() or (row.get("nickname") or "").strip() or uin,
        "avatar": f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100",
        "avatar_id": uin,
        "effect": effect_in_scene(pol, ctx["scene"]) or "inherit",
        "effect_command": effect_in_scene(cm, ctx["scene"]) or "inherit",
        # 分场景显式值（v1.2.3）：列表把私聊 / 群聊两套权限平铺成四列，不再靠场景切换
        "effect_private": effect_in_scene(pol, "private") or "inherit",
        "effect_group": effect_in_scene(pol, "group") or "inherit",
        "effect_cmd_private": effect_in_scene(cm, "private") or "inherit",
        "effect_cmd_group": effect_in_scene(cm, "group") or "inherit",
        "level_id": int(lv_id) if lv_id else None,
        # 两个场景的原始配置（前端按场景编辑时要区分「跟随私聊」和「未分组」）
        "level_id_base": int(base_id) if base_id else None,
        "level_id_group": int(group_id) if group_id else None,
        "level_name": (lv or {}).get("name") or "",
        "quota": q,
        "quota_limit": q["limit"] or None,
        "quota_used": q["used"] if q["limit"] else None,
        "quota_mode": q["mode"] or None,
        "quota_layer": q["layer"],
        "today_tokens": int(ctx["today"].get(uin, 0)),
        "last_bot_ts": int(bm.get("ts") or 0),
        "last_bot_kind": str(bm.get("kind") or ""),
        "last_bot_command": str(bm.get("command") or ""),
        "last_bot_preview": str(bm.get("preview") or ""),
    }


def _today_bounds() -> tuple[int, int]:
    """今日（本地 0 点 → 现在）的时间戳区间。"""
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


# ---------------------------------------------------------------------- #
# 处理函数（第一个参数固定为插件实例）
# ---------------------------------------------------------------------- #
async def h_ping(plugin) -> dict:
    """健康检查：前端据此判断后端是否就绪，并显示版本号。"""
    return ok(
        {
            "plugin": "astrbot_plugin_moe_permission",
            "version": getattr(plugin, "plugin_version", "unknown"),
            "db_ready": bool(plugin.store and plugin.store.ready),
            "data_dir": str(getattr(plugin, "data_dir", "")),
            "db_path": str(getattr(plugin, "store", None).db_path) if plugin.store else "",
            "server_time": int(time.time()),
            # 同步状态（控制台顶部据此提示「尚未同步 / 同步失败」）
            "sync": {
                "last_at": int(getattr(plugin.scheduler, "last_at", 0) or 0),
                "running": bool(getattr(plugin.scheduler, "running", False)),
                "ok": bool((getattr(plugin.scheduler, "last_result", {}) or {}).get("ok")),
                "error": str((getattr(plugin.scheduler, "last_result", {}) or {}).get("error") or ""),
            },
            # 内存里已加载的规则数量（便于确认改动是否真的生效）
            "rules": {
                "effect_users": len(getattr(plugin, "_effect_user", {}) or {}),
                "effect_groups": len(getattr(plugin, "_effect_group", {}) or {}),
                "effect_members": len(getattr(plugin, "_effect_member", {}) or {}),
                "levels": len(getattr(plugin, "_level_effect", {}) or {}),
                "leveled_users": len(getattr(plugin, "_subject_level_user", {}) or {}),
                "leveled_groups": len(getattr(plugin, "_subject_level_group", {}) or {}),
                "limits": sum(len(v) for v in (getattr(plugin, "_limits", {}) or {}).values()),
                "usage_users": len(getattr(plugin, "_usage_user", {}) or {}),
                "usage_groups": len(getattr(plugin, "_usage_group", {}) or {}),
                "usage_members": len(getattr(plugin, "_usage_member", {}) or {}),
            },
            # 头像缓存概况（抓了多少、占多少、最旧的是什么时候）
            "avatars": (
                plugin.avatars.stats() if getattr(plugin, "avatars", None) is not None else {}
            ),
            # 指令权限概况（有多少条指令、配了多少条规则、闸门优先级）
            "commands": {
                "enabled": bool(plugin._cfg("command_guard_enabled", True)),
                "default_effect": plugin.gate.default_command_effect(),
                "priority": int(plugin._cfg("command_guard_priority", 1000) or 1000),
                "rules": sum(len(s) for s in (getattr(plugin, "_cmd_policy", {}) or {}).values()),
            },
            # 模型路由概况（等级模型是否在生效、哪些提供商被熔断了）
            "model_route": {
                "enabled": bool(plugin._cfg("model_route_enabled", True)),
                "levels": len(getattr(plugin, "_level_route", {}) or {}),
                "routed_sessions": len(getattr(plugin, "_last_route", {}) or {}),
                "circuit": (plugin.circuit.snapshot() if getattr(plugin, "circuit", None) else {}),
            },
            # 用户自助切换模型（v9/v10）：总开关、放开了切换的分级数、卡片字体是否就位
            "model_switch": {
            "enabled": bool(plugin._cfg("model_switch_enabled", True)),
            "open_levels": len(getattr(plugin, "_level_switch", {}) or {}),
            "switch_levels": sum(
                1 for v in (getattr(plugin, "_level_switch_enabled", {}) or {}).values() if v
            ),
            # 切换 = 改专属模型（v1.3.12）：这里统计配了专属模型的用户数 + 群数
            "switched_users": (
                len(getattr(plugin, "_subject_model", {}) or {})
                + len(getattr(plugin, "_subject_model_group", {}) or {})
            ),
                "sessions": (
                    plugin.switcher.sessions() if getattr(plugin, "switcher", None) else 0
                ),
                "card": model_card.assets_info(),
                # 卡片自动撤回：时间就是「切换选择有效期」那个数字；这里是实际生效值
                "recall_sec": (
                    plugin.switcher.recall_sec() if getattr(plugin, "switcher", None) else 0
                ),
                # 撤回的计数（排上 / 撤掉 / 失败、当前待撤）
                "recall": (
                    plugin.recaller.stats() if getattr(plugin, "recaller", None) else {}
                ),
            },
        },
    )


def _fmt_member(row: dict, ctx: dict[str, Any]) -> dict:
    """把群成员缓存行补上权限状态、生效额度与本群今日用量（供「群成员」抽屉直接渲染）。"""
    gid = str(row.get("group_id") or "")
    uid = str(row.get("user_id") or "")
    sid = member_scope_id(gid, uid)
    # 额度档位链与闸门同源：命中哪一层（成员专属 / 好友等级 / 群 / 全局）就展示哪一层
    q = _effective_from_chain(ctx["plugin"].quota_chain("member", sid))
    return {
        **row,
        "avatar_id": uid,
        "scope_id": sid,
        "display_name": (row.get("card") or "").strip()
        or (row.get("nickname") or "").strip()
        or uid,
        "effect": effect_in_scene(ctx["policy"].get(sid), "group") or "inherit",
        "effect_command": effect_in_scene(ctx["cmd_master"].get(sid), "group") or "inherit",
        "quota": q,
        "quota_limit": q["limit"] or None,
        "quota_used": q["used"] if q["limit"] else None,
        "quota_mode": q["mode"] or None,
        "quota_layer": q["layer"],
        "today_tokens": int(ctx["today"].get(uid, 0)),
    }


def _filter_level(rows: list[dict], level_filter: str) -> list[dict]:
    """按等级过滤行（``level_filter`` 为空 = 不过滤）。

    - ``0``  = 未分组（没有归到任何等级的行，其 ``level_id`` 为 ``None``）；
    - ``>0`` = 该等级的 id；
    - 非法值当作「不过滤」，避免手动改 URL 时整个列表变空。
    """
    text = str(level_filter or "").strip()
    if text == "":
        return rows
    try:
        want = int(text)
    except (TypeError, ValueError):
        return rows
    return [r for r in rows if int(r.get("level_id") or 0) == want]


async def _list_ctx(plugin, kind: str, scene: str = "private") -> dict[str, Any]:
    """装配列表页要用的映射。

    等级 / 限额 / 用量 / 归级 / 权限这几张都直接取插件**内存里已有的快照**
    （闸门热路径的同一份数据，reload_rules 时整体重建），避免每翻一页都做十来次查询，
    也保证「界面上看到的」与「闸门实际用的」完全一致。只有等级名与最后回复需要查库。

    ``scene`` 决定权限列展示哪一套规则（好友这一层分私聊 / 群聊）。
    """
    store = plugin.store
    from_ts, to_ts = _today_bounds()
    return {
        "plugin": plugin,
        "scene": scene,
        "levels": _levels_index(await store.list_levels()),
        "limits": getattr(plugin, "_limits", {}) or {},
        "usage": {
            "user": getattr(plugin, "_usage_user", {}) or {},
            "group": getattr(plugin, "_usage_group", {}) or {},
        },
        "subject_level": {
            "user": getattr(plugin, "_subject_level_user", {}) or {},
            "group": getattr(plugin, "_subject_level_group", {}) or {},
        },
        # 好友的「群聊专属」等级（v8）：好友列表按场景显示/编辑等级要用
        "subject_level_group": getattr(plugin, "_subject_level_user_group", {}) or {},
        "policy": (
            getattr(plugin, "_effect_user", {}) or {}
            if kind == "user"
            else getattr(plugin, "_effect_group", {}) or {}
        ),
        # 对象级「指令权限」（feature=command）的显式值：{scope_id: effect}
        "cmd_master": dict((getattr(plugin, "_cmd_master", {}) or {}).get(kind) or {}),
        "last_bot": await store.bot_message_map(kind),
        "today": await store.usage_sums("sender_id" if kind == "user" else "group_id", from_ts, to_ts),
        # 每个群已缓存多少成员（没同步过的群 = 0，页面上据此提示「先同步成员」）
        "member_counts": await store.group_member_counts(),
    }


async def h_get_config(plugin) -> dict:
    """读取当前生效配置（含 schema 默认值）。"""
    schema = _load_schema()
    cfg = getattr(plugin, "config", None) or {}
    items: dict[str, Any] = {}
    for key, spec in schema.items():
        default = spec.get("default")
        try:
            val = cfg.get(key, default) if hasattr(cfg, "get") else default
        except Exception:
            val = default
        items[key] = val if val is not None else default
    return ok({"items": items, "schema": schema})


async def h_set_config(plugin) -> dict:
    """写入配置（只接受 schema 中存在的键，按 schema 类型转换并做选项校验）。"""
    body = await _payload()
    raw = body.get("items")
    if not isinstance(raw, dict):
        return err("items 必须是对象 {key: value}")

    schema = _load_schema()
    cfg = getattr(plugin, "config", None)
    if cfg is None or not hasattr(cfg, "__setitem__"):
        return err("插件配置不可写（self.config 缺失）")

    changed: list[str] = []
    for key, value in raw.items():
        spec = schema.get(key)
        if not spec:
            continue
        t = spec.get("type")
        try:
            if t == "bool":
                casted: Any = _as_bool(value, default=bool(spec.get("default")))
            elif t == "int":
                casted = int(value)
            elif t == "float":
                casted = float(value)
            else:
                casted = "" if value is None else str(value)
            options = spec.get("options")
            if options and casted not in options:
                return err(f"{key} 的取值必须是 {options} 之一（收到 {casted!r}）")
        except Exception as e:
            return err(f"{key} 类型转换失败: {e}")
        cfg[key] = casted
        changed.append(key)

    if not changed:
        return err("没有可写入的配置项（键名不在 _conf_schema.json 中）")
    try:
        if hasattr(cfg, "save_config"):
            cfg.save_config()
    except Exception as e:
        logger.warning(f"[UserGateway] save_config 失败: {e}")
        return err(f"配置已改内存但落盘失败: {e}")
    return ok({"changed": changed})


async def h_overview(plugin) -> dict:
    """总览统计：区间总量 + 趋势 + 榜单 + 模型占比 + 拒绝原因 + bot 回复类型分布。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    from_ts, to_ts = _range_bounds(_q("range", "7d"), _q("from"), _q("to"))
    data = await plugin.store.summary(from_ts, to_ts)
    data["range_key"] = _q("range", "7d")
    # 榜单补展示名（备注 / 昵称 / 群名）——只有 id 看不出是谁
    try:
        scopes = data.get("top_scopes") or []
        names = await plugin.store.subject_name_map(
            [r.get("scope_id") for r in scopes if r.get("scope_type") == "user"],
            [r.get("scope_id") for r in scopes if r.get("scope_type") == "group"],
        )
        for r in scopes:
            r["name"] = names.get(str(r.get("scope_id") or ""), "")
    except Exception as e:
        logger.warning(f"[UserGateway] 补榜单展示名失败（忽略）: {e}")
    try:
        data["bot"] = await plugin.store.bot_message_summary(from_ts, to_ts)
    except Exception as e:
        logger.warning(f"[UserGateway] 汇总最后消息失败（忽略）: {e}")
        data["bot"] = {"kinds": [], "sessions": 0, "latest_ts": 0}
    return ok(data)


async def h_friends(plugin) -> dict:
    """好友列表（含头像、等级、权限、生效额度、今日用量、最后回复）。

    排序：``last``（**默认**，最近有 bot 回复的排前面）/ ``active``（按同步时间）/
    ``usage``（今日 token）/ ``name`` / ``qq`` / ``level``（按等级名）。
    除 ``active`` 外都需要**先取全量再排序分页**，好友量级通常只有数百，
    整表取回可接受（上限 5000 条防爆）。

    过滤：``effect``（LLM 权限）/ ``effect_command``（指令权限）/ ``level_id``
    （等级：空 = 全部，``0`` = 未分组，``>0`` = 该等级 id）。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    sort = _q("sort", "last") or "last"
    page = _qi("page", 1, 1, 10**6)
    size = _qi("size", 50, 1, 500)
    platform = _q("platform") or None
    kw = _q("q")
    effect_filter = _q("effect")
    # LLM 权限与指令权限是两套规则，所以支持分别过滤（都命中才算通过）
    cmd_filter = _q("effect_command")
    level_filter = _q("level_id")

    full = (
        sort in ("usage", "name", "qq", "level", "last")
        or effect_filter in ("allow", "deny", "inherit")
        or cmd_filter in ("allow", "deny", "inherit")
        or level_filter != ""
    )
    if full:
        res = await plugin.store.list_friends(platform_id=platform, keyword=kw, limit=5000, offset=0)
    else:
        res = await plugin.store.list_friends(
            platform_id=platform, keyword=kw, limit=size, offset=(page - 1) * size
        )

    ctx = await _list_ctx(plugin, "user", _scene_arg("private"))
    rows = [_fmt_friend(r, ctx) for r in res["rows"]]
    total = res["total"]

    if effect_filter in ("allow", "deny", "inherit"):
        rows = [r for r in rows if str(r.get("effect") or "inherit") == effect_filter]
    if cmd_filter in ("allow", "deny", "inherit"):
        rows = [r for r in rows if str(r.get("effect_command") or "inherit") == cmd_filter]
    rows = _filter_level(rows, level_filter)

    if sort == "usage":
        rows.sort(key=lambda r: int(r.get("today_tokens") or 0), reverse=True)
    elif sort == "name":
        rows.sort(key=lambda r: str(r.get("display_name") or ""))
    elif sort == "qq":
        rows.sort(key=lambda r: str(r.get("uin") or ""))
    elif sort == "level":
        rows.sort(key=lambda r: (str(r.get("level_name") or "~"), str(r.get("display_name") or "")))
    elif sort == "last":
        rows.sort(key=lambda r: int(r.get("last_bot_ts") or 0), reverse=True)

    if full:
        total = len(rows)
        rows = rows[(page - 1) * size : page * size]

    return ok({"total": total, "rows": rows, "page": page, "size": size, "sort": sort})


async def h_groups(plugin) -> dict:
    """群列表（含群头像、等级、权限、生效额度、今日用量、最后回复）。排序与过滤语义同好友列表。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    sort = _q("sort", "last") or "last"
    page = _qi("page", 1, 1, 10**6)
    size = _qi("size", 50, 1, 500)
    platform = _q("platform") or None
    kw = _q("q")
    effect_filter = _q("effect")
    cmd_filter = _q("effect_command")
    level_filter = _q("level_id")

    full = (
        sort in ("usage", "name", "group", "size", "level", "last")
        or effect_filter in ("allow", "deny", "inherit")
        or cmd_filter in ("allow", "deny", "inherit")
        or level_filter != ""
    )
    if full:
        res = await plugin.store.list_groups(platform_id=platform, keyword=kw, limit=5000, offset=0)
    else:
        res = await plugin.store.list_groups(
            platform_id=platform, keyword=kw, limit=size, offset=(page - 1) * size
        )

    # 群页的规则（群专属 / 群等级）本来就只在群里生效，场景固定为群聊
    ctx = await _list_ctx(plugin, "group", "group")
    rows = [_fmt_group(r, ctx) for r in res["rows"]]
    total = res["total"]

    if effect_filter in ("allow", "deny", "inherit"):
        rows = [r for r in rows if str(r.get("effect") or "inherit") == effect_filter]
    if cmd_filter in ("allow", "deny", "inherit"):
        rows = [r for r in rows if str(r.get("effect_command") or "inherit") == cmd_filter]
    rows = _filter_level(rows, level_filter)

    if sort == "usage":
        rows.sort(key=lambda r: int(r.get("today_tokens") or 0), reverse=True)
    elif sort == "name":
        rows.sort(key=lambda r: str(r.get("name") or ""))
    elif sort == "group":
        rows.sort(key=lambda r: str(r.get("group_id") or ""))
    elif sort == "size":
        rows.sort(key=lambda r: int(r.get("member_count") or 0), reverse=True)
    elif sort == "level":
        rows.sort(key=lambda r: (str(r.get("level_name") or "~"), str(r.get("name") or "")))
    elif sort == "last":
        rows.sort(key=lambda r: int(r.get("last_bot_ts") or 0), reverse=True)

    if full:
        total = len(rows)
        rows = rows[(page - 1) * size : page * size]

    return ok({"total": total, "rows": rows, "page": page, "size": size, "sort": sort})


async def h_sync(plugin) -> dict:
    """手动同步好友与群列表（与定时同步共用同一条路径，内部有互斥锁）。"""
    try:
        res = await plugin.scheduler.sync_once()
    except Exception as e:
        logger.exception("[UserGateway] 手动同步异常")
        return err(f"同步失败: {e}")
    if not res.get("ok"):
        return err(str(res.get("error") or "同步失败"))
    return ok(res)


async def h_group_members(plugin) -> dict:
    """某个群的成员列表（含权限状态与本群今日用量）。

    数据来自本地缓存（``group_member`` 表），**不会**在这里打协议端 ——
    成员列表很重，同步走 ``POST /group/members/sync``，由用户显式触发。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    gid = _q("group_id").strip()
    if not gid:
        return err("缺少 group_id")
    page = _qi("page", 1, 1, 10**6)
    size = _qi("size", 200, 1, 1000)
    res = await plugin.store.list_group_members(
        gid, keyword=_q("q"), limit=size, offset=(page - 1) * size
    )
    # 权限直接取插件内存里的快照（与闸门同一份），保证「界面看到的」就是「实际用的」
    policy = (getattr(plugin, "_effect_member", {}) or {})
    cmd_master = ((getattr(plugin, "_cmd_master", {}) or {}).get("member") or {})
    from_ts, to_ts = _today_bounds()
    try:
        usage = await plugin.store.usage_sums_in_group(gid, from_ts, to_ts)
    except Exception as e:
        logger.warning(f"[UserGateway] 统计群内成员用量失败（忽略）: {e}")
        usage = {}
    ctx = {"policy": policy, "cmd_master": cmd_master, "today": usage, "plugin": plugin}
    rows = [_fmt_member(r, ctx) for r in res.get("rows", [])]
    # 同步时间取该群全量成员的最大 updated_at（分页结果只反映当前页，会偏旧）
    try:
        synced_at = await plugin.store.group_members_synced_at(gid)
    except Exception as e:
        logger.debug(f"[UserGateway] 读取成员同步时间失败（忽略）: {e}")
        synced_at = max([int(r.get("updated_at") or 0) for r in rows], default=0)
    return ok(
        {
            "total": res.get("total", 0),
            "rows": rows,
            "page": page,
            "size": size,
            "group_id": gid,
            "synced_at": synced_at,
        },
    )


async def h_sync_group_members(plugin) -> dict:
    """从协议端同步某个群的成员列表（``get_group_member_list``）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    gid = str(body.get("group_id") or "").strip()
    if not gid:
        return err("缺少 group_id")
    timeout = float(plugin._cfg("sync_timeout_sec", 15) or 15)
    res = await sync_group_members(
        plugin.context,
        plugin.store,
        gid,
        str(body.get("platform_id") or "").strip(),
        timeout,
    )
    if not res.get("ok"):
        return err(str(res.get("error") or "同步失败"))
    await plugin.store.log_audit(
        "console", "sync_group_members", json.dumps({"group_id": gid, "n": res.get("count")}, ensure_ascii=False)
    )
    return ok(res)


async def h_subject(plugin) -> dict:
    """单个对象（好友 / 群）的详情：基础信息 + 权限 + 额度 + 区间用量与曲线。

    ``type=user`` 按 sender_id 聚合（含该用户在任意群里的用量）；
    ``type=group`` 按 group_id 聚合。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    subject_type = _q("type", "user") or "user"
    subject_id = _q("id")
    if subject_type not in ("user", "group"):
        return err("type 必须是 user 或 group")
    if not subject_id:
        return err("缺少 id")

    days = _qi("days", 7, 1, 90)
    now = int(time.time())
    from_ts, to_ts = now - days * 86400, now

    info: dict | None = None
    try:
        info = (
            await plugin.store.get_group(subject_id)
            if subject_type == "group"
            else await plugin.store.get_friend(subject_id)
        )
    except Exception as e:
        logger.warning(f"[UserGateway] 读取对象缓存失败（忽略）: {e}")

    scene = _scene_arg()
    # 好友这一层的权限分场景：抽屉里展示的是「当前场景」下的配置与结论
    effect = await plugin.store.resolve_policy(subject_type, subject_id, "llm", scene)
    # 好友详情：把两套场景的权限显式值一起带回（抽屉平铺展示，不再靠场景切换）
    effect_private = effect_group = None
    cmd_private = cmd_group = None
    if subject_type == "user":
        effect_private = await plugin.store.get_effect("user", subject_id, "llm", "private")
        effect_group = await plugin.store.get_effect("user", subject_id, "llm", "group")
        cmd_private = plugin.command_master_of("user", subject_id, "private")
        cmd_group = plugin.command_master_of("user", subject_id, "group")
    quotas = await plugin.store.list_quotas_of(subject_type, subject_id)
    stats = await plugin.store.subject_stats(subject_type, subject_id, from_ts, to_ts)

    from_ts_today, to_ts_today = _today_bounds()
    today = await plugin.store.subject_stats(subject_type, subject_id, from_ts_today, to_ts_today)

    # 近期流水必须与上面的 stats 同口径：好友按 sender_id、群按 group_id。
    # 群一旦漏掉过滤条件，就会把全库最近的流水都搬进来（表现为「流水和该群不符」）。
    recent = await plugin.store.query_usage(
        from_ts=from_ts,
        to_ts=to_ts,
        sender_id=subject_id if subject_type == "user" else None,
        group_id=subject_id if subject_type == "group" else None,
        limit=50,
        offset=0,
    )

    # 等级 / 额度档位链 / 用量计数 / 最后回复
    # 好友的等级分场景（v8）：level_id = 私聊，level_id_group = 群聊专属（None = 跟随私聊）
    if subject_type == "user":
        level_id, level_id_group = await plugin.store.get_user_level_scene(subject_id)
    else:
        level_id = await plugin.store.get_subject_level("group", subject_id)
        level_id_group = None
    level = await plugin.store.get_level(level_id) if level_id else None
    level_quotas = await plugin.store.list_quotas_of("level", str(level_id)) if level_id else []
    chain = plugin.quota_chain(subject_type, subject_id)
    bot = await plugin.store.get_bot_message(subject_type, subject_id)
    usage = await plugin.store.get_usage(subject_type, subject_id)
    model_route = plugin.model_route_of(subject_type, subject_id)
    command_master = plugin.command_master_of(subject_type, subject_id, scene)

    return ok(
        {
            "type": subject_type,
            "id": subject_id,
            "info": info,
            "effect": effect or "inherit",
            "effect_private": effect_private,
            "effect_group": effect_group,
            "cmd_private": cmd_private,
            "cmd_group": cmd_group,
            "quotas": quotas,
            "level_id": level_id,
            "level_id_group": level_id_group,
            "level": level,
            "level_quotas": level_quotas,
            "model_route": model_route,
            "command_master": command_master,
            "quota_chain": chain,
            "quota": _effective_from_chain(chain),
            "usage": usage,
            "bot": bot,
            "days": days,
            "stats": stats,
            "today": today.get("totals", {}),
            "recent": recent.get("rows", []),
        },
    )


async def h_get_policy(plugin) -> dict:
    """读取权限规则。

    ``?feature=llm``（默认）或 ``?feature=command:<指令名>``；``scope_type`` 可再过滤。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    rows = await plugin.store.list_policies(
        scope_type=_q("scope_type") or None,
        feature=_q("feature") or "llm",
        scene=_q("scene") if _q("scene") else None,
    )
    return ok({"items": rows})


async def h_set_policy(plugin) -> dict:
    """写入权限规则。body: ``{scope_type, scope_id, effect, feature?, scene?}`` 或 ``{items: [...]}``。

    - ``feature='llm'``：LLM 对话权限（``scope_type`` 只允许 ``user`` / ``group``）；
    - ``feature='command'``：对象级指令总权限；
    - ``feature='command:<指令名>'``：某条指令的权限，额外支持 ``scope_type='global'``
      （``scope_id`` 固定 ``*``），即「这条指令全局禁用/放行」；
    - ``scene='private' | 'group'``：只对 ``scope_type='user'`` 有意义 ——
      好友这一层分私聊与群聊，例如「私聊禁用某人，但他在群里照用」。
      不传 = 通用规则（两个场景都生效）。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    items = body.get("items")
    if not isinstance(items, list):
        items = [body]

    applied = []
    # 两遍处理（v1.0.0）：第一遍只做校验与规整，任何一条不合法都直接报错返回、
    # 不落库 —— 否则批量中途失败会出现「前几条已写库、内存规则又没刷新」的中间态。
    specs: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        scope_type = str(it.get("scope_type") or "").strip()
        scope_id = str(it.get("scope_id") or "").strip()
        effect = str(it.get("effect") or "").strip()
        feature = str(it.get("feature") or "llm").strip() or "llm"
        # 场景只对「好友」这一层有意义：群 / 全局规则本来就只在（或不分）群里生效
        scene = str(it.get("scene") or "").strip()
        if scope_type != "user":
            scene = ""
        if scene not in ("", "private", "group"):
            return err("scene 必须是 private / group（留空 = 两个场景都生效）")
        if scope_type not in ("user", "group", "member", "global"):
            return err("scope_type 必须是 user / group / member / global")
        if scope_type == "global":
            if feature == "llm":
                return err("LLM 权限的全局默认请在「配置」页用「全局默认策略」设置")
            scope_id = "*"
        if scope_type == "member":
            # 群成员规则：scope_id 必须是「群号:QQ」，且只在群聊里参与判定
            gid, uid = parse_member_scope_id(scope_id)
            if not (gid and uid):
                return err("群成员规则的 scope_id 必须是「群号:QQ」")
            scope_id = member_scope_id(gid, uid)
        if not scope_id:
            return err("scope_id 不能为空")
        if effect not in ("allow", "deny", "inherit"):
            return err("effect 必须是 allow / deny / inherit")
        if feature not in ("llm", "command") and not feature.startswith("command:"):
            return err("feature 必须是 llm / command（对象级指令权限）/ command:<指令名>")
        if feature.startswith("command:") and not feature[len("command:"):].strip():
            return err("command: 后面必须带指令名")
        specs.append(
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "effect": effect,
                "feature": feature,
                "scene": scene,
            }
        )

    for s in specs:
        await plugin.store.set_policy(
            s["scope_type"], s["scope_id"], s["effect"], feature=s["feature"], scene=s["scene"]
        )
        applied.append(s)

    if not applied:
        return err("没有可应用的规则")
    await plugin.store.log_audit("console", "set_policy", json.dumps(applied, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"applied": applied})


async def h_get_quota(plugin) -> dict:
    """读取限额配置。

    返回的每行会补上 ``used_tokens``：
    - ``scope_type=user|group``（对象专属）→ 该对象自己的用量计数；
    - ``scope_type=level|global``（模板）→ ``used_tokens=None``，
      因为模板是「每个对象各自的上限」，没有单一的合计值（合计口径看总览页）。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    st = _q("scope_type") or None
    sid = _q("scope_id") or None
    # 传了 scope_id 就只取这个对象的（成员抽屉设置额度时读初值用）
    rows = (
        await plugin.store.list_quotas_of(st, sid)
        if (st and sid)
        else await plugin.store.list_quotas(st)
    )
    usage = {
        "user": getattr(plugin, "_usage_user", {}) or {},
        "group": getattr(plugin, "_usage_group", {}) or {},
        "member": getattr(plugin, "_usage_member", {}) or {},
    }
    out = []
    for row in rows:
        st = str(row.get("scope_type") or "")
        sid = str(row.get("scope_id") or "")
        period = str(row.get("period") or "")
        used = None
        if st in ("user", "group", "member"):
            used = int(((usage.get(st) or {}).get(sid) or {}).get(period, {}).get("used_tokens") or 0)
        out.append({**row, "used_tokens": used})
    return ok({"items": out})


async def h_set_quota(plugin) -> dict:
    """写入限额。body: ``{scope_type, scope_id, period, limit_tokens, mode?}`` 或 ``{items: [...]}``。

    ``scope_type`` 支持 ``user`` / ``group``（对象专属）、``level``（等级模板，
    ``scope_id`` 传等级 id）、``global``（全局模板，``scope_id`` 为 ``private`` / ``group``
    —— v1.4.0 起私聊与群聊各一份；旧版固定 ``*`` 的写法不再收）。

    ⚠️ ``limit_tokens`` 的语义（见 ``gate.check_quota`` 的说明）：

    - ``0`` = **明确不限**，该档位仍然「占位」，更粗的档位不再生效
      （所以「VIP 等级 = 不限」不会被全局额度反手拦掉）；
    - ``null`` 或 ``delete=true`` = **删掉该周期**，该档位不再占位。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    items = body.get("items")
    if not isinstance(items, list):
        items = [body]

    applied = []
    # 两遍处理（v1.0.0）：先全量校验并规整，任何一条不合法直接报错、不落库，
    # 避免「前几条已写入、内存规则没刷新」的中间态。
    specs: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        scope_type = str(it.get("scope_type") or "").strip()
        scope_id = str(it.get("scope_id") or "").strip()
        period = str(it.get("period") or "day").strip()
        mode = str(it.get("mode") or "enforce").strip()
        if scope_type not in ("user", "group", "member", "level", "global"):
            return err("scope_type 必须是 user / group / member / level / global")
        if scope_type == "global":
            # v1.4.0：全局默认额度分私聊 / 群聊两份（scope_id = private / group）
            if scope_id not in ("private", "group"):
                return err("全局额度的 scope_id 必须是 private / group")
        if scope_type == "member":
            # 按成员设额度：scope_id 是「群号:QQ」，判定时用的是他在这个群里的用量
            gid, uid = parse_member_scope_id(scope_id)
            if not (gid and uid):
                return err("群成员额度的 scope_id 必须是「群号:QQ」")
            scope_id = member_scope_id(gid, uid)
        if not scope_id:
            return err("scope_id 不能为空")
        if period not in ("day", "month", "total"):
            return err("period 必须是 day / month / total")
        if mode not in ("enforce", "observe"):
            return err("mode 必须是 enforce / observe")

        raw_limit = it.get("limit_tokens")
        if _as_bool(it.get("delete"), default=False) or raw_limit is None:
            specs.append({"scope_type": scope_type, "scope_id": scope_id, "period": period, "delete": True})
            continue
        try:
            limit_tokens = int(raw_limit)
        except Exception:
            return err("limit_tokens 必须是整数（0 表示不限；null / delete=true 表示删除）")
        if limit_tokens < 0:
            return err("limit_tokens 不能为负")
        specs.append(
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "period": period,
                "delete": False,
                "limit_tokens": limit_tokens,
                "mode": mode,
            }
        )

    for s in specs:
        if s.pop("delete"):
            await plugin.store.delete_quota(s["scope_type"], s["scope_id"], s["period"])
            applied.append({"scope_type": s["scope_type"], "scope_id": s["scope_id"], "period": s["period"], "deleted": True})
            continue
        await plugin.store.upsert_quota(
            s["scope_type"],
            s["scope_id"],
            s["period"],
            s["limit_tokens"],
            mode=s["mode"],
            reset_at=plugin.quota_reset_at(s["period"]),
        )
        applied.append(
            {"scope_type": s["scope_type"], "scope_id": s["scope_id"], "period": s["period"], "limit_tokens": s["limit_tokens"], "mode": s["mode"]},
        )

    if not applied:
        return err("没有可应用的额度")
    await plugin.store.log_audit("console", "set_quota", json.dumps(applied, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"applied": applied})


async def h_reset_quota(plugin) -> dict:
    """清零已用量（作用于 usage_counter）。

    body: ``{scope_type?, scope_id?, period?}`` —— 全空表示**把所有对象的用量清零**。
    注意：只会动「用量计数」，不会删掉任何额度配置。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    scope_type = str(body.get("scope_type") or "").strip()
    if scope_type and scope_type not in ("user", "group", "member"):
        return err("scope_type 必须是 user / group / member（留空则全部清零）")
    n = await plugin.store.reset_used(
        scope_type=scope_type or None,
        scope_id=str(body.get("scope_id") or "").strip() or None,
        period=str(body.get("period") or "").strip() or None,
    )
    await plugin.store.log_audit("console", "reset_quota", json.dumps(body, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"reset": n})


async def h_usage(plugin) -> dict:
    """用量/事件明细（分页）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    from_ts, to_ts = _range_bounds(_q("range", "7d"), _q("from"), _q("to"))
    res = await plugin.store.query_usage(
        from_ts=from_ts,
        to_ts=to_ts,
        scope_type=_q("scope_type") or None,
        scope_id=_q("scope_id") or None,
        sender_id=_q("sender_id") or None,
        status=_q("status") or None,
        kind=_q("kind") or None,
        limit=_qi("size", 50, 1, 500),
        offset=_qi("page", 1, 1, 10**6) - 1,
    )
    res["page"] = _qi("page", 1, 1, 10**6)
    res["size"] = _qi("size", 50, 1, 500)
    # 明细行补「对象」的展示名（一页最多几百行，按 id 批量查两次缓存表即可）
    try:
        rows = res.get("rows") or []
        names = await plugin.store.subject_name_map(
            [r.get("scope_id") for r in rows if r.get("scope_type") == "user"],
            [r.get("scope_id") for r in rows if r.get("scope_type") == "group"],
        )
        for r in rows:
            r["scope_name"] = names.get(str(r.get("scope_id") or ""), "")
    except Exception as e:
        logger.warning(f"[UserGateway] 补明细展示名失败（忽略）: {e}")
    return ok(res)


async def h_usage_export(plugin) -> dict:
    """导出用量明细为 CSV。

    走桥接（``apiGet``）而不是直接下载：控制台页面跑在 sandbox iframe 里，
    拿不到可下载的 URL，所以后端返回 ``{filename, content}``，前端用 Blob 落地。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    from_ts, to_ts = _range_bounds(_q("range", "7d"), _q("from"), _q("to"))
    try:
        content = await plugin.store.export_usage_csv(
            from_ts=from_ts,
            to_ts=to_ts,
            scope_type=_q("scope_type") or None,
            scope_id=_q("scope_id") or None,
            sender_id=_q("sender_id") or None,
            status=_q("status") or None,
            kind=_q("kind") or None,
        )
    except Exception as e:
        logger.error(f"[UserGateway] 导出明细失败: {e}")
        return err(f"导出失败：{e}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return ok({"filename": f"user_gateway_usage_{stamp}.csv", "content": content})


async def h_stats_commands(plugin) -> dict:
    """指令维度统计：最常触发 / 最常被拦 Top N + 时段热力图（24h × 7d）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    from_ts, to_ts = _range_bounds(_q("range", "7d"), _q("from"), _q("to"))
    limit = _qi("limit", 10, 1, 50)
    top_ok = await plugin.store.top_commands(from_ts, to_ts, status="ok", limit=limit)
    top_denied = await plugin.store.top_commands(from_ts, to_ts, status="denied", limit=limit)
    heat = await plugin.store.hour_heatmap(from_ts, to_ts, kind=_q("heat_kind", "llm") or "llm")
    return ok(
        {
            "range": {"from": from_ts, "to": to_ts},
            "top_ok": top_ok,
            "top_denied": top_denied,
            "heatmap": heat,
            "track_enabled": bool(plugin._cfg("track_command_usage", True)),
        },
    )


async def h_export_rules(plugin) -> dict:
    """导出全部**规则类**配置（权限 / 等级 / 归级 / 额度）为一个 JSON 文件。

    不含用量、明细、审计与成员缓存（历史数据跟着规则搬没意义，也会让文件很大）。
    返回 ``{filename, content, meta}``，前端用 Blob 落地（桥接下拿不到下载 URL）。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    try:
        data = await plugin.store.export_rules()
    except Exception as e:
        logger.error(f"[UserGateway] 导出规则失败: {e}")
        return err(f"导出失败：{e}")
    meta = {
        "plugin": "astrbot_plugin_moe_permission",
        "version": str(getattr(plugin, "plugin_version", "") or ""),
        # 结构版本：导入时会拿它和当前版本比，来自更新版本的备份直接拒绝（避免丢字段）
        "schema_version": int(STORE_SCHEMA_VERSION),
        "exported_at": int(time.time()),
        "counts": {k: len(v) for k, v in data.items()},
    }
    payload = {"meta": meta, **data}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return ok(
        {
            "filename": f"moe_permission_rules_{stamp}.json",
            "content": json.dumps(payload, ensure_ascii=False, indent=2),
            "meta": meta,
        }
    )


async def h_import_rules(plugin) -> dict:
    """导入规则。body: ``{mode?: "merge"|"replace", content?: "json 文本", data?: {...}}``。

    - ``merge``（默认）只覆盖文件里出现的条目；
    - ``replace`` 先清空「权限 / 等级 / 归级 / 额度」四类规则再导入（用量与日志不动）。

    导入会**重映射等级 id**（详见 ``store.import_rules`` 的说明）。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    mode = "replace" if str(body.get("mode") or "").lower() == "replace" else "merge"

    data = body.get("data")
    if data is None:
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return err("缺少导入内容（data 或 content）")
        try:
            data = json.loads(content)
        except Exception as e:
            return err(f"JSON 解析失败：{e}")
    if not isinstance(data, dict):
        return err("导入内容必须是一个 JSON 对象")

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    file_ver = int(meta.get("schema_version") or 0)
    if file_ver and file_ver > int(STORE_SCHEMA_VERSION):
        return err(
            f"这份备份来自更新的插件版本（结构 v{file_ver} > 当前 v{STORE_SCHEMA_VERSION}），"
            "请先升级插件再导入，否则可能丢字段。"
        )

    try:
        stats = await plugin.store.import_rules(data, mode=mode)
    except Exception as e:
        logger.exception("[UserGateway] 导入规则失败")
        return err(f"导入失败：{e}")
    await plugin.store.log_audit(
        "console", "import_rules", json.dumps(stats, ensure_ascii=False)
    )
    # 导入后立刻重建内存规则，让闸门用上新配置
    await plugin.reload_rules()
    # 备份可能是从另一台机器带来的，里面的模型 id 未必在本机存在。
    # 刻意**不在导入时过滤**（跨机器恢复时静默丢配置更糟），只把失效引用报告出来，
    # 让用户去「模型引用」区自行决定是否清理。
    report = await _stale_report(plugin)
    stats["stale_models"] = report.get("stale") or []
    stats["stale_total"] = int(report.get("total") or 0)
    return ok(stats)


async def h_audit(plugin) -> dict:
    """管理员操作审计。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    res = await plugin.store.list_audit(
        limit=_qi("size", 50, 1, 200),
        offset=_qi("page", 1, 1, 10**6) - 1,
    )
    return ok(res)


# ---------------------------------------------------------------------- #
# 等级 / 归级
# ---------------------------------------------------------------------- #
async def _stale_report(plugin) -> dict:
    """取失效模型引用报告；任何异常都降级成空报告，不能拖垮控制台页面。"""
    try:
        return await plugin.provider_ref_report()
    except Exception as e:
        logger.warning(f"[UserGateway] 生成失效模型引用报告失败: {e}")
        return {"stale": [], "total": 0, "counts": {}, "configured_known": False,
                "available": [], "total_refs": 0}


async def h_levels(plugin) -> dict:
    """等级列表（含每个等级的额度模板与成员数）。``?kind=user|group`` 可只取一类。

    附带 ``stale``（失效模型引用汇总）与每行的 ``stale_fields``，让等级页能直接
    把「挂着已删除 / 已禁用模型」的字段标出来，不用再发一次请求。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    levels = await plugin.store.list_levels(_q("kind") or None)
    counts = await plugin.store.level_counts()
    limits = _by_scope(await plugin.store.list_quotas("level"))
    report = await _stale_report(plugin)
    dead = provider_refs_mod.stale_ids(report)
    items = []
    for lv in levels:
        lid = int(lv["id"])
        items.append(
            {
                "stale_fields": provider_refs_mod.stale_level_fields(lv, dead),
                "id": lid,
                "kind": str(lv.get("kind") or "user"),
                "name": str(lv.get("name") or ""),
                "description": str(lv.get("description") or ""),
                "effect": str(lv.get("effect") or "inherit"),
                "sort_order": int(lv.get("sort_order") or 0),
                "effect_command": str(lv.get("command_effect") or "inherit"),
                # 群聊场景的那一套（只对 kind='user' 有意义；inherit = 跟随主值）
                "effect_group": str(lv.get("effect_group") or "inherit"),
                "command_effect_group": str(lv.get("command_effect_group") or "inherit"),
                "provider_id": str(lv.get("provider_id") or ""),
                "fallback_provider_id": str(lv.get("fallback_provider_id") or ""),
                # v9：允许该等级的用户用 /切换模型 自助挑选的模型名单（空 = 用兜底三项）
                "switch_providers": parse_provider_list(lv.get("switch_providers")),
                # v10：是否允许切换（false = 只读，能看不能切；**默认 false**）
                "switch_enabled": bool(int(lv.get("switch_enabled") or 0)),
                # v11：是否允许用 /切换模型检测（会真打模型花钱，**默认 false**）
                "detect_enabled": bool(int(lv.get("detect_enabled") or 0)),
                "members": int(counts.get(lid, 0)),
                "quotas": limits.get(str(lid), {}),
            },
        )
    return ok({"items": items, "counts": counts, "stale": report.get("stale") or [],
               "stale_total": int(report.get("total") or 0),
               "configured_known": bool(report.get("configured_known"))})


async def h_providers(plugin) -> dict:
    """列出可用的对话模型提供商（等级里选「走哪个模型」用）。

    **模型选择以「提供商」为单位**（参考 model_panel 的做法）：AstrBot 里一个提供商就绑定
    一个模型，所以下拉里一项 = 一个提供商，显示成「名称 · 模型」。
    只列**已加载**的：AstrBot 遇到不存在的提供商 id 会直接放弃本次 LLM 请求，
    所以必须让前端只能从这份名单里选。附带返回当前熔断中的提供商与 AstrBot 的默认提供商。
    """
    default_id = ""
    try:
        prov = await plugin.context.get_using_provider_async()
        default_id = str((getattr(prov, "provider_config", {}) or {}).get("id") or "")
    except Exception:
        default_id = ""

    # 展示名口径与 /切换模型 卡片共用 plugin.provider_map()（一处定义，避免两边标签不一致）
    try:
        items = [
            {**info, "is_default": bool(default_id and info["id"] == default_id)}
            for info in plugin.provider_map().values()
        ]
    except Exception as e:
        logger.warning(f"[UserGateway] 读取提供商列表失败: {e}")
        items = []

    items.sort(key=lambda it: (not it["is_default"], it["label"]))
    circuit = getattr(plugin, "circuit", None)
    report = await _stale_report(plugin)
    return ok(
        {
            "items": items,
            "default_id": default_id,
            "circuit_open": sorted(circuit.open_ids()) if circuit else [],
            "route_enabled": bool(plugin._cfg("model_route_enabled", True)),
            # 顺带把失效引用数带给前端，等级页顶部能直接提示「有几个模型配置失效了」
            "stale_total": int(report.get("total") or 0),
            "stale_refs": int(report.get("total_refs") or 0),
        },
    )


# ---------------------------------------------------------------------- #
# 模型引用一致性（AstrBot 删除 / 禁用模型后的残留配置）
# ---------------------------------------------------------------------- #
async def h_model_refs(plugin) -> dict:
    """列出库里所有指向「当前不可用模型」的引用，附状态和出现位置。

    **只读**：绝不自动改数据。禁用是可逆的、加载是有时序的，自动清理会在
    用户临时禁用模型时把配好的等级路由抹掉 —— 清理必须用户主动点。
    """
    return ok(await _stale_report(plugin))


async def h_prune_model_refs(plugin) -> dict:
    """**用户主动触发**的定向清理：只清 body.ids 里勾选的模型 id。

    服务端双重保险：
    1. 只清「当前确实未加载」的 id —— 模型重新启用后这个请求就清不掉它；
    2. ``configured_known=False``（读不到 AstrBot 配置，无法区分禁用/删除）时直接拒绝，
       避免一次 API 抖动让用户误删配置。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    raw_ids = body.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return err("请选择要清理的模型（ids 不能为空）")
    wanted = {str(p or "").strip() for p in raw_ids if str(p or "").strip()}

    report = await _stale_report(plugin)
    if not report.get("configured_known"):
        return err("暂时读不到 AstrBot 的模型配置，无法确认这些模型是否真的被删除，已取消清理")
    alive = set(report.get("available") or ())
    refused = sorted(wanted & alive)
    safe = wanted - alive
    if not safe:
        return err("这些模型当前都已加载（可能刚被重新启用），无需清理，请刷新页面")

    stats = await plugin.store.prune_provider_refs(safe)
    await plugin.store.log_audit(
        "console", "prune_model_refs",
        json.dumps({"ids": sorted(safe), "refused": refused, **stats}, ensure_ascii=False),
    )
    await plugin.reload_rules()  # 内存快照里还留着旧 id，必须立刻重建
    left = await _stale_report(plugin)
    return ok({"pruned": sorted(safe), "refused": refused, "stats": stats,
               "remaining": int(left.get("total") or 0)})


async def h_set_level(plugin) -> dict:
    """新建 / 更新等级，并可同时写入该等级的额度模板。

    body: ``{id?, kind, name, description?, effect?, effect_command?, sort_order?,
    provider_id?, fallback_provider_id?, switch_enabled?, switch_providers?, quotas?: [...]}``

    - ``effect``：等级默认 **LLM** 权限；``effect_command``：等级默认 **指令** 权限
      （``deny`` 即该等级不能用任何指令）；
    - ``switch_enabled``（v10）：该等级**是否允许**用户用 ``/切换模型`` 自助切换（默认假 = 只读）；
    - ``detect_enabled``（v11）：该等级**是否允许**用户用 ``/切换模型检测``（默认假；会真花钱）；
    - ``switch_providers``：可切换的模型名单（v9，空 = 展示兜底三项：当前 / 系统默认 / 备用）；
    - ``quotas`` 里 ``limit_tokens=0`` 表示「明确不限」，``null`` / ``delete=true`` 表示删掉该周期。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    kind = str(body.get("kind") or "user").strip()
    name = str(body.get("name") or "").strip()
    effect = str(body.get("effect") or "inherit").strip()
    # 兼容两种写法：前端用 effect_command（与列表返回字段一致），也接受 command_effect
    cmd_effect = str(body.get("effect_command") or body.get("command_effect") or "inherit").strip()
    # 群聊场景那一套：只对「好友等级」有意义（群聊等级本来就只在群里生效）
    eff_group = str(body.get("effect_group") or "inherit").strip()
    cmd_eff_group = str(body.get("command_effect_group") or "inherit").strip()
    if kind not in ("user", "group"):
        return err("kind 必须是 user 或 group")
    if kind == "group":
        # 群聊等级只有群聊场景，不需要（也不该）有两套默认权限
        eff_group = cmd_eff_group = "inherit"
    if not name:
        return err("等级名称不能为空")
    if effect not in ("inherit", "allow", "deny"):
        return err("effect 必须是 inherit / allow / deny（等级默认 LLM 权限）")
    if cmd_effect not in ("inherit", "allow", "deny"):
        return err("effect_command 必须是 inherit / allow / deny（等级默认指令权限）")
    for label, val in (("effect_group", eff_group), ("command_effect_group", cmd_eff_group)):
        if val not in ("inherit", "allow", "deny"):
            return err(f"{label} 必须是 inherit / allow / deny（群聊场景的默认权限）")
    try:
        sort_order = int(body.get("sort_order") or 0)
    except Exception:
        sort_order = 0
    try:
        level_id = int(body["id"]) if body.get("id") else None
    except Exception:
        level_id = None
    if level_id is not None:
        old = await plugin.store.get_level(level_id)
        if not old:
            return err(f"等级 {level_id} 不存在")

    # 可切换模型（v9）：只收「双向都存在」的提供商 id —— 不存在的 id 写进去用户也切不了
    # （route_model 会因不可用而静默回落，用户只会觉得「切了没用」）。
    old = await plugin.store.get_level(level_id) if level_id else None
    raw_switch = body.get("switch_providers")
    if raw_switch is None:
        # 兼容：字段缺省时沿用原值（避免旧前端 / 部分更新把已配的名单清空）
        switch_providers = parse_provider_list((old or {}).get("switch_providers"))
    else:
        known = plugin.provider_ids()
        switch_providers = [
            pid for pid in parse_provider_list(raw_switch) if not known or pid in known
        ]
    # 是否允许切换（v10）：同样「缺省 = 沿用原值」，默认关（只读）
    if "switch_enabled" in body:
        switch_enabled = _as_bool(body.get("switch_enabled"), default=False)
    else:
        switch_enabled = bool(int((old or {}).get("switch_enabled") or 0))
    # 是否允许检测指令（v11）：与 switch_enabled 同一套写法 —— 缺省沿用原值、默认关
    if "detect_enabled" in body:
        detect_enabled = _as_bool(body.get("detect_enabled"), default=False)
    else:
        detect_enabled = bool(int((old or {}).get("detect_enabled") or 0))

    # 主 / 备模型：只拦「本次新填的、当前未加载的 id」。
    # 用户编辑等级其它字段时会原样回传旧值；若那个模型恰好被临时禁用，
    # 一律拒绝会让他连名字都改不了 —— 所以旧值原样保留，只挡新增悬空引用。
    bad_ids = provider_refs_mod.validate_new_ids(
        {
            "provider_id": body.get("provider_id"),
            "fallback_provider_id": body.get("fallback_provider_id"),
        },
        old,
        plugin.provider_ids(),
    )
    if bad_ids:
        return err(
            "以下模型当前未加载（可能已被删除或禁用），不能新写入等级："
            + "、".join(bad_ids)
            + "。请先在 AstrBot「模型提供商」里启用，或从下拉里另选一个。"
        )

    new_id = await plugin.store.upsert_level(
        kind,
        name,
        description=str(body.get("description") or "").strip(),
        effect=effect,
        sort_order=sort_order,
        level_id=level_id,
        provider_id=str(body.get("provider_id") or "").strip(),
        fallback_provider_id=str(body.get("fallback_provider_id") or "").strip(),
        command_effect=cmd_effect,
        effect_group=eff_group,
        command_effect_group=cmd_eff_group,
        switch_providers=switch_providers,
        switch_enabled=switch_enabled,
        detect_enabled=detect_enabled,
    )
    if not new_id:
        return err("等级写入失败")

    quotas = body.get("quotas")
    changed_quota = False
    if isinstance(quotas, list):
        for q in quotas:
            if not isinstance(q, dict):
                continue
            period = str(q.get("period") or "").strip()
            if period not in ("day", "month", "total"):
                continue
            mode = str(q.get("mode") or "enforce").strip()
            if mode not in ("enforce", "observe"):
                mode = "enforce"
            if _as_bool(q.get("delete"), default=False) or q.get("limit_tokens") is None:
                await plugin.store.delete_quota("level", str(new_id), period)
            else:
                try:
                    limit = max(0, int(q.get("limit_tokens")))
                except Exception:
                    continue
                # limit=0 = 明确「不限」：保留该行占住档位（见 h_set_quota 说明）
                await plugin.store.upsert_quota(
                    "level",
                    str(new_id),
                    period,
                    limit,
                    mode=mode,
                    reset_at=plugin.quota_reset_at(period),
                )
            changed_quota = True

    await plugin.store.log_audit(
        "console",
        "set_level",
        json.dumps({"id": new_id, "kind": kind, "name": name, "quotas": changed_quota}, ensure_ascii=False),
    )
    await plugin.reload_rules()
    return ok({"id": new_id})


async def h_delete_level(plugin) -> dict:
    """删除等级（连带清掉它的额度模板与所有归级记录，相关对象回到「无等级」）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    try:
        level_id = int(body.get("id"))
    except Exception:
        return err("缺少 id")
    lv = await plugin.store.get_level(level_id)
    if not lv:
        return err(f"等级 {level_id} 不存在")
    await plugin.store.delete_level(level_id)
    await plugin.store.log_audit("console", "delete_level", json.dumps({"id": level_id}, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"deleted": level_id})


async def h_set_subject_level(plugin) -> dict:
    """给好友 / 群设置等级。

    body: ``{scope_type, scope_id, level_id, scene?}`` 或 ``{items: [...]}``；

    - 群（scope_type=group）不分场景，``level_id`` 为 0 / null 表示取消；
    - 好友（scope_type=user）**分场景**（v8）：``scene`` = ``private``（默认，写私聊等级）
      或 ``group``（写群聊专属等级，``level_id`` 为 0 / null = 跟随私聊等级）。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    items = body.get("items")
    if not isinstance(items, list):
        items = [body]

    applied = []
    for it in items:
        if not isinstance(it, dict):
            continue
        scope_type = str(it.get("scope_type") or "").strip()
        scope_id = str(it.get("scope_id") or "").strip()
        if scope_type not in ("user", "group"):
            return err("scope_type 必须是 user 或 group")
        if not scope_id:
            return err("scope_id 不能为空")
        raw = it.get("level_id")
        try:
            level_id = int(raw) if raw else None
        except Exception:
            level_id = None
        if level_id:
            lv = await plugin.store.get_level(level_id)
            if not lv:
                return err(f"等级 {level_id} 不存在")
            lv_kind = str(lv.get("kind") or "user")
            if lv_kind != scope_type:
                return err(
                    f"「{lv.get('name')}」是{'好友' if lv_kind == 'user' else '群聊'}等级，"
                    f"不能用在{'好友' if scope_type == 'user' else '群聊'}上"
                )
        if scope_type == "group":
            await plugin.store.set_subject_level("group", scope_id, level_id)
            applied.append({"scope_type": "group", "scope_id": scope_id, "level_id": level_id})
            continue
        # 好友：分场景
        scene = str(it.get("scene") or "private").strip() or "private"
        if scene not in ("private", "group"):
            return err("scene 必须是 private 或 group")
        if scene == "private":
            await plugin.store.set_user_level_scene(scope_id, private_level=level_id)
        else:
            # null / 0 = 清除群聊专属（跟随私聊等级）
            await plugin.store.set_user_level_scene(
                scope_id, group_level=level_id if level_id else None
            )
        applied.append(
            {"scope_type": "user", "scope_id": scope_id, "level_id": level_id, "scene": scene}
        )

    if not applied:
        return err("没有可应用的变化")
    await plugin.store.log_audit("console", "set_subject_level", json.dumps(applied, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"applied": applied})


async def h_set_subject_model(plugin) -> dict:
    """专属模型（v1.2.0 好友 / v1.3.12 群聊）：优先级高于等级里配置的模型。

    body: ``{scope_id, provider_id, type?}``；``type`` 缺省 ``user``（好友专属，仅私聊生效），
    ``group`` = 群专属模型（对**全群**生效）。``provider_id`` 为空 = 恢复跟随等级配置。

    与 ``/切换模型`` 改的是同一把键（feature=model）：指令切换与这里的手动切换互相同步。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    scope_id = str(body.get("scope_id") or "").strip()
    provider_id = str(body.get("provider_id") or "").strip()
    stype = str(body.get("type") or "user").strip()
    if stype not in ("user", "group"):
        return err("type 必须是 user 或 group")
    if not scope_id:
        return err("scope_id 不能为空")
    if provider_id and provider_id not in plugin.provider_ids():
        return err("该提供商未加载或不存在（先在 AstrBot「模型提供商」里启用）")
    await plugin.store.set_policy(
        stype,
        scope_id,
        provider_id or "inherit",
        feature=MODEL_FEATURE,
        note="群专属模型（对全群生效）" if stype == "group" else "好友专属模型（仅私聊生效）",
    )
    await plugin.store.log_audit(
        "console",
        "set_subject_model",
        json.dumps(
            {"type": stype, "scope_id": scope_id, "provider_id": provider_id},
            ensure_ascii=False,
        ),
    )
    await plugin.reload_rules()
    return ok({"type": stype, "scope_id": scope_id, "provider_id": provider_id})


# ---------------------------------------------------------------------- #
# 指令权限
# ---------------------------------------------------------------------- #
async def h_commands(plugin) -> dict:
    """指令清单 + 每条指令的权限规则（控制台「指令」页）。

    指令来自 AstrBot 的 handler 注册表（``plugin.registered_commands()``）：
    没注册的指令不会出现在这里；规则里引用了但已经不存在的指令名放在 ``stale``。
    """
    commands = plugin.registered_commands()
    policy = getattr(plugin, "_cmd_policy", {}) or {}
    items: list[dict] = []
    for c in commands:
        name = str(c.get("name") or "")
        table = policy.get(name) or {}
        rules: list[dict] = []
        for scope_type, mapping in table.items():
            for scope_id, scenes in (mapping or {}).items():
                if scope_type == "global":
                    continue
                for scene, effect in (scenes or {}).items():
                    rules.append(
                        {
                            "scope_type": scope_type,
                            "scope_id": scope_id,
                            "scene": scene,
                            "effect": effect,
                        }
                    )
        rules.sort(key=lambda r: (r["scope_type"], r["scope_id"], r["scene"]))
        items.append(
            {
                **c,
                "global_effect": effect_in_scene((table.get("global") or {}).get("*"), "")
                or "inherit",
                "rules": rules,
                "rule_count": len(rules),
            },
        )
    stale = sorted(set(policy) - {str(c.get("name") or "") for c in commands})
    return ok(
        {
            "items": items,
            "default_effect": plugin.gate.default_command_effect(),
            "enabled": bool(plugin._cfg("command_guard_enabled", True)),
            "priority": int(plugin._cfg("command_guard_priority", 1000) or 1000),
            "stale": stale,
        },
    )


async def h_command_matrix(plugin) -> dict:
    """指令 × 对象矩阵：某对象能用哪些指令、结论来自哪一层。

    ``?scope_type=user|group|member|level|global&scope_id=...&scene=private|group``
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    scope_type = _q("scope_type", "user") or "user"
    scope_id = _q("scope_id")
    if scope_type not in ("user", "group", "member", "level", "global"):
        return err("scope_type 必须是 user / group / member / level / global")
    if scope_type == "global":
        scope_id = "*"
    if not scope_id:
        return err("缺少 scope_id")
    if scope_type == "member":
        gid, uid = parse_member_scope_id(scope_id)
        if not (gid and uid):
            return err("群成员的 scope_id 必须是「群号:QQ」")
        scope_id = member_scope_id(gid, uid)
    try:
        data = await plugin.command_matrix_of(scope_type, scope_id, _scene_arg("private"))
    except Exception as e:
        logger.exception("[UserGateway] 生成指令矩阵失败")
        return err(f"生成矩阵失败：{e}")
    if not data:
        return err("对象不存在（等级 id 可能已删除）")
    return ok(data)


async def h_prune_commands(plugin) -> dict:
    """清理已失效的指令规则（指令被卸载/改名后残留的 policy 行）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    alive = [str(c.get("name") or "") for c in plugin.registered_commands()]
    n = await plugin.store.delete_stale_command_policies(alive)
    await plugin.store.log_audit("console", "prune_commands", json.dumps({"deleted": n}, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"deleted": n, "alive": len(alive)})


# ---------------------------------------------------------------------- #
# 头像
# ---------------------------------------------------------------------- #
async def h_avatars(plugin) -> dict:
    """批量取头像，返回 ``{id: data URI}``（前端直接塞 ``<img src>``）。

    参数：``?type=user|group&ids=1,2,3&force=0&days=30``。
    只对可见行的 id 按需抓取，抓不到的 id 不会出现在结果里（前端退化为首字母色块）。
    """
    cache = getattr(plugin, "avatars", None)
    if cache is None:
        return ok({"items": {}, "stats": {}, "error": "头像缓存未初始化"})
    # 配置页关掉头像时直接返回空（前端退化为首字母色块），逻辑收在服务端、前端不用关心配置
    if (
        str(plugin._cfg("avatar_source", "backend") or "backend").strip().lower() == "off"
        or not _as_bool(plugin._cfg("load_avatars", True), default=True)
    ):
        return ok({"items": {}, "stats": cache.stats(), "requested": 0, "got": 0, "disabled": True})
    kind = _q("type", "user") or "user"
    if kind not in ("user", "group"):
        return err("type 必须是 user 或 group")
    raw = _q("ids")
    ids = [s.strip() for s in raw.split(",") if s.strip()][:400]
    if not ids:
        return ok({"items": {}, "stats": cache.stats(), "requested": 0, "got": 0})
    force = _q("force").lower() in ("1", "true", "yes", "on")
    default_days = int(plugin._cfg("avatar_cache_days", 30) or 0)
    days = _qi("days", default_days, 0, 3650)
    try:
        items = await cache.ensure_map(kind, ids, force=force, max_age_days=days)
    except Exception as e:
        logger.warning(f"[UserGateway] 取头像失败（忽略）: {e}")
        items = {}
    return ok({"items": items, "stats": cache.stats(), "requested": len(ids), "got": len(items)})


async def h_avatars_refresh(plugin) -> dict:
    """更新头像缓存。

    body: ``{type?, ids?: [...]}``；``ids`` 为空表示「把该类型**所有已缓存**的头像重取一遍」
    （用于好友换了头像之后手动刷新）。
    """
    cache = getattr(plugin, "avatars", None)
    if cache is None:
        return err("头像缓存未初始化")
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    kind = str(body.get("type") or "user").strip()
    if kind not in ("user", "group"):
        return err("type 必须是 user 或 group")
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        prefix = "group_" if kind == "group" else "user_"
        try:
            ids = [f.stem[len(prefix):] for f in cache.root.glob(f"{prefix}*.img")]
        except Exception:
            ids = []
    if not ids:
        return ok({"refreshed": 0, "failed": 0, "stats": cache.stats()})
    got = await cache.ensure(kind, ids, force=True)
    await plugin.store.log_audit(
        "console", "refresh_avatars", json.dumps({"type": kind, "n": len(ids)}, ensure_ascii=False)
    )
    return ok({"refreshed": len(got), "failed": len(ids) - len(got), "stats": cache.stats()})


# ---------------------------------------------------------------------- #
# 注册
# ---------------------------------------------------------------------- #
def _bind(plugin, fn: Callable[[Any], Awaitable[dict]]) -> Callable[[], Awaitable[dict]]:
    """把 ``fn(plugin)`` 绑定成 AstrBot 可直接调用的无参协程（保留函数名便于日志识别）。"""

    async def handler() -> dict:
        try:
            return await fn(plugin)
        except Exception as e:  # 任何异常都转成信封，避免前端只看到 500
            logger.exception(f"[UserGateway] {fn.__name__} 处理异常")
            return err(f"{fn.__name__} 失败: {e}")

    handler.__name__ = f"usergw_{fn.__name__}"
    return handler


def register_apis(plugin) -> None:
    """把控制台路由注册到 AstrBot（在插件 initialize() 中调用）。"""
    routes: list[tuple[str, Callable[[Any], Awaitable[dict]], list[str]]] = [
        ("/ping", h_ping, ["GET"]),
        ("/config", h_get_config, ["GET"]),
        ("/config", h_set_config, ["POST"]),
        ("/overview", h_overview, ["GET"]),
        ("/friends", h_friends, ["GET"]),
        ("/groups", h_groups, ["GET"]),
        ("/sync", h_sync, ["POST"]),
        ("/group/members", h_group_members, ["GET"]),
        ("/group/members/sync", h_sync_group_members, ["POST"]),
        ("/subject", h_subject, ["GET"]),
        ("/policy", h_get_policy, ["GET"]),
        ("/policy", h_set_policy, ["POST"]),
        ("/quota", h_get_quota, ["GET"]),
        ("/quota", h_set_quota, ["POST"]),
        ("/quota/reset", h_reset_quota, ["POST"]),
        ("/commands", h_commands, ["GET"]),
        ("/commands/matrix", h_command_matrix, ["GET"]),
        ("/commands/prune", h_prune_commands, ["POST"]),
        ("/levels", h_levels, ["GET"]),
        ("/levels", h_set_level, ["POST"]),
        ("/levels/delete", h_delete_level, ["POST"]),
        ("/subject-level", h_set_subject_level, ["POST"]),
        ("/subject/model", h_set_subject_model, ["POST"]),
        ("/providers", h_providers, ["GET"]),
        ("/model-refs", h_model_refs, ["GET"]),
        ("/model-refs/prune", h_prune_model_refs, ["POST"]),
        ("/avatars", h_avatars, ["GET"]),
        ("/avatars/refresh", h_avatars_refresh, ["POST"]),
        ("/usage", h_usage, ["GET"]),
        ("/usage/export", h_usage_export, ["GET"]),
        ("/stats/commands", h_stats_commands, ["GET"]),
        ("/rules/export", h_export_rules, ["GET"]),
        ("/rules/import", h_import_rules, ["POST"]),
        ("/audit", h_audit, ["GET"]),
    ]
    for path, fn, methods in routes:
        plugin.context.register_web_api(
            f"{ROUTE_PREFIX}{path}",
            _bind(plugin, fn),
            methods,
            f"UserGateway {path}",
        )
    logger.info(f"[UserGateway] 已注册 {len(routes)} 条控制台路由（前缀 {ROUTE_PREFIX}）")
