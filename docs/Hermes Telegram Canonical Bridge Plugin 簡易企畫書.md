# Hermes Telegram Canonical Bridge Plugin 簡易企畫書

## 一、專案名稱

Hermes Telegram Canonical Bridge Plugin

簡稱：

`telegram-canonical-bridge`

---

## 二、專案目的

目前 Hermes Desktop 的 canonical Bot Chat 可以使用：

`message_agent`

但 Telegram 私訊進入 Hermes 後，會建立獨立 Telegram Session，因此無法取得 canonical Bot Chat 專屬的 `message_agent` 能力。

本專案目標是建立一個 Hermes Platform Plugin，讓 Telegram 不再直接進入一般 Telegram Session，而是轉送到指定 Controller 的 canonical Bot Chat。

最終流程：

```text
Telegram
   ↓
Telegram Bridge Plugin
   ↓
Controller Canonical Bot Chat
   ↓
message_agent
   ↓
OT / Coder / Researcher / 其他 Agent
```

---

## 三、核心需求

### 1. Telegram 訊息轉送

收到指定 Telegram 使用者訊息後：

```text
Telegram Message
    ↓
驗證使用者
    ↓
找到指定 Controller Profile
    ↓
找到 canonical Bot Chat
    ↓
送入 canonical Bot Chat
```

禁止建立：

```text
agent:main:telegram:dm:<chat_id>
```

這類一般 Telegram Session 作為主要對話 Session。

---

### 2. 保留 message_agent 能力

Bridge 本身不重新實作：

```text
message_agent
```

而是確保訊息真正執行於：

```text
Controller Canonical Bot Chat
```

由 Hermes 原生提供：

```text
message_agent
peer roster
bot roster
Bot Mode protocol
```

---

### 3. Telegram 回覆

Controller canonical Bot Chat 的即時回覆需回傳 Telegram。

```text
Controller
    ↓
response
    ↓
Telegram Bridge
    ↓
Telegram
```

---

### 4. 背景 Agent 結果回傳

當 Controller 使用：

```text
message_agent("ot", "執行任務")
```

OT 完成後的 background completion 也必須轉回 Telegram。

完整流程：

```text
Telegram
    ↓
Controller
    ↓
message_agent
    ↓
OT
    ↓
完成任務
    ↓
Controller
    ↓
Telegram
```

避免只收到：

```text
已交給 OT
```

但最終結果只存在 Desktop Bot Chat。

---

## 四、安全設計

只允許指定 Telegram User ID。

環境變數：

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=123456789

TELEGRAM_BRIDGE_PROFILE=controller
```

收到 Telegram 訊息時：

```ts
if (!allowedUsers.includes(message.from.id)) {
  return
}
```

未知使用者：

```text
直接忽略
或
回覆 Unauthorized
```

Hermes API 不需要對 Internet 公開。

---

## 五、Plugin 結構

建議：

```text
~/.hermes/plugins/telegram-canonical-bridge/

├── plugin.yaml
├── adapter.py
├── canonical.py
├── state.py
└── README.md
```

### plugin.yaml

定義：

```yaml
name: telegram-canonical-bridge
label: Telegram Canonical Bridge
kind: platform
```

### adapter.py

負責：

```text
Telegram 收訊
Telegram 發訊
User allowlist
附件處理
```

### canonical.py

負責：

```text
找到 Profile
找到 canonical Bot Chat
送出 Agent Turn
取得 Response
```

### state.py

保存：

```text
Telegram chat_id
Telegram user_id
canonical session_id
最後活動時間
```

---

## 六、第一版 V1 範圍

V1 只做必要功能。

包含：

```text
文字訊息
Telegram User Allowlist
固定 Controller Profile
Canonical Bot Chat Routing
Immediate Response
Background Completion
基本錯誤處理
Log
```

暫時不做：

```text
多使用者
多 Profile 切換
群組聊天
語音
圖片
檔案
Inline Keyboard
Web UI
管理後台
權限分級
```

---

## 七、V1 使用方式

使用者：

```text
Telegram：
幫我叫 OT 打開 Chrome 並確認目前頁面
```

Bridge：

```text
Telegram
    ↓
Controller Canonical Bot Chat
```

Controller：

```text
message_agent(
  target="ot",
  message="打開 Chrome 並確認目前頁面"
)
```

OT 執行完成。

結果：

```text
OT → Controller → Bridge → Telegram
```

使用者最後直接在 Telegram 收到：

```text
Chrome 已開啟，目前頁面為……
```

---

## 八、錯誤處理

至少處理以下狀況：

### Canonical Bot Chat 找不到

```text
Canonical Bot Chat unavailable
```

寫入 Log，不建立普通 Telegram Session 作為 fallback。

### Controller 未啟動

回覆：

```text
Controller currently unavailable.
```

### OT / Peer Offline

由 Controller / `message_agent` 原生結果回傳。

### Telegram API 發送失敗

記錄：

```text
chat_id
message_id
timestamp
error
```

---

## 九、技術原則

核心原則只有一個：

```text
Telegram 是 UI / Transport
Controller Canonical Bot Chat 才是 Agent Runtime
```

不要形成：

```text
Telegram Agent
    ↓
Controller Agent
    ↓
OT
```

而應該是：

```text
Telegram
    ↓
Bridge
    ↓
Controller
    ↓
OT
```

Telegram 本身不具備 Agent 身份。

---

## 十、未來 V2

V1 穩定後再加入：

```text
Telegram 圖片
Telegram 檔案
Telegram 語音
多 Controller
/agent 指令
/status
/agents
/tasks
/cancel
Agent 執行狀態
Streaming
任務通知
多 Telegram 使用者權限
```

例如：

```text
/agents

controller   online
ot           online
coder        online
researcher   offline
```

或：

```text
/status

OT
Task: Facebook login
Status: running
Duration: 42s
```

---

## 十一、驗收標準

V1 完成後至少通過：

```text
1. Telegram 傳文字可以進 Controller canonical Bot Chat

2. Controller 可以正常看到 message_agent

3. Telegram 不再產生普通 Telegram Agent Session

4. Controller 可以 message_agent → OT

5. OT 執行完成後結果可以回 Telegram

6. 非 Allowlist Telegram ID 無法使用

7. Desktop Bot Chat 與 Telegram 操作的是同一個 canonical Bot Chat

8. Hermes 重啟後 Bridge 可以恢復運作
```

---

## 十二、最終架構

```text
                 ┌───────────────┐
                 │ Hermes OT     │
                 │ Computer Use  │
                 └───────▲───────┘
                         │
                    message_agent
                         │
┌────────────┐    ┌──────┴──────────┐
│ Telegram   │───►│ Telegram Bridge │
└────────────┘    └──────┬──────────┘
                         │
                         ▼
                 ┌───────────────┐
                 │ Controller    │
                 │ Canonical     │
                 │ Bot Chat      │
                 └───────────────┘
```

核心成果：

**讓手機繼續使用 Telegram，但所有訊息實際由 Hermes Controller canonical Bot Chat 執行，因此保留完整 `message_agent` 與跨 Agent orchestration 能力。**