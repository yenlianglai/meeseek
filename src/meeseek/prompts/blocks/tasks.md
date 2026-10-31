# Task block — the per-turn view of the session's open tasks

Rendered by runtime/tasks.py:instruction; the task bodies are runtime data (one JSON file each under the cache's tasks/). Sections are read by name — `prompts.text("blocks/tasks#<section>", ...)`. This note is not sent to any model.

## open

## 進行中的任務（跨 turn 追蹤）
{{tasks}}

這些任務的目標與成敗條件是使用者同意過的，不要自行更改。推進任務後用 task_note 記下做了什麼；狀態改變或要約下次檢查用 task_update。
