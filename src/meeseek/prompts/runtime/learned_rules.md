# Learned-rule refusals — runtime/learned_rules.py: why propose_sop_rule did not write, and what to do instead

Sections are read by name — `prompts.text("runtime/learned_rules#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## rejected

規則未通過品質檢查：{{reason}}

## duplicate

這條規則已經有了（相似度 ≥ {{threshold}}）：「{{same}}」——不用再寫一次

## at_capacity

'{{skill}}' 的學習規則已達上限 {{max_rules}} 條，沒有寫入。現有規則：
{{rules}}
先把這份清單給使用者，問要用 `forget_sop_rule` 移除哪一條，或把新規則併進其中一條；使用者決定後再寫入。
