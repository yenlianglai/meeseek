# Harness messages — runtime/harness.py, the failed-write reconciliation

Sections are read by name — `prompts.text("runtime/harness#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## resumed_write

這個寫入在中斷前已經送出，不知道有沒有生效，所以沒有重送。先用讀取工具確認它是否已生效，再決定要不要重做；回覆時說明結果不明。

## failure_line

- {{tool}}({{target}}) 失敗：{{detail}}

## reconcile

對帳結果：以下寫入沒有成功，但你的回覆沒有提到。
{{failures}}
重寫給使用者的回覆：分開列出成功的與失敗的部分，失敗的寫出原因與建議的下一步。不要說失敗的部分已經完成。

## appended_note

⚠️ 系統紀錄：以下寫入沒有成功——
{{failures}}

## promise

對帳結果：你的回覆承諾了之後會做某件事（「{{promise}}」），但系統沒有任何排程會再喚醒你，這個承諾不會自動發生。
重寫給使用者的回覆：改成現在就能確定的說法——現在就把事做完、請使用者需要時再問，或清楚說明你不會主動回報。不要承諾系統做不到的事。

## promise_note

⚠️ 系統紀錄：上面提到的後續動作沒有對應的排程，不會自動發生；需要結果時請再問一次。
