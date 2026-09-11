# Hermes Telegram Canonical Bridge

保留 Hermes 原生 Telegram，補上「主 Agent → 專門 Bot」可追蹤、可補充、可取消的非同步派工能力。

**English:** Keep Hermes' native Telegram integration and add a durable, observable delegation path from a controller Agent to specialized local Hermes profiles.

## 為什麼需要它

Hermes 原生 Telegram 已能穩定處理使用者與主 Agent 之間的文字、typing、附件、媒體及命令；但 Telegram session 不一定具備 canonical `Bot Chat` 才會注入的內建 `message_agent`。因此常見情況是：

- 使用者能在 Telegram 正常要求主 Agent 派工。
- 主 Agent 卻無法可靠地喚醒 OT、研究、發布或其他專門 Bot。
- `sent` 容易被誤解成 Bot 已開始；實際上可能只有訊息入列，沒有任何 worker turn。
- 使用者看不到開始、進度、結果或失敗證據。

本外掛只補這段缺口。它提供 `agent_task_start` 等工具，不接管 Telegram，也不覆寫 Hermes 內建 `message_agent`。

```text
使用者
  ↕ Hermes 原生 Telegram（唯一入口）
主 Agent
  ↕ agent_task_* + durable ledger
每個 task 的隔離 Hermes conversation
  ↕
專門 Bot profile（OT／Research／Publisher／其他）
```

## v0.6.3 的核心模式

- 每個派工建立獨立 conversation：`TCB Task <TASK_ID>`。
- 不再把工作塞進可能正由 Desktop 持有的 canonical `Bot Chat`。
- 不同主 Agent 或不同 task 不共用 Bot 對話內容，不會混 session。
- 同一個 Bot profile 的 runner 依序執行，避免瀏覽器或本機資源互撞；不同 profiles 可並行。
- `sent` 只表示背景 runner 已建立。看到 worker hook、明確進度或 final output 才能判定真正開始／完成。
- runner 會直接收斂 durable ledger；來源 session 的 completion notification 是通知主 Agent 與第二條保險，不是唯一完成條件。
- 進度以 Telegram 新訊息發布，不反覆改寫同一則舊卡片。
- 一般新訊息不會中斷舊任務；明確取消才會停止該任務的程序樹。
- 補充指示寫入 durable inbox，由 Bot 在自然檢查點讀取，不是假裝成即時 interrupt。
- 需要逐字對外使用的文字走獨立 `exact_payload` artifact；task ID、進度與 inbox 指引不會接在正文後面。

## 明確邊界

本外掛不會：

- 註冊 Telegram platform adapter。
- 輪詢 Telegram `getUpdates` 或保存另一份 Bot token。
- 重新實作 Telegram 的檔案、圖片、語音、typing、streaming 或命令。
- 連線 Hermes Desktop 私有 WebSocket RPC。
- 呼叫 `session.resume`、修改 Hermes core 或包裝內建 `message_agent`。
- 在外部副作用結果不明時自動重派。

因此 Hermes 更新時，主要相依面只剩公開 plugin tools/hooks、`hermes chat`、`hermes send` 與背景 process 工具。

## 提供的工具

Controller 使用：

- `agent_task_start`：建立 task 並啟動目標 Bot；逐字文字可用 `exact_payload.text` 自動建立 artifact。
- `agent_task_status`：查詢可驗證狀態。
- `agent_task_message`：加入補充指示。
- `agent_task_cancel`：明確取消仍在執行的 runner。

Worker 使用：

- `bridge_task_update`：回報可公開、可驗證的里程碑或結果。
- `bridge_task_inbox`：讀取 Controller 補充指示。
- `bridge_task_status`：讀取 task ledger。

另提供 `/agenttask`：

```text
/agenttask list
/agenttask status TCB-20260910-ABC123
/agenttask message TCB-20260910-ABC123 補充條件
/agenttask cancel TCB-20260910-ABC123
```

## 系統需求

