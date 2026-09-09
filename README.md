# Hermes Telegram Canonical Bridge

> 保留 Hermes 原生 Telegram 的完整能力，補上「主 Agent ↔ 專門 Bot」可追蹤、可補充、可取消的派工通道。

**English:** Keep Hermes' native Telegram integration and add a durable, observable task bridge from a controller Agent to specialized Hermes bot profiles.

## 為什麼需要這個外掛

Hermes 的 canonical `Bot Chat` 具有原生 `message_agent`，但一般 Telegram session 不一定能使用同一套 Agent-to-Agent 溝通能力。結果是：使用者可以在 Telegram 正常與主 Agent 對話，主 Agent 卻不容易可靠地把工作交給 OT、研究、發布或其他專門 Bot。

這個外掛只補上缺少的那一段：

```text
使用者
  ↕ Hermes 原生 Telegram（文字、檔案、媒體、命令、typing）
主 Agent / Controller
  ↕ agent_task_* sidecar
專門 Bot profiles（OT、研究、發布……）
```

它不接管 Telegram polling、不保存另一份 Telegram token，也不重做 Hermes 已有的檔案與媒體功能。主 Agent 仍由 Hermes 原生 Telegram 接收訊息；外掛透過 Hermes 公開 CLI 把任務送入目標 profile 自己的 canonical `Bot Chat`，再以 durable ledger 與原生 `hermes send` 回報任務時間線。

## 核心能力

- `agent_task_start`：主 Agent 非同步派工給 allowlisted 專門 Bot。
- `agent_task_status`：查詢已建立、Bot 是否真正啟動、final 是否出現及 exit code。
- `agent_task_message`：替執行中的任務加入耐久補充指示；不會偷偷中斷目前工作。
- `agent_task_cancel`：明確取消本機 runner，並終止其 Hermes 子程序樹。
- `bridge_task_update`：專門 Bot 回報可驗證的里程碑或最終結果。
- `bridge_task_inbox`：專門 Bot 在自然檢查點讀取補充指示。
- `bridge_task_status`：專門 Bot 查詢自己目前綁定的任務。
- 每個任務都有 `TCB-...` ID；事件、留言、程序關聯及待送通知存入共用 SQLite。
- Telegram 任務狀態以新訊息形成時間線，不反覆改寫同一則舊訊息。
- 短生命週期 delivery runner 會依序傳送並重試暫時失敗的通知；Agent hook 不必卡著等待 Telegram CLI。
- 不修改 Hermes core、不覆寫內建 `message_agent`，盡量縮小 Hermes 更新的破壞面。

## 真實狀態語意

`sent` 只代表背景 runner 已成功建立，不代表 Bot 已完成。外掛把可觀察狀態分開：

| 狀態 | 已有證據 | 不能據此宣稱 |
| --- | --- | --- |
| `dispatching` | 主 Agent 已建立耐久派工 intent | Bot 已收到 |
| `dispatched` | Hermes 已建立背景 process handle | Bot turn 已啟動 |
| `running` | 目標 Bot 的 `pre_llm_call` 已綁定任務 | 瀏覽器或網站已成功 |
| 明確進度 | Bot 呼叫 `bridge_task_update` | Bot 的私密思考或逐 token 內容 |
| `returning` | Bot 已產生 final assistant response | Controller 已收到完成通知 |
| `completed` | 已觀察 final／可辨識 reply，且 runner exit 0 | 所有外部副作用都可回滾 |
| `failed` | runner 非零 exit 或明確錯誤 | 可以安全自動重派 |
| `unconfirmed` | 缺少足夠啟動／final／exit 證據 | 成功或失敗 |
| `cancelled` | Hermes 已確認終止程序樹 | 已完成的網站或外部動作被撤銷 |

這不是遠端桌面監看，也不會揭露 chain-of-thought。要說「頁面已開啟」仍必須有對應工具的成功證據。

## Session、壓縮與並行規則

