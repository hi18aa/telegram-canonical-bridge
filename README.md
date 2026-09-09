# Hermes Telegram Canonical Bridge

> 將受控 Telegram 私訊直接送往 Hermes 既有的 canonical Bot Chat，讓主 agent 保留 Bot Mode 與 `message_agent` 能力。
>
> **English:** A native Hermes platform plugin that forwards allowlisted Telegram DMs to an existing canonical Bot Chat, preserving Bot Mode and `message_agent`.

## 目的 / Purpose

Hermes 的一般 Telegram 平台通常會建立獨立的 Telegram session。這個外掛改為將 Telegram 作為既有 canonical `Bot Chat` 的受控輸入／輸出表面，因此主 agent 仍可使用 `message_agent`、委派子 agent 與其他 Bot Mode 能力。

適合想從 Telegram 遠端操作一個 Hermes Controller、但不希望修改 Hermes core 的使用者。

> **English:** Use this bridge when Telegram should control one existing Hermes controller rather than create a separate messaging session.

## 特色 / Highlights

- Telegram 純文字私訊直接送往指定 profile 的 canonical `Bot Chat`。
- 只允許明確 allowlist 中的 Telegram 使用者；預設拒絕所有人。
- inbound、assistant history 與 outbound 都有 SQLite 耐久紀錄。
- 不覆寫 Hermes 內建工具，也不修改 Hermes core。
- 同一個 Controller 在 V1 僅綁定一個 Telegram 私訊，避免多人交錯操控同一段 canonical 對話。

## 為什麼不是內建 Telegram 平台？ / Why not the built-in adapter?

一般平台訊息會形成獨立的來源 session；而 `message_agent` 是 Hermes 對 canonical `Bot Chat` 的 Bot Mode 提供的能力。這個外掛刻意繞過一般平台的 `handle_message()`，只使用 Hermes backend 的公開 JSON-RPC 流程：找出 canonical session、resume 該 session、提交 prompt、補讀歷史。

```text
Telegram 私訊
    │  long polling
    ▼
SQLite inbound ──> canonical Bot Chat ──> Hermes message_agent / 子 agent
    ▲                                          │
    └──── SQLite outbox <── session.history ───┘
                         │
                         ▼
                    Telegram 回覆
```

## 安裝與啟用 / Install and enable

以下是 Windows PowerShell 的實測流程。外掛放在 Hermes user plugin 目錄，不需要修改 Hermes core，因此 Hermes 更新時不會覆寫外掛來源。

```powershell
git clone https://github.com/hi18aa/telegram-canonical-bridge.git telegram-canonical-bridge
Set-Location telegram-canonical-bridge

$hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
$source = (Resolve-Path .).Path
$target = Join-Path $hermesHome 'plugins\telegram-canonical-bridge'

if (Test-Path -LiteralPath $target) {
    throw "外掛目錄已存在：$target"
}

& robocopy $source $target /E /XD __pycache__ .git .venv .pytest_cache /XF *.pyc
if ($LASTEXITCODE -gt 7) {
    throw "複製外掛失敗，robocopy 結束碼：$LASTEXITCODE"
}

hermes plugins enable telegram-canonical-bridge --no-allow-tool-override
hermes plugins doctor --ci $target
hermes plugins compat $target
```

`--no-allow-tool-override` 是預期設定：本外掛不需要覆寫 Hermes 的任何內建工具。

> **English:** Clone the repository, copy it to the Hermes user plugin directory, enable it without built-in tool overrides, and validate the installed copy with `doctor` and `compat`.

不要把 `bridge.sqlite3`、`.env` 或 Telegram token 放在這個專案的版本控制中。

## 設定 / Configuration

### 1. 設定秘密 / Configure secrets

請將秘密交給 Hermes 的 `.env` 管理，不要把 token 放進 `config.yaml`、Git repository 或 `backend_url`。以下命令會寫入 `<HERMES_HOME>/.env`：

```powershell
# 產生可長期使用的本機 backend session token。
$bytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$backendToken = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

# 由 BotFather 取得真正的 token 後再替換這個範例字串。
hermes config set TELEGRAM_CANONICAL_BRIDGE_BOT_TOKEN '123456:replace-with-real-bot-token'

# 這兩個值必須完全相同。
hermes config set HERMES_DASHBOARD_SESSION_TOKEN $backendToken
hermes config set TELEGRAM_CANONICAL_BRIDGE_BACKEND_TOKEN $backendToken
```

### 2. 設定 bridge / Configure the bridge

將以下區塊合併至 `<HERMES_HOME>/config.yaml`，並替換範例中的數字與 profile 名稱：

