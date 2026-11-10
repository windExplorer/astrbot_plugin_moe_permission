"""用户自助切换模型（``/切换模型`` 指令 + 回序号选择）。

功能形状（与 PRD / CHANGELOG 对应）：

1. 管理员在**等级**里打开「允许切换模型」开关（``quota_level.switch_enabled``，**默认关**），
   并可另配一份「可切换模型」名单（``switch_providers``）；
2. 用户发 ``/切换模型``：按他所在的分组（私聊 = 好友等级、群聊 = 群等级）列出候选模型，
   渲染成卡片图（Pillow，见 :mod:`model_card` 的说明）；
3. 开关打开时用户回一个序号即切换（**0 = 恢复默认**）；开关关闭时**只读**：
   卡片照发、能看清自己在用什么，但切不了。
4. 切换改的是**专属模型**（v1.3.12）：私聊 = 他自己的专属模型、群聊 = **这个群**的专属模型
   （对全群生效，所以群里只有管理员能切）。

几个刻意的设计（改动前请先读）：

- **默认只读**：`switch_enabled` 默认关。放开自助切换是管理员显式的动作，
  配置没读到（比如库刚升级）也只会退回只读，不会凭空放开。
- **只读时也发卡片**：用户至少要知道「我现在用的是哪个、还有哪些可能」——
  需求原话是「没有配置名单就展示当前模型 + 系统默认 + 备用模型」。
  只读时**不记序号会话**，所以此时随手发的数字不会被这条指令截走。
- **没配名单 → 兜底三项**：当前生效的、AstrBot 系统默认、等级备用模型（去重后）。
  配了名单则以名单为准（名单是管理员划的圈）。
- **群聊仅管理员可用**：切换改的是**群的专属模型**（对全群生效），谁都能切会把整个群搅乱。
- **切换 = 改专属模型**（v1.3.12 起废弃旧的 model_choice 层）：私聊写到
  ``policy(user, feature='model')``、群聊写到 ``policy(group, feature='model')`` ——
  与控制台「专属模型」是**同一把键**，手动切换（控制台）与指令切换互相看得见。
  旧版按「用户」存群场景的选择，结果「在 A 群切换 → 这个人在所有群都换了」（跨群串号），
  这就是废弃它的原因；旧数据由 ``store.migrate_model_choice`` 启动时迁走。
- **优先级：专属模型 > 等级模型路由 > 系统默认**。切了不生效等于没切，
  所以专属模型必须压过等级路由；等级路由里配的模型仍是**候选之一**（可切回来）。
- **候选名单不放任**：只能从「等级名单（或兜底三项）+ 专属模型 + 当前生效的那个」里选，
  这样「让人自己挑」不会变成「谁都能挑最贵的那个」。当前生效的那个会额外列出来，
  否则卡片上就没有一行能被标成「当前使用」。
- **序号会话有 TTL 且绑定发言人**：键是 ``umo + 发言人``，所以群里别人发的数字不会误触发，
  60 秒（可配）后失效。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

try:  # 包内正常加载
    from . import model_card
    from .gate import Subject
    from .store import MODEL_FEATURE
except ImportError:  # pragma: no cover - 本地平铺调试
    import model_card  # type: ignore
    from gate import Subject  # type: ignore
    from store import MODEL_FEATURE  # type: ignore

# 序号会话上限（防御性：长期运行不涨内存；正常同时最多几十个）
MAX_SESSIONS = 200
# TTL 边界（配置值是秒）
MIN_TTL = 10
MAX_TTL = 600
# 头像现抓的超时与「抓不到」的记忆时长（秒）：
# 指令回执不能被头像拖住，离线环境也不该每次发指令都空等一遍超时。
AVATAR_TIMEOUT = 3.0
AVATAR_MISS_TTL = 600

# 「当前使用」的来源文案
SOURCE_OWN = "专属模型"  # 私聊 = 他自己的；群聊 = 这个群的（卡片 note 里再区分）
SOURCE_LEVEL = "分组主模型"
SOURCE_LEVEL_FALLBACK = "分组备用模型（主模型不可用）"
SOURCE_DEFAULT = "系统默认模型"

# 只读 / 不可用的原因（也直接当给用户看的文案用）
REASON_LEVEL_OFF = "所在分组未开放模型切换"
REASON_GROUP_ONLY_ADMIN = "群里只有管理员能切换模型"
# 只读时的卡片底部提示
READONLY_HINT = "只读：{reason}，请联系管理员"
# 群聊里非管理员的一句话说清（不发卡片：需求是「群聊仅管理员能用」）
GROUP_REFUSE = "群里只有管理员能切换模型；你可以私聊我发 /切换模型 查看或切换。"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fmt_num(n: Any) -> str:
    """token 数字的紧凑写法（与控制台列表的 1.2K / 3.45M 口径一致）。"""
    v = _as_int(n, 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def _logo_bytes() -> Optional[bytes]:
    """插件 logo（头像取不到时的兜底，读不到就返回 None 让卡片留空位）。"""
    try:
        p = Path(__file__).resolve().parent / "logo.png"
        return p.read_bytes() if p.is_file() else None
    except Exception:
        return None


class ModelSwitcher:
    """``/切换模型`` 的状态机 + 名单组装（一个插件实例一个）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        # key = "umo|sender_id" → {"options": [...], "expire": ts, "scene": str}
        self._pending: dict[str, dict[str, Any]] = {}
        # key = "group:88888" → 该头像「抓取失败」的到期时刻（见 avatar_for）
        self._avatar_miss: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 配置 / 会话
    # ------------------------------------------------------------------ #
    def enabled(self) -> bool:
        return bool(self._plugin._cfg("model_switch_enabled", True))

    def ttl(self) -> int:
        got = _as_int(self._plugin._cfg("model_switch_timeout_sec", 60), 60)
        return max(MIN_TTL, min(MAX_TTL, got))

    def recall_on(self) -> bool:
        """到期后要不要把卡片撤回（总开关，默认开）。"""
        return bool(self._plugin._cfg("model_card_recall", True))

    def recall_sec(self) -> int:
        """卡片在多少秒后自动撤回（``0`` = 不撤）。

        **与「序号有效期」共用同一个数字**（``model_switch_timeout_sec``）：
        用户能回序号的时间一到，卡片就该消失。两个值分开配迟早会不一致 ——
        卡片先没了（用户莫名其妙）、或者过期了还挂着（引着人去点一个已经无效的序号）。
        """
        return self.ttl() if self.recall_on() else 0

    @staticmethod
    def key_of(subject: Subject) -> str:
        """会话键：**带上发言人**，否则群里别人随手发的数字会顶掉你的选择。"""
        return f"{subject.umo}|{subject.sender_id}"

    def _prune(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        stale = [k for k, v in self._pending.items() if float(v.get("expire") or 0) <= now]
        for k in stale:
            self._pending.pop(k, None)
        if len(self._pending) > MAX_SESSIONS:  # 兜底：极端情况直接清空（会话本来就是短时的）
            self._pending.clear()

    def remember(
        self,
        subject: Subject,
        options: list[dict[str, Any]],
        scene: str,
        *,
        can_switch: bool = True,
        reason: str = "",
    ) -> None:
        """记下这次列出的序号选项（用户回序号时按它换算）。

        ``can_switch=False`` 的会话不该被建立（只读时 ``handle_command`` 直接跳过这一步），
        参数留着是为了让 :meth:`apply` 有一道显式防线，而不是靠「外面记得别调」。
        """
        self._prune()
        self._pending[self.key_of(subject)] = {
            "options": options,
            "scene": scene,
            "can_switch": bool(can_switch),
            "readonly_reason": str(reason or ""),
            "expire": time.time() + self.ttl(),
        }

    def peek(self, subject: Subject) -> Optional[dict[str, Any]]:
        """取该会话待选的序号表（过期 / 不存在返回 None，过期的顺手清掉）。"""
        key = self.key_of(subject)
        item = self._pending.get(key)
        if not item:
            return None
        if float(item.get("expire") or 0) <= time.time():
            self._pending.pop(key, None)
            return None
        return item

    def clear(self, subject: Subject) -> None:
        self._pending.pop(self.key_of(subject), None)

    def sessions(self) -> int:
        self._prune()
        return len(self._pending)

    # ------------------------------------------------------------------ #
    # 名单组装
    # ------------------------------------------------------------------ #
    async def describe(self, subject: Subject) -> dict[str, Any]:
        """算出「这个人现在能用哪些模型、当前用的是哪个、能不能切」。

        Returns:
            ``{scene, level_id, level_name, level_kind, options, current_id, current_source,
            open, allowed, can_switch, readonly_reason, is_admin, group_restricted}``；
            ``options`` 每项 ``{index, provider_id, label, note, current, own, unavailable}``。

        ``open`` = 有东西可展示（只读时也是 True）；``can_switch`` = 真的能切
        （只读 / 群聊非管理员时为 False）。
        """
        plugin = self._plugin
        scene = "group" if str(subject.group_id or "") else "private"
        uid = str(subject.sender_id or "")
        is_admin = bool(getattr(subject, "is_admin", False))

        rules = plugin._rules()
        found = plugin.gate.model_level_of(subject, rules)
        level_id = int(found[1]) if found else None
        level_kind = str(found[0]) if found else ("group" if scene == "group" else "user")
        level_name = ""
        if level_id:
            try:
                lv = await plugin.store.get_level(level_id) if plugin.store else None
                level_name = str((lv or {}).get("name") or "")
            except Exception:
                level_name = ""

        allowed = [str(p or "").strip() for p in (plugin.gate.switch_options(rules, level_id) or ())]
        allowed = [p for p in allowed if p]
        level_on = plugin.gate.switch_enabled(rules, level_id)

        available = set(plugin.provider_ids())
        circuit = plugin.circuit.open_ids() if getattr(plugin, "circuit", None) else set()
        usable = available - circuit
        system_default = await self.system_default_of(subject)
        route = plugin.gate.resolve_model(subject, rules) or {}
        # 专属模型：私聊 = 他自己的、群聊 = 这个群的（v1.3.12 起切换写的就是这一份）
        own = self.own_model_of(subject, scene)
        current_id, current_source = await self.current_of(
            subject, scene, usable, system_default, route
        )

        # 能不能切：群聊只放给管理员；等级开关默认关（只读），管理员同样豁免
        group_restricted = scene == "group"
        if group_restricted and not is_admin:
            can_switch, readonly_reason = False, REASON_GROUP_ONLY_ADMIN
        elif not (level_on or is_admin):
            can_switch, readonly_reason = False, REASON_LEVEL_OFF
        else:
            can_switch, readonly_reason = True, ""

        ordered: list[str] = []

        def add(pid: Any) -> None:
            text = str(pid or "").strip()
            if text and text not in ordered:
                ordered.append(text)

        if allowed:
            for pid in allowed:
                add(pid)
        else:
            # 没配名单 → 兜底展示三项：当前生效的 / 系统默认 / 等级备用。
            # 需求原话就是这么要求的：至少让人看清「现在走的是哪个、还能退到哪」。
            add(current_id)
            add(system_default)
            add(route.get("fallback_provider_id"))
        if own:
            add(own)
        # 当前生效的那个也列出来：否则卡片上没有任何一行能标「当前使用」
        if current_id and current_source != SOURCE_DEFAULT:
            add(current_id)

        # v1.5.1：AstrBot 删除 / 禁用模型后，这些 id 会残留在等级路由和专属模型里。
        # 把「当前根本没加载」的从候选名单剔除 —— 它们是死引用，列出来既切不了，
        # 又会被 /切换模型检测 拿去白打一遍。**熔断中的仍在 available 里**，
        # 那只是临时状态，照旧展示并标「暂不可用」，两者不能混为一谈。
        stale_ids = sorted({pid for pid in ordered if pid not in available})
        if stale_ids:
            ordered = [pid for pid in ordered if pid in available]
        # 当前生效的那个也可能已经失效（例如系统默认模型被删）：不能让卡片头部
        # 顶着一个不存在的模型名说「当前使用」。
        if current_id and current_id not in available:
            current_id = ""

        infos = plugin.provider_map()

        options: list[dict[str, Any]] = []
        for i, pid in enumerate(ordered, start=1):
            info = infos.get(pid) or {"label": pid}
            is_current = bool(current_id) and pid == current_id
            is_own = bool(own) and pid == own
            note = ""
            if is_current:
                # 只写「来源」不写「当前使用」：后者已经由行右侧那个实心标签表达了，
                # 重复一遍反而把第二行占满 —— 那一行现在要留给「数据是什么时候的」。
                note = str(current_source or "")
            elif is_own:
                note = "这个群的专属模型" if scene == "group" else "你的专属模型"
            options.append(
                {
                    "index": i,
                    "provider_id": pid,
                    "label": str(info.get("label") or pid),
                    "note": note,
                    "current": is_current,
                    "own": is_own,
                    "unavailable": pid not in available or pid in circuit,
                }
            )

        return {
            "scene": scene,
            "level_id": level_id,
            "level_name": level_name,
            "level_kind": level_kind,
            "options": options,
            "current_id": current_id,
            "current_source": current_source,
            "open": bool(options),
            # 名单本身（不等于 options）：用来区分「配了名单」与「走兜底三项」
            "allowed": allowed,
            # 被剔除的失效 id（AstrBot 已删除 / 禁用 / 未加载）：卡片脚部会提示去控制台清理
            "stale_ids": stale_ids,
            "can_switch": can_switch,
            "readonly_reason": readonly_reason,
            "is_admin": is_admin,
            "group_restricted": group_restricted,
            "system_default": system_default,
            # 今日用量（信息条展示；取不到就是空字典，卡片少一行而已）
            "today": await self.today_of(subject),
        }

    async def today_of(self, subject: Subject) -> dict[str, int]:
        """今日用量：私聊 = **这个人**、群聊 = **这个群**（与总览 / 列表页同口径）。

        用户看卡片时最常问的两件事是「我现在用的是哪个模型」和「今天用掉多少」，
        所以顺手放进标题区那一行。

        **查得到就一定有这一行，哪怕今天是 0** —— 第一版在 0 的时候把这一行藏了，
        结果用户以为是功能没生效（"卡片上没看到今日 token 使用量"）。
        只有**统计读不到**（旧库 / 查询失败 / 库没就绪）才返回空字典降级。
        """
        try:
            store = self._plugin.store
            if not (store and store.ready):
                return {}
            kind = "group" if str(subject.group_id or "") else "user"
            sid = str(subject.group_id or subject.sender_id or "")
            if not sid:
                return {}
            now = datetime.now()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            stats = await store.subject_stats(
                kind, sid, int(start.timestamp()), int(now.timestamp())
            )
            totals = (stats or {}).get("totals") or {}
            return {
                "tokens": _as_int(totals.get("tok_total"), 0),
                "calls": _as_int(totals.get("calls"), 0),
            }
        except Exception as e:  # 统计失败绝不冒泡（卡片少一行，功能照常）
            logger.debug(f"[UserGateway] 读取今日用量失败（忽略）: {e}")
            return {}

    def group_label(self, data: dict[str, Any]) -> str:
        """分组的中文说法（卡片副标题用）。"""
        kind = str(data.get("level_kind") or "user")
        name = str(data.get("level_name") or "").strip()
        what = "群等级" if kind == "group" else "好友等级"
        return f"{name}（{what}）" if name else "未分组"

    async def system_default_of(self, subject: Subject) -> str:
        """AstrBot 的**系统默认**提供商 id（本会话没被插件干预时会走的那个）。"""
        try:
            prov = await self._plugin.context.get_using_provider_async(subject.umo)
            if not prov:
                return ""
            return str((getattr(prov, "provider_config", {}) or {}).get("id") or "")
        except Exception:
            return ""

    def own_model_of(self, subject: Subject, scene: str) -> str:
        """这个**会话主体**的专属模型：私聊 = 他的、群聊 = 这个群的；没配返回空。

        与 ``main.route_model`` 的 ① 层同源（同一份内存缓存），卡片说的「专属模型」
        必须就是路由实际会走的那一层。
        """
        plugin = self._plugin
        if scene == "group":
            gid = str(subject.group_id or "")
            return str((getattr(plugin, "_subject_model_group", {}) or {}).get(gid) or "")
        uid = str(subject.sender_id or "")
        return str((getattr(plugin, "_subject_model", {}) or {}).get(uid) or "")

    async def current_of(
        self,
        subject: Subject,
        scene: str,
        available: Optional[set[str]] = None,
        system_default: Optional[str] = None,
        route: Optional[dict[str, Any]] = None,
    ) -> tuple[str, str]:
        """这个人**当前实际会走**哪个模型 → ``(provider_id, 来源文案)``。

        必须与 ``main.route_model`` 的顺序、以及「不可用就往下回落」的语义保持一致：
        某一层配了但提供商没加载 / 正在熔断时，实际走的是更粗的一层，
        这里若还报「当前使用 = 那个不可用的」，卡片就会撒谎。

        顺序（v1.3.12）：专属模型 > 等级路由 > 系统默认（旧的「用户自选」层已废弃）。

        Args:
            available: 当前可用的提供商集合；为 None 时自己算一遍。
            system_default / route: 已经算好的话就传进来，省一次查询与一次解析。
        """
        plugin = self._plugin
        if available is None:
            available = plugin.provider_ids() - plugin.circuit.open_ids()

        own = self.own_model_of(subject, scene)
        if own and own in available:
            return own, SOURCE_OWN
        if route is None:
            route = plugin.gate.resolve_model(subject, plugin._rules()) or {}
        if route:
            picked = plugin.gate.pick_provider(route, available)
            if picked:
                return (
                    str(picked["provider_id"]),
                    SOURCE_LEVEL_FALLBACK if picked.get("used_fallback") else SOURCE_LEVEL,
                )
        if system_default is None:
            system_default = await self.system_default_of(subject)
        return str(system_default or ""), SOURCE_DEFAULT

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    async def avatar_for(self, subject: Subject) -> Optional[bytes]:
        """卡片头像：**群里用群头像、私聊用对方头像**；取不到才退回插件 logo。

        为什么群里必须用群头像：那张卡片讲的是「这个会话走哪个模型」，
        而群聊的模型是按**群**定的（群等级）。挂发言人的头像会让人误以为
        「这是我个人的设置」，而且群里每个人发一次指令就换个头像，很跳。
        """
        kind = "group" if str(subject.group_id or "") else "user"
        tid = str(subject.group_id or subject.sender_id or "")
        cache = getattr(self._plugin, "avatars", None)
        if cache is None or not tid:
            return _logo_bytes()
        try:
            data = cache.read_any(kind, tid)
        except Exception:
            data = None
        if data:
            return data

        # 本地没缓存就现抓一次（抓完落盘，之后离线也有）。
        # 抓取有超时、失败有短时记忆：指令回执不能因为头像把用户晾在那边，
        # 离线环境也不该每发一次指令都空等一遍超时。
        key = f"{kind}:{tid}"
        now = time.time()
        if float(self._avatar_miss.get(key, 0) or 0) > now:
            return _logo_bytes()
        try:
            got = await asyncio.wait_for(cache.ensure(kind, [tid]), timeout=AVATAR_TIMEOUT)
            data = next(iter((got or {}).values()), None)  # 只传了一个 id，取第一个值即可
        except Exception:
            data = None
        if data:
            self._avatar_miss.pop(key, None)
            return data
        self._avatar_miss[key] = now + AVATAR_MISS_TTL
        if len(self._avatar_miss) > 200:  # 防御性清理，别长期攒着
            self._avatar_miss = {
                k: v for k, v in self._avatar_miss.items() if float(v or 0) > now
            }
        return _logo_bytes()

    def theme_for(self, tone: str = "") -> str:
        """卡片主题：给了**性质**（``error`` / ``alert`` / ``ok``）就强制用固定主题。

        固定的那几套与用户的主题偏好无关 —— 一张卡的第一眼信息该是「它是什么性质」
        （红的=报错、橙的=告警），而不是「我今天喜欢什么颜色」。
        没给性质就回落配置里的 ``model_card_theme``。
        """
        forced = model_card.TONE_THEMES.get(str(tone or "").strip().lower())
        if forced:
            return forced
        return str(self._plugin._cfg("model_card_theme", model_card.DEFAULT_THEME) or "")

    async def build_card(
        self,
        subject: Subject,
        data: dict[str, Any],
        *,
        title: str = "模型切换",
        rows: Optional[list[dict[str, Any]]] = None,
        footer: Optional[str] = None,
        meta_extra: str = "",
        tone: str = "",
    ) -> Optional[bytes]:
        """渲染卡片；渲染不出来返回 None（调用方退回文本列表）。

        ``title`` / ``rows`` / ``footer`` 三个参数是给 `/切换模型检测` 复用的：
        它要发同一张卡的两种形态（「正在检测…」无候选行、「检测完成」带最新数字），
        所以头部（当前模型 + 分组 + 今日用量）与样式只在这里拼一次。
        """
        plugin = self._plugin
        avatar = await self.avatar_for(subject)

        current_label = ""
        if data.get("current_id"):
            current_label = str(
                plugin.provider_info(str(data["current_id"])).get("label")
                or data["current_id"]
            )
        # 当前模型放在**标题区**（字号最大、最多两行、不截断）—— 它是这张卡片的主角；
        # 分组与今日用量合并成一行灰字，避免再开一块"信息条"把版面切碎。
        meta_parts = [f"分组：{self.group_label(data)}"]
        today = data.get("today") or {}
        if today:
            meta_parts.append(
                f"今日 {_fmt_num(today.get('tokens'))} tokens · "
                f"{_as_int(today.get('calls'), 0)} 次对话"
            )
        if meta_extra:
            meta_parts.append(str(meta_extra))

        return model_card.render_model_card(
            title=title,
            current=current_label,
            meta=" · ".join(meta_parts),
            rows=list(rows) if rows is not None else (data.get("options") or []),
            footer=self.footer_of(data) if footer is None else str(footer),
            avatar=avatar,
            font_path=str(plugin._cfg("model_card_font", "") or ""),
            theme=self.theme_for(tone),
        )

    async def build_success_card(self, subject: Subject, res: dict[str, Any]) -> Optional[bytes]:
        """切换成功的**回执小卡片**：头（现在用的是哪个）+ 脚（结果一句话），没有候选行。

        用户诉求：「切换成功也渲染当前使用的模型的图片，图片没出来就用文字兜底」。
        渲染失败返回 None，调用方退回 ``res['message']`` 文本 —— 功能不能挂在一张图上。
        """
        try:
            data = await self.describe(subject)
            current_label = ""
            if data.get("current_id"):
                current_label = str(
                    self._plugin.provider_info(str(data["current_id"])).get("label")
                    or data["current_id"]
                )
            meta_parts = [f"分组：{self.group_label(data)}"]
            today = data.get("today") or {}
            if today:
                meta_parts.append(
                    f"今日 {_fmt_num(today.get('tokens'))} tokens · "
                    f"{_as_int(today.get('calls'), 0)} 次对话"
                )
            return model_card.render_model_card(
                title="模型切换",
                current=current_label,
                meta=" · ".join(meta_parts),
                rows=[],  # 回执不讲候选，只讲「现在用哪个」
                footer="已切换，从下一条消息起生效。",
                avatar=await self.avatar_for(subject),
                font_path=str(self._plugin._cfg("model_card_font", "") or ""),
                theme=str(
                    self._plugin._cfg("model_card_theme", model_card.DEFAULT_THEME) or ""
                ),
            )
        except Exception:
            return None

    async def build_notice_card(self, subject: Subject, *, message: str, footer: str,
                                title: str = "切换模型检测",
                                tone: str = "alert") -> Optional[bytes]:
        """一张**只有头 + 脚**的提示卡（没有候选行）：用来回复「不是结果」的那类话。

        为什么这些提示也得是卡片：它们往往紧跟在另一张卡片后面出现（比如
        「检测中…」之后紧跟「其实有一轮在跑了」），一张图配一段字看起来像两件事；
        而用户刚点完指令，视觉上等的本来就是一张卡。
        注意头部的大字留给 ``message``（不是模型名）：这张卡没有「主角模型」，
        要读的就是这一句话。渲染失败返回 None，调用方退回纯文本。
        """
        try:
            return model_card.render_model_card(
                title=title,
                current=str(message or ""),  # 头部大字：这张卡的主角就是这句话
                meta="",
                rows=[],
                footer=str(footer or ""),
                avatar=await self.avatar_for(subject),
                font_path=str(self._plugin._cfg("model_card_font", "") or ""),
                # 提示卡**强制**用性质色：告警橙 / 报错红，与用户的主题偏好无关
                theme=self.theme_for(tone),
            )
        except Exception:
            return None

    def footer_of(self, data: dict[str, Any]) -> str:
        """卡片底部提示：能切就说怎么切，只读就说为什么切不了。

        开了自动撤回时把「多少秒后撤回」写出来（与有效期是同一个数字），
        用户才不会以为卡片是发丢了。
        """
        stale_n = len(data.get("stale_ids") or [])
        stale_hint = f" · {stale_n} 个配置已失效，请在控制台清理" if stale_n else ""
        if not data.get("can_switch"):
            reason = str(data.get("readonly_reason") or REASON_LEVEL_OFF)
            return READONLY_HINT.format(reason=reason) + stale_hint
        ttl = self.ttl()
        if self.recall_on():
            return f"回复序号切换（{ttl} 秒后自动撤回）· 0 = 恢复默认{stale_hint}"
        return f"回复序号切换 · {ttl} 秒内有效 · 0 = 恢复默认{stale_hint}"

    def text_list(self, data: dict[str, Any], ttl: int) -> str:
        """卡片渲染不出来时的纯文本兜底（功能不能因为画不出图就没了）。"""
        lines: list[str] = []
        if data.get("current_id"):
            lines.append(f"当前使用：{data['current_id']}")
        today = data.get("today") or {}
        if today:
            lines.append(
                f"今日用量：{_fmt_num(today.get('tokens'))} tokens · "
                f"{_as_int(today.get('calls'), 0)} 次对话"
            )
        lines.append("可用模型：" if not data.get("can_switch") else "可切换的模型：")
        for row in data.get("options") or []:
            tail = "（当前使用）" if row.get("current") else ("（专属模型）" if row.get("own") else "")
            mark = "（暂不可用）" if row.get("unavailable") else ""
            lines.append(f"{row.get('index')}. {row.get('label')}{tail}{mark}")
        if data.get("can_switch"):
            lines.append(f"回复序号即可切换（{ttl} 秒内有效）· 回复 0 = 恢复默认")
        else:
            reason = str(data.get("readonly_reason") or REASON_LEVEL_OFF)
            lines.append(READONLY_HINT.format(reason=reason))
        stale_n = len(data.get("stale_ids") or [])
        if stale_n:
            lines.append(f"另有 {stale_n} 个模型已被删除或禁用，已从名单移除，请在控制台清理。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 应用
    # ------------------------------------------------------------------ #
    async def apply(self, subject: Subject, index: int) -> dict[str, Any]:
        """按序号切换；``index=0`` 表示恢复默认（清掉自己的选择）。

        Returns:
            ``{ok, message}``：``message`` 是给用户看的一句话。
        """
        plugin = self._plugin
        item = self.peek(subject)
        if not item:
            return {"ok": False, "gone": True, "message": "选择已过期，请重新发送 /切换模型。"}
        # 只读的会话理论上不会被建立（handle_command 里不 remember），这里是二次防线：
        # 万一以后有人改成「只读也记会话」，也绝不会真的写库。
        if not item.get("can_switch"):
            reason = str(item.get("readonly_reason") or REASON_LEVEL_OFF)
            return {"ok": False, "message": f"{reason}，这次没有切换。"}
        options = list(item.get("options") or [])
        scene = str(item.get("scene") or "private")
        # 切换写的是「专属模型」这一把键（v1.3.12）：
        # 私聊 = policy(user, feature=model)、群聊 = policy(group, feature=model)。
        # 与控制台的「专属模型」是同一份数据，手动切换与指令切换互相看得见；
        # 也因为群聊切的是**全群**的模型，群里才只放管理员切。
        is_group = scene == "group"
        scope_type = "group" if is_group else "user"
        sid = str(subject.group_id or "") if is_group else str(subject.sender_id or "")
        if not sid:
            return {"ok": False, "message": "无法识别会话对象，切换失败。"}

        if index == 0:
            await plugin.store.set_policy(
                scope_type, sid, "inherit", feature=MODEL_FEATURE
            )
            await plugin.reload_rules()
            self.clear(subject)
            await plugin.store.log_audit(
                "chat", "model_switch_reset", f"{scope_type}:{sid} scene={scene}"
            )
            return {"ok": True, "message": "已恢复为分组配置的模型，从下一条消息起生效。"}

        if index < 1 or index > len(options):
            return {
                "ok": False,
                "message": f"序号超出范围，请输入 1 - {len(options)}（或 0 恢复默认）。",
            }
        picked = options[index - 1]
        pid = str(picked.get("provider_id") or "")
        if not pid:
            return {"ok": False, "message": "该选项不可用，请重新发送 /切换模型。"}
        # 卡片发出后到用户回序号之间，AstrBot 可能刚删 / 禁用过这个模型。
        # 这里再校验一次，避免把已失效的 id 写回专属模型（否则配置里又多一条死引用）。
        if pid not in plugin.provider_ids():
            self.clear(subject)
            return {
                "ok": False,
                "message": f"「{picked.get('label') or pid}」现在已不可用（可能被删除或禁用），"
                           "请重新发送 /切换模型 看最新名单。",
            }

        await plugin.store.set_policy(
            scope_type,
            sid,
            pid,
            feature=MODEL_FEATURE,
            note="群专属模型（对全群生效）" if is_group else "好友专属模型（仅私聊生效）",
        )
        await plugin.reload_rules()
        self.clear(subject)
        label = str(picked.get("label") or pid)
        await plugin.store.log_audit(
            "chat", "model_switch", f"{scope_type}:{sid} scene={scene} → {pid}"
        )
        note = "" if not picked.get("unavailable") else "（该模型当前未加载，等它可用后才会生效）"
        scope_note = "，本群已生效" if is_group else ""
        return {
            "ok": True,
            "provider_id": pid,
            "message": f"已切换为：{label}{note}\n从下一条消息起生效{scope_note}。",
        }

    # ------------------------------------------------------------------ #
    # 指令入口
    # ------------------------------------------------------------------ #
    async def handle_command(self, plugin: Any, event: Any, subject: Subject, arg: str = "") -> None:
        """``/切换模型 [序号]``：不带参数发卡片（只读时也发），带参数直接切。"""
        if not self.enabled():
            return
        if not (plugin.store and plugin.store.ready):
            await plugin._send(event, "插件数据未就绪，暂时无法切换模型。")
            return

        data = await self.describe(subject)
        # 给候选行补上「健康点 / 延迟 / 成功率 / 数据时间」（检测数据来自 model_panel）。
        # 拿不到那边就安静跳过：这张卡本身不依赖它，缺另一个插件不该让 /切换模型 变成报错。
        try:
            await plugin.detector.annotate(data)
        except Exception as e:
            logger.warning(f"[UserGateway] 补充模型检测指标失败（忽略）: {e}")
        if data.get("group_restricted") and not data.get("is_admin"):
            # 群聊仅管理员可用：不发卡片（需求就是「群里只有管理员能用」），
            # 但要说清去哪儿用，别让用户以为是插件坏了。
            await plugin._send(event, GROUP_REFUSE)
            return
        if not data.get("open"):
            # 连展示的东西都没有（没归级、也没任何候选）：说清楚而不是发一张空卡。
            # 如果恰好是「配的模型全被删 / 禁用了」，要点破原因，否则用户只会以为插件坏了。
            stale_n = len(data.get("stale_ids") or [])
            if stale_n:
                text = (
                    f"当前分组（{self.group_label(data)}）配置的 {stale_n} 个模型都已被删除或禁用，"
                    "没有可用模型了。请让管理员在控制台「等级与额度 → 失效模型引用」里清理，"
                    "或先在 AstrBot 里重新启用模型。"
                )
            else:
                text = f"当前分组（{self.group_label(data)}）没有可展示的模型信息，请联系管理员。"
            await plugin._send(event, text)
            return

        text = str(arg or "").strip()
        if text:
            try:
                index = int(text)
            except ValueError:
                index = -1
            if index >= 0:
                if not data.get("can_switch"):
                    # 只读：明确说不能切，不给「沉默失败」的错觉
                    await plugin._send(
                        event,
                        READONLY_HINT.format(
                            reason=str(data.get("readonly_reason") or REASON_LEVEL_OFF)
                        ),
                    )
                    return
                # 带参数 = 直接切（不用先发卡片）：先记下序号表再套用
                self.remember(
                    subject,
                    data.get("options") or [],
                    str(data.get("scene") or "private"),
                    can_switch=True,
                    reason="",
                )
                res = await self.apply(subject, index)
                if res.get("ok"):
                    # 成功回执默认用图片（只讲「现在用哪个」的小卡片），画不出再退文字
                    card = await self.build_success_card(subject, res)
                    if card is not None and await plugin._send_image(event, card):
                        return
                await plugin._send(event, res.get("message", ""))
                return
            await plugin._send(event, "参数要填序号（如 /切换模型 2），或直接发 /切换模型 看列表。")
            return

        if data.get("can_switch"):
            # 只有能切时才记序号会话：只读时随手发的数字不该被这条指令截走
            self.remember(
                subject,
                data.get("options") or [],
                str(data.get("scene") or "private"),
                can_switch=True,
                reason="",
            )
        card = await self.build_card(subject, data)
        if card is not None and await plugin._send_image(
            event, card, recall_sec=self.recall_sec()
        ):
            return
        # 渲染不出来 / 图片发不出去 → 退化成文本列表（功能不能因为一张图就没了；
        # 文本兜底同样不撤回：它可能带着用户唯一能看到的信息）
        await plugin._send(event, self.text_list(data, self.ttl()))

    async def handle_index(self, plugin: Any, event: Any, subject: Subject) -> bool:
        """把一条「纯数字」消息当作序号处理；不是待选会话则返回 False（交还给正常流程）。

        Returns:
            True = 已处理（调用方应 stop 事件，避免数字又被当成聊天内容送进 LLM）。
        """
        if not self.enabled():
            return False
        if self.peek(subject) is None:
            return False
        try:
            index = int(str(event.get_message_str() or "").strip())
        except Exception:
            return False
        if index < 0:
            return False
        try:
            res = await self.apply(subject, index)
        except Exception:
            logger.exception("[UserGateway] 处理模型切换序号失败（忽略）")
            return False
        if res.get("ok"):
            # 成功回执默认用图片（只讲「现在用哪个」的小卡片），画不出再退文字
            try:
                card = await self.build_success_card(subject, res)
            except Exception:
                card = None
            if card is not None and await plugin._send_image(event, card):
                return True
        await plugin._send(event, str(res.get("message") or ""))
        return True