- Hermes Agent v0.21.0 或更新版本。
- Hermes 原生 Telegram 已完成設定。
- Controller 與 worker profiles 在同一台機器上。
- 每個 worker profile 可由 `hermes -p <profile> chat ...` 正常執行。
- Python 3.11 以上。

目前是同機 sidecar，不是跨機器 Runs API。

## 安裝

### 1. 先確認原生 Telegram

```powershell
hermes gateway status
hermes send --list telegram
```

Telegram token、allowlist 與 home channel 一律用 Hermes 原生 `gateway setup` 管理：

```powershell
hermes gateway setup
```

### 2. 安裝 Controller 外掛

```powershell
hermes plugins install https://github.com/hi18aa/telegram-canonical-bridge.git --enable
hermes tools enable telegram_canonical_bridge --platform telegram
hermes tools enable telegram_canonical_bridge --platform cli
```

### 3. 安裝每個 worker profile

以下以 `operitrace-agent` 為例：

```powershell
hermes -p operitrace-agent plugins install https://github.com/hi18aa/telegram-canonical-bridge.git --enable
hermes -p operitrace-agent tools enable telegram_canonical_bridge --platform cli
```

每個可能接收派工的 profile 都要安裝並啟用外掛，才能提供 worker 啟動、里程碑、inbox 與 final hook 證據。

### 4. 設定 Controller

合併以下片段到 Controller 的 `config.yaml`；可用 `hermes config path` 查詢位置：

```yaml
plugins:
  enabled:
    - telegram-canonical-bridge
  entries:
    telegram-canonical-bridge:
      allow_tool_override: false
      settings:
        delivery_target: telegram
        controller_profiles:
          - default
        agents:
          operitrace-agent:
            role: OperiTrace 網站與瀏覽器操作助手
          research-agent:
            role: 研究與資料查證助手
        lock_timeout_seconds: 3600
        copy_attachments: true
```

`delivery_target: telegram` 會使用 Hermes 原生 Telegram home channel；若要固定目的地，可用 `telegram:<chat_id>`。

完整片段見 [examples/gateway-config.yaml](examples/gateway-config.yaml)。

### 5. 重啟與檢查

```powershell
hermes gateway restart
hermes plugins doctor telegram-canonical-bridge
hermes -p operitrace-agent plugins doctor telegram-canonical-bridge
hermes tools list --platform telegram
hermes -p operitrace-agent tools list --platform cli
```

Doctor 應顯示一般 plugin、7 個 tools、5 個 hooks，且沒有 platform registration。

## 從 v0.5 或更舊版本乾淨升級

v0.6 是刻意的 clean break，不讀舊 schema，也不啟動舊 adapter。

1. 先確認舊任務是否可能已造成外部副作用；不確定時不要重派。
2. 強制重裝 Controller 與 workers：

```powershell
hermes plugins install https://github.com/hi18aa/telegram-canonical-bridge.git --force --enable
hermes -p operitrace-agent plugins install https://github.com/hi18aa/telegram-canonical-bridge.git --force --enable
```

3. 移除舊 platform 設定；原生 `platforms.telegram` 保留：

```powershell
hermes config unset platforms.telegram_canonical_bridge
```

4. 重啟 gateway，再執行 Doctor。

舊資料庫：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/bridge.sqlite3
```

v0.6 完全不讀它，因此不會重播舊 inbound、route 或 outbox。確認不再需要稽核後可自行備份或刪除。新版使用：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/tasks.sqlite3
```

## 使用方式

在 Telegram 直接對主 Agent 說：

```text
請派給 OT 助手檢查這個網站目前是否可開啟；不要登入或修改資料。派工後告訴我 task ID。
```

主 Agent 應呼叫 `agent_task_start` 並先回覆 `TCB-...`。後續 Telegram 會收到新的事件訊息，例如：

