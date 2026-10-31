# Inbox — runtime/inbox.py: what an agent in an Inbox session is told about the approval gate

Sections are read by name — `prompts.text("runtime/inbox#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## held

`{{tool}}` 沒有執行：這是 Inbox 自動處理的事件，所有寫入都要使用者按同意（在 Inbox 頁，或從手機傳來的就在手機上）才會執行，這個呼叫已經列為待同意的動作。在回覆裡用一兩句說明這個動作要做什麼、為什麼，然後結束這一輪；不要說已經完成，也不要換別的工具繞過。

## approved

使用者同意執行 `{{tool}}`，參數如下（可能經過使用者修改，請完全照用）：

```json
{{args}}
```

現在用這組參數呼叫 `{{tool}}`，回報結果，然後繼續下一步。下一個寫入動作一樣會等使用者同意。

## rejected

使用者不同意執行 `{{tool}}`。說明：{{note}}

不要再嘗試同一個動作；依照說明調整，或在回覆裡說明還能怎麼做。
