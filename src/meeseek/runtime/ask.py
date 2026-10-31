"""Asking the user is a tool call.

`ask_user` is long-running: it pauses the turn (the App is resumable), and the
user's answer comes back as that call's function response, resuming the turn
where it stopped. Every surface reads the open question from the session and
turns the user's next message into the answer.

Only agents with a person on the other end get it: a headless turn that asked
would wait forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TOOL_NAME = "ask_user"


def ask_user(question: str, options: list[str] | None = None, recommended: str = "") -> dict:
    """問使用者一個問題，暫停這一輪；他的回答會回到這一輪，你從原地接著做。

    用在：決策屬於使用者（多種合理做法、範圍或驗收標準不明），或寫入規則、記憶、
    外部系統前需要同意。事實自己查，不要問。不要只在回覆文字裡提問就結束這一輪。
    一次只問一題；附上你的建議答案，讓對方可以直接說好。
    呼叫後用一句話說明你在等什麼就停，不要再寫一份問題清單。

    Args:
        question: 要問的一個問題
        options: 可選的答案（2-4 個）；開放式問題可省略
        recommended: 你建議的答案與一句理由
    """
    return {"status": "waiting for the user's answer"}


@dataclass(slots=True)
class Question:
    call_id: str
    invocation_id: str
    question: str
    options: list[str] = field(default_factory=list)
    recommended: str = ""

    def as_text(self) -> str:
        lines = [self.question]
        if self.options:
            lines.append("選項：" + " / ".join(self.options))
        if self.recommended:
            lines.append(f"建議：{self.recommended}")
        return "\n".join(lines)


