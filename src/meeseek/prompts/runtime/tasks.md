# Task messages — runtime/tasks.py, what the task tools answer

Sections are read by name — `prompts.text("runtime/tasks#<section>", ...)`. This note is not sent to any model.

## not_console

[系統] 沒有執行：任務的{{action}}只開放給使用者在 console 下的指令，這一輪不是。把你的建議告訴使用者，由他決定。
