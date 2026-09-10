"""llbot_config — 从 AstrBot 配置推导 LLBot v8 的 OneBot11 反向 WS 连接配置。

背景（2026-09-10 NapCat → LLBot 迁移）：
  AstrBot 的 `aiocqhttp` 适配器**只支持反向 WebSocket**（`use_ws_reverse=True`，
  aiocqhttp 注册 `/ws`、`/ws/event`、`/ws/api` 三条路径），默认监听 `0.0.0.0:6199`。
  LLBot 侧需要在 `ob11.connect[]` 里加一条 `type=ws-reverse` 指向该地址。
  LLBot 官方文档《对接 AstrBot》给出的正是这一组合。

本工具把「两侧配置必须一致」这件事变成可执行校验，消除手工抄错的可能：
  - url      ← AstrBot 的 `ws_reverse_host` / `ws_reverse_port`
  - token    ← AstrBot 的 `ws_reverse_token`（LLBot 发 `Authorization: Bearer <t>`，
               aiocqhttp 校验同一 header → 必须一致）
  - messageFormat 恒为 **array**：AstrBot 对非 list 的 `message` 会
              `logger.critical` + 往群里主动发错误文本 + `raise ValueError`（刷屏级故障）

安全（security-secrets）：
  - token **绝不**打印到 stdout/日志；终端输出一律掩码（`<set:N chars>`）。
  - `--out` / `--merge-into` 写出的文件 chmod 600；写入前自动备份、原子写。
  - 读取兼容 UTF-8 BOM（AstrBot 的 cmd_config.json 实测带 BOM，json.load 会直接报错）。

幂等：`--merge-into` 只更新/新增那一条 ws-reverse 连接，保留 connect[] 中其它条目。

用法：
    # 1) 打印推导结果（token 掩码）
    python -m tools.llbot_config --astrbot-config /opt/AstrBot/data/cmd_config.json

    # 2) 导出可直接合并的片段（含真 token，0600）
    python -m tools.llbot_config --astrbot-config ... --out llbot_ob11_ws_reverse.json

    # 3) 就地合并进 LLBot 配置（备份 + 原子写）
    python -m tools.llbot_config --astrbot-config ... --merge-into <LLBot>/data/config.json

    # 4) 校验已有 LLBot 配置与 AstrBot 是否一致
    python -m tools.llbot_config --astrbot-config ... --check <LLBot>/data/config.json

退出码：0 = 一致/成功；1 = 不一致或有问题项（check 模式）；2 = 错误。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

WS_REVERSE_TYPE = "ws-reverse"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6199
DEFAULT_HEART_INTERVAL = 60000


# --------------------------------------------------------------------- helpers

def load_json(path: Path) -> Any:
    """读 JSON；兼容 UTF-8 BOM（AstrBot WebUI 写出的文件带 BOM）。"""
    return json.loads(path.read_text("utf-8-sig"))


def write_json_private(path: Path, data: Any) -> None:
    """原子写 + chmod 600（含 token，按机密文件对待）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def backup(path: Path) -> Optional[Path]:
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{stamp}")
    bak.write_bytes(path.read_bytes())
    return bak


def mask_secret(value: Any) -> str:
    """掩码显示：绝不回显 token 本身。"""
    if value is None or value == "":
        return "<empty>"
    return f"<set:{len(str(value))} chars>"


def normalize_host(host: str) -> str:
    """把监听地址映射为可连接的地址（同机部署）。

    AstrBot 默认监听 0.0.0.0 / :: → LLBot 在本机应连 127.0.0.1 / [::1]。
    """
    h = (host or "").strip()
    if h in ("", "0.0.0.0", "*"):
        return DEFAULT_HOST
    if h == "::":
        return "[::1]"
    if ":" in h and not h.startswith("["):  # 裸 IPv6
        return f"[{h}]"
    return h


def ws_reverse_url(host: str, port: int) -> str:
    return f"ws://{normalize_host(host)}:{int(port)}/ws"


# ------------------------------------------------------------------ derivation

def find_aiocqhttp_platform(astrbot_config: dict) -> Optional[dict]:
    """在 AstrBot cmd_config.json 中定位 aiocqhttp 平台条目。"""
    if not isinstance(astrbot_config, dict):
        return None
    platforms = astrbot_config.get("platform")
    if not isinstance(platforms, list):
        return None
    for entry in platforms:
        if isinstance(entry, dict) and entry.get("type") == "aiocqhttp":
            return entry
    return None


