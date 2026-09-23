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
# 拉取策略（2026-09-23 起）：**多源顺序降级**
#   原因：台式机直连 github 间歇性报 `GnuTLS recv error (-110)`（TLS 被中断），
#   同一时段镜像却稳。所以 origin 失败后依次试镜像，而不是一次失败就放弃。
#   * 顺序：origin（github 直连）→ ghfast.top → gh-proxy.com → ghproxy.net
#   * 每个源重试 2 次（TLS 抖动是间歇性的，重试比换源更划算）
#   * 镜像按 `git fetch <url> +refs/heads/main:refs/remotes/origin/main` 写入**同名 ref**，
#     因此后续 HEAD 比较与 `--ff-only` 快进语义与直连**完全一致**
#   * 每个源的成败 / 耗时 / 失败原因全部写进日志，换源时打印一行 ↳ —— 降级必须可见
#   ⚠️ 镜像只能代理**公开**仓库。本仓库 2026-09-23 实测为 public；若哪天改回 private，
#      镜像会如实报 404，只剩 origin 一条路（脚本不会假装成功）。
#
# 可覆盖的环境变量（自测台/运维用它们注入桩或改源）：
#   PLUGIN_DIR ASTROBOT_LOG ASTROBOT_START SCREEN_NAME BRANCH PYTHON STATE_FILE
#   GIT_BIN SCREEN_BIN TIMEOUT_BIN CRONTAB_BIN
#   FETCH_SOURCES（空格分隔的完整 URL，**接管**整个源列表，跳过镜像自动推导）
#   FETCH_ATTEMPTS（每源重试次数，默认 2） FETCH_TIMEOUT（每次上限秒数，默认 60）
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
#: 每个源**单次**拉取的上限（秒）。镜像实测 1~3s，60s 足够；卡死就快速失败换源。
FETCH_TIMEOUT="${FETCH_TIMEOUT:-60}"
#: 每个源的重试次数（GnuTLS -110 这类中断是间歇性的，重试常常一次就好）
FETCH_ATTEMPTS="${FETCH_ATTEMPTS:-2}"
#: 显式接管源列表（空格分隔的完整 URL）。留空 = origin + 按 origin 推导的镜像。
FETCH_SOURCES="${FETCH_SOURCES:-}"
#: 镜像模板（%s = owner/repo）。按顺序降级；实测只有这三个通（见 scratch/DEPLOY_mirror_report.md）
MIRROR_URLS=(
  "https://ghfast.top/https://github.com/%s.git"
  "https://gh-proxy.com/https://github.com/%s.git"
  "https://ghproxy.net/https://github.com/%s.git"
)
TEST_TIMEOUT=600
STOP_WAIT=60
BOOT_WAIT=120

MODE=deploy
FORCE_RESTART=0
ALLOW_DIRTY=0
SKIP_TESTS=0
ONE_SHOT=0
CRON_SPEC=""

#: 自己的绝对路径 —— cron 行里必须写绝对路径（相对路径在 cron 里必然找不到）
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
[ -x "$SELF" ] || SELF="$0"

LOG_FILE=""

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log()  { printf '%s %s\n' "$(ts)" "$*" | tee -a "$LOG_FILE"; }
warn() { printf '%s ⚠️  %s\n' "$(ts)" "$*" | tee -a "$LOG_FILE" >&2; }
die()  { printf '%s ❌ %s\n' "$(ts)" "$*" | tee -a "$LOG_FILE" >&2;
         printf 'FAILED %s %s\n' "$(ts)" "$*" > "$STATE_FILE"; exit 1; }
ok()   { printf 'OK %s %s\n' "$(ts)" "$*" > "$STATE_FILE"; }

usage() { awk 'NR>1 && /^set -/{exit} NR>1{sub(/^# ?/,""); print}' "$0"; }

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
  local line="$CRON_SPEC $SELF --force-restart"
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
  # ⚠️ 只看**已跟踪文件**的改动：未跟踪文件（运行时产物如 memory_store.db、
  # style_drift_report.json）不会被快进合并覆盖，也不该拦住部署 ——
  # 实测：用 `git status --porcelain` 会把它们一并算成"脏"，**部署直接被拒绝**。
  DIRTY="$("$GIT_BIN" status --porcelain --untracked-files=no)"
  if [ -n "$DIRTY" ]; then
    printf '%s\n' "$DIRTY" | tee -a "$LOG_FILE" >&2
    die "工作树有**已跟踪文件**的改动 → 拒绝部署（人工先处理；或 --allow-dirty 强制）"
  fi
  UNTRACKED="$("$GIT_BIN" ls-files --others --exclude-standard | head -5 | tr '\n' ' ')"
  [ -n "$UNTRACKED" ] && log "（未跟踪文件，不影响部署）：$UNTRACKED"
  true
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

