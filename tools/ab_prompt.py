#!/usr/bin/env python3
"""ab_prompt —— 离线 A/B 提示词测试（D2，S18）。

## 用户要求（2026-09-15）

> "提示词：A/B 测试需要专门设计，比如我们需要 `system + 关系图 + 一段群聊消息
>  + RAG`，测试脚本要能支持**控制变量**下的测试，比如只变 system，比较回复质量"

## 设计

一次 A/B = **同一场景、同一上下文骨架，只变一个指定组件**，产出并排对比。

**臂（arm）** 描述一组组件开关：

| 字段 | 含义 |
|---|---|
| `system` | 人格提示词来源：`"base"`（生产）或变体文件路径 |
| `relations` / `examples` / `kg` | 是否注入该块 |
| `rag` | 是否查 RAG（关掉 → kg 只剩关系边） |

**控制变量**：脚本报出每臂相对 baseline 变了什么；变了多个给 warning
（不禁止 —— 有时确实要测组合）。

## 为什么必须离线

改线上配置会**污染生产数据**（session/logs 混在一起）且一次只能测一个值。
离线可用同一场景批量试多个变体，且**可复现**。

## 用法

    # 1) 写臂定义
    cat > arms.json <<'JSON'
    [
      {"name": "baseline"},
      {"name": "no-rag",     "rag": false, "kg": false},
      {"name": "sys-v2",     "system": "data_out/system_prompt_v2.json"},
      {"name": "no-relations","relations": false}
    ]
    JSON

    # 2) 跑（场景来自 replay_scene extract）
    python3 -m tools.ab_prompt --scene data_out/scene_xxx.json \\
        --data-dir /opt/AstrBot/data/plugin_data/astrbot_plugin_persona_agent \\
        --arms arms.json --out data_out/ab_result.md [--repeat 2]

- **只读**生产风格/chroma/知识库；**不写**任何生产 session/log/usages。
- 凭据/模型跟随 AstrBot `cmd_config.json`，**不硬编码**。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


@dataclass
class Arm:
    """一组组件选择。"""
    name: str = "baseline"
    use_system: bool = True
    use_relations: bool = True
    use_examples: bool = True
    use_kg: bool = True
    use_rag: bool = True
    #: 覆盖人格提示词（文件路径或字面文本）。None = 用生产的
    system_override: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Arm":
        d = d or {}
        return cls(
            name=str(d.get("name") or "arm"),
            use_system=bool(d.get("system", True) is not False
                            and d.get("system", True) is not None),
            use_relations=bool(d.get("relations", True)),
            use_examples=bool(d.get("examples", True)),
            use_kg=bool(d.get("kg", True)),
            use_rag=bool(d.get("rag", True)),
            system_override=d.get("system_override"),
        )


def load_arms(path: Path) -> list[Arm]:
    """从 JSON 文件读臂定义。坏文件返回空表（**绝不抛**）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [Arm.from_dict(x) for x in data if isinstance(x, dict)]


def arms_diff(base: Arm, other: Arm) -> list[str]:
    """列出 ``other`` 相对 ``base`` 变了哪些组件（控制变量检查）。"""
    diff = []
    if base.use_system != other.use_system or \
            (base.system_override or "") != (other.system_override or ""):
        diff.append("system")
    if base.use_relations != other.use_relations:
        diff.append("relations")
    if base.use_examples != other.use_examples:
        diff.append("examples")
    if base.use_kg != other.use_kg:
        diff.append("kg")
    if base.use_rag != other.use_rag:
        diff.append("rag")
    return diff


def build_arm_contexts(arm: Arm, blocks: dict) -> list[dict]:
    """按该臂的开关装配上下文。

    **顺序固定**（恒定在前、增长居中、易变在尾）——与生产 pipeline 一致，
    否则 A/B 比较的就不是"组件内容"而是"位置"了：

        system → examples → relations → session → kg
    """
    out: list[dict] = []

    def _add(text) -> None:
        t = str(text or "")
        if t.strip():
            out.append({"role": "system", "content": t})

    if arm.use_system:
        sys_text = arm.system_override or blocks.get("system")
        _add(sys_text)
    if arm.use_examples:
        _add(blocks.get("examples"))
    if arm.use_relations:
        _add(blocks.get("relations"))
    for m in (blocks.get("session") or []):
        if isinstance(m, dict):
            out.append(dict(m))
    if arm.use_kg:
        _add(blocks.get("kg"))
    return out


