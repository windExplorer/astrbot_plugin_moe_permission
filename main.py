"""萌萌权限控制台：按好友 / 按群管控 LLM 权限与 token 额度，并提供用量统计。

实现依据见工作区文档 ``docs/astrbot_plugin_moe_permission_PRD.md``。

当前进度（M0 骨架）：
- 插件生命周期：数据目录、SQLite 建库、配置读取、控制台路由注册
- 控制台后端接口：健康检查 / 配置读写 / 总览统计 / 好友群列表 / 额度 / 明细
- **LLM 闸门与好友群同步在 M1 接入**（本文件已预留 ``reload_rules()`` 与内存规则缓存）

设计红线（改动时务必保持）：
1. **fail-open**：本插件任何异常都必须放行消息，绝不能把用户消息吞掉。
2. 判定走内存缓存（``_effect_*`` / ``_quota_*``），只有写操作后 ``reload_rules()`` 重建，
   保证闸门热路径不做磁盘 / SQL 查询。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter as astr_filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register

try:  # 钩子类型仅用于注解；缺失不影响运行
    from astrbot.api.provider import LLMResponse, ProviderRequest
except ImportError:  # pragma: no cover
    LLMResponse = Any  # type: ignore
    ProviderRequest = Any  # type: ignore

# 插件目录加进 sys.path：本地直接跑 main.py 调试时，平铺导入才能生效
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def _reload_sibling_modules() -> None:
    """热更新时强制重载本插件的同级模块（必须在下面的 ``from .x import y`` 之前执行）。

    为什么需要：AstrBot 的热更新（watchfiles）只重载插件主模块 ``main.py``，**不会**级联重载
    依赖模块。从已装的旧版本热更上来时，``sys.modules`` 里还是上一次加载的 store / gate，
    下面那些 ``from .store import Store`` 拿到的仍旧是旧代码，表现是「新功能静默不生效」
    —— 最典型的就是 store 的新迁移不跑、新加的列写不进去（配置界面存了、闸门却不认）。

    这里先把**已经加载过**的同级模块 reload 一遍，再让下面的 import 绑定到新代码上。
    全新启动时这些模块还没进 ``sys.modules``，循环自然是空转，不影响冷启动。
    """
    import importlib

    # 按依赖顺序（被依赖者在前）：gate / store 是底座，webui_api 依赖它们。
    for name in (
        "rename_migration",
        "provider_refs",
        "quota",
        "gate",
        "store",
        "avatar",
        "sync",
        "model_card",
        "recall",
        "detect",
        "model_switch",
        "webui_api",
    ):
        full = f"{__package__}.{name}" if __package__ else name
        if full not in sys.modules:
            continue
        try:
            importlib.reload(sys.modules[full])
        except Exception as e:
            logger.warning(f"[UserGateway] 同级模块 {name} 强制重载失败（沿用已加载的旧代码）: {e}")


_reload_sibling_modules()

try:  # 包内相对导入（AstrBot 正常加载路径）
    from . import quota as quota_mod
    from . import provider_refs as provider_refs_mod
    from .rename_migration import migrate_renamed_plugin
    from .avatar import AvatarCache
    from .gate import (
        Cooldown,
        Gate,
        ProviderCircuit,
        Rules,
        Subject,
        effect_in_scene,
        layer_label,
    )
    from .detect import ModelDetector
    from .model_switch import ModelSwitcher
    from .recall import Recaller
    from .store import (
        COMMAND_MASTER_FEATURE,
        MODEL_FEATURE,
        Store,
        member_scope_id,
        parse_member_scope_id,
        parse_provider_list,
    )
    from .sync import SyncScheduler
    from .webui_api import register_apis
except ImportError as _rel_err:
    # 相对导入失败必须区分两种原因，否则会抛出误导性的错误：
    #   1) 本模块不是以「包」的形式被加载（本地直接 `python main.py`）→ 平铺导入兜底即可；
    #   2) **发布包缺文件**（build_zip.ps1 的 $includeList 漏了新模块）→ 平铺导入必然报
    #      "No module named 'quota'"，看起来像代码问题，其实是打包漏了文件。
    # v0.2.0 就真的这么翻车过，所以这里把两种错误都原样带出来。
    try:
        import quota as quota_mod  # type: ignore
        import provider_refs as provider_refs_mod  # type: ignore
        from rename_migration import migrate_renamed_plugin  # type: ignore
        from avatar import AvatarCache  # type: ignore
        from gate import (  # type: ignore
            Cooldown,
            Gate,
            ProviderCircuit,
            Rules,
            Subject,
            effect_in_scene,
            layer_label,
        )
        from detect import ModelDetector  # type: ignore
        from model_switch import ModelSwitcher  # type: ignore
        from recall import Recaller  # type: ignore
        from store import (  # type: ignore
            COMMAND_MASTER_FEATURE,
            MODEL_FEATURE,
            Store,
            member_scope_id,
            parse_member_scope_id,
            parse_provider_list,
        )
        from sync import SyncScheduler  # type: ignore
        from webui_api import register_apis  # type: ignore
    except ImportError as _flat_err:
        raise ImportError(
            "萌萌权限控制台：子模块导入失败。"
            f"相对导入报错 {_rel_err!r}；平铺导入报错 {_flat_err!r}。"
            "若报错是 No module named 'gate' / 'quota' / 'sync' / 'avatar' / 'model_switch' / 'model_card' / 'recall' / 'detect'，"
            "说明**安装包少了文件**（打包脚本 build_zip.ps1 的 $includeList 未同步新增模块），"
            "请用仓库里最新的 zip 重新安装，或把缺失的 .py 补进插件目录。"
        ) from _flat_err

PLUGIN_NAME = "astrbot_plugin_moe_permission"

# 当前生效的插件实例（发送钩子要用它拿 store）。
# 用模块级变量而不是闭包捕获插件实例：插件热重载时旧实例会被替换，
# 闭包会一直抓着已经 terminate() 的旧实例（store 已关闭）。
_ACTIVE_PLUGIN: "Optional[UserGatewayPlugin]" = None


def _read_plugin_version() -> str:
    """从 metadata.yaml 读取版本号（单一来源），失败时降级为 0.0.0。"""
    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(encoding="utf-8")
        for line in text.splitlines():
            m = re.match(r"\s*version\s*:\s*v?([0-9][^\s#]*)", line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "0.0.0"


@register(
    PLUGIN_NAME,
    "windExplorer",
    "按好友 / 按群细粒度管控 LLM 使用权限与 token 额度，并提供用量统计控制台。",
    _read_plugin_version(),
)
class UserGatewayPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        # AstrBot 注入的插件配置（AstrBotConfig，dict 子类，带 save_config()）
        self.config = config or {}
        # 改名迁移要读 AstrBotConfig 的 config_path / first_deploy / save_config()；
        # 上面 `or {}` 在配置对象为空时会退化成普通 dict、丢掉这些属性，故留原对象。
        self._raw_config = config
        self.plugin_version = _read_plugin_version()

        # 数据目录与数据库（initialize() 中创建）
        self.data_dir: Path = Path(".")
        self.store: Optional[Store] = None

        # 闸门热路径用的内存规则缓存（reload_rules() 重建）
        # 值是 {scope_id: {scene: effect}}（v6 起带场景维度，见 gate.Rules 的说明）
        self._effect_user: dict[str, dict[str, str]] = {}
        self._effect_group: dict[str, dict[str, str]] = {}
        # 群成员专属：「群号:QQ」→ {scene: effect}（v7）
        self._effect_member: dict[str, dict[str, str]] = {}
        # 等级：{level_id: effect}、{scope_type: {scope_id: level_id}}
        # 等级键用 id 而非 (kind, id)：id 是 quota_level 的主键、全局唯一，
        # kind 只是控制台的分组标签（好友等级 / 群聊等级），判定不需要它。
        self._level_effect: dict[int, str] = {}
        self._subject_level_user: dict[str, int] = {}
        self._subject_level_group: dict[str, int] = {}
        # 好友的群聊专属等级（v8）+ 好友专属模型（feature=model，仅私聊生效）
        self._subject_level_user_group: dict[str, int] = {}
        self._subject_model: dict[str, str] = {}
        # 群专属模型（v1.3.12）：{群号: provider_id}，对**整个群**生效；
        # /切换模型 与控制台改的都是这一份（旧的「按用户存群场景」会跨群串号，已废弃）。
        self._subject_model_group: dict[str, str] = {}
        # 等级模型路由：{level_id: {provider_id, fallback_provider_id}}
        self._level_route: dict[int, dict[str, str]] = {}
        # 等级允许用户自助切换的模型名单：{level_id: (provider_id, ...)}（v9）
        self._level_switch: dict[int, tuple[str, ...]] = {}
        # 等级**是否允许**切换（v10，默认关 = 只读）：{level_id: bool}
        self._level_switch_enabled: dict[int, bool] = {}
        # 等级**是否允许**用 /切换模型检测（v11，默认关）：{level_id: bool}
        self._level_detect_enabled: dict[int, bool] = {}
        # 限额规则：{scope_type(user|group|level|global): {scope_id: {period: row}}}
        self._limits: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        # 用量计数：{scope_type(user|group|member): {scope_id: {period: row}}}
        self._usage_user: dict[str, dict[str, dict[str, Any]]] = {}
        self._usage_group: dict[str, dict[str, dict[str, Any]]] = {}
        # 群成员维度：「群号:QQ」→ {period: row}（按成员设额度要用它）
        self._usage_member: dict[str, dict[str, dict[str, Any]]] = {}
        # 指令权限：{指令名: {scope_type: {scope_id: {scene: effect}}}}（单条指令的规则）
        self._cmd_policy: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
        # 对象级指令总权限：{scope_type: {scope_id: {scene: effect}}}
        self._cmd_master: dict[str, dict[str, dict[str, str]]] = {}
        # 等级的默认「指令」权限：{level_id: effect}（与 LLM 的 _level_effect 分开）
        self._level_cmd_effect: dict[int, str] = {}
        # 上面两者的「群聊场景」版本（v6）：只有好友等级会显式配，群聊等级恒为 inherit
        self._level_eff_group: dict[int, str] = {}
        self._level_cmd_eff_group: dict[int, str] = {}

        # M1：判定内核 / 提示冷却 / 后台任务
        self.gate = Gate(self._cfg)
        self._notify_cooldown = Cooldown(int(self._cfg("notice_cooldown_sec", 60) or 0))
        # M3：模型路由的提供商熔断器 + 每个会话最近一次的下发结果（用于失败归因与控制台展示）
        self.circuit = ProviderCircuit(
            threshold=int(self._cfg("model_route_failure_threshold", 2) or 2),
            cooldown_sec=int(self._cfg("model_route_circuit_sec", 300) or 0),
        )
        self._last_route: dict[str, dict[str, Any]] = {}
        self.scheduler = SyncScheduler(self)
        # M12：用户自助切换模型（/切换模型 + 回序号），状态全在内存（见 model_switch.py）
        self.switcher = ModelSwitcher(self)
        # M14：切换模型检测（/切换模型检测）—— 检测逻辑走 model_panel，见 detect.py
        # 两道冷却（指令级 + 模型级）都在 detector 里，因为它读的配置与判定对象都是指令侧的
        self.detector = ModelDetector(self)
        # M13：临时消息（卡片）到点自动撤回（见 recall.py；只有 QQ 能真撤）
        self.recaller = Recaller(self)
        # 会话 → 本次 LLM 请求的起始信息（用于估算 token 与统计延迟）
        self._inflight: dict[str, dict[str, Any]] = {}
        self._maintenance_task: Optional[asyncio.Task] = None
        self._running = False

        # M2：头像缓存（initialize() 里创建）
        self.avatars: Optional[AvatarCache] = None
        # M2：bot 最后消息的乱序保护（流式回复按段发送，可能乱序落库）
        self._bot_msg_seq: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        """创建数据目录、建库、注册控制台路由。"""
        try:
            self.data_dir = self._resolve_data_dir()
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"[UserGateway] 数据目录创建失败: {e}")
            return

        # v1.5.0：插件改名的一次性搬迁。必须在建库与 reload_rules() 之前：
        # 数据库文件要先就位，配置值也要先搬进来，否则本轮会按 schema 默认值启动。
        try:
            moved_files, moved_keys = migrate_renamed_plugin(
                self.data_dir, self._raw_config or self.config, logger.info
            )
            if moved_files or moved_keys:
                logger.info(
                    f"[UserGateway] 改名迁移完成：{moved_files} 个数据文件 / "
                    f"{moved_keys} 个配置键（旧文件保留）"
                )
        except Exception as e:
            # fail-open：迁移失败不能阻断插件启动，最坏情况是用户手动导入规则备份
            logger.warning(f"[UserGateway] 改名迁移失败（不阻断启动）: {e}")

        try:
            self.store = Store(str(self.data_dir / "user_gateway.db"))
            await self.store.open()
            logger.info(f"[UserGateway] 数据库就绪: {self.store.db_path}")
        except Exception as e:
            # 数据库打不开时插件仍要保持可用（fail-open：不拦截任何消息）
            logger.error(f"[UserGateway] 数据库初始化失败，插件将不拦截任何消息: {e}")
            self.store = None
            return

        # v1.3.12：切换语义并入专属模型 —— 旧「用户自选（model_choice）」先迁走再加载，
        # 否则这次 reload 读到的还是旧数据（迁移必须发生在第一次 reload_rules 之前）。
        try:
            moved = await self.store.migrate_model_choice()
            if moved:
                logger.info(
                    f"[UserGateway] 已把 {moved} 位用户的旧「/切换模型 选择」迁移为专属模型"
                )
        except Exception as e:
            logger.warning(f"[UserGateway] model_choice 迁移失败（跳过，不影响启动）: {e}")

        # v1.4.0：全局默认额度拆私聊 / 群聊 —— 旧 global:* 行复制成两份再删掉，
        # 同样必须在第一次 reload_rules 之前完成。
        try:
            migrated = await self.store.migrate_global_quota_scene()
            if migrated:
                logger.info(
                    f"[UserGateway] 已把旧全局额度拆成私聊 / 群聊两份（{migrated} 行）"
                )
        except Exception as e:
            logger.warning(f"[UserGateway] 全局额度场景迁移失败（跳过，不影响启动）: {e}")

        await self.reload_rules()
        await self._purge_old_usage()

        try:
            register_apis(self)
        except Exception as e:
            logger.error(f"[UserGateway] 控制台路由注册失败: {e}")

        # 头像缓存（抓取 + 落盘）
        try:
            self.avatars = AvatarCache(
                self.data_dir / "avatars",
                timeout=float(self._cfg("avatar_timeout_sec", 8) or 8),
                max_age_days=int(self._cfg("avatar_cache_days", 30) or 0),
            )
        except Exception as e:
            logger.warning(f"[UserGateway] 头像缓存初始化失败（头像不可用）: {e}")
            self.avatars = None

        # 把配置里的闸门优先级应用到已注册的钩子上
        self._apply_guard_priority()

        # 「bot 最后一条消息」的记录钩子（patch AstrMessageEvent.send）
        global _ACTIVE_PLUGIN
        _ACTIVE_PLUGIN = self
        self._install_send_hook()

        # 后台任务：好友/群定时同步 + 额度过期维护
        self._running = True
        try:
            await self.scheduler.start()
        except Exception as e:
            logger.warning(f"[UserGateway] 启动自动同步失败（忽略）: {e}")
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

        logger.info(
            f"[UserGateway] 初始化完成（v{self.plugin_version}）｜"
            f"LLM 管控={'开' if self._cfg('llm_guard_enabled', True) else '关'} "
            f"额度={'开' if self._cfg('quota_enabled', True) else '关'}"
            f"（{self._cfg('quota_mode', 'observe')}）"
        )

    async def terminate(self) -> None:
        """停止后台任务、还原本插件打的补丁、关闭数据库与头像会话。"""
        global _ACTIVE_PLUGIN
        self._running = False
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None
        self._restore_send_hook()
        for task in (self._maintenance_task, getattr(self.scheduler, "_task", None)):
            if task is not None:
                task.cancel()
        self._maintenance_task = None
        try:
            await self.scheduler.stop()
        except Exception:
            pass
        try:
            if self.avatars:
                await self.avatars.close()
        except Exception:
            pass
        try:
            await self.recaller.close()  # 待撤回任务要一并取消，别去碰已关闭的连接
        except Exception:
            pass
        try:
            if self.store:
                await self.store.close()
                logger.info("[UserGateway] 数据库已关闭")
        except Exception as e:
            logger.warning(f"[UserGateway] 关闭数据库异常（忽略）: {e}")

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _resolve_data_dir(self) -> Path:
        """取插件专属数据目录（``data/plugin_data/<插件名>``），失败时退回插件目录。"""
        try:
            d = StarTools.get_data_dir(PLUGIN_NAME)
            if d:
                return Path(d)
        except Exception as e:
            logger.warning(f"[UserGateway] get_data_dir 失败，回退插件目录: {e}")
        return Path(__file__).resolve().parent

    def _cfg(self, key: str, default: Any = None) -> Any:
        """安全读取配置项（配置对象缺失 / 类型异常时返回默认值）。"""
        try:
            if hasattr(self.config, "get"):
                v = self.config.get(key)
                return default if v is None else v
        except Exception:
            pass
        return default

    async def _purge_old_usage(self) -> None:
        """按配置清理超期用量明细。"""
        try:
            days = int(self._cfg("retention_days", 180) or 0)
            n = await self.store.purge_old_usage(days)
            if n:
                logger.info(f"[UserGateway] 已清理 {n} 条超期用量明细（保留 {days} 天）")
        except Exception as e:
            logger.warning(f"[UserGateway] 清理超期明细失败（忽略）: {e}")

    async def reload_rules(self) -> None:
        """把权限、等级、限额、用量从 SQLite 重新加载进内存（任何写操作后调用）。

        闸门热路径只读这些字典，保证判定过程**不做任何 SQL**。
        """
        if not (self.store and self.store.ready):
            return
        try:
            self._effect_user = await self.store.effect_map("user")
            self._effect_group = await self.store.effect_map("group")
            self._effect_member = await self.store.effect_map("member")

            # 等级默认权限与模型路由。
            # 键一律是 level_id（quota_level.id 全局唯一）——不带 kind，原因见
            # Gate._level_key 的说明：归级记录只存 id，带 kind 查表会把
            # 「好友的群聊等级指向群聊等级」这类合法引用查空。
            levels = await self.store.list_levels()
            self._level_effect = {
                int(lv["id"]): str(lv.get("effect") or "inherit")
                for lv in levels
            }
            # 等级的默认「指令」权限（与 LLM 权限分开配置，避免互相牵连）
            self._level_cmd_effect = {
                int(lv["id"]): str(lv.get("command_effect") or "inherit")
                for lv in levels
            }
            # 上面两者的「群聊场景」版本：好友等级在群里也生效，所以能单独设一套
            # （inherit = 跟随主值，旧数据就是这样 → 行为不变）
            self._level_eff_group = {
                int(lv["id"]): str(lv.get("effect_group") or "inherit")
                for lv in levels
            }
            self._level_cmd_eff_group = {
                int(lv["id"]): str(lv.get("command_effect_group") or "inherit")
                for lv in levels
            }
            self._level_route = {
                int(lv["id"]): {
                    "provider_id": str(lv.get("provider_id") or ""),
                    "fallback_provider_id": str(lv.get("fallback_provider_id") or ""),
                }
                for lv in levels
            }
            # 等级允许用户自助切换的模型名单（v9）：只留非空的等级，省得后面每次判空
            self._level_switch = {
                int(lv["id"]): tuple(got)
                for lv in levels
                for got in [parse_provider_list(lv.get("switch_providers"))]
                if got
            }
            # 是否允许切换（v10）：只留打开的等级（默认关 = 只读，所以「有没有这个键」就是判据）
            self._level_switch_enabled = {
                int(lv["id"]): bool(int(lv.get("switch_enabled") or 0)) for lv in levels
            }
            # 是否允许 /切换模型检测（v11）：同样是「显式打开才允许」
            self._level_detect_enabled = {
                int(lv["id"]): bool(int(lv.get("detect_enabled") or 0)) for lv in levels
            }
            self._subject_level_user = await self.store.subject_level_map("user")
            self._subject_level_group = await self.store.subject_level_map("group")
            # 好友的「群聊专属」等级（v8：私聊等级与群聊等级分开配）
            self._subject_level_user_group = await self.store.subject_level_group_map()
            # 好友专属模型（feature=model 的 policy；effect 存提供商 id，仅私聊生效）
            self._subject_model = {
                uid: pid
                for uid, row in (
                    await self.store.effect_map("user", feature=MODEL_FEATURE)
                ).items()
                for pid in [next(iter(row.values()), "") if row else ""]
                if pid
            }
            # 群专属模型（v1.3.12）：对整个群生效，/切换模型（群管理员）与控制台都写这一份
            self._subject_model_group = {
                gid: effect_in_scene(row, "group")
                for gid, row in (
                    await self.store.effect_map("group", feature=MODEL_FEATURE)
                ).items()
                if effect_in_scene(row, "group")
            }

            def _by_scope(rows: list[dict]) -> dict[str, dict[str, dict[str, Any]]]:
                out: dict[str, dict[str, dict[str, Any]]] = {}
                for row in rows:
                    out.setdefault(str(row["scope_id"]), {})[str(row["period"])] = row
                return out

            # 限额：用户专属 / 群专属 / 等级模板 / 全局模板
            self._limits = {
                st: _by_scope(await self.store.list_quotas(st))
                for st in ("user", "group", "level", "global")
            }
            self._usage_user = await self.store.usage_map("user")
            self._usage_group = await self.store.usage_map("group")
            self._usage_member = await self.store.usage_map("member")
            # 指令权限：单条指令的规则 + 对象级总权限（好友 / 群 / 群成员 / 全局）。
            # 注意 member / global 两类也要装：档位链里有成员层与全局层，
            # 漏装 = 界面写进去的规则闸门不认（v1.0.0 修复）。
            self._cmd_policy = await self.store.command_policies()
            self._cmd_master = {
                st: await self.store.effect_map(st, feature=COMMAND_MASTER_FEATURE)
                for st in ("user", "group", "member", "global")
            }

            if self._cfg("debug_log", False):
                logger.info(
                    f"[UserGateway] 规则已加载: 好友权限 {len(self._effect_user)} / "
                    f"群权限 {len(self._effect_group)} / "
                    f"群成员权限 {len(self._effect_member)} / 等级 {len(self._level_effect)} / "
                    f"归级 {len(self._subject_level_user)}+{len(self._subject_level_group)} / "
                    f"限额 {sum(len(v) for v in self._limits.values())} 条 / "
                    f"用量计数 {len(self._usage_user)}+{len(self._usage_group)} 条 / "
                    f"指令规则 {sum(len(s) for s in self._cmd_policy.values())} 条 / "
                    f"对象级指令权限 {len(self._cmd_master.get('user') or {})}"
                    f"+{len(self._cmd_master.get('group') or {})}"
                    f"+{len(self._cmd_master.get('member') or {})} 条"
                )
        except Exception as e:
            # 加载失败按"没有规则"处理（全部继承默认策略），不影响消息通行
            logger.error(f"[UserGateway] 规则加载失败（按无规则处理）: {e}")

    def _rules(self) -> Rules:
        """组装判定快照（给 Gate 用）。

        性能说明：这里只做 **引用拼装**（6 个 dict，不复制内容），所以每次 LLM 请求
        都构造一次也几乎零开销；写操作后 ``reload_rules()`` 会整体替换这些字典对象，
        因此不存在「快照过期」的问题。
        """
        return Rules(
            effect_user=self._effect_user,
            effect_group=self._effect_group,
            effect_member=self._effect_member,
            level_effect=self._level_effect,
            subject_level={"user": self._subject_level_user, "group": self._subject_level_group},
            subject_level_user_group=self._subject_level_user_group,
            limits=self._limits,
            usage={
                "user": self._usage_user,
                "group": self._usage_group,
                "member": self._usage_member,
            },
            level_model=self._level_route,
            level_switch=self._level_switch,
            level_switch_enabled=self._level_switch_enabled,
            level_detect_enabled=self._level_detect_enabled,
            command_policy=self._cmd_policy,
            command_master=self._cmd_master,
            level_command_effect=self._level_cmd_effect,
            level_effect_group=self._level_eff_group,
            level_command_effect_group=self._level_cmd_eff_group,
        )

    # ------------------------------------------------------------------ #
    # M3：按等级路由模型（主提供商 + 备用提供商）
    # ------------------------------------------------------------------ #
    def provider_map(self) -> dict[str, dict[str, Any]]:
        """当前**已加载**的对话提供商 ``{id: {id, name, model, label, type, modalities}}``。

        展示名口径全插件统一：「供应商 · 模型」（AstrBot 里一个提供商就绑定一个模型）。
        刻意**不用 ``type`` 退化**（``openai_chat_completion`` 这种名字对用户毫无意义）。
        控制台下拉、/切换模型 卡片都走这一份，避免两处标签不一致。
        """
        out: dict[str, dict[str, Any]] = {}
        try:
            for p in self.context.get_all_providers() or []:
                cfg = getattr(p, "provider_config", {}) or {}
                pid = str(cfg.get("id") or "")
                if not pid:
                    continue
                name = str(
                    cfg.get("provider_source_id") or cfg.get("name") or cfg.get("provider") or pid
                )
                try:
                    model = str(p.get_model() or "")
                except Exception:
                    model = ""
                if not model:
                    model = str(cfg.get("model") or cfg.get("default_model") or "")
                if name and model and name != model:
                    label = f"{name} · {model}"
                else:
                    label = name or model or pid
                out[pid] = {
                    "id": pid,
                    "name": name,
                    "model": model,
                    "label": label,
                    "type": str(cfg.get("type") or cfg.get("provider_type") or ""),
                    "modalities": list(cfg.get("modalities") or []),
                }
        except Exception as e:
            logger.debug(f"[UserGateway] 读取提供商列表失败（忽略）: {e}")
        return out

    def provider_info(self, pid: str) -> dict[str, Any]:
        """单个提供商的展示信息。

        未加载的提供商退化为「id 当名字」——它可能只是暂时没加载，
        卡片 / 列表会另外标「暂不可用」，这里不编造名字。
        """
        want = str(pid or "")
        got = self.provider_map().get(want)
        if got:
            return got
        return {
            "id": want,
            "name": want,
            "model": "",
            "label": want or "(未配置)",
            "type": "",
            "modalities": [],
        }

    def provider_ids(self) -> set[str]:
        """当前**已加载可用**的对话提供商 id 集合。"""
        return set(self.provider_map())

    def provider_config_map(self) -> Optional[dict[str, dict[str, Any]]]:
        """AstrBot **配置里**的全部对话模型（含已禁用 / 加载失败的）``{id: {enable}}``。

        ``get_all_providers()`` 只给已加载实例，看不出「禁用」和「删除」的区别：
        禁用的模型配置还在、随时能启用，删掉的则配置里彻底没了。这里读配置是为了
        给控制台区分这两种状态 —— **返回 None 表示读不到配置**，此时一律按
        「状态未知」处理，绝不推断成「已删除」而诱导用户清理。

        只取配置里声明的字段，不缓存：用户在 AstrBot 后台改完，下次打开页面就是新的。
        """
        raw: Any = None
        try:
            cfg = self.context.get_config()
            if cfg is not None:
                raw = cfg.get("provider")
        except Exception as e:
            logger.debug(f"[UserGateway] 读取 AstrBot 模型配置失败（忽略）: {e}")
        if raw is None:
            # 老版本 / 配置对象形态不同时退到 manager 的内存视图
            try:
                raw = getattr(getattr(self.context, "provider_manager", None), "providers_config", None)
            except Exception:
                raw = None
        if not isinstance(raw, (list, tuple)):
            return None
        out: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            if pid:
                out[pid] = {"id": pid, "enable": bool(item.get("enable", True))}
        return out

    async def provider_ref_report(self) -> dict[str, Any]:
        """库里的模型引用 vs 当前 AstrBot 模型：生成失效引用报告（不改动任何数据）。"""
        levels, policies = [], []
        if self.store and self.store.ready:
            try:
                levels, policies = await self.store.model_ref_rows()
            except Exception as e:
                logger.warning(f"[UserGateway] 读取模型引用失败: {e}")
        refs = provider_refs_mod.collect_refs(levels, policies)
        return provider_refs_mod.build_report(
            refs, self.provider_ids(), self.provider_config_map()
        )

    def _provider_by_id(self, pid: str) -> Optional[Any]:
        """按 id 从已加载的提供商里找对象（找不到返回 None）。"""
        try:
            for p in self.context.get_all_providers() or []:
                if str((getattr(p, "provider_config", {}) or {}).get("id") or "") == pid:
                    return p
        except Exception:
            pass
        return None

    @astr_filter.on_waiting_llm_request()
    async def route_model(self, event: AstrMessageEvent) -> None:
        """按等级把「本次请求走哪个模型」下发给 AstrBot。

        时机依据（AstrBot 4.27 ``internal.py`` 的 ``process()``）：

        - 本钩子在 **214 行**触发（获取会话锁之前）；
        - 提供商在 **230 行**才由 ``_select_provider`` 决定，它读
          ``event.get_extra("selected_provider")``（``astr_main_agent.py:242``）；
        - ``req`` 在 **238 行**构造，``selected_model`` 在那时被读进 ``req.model``。

        所以这里设的两个 extra 都来得及生效；而在 ``on_llm_request``（333 行）里
        再改提供商就已经晚了。**未知的提供商 id 绝不能设**：AstrBot 找不到会直接
        放弃本次请求并报错，所以这里先做「可用性 + 熔断」校验，都不行就不干预。
        """
        try:
            if not self._cfg("model_route_enabled", True):
                return
            if not (self.store and self.store.ready):
                return
            subject = self._subject_of(event)
            self._last_route.pop(subject.umo, None)
            debug = self._cfg("debug_log", False)
            # 本会话的场景：群里按群聊场景取「用户自己的选择」，与私聊那份分开
            scene = "group" if str(subject.group_id or "") else "private"
            sender = str(subject.sender_id or "")
            available = self.provider_ids() - self.circuit.open_ids()

            def use(
                pid: str,
                label: str,
                layer: str,
                used_fallback: bool = False,
                **extra: Any,
            ) -> bool:
                """把某提供商下发给本次请求（不可用则不干预）并记下归因。"""
                if not pid or pid not in available:
                    return False
                event.set_extra("selected_provider", pid)
                if len(self._last_route) > 2000:  # 防御性清理
                    self._last_route.clear()
                # 把实际下发的提供商与它的模型名一起存下来：用量记录要记「真的用了谁」，
                # 而不是 get_using_provider_async 返回的会话默认（按次下发不改会话默认）
                prov = self._provider_by_id(pid)
                self._last_route[subject.umo] = {
                    **extra,
                    "provider_id": pid,
                    "model": str(prov.get_model() or "") if prov is not None else "",
                    "used_fallback": bool(used_fallback),
                    "label": label,
                    "layer": layer,
                }
                if debug:
                    logger.info(
                        f"[UserGateway] 模型路由：{subject.umo} → {pid}"
                        f"{'（备用）' if used_fallback else ''}｜来源 {label}"
                    )
                return True

            # ① 专属模型（feature=model）：私聊 = 好友专属、群聊 = 群专属（v1.3.12 起统一起来）。
            # /切换模型 改的就是这一份：私聊切自己的专属模型、群里（管理员）切这个群的专属模型。
            # 优先级高于等级路由 —— 「手动钉过的模型」不该被分组配置悄悄按回去。
            if str(subject.group_id or ""):
                override = (self._subject_model_group.get(str(subject.group_id)) or "").strip()
                own_label = "群专属"
            else:
                override = (self._subject_model.get(sender) or "").strip()
                own_label = "好友专属"
            if override:
                if use(override, own_label, "subject_model"):
                    return
                if debug:
                    logger.info(
                        f"[UserGateway] {own_label}模型 {override} 当前不可用，回落等级路由｜{subject.umo}"
                    )

            # ② 等级模型路由
            route = self.gate.resolve_model(subject, self._rules())
            if not route:
                return
            picked = self.gate.pick_provider(route, available)
            if not picked:
                if debug:
                    logger.info(
                        f"[UserGateway] 模型路由：{route['label']} 配的提供商当前不可用，"
                        f"本次不干预（走 AstrBot 默认模型）｜{subject.umo}"
                    )
                return
            use(
                str(picked["provider_id"]),
                str(route.get("label") or ""),
                str(route.get("layer") or ""),
                bool(picked.get("used_fallback")),
                level_id=route.get("level_id"),
                fallback_provider_id=route.get("fallback_provider_id"),
            )
        except Exception:
            # 路由失败绝不能影响对话：静默放行，交给 AstrBot 自己的默认逻辑
            logger.exception("[UserGateway] 模型路由异常（忽略）")

    @staticmethod
    def _is_llm_error(response: Any) -> bool:
        """判断这次响应是不是「所有候选模型都失败了」的兜底响应。

        依据：AstrBot ``_iter_llm_responses_with_fallback`` 在候选用尽后
        yield ``LLMResponse(role="err", completion_text="All chat models failed: ...")``。
        """
        try:
            if str(getattr(response, "role", "") or "") == "err":
                return True
            text = str(getattr(response, "completion_text", "") or "")
            return text.startswith("All chat models failed") or text.startswith(
                "All available chat models"
            )
        except Exception:
            return False

    def _update_circuit(self, umo: str, response: Any) -> None:
        """按本次响应更新「我们路由过的那个提供商」的熔断状态。

        只统计**本插件下发过的**提供商，不碰 AstrBot 自己的默认模型。
        """
        if not self._cfg("model_route_enabled", True):
            return
        routed = self._last_route.get(umo) or {}
        pid = str(routed.get("provider_id") or "")
        if not pid:
            return
        try:
            if self._is_llm_error(response):
                if self.circuit.note_failure(pid):
                    logger.warning(
                        f"[UserGateway] 提供商 {pid} 连续失败，已熔断 "
                        f"{self.circuit.cooldown}s；期间配了备用模型的等级会自动落过去"
                    )
            else:
                self.circuit.note_success(pid)
        except Exception:
            pass

    @staticmethod
    def quota_reset_at(period: str, now: Optional[datetime] = None) -> Optional[int]:
        """计算额度的下次重置时间戳（epoch 秒）；``total`` 周期不重置，返回 None。"""
        return quota_mod.next_reset_at(period, now)

    def _apply_guard_priority(self) -> None:
        """把配置里的优先级应用到本插件的两个闸门处理器上。

        - ``guard_llm_request``（LLM 闸门）用 ``guard_priority``（默认 100，越大越先）；
        - ``guard_commands``（指令闸门）用 ``command_guard_priority``（默认 1000）——
          它必须**排在指令 handler 之前**才能拦住指令，见 ``star_request.py:36-38``。

        装饰器在类定义时就把优先级固定了，这里改 ``extras_configs`` 并对注册表**原地重排**
        （只重排现有条目、不新增，幂等）。
        """
        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            wanted = {
                "guard_llm_request": int(self._cfg("guard_priority", 100) or 100),
                "guard_commands": int(self._cfg("command_guard_priority", 1000) or 1000),
            }
            handlers = getattr(star_handlers_registry, "_handlers", None)
            if not handlers:
                return
            changed = False
            for md in handlers:
                if md.handler_module_path != __name__:
                    continue
                want = wanted.get(str(getattr(md, "handler_name", "") or ""))
                if want is None or md.extras_configs.get("priority") == want:
                    continue
                md.extras_configs["priority"] = want
                changed = True
            if changed:
                handlers.sort(key=lambda h: -h.extras_configs.get("priority", 0))
                logger.info(f"[UserGateway] 闸门优先级已应用: {wanted}")
        except Exception as e:
            logger.warning(f"[UserGateway] 应用闸门优先级失败（忽略）: {e}")

    # ------------------------------------------------------------------ #
    # M1 核心：LLM 请求闸门
    # ------------------------------------------------------------------ #
    @astr_filter.on_llm_request()
    async def guard_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:  # noqa: D401
        """LLM 请求前的权限与额度闸门（PRD §8.1）。

        放行时不做任何事；拒绝时**直发提示**并 ``event.stop_event()`` 掐断本次 LLM 调用。

        AstrBot 侧依据：agent 阶段 `if await call_event_hook(event, OnLLMRequestEvent, req): return`，
        事件被 stop 后不会再向 provider 发请求，也不会影响指令与其它插件的 handler。
        """
        try:
            if not (self.store and self.store.ready):
                return  # fail-open：数据库不可用时不拦任何请求
            subject = self._subject_of(event)
            verdict = self.gate.evaluate(subject, self._rules())
            self._mark_request(event, req)

            if verdict.allow:
                # 给事件打标：本条消息确实走了 LLM。
                # 下面记录 bot 最后消息的 send 钩子据此把回复归类为「LLM 消息」。
                event.set_extra("_ugw_llm", True)
                if verdict.observed:
                    # 观察模式：本该被拦但放行，记一条流水便于灰度评估
                    await self._log_denied(subject, verdict, blocked=False)
                elif self._cfg("debug_log", False):
                    logger.info(f"[UserGateway] 放行 {subject.umo} → {verdict.detail}")
                return

            await self._log_denied(subject, verdict, blocked=True)
            await self._notify_denied(event, subject, verdict)
            event.stop_event()
            logger.info(f"[UserGateway] 已拦截 LLM 请求 {subject.umo} → {verdict.detail}")
        except Exception:
            # 红线：权限插件自身异常必须放行，绝不能把用户消息吞掉
            logger.exception("[UserGateway] 闸门异常，已放行（fail-open）")

    @astr_filter.on_llm_response()
    async def record_llm_usage(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """记录一次 LLM 用量（AstrBot 每轮 agent 结束时触发一次）。

        ⚠️ 已知粒度：该钩子只在「最终无工具调用」的那一轮触发
        （`tool_loop_agent_runner.py:912`），因此**多轮工具循环的中间轮 token 不会计入**。
        对限额而言这是系统性低估，M2 会改为按轮累计或增量读 `provider_stats`。

        这里顺带做模型路由的熔断统计（与 ``stats_enabled`` 无关，开关只管统计）。
        """
        try:
            self._update_circuit(str(getattr(event, "unified_msg_origin", "") or ""), response)
            if not (self.store and self.store.ready):
                return
            if not self._cfg("stats_enabled", True):
                return
            await self._record_usage(event, response)
        except Exception:
            logger.exception("[UserGateway] 用量记录异常（忽略，不影响对话）")

    # ------------------------------------------------------------------ #
    # M4：指令权限闸门
    # ------------------------------------------------------------------ #
    def registered_commands(self) -> list[dict[str, Any]]:
        """列出 AstrBot 里已注册的指令（控制台「指令」页的数据源）。

        来源：handler 注册表里 ``event_filters`` 含指令过滤器（CommandFilter /
        CommandGroupFilter）的处理器。指令名取 ``get_complete_command_names()[0]``
        —— 完整主名（子指令形如 ``父 子``），**别名不单独建键**（与主名共用一条规则）。
        """
        out: dict[str, dict[str, Any]] = {}
        try:
            from astrbot.core.star.star_handler import (  # type: ignore
                EventType,
                star_handlers_registry,
            )
        except Exception as e:
            logger.debug(f"[UserGateway] 导入 handler 注册表失败（指令列表不可用）: {e}")
            return []
        try:
            handlers = list(
                star_handlers_registry.get_handlers_by_event_type(
                    EventType.AdapterMessageEvent, only_activated=False
                )
                or []
            )
        except Exception:
            handlers = list(getattr(star_handlers_registry, "_handlers", []) or [])
        for h in handlers:
            try:
                if getattr(h, "event_type", None) is not EventType.AdapterMessageEvent:
                    continue
            except Exception:
                continue
            for f in getattr(h, "event_filters", []) or []:
                if type(f).__name__ not in ("CommandFilter", "CommandGroupFilter"):
                    continue
                names: list[str] = []
                try:
                    names = [str(x) for x in (f.get_complete_command_names() or [])]
                except Exception:
                    nm = str(getattr(f, "command_name", "") or "")
                    names = [nm] if nm else []
                if not names or not names[0].strip():
                    continue
                key = names[0].strip()
                out.setdefault(
                    key,
                    {
                        "name": key,
                        "aliases": [a for a in names[1:] if a],
                        "desc": str(getattr(h, "desc", "") or "").strip(),
                        "plugin": self._plugin_name_of(
                            str(getattr(h, "handler_module_path", "") or "")
                        ),
                        "handler": str(getattr(h, "handler_full_name", "") or ""),
                        "is_group": type(f).__name__ == "CommandGroupFilter",
                    },
                )
        return sorted(out.values(), key=lambda it: it["name"])

    @staticmethod
    def _plugin_name_of(module_path: str) -> str:
        """handler 所属插件的展示名（取 star_map；失败返回空串）。"""
        try:
            from astrbot.core.star.star import star_map  # type: ignore

            md = star_map.get(module_path)
            name = str(getattr(md, "name", "") or "")
            if name:
                return name
        except Exception:
            pass
        return ""

    @staticmethod
    def _matched_commands(event: AstrMessageEvent) -> list[str]:
        """本次消息命中了哪些指令（完整主名；别名与主名共用一条规则）。"""
        names: list[str] = []
        try:
            handlers = event.get_extra("activated_handlers") or []
        except Exception:
            return names
        for h in handlers:
            for f in getattr(h, "event_filters", []) or []:
                if type(f).__name__ not in ("CommandFilter", "CommandGroupFilter"):
                    continue
                got: list[str] = []
                try:
                    got = [str(x) for x in (f.get_complete_command_names() or [])]
                except Exception:
                    got = []
                if not got:
                    nm = str(getattr(f, "command_name", "") or "")
                    got = [nm] if nm else []
                if got and got[0].strip():
                    names.append(got[0].strip())
        return list(dict.fromkeys(names))

    @astr_filter.event_message_type(astr_filter.EventMessageType.ALL, priority=1000)
    async def guard_commands(self, event: AstrMessageEvent) -> None:
        """指令权限闸门（PRD §8.2）。

        为什么这样拦得住：waking 阶段（``waking_check/stage.py:253``）已经把「本次命中的
        handler」写进 ``activated_handlers``；process 阶段按**优先级降序**依次执行这些
        handler，并在每个 handler 之前检查 ``event.is_stopped()``（``star_request.py:36-38``）。
        所以本插件用「所有消息 + 高优先级」注册一个处理器，在这里 ``stop_event()``
        就能让后面的指令 handler 不执行 —— 与动画/图片类插件提前介入指令是同一套机制。

        红线：判错一律放行（fail-open），绝不能因为权限插件把正常指令吞掉。
        """
        try:
            if not self._cfg("enabled", True) or not self._cfg("command_guard_enabled", True):
                return
            if not (self.store and self.store.ready):
                return
            names = self._matched_commands(event)
            if not names:
                return
            subject = self._subject_of(event)
            exempt = subject.is_admin and bool(self._cfg("admin_exempt", True))
            allowed: list[str] = []
            for name in names:
                verdict = self.gate.check_command(subject, name, self._rules())
                if verdict.allow or exempt:
                    allowed.append(name)
                    continue
                await self._log_command_denied(subject, name, verdict)
                await self._notify_command_denied(event, subject, name, verdict)
                # 掐断本次消息：后面的指令 handler 不会再执行（也不会进 LLM）
                event.stop_event()
                logger.info(f"[UserGateway] 已拦截指令 {name} {subject.umo} → {verdict.detail}")
                return
            # 放行的也记一条流水（「最常触发的指令」排行要用）。
            # 只记第一个：一条消息通常只对应一个主指令，全记会让榜单被同一条消息刷高。
            if allowed:
                await self._log_command_used(subject, allowed[0])
                # 兜底：指令的回复也算「bot 最后消息」（send 钩子失效时保底不断更）
                try:
                    await self._touch_bot_reply(event, "command", allowed[0])
                except Exception as e:
                    self._note_record_failure(e)
        except Exception:
            logger.exception("[UserGateway] 指令闸门异常，已放行（fail-open）")

    async def _log_command_used(self, subject: Subject, command: str) -> None:
        """写一条「指令已触发」流水（``kind=command`` + ``status=ok``）。

        用途：统计页的「最常触发的指令」排行 + 指令维度的审计追溯。
        可用配置 ``track_command_usage`` 关掉（指令极频繁的部署可以省这次写库）；
        ``kind='command'`` 与 LLM 的口径完全分开，不会污染调用次数 / token 统计。
        """
        if not self._cfg("track_command_usage", True):
            return
        try:
            await self.store.log_usage(
                platform_id=subject.platform_id,
                umo=subject.umo,
                scope_type="group" if subject.group_id else "user",
                scope_id=subject.group_id or subject.sender_id,
                sender_id=subject.sender_id,
                group_id=subject.group_id,
                kind="command",
                command_name=command,
                status="ok",
            )
        except Exception as e:
            logger.warning(f"[UserGateway] 写指令流水失败（忽略）: {e}")

    async def _log_command_denied(self, subject: Subject, command: str, verdict: Any) -> None:
        """写一条指令拦截流水（``kind=command``，不影响 LLM 的调用统计口径）。"""
        try:
            await self.store.log_usage(
                platform_id=subject.platform_id,
                umo=subject.umo,
                scope_type=verdict.scope_type or ("group" if subject.group_id else "user"),
                scope_id=verdict.scope_id or (subject.group_id or subject.sender_id),
                sender_id=subject.sender_id,
                group_id=subject.group_id,
                kind="command",
                command_name=command,
                status="denied",
                deny_reason=verdict.reason or "command",
            )
        except Exception as e:
            logger.warning(f"[UserGateway] 写指令拦截流水失败（忽略）: {e}")

    async def _notify_command_denied(
        self, event: AstrMessageEvent, subject: Subject, command: str, verdict: Any
    ) -> None:
        """给被拒用户发提示（带冷却），必要时同时通知管理员。"""
        if not self._notify_cooldown.allow(f"cmd:{command}:{verdict.scope_type}:{verdict.scope_id}"):
            return
        if not self._cfg("silent", False):
            text = str(self._cfg("command_deny_notice", "") or "").strip()
            if text:
                await self._send(event, text.replace("{command}", command))
        if self._cfg("notify_admin", False):
            where = f"群 {subject.group_id}" if subject.group_id else "私聊"
            await self._notify_admins(
                f"已拦截指令 {command}：{subject.sender_id or subject.umo}（{where}）｜{verdict.detail}",
                subject.platform_id,
            )

    # ------------------------------------------------------------------ #
    # M12：用户自助切换模型（/切换模型 + 回序号）
    # ------------------------------------------------------------------ #
    @astr_filter.command("切换模型", alias={"模型切换"})
    async def cmd_switch_model(self, event: AstrMessageEvent, index: str = "") -> None:
        """列出（或直接切换）**我自己**能用的模型。

        - 不带参数：卡片列出「所在分组开放的模型 + 我的专属模型 + 当前生效的那个」，回序号即切换；
        - 带序号（``/切换模型 2``）：直接切；``0`` = 恢复默认（跟随分组配置）。

        为什么这是**用户向**指令、与「管控入口只有控制台」那条红线不冲突：
        它只能改「自己用哪个模型」，动不了任何人的权限与额度；而且可选范围是管理员
        在等级里圈定的名单（``switch_providers``），所以「放开自助切换」不等于「谁都能挑最贵的」。
        """
        try:
            subject = self._subject_of(event)
            await self.switcher.handle_command(self, event, subject, index)
        except Exception:
            logger.exception("[UserGateway] /切换模型 处理异常（忽略）")
            await self._send(event, "切换模型失败，请稍后再试。")

    @astr_filter.regex(r"^\s*\d{1,2}\s*$")
    async def on_switch_index(self, event: AstrMessageEvent) -> None:
        """把「纯数字」消息当作模型切换的序号。

        为什么要单独一个正则处理器：用户看完卡片**只会回一个数字**，这条消息既没有唤醒前缀
        也不是指令，只能靠正则接住。它做得很克制：

        - 该会话**没有**待选列表 → 直接返回，不拦不答（正常的数字聊天完全不受影响）；
        - 有 → 切换、回执、``stop_event()``（否则那个数字还会被送进 LLM，用户会看到两段回复）。

        注意：正则过滤器不受唤醒前缀约束（``RegexFilter``），所以群里任何人发数字都会进来一次；
        但会话键带了发言人 + TTL，别人发的数字不会误触发、也不会顶掉你的选择。
        """
        try:
            if not self.switcher.enabled():
                return
            if not (self.store and self.store.ready):
                return
            subject = self._subject_of(event)
            if await self.switcher.handle_index(self, event, subject):
                event.stop_event()
        except Exception:
            logger.exception("[UserGateway] 模型切换序号处理异常（忽略）")

    # ------------------------------------------------------------------ #
    # M14：切换模型检测（检测走 model_panel，见 detect.py）
    # ------------------------------------------------------------------ #
    @astr_filter.command("切换模型检测")
    async def cmd_switch_detect(self, event: AstrMessageEvent) -> None:
        """给本分组允许使用的模型做一次体检（存活 / 延迟 / 成功率），然后发最新的切换卡。

        为什么指令名是「切换模型检测」而不是「模型检测」：**不与 model_panel 抢名字**。
        那个插件已经有 ``/模型检测``（管理员限定、可在聊天里直接打单个模型），
        两个插件注册同一个指令名会让一条消息触发两次回复 —— 用户只会觉得 bot 抽了。
        名字带「切换模型」也是刻意的：它检测的就是 ``/切换模型`` 卡片上那份名单。

        会真打模型、消耗额度，所以：分组开关默认关、非管理员有指令冷却、
        同一模型还有探测冷却（都可在配置里调）。
        """
        try:
            subject = self._subject_of(event)
            await self.detector.handle_command(self, event, subject)
        except Exception:
            logger.exception("[UserGateway] /切换模型检测 处理异常（忽略）")
            await self._send(event, "检测失败，请稍后再试。")

    # ------------------------------------------------------------------ #
    # 闸门辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _subject_of(event: AstrMessageEvent) -> Subject:
        """从事件里解析出判定对象（任何取值失败都退化为空，不影响放行判断）。"""
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""
        return Subject(
            sender_id=sender_id,
            group_id=group_id,
            is_admin=is_admin,
            umo=str(getattr(event, "unified_msg_origin", "") or ""),
            platform_id=platform_id,
        )

    def _mark_request(self, event: AstrMessageEvent, req: Any) -> None:
        """记下本次请求的起始时间与提示词长度（供用量估算与延迟统计）。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                return
            if len(self._inflight) > 2000:  # 防御性清理，避免长期运行内存增长
                self._inflight.clear()
            self._inflight[umo] = {
                "ts": time.time(),
                "prompt_len": len(str(getattr(req, "prompt", "") or "")),
            }
        except Exception:
            pass

    async def _record_usage(self, event: AstrMessageEvent, response: Any) -> None:
        """把一次 LLM 响应写入统计，并累加相关对象的额度已用量。"""
        subject = self._subject_of(event)
        info = self._inflight.pop(subject.umo, None) or {}

        count_cached = bool(self._cfg("count_cached_tokens", True))
        tok_in_other, tok_in_cached, tok_out, total = quota_mod.tokens_from_usage(
            getattr(response, "usage", None),
            count_cached=count_cached,
        )
        estimated = 0
        if total <= 0:
            # provider 没返回 usage：按字符数粗估，并打标记（控制台会区分展示）
            prompt_len = int(info.get("prompt_len", 0) or 0)
            est_out = quota_mod.estimate_tokens(getattr(response, "completion_text", "") or "")
            est_in = max(1, prompt_len // quota_mod.CHARS_PER_TOKEN) if prompt_len else 0
            if est_in + est_out > 0:
                tok_in_other, tok_in_cached, tok_out = est_in, 0, est_out
                total, estimated = est_in + est_out, 1

        latency_ms = 0
        ts = info.get("ts")
        if ts:
            latency_ms = max(0, int((time.time() - float(ts)) * 1000))

        # 记「真的用了谁」：本插件按等级路由过的请求，用下发时的提供商 / 模型
        # （route_model 存在 _last_route 里）。get_using_provider_async 返回的是
        # **会话默认**——按次下发不改会话默认，读它会错记成系统默认模型（v1.0.4 修复）。
        # 注意 _update_circuit 只读不弹，这里弹；且 route_model 在每次请求开头都会清键，
        # 所以不会被上一次的路由结果污染。
        routed = self._last_route.pop(subject.umo, None) or {}
        provider_id = str(routed.get("provider_id") or "")
        model = str(routed.get("model") or "")
        if not provider_id:
            # 没经过路由 → 记 AstrBot 的会话默认
            try:
                prov = await self.context.get_using_provider_async(subject.umo)
                if prov is not None:
                    provider_id = str((getattr(prov, "provider_config", {}) or {}).get("id", "") or "")
                    model = str(prov.get_model() or "")
            except Exception:
                pass

        # 归属对象：群消息记在群上、私聊记在人上（sender_id / group_id 另行保留，可细查）
        scope_type = "group" if subject.group_id else "user"
        scope_id = subject.group_id or subject.sender_id

        await self.store.log_usage(
            platform_id=subject.platform_id,
            umo=subject.umo,
            scope_type=scope_type,
            scope_id=scope_id,
            sender_id=subject.sender_id,
            group_id=subject.group_id,
            kind="llm",
            provider_id=provider_id,
            model=model,
            tok_in_other=tok_in_other,
            tok_in_cached=tok_in_cached,
            tok_out=tok_out,
            estimated=estimated,
            status="ok",
            latency_ms=latency_ms,
        )

        # 兜底：send 钩子若被其它插件覆盖 / 发送路径变化，「最后回复」会停更——
        # 这里在用量记账成功后顺带补一条 LLM 回复。send 钩子正常时两处写的是
        # 同一会话、相邻时间戳，upsert 幂等；钩子失效时这里保证最后回复不断更。
        try:
            await self._touch_bot_reply(
                event,
                "llm",
                "",
                str(getattr(response, "completion_text", "") or ""),
            )
        except Exception as e:
            self._note_record_failure(e)

        if total <= 0 or not self._cfg("quota_enabled", True):
            return

        # 累加用量计数（v2 起**无条件**记账：等级/全局额度都是模板，必须用对象自己的用量判定）
        #
        # 三个维度一起记：
        #   user   = 这个人（跨群 + 私聊的合计口径）
        #   group  = 这个群
        #   member = 「这个人 + 这个群」（按成员设额度时要用它，群聊里才有）
        # 只对群聊多记 member 一套，私聊没有「群成员」这个概念。
        reset_at_map = {p: quota_mod.next_reset_at(p) for p in quota_mod.PERIODS}
        targets: list[tuple[str, str]] = []
        if subject.sender_id:
            targets.append(("user", subject.sender_id))
        if subject.group_id:
            targets.append(("group", subject.group_id))
        if subject.group_id and subject.sender_id:
            targets.append(("member", member_scope_id(subject.group_id, subject.sender_id)))

        warns: list[dict] = []
        for st, sid in targets:
            try:
                await self.store.add_used(st, sid, total, reset_at_map=reset_at_map)
            except Exception as e:
                logger.warning(f"[UserGateway] 累加用量失败（忽略）: {e}")
            warns.extend(self._bump_memory_usage(st, sid, total))

        if warns and self._cfg("notify_admin", False):
            await self._notify_admins(
                "额度预警：" + "；".join(
                    f"{self._scope_label(w['scope_type'], w['scope_id'])}"
                    f"（{layer_label(w.get('layer', ''))}）{w['period']} 已用 "
                    f"{w['used']}/{w['limit']}（{w['percent']}%）"
                    for w in warns
                ),
                subject.platform_id,
            )

    @staticmethod
    def _scope_label(scope_type: str, scope_id: str) -> str:
        """把额度/权限对象写成一句人话（「群 88888 的成员 10001」这种）。"""
        if scope_type == "member":
            gid, uid = parse_member_scope_id(scope_id)
            return f"群 {gid} 的成员 {uid}"
        if scope_type == "group":
            return f"群 {scope_id}"
        if scope_type == "user":
            return f"好友 {scope_id}"
        return f"{scope_type} {scope_id}"

    def quota_chain(self, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        """列出某对象适用的**全部额度档位**（含每层限额、用量、是否生效/超限）。

        控制台用它展示「这个人现在到底按哪条额度算」；与闸门判定共用
        ``gate.layers_for`` 与 ``gate.quota_hit``，保证界面显示的生效档位
        就是真正在生效的那一个（避免两套优先级逻辑漂移）。

        Returns:
            每项形如 ``{layer, label, scope_type, scope_id, limits, usage, effective, exceeded}``；
            ``limits`` / ``usage`` 都是 ``{period: ...}`` 形式。
        """
        try:
            subject = self._quota_subject(scope_type, scope_id)
            if subject is None:
                return []
            rules = self._rules()
            refs = self.gate.layers_for(subject, rules)
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        taken = False  # 已经命中最具体的一层 → 更粗的层不再参与
        for ref in refs:
            rows = dict(rules.limits_of(ref.scope_type, ref.quota_key))
            raw_usage = rules.usage_of(ref.usage_type, ref.usage_id)
            limits = {
                period: {
                    "limit_tokens": int((row or {}).get("limit_tokens") or 0),
                    "mode": str((row or {}).get("mode") or "enforce"),
                    "reset_at": (row or {}).get("reset_at"),
                }
                for period, row in rows.items()
                if int((row or {}).get("limit_tokens") or 0) > 0
            }
            usage = {
                period: int((raw_usage.get(period) or {}).get("used_tokens") or 0)
                for period in ("day", "month", "total")
            }
            hit = self.gate.quota_hit(rows, raw_usage) if rows else None
            effective = bool(limits) and not taken
            if effective:
                taken = True
            out.append(
                {
                    "layer": ref.layer,
                    "label": ref.label,
                    "scope_type": ref.scope_type,
                    "scope_id": ref.scope_id,
                    "limits": limits,
                    "usage": usage,
                    "effective": effective,
                    "exceeded": bool(hit),
                    "hit": hit or {},
                },
            )
        return out

    def model_route_of(self, scope_type: str, scope_id: str) -> dict[str, Any]:
        """给控制台用：某对象当前**会走哪个模型**（含来源等级与实际选中的提供商）。

        ``available`` 一起返回，前端可以就地标出「配置的提供商当前不可用」。
        """
        try:
            subject = Subject(sender_id=scope_id) if scope_type == "user" else Subject(group_id=scope_id)
            route = self.gate.resolve_model(subject, self._rules()) or {}
            available = self.provider_ids()
            out: dict[str, Any] = {**route, "available": sorted(available), "circuit_open": sorted(self.circuit.open_ids())}
            if route:
                picked = self.gate.pick_provider(route, available - self.circuit.open_ids()) or {}
                out.update(picked)
                if not picked:
                    out["reason"] = "配置的提供商当前不可用（未加载或已熔断），本次会走 AstrBot 默认模型"
            # 专属模型（v1.3.12）：好友 = 好友专属（仅私聊）、群 = 群专属（对全群生效）。
            # /切换模型 改的也是这一份，控制台要能看见它，否则会出现
            # 「界面上写等级模型、实际走另一个」的悬案。
            out["subject_model"] = (
                (self._subject_model.get(str(scope_id)) or "")
                if scope_type == "user"
                else (self._subject_model_group.get(str(scope_id)) or "")
            )
            return out
        except Exception as e:
            logger.debug(f"[UserGateway] 解析模型路由失败（忽略）: {e}")
            return {}

    def command_master_of(
        self, scope_type: str, scope_id: str, scene: str = "private"
    ) -> dict[str, Any]:
        """给控制台用：某对象的**指令权限**（自己配的值 + 实际生效的层）。

        Args:
            scene: ``private`` / ``group`` —— 好友这一层是分场景的，控制台要按当前场景取。

        - ``effect``：该对象在当前场景下配的三态值（``inherit`` = 没单独配）；
        - ``resolved``：按档位链算出来的最终结论（含等级 / 全局 / 配置默认）；
        - ``layer_label``：这个结论是哪来的（好友专属 / 好友等级 / … / 系统默认）。
        """
        try:
            subject = self._probe_subject(scope_type, scope_id, scene)
            eff, layer, st, sid = self.gate.check_command_master(subject, self._rules())
            explicit = effect_in_scene(
                (self._cmd_master.get(scope_type) or {}).get(scope_id), scene
            )
            if not eff:
                return {
                    "effect": explicit or "inherit",
                    "resolved": self.gate.default_command_effect(),
                    "layer": "",
                    "layer_label": "系统默认",
                    "scope_type": "global",
                    "scope_id": "*",
                }
            return {
                "effect": explicit or "inherit",
                "resolved": eff,
                "layer": layer,
                "layer_label": layer_label(layer),
                "scope_type": st,
                "scope_id": sid,
            }
        except Exception as e:
            logger.debug(f"[UserGateway] 解析指令权限失败（忽略）: {e}")
            return {}

    async def command_matrix_of(
        self, scope_type: str, scope_id: str, scene: str = "private"
    ) -> dict[str, Any]:
        """给控制台用：**某对象 × 所有指令** 的可用性矩阵。

        回答「这个人 / 这个群 / 这一等级到底能用哪些指令、以及为什么」——
        以前只能在「指令」页逐条看规则（或逐个对象跑一下才知道），看不到整体结论。

        Args:
            scope_type: ``user`` / ``group`` / ``member`` / ``level`` / ``global``
            scope_id: 对象 id（``member`` 是「群号:QQ」，``level`` 是等级 id，``global`` 传 ``*``）
            scene: 好友这一层分场景，``private`` / ``group``

        Returns:
            ``{scope_type, scope_id, scene, editable, summary, rows}``；
            ``rows`` 每项含 ``{name, desc, plugin, aliases, allow, layer, layer_label,
            explicit}``。``editable=False``（等级）表示这一层不支持单条指令规则。
        """
        scene = "group" if str(scene) == "group" else "private"
        rules = self._rules()
        level_kind = ""
        probe = "__probe__"

        if scope_type == "level":
            # 等级本身不是一个「会话对象」，得造一个探针对象并临时把它归到该等级，
            # 才能跑出「这一档人」的结论。用 dataclasses.replace 派生一份临时快照，
            # 不动插件里那份（热路径正在用）。
            try:
                lv = await self.store.get_level(int(scope_id)) if self.store else None
            except Exception:
                lv = None
            if not lv:
                return {}
            level_kind = "group" if str(lv.get("kind")) == "group" else "user"
            merged = {**((rules.subject_level or {}).get(level_kind) or {}), probe: int(lv["id"])}
            rules = dataclasses.replace(
                rules,
                subject_level={**(rules.subject_level or {}), level_kind: merged},
            )
            if level_kind == "group":
                scene, subject = "group", Subject(group_id=probe)
            elif scene == "group":
                subject = Subject(sender_id=probe, group_id="__scene_probe__")
            else:
                subject = Subject(sender_id=probe)
        else:
            subject = self._probe_subject(scope_type, scope_id, scene)

        def explicit_of(cmd: str) -> str:
            """该对象在这条指令上的**显式**规则（没配 = inherit）。"""
            if scope_type == "level":
                return "inherit"  # 单条指令不参与等级层（等级只管整体开关，见 gate.check_command）
            row = (self._cmd_policy.get(cmd) or {}).get(scope_type) or {}
            return effect_in_scene(row.get(scope_id), scene) or "inherit"

        rows: list[dict[str, Any]] = []
        by_layer: dict[str, int] = {}
        allowed = 0
        for c in self.registered_commands():
            name = str(c.get("name") or "")
            if not name:
                continue
            v = self.gate.check_command(subject, name, rules)
            if v.allow:
                allowed += 1
            layer = v.layer or "global"
            by_layer[layer] = by_layer.get(layer, 0) + 1
            rows.append(
                {
                    **c,
                    "command": name,
                    "allow": bool(v.allow),
                    "layer": v.layer,
                    "layer_label": layer_label(v.layer) if v.layer else "系统默认",
                    "explicit": explicit_of(name),
                }
            )
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "scene": scene,
            "level_kind": level_kind,
            # 等级层不支持单条指令规则（设计如此），前端据此隐藏编辑入口
            "editable": scope_type != "level" and bool(rows),
            "summary": {
                "total": len(rows),
                "allowed": allowed,
                "denied": len(rows) - allowed,
                "by_layer": by_layer,
            },
            "rows": rows,
        }

    @staticmethod
    def _probe_subject(scope_type: str, scope_id: str, scene: str) -> Subject:
        """构造一个「探针」Subject，用来在指定场景下跑档位链。

        群聊场景必须有 ``group_id`` 才会走群聊那条链（群专属 / 群等级）。控制台问的是
        「这个好友在群里会怎样」，所以用一个**不存在的探针群号**：它在库里没有任何
        专属规则与归级，对应档位自然落空，不会扰动结论。

        - ``user`` → 私聊（或群聊场景用探针群号）
        - ``group`` → 该群
        - ``member`` → 该群里那个人（真实组合，两个 id 都是真的）
        - ``level`` / ``global`` → 用一个不存在的探针对象，档位链自然落到对应层
        """
        if scope_type == "member":
            gid, uid = parse_member_scope_id(scope_id)
            return Subject(sender_id=uid, group_id=gid)
        if scope_type == "group":
            return Subject(group_id=scope_id)
        if scope_type == "user":
            if scene == "group":
                return Subject(sender_id=scope_id, group_id="__scene_probe__")
            return Subject(sender_id=scope_id)
        # level / global：探针对象，库里没有它的任何规则
        return Subject(sender_id="__probe__")

    def _quota_subject(self, scope_type: str, scope_id: str) -> Optional[Subject]:
        """把「额度作用对象」转成一个用于跑档位链的 ``Subject``。

        - ``user``   → 私聊里的这个人（会走 好友专属 → 好友等级 → 全局）
        - ``group``  → 这个群（会走 群专属 → 群等级 → 全局）
        - ``member`` → 这个人在这个群（会走 群成员专属 → 好友专属 → … → 群 → 全局）
        """
        sid = str(scope_id or "")
        if not sid:
            return None
        if scope_type == "member":
            gid, uid = parse_member_scope_id(sid)
            if not (gid and uid):
                return None
            return Subject(sender_id=uid, group_id=gid)
        if scope_type == "group":
            return Subject(group_id=sid)
        return Subject(sender_id=sid)

    def _effective_limit(self, scope_type: str, scope_id: str) -> Optional[tuple[str, dict]]:
        """取某对象**生效**的那一层限额 ``(layer, {period: row})``。

        直接复用闸门的档位链（``gate.layers_for``），而不是再手写一条 ——
        以前这里是手写的副本，加了「群成员」这一层后很容易和闸门漂移。
        仅用于额度预警文案（真正的拦截判定在 ``Gate.check_quota``）。
        """
        subject = self._quota_subject(scope_type, scope_id)
        if subject is None:
            return None
        rules = self._rules()
        for ref in self.gate.layers_for(subject, rules):
            rows = rules.limits_of(ref.scope_type, ref.scope_id)
            if rows:
                return ref.layer, dict(rows)
        return None

    def _usage_table(self, scope_type: str) -> dict[str, dict[str, dict[str, Any]]]:
        """取某维度的内存用量计数表（闸门热路径与预警共用同一份）。"""
        if scope_type == "user":
            return self._usage_user
        if scope_type == "group":
            return self._usage_group
        return self._usage_member

    def _bump_memory_usage(self, scope_type: str, scope_id: str, tokens: int) -> list[dict]:
        """把刚消费的 token 同步进内存用量计数，返回本次新触发的额度预警。

        用量计数表在内存里按需创建，保证下一次闸门判定立刻看到新用量
        （不必等下一次 reload_rules）。
        """
        if tokens <= 0:
            return []
        table = self._usage_table(scope_type)
        rows = table.setdefault(str(scope_id), {})
        before: dict[str, int] = {}
        for period in quota_mod.PERIODS:
            row = rows.get(period)
            if row is None:
                row = {
                    "scope_type": scope_type,
                    "scope_id": str(scope_id),
                    "period": period,
                    "used_tokens": 0,
                    "reset_at": None,
                }
                rows[period] = row
            before[period] = int(row.get("used_tokens") or 0)
            row["used_tokens"] = before[period] + tokens

        found = self._effective_limit(scope_type, scope_id)
        if not found:
            return []
        layer, limits = found
        ratio = float(self._cfg("warn_ratio", 0.8) or 0)
        warns: list[dict] = []
        for period in quota_mod.PERIODS:
            row = limits.get(period)
            if not row:
                continue
            limit = int(row.get("limit_tokens") or 0)
            if limit <= 0:
                continue
            after = before[period] + tokens
            # 只在「跨过阈值的那一刻」报一次，避免每次调用都刷屏
            if quota_mod.should_warn(after, limit, ratio) and not quota_mod.should_warn(before[period], limit, ratio):
                warns.append(
                    {
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "layer": layer,
                        "period": period,
                        "used": after,
                        "limit": limit,
                        "percent": round(after * 100 / limit, 1),
                    },
                )
        return warns

    async def _log_denied(self, subject: Subject, verdict: Any, blocked: bool) -> None:
        """写一条拦截/观察流水。``blocked=False`` 表示观察模式（放行但记录）。"""
        try:
            await self.store.log_usage(
                platform_id=subject.platform_id,
                umo=subject.umo,
                scope_type=verdict.scope_type or ("group" if subject.group_id else "user"),
                scope_id=verdict.scope_id or (subject.group_id or subject.sender_id),
                sender_id=subject.sender_id,
                group_id=subject.group_id,
                kind="llm",
                status="denied" if blocked else "ok",
                deny_reason=verdict.reason if blocked else f"observe:{verdict.reason}",
            )
        except Exception as e:
            logger.warning(f"[UserGateway] 写拦截流水失败（忽略）: {e}")

    async def _notify_denied(self, event: AstrMessageEvent, subject: Subject, verdict: Any) -> None:
        """给被拒用户发提示（带冷却），必要时同时通知管理员。"""
        key = f"{verdict.scope_type}:{verdict.scope_id}"
        if not self._notify_cooldown.allow(key):
            return

        if not self._cfg("silent", False):
            text = str(self._cfg("deny_notice", "") or "").strip()
            if text:
                await self._send(event, text)

        if self._cfg("notify_admin", False):
            who = subject.sender_id or subject.umo
            where = f"群 {subject.group_id}" if subject.group_id else "私聊"
            await self._notify_admins(
                f"已拦截一次 LLM 请求：{who}（{where}）｜原因 {verdict.reason}｜{verdict.detail}",
                subject.platform_id,
            )

    @staticmethod
    async def _send(event: AstrMessageEvent, text: str) -> None:
        """直发一条文本。

        必须是**直发**而不是 yield：闸门随后会 stop_event，yield 出去的结果不会被发送
        （pipeline 在 stop 后中断）。
        """
        try:
            await event.send(MessageChain([Plain(str(text))]))
        except Exception as e:
            logger.warning(f"[UserGateway] 发送提示失败（忽略）: {e}")

    async def _send_image(self, event: AstrMessageEvent, png: bytes, *, recall_sec: int = 0) -> bool:
        """直发一张图片（同样必须直发）。

        走 ``Image.fromBytes``（``base64://``）：不落盘、不需要清理临时文件；
        aiocqhttp 适配器本来也会把图片统一转成 base64 再发（见
        ``aiocqhttp_message_event._from_segment_to_dict``）。

        Args:
            recall_sec: 多少秒后自动撤回这张图（``0`` = 不撤；非 QQ 平台没有撤回接口，
                会自动退化成「只发不撤」，见 :mod:`recall`）。

        Returns:
            True = 发送成功（失败时调用方可以退回文本列表，别让用户什么都收不到）。
        """
        try:
            chain = MessageChain([Image.fromBytes(png)])
            if recall_sec > 0:
                await self.recaller.send(event, chain, recall_sec)
            else:
                await event.send(chain)
            return True
        except Exception as e:
            logger.warning(f"[UserGateway] 发送图片失败（退回文本）: {e}")
            return False

    async def _notify_admins(self, text: str, platform_id: str) -> None:
        """给全局配置里的管理员发私聊通知（best-effort，失败只记日志）。

        UMO 拼接依据 aiocqhttp 适配器：私聊 session_id 就是对方 QQ，
        故 ``platform_id:FriendMessage:{admin_qq}``。
        """
        try:
            cfg = self.context.get_config()
            admins = list((cfg or {}).get("admins_id", []) or [])
        except Exception as e:
            logger.warning(f"[UserGateway] 读取管理员列表失败（忽略通知）: {e}")
            return
        if not admins:
            return
        for admin in admins:
            aid = str(admin or "").strip()
            if not aid:
                continue
            umo = f"{platform_id}:FriendMessage:{aid}" if platform_id else ""
            if not umo:
                continue
            try:
                await self.context.send_message(umo, MessageChain([Plain(str(text))]))
            except Exception as e:
                logger.warning(f"[UserGateway] 通知管理员 {aid} 失败（忽略）: {e}")

    # ------------------------------------------------------------------ #
    # 后台维护：额度重置 + 冷却清理
    # ------------------------------------------------------------------ #
    async def _maintenance_loop(self) -> None:
        """每分钟一次：把到期的额度清零并重建内存缓存；顺带清理冷却表。"""
        while self._running:
            try:
                await asyncio.sleep(60)
                await self._reset_stale_quotas()
                self._notify_cooldown.prune()
                await self._ensure_send_hook()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[UserGateway] 维护任务异常（忽略）: {e}")

    async def _reset_stale_quotas(self) -> None:
        """按 ``reset_at`` 清零到期的用量计数（日 / 月周期），并重建内存缓存。

        v2 起用量在 ``usage_counter``，额度行不再自带用量，所以重置的对象是计数行。
        """
        if not (self.store and self.store.ready):
            return
        now = int(time.time())
        rows = await self.store.counters_due(now)
        reset_count = 0
        for row in rows:
            period = str(row.get("period") or "")
            try:
                await self.store.reset_used(
                    scope_type=str(row.get("scope_type") or ""),
                    scope_id=str(row.get("scope_id") or ""),
                    period=period,
                    reset_at=quota_mod.next_reset_at(period),
                )
                reset_count += 1
            except Exception as e:
                logger.warning(f"[UserGateway] 重置用量失败（忽略）: {e}")
        if reset_count:
            logger.info(f"[UserGateway] 已重置 {reset_count} 条到期用量计数")
            await self.reload_rules()

    # ------------------------------------------------------------------ #
    # M2：记录 bot 最后一条消息（控制台「最后回复」列的数据来源）
    # ------------------------------------------------------------------ #
    def _install_send_hook(self) -> None:
        """给 ``AstrMessageEvent.send`` 挂上记录钩子（幂等，可重复调用）。

        为什么不用官方的 ``@filter.after_message_sent``：该钩子只在 respond 阶段的
        **非流式**路径触发（``respond/stage.py`` 里空消息链、流式结果、重复文本等多处
        提前 return），拿不到 ① 流式回复；② 插件里直接 ``await event.send(...)`` 的主动
        发送（本插件自己的拦截提示就属于这一类）。
        而 ``send()`` 是所有适配器最终都会经过的公共出口（aiocqhttp 的 ``send()``
        末尾同样是 ``await super().send(...)``），在这里记录覆盖最全。

        钩子只做「读事件 + 写一行小表」，且整体包在 try 里：记录失败绝不影响发送。
        """
        try:
            from astrbot.core.platform.astr_message_event import AstrMessageEvent as _Event
        except Exception as e:
            logger.warning(f"[UserGateway] 无法安装发送记录钩子（最后回复将不可见）: {e}")
            return
        if getattr(_Event.send, "_ugw_hooked", False):
            return
        original = _Event.send

        async def _send_with_record(self, message, *args, **kwargs):
            plugin = _ACTIVE_PLUGIN
            if plugin is not None:
                try:
                    await plugin._record_bot_message(self, message)
                except Exception as e:  # 记录失败绝不阻断发送
                    logger.debug(f"[UserGateway] 记录最后消息失败（忽略）: {e}")
            return await original(self, message, *args, **kwargs)

        _send_with_record._ugw_hooked = True  # type: ignore[attr-defined]
        _send_with_record._ugw_original = original  # type: ignore[attr-defined]
        _Event.send = _send_with_record  # type: ignore[method-assign]
        logger.info("[UserGateway] 已挂载「bot 最后消息」记录钩子（AstrMessageEvent.send）")

    @staticmethod
    def _restore_send_hook() -> None:
        """还原 ``AstrMessageEvent.send``（插件卸载 / 重载时调用）。"""
        try:
            from astrbot.core.platform.astr_message_event import AstrMessageEvent as _Event

            original = getattr(getattr(_Event, "send", None), "_ugw_original", None)
            if original is not None:
                _Event.send = original  # type: ignore[method-assign]
        except Exception:
            pass

    async def _ensure_send_hook(self) -> None:
        """确认「bot 最后消息」钩子还挂在类上。

        其它插件若用同样手段 patch ``AstrMessageEvent.send`` 且**没有链式调用**
        我们打过的补丁，钩子就会被顶掉——表现为「最后回复」从某一刻起停止更新。
        维护循环每分钟检查一次，丢了就重新挂载并告警。
        """
        try:
            from astrbot.core.platform.astr_message_event import AstrMessageEvent as _Event

            if not getattr(_Event.send, "_ugw_hooked", False):
                logger.warning(
                    "[UserGateway] 「bot 最后消息」钩子被其它插件覆盖（最后回复会停更），已重新挂载"
                )
                self._install_send_hook()
        except Exception:
            pass

    @staticmethod
    def _classify_reply(event: AstrMessageEvent) -> tuple[str, str]:
        """判断这条 bot 消息属于哪一类：``llm`` / ``command`` / ``normal``。

        依据（都用「事件上的既有标记」，不猜文本）：

        - ``llm``：本插件在 ``on_llm_request`` 里给事件打了 ``_ugw_llm`` 标，
          只有真的要走 LLM 才会打标；
        - ``command``：waking 阶段会把命中的 handler 写进 ``activated_handlers``
          （``waking_check/stage.py:253``），带指令过滤器的就是指令消息；
        - 其余（其它插件主动推送、关键词回复等）算 ``normal``。

        这里按**类名**判断过滤器类型而不是 import AstrBot 的过滤器类，跨版本更稳。
        """
        try:
            if event.get_extra("_ugw_llm", False):
                return "llm", ""
        except Exception:
            pass
        try:
            handlers = event.get_extra("activated_handlers") or []
        except Exception:
            handlers = []
        for h in handlers:
            for f in getattr(h, "event_filters", []) or []:
                if type(f).__name__ in ("CommandFilter", "CommandGroupFilter"):
                    return "command", str(getattr(f, "command_name", "") or "")
        return "normal", ""

    @staticmethod
    def _chain_preview(chain: Any) -> str:
        """把消息链压成一句预览：有文本取文本，纯富媒体给 ``[Image]`` 之类的占位。"""
        try:
            comps = list(getattr(chain, "chain", None) or [])
        except Exception:
            return ""
        texts: list[str] = []
        for comp in comps:
            t = getattr(comp, "text", None)
            if isinstance(t, str) and t.strip():
                texts.append(t.strip())
        if texts:
            return " ".join(texts)[:120]
        names = [type(c).__name__ for c in comps]
        return ("[" + "/".join(names[:3]) + "]") if names else ""

    def _note_record_failure(self, e: Exception) -> None:
        """「最后回复」记账失败的告警（5 分钟节流，避免刷日志）。"""
        now = time.time()
        if now - getattr(self, "_last_rec_fail_log", 0.0) > 300:
            self._last_rec_fail_log = now
            logger.warning(f"[UserGateway] 记录「bot 最后回复」失败（5 分钟内不再重复提示）: {e}")

    async def _touch_bot_reply(
        self,
        event: AstrMessageEvent,
        kind: str,
        command: str = "",
        preview: str = "",
    ) -> None:
        """把一条「bot 回复」记入最后消息表（send 钩子与用量 / 指令记账共用）。

        私聊记在对方（好友）名下，群聊记在群名下；流式回复按段多次到达，
        只接受更晚的时间戳（秒级，乱序旧段直接丢弃）。
        """
        if not (self.store and self.store.ready):
            return
        if not self._cfg("track_bot_messages", True):
            return
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:
            sid = ""
        scope_type, scope_id = ("group", gid) if gid else ("user", sid)
        if not scope_id:
            return
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""

        ts = int(time.time())
        key = f"{scope_type}:{scope_id}"
        if ts < self._bot_msg_seq.get(key, 0):
            return
        if len(self._bot_msg_seq) > 5000:  # 防御性清理，避免长期运行内存增长
            self._bot_msg_seq.clear()
        self._bot_msg_seq[key] = ts

        await self.store.set_bot_message(
            scope_type,
            scope_id,
            kind,
            ts=ts,
            command=command,
            preview=(preview or "")[:120],
            platform_id=platform_id,
        )

    async def _record_bot_message(self, event: AstrMessageEvent, chain: Any) -> None:
        """send 钩子入口：分类这条 bot 消息并记录（每个会话只留最新一条）。"""
        if not (self.store and self.store.ready):
            return
        if not self._cfg("track_bot_messages", True):
            return
        kind, command = self._classify_reply(event)
        try:
            await self._touch_bot_reply(event, kind, command, self._chain_preview(chain))
        except Exception as e:
            self._note_record_failure(e)
