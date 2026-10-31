# Memory headings — the per-turn memory block every agent receives

Assembled by context/memory.py:memory_instruction; the bodies are runtime data (knowledge_base/notes/, skills/<name>/learned.md). Sections are read by name — `prompts.text("blocks/memory#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## pinned

## 團隊筆記（手動維護）
{{notes}}

## me

## 關於使用者（個人化：確定的和多次觀察到的照做；標「推斷」的只是參考）
{{me}}

## team

## 團隊互動（從多場會議觀察到，團隊名單沒寫的；參考用）
{{team}}

## facts

## 已確認的事實
{{facts}}

## rules

## 學習規則（經使用者同意）
{{rules}}
