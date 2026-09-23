# merge.json 字段陷阱

> 自根 `AGENTS.md` 下沉（2026-09-16）。**高成本重发现，改动任何解析代码前必读。**

## `merge.json` gotchas (high-cost to rediscover)
- **Encoding is UTF-8.** Always read with `encoding="utf-8"`. Do NOT pass `encoding="gbk"`.
- **PowerShell stdout trap.** Write-Output/Get-Content re-encodes through CP936. Debug via write to UTF-8 file.
- **Reply messages are textual.** No `replyElement` / `sourceMsgId`. Quote-replies show as `@<昵称> <正文>`. `mentions[*].uid` is literally `"unknown"`.
- **Square-bracket prefixes `[...]`** mark resource placeholders, not replies. Filter by `messageType`.
- **Do not trust `chatInfo.type`** — it is `"private"` in this export. Filter on `receiver.type=="group"` + uid.
- Root JSON: `{ metadata, chatInfo, statistics, senders[], messages[] }`. Use `ijson` path `messages.item`. Never `json.load` whole file.
- Sender identity: `sender.uin` is a numeric string (e.g. `"123456789"`). Comparing to int silently fails.
- `senders[]` contains bookkeeping entries (e.g. `uid:"<group_id>"` is NOT a real user). Don't iterate `senders[]` to find people.
- Text in `content.text`; canonical element is `rawMessage.elements[*].textElement.content`. `messageType==2` is plain text.
- Empty/truncated `content.text` occurs. Treat as skippable.