- 主 Agent 的 Telegram 對話仍是 Hermes 原生 session；外掛不建立第二條 Telegram 對話。
- 每次派工都是新的 task、turn 與 background process，但會沿用目標 profile 的 canonical `Bot Chat`，不是每次建立全新對話。
- 同一個目標 Bot profile 以跨程序鎖序列化，避免兩個 runner 同時寫入同一個 canonical 對話。
- 不同 Bot profiles 可並行工作。
- 不同主 Agent 若呼叫同一個 Bot profile，會共享該 Bot 的 canonical 歷史；每次任務仍有獨立 task ID。若需要資料、權限或上下文隔離，請建立不同的 worker profile。
- canonical 對話過長時仍由 Hermes 自己 compaction；外掛不關閉或取代壓縮。
- 任務 ledger 不隨對話壓縮消失，但只保存狀態、必要摘要與留言，不保存私密推理。

## 新訊息與取消會怎麼運作

一般 Telegram 新訊息只會開啟主 Agent 的新 turn，**不會自動取消舊任務**。

若要補充正在執行的任務，主 Agent 使用 `agent_task_message`。內容會進入 durable inbox，Bot 在下一個 `bridge_task_inbox` 檢查點讀取；若 Bot 正卡在長時間工具呼叫中，必須等工具返回。這是可驗證的非同步留言，不是假裝存在即時雙向 socket。

若要停止，必須明確要求主 Agent 取消，或使用：

```text
/agenttask cancel TCB-...
```

取消會 tree-kill 本機 runner 及其子程序。若 Bot 已產生 final，或外部網站已經完成操作，取消不會回滾那些結果。

## Telegram 呈現方式

Hermes 原生 Telegram 繼續負責使用者與主 Agent 之間的 typing、streaming、附件及命令。主 Agent 完成派工 turn 後，背景 Bot 不會偽裝成持續輸入；它會在有真實生命週期證據時直接送出新的任務訊息，例如：

```text
🧭 TCB-...｜已派工
⚙️ TCB-...｜OT 處理中
📍 TCB-...｜已完成登入前檢查
✅ TCB-...｜已完成
```

這些訊息透過 Hermes 原生 outbound route 傳送。短暫網路或 gateway 失敗時會留在 SQLite outbox 依序重試，後面的 completion 不會越過前面的進度。

## 需求

- Hermes Agent，需支援 native plugins、tools 與 hooks；本版已在 Hermes `0.21.0` 驗證。
- Controller 與 worker profiles 位於同一台機器。
- Hermes 原生 Telegram 已設定完成，且有 home channel 可供 `hermes send --to telegram` 使用。
- 每個 worker profile 都能建立或開啟 canonical `Bot Chat`。
- Python 3.11 以上；一般 Hermes 安裝已自帶相容的 Python 環境。

目前版本不是跨電腦 Runs API。跨電腦不會自動共享 SQLite、inbox 或逐步 hooks；請勿把本機 sidecar 的保證套用到遠端 peer。

## 安裝

以下命令以 Windows PowerShell 為例；Hermes CLI 命令在其他平台相同。

### 1. 先確認 Hermes 原生 Telegram

```powershell
hermes gateway setup
hermes gateway restart
hermes gateway status
hermes send --list telegram
```

最後一個命令應列出 Telegram home channel。Telegram token 與 allowlist 仍由 Hermes 原生設定管理。

### 2. 安裝到 Controller

```powershell
hermes plugins install https://github.com/hi18aa/telegram-canonical-bridge.git --enable
hermes tools enable telegram_canonical_bridge --platform telegram

# 選填：若也要從 Hermes CLI 測試或操作任務。
hermes tools enable telegram_canonical_bridge --platform cli
```

### 3. 安裝到每個 worker profile

以 `operitrace-agent` 為例：

```powershell
hermes -p operitrace-agent plugins install https://github.com/hi18aa/telegram-canonical-bridge.git --enable
hermes -p operitrace-agent tools enable telegram_canonical_bridge --platform cli
```

每個接收派工的 profile 都要安裝並啟用 plugin，才能提供完整的 `running`、進度、inbox 與 final hook 證據。worker 不需要 Telegram token，也不需要啟用 Telegram platform。

