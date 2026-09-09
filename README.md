# Hermes Telegram Canonical Bridge

> 讓 Telegram 成為 Hermes canonical `Bot Chat` 的受控入口，並為原生 `message_agent` 加上可驗證的任務狀態與留言信箱。

**English:** A native Hermes plugin that routes allowlisted Telegram DMs to an existing canonical Bot Chat and adds evidence-based task tracking around the built-in `message_agent`.

## 這個外掛解決什麼問題

Hermes Controller 可透過 `message_agent` 把工作非同步派給 OT（其他 profile／bot），但原生回傳的 `sent` 只代表背景派工已建立：它不代表 OT 已完成，也不能證明瀏覽器已開啟。Telegram 使用者因此只能等最終回覆，不容易分辨工作是執行中、卡住，或已失敗。

V2 保留 Hermes 原生 `message_agent`，在外面加上一層耐久協調：

- Telegram 訊息仍進入同一個 canonical `Bot Chat`，不建立一般 Telegram session。
- Controller 派工時建立 `TCB-...` 任務，Telegram 收到一張會持續編輯的狀態卡。
- OT 開始、呼叫高階工具、主動回報里程碑、產生最終答覆及背景程序結束時，狀態會依證據更新。
- 使用者可回覆任務卡或使用 `/tell` 留言；OT 在工作檢查點透過 durable inbox 讀取。
- 任務、事件、留言與待送訊息存放在共用 SQLite；Controller 與 named profile 看到同一份 ledger。
- 不修改 Hermes core，也不覆寫內建工具，降低 Hermes 更新造成失效的範圍。

## 能知道什麼，不能知道什麼

| 顯示狀態 | 實際證據 | 不應解讀成 |
| --- | --- | --- |
| 準備派工 | `pre_tool_call` 已建立任務 | OT 已收到 |
| 已排入背景程序 | 原生 `message_agent` 回傳 `sent` 與 process handle | OT 已完成 |
| OT 處理中 | OT turn 開始、背景程序為 running，或觀察到工具呼叫 | 特定 UI 已成功開啟 |
| 明確進度 | OT 呼叫 `bridge_task_update` 回報可驗證里程碑 | OT 的內部思考或逐 token 串流 |
| 正在回傳 | OT 已產生最終答覆 | Controller 已把答案送到 Telegram |
| 已完成 | OT 已產生最終答覆且程序已結束，或 telemetry 明確取得 exit code 0 | 每個外部系統都一定成功 |
| 結果待確認 | 程序已結束，但全域摘要沒有 exit code／final hook | 工作成功或失敗 |

這不是遠端桌面監看，也不會公開 chain-of-thought。若 OT 要說「頁面已開啟」，仍必須先有相應工具成功的結果。狀態卡提供的是可驗證的生命週期，不是假裝能看見 agent 的每一步。

## Session 與訊息壓縮

- **Controller 端：同一個 session。** Telegram 文字送到指定 profile 既有的 canonical `Bot Chat`，因此延續原本上下文與 Bot Mode。
- **OT 端：訊息進入目標 OT 自己的 canonical `Bot Chat`，但每次呼叫都是新的非同步背景 turn／process。** canonical 對話會延續；它仍不是一條可隨時插話的長連線，runtime ID 也可能因壓縮或重啟而輪替。
- **Hermes 仍負責上下文壓縮。** 外掛不關閉或取代 Hermes 的 compaction；canonical runtime ID 改變時會重新解析 binding。
- **任務 ledger 不跟著對話壓縮。** 任務狀態與留言獨立存放於 SQLite，但它不是完整聊天記憶，也不保存 OT 私密思考。

`/tell` 與回覆任務卡是**耐久留言**，不是即時中斷。OT 會在開始、自然里程碑與最終答覆前呼叫 `bridge_task_inbox`；若 OT 正卡在一個長時間、不可中斷的工具呼叫中，留言要等下一個檢查點才會被讀到。

## 架構

```text
Telegram private DM
        │ allowlist + durable inbound
        ▼
canonical Bot Chat (Controller)
        │ native message_agent + task marker
        ▼
OT background turn ── hooks/tools ──┐
        │ final response             │ progress / inbox
        ▼                            ▼
Controller history              shared SQLite
        │                            │ editable task card
        └──────── durable outbox ────┴──> Telegram
```