```text
📨 任務 TCB-...｜@operitrace-agent
已排入 Hermes 背景程序：等待隔離對話啟動

🔄 任務 TCB-...｜@operitrace-agent
Bot 處理中：@operitrace-agent 已開始處理這個 turn

✅ 任務 TCB-...｜@operitrace-agent
已完成：公開頁面檢查完成，未登入也未修改資料
```

Hermes 原生 Telegram 仍負責主 Agent 的 typing。背景 Bot 不會偽造持續 typing；它只在有真實狀態變化時發布新訊息。

### 逐字公開文字

若任務包含「這段文字必須原樣發布／提交」，主 Agent 應把純正文放在 `exact_payload.text`，`message` 只放操作摘要與限制。例如：

```json
{
  "target": "operitrace-agent",
  "message": "把 exact payload 作為單篇貼文；先完成安全檢查，未獲授權不要發布。",
  "exact_payload": {
    "kind": "exact_text",
    "text": "第一行\n\n保留兩個空格  與星星 ⭐⭐"
  }
}
```

使用者只需在 Telegram 提供一般命令與正文，不需要手動建立檔案。Bridge 會把 `text` 的 UTF-8 bytes 原樣寫入：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/exact-payloads/blobs/sha256/<SHA256>/body.utf8.txt
```

工具回傳與 `agent_task_status` 都會提供 `artifact_path`、`byte_length`、`sha256`、`manifest_path` 與即時 `verified` 結果。Worker 必須直接讀取 artifact bytes 並核對 digest；不得從 task 對話重建逐字正文。Bridge 的 task marker、`bridge_task_update`、`bridge_task_inbox` 與 completion 指引只留在控制面，正文 artifact 與 manifest 不含這些注入內容。

`pre_llm_call` 不再對一般 Controller turn 回傳重複的動態派工說明；Controller 指引只由正式 system prompt section 提供。對 worker 則只綁定 task 與記錄啟動證據，不回傳逐 task 控制文字。控制說明位於 handoff 前段，Controller 任務摘要位於後段，而 exact body 完全不進入 handoff conversation。

**English:** For byte-exact public text, pass `exact_payload.text`. The bridge stores it as a separate UTF-8 artifact and returns a verified SHA-256 contract; the worker conversation receives only the artifact reference, never the body.

## 新訊息、取消與 session 規則

- Telegram 的一般新訊息只會開啟主 Agent 的新 turn，不會自動取消任何 task。
- 要補充：請說「補充到 task `TCB-...`：……」，主 Agent 會呼叫 `agent_task_message`。
- 要停止：必須明確要求取消指定 task，主 Agent 才能呼叫 `agent_task_cancel`。
- 取消先保存為 `stopping`。同一 Controller 可使用既有程序 handle；不同 Controller 程序由原 runner 讀取取消要求，停止它自己啟動的 worker 程序樹並確認結束。
- `stopping`／`ok: true` 只表示已接受取消；查到 `cancelled` 才表示停止已確認。較晚的 worker 進度不會把取消要求蓋回 `running`。取消不能回滾已送出的貼文、交易或其他外部副作用，也不能當成允許重送。
- v0.6.3 不會熱更新升級前已啟動的舊 runner；更新應在任務閒置時進行。舊 runner 找不到 handle 時不可宣稱已停止，也不應手改 ledger 或刪除 lock。
- 每個 task 都是新的隔離 conversation，因此不同 Controller／task 不會共用上下文。
- task conversation 不跨任務累積大量訊息；主 Telegram session 的壓縮仍由 Hermes 原生機制管理。

## 附件

Telegram 附件仍由 Hermes 原生 adapter 接收。主 Agent 若取得同機檔案路徑，可把它放入 `agent_task_start.attachments`。預設會複製到：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/attachments/<TASK_ID>/
```

限制：最多 10 個檔案，合計 128 MiB。這是同機檔案交接，不支援跨機器路徑。

## 狀態真實性

`sent`、`dispatched` 與 `completed` 意義不同：

