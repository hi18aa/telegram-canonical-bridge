# 開發規範

## 文件語言

文件一律以繁體中文為主；資料名稱、路徑、程式碼與必要的 API 名稱維持原文。

## 產品邊界

本專案只提供 Hermes 主 Agent 與本機專門 Bot profiles 之間的非同步派工 sidecar。

- 使用者與主 Agent 的 Telegram 對話一律由 Hermes 原生 Telegram adapter 處理。
- 本外掛不得註冊 platform adapter、輪詢 Telegram、讀取 Telegram token，或自行實作 Telegram Bot API。
- 本外掛不得連接 Hermes Desktop 私有 WebSocket RPC、呼叫 `session.resume`，或修改 Hermes core。
- 本外掛不得覆寫內建 `message_agent`；缺少該工具的 surface 使用 `agent_task_start`。
- Worker 只透過公開 Hermes CLI，在每個 task 的隔離 conversation 中啟動。
- 任務通知只透過公開 `hermes send` 回到原生平台。

## 可靠性契約

- `sent` 只代表背景 runner 已建立，不能描述成 Bot 已開始或完成。
- 任務完成必須有 worker hook、final reply 或背景程序完成通知等可指認證據。
- 同一 worker profile 序列執行；不同 profiles 才可並行。
- 一般補充訊息寫入 task inbox，不中斷既有 turn；只有明確取消才終止程序樹。
- 外部副作用結果不明時不得自動重派。
- 通知使用 durable outbox 並逐 task 保序。

## 變更門檻

提交前至少執行：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q telegram_canonical_bridge tests
hermes plugins doctor --ci .
hermes plugins compat .
```

發布前還必須把實際 artifact 安裝到 Controller 與至少一個 worker profile，從正式 Hermes 入口完成無外部副作用的端到端驗證。
