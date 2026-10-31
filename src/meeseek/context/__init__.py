"""Context layer — what the model sees before the loop starts: SOP templates
rendered with project placeholders (prompts) and long-term memory (memory)."""

from meeseek.context.prompts import render, text
from meeseek.context.memory import memory_instruction

__all__ = ["render", "text", "memory_instruction"]
