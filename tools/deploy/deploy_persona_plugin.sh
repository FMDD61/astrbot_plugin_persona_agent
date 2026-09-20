#!/usr/bin/env bash
# deploy-persona-plugin —— 台式机上 persona 插件的**无人值守**部署。
#
#   拉取(ff-only) → 跑测试 → 【只重启 AstrBot】→ 核对启动日志
#
# 铁律（AGENTS.md 底线 + 用户 2026-09-20 口径）：
#   * **只重启 AstrBot**（screen 会话 astrbot）。**绝不碰 docker / QQ 容器**。
#   * 测试不过 → **回滚到原 HEAD 并中止**，运行中的进程一个字节都不动。
#   * 全程写日志；失败信息一定是可读的中文行，别让人猜。
#
# 用法：
#   deploy-persona-plugin --check-only              # 只看有多少新提交，不动作
#   deploy-persona-plugin                            # 拉取 → 测试 → 重启
#   deploy-persona-plugin --force-restart            # 已是最新也重启（盘上代码已新、只差激活）
#   deploy-persona-plugin --install-cron '50 1 21 9 *' --force-restart --one-shot
#                                                    # 排一次定时任务（跑完自动摘掉）
#   deploy-persona-plugin --remove-cron
#
# 可覆盖的环境变量（自测台用它们注入桩）：
#   PLUGIN_DIR ASTROBOT_LOG ASTROBOT_START SCREEN_NAME BRANCH PYTHON STATE_FILE
#   GIT_BIN SCREEN_BIN TIMEOUT_BIN CRONTAB_BIN
set -uo pipefail

PLUGIN_DIR="${PLUGIN_DIR:-/opt/AstrBot/data/plugins/astrbot_plugin_persona_agent}"
ASTROBOT_LOG="${ASTROBOT_LOG:-/opt/AstrBot/data/runtime.log}"
ASTROBOT_START="${ASTROBOT_START:-/home/fmdd61/llbot/pilot/start-astrbot.sh}"
SCREEN_NAME="${SCREEN_NAME:-astrbot}"
BRANCH="${BRANCH:-main}"
PYTHON="${PYTHON:-python3}"
STATE_FILE="${STATE_FILE:-$HOME/.deploy-persona-plugin.status}"
GIT_BIN="${GIT_BIN:-git}"
SCREEN_BIN="${SCREEN_BIN:-screen}"
TIMEOUT_BIN="${TIMEOUT_BIN:-timeout}"
CRONTAB_BIN="${CRONTAB_BIN:-crontab}"

CRON_MARK="# deploy-persona-plugin"
FETCH_TIMEOUT=120
TEST_TIMEOUT=600
STOP_WAIT=60
BOOT_WAIT=120

MODE=deploy
FORCE_RESTART=0
ALLOW_DIRTY=0
SKIP_TESTS=0
ONE_SHOT=0
CRON_SPEC=""

LOG_FILE=""

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log()  { printf '%s %s\n' "$(ts)" "$*" | tee -a "$LOG_FILE"; }
warn() { printf '%s ⚠️  %s\n' "$(ts)" "$*" | tee -a "$LOG_FILE" >&2; }
die()  { printf '%s ❌ %s\n' "$(ts)" "$*" | tee -a "$LOG_FILE" >&2;
         printf 'FAILED %s %s\n' "$(ts)" "$*" > "$STATE_FILE"; exit 1; }
ok()   { printf 'OK %s %s\n' "$(ts)" "$*" > "$STATE_FILE"; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------- 参数
while [ $# -gt 0 ]; do
  case "$1" in
    --check-only)    MODE=check ;;
    --force-restart) FORCE_RESTART=1 ;;
    --allow-dirty)   ALLOW_DIRTY=1 ;;
    --skip-tests)    SKIP_TESTS=1 ;;
    --one-shot)      ONE_SHOT=1 ;;
    --no-restart)    MODE=no-restart ;;
    --install-cron)  MODE=install-cron; CRON_SPEC="${2:-}"; shift ;;
    --remove-cron)   MODE=remove-cron ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------- cron 管理
cron_lines() { "$CRONTAB_BIN" -l 2>/dev/null || true; }