外掛只追蹤已綁定 canonical Bot Chat 發出的派工，以及那些任務內的巢狀派工；同一台電腦上其他 Hermes 對話的 `message_agent` 不會被送到 Telegram。

## 需求

- Python 3.11 以上。
- 支援原生 plugin tools 與 hooks 的 Hermes；本版已在 Hermes `0.21.0` 驗證。
- 一個專用 Telegram bot，及可信任使用者的 numeric Telegram User ID。
- 目標 Controller profile 已有可用的 canonical `Bot Chat`。
- 本機 `hermes serve` WebSocket backend；建議只監聽 loopback。

## 安裝與啟用

以下命令可在 Windows PowerShell 執行；其他平台使用相同 Hermes CLI 命令。

### 1. 安裝到 Controller

```powershell
hermes plugins install hi18aa/telegram-canonical-bridge --enable
hermes tools enable telegram_canonical_bridge --platform cli
```

### 2. 安裝到每一個會接收派工的 profile

OT profile 必須載入 hooks 與三個 bridge tools。以 `operitrace-agent` 為例：

```powershell
hermes -p operitrace-agent plugins install hi18aa/telegram-canonical-bridge --enable
hermes -p operitrace-agent tools enable telegram_canonical_bridge --platform cli
```

有多個 OT profile 時逐一執行。OT profile 不必設定 Telegram bot token，也不必啟用 bridge platform；它只需要 plugin 與 toolset。

### 3. 設定 Controller 的秘密

秘密寫入 Hermes `.env`，不要放進 `config.yaml`、Git 或 `backend_url`。

```powershell
# 從 BotFather 取得真正的 token 後替換範例值。
hermes config set TELEGRAM_CANONICAL_BRIDGE_BOT_TOKEN '123456:replace-with-real-token'

# 建立固定的本機 backend token。
$bytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$backendToken = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

# 兩者必須完全相同。
hermes config set HERMES_DASHBOARD_SESSION_TOKEN $backendToken
hermes config set TELEGRAM_CANONICAL_BRIDGE_BACKEND_TOKEN $backendToken
```

### 4. 設定 Controller platform

將以下內容合併到 Controller 的 `<HERMES_HOME>/config.yaml`。設定位置是根層的 `platforms`，不是 `gateway.platforms`。

```yaml
platforms:
  telegram_canonical_bridge:
    enabled: true
    extra:
      backend_url: ws://127.0.0.1:9119/api/ws
      controller_profile: default

      # 必填；預設拒絕所有人。
      allowed_user_ids:
        - '123456789'

      # 選填：再鎖定單一 private chat。
      home_chat_id: '123456789'

      telegram_poll_timeout_seconds: 40
      history_poll_interval_seconds: 3
      rpc_timeout_seconds: 25
      retry_base_seconds: 2
      retry_max_seconds: 60
```

完整範例見 [`examples/gateway-config.yaml`](examples/gateway-config.yaml)。

若自訂 `state_path`，Controller 與所有 OT profile 必須透過 `TELEGRAM_CANONICAL_BRIDGE_STATE_PATH` 指向**同一個絕對路徑**。未設定時，外掛會使用 machine-root：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/bridge.sqlite3
```

### 5. 避免 Telegram polling 衝突

同一個 bot token 只能有一個 `getUpdates` consumer。建議建立 bridge 專用 bot；若重用 Hermes 原生 Telegram bot，先停用原生 adapter：

```yaml
platforms:
  telegram:
    enabled: false
