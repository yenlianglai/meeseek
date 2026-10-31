# Replay stubs — runtime/replays.py, the answers a replayed write gets

Sections are read by name — `prompts.text("runtime/replay#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## tool_error

工具呼叫失敗。如果是參數或服務問題，調整參數或改用其他工具。同一個呼叫最多重試一次。