### 4. 設定 Bot roster

將下列設定合併到 Controller 的 `<HERMES_HOME>/config.yaml`；建議明確列出可派工 profiles，不要開放所有本機 profiles。

```yaml
plugins:
  enabled:
    - telegram-canonical-bridge
  entries:
    telegram-canonical-bridge:
      allow_tool_override: false
      settings:
        # 任務事件送到 Hermes 原生 Telegram home channel。
        # 也可指定 telegram:<chat_id>。
        delivery_target: telegram

        # 只有這些主 Agent profiles 可以建立／管理任務。
        controller_profiles:
          - default

        # profile 名稱同時是允許清單；role 會注入主 Agent system prompt。
        agents:
          operitrace-agent:
            role: OperiTrace 網站與瀏覽器操作助手
          research-agent:
            role: 研究與資料查證助手

        # 同一 worker 最多等待前一任務多久，預設 3600 秒。
        lock_timeout_seconds: 3600

        # 將同機附件複製到共用 plugin-data，預設 true。
        copy_attachments: true

platforms:
  telegram:
    enabled: true

  # 從 v0.4 升級時，請停用舊自管 adapter，避免兩個 poller 共用 token。
  telegram_canonical_bridge:
    enabled: false
```

完整範例見 [`examples/gateway-config.yaml`](examples/gateway-config.yaml)。v0.5 sidecar 不需要新增秘密；[`examples/hermes.env.example.txt`](examples/hermes.env.example.txt) 只說明與原生 Telegram 的責任邊界。

### 5. 驗證並重啟

```powershell
hermes plugins doctor --ci "$env:LOCALAPPDATA\hermes\plugins\telegram-canonical-bridge"
hermes -p operitrace-agent plugins doctor --ci "$env:LOCALAPPDATA\hermes\profiles\operitrace-agent\plugins\telegram-canonical-bridge"
hermes plugins compat "$env:LOCALAPPDATA\hermes\plugins\telegram-canonical-bridge"
hermes gateway restart
hermes gateway status
```

不同 Hermes 安裝方式的 plugin 路徑可能不同；可先用 `hermes plugins show telegram-canonical-bridge` 查詢。

## 使用方式

平常直接用自然語言告訴主 Agent：

```text
請派給 OT 助手檢查這個網站；不要登入或修改資料。派工後先告訴我 task ID。
```

主 Agent 會依 roster 選擇 `agent_task_start`，先回傳 `TCB-...`，後續進度與結果會以 Telegram 新訊息出現。

也可直接使用 plugin command：

| 命令 | 作用 |
| --- | --- |
| `/agenttask list` | 最近 10 個由此 Controller 建立的任務 |
| `/agenttask status TCB-...` | 查詢一個任務的證據與狀態 |
| `/agenttask message TCB-... 內容` | 加入補充指示，不中斷目前 turn |
| `/agenttask cancel TCB-...` | 明確終止 runner 程序樹 |

### 附件

Telegram 檔案仍由 Hermes 原生 adapter 接收。主 Agent 若已取得同機檔案路徑，可在 `agent_task_start.attachments` 傳給 worker；預設會複製到：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/attachments/<TASK_ID>/
```

每次最多 10 個檔案，總計 128 MiB。這不是跨電腦檔案傳輸。

## 更新與移除

```powershell
hermes plugins update telegram-canonical-bridge
hermes -p operitrace-agent plugins update telegram-canonical-bridge
hermes gateway restart
```

停用：

```powershell
hermes plugins disable telegram-canonical-bridge
hermes -p operitrace-agent plugins disable telegram-canonical-bridge
hermes gateway restart
```

SQLite ledger 不會因停用 plugin 自動刪除。確認不再需要歷史與待送資料後，再由管理者備份或移除：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/bridge.sqlite3
```

## 安全與可靠性

