# Callback messages — what the guardrails in runtime/results.py inject into a turn

Sections are read by name — `prompts.text("runtime/callbacks#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## soft_cap

ℹ️ 本回合已呼叫 `{{tool}}` {{calls}} 次，超過這個 skill 的建議上限。用目前已取得的證據收斂作答；只有在明確缺少關鍵資訊時才再查一次。

## hard_cap

⚠️ 本回合 `{{tool}}` 已呼叫 {{calls}} 次（建議上限 {{budget}}）。停止再查，用目前已取得的證據直接回答；證據不足就明講查了哪些關鍵字、找到了什麼。

## grace

⚠️ 本回合的工具呼叫預算（{{budget}} 次）已用完。這是最後一次回覆機會：整理目前已取得的證據作答，列出已完成與未完成的部分，並建議使用者下一回合從哪裡繼續。不要再呼叫工具。

## time_grace

⏱ 這個回合已經跑了超過 {{minutes}} 分鐘（自動任務的時間上限）。這是最後一次回覆機會：用目前已取得的資料立刻作答，列出已完成與未完成的部分；查不完的明說還缺什麼。不要再呼叫工具。

## empty_reply

（系統）你上一步結束時沒有寫出任何回覆文字，使用者什麼都沒看到。根據上面已經查到的結果直接回答；資訊不夠就說明查了什麼、還缺什麼。不要重複已經做過的查詢。

## model_error

⚠️ 模型呼叫失敗（已自動重試仍未成功）：{{detail}}
請稍後重試這個問題；若持續發生，請把這段錯誤訊息回報給維護者。

## compaction_prompt

你在為一個工程團隊的 AI agent 壓縮對話歷史。請把下面的對話（可能含工具呼叫與結果）濃縮成一段給 agent 自己看的工作記憶，繁體中文，最多 1200 字，必須保留：
1. 使用者的原始目標與所有明確要求／限制
2. 已確認的事實與數字（含來源：檔名、ticket、路徑）
3. 已做過的查詢與結論（避免重查）
4. 已做出的決定與待辦
5. 尚未解決的問題
不要加評論，不要省略檔案路徑與 ID。

## compaction_previous

## 更早的摘要
{{summary}}

## compaction_transcript

## 要壓縮的對話
{{transcript}}

## compaction_head

【先前對話摘要（自動壓縮）】
{{summary}}

（以上是較早對話的壓縮摘要；下面是最近的對話原文。）

## truncated

... (truncated — result was {{chars}} chars, capped at {{cap}}. Narrow the query: shorter time range, fewer entries, or a more specific filter.)

## dropped_result

... (older tool result dropped to fit the context window; re-run the query if you still need it)
