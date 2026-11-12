# tokenmaster

在一個終端機畫面裡看 Codex、Claude、Antigravity、Grok、Cursor 和 OpenRouter 的每週配額與預付點數。

憑證留在你已經登入的那台電腦上。tokenmaster 從各產品的設定目錄讀取；在 macOS 上也會讀鑰匙圈。它不會把權杖寫進這個儲存庫。不要提交 `~/.config/tokenmaster`，也不要把還在使用的 API 金鑰貼進問題回報。

```bash
tokenmaster                         # 儀表板
tokenmaster --detail                # 模型池與 5 小時視窗
tokenmaster -p codex,claude         # 只看部分平台
tokenmaster --width 110             # 配合較窄的終端機
tokenmaster --watch                 # 原地重新整理
tokenmaster watch -i 10 -d
tokenmaster --json
```

`master token` 是同一條命令。

## 平台

| 識別名 | 它讀取什麼 |
|---|---|
| `codex` | `~/.codex/auth.json` 與 Codex 用量 API。本地工作階段 JSONL 是後援 |
| `claude` | `~/.claude/.credentials.json` 或 macOS 鑰匙圈，然後是 Anthropic 用量 API |
| `antigravity` | 本地語言伺服器 Connect-RPC；Linux 預設個人帳號也支援[原生鑰匙圈配額 API](antigravity-linux.md) |
| `grok` | `grok` CLI 的驗證檔、grok.com cookie，或 xAI API 金鑰 |
| `cursor` | `cursor-agent` 的驗證、macOS 鑰匙圈，或 `state.vscdb` |
| `openrouter` | `OPENROUTER_API_KEY`，或用 `tokenmaster auth` 存下來的金鑰 |

呼叫是並行的。期限（`TOKENMASTER_DEADLINE`，或設定裡的 `deadline_seconds`）讓一個慢的平台不會擋住其他平台。已經拿到的部分資料會保留。

## 帳號

同一個平台可以同時看多個訂閱。每個具名帳號在 `~/.config/tokenmaster/profiles/<provider>/<account>` 有自己的目錄。

```bash
tokenmaster login claude work
tokenmaster account snapshot codex personal
tokenmaster account add openrouter team --api-key "$OPENROUTER_API_KEY"
tokenmaster account list
tokenmaster exec claude work
tokenmaster exec claude:auto          # 餘裕最多的那個帳號
eval "$(tokenmaster env claude work)" # 只影響這個 shell
tokenmaster -p claude:default,claude:work
tokenmaster account remove claude work --purge
```

`tokenmaster pick claude` 印出帳號名稱。策略有 `smart`（預設）、`most-remaining` 和 `expiring-first`。

## 其他命令

```bash
tokenmaster auth                      # 憑證精靈
tokenmaster auth openrouter --api-key "$OPENROUTER_API_KEY"
tokenmaster quota openrouter --weekly-limit 30 --weekly-unit USD
tokenmaster quota grok --credit-limit 50 --credit-used 12.5 --credit-unit USD
tokenmaster quota openrouter --reset

tokenmaster doctor                    # 本地憑證與快取
tokenmaster config show
tokenmaster config path
tokenmaster config clear-cache

tokenmaster statusline                # 從快照讀一行，給提示字元用
tokenmaster statusline --format tmux
tokenmaster statusline --format claude-code

tokenmaster history --days 30 --by day
tokenmaster history --by provider
tokenmaster history --by model
tokenmaster history --by project

tokenmaster notify                    # 80%、95%、100% 時的桌面通知
tokenmaster notify --threshold 75,90,100 --dry-run
tokenmaster --watch --notify
```

重設那一欄包含消耗節奏（`surplus`、`on_track`、`fast`、`overburn`），以及視窗可能耗盡的估計。`statusline` 讀上一份快照，不會擋住提示字元；快照過舊時才在背景重新整理。`history` 讀本地的 SQLite 檔 `warehouse.db`，所以上游 CLI 之後清掉日誌，這本帳仍然在。

寫進這本帳的專案名稱會收成短標籤。家目錄前綴在寫入之前就會被拿掉。

## 畫面

儀表板有五欄：平台、方案、每週剩餘、重設、點數。顏色跟著剩餘比例：15% 或以下是紅色，40% 或以下是黃色，更高是綠色。`--detail` 會加上各模型池。版面會計算東亞寬字元，終端機較窄時可用 `--width`（或 `TOKENMASTER_MAX_WIDTH`）壓縮欄寬。
