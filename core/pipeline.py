"""把 detect → prepare → extract → translate → apply → finalize 串成一条流水线。

GUI 和 CLI 共用这一份，避免两边逻辑漂移。

关键设计
--------
* **每一步都可中断**：长任务（反编译、跑几千条翻译）都通过 `should_stop` 回调
  检查，用户点「取消」能立刻停，且已翻好的部分留在缓存里，下次接着来。
* **每步都可单跑**：`Pipeline.run(steps=["extract"])` 这样用，调试/分步执行
  不必全量重跑。
* **进度是一条流**：所有对外输出走 `emit(kind, payload)`，GUI 挂到日志区和进度条，
  CLI 挂到 stdout。
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .detect import Detection, detect
from .engines import get_adapter
from .llm import Backend, Translator

STEPS = ["detect", "prepare", "extract", "glossary", "translate", "apply", "finalize"]

_KANA_RE = None

# 日文新字体 / 和制汉字 / 日文专用符号：这些字在简体中文里不会出现。
# 只用假名判断不够 ——「回復薬」「図書館」「駅前」没有假名，但一眼是日文；
# 「艾蕾娜」「回复药」是中文。靠字形差集才判得准。
# 注意：这里只能放「简体中文绝对不会用」的字。像 残/当/画/简/铁/亚 这些
# 简中常用字一个都不能放进去，否则中文译文会被误判成日文、反复重翻。
_JA_ONLY = set(
    "薬図駅読売変発実検験単戦対続経覚帰込働達様歳円気曜鉄"
    "伝値軽県専圧広応悪仮価絵亜圏雑歩園齢竜滝桜浜渋渓沢辺増徳恵涙焼塩蔵臓児斎"
    "剤済穏聴覧観訳証説豊賛転遅髪喫髭塁涙渉瀬瀬歩"
    "々〆ヶヵ"
)


def _is_already_chinese(s: str) -> bool:
    """判断一行是否已经是中文（用来实现「重复运行不重复翻」的幂等性）。

    三条判据，按可靠性排序：
      1. 有假名（平假名/片假名）-> 一定是日文
      2. 有日文特有形声/新字体 -> 一定是日文
      3. 有汉字、无假名、无日文特异字 -> 中文（含纯人名如「艾蕾娜」）
    纯符号（「……」）当未翻译处理。
    """
    global _KANA_RE
    import re as _re
    if _KANA_RE is None:
        _KANA_RE = _re.compile(r"[\u3040-\u30ff]")
    if _KANA_RE.search(s):
        return False
    if any(c in _JA_ONLY for c in s):
        return False
    han = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return han >= 2


@dataclass
class PipelineOptions:
    target: str = ""
    backend: Optional[Backend] = None
    glossary: dict[str, str] = field(default_factory=dict)
    batch_size: int = 20
    context_lines: int = 2
    max_escalate: int = 3
    # 并发请求数。串行翻 19000 句要十几小时，实测 4 路不会触发限速。
    concurrency: int = 4
    out_dir: str = ""
    # 只翻译、不改文件（预览模式）
    dry_run: bool = False
    # 跳过已有译文（只补漏翻）
    resume: bool = True
    # 自动生成术语表（用模型从台词里找专有名词，配合下面的 auto_glossary_save）
    auto_glossary: bool = False
    # 生成后写到哪。空 = 只用在本次翻译，不落盘
    auto_glossary_save: str = ""
    # 自动生成的候选门槛：出现几次才算候选
    glossary_min_freq: int = 3
    # Windows 版 Ren'Py：换成 Linux 引擎，让它在本机原生跑（不需要 Wine）
    port_to_linux: bool = False
    # 指定从哪儿借 Linux 引擎（留空 = 自动找）
    engine_src: str = ""
    # 主模型翻不出来的句子交给 NVIDIA Nemotron 兜底。
    # DeepSeek 那边对露骨性描写是内容过滤直拒 + 流中途掐断，实测 3% 过不去；
    # nemotron-3-ultra 同一批 20/20 全过。key 从 ~/.dsh/.credentials.yaml 的
    # refs.NVIDIA_API_KEY 读，没有就自动关掉。
    nv_fallback: bool = True
    # 留空 = nemotron-3-ultra-550b（质量最好）；备选 super-vl-120b / lightning-30b
    nv_model: str = ""
    # 第三道兜底：前两道（大模型）都拒的句子交给谷歌翻译。
    # 机器翻译不内容过滤，剩下的露骨句它能翻；质量差一档，但比留英文强。
    # 需要代理（gfallback 自己探环境变量与本机 7890）。
    gf_fallback: bool = True
    # 收尾时把译文里混入的繁体统一成简体（zhconv 的 zh-cn 档，连词汇一起转）。
    # 模型偶尔会滑到繁体，实测 TLS 8483 条译文有 1047 条混了繁体字。
    simplify: bool = True
    # 汉化完成后给游戏建一个带原图标的启动快捷方式（.desktop）。
    # 图标一律用游戏自带的（Ren'Py 的 window_icon.png / MZ 的 icon/icon.png），
    # 找不到才退回按名字生成的字母图标。
    make_shortcut: bool = True
    # 顺便往桌面目录也放一份（GNOME 需要 trusted 标记才会显示成图标）
    shortcut_on_desktop: bool = True
    # 快捷方式的显示名；留空 = 自动取（package.json 的 window.title 或目录名）
    shortcut_title: str = ""


@dataclass
class PipelineResult:
    ok: bool = False
    engine: str = ""
    variant: str = ""
    root: str = ""
    extracted: int = 0
    unique: int = 0
    translated: int = 0
    applied: int = 0
    deferred: int = 0
    refused: int = 0
    elapsed: float = 0.0
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    json_path: str = ""
    # NVIDIA 兜底接手的句子数（主模型翻不了的那些）
    nv_used: int = 0
    # 谷歌翻译兜底接手的句子数（前两道都拒的那些）
    gf_used: int = 0
    # 繁转简清洗了几条
    simplified: int = 0
    # 术语表统计（自动生成时才有意义）
    glossary_terms: int = 0
    glossary_added: int = 0
    # Windows → Linux 移植结果（一句话）
    ported: str = ""
    # 快捷方式
    shortcut_path: str = ""
    shortcut_icon: str = ""        # original / generated
    shortcut_launch: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"失败：{self.error}"
        lines = [
            f"引擎 {self.engine}" + (f" ({self.variant})" if self.variant else ""),
            f"提取 {self.extracted} 条（唯一 {self.unique}）",
            f"译出 {self.translated} 条，写入 {self.applied} 条",
        ]
        if self.deferred:
            lines.append(f"未成功 {self.deferred} 条（已保留原文，不是脏数据）")
        if self.refused:
            lines.append(f"触发拒绝重试 {self.refused} 次")
        if self.nv_used:
            lines.append(f"NVIDIA Nemotron 兜底 {self.nv_used} 句")
        if self.gf_used:
            lines.append(f"谷歌翻译兜底 {self.gf_used} 句")
        if self.simplified:
            lines.append(f"繁转简清洗 {self.simplified} 条")
        if self.glossary_terms:
            lines.append(f"术语表 {self.glossary_terms} 条"
                         + (f"（新增 {self.glossary_added}）" if self.glossary_added else ""))
        if self.shortcut_path:
            src = "游戏原图标" if self.shortcut_icon == "original" else "生成的字母图标"
            lines.append(f"启动快捷方式已创建（{src}）")
        lines.append(f"耗时 {self.elapsed:.1f}s")
        return " / ".join(lines[:2]) + "\n" + "\n".join(lines[2:])


class Pipeline:
    def __init__(self, opts: PipelineOptions,
                 emit: Optional[Callable[[str, object], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None):
        self.opts = opts
        self._emit = emit or (lambda kind, payload: None)
        self._should_stop = should_stop or (lambda: False)
        self.det: Optional[Detection] = None
        self.items: list[dict] = []
        self.cache_path = ""
        self.result = PipelineResult()
        self.port_result = None

    # ------------------------------------------------------------ 事件
    def log(self, msg: str):
        self._emit("log", msg)

    def stage(self, name: str, pct: Optional[float] = None):
        self._emit("stage", {"name": name, "pct": pct})

    def progress(self, done: int, total: int):
        self._emit("progress", {"done": done, "total": total})

    def stopped(self) -> bool:
        return self._should_stop()

    # ------------------------------------------------------------ 主流程
    def run(self, steps: Optional[list[str]] = None) -> PipelineResult:
        steps = steps or STEPS
        t0 = time.time()
        r = self.result
        try:
            if "detect" in steps:
                self._step_detect()

            if not self.det or not self.det.ok:
                r.error = "无法识别游戏引擎"
                r.elapsed = time.time() - t0
                return r

            root = self.det.root
            r.engine, r.variant, r.root = self.det.engine, self.det.variant, str(root)
            r.warnings.extend(self.det.warnings)

            adapter = get_adapter(self.det.engine, self.det.variant)
            if adapter is None:
                r.error = f"{self.det.engine} 还没有适配器"
                r.elapsed = time.time() - t0
                return r

            if "prepare" in steps:
                self.stage("prepare", 0.05)
                self.log(f"[准备] {self.det.summary()} @ {root}")
                adapter.prepare(root, self.log)
                # Ren'Py 特有的"能开但卡"问题：补 libGLU、选 X11。
                # 这里做一次顺手修，用户点开游戏就不用自己折腾。
                if self.det.engine == "renpy":
                    try:
                        from . import renv
                        if renv.is_windows_build(root):
                            self.log("[运行环境] Windows 版：翻译不依赖本机能跑它。")
                            if self.opts.port_to_linux:
                                from . import winport
                                pr = winport.port(root, engine_src=self.opts.engine_src)
                                self.log(f"[移植] {pr.summary()}")
                                for n in pr.notes:
                                    self.log(f"[移植] {n}")
                                self.port_result = pr
                            else:
                                self.log("[移植] 未开启。加 --port-to-linux 可换成 Linux 引擎"
                                         "原生运行（不需要 Wine）")
                        else:
                            for line in renv.diagnose(root):
                                self.log(f"[运行环境] {line}")
                            renv_env = renv.prepare(root)
                            for line in renv_env.notes:
                                self.log(f"[运行环境] {line}")
                    except Exception as e:
                        self.log(f"[运行环境] 检查失败（不影响汉化）: {e}")

            if "extract" in steps:
                self.stage("extract", 0.15)
                self.log("[提取] 扫描文本…")
                self.items = adapter.extract(root, self.log)
                if not self.items:
                    r.error = "没有提取到任何文本"
                    r.elapsed = time.time() - t0
                    return r
                # 幂等由适配器保证：RPG Maker 从 _bak_data 原始快照读，
                # Ren'Py 每次整目录重建 tl/chinese。这里不需要逐行猜语言
                # （「残念」「当然」这类纯汉字日文词字形上和中文无法区分）。
                r.extracted = len(self.items)
                r.unique = len({i["src"] for i in self.items})
                self._dump_json()

            if "glossary" in steps and self.opts.auto_glossary:
                if self.opts.backend and self.items:
                    self.stage("glossary", 0.2)
                    self._step_glossary()

            if "translate" in steps:
                if not self.opts.backend:
                    r.error = "没有配置翻译模型"
                    r.elapsed = time.time() - t0
                    return r
                self.stage("translate", 0.25)
                self._step_translate()

            if "apply" in steps:
                self.stage("apply", 0.9)
                if self.opts.dry_run:
                    self.log("[回填] 预览模式，跳过写入")
                else:
                    self.log("[回填] 写入游戏…")
                    r.applied = adapter.apply(root, self.items, self.log)
                    self._dump_json()

            if self.port_result is not None:
                r.ported = self.port_result.summary()

            if "finalize" in steps:
                self.stage("finalize", 0.97)
                adapter.finalize(root, self.log)

            if "finalize" in steps and self.opts.make_shortcut and not self.opts.dry_run:
                self.stage("shortcut", 0.99)
                self._step_shortcut()

            r.ok = True
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"
            self.log("[错误] " + r.error)
            self.log(traceback.format_exc(limit=3))
        finally:
            r.elapsed = time.time() - t0
            self.stage("done", 1.0)
        return r

    # ------------------------------------------------------------ 各步
    def _step_detect(self):
        self.stage("detect", 0.01)
        self.log(f"[识别] {self.opts.target}")
        self.det = detect(self.opts.target)
        if self.det.ok:
            self.log(f"  → {self.det.summary()}，根目录 {self.det.root}")
            for e in self.det.evidence:
                self.log(f"    · {e}")
            for w in self.det.warnings:
                self.log(f"    ! {w}")
        else:
            self.log("  → 没能识别。已知线索：")
            for e in self.det.evidence:
                self.log(f"    · {e}")

    def _step_glossary(self):
        """从已提取的台词里生成术语表，写回 self.opts.glossary。

        放在 translate 之前、extract 之后——此时 items 已在手，不用重新扫盘。
        生成失败不阻断流程：术语表只是让译名更统一，没有它照样能翻。
        """
        from . import glossary as gl

        self.log("[术语] 从台词里筛专有名词…")
        try:
            merged = gl.build(
                self.opts.backend, self.items,
                root=self.det.root if self.det else None,
                engine=self.det.engine if self.det else "",
                existing=self.opts.glossary,
                log=self.log,
                min_freq=self.opts.glossary_min_freq,
            )
        except Exception as e:
            self.log(f"[术语] 生成失败（不影响翻译）: {e}")
            return

        added = len(merged) - len(self.opts.glossary)
        self.opts.glossary = merged
        self.result.glossary_terms = len(merged)
        self.result.glossary_added = added
        self.log(f"[术语] 共 {len(merged)} 条（本次新增 {added} 条）")

        if added and self.opts.auto_glossary_save:
            try:
                gl.save(self.opts.auto_glossary_save, merged)
                self.log(f"[术语] 已写入 {self.opts.auto_glossary_save}")
            except Exception as e:
                self.log(f"[术语] 写文件失败: {e}")
        elif added:
            self.log("[术语] 只在本次生效，没落盘（可在界面上点「保存术语表」）")

    def _out_dir(self) -> Path:
        """输出目录。显式指定优先，否则用游戏目录旁边的 _galgame_tl/。

        注意"旁边"要看情况：从 .exe 自解压出来的游戏本来就住在
        `~/下载/_galgame_tl/<包名>/`，再拼一层就成了
        `_galgame_tl/_galgame_tl/`（GUI 上真出现过）。父目录已经叫
        `_galgame_tl` 时直接用父目录。
        """
        if self.opts.out_dir:
            return Path(self.opts.out_dir)
        parent = self.det.root.parent
        if parent.name == "_galgame_tl":
            return parent
        return parent / "_galgame_tl"

    def _step_translate(self):
        out_dir = self._out_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = str(out_dir / f"{self.det.root.name}.cache.json")

        tr = Translator(
            backend=self.opts.backend,
            glossary=self.opts.glossary,
            cache_path=self.cache_path,
            batch_size=self.opts.batch_size,
            context_lines=self.opts.context_lines,
            max_escalate=self.opts.max_escalate,
            log=self.log,
            concurrency=self.opts.concurrency,
            nv_fallback=self.opts.nv_fallback,
            nv_model=self.opts.nv_model,
            gf_fallback=self.opts.gf_fallback,
        )
        if tr.nv_fallback:
            self.log(f"[翻译] NVIDIA 兜底已启用（{tr.nv_model or 'nemotron-3-ultra'}）"
                     f"：主模型过不去的露骨内容交给它")
        if tr.gf_fallback:
            self.log("[翻译] 谷歌翻译兜底已启用（第三道）：前两道都拒的句子交给它，"
                     "人名仍走术语表")
        # 历史缓存里可能留着旧版 protect/restore 的脏数据（占位符闭合符被模型
        # 抄成形近字、控制码被吃掉）。不清的话这些句子命中缓存，脏译文会被
        # 原样写回游戏。清洗器只删不猜，修不好的条目下次当未翻译重翻。
        if tr.cache:
            from . import cachefix
            cleaned, cstats = cachefix.clean(tr.cache, self.opts.glossary)
            n_bad = len(tr.cache) - len(cleaned)
            if n_bad:
                self.log(f"[缓存] 清洗出 {cstats.get('repaired', 0)} 条可修复、"
                         f"{n_bad - cstats.get('repaired', 0)} 条需重翻，已更新缓存")
                tr.cache = cleaned
                tr._save_cache()
        self.log(f"[翻译] 模型 {self.opts.backend.model} @ {self.opts.backend.base_url}"
                 f"（{self.opts.concurrency} 路并发）")
        if self.opts.glossary:
            self.log(f"[翻译] 术语表 {len(self.opts.glossary)} 条")

        def on_progress(done, total):
            self.progress(done, total)
            self._emit("stage", {"name": "translate",
                                 "pct": 0.25 + 0.6 * (done / max(1, total))})

        res = tr.translate_items(self.items, progress=on_progress,
                                 stop_flag=_StopFlag(self._should_stop))
        self.result.translated = res["stats"]["translated"]
        self.result.deferred = res["stats"]["deferred"]
        self.result.refused = res["stats"]["refused"]
        self.result.nv_used = res["stats"].get("nv_used", 0)
        self.result.gf_used = res["stats"].get("gf_used", 0)
        # 简体化清洗：模型偶尔滑到繁体（实测 TLS 有 12% 的句子混了繁体字），
        # 这里统一转成大陆简体，连词汇一起（软体→软件、网路→网络）。
        # 放在写回 items 之前，缓存、text.json、游戏数据三处保持一致。
        if self.opts.simplify and tr.cache:
            from . import simplify as sp
            ok_s, why = sp.available()
            if ok_s:
                tr.cache, n_fix = sp.clean_mapping(tr.cache, self.log)
                self.result.simplified = n_fix
                if n_fix:
                    tr._save_cache()      # 清洗后的缓存要重落盘，否则下次又读到繁体
            else:
                self.log(f"[清洗] 跳过：{why}")

        # 把译文写回 items
        for it in self.items:
            d = tr.cache.get(it["src"])
            if d:
                it["dst"] = d
        if res["failed"]:
            preview = "、".join(s[:24] for s in res["failed"][:4])
            self.log(f"[翻译] {len(res['failed'])} 条未成功（保留原文）: {preview}…")
            self._emit("deferred", res["failed"])

    def _step_shortcut(self):
        """收尾时给游戏建启动快捷方式。

        放在 finalize 之后：此时文件已经回填完、字体补丁也写好了，
        快捷方式指向的启动器才是"已经改好的那个游戏"。
        失败只记日志，不影响汉化结果。
        """
        from . import shortcut as sc

        try:
            res = sc.create(
                Path(self.det.root) if self.det else Path(self.opts.target),
                det=self.det,
                title=self.opts.shortcut_title,
                also_desktop=self.opts.shortcut_on_desktop,
                log=self.log,
            )
        except Exception as e:
            self.log(f"[快捷方式] 生成失败（不影响汉化）: {e}")
            return
        if not res.ok:
            self.log(f"[快捷方式] {res.error}")
            return
        self.result.shortcut_path = res.desktop_path
        self.result.shortcut_icon = res.icon_source
        self.result.shortcut_launch = res.launch
        self.log(f"[快捷方式] {res.summary()}")

    def _dump_json(self):
        out_dir = self._out_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{self.det.root.name}.text.json"
        payload = {
            "meta": {
                "root": str(self.det.root),
                "engine": self.det.engine,
                "variant": self.det.variant,
                "count": len(self.items),
                "unique": len({i["src"] for i in self.items}),
            },
            "items": self.items,
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        self.result.json_path = str(p)
        self.log(f"[保存] {p}")


class _StopFlag:
    """给 Translator 用的 stop_flag 适配（它只要求有 is_set()）"""
    def __init__(self, fn):
        self._fn = fn

    def is_set(self):
        return self._fn()
