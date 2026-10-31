# Consent — runtime/consent.py: what a rule or fact write gets back before the user agreed

Sections are read by name — `prompts.text("runtime/consent#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## ask_first

還沒寫入：{{what}}會改變之後每一輪的行為，要先經使用者同意。用 `ask_user` 把全文給使用者確認（options：寫入／不要），使用者同意後再呼叫一次。
