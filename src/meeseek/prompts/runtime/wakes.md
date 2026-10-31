# Wake messages — runtime/wakes.py, the turns a wake starts

Sections are read by name — `prompts.text("runtime/wakes#<section>", ...)`. This note is not sent to any model.

## claude_followup

[系統] 先前交給 Claude Code 的任務{{status}}：「{{title}}」（session {{session_id}}）。
Claude 的最後回覆：

{{reply}}

接手處理：對照原本的任務目標檢查這個結果（需要時用 claude_check 或讀取工具確認），把結論回報給使用者；沒完成就說明差在哪、建議下一步。

## task_check

[系統] 任務 [{{task_id}}] 的檢查時間到了。

{{task}}

推進這個任務：用讀取工具查目前狀態，往成功條件推進一步。做了什麼用 task_note 記下（成功的一步記 progress，嘗試與觀察記 note）；還沒完成就用 task_update 安排下次檢查（next_check_hours）；達成成功條件就用 task_update 設 done 並回報使用者；確定無法達成就說明原因，等使用者決定是否放棄。
