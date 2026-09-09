# Hermes Telegram Canonical Bridge

> 讓 Telegram 成為 Hermes canonical `Bot Chat` 的受控入口，並為原生 `message_agent` 加上可驗證的任務狀態與留言信箱。

**English:** A native Hermes plugin that routes allowlisted Telegram DMs to an existing canonical Bot Chat and adds evidence-based task tracking around the built-in `message_agent`.

## 這個外掛解決什麼問題

Hermes Controller 可透過 `message_agent` 把工作非同步派給 OT（其他 profile／bot），但原生回傳的 `sent` 只代表背景派工已建立：它不代表 OT 已完成，也不能證明瀏覽器已開啟。Telegram 使用者因此只能等最終回覆，不容易分辨工作是執行中、卡住，或已失敗。

V4 保留 Hermes 原生 `message_agent`，在外面加上一層耐久協調與 Telegram 活動呈現：

- Telegram 訊息仍進入同一個 canonical `Bot Chat`，不建立一般 Telegram session。
- 收到一般文字後立即回覆 ACK；Controller 或 OT 有活動證據時，每 4 秒續期 Telegram 原生「正在輸入…」。
- Controller 派工時建立 `TCB-...` 任務；重要狀態、明確里程碑與最終結果以新訊息形成時間線，不再默默改寫同一張卡。
- 使用者可回覆任一任務時間線訊息或使用 `/tell` 留言；OT 在工作檢查點透過 durable inbox 讀取。
- 任務、事件、留言與待送訊息存放在共用 SQLite；Controller 與 named profile 看到同一份 ledger。
- 以 process ID 關聯 Hermes 正式的背景完成通知，補回 `agents.list` 沒有提供的 exit code、類型化失敗原因，以及可辨識的遠端 OT 回覆。
- 不修改 Hermes core，也不覆寫內建工具，降低 Hermes 更新造成失效的範圍。

## 能知道什麼，不能知道什麼

| 顯示狀態 | 實際證據 | 不應解讀成 |
| --- | --- | --- |
| 準備派工 | `pre_tool_call` 已建立任務 | OT 已收到 |
| 已排入背景程序 | 原生 `message_agent` 回傳 `sent` 與 process handle | 訊息已交付、OT 已啟動或已完成 |
| Runner 執行中 | `agents.list` 顯示背景 handle 為 running | OT turn 已啟動或特定 UI 已開啟 |
| OT 處理中 | OT 的 `pre_llm_call` 已綁定任務；之後才可能觀察到 OT 工具呼叫 | 特定工具或網站已成功 |
| 等待收件／完成通知 | Hermes 回傳 durable `queued`／live Bot Chat 收件回條，或 runner 已退出但正式完成通知尚未抵達 | OT 已完成 |
| 明確進度 | OT 呼叫 `bridge_task_update` 回報可驗證里程碑；最終里程碑使用 `status=result` | OT 的內部思考或逐 token 串流 |
| 最終結果保底 | `post_llm_call` 的 final assistant response 經清理、截短後寫入任務時間線；`status=result` 的明確結果優先 | Controller 已收到 Hermes 背景通知 |
| OT 已產生回覆 | OT `post_llm_call` 已執行 | Controller 已收到 Hermes 原生通知 |
| 已完成 | OT `post_llm_call` 已產生 final 且 runner 結束，或 Hermes 完成通知帶有 exit 0 與可辨識的 OT 回覆 | 每個外部系統都一定成功 |
| 失敗 | Hermes 完成通知／process telemetry 提供非零 exit code，或 worker hook 明確失敗 | 可以安全地自動重派 |
| 執行結果未確認 | grace 後仍沒有 OT 啟動、final、exit code 或可辨識回覆 | 工作成功或失敗；尤其不可當成完成 |

這不是遠端桌面監看，也不會公開 chain-of-thought。若 OT 要說「頁面已開啟」，仍必須先有相應工具成功的結果。typing 與時間線提供的是可驗證的生命週期，不是假裝能看見 agent 的每一步。

## Session 與訊息壓縮

- **Controller 端：同一個 session。** Telegram 文字送到指定 profile 既有的 canonical `Bot Chat`，因此延續原本上下文與 Bot Mode。
- **OT 端：訊息進入目標 OT 自己的 canonical `Bot Chat`，但每次呼叫都是新的非同步背景 turn／process。** canonical 對話會延續；它仍不是一條可隨時插話的長連線，runtime ID 也可能因壓縮或重啟而輪替。
- **不同 Controller 呼叫同一個 OT：** 都進入該 OT 同一個 canonical `Bot Chat` 歷史，但每次是不同的 task ID／turn／runner。Hermes 負責依 Bot Chat 佇列序列化，不應同時寫同一段 session。
- **跨電腦：** 每台機器的 SQLite ledger 不會自動共享。遠端 OT 的逐步 hook 進度與 `/tell` inbox 不能跨機器同步；sender 端仍可在 Hermes 背景完成通知帶回 reply 時完成任務。長時間、需要可查狀態與防重複的跨機工作，優先使用 Hermes `peer run`／`peer status`。
- **Hermes 仍負責上下文壓縮。** 外掛不關閉或取代 Hermes 的 compaction；canonical runtime ID 改變時會重新解析 binding。
- **任務 ledger 不跟著對話壓縮。** 任務狀態、留言及必要時清理後的 OT 最終答覆摘要會獨立存放於 SQLite；它不是完整聊天記憶，也不保存 OT 私密思考、conversation history 或原始工具資料。