# ---------------------------------------------------------------- 拉取（多源顺序降级）
# 口径：origin 直连优先；失败则依次试镜像。每个源的成败 / 耗时 / 失败原因都进日志。
# 镜像用 git fetch <url> +refs/heads/$BRANCH:refs/remotes/origin/$BRANCH 写入**同名 ref**，
# 所以下面的 HEAD 比较与 --ff-only 快进语义跟直连一模一样，不需要任何分支特判。
_now_us() {   # 微秒时间戳（EPOCHREALTIME 可能带小数点，只留数字）
  if [ -n "${EPOCHREALTIME:-}" ]; then printf '%s' "${EPOCHREALTIME//[!0-9]/}"; return; fi
  printf '%s000000' "$(date +%s)"
}
_elapsed_ms() { echo $(( ($(_now_us) - $1) / 1000 )); }
_fmt_ms()     { printf '%d.%03ds' "$(( $1 / 1000 ))" "$(( $1 % 1000 ))"; }

case "$FETCH_ATTEMPTS" in ''|*[!0-9]*) FETCH_ATTEMPTS=2 ;; esac
[ "$FETCH_ATTEMPTS" -ge 1 ] || FETCH_ATTEMPTS=1

ORIGIN_URL="$("$GIT_BIN" remote get-url origin 2>/dev/null || true)"
SOURCES=()
if [ -n "$FETCH_SOURCES" ]; then
  # shellcheck disable=SC2206
  SOURCES=($FETCH_SOURCES)
  log "拉取源：FETCH_SOURCES 显式指定了 ${#SOURCES[@]} 个（跳过镜像自动推导）"
else
  [ -n "$ORIGIN_URL" ] || die "仓库没有 origin remote，也没给 FETCH_SOURCES —— 无处可拉"
  SOURCES=("$ORIGIN_URL")
  case "$ORIGIN_URL" in
    https://github.com/*)
      SLUG="${ORIGIN_URL#https://github.com/}"; SLUG="${SLUG%.git}"
      for _tpl in "${MIRROR_URLS[@]}"; do SOURCES+=("$(printf "$_tpl" "$SLUG")"); done
      ;;
    *) log "origin 不是 github HTTPS 地址（$ORIGIN_URL）→ 只用 origin，不启用镜像" ;;
  esac
fi
[ "${#SOURCES[@]}" -ge 1 ] || die "拉取源列表为空 —— 拒绝继续"