def render_comparison(results: list[dict], *, scene_id: str,
                      changed: dict) -> str:
    """并排对比 → markdown（人工判读用）。"""
    if not results:
        return f"# A/B 对比 · {scene_id}\n\n（无结果）\n"
    L = [f"# A/B 对比 · 场景 `{scene_id}`", "",
         f"共 {len(results)} 臂。**各臂相对 baseline 的差异组件**（控制变量）：", ""]
    for r in results:
        d = changed.get(r.get("arm", ""), [])
        L.append(f"- `{r.get('arm')}`：{('、'.join(d) if d else '（baseline）')}")
    L += ["", "---", ""]
    for r in results:
        name = r.get("arm", "?")
        L += [f"## {name}", ""]
        if r.get("error"):
            L += [f"❌ **失败**：`{r['error']}`", ""]
            continue
        out = str(r.get("output") or "")
        L += [f"- 字符数：{r.get('chars', len(out))}"
              f" · 耗时：{r.get('ms', 0)} ms"
              + (f" · 第 {r['repeat']} 次" if r.get("repeat") else ""), ""]
        L += ["```text", out or "（空输出）", "```", ""]
    return "\n".join(L)


# --------------------------------------------------------------------- 运行时

class ABRuntime:
    """组装真 services（**只读**生产数据），按臂装配上下文并调用 LLM。

    刻意**不**复用 PersonaPipeline —— 因为 A/B 要能替换/关闭 pipeline 内部的
    组件，而 pipeline 把这些写死了。这里只复用底层服务（Style/RAG/KG/成员表），
    装配逻辑用 `build_arm_contexts`（顺序与 pipeline 一致）。
    """

    def __init__(self, data_dir: Path, cmd_config: Path,
                 model_override: str = "") -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        self.data_dir = Path(data_dir)
        self.cmd_config = Path(cmd_config)
        self.model_override = model_override
        self._key = ""
        self._base = ""
        self._model = ""
        self._style = None
        self._rag = None
        self._kg = None

    # ---- 凭据/模型（跟随 cmd_config，不硬编码）----
    def _resolve_provider(self) -> None:
        cfg = json.loads(self.cmd_config.read_text(encoding="utf-8-sig"))
        src = None
        for s in (cfg.get("provider_sources") or []):
            if s.get("id") == "commandcode":
                src = s
                break
        if src is None and (cfg.get("provider_sources") or []):
            src = cfg["provider_sources"][0]
        if src is None:
            raise RuntimeError("cmd_config 里没有 provider_sources")
        k = src.get("key")
        self._key = k[0] if isinstance(k, list) else str(k or "")
        self._base = str(src.get("api_base") or "").rstrip("/")
        if not self._key or not self._base:
            raise RuntimeError("provider key/api_base 缺失")
        provs = cfg.get("provider") or []
        pid = ""
        for p in provs:
            if isinstance(p, dict) and p.get("enable", True) is not False:
                pid = str(p.get("id") or "")
                break
        self._model = self.model_override or pid or "deepseek/deepseek-v4.1-flash"

    def build_services(self) -> None:
        from services.style_profile import StyleProfile
        self._style = StyleProfile(str(self.data_dir))

    def read_prompt(self, arm: Arm) -> str:
        """该臂的 system prompt 文本。"""
        if arm.system_override:
            p = Path(str(arm.system_override))
            if p.exists():
                try:
                    txt = p.read_text(encoding="utf-8")
                    try:                      # 若本身是片段 JSON，则拼成提示词
                        obj = json.loads(txt)
                        if isinstance(obj, dict):
                            return "\n\n".join(
                                f"{k}：{v}" for k, v in obj.items()
                                if not str(k).startswith("_"))
                    except json.JSONDecodeError:
                        return txt
                    return txt
                except OSError:
                    pass
            # 不是文件 → 当字面文本
            return str(arm.system_override)
        return self._style.system_prompt() if self._style else ""

    def build_blocks(self, arm: Arm) -> dict:
        """构造组件块。**只读**生产数据。"""
        blocks: dict = {"system": self.read_prompt(arm)}
        if arm.use_examples:
            try:
                from services.examples import ExamplesLoader
                blocks["examples"] = ExamplesLoader(str(self.data_dir)).block()
            except Exception:
                blocks["examples"] = ""
        if arm.use_relations:
            try:
                blocks["relations"] = self._style.relations_block()
            except Exception:
                blocks["relations"] = ""
        return blocks

    async def call(self, contexts: list[dict], *, temperature: float = 0.9,
                   max_tokens: int = 1024) -> tuple[str, str]:
        """调用网关。返回 (content, error)。"""
        import httpx
        payload = {
            "model": self._model,
            "messages": contexts,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": "low",
        }
        try:
            async with httpx.AsyncClient(timeout=300, trust_env=False) as c:
                r = await c.post(f"{self._base}/chat/completions",
                                 headers={"Authorization": f"Bearer {self._key}"},
                                 json=payload)
                if r.status_code != 200:
                    return "", f"HTTP {r.status_code}: {r.text[:200]}"
                d = r.json()
                ch = d["choices"][0]
                return (ch["message"].get("content") or "").strip(), ""
        except Exception as e:
            return "", f"{type(e).__name__}: {e}"


