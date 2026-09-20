#!/usr/bin/env bash
# deploy-persona-plugin 的**离线自测台**：用真 git + 桩 screen/python3/crontab
# 跑 7 个场景，验证「不该动的时候绝不动、该回滚的时候真回滚」。
#
#   bash selftest.sh [待测脚本路径]      # 默认测同目录的 deploy_persona_plugin.sh
#
# 为什么要它：这个脚本会在**凌晨无人值守**时决定"要不要重启线上 bot"，
# 判错一次就是一次事故 —— 所以每条分支都要有假环境里的实证，不靠读代码。
set -uo pipefail

SUT="${1:-$(dirname "$0")/deploy_persona_plugin.sh}"
[ -f "$SUT" ] || { echo "找不到待测脚本：$SUT" >&2; exit 2; }
SUT="$(cd "$(dirname "$SUT")" && pwd)/$(basename "$SUT")"

WORK="$(mktemp -d /tmp/dpp-selftest-XXXXXX)"
export WORK          # 桩脚本要读它（否则桩写到 / 根目录，静默失败）
export STUB_DIR="$WORK"
trap 'rm -rf "$WORK"' EXIT
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✔ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ✘ $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1（$3）"; else bad "$1：期望 $3，实际 $2"; fi; }

# ---------------------------------------------------------------- 桩
mkdir -p "$WORK/bin"
STUB="$(cd "$(dirname "$0")" && pwd)/screen_stub.sh"
[ -f "$STUB" ] || { echo "缺少桩文件：$STUB" >&2; exit 2; }
cp "$STUB" "$WORK/bin/screen"
cat > "$WORK/bin/pyt" <<'EOF'
#!/usr/bin/env bash
if [ -f "$WORK/tests_fail" ]; then echo "Ran 1 test"; echo "FAILED"; exit 1; fi
echo "Ran 700 tests"; echo "OK"; exit 0
EOF
cat > "$WORK/bin/crontab" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-l" ]; then cat "$WORK/crontab.txt" 2>/dev/null || true; exit 0; fi
cat > "$WORK/crontab.txt"; exit 0
EOF
chmod +x "$WORK/bin/"*

# ---------------------------------------------------------------- 真 git：裸远端 + 克隆
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
git init -q --bare "$WORK/remote.git"
git init -q "$WORK/src" && cd "$WORK/src"
git remote add origin "$WORK/remote.git" >/dev/null
mkdir -p tests && echo v1 > version.txt && echo x > tests/test_x.py
git add -A && git commit -qm 'v1' && git branch -M main && git push -q origin main
git -C "$WORK/remote.git" symbolic-ref HEAD refs/heads/main   # 夹具：裸仓库 HEAD 指对分支
cd "$WORK" && git clone -q "$WORK/remote.git" plugin

# 远端前进一个提交（模拟"有新提交"）
cd "$WORK/src" && echo v2 > version.txt && git commit -qam 'v2' && git push -q origin main

run() {  # run <场景名> <期望退出码> [额外参数…]
  local name="$1" want="$2"; shift 2
  rm -f "$WORK/screen.log"
  HOME="$WORK" PLUGIN_DIR="$WORK/plugin" ASTROBOT_LOG="$WORK/runtime.log" \
  ASTROBOT_START="$WORK/start.sh" SCREEN_NAME=astrbot \
  SCREEN_BIN="$WORK/bin/screen" PYTHON="$WORK/bin/pyt" CRONTAB_BIN="$WORK/bin/crontab" \
  STATE_FILE="$WORK/status" BRANCH=main \
    bash "$SUT" "$@" >"$WORK/out.txt" 2>&1
  local rc=$?
  check "$name 退出码" "$rc" "$want"
  LAST_OUT="$(cat "$WORK/out.txt")"
}
restarted() { grep -q -- '-dmS astrbot' "$WORK/screen.log" 2>/dev/null && echo yes || echo no; }
head_of() { git -C "$WORK/plugin" rev-parse --short HEAD; }

echo '── 1) 工作树脏 → 拒绝（不重启、不改动）'
echo dirty >> "$WORK/plugin/version.txt"
touch "$WORK/start.sh"; run 'dirty' 1
check 'dirty 未重启' "$(restarted)" no
check 'dirty 有明确原因' "$(grep -c '工作树不干净' "$WORK/out.txt")" 1
git -C "$WORK/plugin" checkout -q -- version.txt

