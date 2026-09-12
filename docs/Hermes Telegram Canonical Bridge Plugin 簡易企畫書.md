# Hermes Telegram Canonical Bridge Plugin 簡易企畫書

版本：v0.6 clean break

## 一、企畫目的

使用者維持透過 Hermes 原生 Telegram 與主 Agent 溝通。外掛只補上主 Agent 派工給 OT、Research、Publisher 或其他專門 Bot 的缺口，使派工可啟動、可追蹤、可補充、可取消，且能把實際結果送回原 Telegram。

```text
使用者 ↔ Hermes 原生 Telegram ↔ 主 Agent
                                  ↕ Agent Task Bridge
                     OT／Research／Publisher／其他 Bot
```

Hermes 原生 Telegram 已有文字、附件、圖片、語音、命令、streaming 與 typing。重新實作只會增加風險，因此 bridge 是派工 sidecar，不是第二套 Telegram adapter。

## 二、要解決的問題

1. Telegram session 不一定有 canonical `Bot Chat` 才注入的內建 `message_agent`。
2. `sent` 只能證明派送程序建立，不能證明 Bot turn 已啟動。
3. canonical `Bot Chat` 若由 Desktop 持有，其他 CLI 可能只入列而不執行。
4. 背景 Bot 沒有 typing 狀態時，使用者容易以為整個流程死掉。
5. 修改同一則狀態訊息會讓時間線難以閱讀。
6. 不同主 Agent 若共用同一個 Bot conversation，可能混入上下文。

## 三、設計目標

1. 原生 Telegram 是唯一使用者入口。
2. 主 Agent 有明確 `agent_task_start` 工具可派工。
3. 每個 task 真正啟動一個獨立 Bot turn，而不是只寫入 conversation。
4. 使用者依序看到 dispatched、running、progress、result 或 failure 新訊息。
5. 不同 Controller 與 task 的 worker context 完全隔離。
6. 同一 worker 依序執行，避免瀏覽器／桌面操作互撞。
7. 不修改 Hermes core、不覆寫內建 `message_agent`。
8. Hermes 更新時只需維持公開 plugin、CLI 與 process contracts。

## 四、非目標

- 不輪詢 Telegram。
- 不保存另一份 Telegram Bot token。
- 不重新實作 Telegram 檔案、媒體、語音、群組或 typing。
- 不把 Telegram 訊息改送到 Desktop 對話。
- 不依賴 Desktop 私有 WebSocket RPC。
- 不在外部操作結果不明時自動重派。
- v0.6 不相容舊資料 schema，也不遷移舊 platform adapter 狀態。

## 五、主要流程

使用者在 Telegram 說：

```text
請交給 OT 助手檢查網站目前狀態，不要登入或修改資料。
```

主 Agent：

1. 從設定的 roster 選出 `operitrace-agent`。
2. 呼叫 `agent_task_start`。
3. 取得 `TCB-...` 與 background process ID。
4. 立即告訴使用者 task ID，並結束目前 turn。

runner：

1. 為 task 建立 `TCB Task <TASK_ID>` 隔離 conversation。
2. 取得同一 worker 的 OS lock 後呼叫公開 `hermes chat`。
3. Worker `pre_llm_call` 出現後才標記 `running`。
4. Worker 以 `bridge_task_update` 回報重要里程碑。
5. runner 直接寫入 process completion；來源 session notification 用相同規則再確認。
6. `post_llm_call` 與 process completion 共同收斂 completed。

使用者會收到新的 Telegram 任務事件，不會只看到一張持續被改寫的卡片。

## 六、session 與並行模型

### 每個 task 都是新 session

conversation title 固定為：

```text
TCB Task <TASK_ID>
```

所以：

- 不同主 Agent 呼叫同一 Bot：不同 session，不混上下文。
- 同一主 Agent連續派兩個 task：仍是不同 session。
- Desktop 正開著 canonical `Bot Chat`：不影響 task conversation。
- task 本身不長期累積訊息；主 Telegram conversation 的 compaction 仍由 Hermes 負責。

### 同一 worker 仍序列執行

session 雖分開，但瀏覽器 profile、桌面或本機資源可能共享，所以相同 worker profile 以 lock 排隊。未來若某個 worker 能安全並行，再把 concurrency 做成明確可設定能力，不能靠猜測。

## 七、雙向溝通

### Bot → 使用者

Bot 在有可驗證進展時呼叫 `bridge_task_update`。事件經 SQLite outbox 與 `hermes send` 直接發布成 Telegram 新訊息。

### 使用者 → Bot

一般 Telegram 新訊息不會自動中斷既有 task。主 Agent 必須：