- roster、`controller_profiles` 與 profile existence 三層限制派工目標。
- 任務內容放入權限受限的暫存檔，不出現在 runner command line。
- 同機附件會複製到共用 plugin-data；該目錄與 SQLite 都應視為敏感資料。
- start 以來源 session＋tool call ID 冪等化，避免 tool retry 重複建立 runner。
- 同一 worker profile 以 OS file lock 序列化；不同 workers 才並行。
- 狀態事件使用 durable outbox、逐 task 保序與 bounded exponential retry。
- background completion 以 Hermes process ID、worker session/turn、explicit result 舷取 final reply 交叉驗證。
- 只有建立任務的 Controller profile 可以查詢、補充或取消該任務。
- hook 失效不會覆寫 Hermes core；缺少證據時標示 `unconfirmed`，不假裝完成，也不自動重派有副作用的工作。

## 疑難排解

### Telegram 對主 Agent 正常，但沒有 `agent_task_start`

```powershell
hermes plugins show telegram-canonical-bridge
hermes tools enable telegram_canonical_bridge --platform telegram
hermes gateway restart
```

既有 Telegram session 若沒有更新過 system prompt，可先建立新 session 或重新啟動 gateway。

### 只有 `sent`，Bot 沒有啟動

使用 `/agenttask status TCB-...`。若 `worker_started` 為 false，常見原因是目標 profile 不存在、plugin 未安裝、canonical `Bot Chat` 正被另一個 surface 持有，或 provider 在第一個 turn 前失敗。不要把這種情況描述成網站流程失敗。

### 同一個 Bot 的第二項工作在等待

這是預期的序列化。若工作本來就應並行，建立第二個 worker profile；不要讓兩個 runner 同時寫同一個 canonical session。

### 任務訊息暫時沒出現在 Telegram

確認：

```powershell
hermes send --list telegram
hermes gateway status
```

事件會留在 durable outbox，由 delivery runner 重試。請勿因畫面安靜就立刻重派可能有外部副作用的工作。

### `Unknown toolsets: telegram_canonical_bridge`

Hermes `0.21.0` 在 plugin discovery 與 CLI toolset 驗證之間可能出現啟動競態。只要 `plugins doctor`、工具清單及實際 hook 正常，這不是任務失敗；本外掛 runner 只會過濾「完全由本外掛名稱構成」的這一條已知假警告，其他 unknown toolsets 仍會保留。

更完整的資料模型與驗收紀錄見 [`docs/實作設計與驗收.md`](docs/實作設計與驗收.md)。

## 開發與驗收

```powershell
python -m compileall -q telegram_canonical_bridge tests
python -m unittest discover -s tests -v
hermes plugins doctor --ci .
hermes plugins compat .
```

本機安全 E2E 應至少證明：主 Agent 可派工、worker 沿用 canonical `Bot Chat`、明確 progress/result 可回傳、補充留言在 inbox 被讀取、取消會 tree-kill，以及 outbox 最終歸零。

## English quick start

This plugin keeps Hermes' native Telegram adapter in charge of user-facing messaging, files, media, commands, streaming, and typing. It adds only the missing controller-to-worker path.

1. Configure native Telegram with `hermes gateway setup` and verify `hermes send --list telegram`.
2. Install and enable this plugin on the controller and every worker profile.
3. Enable the `telegram_canonical_bridge` toolset for the controller's `telegram` platform and each worker's `cli` platform.
4. Set `delivery_target: telegram`, explicitly list `controller_profiles`, and define the allowed `agents` roster.
5. Disable the legacy `platforms.telegram_canonical_bridge` adapter when upgrading from v0.4; native `platforms.telegram` stays enabled.
6. Restart the gateway and ask the controller to delegate a safe test task.

Each dispatch creates a new task/turn/process but reuses the target profile's persistent canonical `Bot Chat`. Calls to the same worker are serialized; different worker profiles may run in parallel. New instructions are durable inbox notes, not live interrupts. Cancellation tree-kills the local runner but cannot roll back external actions already completed. Hermes remains responsible for conversation compaction. v0.5 is a same-machine sidecar, not a cross-machine Runs API.

## License

[MIT License](LICENSE)
