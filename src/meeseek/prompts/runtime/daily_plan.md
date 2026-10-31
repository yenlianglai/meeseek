# Daily plan messages — runtime/daily_plan.py (its system prompt: internal/daily_plan.md)

Sections are read by name — `prompts.text("runtime/daily_plan#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## user_message

以下是今天早上自動蒐集到的原始資料，據此產出今日計畫：

{{collected}}

## unread

⚠️ 未讀取（{{error}}）