def build_connect_entry(
    host: str,
    port: int,
    token: str,
    *,
    heart_interval: int = DEFAULT_HEART_INTERVAL,
    report_self_message: bool = False,
    report_offline_message: bool = False,
    debug: bool = False,
) -> dict:
    """构造 LLBot ob11.connect[] 的 ws-reverse 条目。

    messageFormat 恒为 array —— 见模块 docstring（AstrBot 硬要求，不可配成 string）。
    """
    return {
        "type": WS_REVERSE_TYPE,
        "name": "AstrBot",
        "enable": True,
        "url": ws_reverse_url(host, port),
        "heartInterval": int(heart_interval),
        "token": token or "",
        "messageFormat": "array",
        "reportSelfMessage": bool(report_self_message),
        "reportOfflineMessage": bool(report_offline_message),
        "debug": bool(debug),
    }


def derive_entry(astrbot_config: dict, args) -> tuple[Optional[dict], list[str]]:
    """从 AstrBot 配置推导 LLBot 连接条目；返回 (entry, problems)。"""
    problems: list[str] = []
    platform = find_aiocqhttp_platform(astrbot_config)
    if platform is None:
        return None, ["AstrBot 配置中找不到 type=aiocqhttp 的平台条目"]
    if not platform.get("enable", False):
        problems.append("AstrBot 的 aiocqhttp 平台当前 enable=false（协议端连上也不会处理消息）")

    host = args.host or str(platform.get("ws_reverse_host") or "")
    port = args.port or platform.get("ws_reverse_port") or DEFAULT_PORT
    token = platform.get("ws_reverse_token") or ""
    if not host:
        problems.append(f"ws_reverse_host 为空，已回退 {DEFAULT_HOST}")
    try:
        port = int(port)
    except (TypeError, ValueError):
        problems.append(f"ws_reverse_port 非法（{port!r}），已回退 {DEFAULT_PORT}")
        port = DEFAULT_PORT
    if not token:
        problems.append("ws_reverse_token 为空：LLBot 侧 token 也须留空，两侧必须一致")

    return build_connect_entry(host, port, token), problems


# ------------------------------------------------------------------- checking

def check_llbot_config(llbot_config: dict, expected: dict) -> list[str]:
    """校验 LLBot 配置里的 ws-reverse 条目是否与 AstrBot 侧一致。"""
    problems: list[str] = []
    ob11 = llbot_config.get("ob11") if isinstance(llbot_config, dict) else None
    if not isinstance(ob11, dict):
        return ["LLBot 配置中没有 ob11 段"]
    if not ob11.get("enable", False):
        problems.append("ob11.enable=false：OneBot11 总开关未开，AstrBot 收不到任何事件")

    connects = ob11.get("connect")
    if not isinstance(connects, list):
        return problems + ["ob11.connect 缺失或不是数组"]

    found = [c for c in connects if isinstance(c, dict) and c.get("type") == WS_REVERSE_TYPE]
    if not found:
        return problems + [f"ob11.connect 中没有 type={WS_REVERSE_TYPE} 的条目"]

    exp_url = expected["url"]
    exp_token = expected.get("token") or ""
    for entry in found:
        if not entry.get("enable", False):
            problems.append(f"ws-reverse 条目 enable=false（url={entry.get('url')!r}）")
        if entry.get("url") != exp_url:
            problems.append(
                f"url 不一致：LLBot={entry.get('url')!r} AstrBot 期望={exp_url!r}"
            )
        if (entry.get("token") or "") != exp_token:
            problems.append(
                f"token 不一致：LLBot={mask_secret(entry.get('token'))} "
                f"AstrBot={mask_secret(exp_token)}"
            )
        fmt = entry.get("messageFormat")
        if fmt != "array":
            problems.append(
                f"messageFormat={fmt!r}：必须为 'array'，否则 AstrBot 会拒绝整条消息并往群里发错误文本"
            )
    return problems