log "拉取：共 ${#SOURCES[@]} 个源，每源最多试 ${FETCH_ATTEMPTS} 次、单次上限 ${FETCH_TIMEOUT}s"
FETCH_ROWS=()
NEW_HEAD=""; WIN_SRC=""; WIN_MS=0; WIN_ATTEMPT=0
si=0
for src in "${SOURCES[@]}"; do
  si=$((si + 1))
  if [ -n "$ORIGIN_URL" ] && [ "$src" = "$ORIGIN_URL" ]; then label="origin（$src）"; else label="$src"; fi
  ai=1; reason=""
  while [ "$ai" -le "$FETCH_ATTEMPTS" ]; do
    log "→ 源 $si/${#SOURCES[@]} 第 $ai/$FETCH_ATTEMPTS 次：$label"
    t0="$(_now_us)"
    if [ -n "$ORIGIN_URL" ] && [ "$src" = "$ORIGIN_URL" ]; then
      GIT_TERMINAL_PROMPT=0 "$TIMEOUT_BIN" "$FETCH_TIMEOUT" \
        "$GIT_BIN" fetch --prune origin "$BRANCH" >>"$LOG_FILE" 2>&1
    else
      GIT_TERMINAL_PROMPT=0 "$TIMEOUT_BIN" "$FETCH_TIMEOUT" \
        "$GIT_BIN" fetch --prune --no-tags "$src" \
          "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" >>"$LOG_FILE" 2>&1
    fi
    rc=$?
    ms="$(_elapsed_ms "$t0")"
    if [ "$rc" = 0 ]; then
      cand="$("$GIT_BIN" rev-parse --verify "refs/remotes/origin/$BRANCH" 2>/dev/null || true)"
      case "$cand" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
          NEW_HEAD="$cand"; WIN_SRC="$src"; WIN_MS="$ms"; WIN_ATTEMPT="$ai"
          log "  ✔ 源 $si/${#SOURCES[@]} 成功（$(_fmt_ms "$ms")，第 $ai 次）：$label → origin/$BRANCH=${cand:0:8}"
          FETCH_ROWS+=("✔ 源 $si/${#SOURCES[@]}  $label  →  ${cand:0:8}  $(_fmt_ms "$ms")  第 $ai 次成功")
          break 2
          ;;
      esac
      reason="命令返回 0，但 origin/$BRANCH 还不是合法 SHA"
    elif [ "$rc" = 124 ]; then
      reason="超时：${FETCH_TIMEOUT}s 内没完成（TLS 卡死的典型表现）"
    else
      reason="$(tail -n 40 "$LOG_FILE" 2>/dev/null | grep -a -v '^[[:space:]]*$' | tail -1 | tr -d '\000' | cut -c1-140)"
      [ -n "$reason" ] || reason="退出码 $rc，且没有输出"
    fi
    warn "  ✘ 源 $si/${#SOURCES[@]} 第 $ai/$FETCH_ATTEMPTS 次失败（$(_fmt_ms "$ms")）：$reason"
    ai=$((ai + 1))
    [ "$ai" -le "$FETCH_ATTEMPTS" ] && sleep 3
  done
  FETCH_ROWS+=("✘ 源 $si/${#SOURCES[@]}  $label  →  失败 ×$FETCH_ATTEMPTS（$reason）")
  [ "$si" -lt "${#SOURCES[@]}" ] && log "  ↳ 降级：换下一个源（$((si + 1))/${#SOURCES[@]}）"
  true
done

# 汇总：无论成败都打印 —— 降级必须可见
log "拉取小结（共 ${#SOURCES[@]} 个源）："
for row in "${FETCH_ROWS[@]}"; do log "    $row"; done

if [ -z "$NEW_HEAD" ]; then
  # 网络/凭据失败**不该让「激活盘上代码」一起废掉**：定时任务里带了 --force-restart 时，
  # 照旧跑测试并重启（用当前 HEAD）。否则凌晨那一跑会静默地什么都没做。
  if [ "$FORCE_RESTART" = 1 ]; then
    warn "fetch 失败：${#SOURCES[@]} 个源全部不可用（网络/凭据？）→ 因指定了 --force-restart，改用**盘上现有代码**继续"
    NEW_HEAD="$OLD_HEAD"
  else
    die "fetch 失败：${#SOURCES[@]} 个源全部不可用（网络/凭据？）—— 未做任何改动"
  fi
elif [ -n "$ORIGIN_URL" ] && [ "$WIN_SRC" != "$ORIGIN_URL" ]; then
  warn "本次是**降级**拉取：origin 直连没成功，改用镜像 $WIN_SRC（$(_fmt_ms "$WIN_MS")，第 $WIN_ATTEMPT 次）"
  warn "  refs 已按 origin/$BRANCH 写入，HEAD 比较与 --ff-only 快进语义与直连一致"
fi

# 陈旧镜像保护：拉到的提交若是本地 HEAD 的**祖先**，说明这个源比本地旧（缓存滞后）
# → 按「已是最新」处理，绝不把代码退回去。
if [ -n "$NEW_HEAD" ] && [ "$OLD_HEAD" != "$NEW_HEAD" ] \
   && "$GIT_BIN" merge-base --is-ancestor "$NEW_HEAD" "$OLD_HEAD" 2>/dev/null; then
  warn "拉到的 $NEW_HEAD 是本地 HEAD 的祖先（镜像缓存滞后？）→ 按『已是最新』处理，不回退"
  NEW_HEAD="$OLD_HEAD"
fi

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
# ⚠️ tr -d '\000'：runtime.log 含 NUL 字节，直接 $(...) 捕获会报
# 「警告：命令替换：忽略输入中的 null 字节」（2026-09-23 实测）。
NEW_LOG="$([ -f "$ASTROBOT_LOG" ] && tail -n 400 "$ASTROBOT_LOG" 2>/dev/null | tr -d '\000' || true)"
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
