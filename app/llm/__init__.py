"""表达层：大模型只写人话，不写数字。"""

from app.llm.explainer import Explainer, find_hallucinations, allowed_numbers

__all__ = ["Explainer", "find_hallucinations", "allowed_numbers"]
