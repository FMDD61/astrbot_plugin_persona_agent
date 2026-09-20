#!/usr/bin/env bash
# screen 桩：只记录调用 + 在 -dmS 时伪造一条启动日志（供部署脚本自测台用）
# 注意：$STUB_DIR 由自测台 export，不在这里展开（heredoc 会吃转义）。
echo "screen $*" >> "$STUB_DIR/screen.log"
case "${1:-}" in
  -ls)
    [ -f "$STUB_DIR/session" ] && echo "There is a screen on: 1.astrbot (Detached)"
    exit 0 ;;
  -S)
    case " $* " in *" quit "*) rm -f "$STUB_DIR/session" ;; esac
    exit 0 ;;
  -dmS)
    touch "$STUB_DIR/session"
    {
      echo "[persona] mode=v2 段=8/8 缺=无 按设计省略=['s3_memory'] 自定义段=无 人格 1469 字符"
      echo "[examples] 示例块来源=bundled 条数=20"
      echo "[persona_agent] memory digest 已组装：{'recent_days': 1} 共 1 行"
      echo "[persona_agent] daily summary cron registered"
    } >> "$STUB_DIR/runtime.log"
    exit 0 ;;
esac
exit 0