remove_cron() {
  local kept
  kept="$(cron_lines | grep -v -- "$CRON_MARK" || true)"
  printf '%s\n' "$kept" | grep -v '^$' | "$CRONTAB_BIN" - 2>/dev/null || true
}

install_cron() {
  [ -n "$CRON_SPEC" ] || { echo "--install-cron 需要 cron 时间表达式，例：'50 1 21 9 *'" >&2; exit 2; }
  remove_cron
  local line="$CRON_SPEC $0 --force-restart"
  [ "$ONE_SHOT" = 1 ] && line="$line --one-shot"
  line="$line >> $HOME/deploy-persona-plugin.log 2>&1 $CRON_MARK"
  { cron_lines | grep -v '^$'; printf '%s\n' "$line"; } | "$CRONTAB_BIN" -
  echo "已安装定时任务："
  cron_lines | grep -- "$CRON_MARK"
  echo
  echo "（--one-shot：成功跑完会自己摘掉这一行；失败会保留，便于次日排查）"
}

if [ "$MODE" = install-cron ]; then install_cron; exit 0; fi
if [ "$MODE" = remove-cron ]; then remove_cron; echo "已移除定时任务"; exit 0; fi

LOG_FILE="$HOME/deploy-persona-plugin.log"
log "===== deploy-persona-plugin 开始（mode=$MODE）====="

# ---------------------------------------------------------------- 前置检查
command -v "$GIT_BIN" >/dev/null || die "找不到 $GIT_BIN"
[ -d "$PLUGIN_DIR/.git" ] || die "插件目录不是 git 仓库：$PLUGIN_DIR"
cd "$PLUGIN_DIR" || die "进不去 $PLUGIN_DIR"

if [ "$ALLOW_DIRTY" != 1 ]; then
  if [ -n "$("$GIT_BIN" status --porcelain)" ]; then
    "$GIT_BIN" status --short | tee -a "$LOG_FILE" >&2
    die "工作树不干净 → 拒绝部署（人工先处理；或 --allow-dirty 强制）"
  fi
fi

OLD_HEAD="$("$GIT_BIN" rev-parse --verify HEAD 2>/dev/null || true)"
case "$OLD_HEAD" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) die "当前 HEAD 无效（仓库没有提交？）—— 拒绝部署" ;;
esac
log "当前 HEAD：$OLD_HEAD"

if [ "$MODE" != no-restart ]; then
  command -v "$SCREEN_BIN" >/dev/null || die "找不到 $SCREEN_BIN"
  [ -f "$ASTROBOT_START" ] || die "AstrBot 启动脚本不存在：$ASTROBOT_START"
  "$SCREEN_BIN" -ls 2>/dev/null | grep -q "\.$SCREEN_NAME" \
    || warn "screen 会话 $SCREEN_NAME 当前不存在（重启时会新建）"
fi

# ---------------------------------------------------------------- 拉取
log "fetch origin/$BRANCH …"
if ! GIT_TERMINAL_PROMPT=0 "$TIMEOUT_BIN" "$FETCH_TIMEOUT" \
     "$GIT_BIN" fetch --prune origin "$BRANCH" >>"$LOG_FILE" 2>&1; then
  die "git fetch 失败（网络/凭据？）—— 未做任何改动"
fi
NEW_HEAD="$("$GIT_BIN" rev-parse "origin/$BRANCH")"

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
  log "已是最新（$OLD_HEAD）"
  if [ "$FORCE_RESTART" != 1 ]; then
    log "无新提交且未指定 --force-restart → 不做重启"
    ok "up-to-date，无动作"
    exit 0
  fi
  log "无新提交，但指定了 --force-restart → 继续（用于激活盘上已有代码）"
else
  log "新增提交（$OLD_HEAD..$NEW_HEAD）："
  "$GIT_BIN" log --oneline --no-decorate "$OLD_HEAD..$NEW_HEAD" | tee -a "$LOG_FILE"
fi

if [ "$MODE" = check ]; then
  log "--check-only：到此为止，未改动任何东西"
  ok "check-only 完成"
  exit 0
fi

