# Hermes Telegram Canonical Bridge Plugin 簡易企畫書

## 一、企畫結論

本專案不再取代 Hermes 原生 Telegram，也不把 Telegram 訊息改送到 Desktop canonical 對話。

正確責任分工是：

```text
使用者 ↔ Hermes 原生 Telegram ↔ 主 Agent
                                  ↕
                           Canonical Bridge
                                  ↕
                  OT／Research／Publisher／其他 Bot
```

Hermes 原生 Telegram 已能處理文字、檔案、圖片、語音、命令、streaming 與 typing；重做這些能力只會擴大維護成本。真正缺少的是一般 Telegram 主 Agent session 無法可靠使用 canonical `message_agent` 所提供的 Agent-to-Agent 能力。因此 bridge 應是「派工 sidecar」，不是第二套 Telegram adapter。

## 二、專案目標

1. 主 Agent 在任何正常 session 中都能把工作派給本機專門 Bot profile。
2. 每項工作都有獨立 task ID、可查狀態、可驗證進度與 final 結果。
3. 一般新訊息不會自動中斷既有工作；補充與取消必須有明確語意。
4. 同一 Bot 的 canonical 對話不被多個 runner 同時寫入；不同 Bots 可並行。
5. 不修改 Hermes core、不覆寫 `message_agent`，以正式 plugin tools、hooks 與公開 CLI 實作。
6. Hermes 更新後即使 telemetry 降級，也不應把未知狀態誤報為成功。

## 三、非目標

- 不自行輪詢 Telegram。
- 不保存另一份 Telegram bot token。
- 不重新實作 Telegram 檔案、媒體、語音、群組或 streaming。
- 不公開 Agent chain-of-thought。
- 不把 `sent`、process running 或 exit 0 單獨當成任務完成。
- v0.5 不處理跨電腦 worker 的共享 ledger、即時 inbox 與遠端取消。

## 四、使用情境

使用者在 Telegram 對主 Agent 說：

```text
請交給 OT 助手檢查網站目前狀態，不要登入或修改資料。
```

主 Agent 根據 roster 呼叫：

```text
agent_task_start(target="operitrace-agent", message="...")
```

bridge 立即回傳 `TCB-...`，之後依真實證據送出新的 Telegram 任務訊息：

```text
已派工 → Bot 已啟動 → 明確里程碑 → 已產生 final → 已完成
```

主 Agent 不必佔住原 turn 輪詢；Hermes 背景完成通知與 bridge 時間線會回報結果。

## 五、Bot roster 與未來擴充

Controller 設定明確的 profile／角色對照：

```yaml
agents:
  operitrace-agent:
    role: 網站與瀏覽器操作
  research-agent:
    role: 研究與資料查證
  publisher-agent:
    role: 內容發布與結果確認
```

role 會注入主 Agent system prompt，讓它先判斷責任再派工。profile 名稱同時是允許清單；未列出的 Bot 不可呼叫。

每個 Bot profile 都有自己的 canonical `Bot Chat`、工具與權限。需要真正隔離上下文或憑證時，必須分成不同 profile，而不是只靠 task ID。

## 六、Session 與並行設計

- 一次派工建立一個新 task、turn 與 background process。
- 目標 Bot 的 canonical `Bot Chat` 持續沿用，因此 Bot 保有自己的長期工作脈絡。
- 同一 Bot profile 使用跨程序 file lock 序列化。
- 不同 Bot profiles 使用不同 lock，可平行執行。
- 不同主 Agent 呼叫同一 Bot 時仍共享該 Bot 的 canonical 歷史；task ledger 會記錄來源 Controller，並限制只有建立者能查詢、補充或取消。
- 對話過長時仍由 Hermes 自動 compaction；task ledger 不隨 compaction 消失。

## 七、雙向溝通語意

### 補充指示

`agent_task_message` 只把訊息寫入 durable inbox，不會中斷 Bot 目前的工具呼叫。Bot 在開始、自然里程碑或 final 前呼叫 `bridge_task_inbox` 讀取。

這能明確區分：

- 已保存：Controller 端已有耐久紀錄。
- 已讀取：Bot 的 inbox tool 已取走留言。
- 未納入：留言在 final 後抵達，不假裝 Bot 曾讀到。

### 明確取消

`agent_task_cancel` 先進入 `stopping`，再透過 Hermes `process_manage kill` tree-kill runner 與子程序。確認成功才標示 `cancelled`；找不到 handle 時標示 `unconfirmed`。

取消無法回滾已完成的外部操作，因此有發布、付款或刪除等副作用的工作仍需冪等設計。

## 八、Telegram 呈現原則

原生 Telegram 繼續負責主 Agent 的 typing。背景 Bot 不會偽裝成持續輸入；bridge 只在有新證據時直接發布一則新訊息，不反覆編輯舊卡片。

狀態通知透過 `hermes send` 使用原生 outbound route。SQLite outbox 保證同一 task 依序送出；delivery runner 對暫時錯誤做 exponential retry，避免最後的 completion 卡在佇列。

## 九、相容性策略

穩定邊界：

- Hermes plugin manifest。
- `register_tool`、`register_hook`、`register_command`、`register_system_prompt_section`。
- 公開 `hermes chat`、`hermes send` CLI。
- Hermes 正式 `terminal` background process 與 `process_manage`。

避免：

- 修改 Hermes 安裝檔。
- import 私有 Desktop UI state。
- 直接寫入 Hermes conversation database。
- 模擬 `message_agent` 的未公開內部協定。

每次 Hermes 更新後應執行 compile、unit tests、plugin doctor、compat，以及一輪安全的本機 E2E。

## 十、版本路線

### v0.5：同機 sidecar

- 多 Bot roster。
- canonical Bot Chat reuse。
- task lifecycle、進度、final、inbox、取消。
- durable SQLite 與原生 Telegram outbound timeline。
- 同 Bot 序列化、不同 Bot 並行。
- 同機附件路徑複製。

### 後續版本：正式 Runs API

若要支援跨電腦與更強的即時控制，應建立具下列能力的 transport-neutral Runs API：

- `POST /runs`
- `GET /runs/{id}`
- `POST /runs/{id}/messages`
- `POST /runs/{id}/cancel`
- event stream／webhook
- artifact upload/download
- idempotency key、租約與權限範圍

Telegram、CLI、Desktop 或其他平台都只成為 Runs API 的入口，而不是各自實作一套 Agent-to-Agent 邏輯。

## 十一、完成條件

1. 原生 Telegram 功能維持不變。
2. 主 Agent 在 Telegram session 可看見並呼叫 `agent_task_start`。
3. worker 沿用既有 canonical `Bot Chat`，且同一 profile 不發生並行寫入。
4. `sent`、worker start、explicit progress、final、exit code 各自有真實狀態。
5. 新訊息不會隱式中斷；inbox 可證明已保存與已讀取。
6. 明確取消能 tree-kill 本機程序樹，終態不被稍後通知覆寫。
7. Telegram 狀態以新訊息發布，暫時失敗後 outbox 能自行排空。
8. Hermes core 零修改，plugin doctor 與 compat 通過。