```yaml
platforms:
  telegram_canonical_bridge:
    enabled: true
    extra:
      # 不要將 token 寫進 URL。
      backend_url: ws://127.0.0.1:9119/api/ws

      # 這個 profile 必須已有 canonical Bot Chat。
      controller_profile: default

      # 必填：只允許可信任的 Telegram User ID。
      allowed_user_ids:
        - '123456789'

      # 選填：再限制為單一 Telegram 私訊 chat。
      home_chat_id: '123456789'

      # 以下皆為選填，列出預設值。
      telegram_poll_timeout_seconds: 40
      history_poll_interval_seconds: 3
      rpc_timeout_seconds: 25
      retry_base_seconds: 2
      retry_max_seconds: 60
```

`allowed_user_ids` 不可為空。請使用可信任的 Telegram ID 查詢方式，或由既有 Telegram update／Hermes session metadata 查閱數字 User ID。

### 3. 同一 bot token 的衝突 / Do not share a polling token

同一 Telegram bot token 不能同時由 Hermes 原生 Telegram adapter 和本外掛 long-poll。最安全的方式是為 bridge 建立專用 bot。

若確實要重用既有 `TELEGRAM_BOT_TOKEN`，請先將原生 adapter 關閉：

```yaml
platforms:
  telegram:
    enabled: false
```

之後重新啟動 gateway。否則 Telegram update 可能被另一個 consumer 取走，造成遺失或不穩定行為。

> **English:** Store secrets in Hermes `.env`, configure an explicit user allowlist, and use one long-poll consumer per Telegram bot token. A dedicated bridge bot is recommended.

## 必要的後端模式 / Backend mode

使用獨立的 `hermes serve` 作為 bridge backend，而不是依賴 Desktop 每次啟動時產生的暫時 session token。目標 profile 必須已經有可用的 canonical `Bot Chat`；請先在 Hermes Desktop 或 CLI 開啟目標 profile 的 Bot Chat。

在兩個終端機分別執行：

```powershell
# 終端機 A：backend 僅綁定 loopback。
hermes serve --host 127.0.0.1 --port 9119 --skip-build
```

```powershell
# 終端機 B：已在背景執行的 gateway 用 restart；首次啟動可用 start。
hermes gateway restart
# 或：hermes gateway start
```

`HERMES_DASHBOARD_SESSION_TOKEN` 讓 backend 重啟後仍可使用同一個 bridge token。現行 Hermes loopback `/api/ws` 在 WebSocket handshake 會以 query credential 驗證；外掛只在記憶體中附加它，設定檔禁止寫入 `token=`。因此請只綁定 `127.0.0.1`；V1 不支援將這種 session token 直接用於遠端 backend。若要跨機器，請先用受保護的 tunnel 將 backend 保持為本機 loopback。

啟動後可使用以下命令確認狀態：

```powershell
hermes plugins list --plain --no-bundled
hermes gateway status
hermes status
```

> **English:** Run `hermes serve` on loopback in one terminal, then start or restart the gateway in another terminal. The target profile must already have a canonical Bot Chat.

## 使用與測試 / Use and test

- Telegram 僅接受 allowlist 中使用者的 private chat；群組、其他使用者與 bot 訊息均不會進入 Hermes。
- 每一個 `controller_profile` 在 V1 只允許綁定一個 Telegram 私訊，避免兩人交錯操控相同的 canonical conversation。
- `/help`、`/start`、`/status` 由橋接器本地處理；其餘純文字送到 canonical `Bot Chat`。
- 不支援媒體、檔案、語音與 Telegram 群組。這些輸入會得到一則可重試佇列送出的提示。
- Telegram 回覆依 4,000 UTF-16 單位切段；emoji 也不會超出 Telegram 的 4,096 單位限制。

### Telegram 快速測試 / Telegram smoke test

先在 Telegram 對 bot 傳送：

```text
/status
```

接著傳送以下低風險測試 prompt：

```text
請使用 message_agent 委派一位子 agent，請它只回覆「Bridge test OK」，再把結果轉告我。
```

預期結果是：該訊息會出現在目標 canonical `Bot Chat`、主 agent 可正常呼叫 `message_agent`，最終 assistant 回覆會回到同一個 Telegram 私訊。V1 以 `session.history` 補讀結果，預設會有約數秒延遲，不是 token streaming。

> **English:** Send `/status`, then ask the canonical agent to delegate a small task through `message_agent`. The final answer should return to the same DM after a short history-polling delay.

## 驗證

不需真實 token 的單元測試：

```powershell
python -m compileall -q .
python -m unittest discover -s tests -v
hermes plugins doctor --ci .
hermes plugins compat .
```

完整上線驗收與設計取捨請看 [docs/實作設計與驗收.md](docs/實作設計與驗收.md)。

## 設定參考 / Configuration reference