# ---------------------------------------------------------------- 快进合并
if [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
  if ! "$GIT_BIN" merge --ff-only "origin/$BRANCH" >>"$LOG_FILE" 2>&1; then
    "$GIT_BIN" merge --abort >>"$LOG_FILE" 2>&1 || true
    die "不是快进合并（本地有分叉提交？）→ 已中止，未重启"
  fi
  log "已快进到 $("$GIT_BIN" rev-parse --short HEAD)"
fi

# ---------------------------------------------------------------- 测试闸门
if [ "$SKIP_TESTS" != 1 ]; then
  log "跑测试（$TEST_TIMEOUT s 上限）…"
  if ! "$TIMEOUT_BIN" "$TEST_TIMEOUT" "$PYTHON" -m unittest discover -s tests -t . \
       >>"$LOG_FILE" 2>&1; then
    warn "测试未通过 → 回滚到 $OLD_HEAD（运行中的 AstrBot **不动**）"
    "$GIT_BIN" reset --hard "$OLD_HEAD" >>"$LOG_FILE" 2>&1 || warn "回滚失败，需人工处理！"
    die "测试失败，已回滚；未重启、未影响运行中的进程"
  fi
  tail -3 "$LOG_FILE" | sed 's/^/    /'
  log "测试通过"
fi

# ---------------------------------------------------------------- 重启 AstrBot（唯一副作用）
if [ "$MODE" = no-restart ]; then
  log "--no-restart：代码已更新，未重启"
  ok "已更新代码（未重启）"
  exit 0
fi

# 核对启动只读日志**末尾**（不用字节偏移 —— 那玩意儿在文件不存在/被轮转时很脆）
[ -f "$ASTROBOT_LOG" ] || warn "日志文件还不存在：$ASTROBOT_LOG（首次启动？）"
log "停 AstrBot（screen -S $SCREEN_NAME -X quit）…"
"$SCREEN_BIN" -S "$SCREEN_NAME" -X quit >>"$LOG_FILE" 2>&1 || warn "quit 返回非零（可能本来就没在跑）"

i=0
while [ $i -lt "$STOP_WAIT" ]; do
  "$SCREEN_BIN" -ls 2>/dev/null | grep -q "\.$SCREEN_NAME" || break
  sleep 1; i=$((i + 1))
done
if "$SCREEN_BIN" -ls 2>/dev/null | grep -q "\.$SCREEN_NAME"; then
  die "等了 ${STOP_WAIT}s 会话仍未退出 → 中止（**没有**强杀，需人工看一眼）"
fi
log "已停（${i}s）"

log "启动 AstrBot…"
if ! "$SCREEN_BIN" -dmS "$SCREEN_NAME" bash "$ASTROBOT_START" >>"$LOG_FILE" 2>&1; then
  die "screen 启动失败 —— **服务当前是停的**，请立刻人工拉起：screen -dmS $SCREEN_NAME bash $ASTROBOT_START"
fi

# ---------------------------------------------------------------- 启动核对
log "等启动（最多 ${BOOT_WAIT}s），核对关键日志行…"
i=0
BOOT_OK=0
while [ $i -lt "$BOOT_WAIT" ]; do
  if [ -f "$ASTROBOT_LOG" ] && tail -n 400 "$ASTROBOT_LOG" 2>/dev/null | grep -q '\[persona\] mode='; then
    BOOT_OK=1; break
  fi
  sleep 2; i=$((i + 2))
done
NEW_LOG="$([ -f "$ASTROBOT_LOG" ] && tail -n 400 "$ASTROBOT_LOG" 2>/dev/null || true)"
for pat in '\[persona\] mode=' '\[examples\] 示例块来源' '已组装' 'cron'; do
  line="$(printf '%s' "$NEW_LOG" | grep -m1 -- "$pat" || true)"
  [ -n "$line" ] && log "    ✔ $line" || log "    · 未见「$pat」（不一定异常，见部署文档的预期表）"
done

if [ "$BOOT_OK" != 1 ]; then
  warn "启动后 ${BOOT_WAIT}s 内没看到『[persona] mode=』—— 可能还在加载（BGE/Chroma 预热较慢），请人工 `tail -f $ASTROBOT_LOG` 确认"
fi

log "完成：$OLD_HEAD → $("$GIT_BIN" rev-parse HEAD)"
ok "部署完成（已重启 AstrBot）"

[ "$ONE_SHOT" = 1 ] && { remove_cron; log "--one-shot：已摘掉本次定时任务"; }
exit 0