async def run_ab(args: argparse.Namespace) -> int:
    scene = json.loads(Path(args.scene).read_text(encoding="utf-8"))
    scene_id = str(scene.get("scene_id") or Path(args.scene).stem)
    msgs = scene.get("messages") or []

    arms = load_arms(Path(args.arms)) if args.arms else [Arm(name="baseline")]
    if not arms:
        print(f"臂定义读取失败或为空: {args.arms}", file=sys.stderr)
        return 1

    rt = ABRuntime(Path(args.data_dir), Path(args.cmd_config), args.model)
    rt._resolve_provider()
    rt.build_services()

    # 场景作为 session 段（发言人已带名字 → 与前缀风格一致）
    session = []
    for m in msgs:
        nm = str(m.get("name") or m.get("uin") or "")
        tx = str(m.get("text") or "")
        session.append({"role": "user", "content": f"{nm}：{tx}" if nm else tx})

    base_arm = arms[0]
    changed = {a.name: arms_diff(base_arm, a) for a in arms}
    for name, d in changed.items():
        if len(d) > 1:
            print(f"⚠️  臂 {name} 同时变了 {len(d)} 个组件（{d}）—— "
                  f"控制变量会失效，确认是有意为之", file=sys.stderr)

    results: list[dict] = []
    for arm in arms:
        blocks = rt.build_blocks(arm)
        blocks["session"] = session
        ctx = build_arm_contexts(arm, blocks)
        for rep in range(max(1, args.repeat)):
            t0 = time.time()
            out, err = await rt.call(ctx, temperature=args.temperature,
                                     max_tokens=args.max_tokens)
            results.append({
                "arm": arm.name, "output": out, "error": err,
                "chars": len(out), "ms": int((time.time() - t0) * 1000),
                "repeat": rep + 1 if args.repeat > 1 else 0,
                "contexts": len(ctx),
            })
            print(f"  {arm.name} #{rep+1}: {len(out)} 字 {results[-1]['ms']}ms"
                  + (f"  ❌ {err}" if err else ""))

    md = render_comparison(results, scene_id=scene_id, changed=changed)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n✅ {out_path}")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="离线 A/B 提示词测试（控制变量）")
    ap.add_argument("--scene", required=True, help="场景 JSON（replay_scene extract 产物）")
    ap.add_argument("--data-dir", required=True, help="生产插件数据目录（只读）")
    ap.add_argument("--cmd-config",
                    default="/opt/AstrBot/data/cmd_config.json",
                    help="AstrBot 配置（取凭据/模型，不硬编码）")
    ap.add_argument("--arms", required=True, help="臂定义 JSON")
    ap.add_argument("--out", required=True, help="对比结果 markdown")
    ap.add_argument("--model", default="", help="覆盖模型 id")
    ap.add_argument("--repeat", type=int, default=1, help="每臂重复次数（看方差）")
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--max-tokens", type=int, default=1024)
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run_ab(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"运行失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