```

### 6. 啟動 backend 與 gateway

```powershell
# 終端機 A；只監聽本機。
hermes serve --host 127.0.0.1 --port 9119 --skip-build
```

```powershell
# 終端機 B。
hermes gateway restart
```

## Telegram 使用方式

| 輸入 | 作用 |
| --- | --- |
| 一般文字 | 送進 canonical Bot Chat |
| `/status` | 顯示 bridge/backend 佇列狀態 |
| `/tasks` | 最近 10 個派工任務 |
| `/task TCB-...` | 查看一個任務的最新狀態與證據 |
| `/tell TCB-... 內容` | 對仍在執行的任務留下 durable note |
| 回覆任務卡 | 等同對該任務留言，不必手打 ID |
| `/help` | 顯示 Bot 內建說明 |

典型流程：

1. 在 Telegram 要求 Controller 派 OT 完成一項低風險工作。
2. Controller 呼叫原生 `message_agent` 後，Bot 送出一張 `TCB-...` 任務卡。
3. 同一張卡依實際證據更新；需要補充時直接回覆它。
4. OT 的最終結果仍先回到 Controller，再由 canonical history 傳回 Telegram。

## 驗收

```powershell
hermes plugins doctor --ci <controller-plugin-path>
hermes plugins compat <controller-plugin-path>
hermes -p operitrace-agent plugins doctor --ci <ot-plugin-path>
hermes gateway status
```

Telegram 可用這個低風險測試：

```text
請用 message_agent 請 OT 開啟 https://example.com，確認頁面標題後回報；不要登入或修改任何資料。
```

預期結果：出現一張任務卡、狀態至少經過派工與執行證據、OT 結果回到 Controller，最後 Telegram 收到 Controller 回覆。`process running` 本身不應被描述成「頁面已開啟」。

## 更新與移除

```powershell
# 更新 Controller；各 named profile 也要分別更新。
hermes plugins update telegram-canonical-bridge
hermes -p operitrace-agent plugins update telegram-canonical-bridge

hermes plugins doctor --ci <plugin-path>
hermes plugins compat <plugin-path>
hermes gateway restart
```

停用：

```powershell
hermes plugins disable telegram-canonical-bridge
hermes -p operitrace-agent plugins disable telegram-canonical-bridge
hermes gateway restart
```

SQLite ledger 不會因停用 plugin 自動刪除。確認不再需要歷史與待送資料後，才由管理者自行備份或移除。

## 安全與可靠性

- 僅接受 allowlist 中使用者的 private chat；預設 fail closed。
- Telegram update、canonical history、任務卡與留言都有 SQLite 去重／佇列。
- `prompt.submit` 若送出後失去確認，會標成 `uncertain`，不盲目重送非冪等工作。
- token 不寫入 URL、SQLite 或 log；backend 建議只綁定 `127.0.0.1`。
- SQLite 可能含 Telegram ID、輸入內容、回覆及任務留言，請視為敏感本機資料。
- Hook 故障採 fail-open：原生 `message_agent` 仍可執行，但該次狀態追蹤可能不完整。
- 事件 replay 是加速與補漏；最終 assistant 回覆仍以持久 `session.history` 為準。

## 已知限制

- 目前只支援純文字 private chat；不支援群組、媒體或 token streaming。
- 一個 Controller profile 只綁定一個 Telegram 私訊 route。
- `/tell` 不會中斷正在執行的工具；讀取速度取決於 OT 是否到達 inbox 檢查點。
- Hermes 的事件 replay 是有界 buffer；重啟或長時間中斷後可能缺少中間狀態，但 final history 與 process 狀態仍可恢復主要結果。
- `completed` 是 final hook 加程序結束，或 exit code 0 的證據，不是對任務內容正確性的保證；缺少足夠證據時會顯示「結果待確認」。

更完整的資料模型、相容性策略與故障行為見 [`docs/實作設計與驗收.md`](docs/實作設計與驗收.md)。

## 開發

```powershell
python -m compileall -q .
python -m unittest discover -s tests -v
hermes plugins doctor --ci .
hermes plugins compat .
```

## English quick start

1. Install and enable the plugin on the Controller and every worker profile.
2. Enable the `telegram_canonical_bridge` toolset for each profile.
3. Configure the Telegram/backend secrets only on the Controller.
4. Add the root-level `platforms.telegram_canonical_bridge` block with an explicit user allowlist.
5. Run `hermes serve` on loopback and restart the gateway.

The Controller keeps its existing canonical Bot Chat, and a local message is delivered into the target worker's own canonical Bot Chat. Each `message_agent` call is still a separate asynchronous background turn/process; the plugin does not turn it into a live duplex connection. Task cards report evidence-backed lifecycle events, and `/tell` creates a durable note that the worker reads at explicit checkpoints. Hermes remains responsible for context compaction.

## License

[MIT License](LICENSE)