def merge_entry(llbot_config: dict, entry: dict) -> tuple[dict, bool]:
    """就地 upsert ws-reverse 条目；返回 (config, changed)。保留其它 connect 条目。"""
    ob11 = llbot_config.get("ob11")
    if not isinstance(ob11, dict):
        ob11 = {}
        llbot_config["ob11"] = ob11
    ob11["enable"] = True
    connects = ob11.get("connect")
    if not isinstance(connects, list):
        connects = []
        ob11["connect"] = connects

    for i, existing in enumerate(connects):
        if isinstance(existing, dict) and existing.get("type") == WS_REVERSE_TYPE:
            if existing == entry:
                return llbot_config, False
            connects[i] = entry
            return llbot_config, True
    connects.append(entry)
    return llbot_config, True


def describe(entry: dict) -> str:
    """人读摘要（token 掩码）。"""
    return (
        f"url           : {entry['url']}\n"
        f"type          : {entry['type']}\n"
        f"enable        : {entry['enable']}\n"
        f"messageFormat : {entry['messageFormat']}  (必须 array)\n"
        f"heartInterval : {entry['heartInterval']}\n"
        f"token         : {mask_secret(entry['token'])}"
    )


# ----------------------------------------------------------------------- main

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--astrbot-config", required=True,
                        help="AstrBot cmd_config.json 路径")
    parser.add_argument("--host", default="", help="覆盖 LLBot 要连的 host（默认取 AstrBot 监听地址）")
    parser.add_argument("--port", type=int, default=0, help="覆盖端口（默认取 ws_reverse_port）")
    parser.add_argument("--out", default="", help="把连接条目写成 JSON 片段（0600，含真 token）")
    parser.add_argument("--merge-into", default="", help="就地合并进 LLBot config.json（备份 + 原子写）")
    parser.add_argument("--check", default="", help="校验已有 LLBot config.json 与 AstrBot 是否一致")
    parser.add_argument("--json", action="store_true", help="机器可读输出（token 仍掩码）")
    args = parser.parse_args(argv)

    # ---- 读 AstrBot 配置
    try:
        astrbot_config = load_json(Path(args.astrbot_config))
    except Exception as e:
        print(f"error: 无法读取 AstrBot 配置 {args.astrbot_config}: {e}", file=sys.stderr)
        return 2

    entry, problems = derive_entry(astrbot_config, args)
    if entry is None:
        for p in problems:
            print(f"! {p}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "entry": {**entry, "token": mask_secret(entry["token"])},
            "problems": problems,
        }, ensure_ascii=False, indent=2))
    else:
        print("=== 推导结果（来自 AstrBot aiocqhttp 平台）===")
        print(describe(entry))
        for p in problems:
            print(f"! {p}")

    # ---- check 模式
    if args.check:
        try:
            llbot_config = load_json(Path(args.check))
        except Exception as e:
            print(f"error: 无法读取 LLBot 配置 {args.check}: {e}", file=sys.stderr)
            return 2
        issues = check_llbot_config(llbot_config, entry)
        if args.json:
            print(json.dumps({"check": args.check, "issues": issues}, ensure_ascii=False, indent=2))
        else:
            print(f"\n=== 校验 {args.check} ===")
            for i in issues:
                print(f"! {i}")
            print("status: " + ("in-sync" if not issues else f"{len(issues)} issue(s)"))
        return 0 if not issues else 1

    # ---- merge 模式
    if args.merge_into:
        path = Path(args.merge_into)
        if not path.exists():
            print(f"error: 目标不存在 {path}（LLBot 首次启动后才会生成 data/config.json）",
                  file=sys.stderr)
            return 2
        try:
            llbot_config = load_json(path)
        except Exception as e:
            print(f"error: 无法读取 {path}: {e}", file=sys.stderr)
            return 2
        if not isinstance(llbot_config, dict):
            print(f"error: {path} 顶层不是对象", file=sys.stderr)
            return 2
        merged, changed = merge_entry(llbot_config, entry)
        if not changed:
            print(f"status: in-sync（{path} 已是目标配置，未改动）")
            return 0
        bak = backup(path)
        write_json_private(path, merged)
        print(f"backup : {bak}")
        print(f"written: {path}")
        print(f"status : merged（{entry['url']}，messageFormat=array）")
        return 0

    # ---- 片段导出
    if args.out:
        out = Path(args.out)
        if out.exists():
            print(f"error: {out} 已存在（不覆盖人工文件；请先移走或改用 --merge-into）",
                  file=sys.stderr)
            return 2
        write_json_private(out, entry)
        print(f"\nwritten: {out}  (chmod 600，含真实 token，勿提交/外传)")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
