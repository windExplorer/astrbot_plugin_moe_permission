"""插件改名迁移与「标识全链路一致性」的自检。

用法：
    uv run --no-project python tests/test_rename_migration.py

改名最容易翻车的不是代码逻辑，而是**漏改一处硬编码**：路由前缀少改一边，
表现为控制台整页接口 404；字体目录少改一处，表现为用户放了字体却不生效；
打包清单漏了新模块，表现为仓库里有文件、用户装的 zip 里没有迁移代码，
老数据静默丢失。所以本脚本除了测搬迁行为，还做一遍源码级静态守卫。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import rename_migration as RM  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  MISS ") + label)
    if not cond:
        _failures.append(label)


class FakeConfig(dict):
    """模拟 AstrBotConfig：dict 子类 + config_path / first_deploy / save_config。"""

    def __init__(self, path: Path, first_deploy: bool = False, **kw: Any) -> None:
        super().__init__(**kw)
        self.config_path = str(path)
        if first_deploy:
            self.first_deploy = True
        self.saved = 0

    def save_config(self) -> None:
        self.saved += 1


def test_constants() -> None:
    print("[改名常量]")
    check(RM.OLD_PLUGIN_NAME == "astrbot_plugin_user_gateway", "旧名定位准确")
    check(RM.NEW_PLUGIN_NAME == "astrbot_plugin_moe_permission", "新名与 metadata 一致")
    check(RM.DB_FILE_NAME == "user_gateway.db",
          "库文件名保持不变（改了反而读不到搬过来的数据）")
    check(RM.OLD_CONFIG_FILE == "astrbot_plugin_user_gateway_config.json",
          "旧配置名按 AstrBot 的 <root_dir_name>_config.json 规则拼")


def test_data_dir(tmp: Path) -> None:
    print("[数据目录搬迁]")
    plugin_data = tmp / "plugin_data"
    new_dir = plugin_data / RM.NEW_PLUGIN_NAME
    old_dir = plugin_data / RM.OLD_PLUGIN_NAME
    new_dir.mkdir(parents=True)  # 模拟 StarTools.get_data_dir 的 ensure_dir

    check(RM.migrate_data_dir(new_dir) == 0, "没有旧目录时什么都不做")

    (old_dir / "avatars").mkdir(parents=True)
    (old_dir / "fonts").mkdir()
    (old_dir / RM.DB_FILE_NAME).write_bytes(b"sqlite-main")
    (old_dir / "user_gateway.db-wal").write_bytes(b"wal")
    (old_dir / "user_gateway.db-shm").write_bytes(b"shm")
    (old_dir / "avatars" / "10001.jpg").write_bytes(b"avatar")
    (old_dir / "fonts" / "lxgw.woff2").write_bytes(b"font")

    logs: list[str] = []
    moved = RM.migrate_data_dir(new_dir, logs.append)
    check(moved == 5, f"5 个文件全部搬走（含 wal/shm 与两级子目录）：{moved}")
    check((new_dir / RM.DB_FILE_NAME).read_bytes() == b"sqlite-main", "主库内容一致")
    check((new_dir / "user_gateway.db-wal").read_bytes() == b"wal", "WAL 一并搬迁")
    check((new_dir / "avatars" / "10001.jpg").read_bytes() == b"avatar", "头像缓存保留目录结构")
    check((new_dir / "fonts" / "lxgw.woff2").exists(), "投放字体一并搬迁")
    check((old_dir / RM.DB_FILE_NAME).exists(), "旧文件保留不动（便于回退 / 卸载旧插件）")
    check(any("改名迁移" in s for s in logs), "搬迁写了可检索的日志")

    check(RM.migrate_data_dir(new_dir) == 0, "可重入：第二次跑不再搬任何文件")

    # 目标已存在的文件绝不能被旧数据覆盖
    (new_dir / RM.DB_FILE_NAME).write_bytes(b"NEW-DATA")
    (old_dir / "late.txt").write_bytes(b"late")
    (new_dir / "late.txt").write_bytes(b"MUST-KEEP")
    (old_dir / "extra.txt").write_bytes(b"extra")
    moved2 = RM.migrate_data_dir(new_dir)
    check(moved2 == 1, "只搬目标里还没有的文件")
    check((new_dir / RM.DB_FILE_NAME).read_bytes() == b"NEW-DATA", "不覆盖改名后新产生的数据")
    check((new_dir / "late.txt").read_bytes() == b"MUST-KEEP", "同名文件保留新版本")
    check((new_dir / "extra.txt").read_bytes() == b"extra", "新增的旧文件仍会补搬")


def test_config(tmp: Path) -> None:
    print("[配置搬迁]")
    cfg_dir = tmp / "config"
    cfg_dir.mkdir()
    new_cfg_path = cfg_dir / "astrbot_plugin_moe_permission_config.json"
    old_cfg_path = cfg_dir / RM.OLD_CONFIG_FILE

    check(RM.migrate_config({}) == 0, "配置对象退化成普通 dict 时安全跳过")

    cfg = FakeConfig(new_cfg_path, first_deploy=True, llm_guard_enabled=True)
    check(RM.migrate_config(cfg) == 0, "旧配置不存在时不搬")

    old_cfg_path.write_text(json.dumps({"llm_guard_enabled": False, "quota_mode": "block"}),
                            encoding="utf-8")
    cfg = FakeConfig(new_cfg_path, first_deploy=True, llm_guard_enabled=True)
    n = RM.migrate_config(cfg)
    check(n == 2, f"first_deploy 时搬入 2 个键：{n}")
    check(cfg["quota_mode"] == "block" and cfg["llm_guard_enabled"] is False,
          "旧值整体覆盖 schema 默认值")
    check(cfg.saved == 1, "搬完立刻 save_config 落盘")

    cfg2 = FakeConfig(new_cfg_path, first_deploy=False, llm_guard_enabled=True)
    check(RM.migrate_config(cfg2) == 0,
          "非首次部署不搬（否则会把用户后来改的配置覆盖回旧值）")
    check(cfg2.saved == 0, "不写盘")

    old_cfg_path.write_bytes(b"\xef\xbb\xbf{broken json")
    check(RM.migrate_config(FakeConfig(new_cfg_path, first_deploy=True)) == 0,
          "旧配置是坏 JSON（含 BOM）时降级，不抛异常")
    old_cfg_path.write_text(json.dumps([]), encoding="utf-8")
    check(RM.migrate_config(FakeConfig(new_cfg_path, first_deploy=True)) == 0,
          "旧配置不是对象时跳过")


def test_entry(tmp: Path) -> None:
    print("[入口组合]")
    plugin_data = tmp / "plugin_data2"
    new_dir = plugin_data / RM.NEW_PLUGIN_NAME
    old_dir = plugin_data / RM.OLD_PLUGIN_NAME
    new_dir.mkdir(parents=True)
    old_dir.mkdir()
    (old_dir / RM.DB_FILE_NAME).write_bytes(b"db")
    cfg_dir = tmp / "config2"
    cfg_dir.mkdir()
    (cfg_dir / RM.OLD_CONFIG_FILE).write_text('{"a": 1}', encoding="utf-8")
    cfg = FakeConfig(cfg_dir / "astrbot_plugin_moe_permission_config.json", first_deploy=True)
    files, keys = RM.migrate_renamed_plugin(new_dir, cfg)
    check((files, keys) == (1, 1), f"入口返回 (文件数, 配置键数)：{(files, keys)}")


def test_static_guards() -> None:
    print("[源码静态守卫：标识全链路一致]")
    meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    name = re.search(r"(?m)^name:\s*(\S+)", meta).group(1)
    check(name == RM.NEW_PLUGIN_NAME, "metadata.yaml 的 name 与新名一致")
    check(f"windExplorer/{RM.NEW_PLUGIN_NAME}" in meta, "repo 指向新仓库")

    main_py = (ROOT / "main.py").read_text(encoding="utf-8")
    check(f'PLUGIN_NAME = "{RM.NEW_PLUGIN_NAME}"' in main_py,
          "main.py 的 PLUGIN_NAME（@register / get_data_dir 都吃它）")
    check("migrate_renamed_plugin" in main_py, "main.py 接入了迁移入口")
    check('"rename_migration"' in main_py, "新模块在热重载清单里")

    webui = (ROOT / "webui_api.py").read_text(encoding="utf-8")
    check(f'ROUTE_PREFIX = "/{RM.NEW_PLUGIN_NAME}"' in webui, "后端路由前缀")
    api_ts = (ROOT / "webui-src/src/api.ts").read_text(encoding="utf-8")
    check(f'PAGE_PLUGIN_NAME = "{RM.NEW_PLUGIN_NAME}"' in api_ts,
          "前端端点候选前缀与后端路由一致")

    pack = (ROOT / "build_zip.ps1").read_text(encoding="utf-8")
    check(f'$pluginName = "{RM.NEW_PLUGIN_NAME}"' in pack, "zip 顶层目录名")
    check('"rename_migration.py"' in pack, "新模块进了打包清单")

    card = (ROOT / "model_card.py").read_text(encoding="utf-8")
    check(f'"plugin_data", "{RM.NEW_PLUGIN_NAME}", "fonts"' in card, "字体投放目录")
    schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    check(f"plugin_data/{RM.NEW_PLUGIN_NAME}/fonts" in schema, "配置 hint 里的投放路径")

    # 旧名只允许出现在迁移模块的常量、CHANGELOG 的改名说明里
    allowed = {"rename_migration.py", "CHANGELOG.md"}
    leftovers: list[str] = []
    for ext in ("*.py", "*.ts", "*.ps1", "*.json", "*.yaml", "*.md"):
        for p in ROOT.glob(ext):
            if p.name in allowed:
                continue
            if RM.OLD_PLUGIN_NAME in p.read_text(encoding="utf-8"):
                leftovers.append(p.name)
    for p in (ROOT / "webui-src/src").rglob("*.ts"):
        if RM.OLD_PLUGIN_NAME in p.read_text(encoding="utf-8"):
            leftovers.append(f"webui-src/src/{p.name}")
    check(not leftovers, f"没有漏改的源码硬标识：{leftovers}")


def main() -> None:
    test_constants()
    tmp = Path(tempfile.mkdtemp(prefix="moe_perm_mig_"))
    try:
        test_data_dir(tmp)
        test_config(tmp)
        test_entry(tmp)
        test_static_guards()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("\n失败 %d 项" % len(_failures))
    for f in _failures:
        print("  - " + f)
    sys.exit(1 if _failures else 0)