- `sent`／`dispatched`：只證明 background runner 已建立。
- `running`：worker profile 的 `pre_llm_call` 或可辨識 tool hook 已出現。
- `returning`：worker final response 已被 hook 觀察。
- `stopping`：取消要求已保存，仍待原 runner 或程序管理確認停止。
- `cancelled`：已確認取消；不代表外部動作已回滾。
- `completed`：final 證據與 runner 結束已收斂，或 runner 有可辨識的 final output。
- `unconfirmed`：runner 已結束，但缺少足夠 final 證據；不可描述成成功，也不可盲目重派。

事件寫入 SQLite durable outbox，再透過公開 `hermes -p <Controller profile> send` 依 task 保序傳送；暫時失敗會退避重試。送信程序即使由 worker hook 喚醒，也會明確切回來源 Controller profile，不會誤用 worker 的 Telegram 設定。每個正常 Controller turn 也會重新喚醒尚未送完的 outbox。即使一次性 Controller CLI 已先結束，runner 仍會直接寫入最終狀態並喚醒 outbox。

## 疑難排解

### Telegram 正常，但主 Agent 沒有派工工具

```powershell
hermes tools enable telegram_canonical_bridge --platform telegram
hermes gateway restart
```

再用 `hermes tools list --platform telegram` 確認已啟用。

### 只有「已建立 runner」，沒有 Bot 開始事件

依序檢查：

```powershell
hermes -p operitrace-agent plugins doctor telegram-canonical-bridge
hermes -p operitrace-agent tools list --platform cli
hermes -p operitrace-agent chat -Q -q "只回覆 WORKER_OK"
```

若 worker CLI 本身無法完成，先修正該 profile 的 provider、model、credentials 或 tool 設定。這不是網站操作失敗，而是 worker turn 尚未啟動。

### 看不到 Telegram 任務事件

確認 Controller 設定 `delivery_target: telegram`，並執行：

```powershell
hermes send --list telegram
```

若工具是由非預設 Controller profile 使用，請改成 `hermes -p <profile> send --list telegram`。`delivery_target: local` 只確認 ledger/outbox，不會真的傳 Telegram，適合測試。

### 檢查 ledger

```powershell
python -c "from telegram_canonical_bridge import BridgeState, shared_state_path; s=BridgeState(shared_state_path()); print(shared_state_path()); print([(t.id,t.target,t.status,t.progress) for t in s.list_tasks(limit=10)])"
```

## 開發與驗證

```powershell
python -m compileall -q telegram_canonical_bridge tests
python -m unittest discover -s tests -v
hermes plugins doctor --ci .
hermes plugins compat .
```

建立不含 `build/`、pycache 或 legacy runtime 的 source artifact：

```powershell
./scripts/build-source-artifact.ps1
```

產物位於 `dist/telegram-canonical-bridge-<version>-source.zip`。正式 `hermes plugins install` 仍以 immutable Git commit 為權威；此 zip 用於 cutover 稽核、離線比對與從同一份 source 重建。

發布前還要把同一個 commit 的 artifact 安裝到 Controller 與至少一個 worker，完成無外部副作用的真實 E2E。

## English quick start

This plugin leaves native Telegram completely in charge of user-facing messages, files, media, commands, streaming, and typing. It adds a separate controller-to-worker task bridge for surfaces where Hermes' built-in `message_agent` is unavailable.

1. Configure native Telegram with `hermes gateway setup` and verify `hermes send --list telegram`.
2. Install and enable this plugin on the controller and every worker profile.
3. Enable `telegram_canonical_bridge` for the controller's `telegram` surface and every worker's `cli` surface.
4. Configure `delivery_target: telegram` and an explicit `agents` roster.
5. Restart the gateway and run Plugin Doctor for both controller and worker.

Every task runs in its own named Hermes conversation (`TCB Task <TASK_ID>`), so Desktop ownership of canonical `Bot Chat` cannot turn a dispatch into a queue-only acknowledgement. Tasks targeting the same worker profile are serialized. Follow-up messages are durable inbox notes; only explicit cancellation terminates the process tree.

## License

[MIT](LICENSE)