echo '── 2) --check-only：只看不动'
BEFORE="$(head_of)"; run 'check-only' 0 --check-only
check 'check-only HEAD 不变' "$(head_of)" "$BEFORE"
check 'check-only 未重启' "$(restarted)" no
check 'check-only 列出了新提交' "$(grep -c 'v2' "$WORK/out.txt")" 1

echo '── 3) 正常部署：拉取 → 测试 → 重启'
run 'deploy' 0
check 'deploy 已重启' "$(restarted)" yes
check 'deploy HEAD 已前进' "$(grep -c 'v2' "$WORK/plugin/version.txt")" 1
n=$(grep -c '✔.*persona' "$WORK/out.txt" || true); if [ "$n" -ge 1 ]; then ok "deploy 读到启动日志（$n 条）"; else bad 'deploy 未读到启动日志'; fi

echo '── 4) 已是最新 + 无 --force-restart → 不重启'
run 'uptodate' 0
check 'uptodate 未重启' "$(restarted)" no
check 'uptodate 明说不重启' "$(grep -c '不做重启' "$WORK/out.txt")" 1

echo '── 5) 已是最新 + --force-restart → 重启（用于激活盘上代码）'
run 'force' 0 --force-restart
check 'force 已重启' "$(restarted)" yes

echo '── 6) 测试失败 → 回滚 + 不重启'
cd "$WORK/src" && echo v3 > version.txt && git commit -qam 'v3' && git push -q origin main
HEAD_BEFORE="$(head_of)"; touch "$WORK/tests_fail"
run 'tests-fail' 1
check '测试失败未重启' "$(restarted)" no
check '测试失败已回滚' "$(head_of)" "$HEAD_BEFORE"
check '测试失败有回滚说明' "$(grep -c '已回滚' "$WORK/out.txt")" 1
rm -f "$WORK/tests_fail"

echo '── 7) 非快进（远端被改写）→ 中止，不重启'
cd "$WORK/src" && git commit -q --amend -m 'v3-rewritten' && git push -qf origin main
cd "$WORK/plugin" && echo local > local_only.txt && git add -A && git commit -qm 'local diverge'
run 'diverge' 1
check 'diverge 未重启' "$(restarted)" no
cd "$WORK/plugin" && git reset -q --hard origin/main

echo '── 8) cron 安装 / 摘除（幂等）'
run 'cron-install' 0 --install-cron '50 1 21 9 *' --one-shot
check 'cron 装了 1 行' "$(grep -c 'deploy-persona-plugin' "$WORK/crontab.txt")" 1
run 'cron-install-again' 0 --install-cron '50 1 21 9 *'
check 'cron 幂等（仍 1 行）' "$(grep -c 'deploy-persona-plugin' "$WORK/crontab.txt")" 1
run 'cron-remove' 0 --remove-cron
check 'cron 已摘除' "$(grep -c 'deploy-persona-plugin' "$WORK/crontab.txt")" 0

echo '── 9) --one-shot 成功后自动摘除定时任务'
cd "$WORK/src" && echo v4 > version.txt && git commit -qam 'v4' && git push -q origin main
run 'cron-install2' 0 --install-cron '50 1 21 9 *' --one-shot
run 'oneshot-run' 0 --one-shot
check 'one-shot 已重启' "$(restarted)" yes
check 'one-shot 摘掉了自身定时任务' "$(grep -c 'deploy-persona-plugin' "$WORK/crontab.txt")" 0


echo '── 10) fetch 失败：带 --force-restart 时仍激活盘上代码'
git -C "$WORK/plugin" remote set-url origin "$WORK/nonexistent.git"
run 'fetch-fail-force' 0 --force-restart
check 'fetch 失败但 --force-restart → 仍重启' "$(restarted)" yes
check 'fetch 失败有明确告警' "$(grep -c 'fetch 失败' "$WORK/out.txt")" 1

echo '── 11) fetch 失败：不带 --force-restart → 中止、不重启'
run 'fetch-fail-plain' 1
check 'fetch 失败且无 force → 不重启' "$(restarted)" no
git -C "$WORK/plugin" remote set-url origin "$WORK/remote.git"
echo
echo "==== 自测结果：$PASS 通过 / $FAIL 失败 ===="
[ "$FAIL" = 0 ] || exit 1