- 補充 active task：呼叫 `agent_task_message`，寫入 inbox。
- 接續 resumable task：仍呼叫 `agent_task_message`，但指示只做既有狀態查詢、修復或 reconcile；bridge 會進入同一個 task conversation，不重送原任務。
- 明確取消指定 task：呼叫 `agent_task_cancel`，終止程序樹。
- 新需求：建立新 task。

Bot 在開始、重要步驟前後及 final 前讀取 `bridge_task_inbox`。這是 cooperative checkpoint，不宣稱毫秒級 interrupt。

Worker turn 在 final 前異常結束時，bridge 先標記 `settling` 並等待原 runner exit。只有收斂為 `interrupted`／`unconfirmed` 後才允許明確 continuation；中斷本身不會自動啟動第二個 runner，也不代表網站或其他外部操作失敗。

## 八、狀態與真實性

- `sent`：runner 已建立。
- `running`：真的觀察到 worker turn 或工具活動。
- `waiting`：Bot 明確在等條件／留言。
- `blocked`：需要協助。
- `settling`：Bot turn 已中斷，等待原 runner 收斂；不可另開重複任務。
- `returning`：worker final 已產生。
- `completed`：有足夠 final 與 process 證據。
- `interrupted`：worker 已開始但沒有 final，程序已非零結束；可接續同一 task，外部結果保持未知。
- `unconfirmed`：程序結束但結果證據不足；可接續同一 task 查證。
- `failed`：worker 啟動前已有可分類或明確失敗。若 continuation 無法取得原 task conversation，原 task 保持 resumable，等待人員判斷，不自動建立空白 conversation。
- `cancelled`：process kill 已確認。

Bridge 不會把「已排入」、「exit 0 但空回覆」或「背景 handle 消失」描述成成功。狀態查詢另提供 `lifecycle=active|settling|resumable|terminated`，讓主 Agent 可明確選擇補充、等待、接續或停止。

## 九、Telegram 呈現

原生 Telegram 繼續負責主 Agent 的 typing。背景 Bot 不偽造 typing；有事件時直接 PO 新訊息：

```text
📨 任務 TCB-...｜@operitrace-agent
已排入 Hermes 背景程序：等待隔離對話啟動

🔄 任務 TCB-...｜@operitrace-agent
Bot 處理中：已開始處理

✅ 任務 TCB-...｜@operitrace-agent
已完成：公開頁面檢查完成
```

不可反覆編輯同一則舊內容，因為使用者需要看見事件先後。

## 十、資料與安全

新版 ledger：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/tasks.sqlite3
```

只保存 task、event、note、outbox。任務原文用權限受限的暫存檔交給 CLI，runner 接管後刪除；不把內容放進 shell command line。

附件採同機複製，最多 10 個、合計 128 MiB。外掛不讀 Telegram token，也不保存模型的內部思考或原始工具資料。

## 十一、舊版處理

舊的 platform adapter、Telegram API client、Desktop RPC、protocol 與 platform service 全部刪除。v0.6 不讀：

```text
<HERMES_ROOT>/plugin-data/telegram-canonical-bridge/bridge.sqlite3
```

舊檔保留只供稽核。確認沒有未明外部副作用後，管理者可以備份或刪除。安裝時也應移除 `platforms.telegram_canonical_bridge`；原生 `platforms.telegram` 不動。

## 十二、第一階段交付

- general Hermes plugin，0 platform registration。
- 4 個 Controller tools。
- 3 個 Worker tools。
- 5 個 lifecycle hooks。
- task 專屬 conversation runner。
- per-worker OS lock 與 tree-kill cancellation。
- 精簡 SQLite ledger 與 durable outbound timeline。
- Controller／worker 安裝文件、繁體中文 README、英文 quick start、MIT License。

## 十三、後續方向

如果 Hermes 未來提供正式 Runs API，bridge 可把 transport 改成：

```text
start_run → run_id
get_events(run_id, cursor)
send_input(run_id, message)
cancel_run(run_id)
```

現有 `agent_task_*` 契約、task ID、狀態機與 Telegram 呈現可以保留，只替換底層 runner。

## 十四、完成條件

1. 原生 Telegram 功能不變。
2. 主 Agent 的 Telegram session 可呼叫 `agent_task_start`。
3. worker 執行的是 `TCB Task <TASK_ID>`，不是被 Desktop 持有的 `Bot Chat`。
4. 真實測試能看到 running 與 completed，不只 sent。
5. 多個 Controller／task 不共享 worker session。
6. 補充留言可在 inbox 被讀取；明確取消可 tree-kill。
7. Telegram 事件用新訊息發布，outbox 最終歸零。
8. Plugin Doctor 為 7 tools、5 hooks、0 platform。
9. 程式碼中沒有舊 adapter、Telegram API、Desktop RPC 或 schema migration。