`/tell` 與回覆任一任務時間線訊息是**耐久留言**，不是即時中斷。OT 會在開始、自然里程碑與最終答覆前呼叫 `bridge_task_inbox`；若 OT 正卡在一個長時間、不可中斷的工具呼叫中，留言要等下一個檢查點才會被讀到。若留言在最後檢查點後才到達，bridge 會明確通知「未納入本次結果」、結清待讀數，並請使用者把需求另傳給 Controller，不會假裝已完成雙向傳遞。

## 架構

```text
Telegram private DM
        │ allowlist + durable inbound
        ▼
canonical Bot Chat (Controller)
        │ native message_agent + task marker
        ▼
OT background turn ── hooks/tools ──┐
        │ native final               │ progress / inbox / final fallback
        ▼                            ▼
Controller canonical history    shared SQLite
        │ optional reply mirror      │ durable task event timeline
        └──────── durable outbox ────┴──> Telegram + typing heartbeat
```

外掛只追蹤已綁定 canonical Bot Chat 發出的派工，以及那些任務內的巢狀派工；同一台電腦上其他 Hermes 對話的 `message_agent` 不會被送到 Telegram。

### Telegram 呈現模式

預設 `task_presentation: timeline`：

- 使用者送出一般文字後，Bot 先以新訊息確認收到，Controller 最終回覆也會回覆原始 Telegram 訊息。
- Controller 尚未回覆，或 task 處於 `dispatching`、`dispatched`、`running`、`returning` 時，bridge 每 4 秒重送 [`sendChatAction(typing)`](https://core.telegram.org/bots/api#sendchataction)。完成、失敗、等待使用者或後端狀態不明時不會假裝正在輸入。
- 任務狀態轉換與 OT 明確里程碑會各自呼叫 `sendMessage`。自動工具類別進度最短間隔 15 秒，避免洗版；一般進度使用 Telegram 靜音訊息，完成與錯誤正常通知。
- 第一則任務事件是 reply anchor，後續事件都回覆它；回覆任一已送出的任務事件都會對應到同一個 durable inbox。
- 同一任務的 outbox 嚴格依序傳送；前一則尚未送達時，後一則不會超車。

若偏好舊版單卡行為，可設定 `task_presentation: compact`。compact 仍會持續顯示 typing，但任務 revision 會編輯同一張完整狀態卡。

## 需求

- Python 3.11 以上。
- 支援原生 plugin tools 與 hooks 的 Hermes；本版已在 Hermes `0.21.0` 驗證。
- 若目標 Bot Chat 會長時間開在 Desktop／TUI，Hermes backend 應包含 2026-09-02 之後的 live Bot Chat delivery 修正；舊 backend 可能先回 `sent`，之後才以 `target_busy` 失敗。V4 會如實顯示失敗，但外掛不會修改 Hermes core 來繞過 session ownership。
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
可複製的欄位範例見 [`examples/hermes.env.example.txt`](examples/hermes.env.example.txt)；檔名刻意不使用 `.env.example`，避免 Hermes plugin installer 在外掛目錄自動產生一份帶 placeholder 的 `.env`。

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
      # timeline：每個重要事件都是新訊息；compact：沿用單一可編輯狀態卡。
      task_presentation: timeline
      # Telegram typing 最多只維持數秒；建議保留 4 秒。
      typing_interval_seconds: 4
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
| 回覆任一任務進度訊息 | 等同對該任務留言，不必手打 ID |
| `/help` | 顯示 Bot 內建說明 |

典型流程：

1. 在 Telegram 要求 Controller 派 OT 完成一項低風險工作。
2. Bot 立即 ACK，處理期間持續顯示「正在輸入…」。
3. Controller 呼叫原生 `message_agent` 後，Bot 送出第一則 `TCB-...` 任務事件。
4. 後續狀態與明確里程碑以新訊息加入時間線；需要補充時可回覆其中任何一則。
5. OT 的明確最終里程碑會留在時間線；若 OT 漏掉主動回報，bridge 會以 `post_llm_call` 的清理後最終答覆補上。
6. Hermes 原生背景完成通知會依 process ID 回填 exit code。exit 0 只有在同時有 final／可辨識 OT reply 時才結案；live Bot Chat 的排隊回條只會顯示等待。
7. Controller 對完成通知產生的後續回覆，仍會透過 canonical history 傳到 Telegram。

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

預期結果：先收到 ACK；工作期間可見 Telegram「正在輸入…」；派工、執行與完成各以新訊息出現，且完成訊息保留 OT 驗證結果。Hermes 若也把原生背景結果送回 Controller，Telegram 會另外收到 Controller 回覆。`process running` 本身不應被描述成「頁面已開啟」。

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
- Telegram update、canonical history、任務時間線與留言都有 SQLite 去重／佇列。
- `prompt.submit` 若送出後失去確認，會標成 `uncertain`，不盲目重送非冪等工作。
- token 不寫入 URL、SQLite 或 log；backend 建議只綁定 `127.0.0.1`。
- SQLite 可能含 Telegram ID、輸入內容、回覆及任務留言，請視為敏感本機資料。
- 對已追蹤任務，OT 的 final assistant response 可能被截短、清理後顯示給同一位 allowlisted Telegram 使用者；`bridge_task_update(status=result)` 的明確最終里程碑會優先，且不會保存 conversation history、原始 tool args/result 或 chain-of-thought。
- Hermes completion notification 為 exit 0 時，bridge 只擷取 runner stdout 中可辨識的 OT reply 並清理／截短；非零 exit 的原始 command、stack trace 與任意輸出不會複製到 Telegram，只顯示類型化失敗原因。
- typing 只在 Controller 有未完成回覆或 task 有活動狀態證據時續期；它代表「bridge 有工作中證據」，不代表某個特定視窗已開啟。
- Hook 故障採 fail-open：原生 `message_agent` 仍可執行，但該次狀態追蹤可能不完整。
- 事件 replay 是生命週期加速與補漏；Controller 自己的 assistant 回覆仍由持久 `session.history` 回收，OT 任務結果則由明確進度或 final hook 保底。

## 已知限制

- 目前只支援純文字 private chat；不支援群組、媒體或 token streaming。
- 一個 Controller profile 只綁定一個 Telegram 私訊 route。
- `/tell` 不會中斷正在執行的工具；讀取速度取決於 OT 是否到達 inbox 檢查點。
- OT 產生 final 時仍未讀的留言會標為 `missed` 並另發警告；它們不會被偷偷套用到已結束的工作。
- Hermes 若沒有把背景完成通知送回原始 Controller session，bridge 最多只能根據 hook 與全域摘要判定；缺少足夠證據時會進入「執行結果未確認」，不會自動重派。
- Hermes 的事件 replay 是有界 buffer；重啟或長時間中斷後可能缺少中間狀態，但 SQLite 中的 final hook 結果與 process 狀態仍可恢復主要任務狀態。
- 單獨的 `sent`、runner running、runner exited 或 exit code 0 都不再足以證明 OT 完成；缺少 final／reply 證據時會停在等待或「執行結果未確認」。
- 跨電腦時，逐步 hook telemetry 與 task inbox 不會穿越兩台機器各自的 SQLite；可攜的結果來源是 Hermes 自己帶回 sender 的 completion reply。

## `sent` 後沒有 OT 動靜時

先用 `/task TCB-...` 看 `OT turn`、`Final`、exit code 與 evidence：

- `OT turn：尚未觀察啟動` 且稍後出現 `target_busy`：更新並重啟**目標電腦**的 Hermes backend；訊息沒有進入 OT，不要把它當成網站流程失敗。
- `執行結果未確認`：bridge 沒拿到足以判斷的完成證據。不要盲目重派有外部副作用的工作；先查目標 Bot Chat 是否新增 assistant/tool row、`hermes --version`、`hermes plugins list` 與目標 backend log。
- `OT turn：已觀察啟動` 但 `Final：尚無證據`：runner／provider 在 turn 中途終止，或目標外掛 hook 沒有載入；在該 OT profile 執行 `hermes -p <profile> plugins doctor --ci <plugin-path>`。

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

The Controller keeps its existing canonical Bot Chat, and a local message is delivered into the target worker's own canonical Bot Chat. Each `message_agent` call is a separate asynchronous turn/process while the target's canonical conversation remains shared and persistent. V4 distinguishes a spawned runner from a started worker turn, correlates Hermes completion notifications by process ID, and never treats `sent`, runner activity, or exit 0 alone as proof that the worker finished. Cross-machine workers do not share the local SQLite ledger; their portable fallback is the reply carried back by Hermes' completion notification. Set `task_presentation: compact` for the legacy editable-card view. `/tell` is a checkpoint inbox, not a live interrupt. Hermes remains responsible for context compaction.

## License

[MIT License](LICENSE)
