"""「切换模型检测」：给当前分组允许使用的模型做一次存活 / 延迟 / 成功率体检。

三条分工（改动前先读，这是本模块存在的全部理由）：

1. **检测不在本插件实现**。真打模型这一步调 ``astrbot_plugin_model_panel`` 的
   ``external_detect()``，结果也由它落库（面板上记为「插件联动」）。本插件只负责
   挑哪些模型、什么时候能测、发什么卡片。探测口径只此一份 —— 两边各写一套的话，
   同一个模型会出现「你那边绿、我这边红」，而没有人知道该信谁。
2. **两道冷却都配在本插件**。指令冷却（防同一个人狂点，默认 30 分钟、管理员豁免）
   与模型冷却（防同一个模型被打爆，默认 5 分钟）都读本插件的配置：
   model_panel 的指令目前一律管理员限定，限流是「面向普通用户」的需求，
   配置项跟着用户向的这一侧走，才会有人真的去调它。
3. **没有 model_panel 就明说不可用**。不静默失败、也不自己另起一套探测 ——
   功能挂在对方身上，就该把这件事和安装地址一起说清楚。

模型冷却为什么不自己记时间：``external_snapshot()`` 里的 ``last_probe_ts``
就是「这个模型上次被探测是什么时候」（对面落库的），拿它算冷却跨用户、跨群、
重启都不丢；自己存一份只会多出一种「两边不一致」的可能。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from astrbot.api import logger

# 检测逻辑的提供方（萌萌模型控制台）。名字要与对方的 metadata.yaml / @register 一致。
PANEL_NAME = "astrbot_plugin_model_panel"
PANEL_REPO = "https://github.com/windExplorer/astrbot_plugin_model_panel"

PANEL_MISSING_HINT = (
    "这个功能需要先安装「萌萌模型控制台」插件（astrbot_plugin_model_panel）：\n"
    f"{PANEL_REPO}\n"
    "装好并启用后再试一次。检测本身由它执行、结果也存在它那边，本插件只负责发起与展示。"
)

DETECT_DISABLED_HINT = (
    "当前分组没有开放「切换模型检测」。\n"
    "这是管理员在控制台的「分组」里逐个打开的开关（默认关），"
    "因为它会真打模型、消耗额度；急着看状态可以先发 /切换模型。"
)

GROUP_REFUSE = "群聊里只有管理员能检测模型，私聊我随时可以帮你测～"


def _fmt_ms(value: Any) -> str:
    """延迟：一律按毫秒原样给（与面板同口径），拿不到给空串（整组指标不画）。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    ms = int(round(n))
    if ms < 60_000:
        return f"{ms}ms"
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}分{seconds}秒" if minutes < 60 else f"{minutes // 60}时{minutes % 60}分"


def _fmt_rate(value: Any) -> str:
    """成功率：**没有样本时给空串**，不显示 100%（那是「没数据」不是「全对」）。"""
    try:
        if value is None:
            return ""
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return ""


def _fmt_ago(ts: Any, now: Optional[float] = None) -> str:
    """时间戳 → 「刚刚 / 3 分钟前 / 2 小时前 / 3 天前」；拿不到给空串。

    卡片上写相对时间而不是「09-22 14:05」：用户要看的是「这条数据还新鲜吗」，
    而绝对时间还得他自己跟当前时刻做一次减法。
    """
    try:
        t = int(float(ts or 0))
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    delta = max(0.0, float(now if now is not None else time.time()) - t)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    return time.strftime("%m-%d", time.localtime(t))


# 错误码 → 人话。与 model_panel 的 _ERROR_LABELS 保持同一套说法：
# 同一个故障在两个插件的卡片上叫两个名字，读的人会以为是两件事。
_ERROR_LABELS = {
    "timeout": "超时",
    "refused": "拒绝连接",
    "connect": "连接失败",
    "auth": "鉴权失败",
    "rate_limit": "请求受限",
    "not_found": "模型不存在",
    "server": "服务异常",
    "unknown": "未知异常",
}


def _error_label(code: Any) -> str:
    """内部错误码 → 卡片上的说法。空码返回空串（不编「未知异常」骗人）。"""
    key = str(code or "").strip()
    if not key:
        return ""
    return _ERROR_LABELS.get(key, "异常")


class ModelDetector:
    """``/切换模型检测`` 的状态机 + 与 model_panel 的桥（一个插件实例一个）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        # 指令冷却：``{umo|sender_id: 上次真正开始检测的时间戳}``。
        # 不用 gate.Cooldown 是因为它只回答「能不能」，而这里还要告诉用户
        # 「还要等几分钟」；冷却时长又要能随时改配置，所以自己算更直接。
        self._last: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def enabled(self) -> bool:
        return bool(self._plugin._cfg("detect_command_enabled", True))

    def command_cooldown_sec(self) -> int:
        """非管理员的指令冷却（秒）。``0`` = 不冷却。"""
        try:
            mins = int(self._plugin._cfg("detect_command_cooldown_min", 30) or 0)
        except (TypeError, ValueError):
            mins = 30
        return max(0, mins) * 60

    def model_cooldown_sec(self) -> int:
        """同一个模型两次检测之间的最小间隔（秒）。``0`` = 不限制。"""
        try:
            mins = int(self._plugin._cfg("detect_model_cooldown_min", 5) or 0)
        except (TypeError, ValueError):
            mins = 5
        return max(0, mins) * 60

    def max_models(self) -> int:
        """单次最多检测几个模型。``0`` = **不限**（默认），一次测完分组里的全部可用模型。

        为什么默认改成不限：限制的初衷是防成本失控，但实际效果是「分组里 15 个模型、
        只测了前 8 个」，而用户要的恰恰是**一眼看完这一组到底谁不健康** ——
        剩下的 7 个还得再等下一个冷却周期才能看到，等于把一次体检拆成两次。
        真正管住成本的是那两道冷却（人 30 分钟、模型 5 分钟），不是这个截断。
        真要限量时配一个正整数即可（上限 100，纯属防御性）。
        """
        try:
            n = int(self._plugin._cfg("detect_max_models", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        return 0 if n <= 0 else min(100, n)

    def probe_plan(self) -> tuple[int, float]:
        """对面的探测并发数与单模型超时，用来估「最坏要等多久」。

        并发由 model_panel 定（它的 ``external_available()`` 会报），拿不到就按 3 估 ——
        估算可以保守（说「≤5 分钟」实际 2 分钟），但不能反过来。
        """
        conc, timeout = 3, 45.0
        panel = self.panel()
        info = None
        if panel is not None:
            try:
                info = panel.external_available()
            except Exception:
                info = None
        if isinstance(info, dict):
            try:
                conc = max(1, int(info.get("probe_concurrency") or conc))
            except (TypeError, ValueError):
                pass
            try:
                timeout = max(1.0, float(info.get("probe_timeout") or timeout))
            except (TypeError, ValueError):
                pass
        return conc, timeout

    def eta_text(self, n: int) -> str:
        """``n`` 个模型最坏要等多久（并发跑，所以按轮数算，不是 n × 超时）。"""
        if n <= 0:
            return ""
        conc, timeout = self.probe_plan()
        secs = max(1.0, float(-(-n // conc)) * timeout)
        if secs < 90:
            return f"约 {int(secs)} 秒"
        return f"约 {int(round(secs / 60))} 分钟"

    def admin_exempt(self) -> bool:
        """管理员是否豁免「分组开关 + 指令冷却」（与既有的 admin_exempt 同一个键）。"""
        return bool(self._plugin._cfg("admin_exempt", True))

    # ------------------------------------------------------------------ #
    # 与 model_panel 的桥
    # ------------------------------------------------------------------ #
    def panel_busy(self) -> Optional[bool]:
        """对面此刻是否有检测在跑。``None`` = **不知道**（对面版本旧、没报这个字段）。

        三态而不是 bool：不知道时不能当成「不忙」也不能当成「忙」——
        前者会照旧发出「检测中」卡片（老行为，可接受），后者会把功能整个拒掉。
        """
        panel = self.panel()
        if panel is None:
            return None
        try:
            info = panel.external_available()
        except Exception:
            return None
        if not isinstance(info, dict) or "busy" not in info:
            return None
        return bool(info.get("busy"))

    def panel(self) -> Optional[Any]:
        """取 model_panel 的插件实例；没装 / 被禁用 / 版本太老都返回 ``None``。

        版本太老也算「没有」：对面的 ``external_detect`` 是新加的，
        装了旧版却在调用时抛 ``AttributeError`` 是最难查的一类报错 ——
        不如在这里就判成不可用，让用户看到「请升级/安装」的提示。
        """
        try:
            meta = self._plugin.context.get_registered_star(PANEL_NAME)
        except Exception:
            meta = None
        inst = getattr(meta, "star_cls", None) if meta is not None else None
        if inst is None:
            return None
        if not callable(getattr(inst, "external_detect", None)):
            logger.warning(
                "[UserGateway] 检测到 model_panel 但没有 external_detect 接口"
                "（版本过旧），按不可用处理"
            )
            return None
        return inst

    async def snapshot(self, provider_ids: list[str], days: float = 0.0) -> dict[str, dict]:
        """读对面已有的记录（只读、不花钱）。对面不可用或出错一律返回空表。"""
        panel = self.panel()
        if panel is None or not provider_ids:
            return {}
        try:
            res = await panel.external_snapshot(list(provider_ids), days=days)
        except Exception as e:
            logger.warning(f"[UserGateway] 读取 model_panel 快照失败（忽略）: {e}")
            return {}
        return (res.get("items") or {}) if isinstance(res, dict) and res.get("ok") else {}

    async def annotate(self, data: dict[str, Any],
                       probe_of: Optional[dict[str, dict]] = None) -> None:
        """给候选行补上「状态 / 延迟 / 成功率 / 数据时间」，有本次探测结果时再补「本次结论」。

        两套数字必须分清，这是用户提过的问题：「测出来是红的，却有毫秒显示、成功率 2.3%，
        不知道最新那次到底是报错还是通过」——
        ``latency`` / ``rate`` 来自**已有记录**（可能是几小时前那次成功、和今天一整天窗口的
        成功率），跟「我刚刚打的这一次」不是一回事。所以：

        - 有本次结果时，延迟改用**本次探测**的那个数（失败就一个数字都不画）；
        - 另给 ``probe_ok`` / ``probe_label``，卡片在行尾画一颗「本次通过 / 本次失败」的标签 ——
          这是用户第一眼要找的答案；
        - 失败的原因（超时 / 鉴权失败…）写进副标题；
        - 成功率一律标注**统计范围**（``今日 2.3%``），不再是一个孤零零的百分数。

        **拿不到数据就整组不画**（而不是画一排「-」）：一列占位符比空着更难看。
        没装 model_panel 时这里安静跳过 —— 缺另一个插件不该让 /切换模型 变成一个报错。
        """
        options = [o for o in (data.get("options") or []) if o.get("provider_id")]
        if not options:
            return
        probe_of = probe_of or {}
        items = await self.snapshot([str(o["provider_id"]) for o in options])
        if not items and not probe_of:
            return
        for opt in options:
            pid = str(opt.get("provider_id") or "")
            it = items.get(pid) or {}
            probe = probe_of.get(pid)
            if not it and probe is None:
                continue
            # 状态用对面**写回状态机之后**的值：本次失败的模型在这一步已经是 down，
            # 所以行底会跟着变红（这正是「测完就看得见」的意思）。
            opt["health"] = str(it.get("state") or "")
            rate = _fmt_rate(it.get("success_rate"))
            opt["rate"] = f"今日 {rate}" if rate else ""
            note = str(opt.get("note") or "")
            if probe is None:
                opt["latency"] = _fmt_ms(it.get("latency_ms"))
                stamp = _fmt_ago(it.get("last_ts"))
                if stamp:
                    # 时间放**最前**：它是「后面这些数字有多新」的限定语，
                    # 放末尾会被 _fit 先截掉（note 过长时），那就等于没写。
                    opt["note"] = f"{stamp} · {note}" if note else stamp
                continue
            ok = bool(probe.get("ok"))
            opt["probe_ok"] = ok
            opt["probe_label"] = "本次通过" if ok else "本次失败"
            if ok:
                # 通过：显示**本次**的延迟（和标签挨着，读起来就是「本次通过、耗时 812ms」）
                opt["latency"] = _fmt_ms(probe.get("latency_ms"))
            else:
                # 失败：一个延迟都不画。留着上一次成功的毫秒数，
                # 正是「红的却显示 400ms」那种把人绕晕的根源。
                opt["latency"] = ""
                label = _error_label(probe.get("error_code"))
                if label:
                    opt["note"] = f"{label} · {note}" if note else label
        data["metrics_available"] = True

    # ------------------------------------------------------------------ #
    # 冷却
    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(subject: Any) -> str:
        return f"{subject.umo}|{subject.sender_id}"

    def cooldown_left(self, subject: Any, now: Optional[float] = None) -> int:
        """指令冷却还剩几分钟（向上取整）；``0`` = 现在就能测。"""
        cd = self.command_cooldown_sec()
        if cd <= 0:
            return 0
        now = now if now is not None else time.time()
        last = self._last.get(self._key(subject))
        if last is None:
            return 0
        left = cd - (now - last)
        return max(0, int((left + 59) // 60)) if left > 0 else 0

    def mark(self, subject: Any, now: Optional[float] = None) -> None:
        """记一次「开始检测」。只在**真的要跑**时调用（被冷却跳过的那些不消耗）。"""
        self._purge(now)
        self._last[self._key(subject)] = now if now is not None else time.time()

    def unmark(self, subject: Any) -> None:
        """撤销一次已经记下的冷却。

        给「撞车」用：开跑前记了冷却，结果对面说已经有一轮在跑 ——
        那一轮**压根没打模型**，把冷却扣在用户头上就是冤枉他
        （别人触发的一轮检测不该让下一个人白等 30 分钟）。
        """
        self._last.pop(self._key(subject), None)

    def _purge(self, now: Optional[float] = None) -> None:
        """清掉已过期的冷却记录，防止长期运行内存只增不减。"""
        cd = self.command_cooldown_sec()
        now = now if now is not None else time.time()
        if cd <= 0:
            self._last.clear()
            return
        for key in [k for k, ts in self._last.items() if now - ts >= cd]:
            self._last.pop(key, None)

    def _split(self, options: list[dict], items: dict[str, dict]) -> tuple[list[dict], list[dict]]:
        """按「同一模型 N 分钟内不重复测」把候选分成 ``(要测, 跳过)``。

        依据是对面落库的 ``last_probe_ts``：**只有探测会更新它**，
        真实对话不算（对话本来就随时在发生，不能拿它当「刚测过」）。
        """
        cd = self.model_cooldown_sec()
        now = time.time()
        todo: list[dict] = []
        skipped: list[dict] = []
        for opt in options:
            pid = str(opt.get("provider_id") or "")
            ts = 0
            try:
                ts = int(float((items.get(pid) or {}).get("last_probe_ts") or 0))
            except (TypeError, ValueError):
                ts = 0
            if cd > 0 and ts and (now - ts) < cd:
                opt["probed_ago"] = _fmt_ago(ts, now)
                skipped.append(opt)
            else:
                todo.append(opt)
        return todo, skipped

    # ------------------------------------------------------------------ #
    # 指令入口
    # ------------------------------------------------------------------ #
    async def _notice(self, plugin: Any, event: Any, subject: Any, message: str,
                      footer: str, tone: str = "alert") -> None:
        """发一张提示卡（只有头 + 脚）；渲染不出来退回纯文本。

        「不是结果」的那些话（撞车 / 冷却里被跳过前的提醒）也走卡片：
        它们紧跟在别的卡片前后出现，图文混排看起来像两件事。

        ``tone`` 决定**固定主题**：``alert``（默认，橙）= 需要你处理的通知，
        ``error``（红）= 真的出错了。两种都不是用户主题偏好能改的 ——
        看的人要能一眼分清「这是提醒」还是「这是报错」。
        """
        card = None
        try:
            card = await plugin.switcher.build_notice_card(
                subject, message=message, footer=footer, tone=tone)
        except Exception as e:
            logger.warning(f"[UserGateway] 提示卡渲染失败（退回文本）: {e}")
        if card is not None and await plugin._send_image(event, card):
            return
        await plugin._send(event, f"{message}\n{footer}")

    async def handle_command(self, plugin: Any, event: Any, subject: Any) -> None:
        """``/切换模型检测``：先回一张「检测中」提示卡，跑完再发最新的切换卡。"""
        if not self.enabled():
            return
        if not (plugin.store and plugin.store.ready):
            await plugin._send(event, "插件数据未就绪，暂时无法检测。")
            return
        if self.panel() is None:
            await plugin._send(event, PANEL_MISSING_HINT)
            return

        data = await plugin.switcher.describe(subject)
        is_admin = bool(data.get("is_admin"))
        if data.get("group_restricted") and not (is_admin and self.admin_exempt()):
            await plugin._send(event, GROUP_REFUSE)
            return
        if not (is_admin and self.admin_exempt()):
            if not plugin.gate.detect_enabled(plugin._rules(), data.get("level_id")):
                await plugin._send(event, DETECT_DISABLED_HINT)
                return
        if not data.get("open"):
            await plugin._send(
                event,
                f"当前分组（{plugin.switcher.group_label(data)}）没有可检测的模型，"
                "请联系管理员配置。",
            )
            return

        if not (is_admin and self.admin_exempt()):
            left = self.cooldown_left(subject)
            if left > 0:
                await plugin._send(
                    event,
                    f"检测有 {self.command_cooldown_sec() // 60} 分钟冷却，请约 {left} 分钟后再试～\n"
                    "（只想看看现在的状态，发 /切换模型 就行，不花钱）",
                )
                return

        options = [o for o in (data.get("options") or []) if o.get("provider_id")]
        # 防御：describe() 已经把失效 id 剔掉了，这里再按「当前已加载」过一遍，
        # 免得将来有人从缓存 / 旧会话拿到 options 时，把已删除或禁用的模型送去
        # 真打一遍（既白花钱，对方也只会返回探测失败）。
        known = None
        getter = getattr(plugin, "provider_ids", None)
        if callable(getter):
            try:
                known = set(getter() or ())
            except Exception:
                known = None
        if known:
            options = [o for o in options if str(o["provider_id"]) in known]
        if not options:
            await self._notice(
                plugin, event, subject,
                "当前分组没有可检测的模型",
                "配置的模型可能已被删除或禁用，请让管理员在控制台清理后重试",
            )
            return
        items = await self.snapshot([str(o["provider_id"]) for o in options])
        todo, skipped = self._split(options, items)
        # cap = 0（默认）= 不限：一次把分组里的可用模型测完（见 max_models 的说明）。
        # 截断只在用户**明确配了上限**时发生，且必须在头部说明 —— 否则会被当成「漏测」。
        cap = self.max_models()
        truncated = len(todo) - cap if cap and len(todo) > cap else 0
        if truncated:
            todo = todo[:cap]
            logger.info(
                f"[UserGateway] 检测按配置上限 {cap} 截断（分组可选 {len(options)} 个，"
                f"本次待测 {len(todo) + truncated} 个）"
            )
        if not todo:
            # 卡片脚部只有一行，名字列多了会被 _fit 截掉 —— 只点三个，其余报个数
            names = "、".join(str(o.get("label") or o.get("provider_id")) for o in skipped[:3])
            if len(skipped) > 3:
                names += f" 等 {len(skipped)} 个"
            await self._notice(
                plugin, event, subject,
                f"这些模型 {self.model_cooldown_sec() // 60} 分钟内刚测过，不重复打（省额度）",
                f"{names} · 直接发 /切换模型 看它们现在的状态就好",
            )
            return

        # 对面已经有一轮在跑时**先别发「检测中」**：发了之后紧接着又说「其实没跑」，
        # 两句自相矛盾（用户就是这么反馈的）。能提前知道是因为对面把锁状态报给了我们；
        # 万一这只是瞬时快照、真正开跑时还是撞上了，_run 里还有一道兜底。
        if self.panel_busy() is True:
            await self._notice(
                plugin, event, subject,
                "模型控制台那边已经有一轮检测在跑了",
                "等那一轮跑完再发一次 /切换模型检测（同一时间只跑一轮，避免两边互相拖超时）",
            )
            return

        if not (is_admin and self.admin_exempt()):
            self.mark(subject)

        # ① 立刻回一张「检测中」提示卡：检测是后台跑的，不回一句用户会以为 bot 没反应。
        #    带上「最坏要等多久」：默认不限量之后，一次就是分组里全部模型，没有 ETA 会像卡死。
        n = len(todo)
        eta = self.eta_text(n)
        meta_extra = f"正在检测 {n} 个模型" + (f" · 最坏 {eta}" if eta else "")
        if truncated:
            meta_extra += f"（按配置只测前 {cap} 个，还有 {truncated} 个没测）"
        progress = await plugin.switcher.build_card(
            subject, data, title="切换模型检测", rows=[],
            footer="检测中…结果出来后会自动发一张最新的卡片",
            meta_extra=meta_extra,
        )
        if progress is None or not await plugin._send_image(event, progress):
            await plugin._send(event, f"正在检测 {len(todo)} 个模型，稍等…")

        # ② 后台跑完再发最新卡片：单模型最坏要等一个超时，同步等会让用户以为 bot 挂了。
        # 目标就在上面定好了、原样传过去，**不在后台再算一遍** ——
        # 两处各算一次的话，用户在「检测中」看到的数量可能和实际打的对不上。
        asyncio.create_task(
            self._run(plugin, event, subject,
                      pids=[str(o["provider_id"]) for o in todo], skip_n=len(skipped))
        )

    async def _run(self, plugin: Any, event: Any, subject: Any, *,
                   pids: list[str], skip_n: int = 0) -> None:
        """后台：调对面检测 → 重新取数 → 发一张带最新数字的切换卡。"""
        panel = self.panel()
        if panel is None:
            await plugin._send(event, PANEL_MISSING_HINT)
            return
        if not pids:
            await plugin._send(event, "没有要检测的模型。")
            return
        try:
            res = await panel.external_detect(pids)
        except Exception as e:
            logger.warning(f"[UserGateway] 调用 model_panel 检测失败: {e}")
            res = {"ok": False, "error": str(e)}
        if not isinstance(res, dict) or not res.get("ok"):
            if isinstance(res, dict) and res.get("busy"):
                # 开跑前没探到忙、真正开跑时撞上了（快照过期 / 恰好同时点）。
                # 这一轮一个模型都没打，所以**把冷却撤回来**：别人触发的那轮不该让这个人白等。
                self.unmark(subject)
                await self._notice(
                    plugin, event, subject,
                    "模型控制台那边已经有一轮检测在跑了",
                    "这一轮没有检测任何模型 · 等它跑完再发一次 /切换模型检测",
                )
                return
            # 真出错才用**红卡**：红色在两张卡上是同一个意思 —— 「这不是提醒，是出问题了」。
            # 与「撞车」那条橙卡的区分就在这里：橙 = 等一会儿就好，红 = 得看看为什么。
            await self._notice(
                plugin, event, subject, "检测失败",
                f"{(res or {}).get('error') or '未知原因'} · 这一轮没有结果，可稍后再试",
                tone="error",
            )
            return

        results = [r for r in (res.get("results") or []) if isinstance(r, dict)]
        bad = [r for r in results if not r.get("ok")]
        # 本次每一行的结论：卡片要把它画成「本次通过 / 本次失败」，
        # 并与库里那些历史数字区分开（用户提过：红的行上还挂着毫秒和成功率，看不出到底过没过）。
        probe_of = {str(r.get("id")): r for r in results if r.get("id")}
        logger.info(
            f"[UserGateway] 切换模型检测完成：{len(pids)} 个模型，"
            f"失败 {len(bad)}（{subject.group_id or subject.sender_id}）"
        )

        # 重新 describe + 注解：此刻的数字才是刚打出来的（而不是发卡片前那一刻的快照）
        try:
            fresh = await plugin.switcher.describe(subject)
            await self.annotate(fresh, probe_of)
        except Exception as e:
            logger.warning(f"[UserGateway] 检测后刷新卡片数据失败: {e}")
            await self._notice(
                plugin, event, subject, "检测跑完了，但取最新数据失败",
                f"{e} · 模型本身已经测过（结果在模型控制台里），只是这张卡没生成出来",
                tone="error",
            )
            return
        if fresh.get("can_switch"):
            # 让用户看完就能直接回序号切换（否则刚测完还得再发一次 /切换模型）
            plugin.switcher.remember(
                subject, fresh.get("options") or [], str(fresh.get("scene") or "private"),
                can_switch=True, reason="",
            )
        summary = f"本次检测：{len(results) - len(bad)} 通过"
        if bad:
            summary += f" / {len(bad)} 失败"
        if skip_n:
            # 跳过的也写在头部：否则用户会觉得「我分组里有 5 个，怎么只测了 3 个」
            summary += f"（跳过 {skip_n} 个刚测过的）"
        card = await plugin.switcher.build_card(
            subject, fresh, title="模型切换", meta_extra=summary,
        )
        if card is not None and await plugin._send_image(
            event, card, recall_sec=plugin.switcher.recall_sec()
        ):
            return
        await plugin._send(event, f"{summary}\n{plugin.switcher.text_list(fresh, plugin.switcher.ttl())}")