| 設定 | 必填 | 預設值 | 說明 |
| --- | --- | --- | --- |
| `TELEGRAM_CANONICAL_BRIDGE_BOT_TOKEN` | 是 | — | Telegram Bot API token，只能放在 Hermes `.env`。 |
| `TELEGRAM_CANONICAL_BRIDGE_BACKEND_TOKEN` | 是 | — | bridge 連線 Hermes `/api/ws` 的 backend token。 |
| `HERMES_DASHBOARD_SESSION_TOKEN` | 是 | — | 必須與 bridge backend token 相同。 |
| `backend_url` | 是 | — | `ws://` 或 `wss://` endpoint；不可包含 `token=`。 |
| `controller_profile` | 是 | `default` | 含 canonical `Bot Chat` 的 Hermes profile。 |
| `allowed_user_ids` | 是 | — | Telegram User ID allowlist，必須為數字清單。 |
| `home_chat_id` | 否 | — | 額外鎖定為一個 Telegram private chat。 |
| `state_path` | 否 | `<HERMES_HOME>/plugin-data/telegram-canonical-bridge/bridge.sqlite3` | SQLite 狀態檔位置。 |
| `telegram_poll_timeout_seconds` | 否 | `40` | Telegram long-poll timeout，範圍 1–50 秒。 |
| `history_poll_interval_seconds` | 否 | `3` | 補讀 assistant history 的間隔，範圍 0.5–60 秒。 |
| `rpc_timeout_seconds` | 否 | `25` | 單次 Hermes RPC timeout，範圍 3–180 秒。 |
| `retry_base_seconds` / `retry_max_seconds` | 否 | `2` / `60` | 失敗時的指數退避範圍。 |

## 安全與可靠性 / Security and reliability

- 外掛預設拒絕所有 Telegram 使用者，只接收 allowlist 中的 private chat。
- `bridge.sqlite3` 可能包含 chat ID、待處理輸入與待回覆文字；請把它當成敏感本機資料並設定合適的磁碟權限與備份政策。
- Hermes backend token 是高權限憑證。V1 只支援 loopback `hermes serve`；若跨機器使用，請以具身分驗證與加密的 tunnel 保持 endpoint 在本機邊界。
- Telegram update、歷史回覆與 outbound 訊息都有 SQLite 去重與佇列。Telegram 或 backend 短暫斷線時，正常情況會延後而非遺失。
- `prompt.submit` 結果不明時會進入 `uncertain`，而不是自動重送。這是刻意的防重複執行安全機制。

> **English:** The bridge is fail-closed for authorization, stores durable work in SQLite, and refuses to blindly retry an uncertain non-idempotent prompt submission.

## 升級、停用與還原 / Upgrade, disable, and rollback

Hermes core 與外掛原始碼彼此分離。升級 Hermes 或更新本外掛後，請先對已安裝副本執行：

```powershell
hermes plugins doctor --ci <plugin-path>
hermes plugins compat <plugin-path>
hermes gateway restart
```

若要改回 Hermes 原生 Telegram adapter：

```powershell
hermes plugins disable telegram-canonical-bridge
hermes config set platforms.telegram.enabled true
hermes gateway restart
```

只有在原生 adapter 已設定有效 `TELEGRAM_BOT_TOKEN` 時，才應重新啟用它。

## 疑難排解 / Troubleshooting

| 現象 | 建議處理 |
| --- | --- |
| Telegram 沒有回覆 | 先執行 `hermes gateway status`，再用 `hermes logs` 查看 gateway 與 plugin 啟動紀錄。 |
| bridge 顯示未設定 | 確認三個必要秘密已在 `<HERMES_HOME>/.env`，然後重啟 gateway。 |
| bot 無法連線或收訊不穩 | 檢查是否有另一個程式或原生 Telegram adapter 使用相同 bot token。 |
| `/status` 可用，但一般訊息沒有進入 Bot Chat | 檢查 `allowed_user_ids`、`home_chat_id` 與實際私訊 chat／使用者 ID 是否一致。 |
| 找不到 Controller 或 canonical Bot Chat | 在目標 profile 開啟或建立 canonical `Bot Chat`，並確認 `controller_profile` 完全一致。 |
| Controller 暫時無法連線 | 確認 `hermes serve` 正在 `127.0.0.1:9119` 監聽，且兩個 backend token 完全相同。 |
| 出現 `uncertain` 提示 | 先等候可能已在執行的回覆；若需要重試，請用新訊息重新提出需求，不要盲目重送同一筆工作。 |

## English summary

Telegram Canonical Bridge is a native Hermes platform plugin, not a fork of Hermes core. It routes allowlisted Telegram DMs to a profile's canonical Bot Chat so the main agent can keep Bot Mode and `message_agent`.

Install the repository in the Hermes user plugin directory, configure the three secret variables, add the bridge platform block with an explicit Telegram allowlist, run `hermes serve` on loopback, then start the gateway. Prefer a dedicated Telegram bot. If a token is already used by Hermes' built-in Telegram adapter, disable that adapter before starting this bridge.
