"""卡片字体查找顺序的自检：投放目录要能盖过插件自带，读不动的要跳过去。

用法：
    uv run --no-project --with pillow python tests/test_model_card_font.py

为什么单独测这个：Docker 容器里经常一个中文字体都没有，用户的解法是往挂载卷的
``data/fonts/`` 丢一个字体文件。这条链路一旦排错顺序，表现是「我明明放了文件，
卡片还是原来那个字体」或者干脆「图片没了变成纯文本」——两种都很难联想到字体查找。

覆盖：
- ``dropin_font_dirs``：拿得到 data 根目录时给两个位置，拿不到时为空（不抛错）；
- 目录内按名字优先级排序，非字体文件与子目录被过滤掉；
- 查找顺序：配置指定 → 投放目录 → 插件自带 → 系统候选；
- **投放目录里的字体要盖过插件自带**（否则用户放文件等于白放）；
- 读不动的字体（扩展名对但内容损坏）要被跳过并顺势回落到下一档，而不是让整张卡降级；
- **缓存只记成功**：先按配置填一个还没拷进去的路径、之后再补文件，必须不重启就认得到。
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# model_card 只在函数里惰性 import astrbot，这里给一个可切换的桩
if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("test")  # type: ignore[attr-defined]
    _astrbot.api = _api  # type: ignore[attr-defined]
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

import model_card as MC  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  MISS ") + label)
    if not cond:
        _failures.append(label)


def set_data_root(root: str | None) -> None:
    """把 ``get_astrbot_data_path`` 换成指向临时目录的实现；传 None 表示「核心没这个助手」。"""
    if root is None:
        sys.modules.pop("astrbot.core.utils.astrbot_path", None)
        sys.modules.pop("astrbot.core.utils", None)
        sys.modules.pop("astrbot.core", None)
        return
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: root  # type: ignore[attr-defined]
    core.utils = utils  # type: ignore[attr-defined]
    utils.astrbot_path = path_mod  # type: ignore[attr-defined]
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.utils"] = utils
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod


def main() -> None:
    bundled = next(iter(sorted(MC.ASSETS_DIR.glob("*.woff2"))), None)
    if bundled is None:
        # 仓库里没有随包字体时（例如精简检出），用系统字体当「能加载的字体」样本
        bundled = next((Path(p) for p in MC.SYSTEM_FONT_CANDIDATES if Path(p).is_file()), None)
    assert bundled is not None, "本机找不到任何可加载的字体样本，测不了"

    tmp = Path(tempfile.mkdtemp(prefix="ug_font_"))
    data = tmp / "data"
    shared = data / "fonts"
    private = data / "plugin_data" / "astrbot_plugin_moe_permission" / "fonts"
    shared.mkdir(parents=True)
    private.mkdir(parents=True)

    print("[投放目录解析]")
    set_data_root(str(data))
    dirs = MC.dropin_font_dirs()
    check(len(dirs) == 2 and dirs[0] == private and dirs[1] == shared,
          f"插件专属在前、共享 data/fonts 在后：{[str(d) for d in dirs]}")
    set_data_root(None)
    check(MC.dropin_font_dirs() == [], "核心没有 data 路径助手时返回空，而不是抛错打断渲染")
    set_data_root(str(data))

    print("[目录内排序与过滤]")
    (shared / "LXGWWenKai.woff2").write_bytes(bundled.read_bytes())
    (shared / "aaa-zzz-other.woff2").write_bytes(bundled.read_bytes())
    (shared / "readme.txt").write_text("不是字体")
    (shared / "sub").mkdir()
    ranked = [p.name for p in MC._fonts_in(shared)]
    check(ranked == ["LXGWWenKai.woff2", "aaa-zzz-other.woff2"],
          f"文楷优先、其余垫底，非字体与子目录被滤掉：{ranked}")
    check(MC._fonts_in(shared / "sub") == [], "空目录返回空列表")
    check(MC._fonts_in(tmp / "不存在") == [], "目录不存在返回空列表")

    print("[查找顺序：投放目录盖过插件自带]")
    got = MC.find_font_path("")
    check(got == shared / "LXGWWenKai.woff2", f"默认命中投放目录里优先级最高的那份：{got}")
    check(got != bundled, "投放目录有货时**不**该返回插件自带的那份")
    got2 = MC.find_font_path(str(private / "put-here.woff2"))
    check(got2 == shared / "LXGWWenKai.woff2", "配置项指向不存在的文件时继续往下找，不返回 None")
    shutil.copyfile(bundled, private / "put-here.woff2")
    check(MC.find_font_path("") == private / "put-here.woff2",
          "插件专属目录排在共享目录之前")
    (private / "put-here.woff2").unlink()

    print("[配置指定优先级最高]")
    check(MC.find_font_path(str(bundled)) == bundled, "填了绝对路径就以它为准")

    print("[读不动的要跳过去]")
    bogus = tmp / "坏字体.ttf"
    bogus.write_bytes(b"definitely not a font file")
    check(MC.find_font_path(str(bogus)) is not None,
          "指定的路径读不动时回落到其它候选，而不是让整张卡降级成纯文本")
    check(not MC._loadable(bogus), "_loadable 认得出损坏文件")
    check(MC._loadable(bundled), "_loadable 认得出现随包的 woff2（Pillow 能直接读）")

    print("[缓存只记成功]")
    MC._FONT_LOAD_OK.clear()
    first = MC.find_font_path(str(bogus))
    check(str(bogus) not in MC._FONT_LOAD_OK,
          "读不动的路径**不**进缓存 —— 否则以后把文件补上也会永远跳过它")
    check(len(MC._FONT_LOAD_OK) >= 1 and all(MC._FONT_LOAD_OK.values()), "缓存里只留成功的那些")
    n = len(MC._FONT_LOAD_OK)
    second = MC.find_font_path(str(bogus))
    check(first == second and len(MC._FONT_LOAD_OK) == n, "同一批好字体只验一次，不重复解析字体头")

    print("[先填路径、后放文件]")
    later = shared / "later.woff2"
    check(MC.find_font_path(str(later)) is not None, "路径还不存在时先回落到别的候选")
    check(MC.find_font_path(str(later)) != later, "文件没到位时不会假装命中")
    later.write_bytes(bundled.read_bytes())
    check(MC.find_font_path(str(later)) == later,
          "文件补上后不必重启就认（配置指哪就是哪）")
    later.unlink()

    print("[没有投放目录时仍能出卡]")
    for p in shared.iterdir():
        if p.is_file():
            p.unlink()
    got3 = MC.find_font_path("")
    check(got3 is not None and got3 != shared / "LXGWWenKai.woff2",
          f"投放目录空了会退回自带 / 系统：{got3}")

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("\n失败 %d 项" % len(_failures))
    for f in _failures:
        print("  - " + f)
    sys.exit(1 if _failures else 0)
