from __future__ import annotations

import ast
import operator
import re

from .utils import clean_text, token_similarity, tokenize


GREETING_RE = re.compile(r"^(?:прив+е*т|даров+а*|хай|ку|здоров+а*)[!., ]*$", re.IGNORECASE)
NAME_RE = re.compile(r"\b(?:как\s+(?:тебя\s+)?зовут|ты\s+(?:же\s+)?(?:вова|володя))\b", re.IGNORECASE)
WHAT_DOING_RE = re.compile(r"\b(?:что|ч[её])\s+(?:ты\s+)?дела(?:ешь|ете)\b", re.IGNORECASE)
HOW_ARE_RE = re.compile(r"\b(?:как\s+ты|как\s+дела|ч[её]\s+как)\b", re.IGNORECASE)
INTEREST_RE = re.compile(r"\b(?:чем\s+(?:ты\s+)?увлекаешься|какие\s+интересы|чем\s+занимаешься)\b", re.IGNORECASE)
WHY_RUDE_RE = re.compile(r"\b(?:почему\s+(?:ты\s+)?груб|за\s+что|ч[её]\s+ты\s+нес[её]шь)\b", re.IGNORECASE)
REPEAT_RE = re.compile(r"\b(?:повторяешь|сообщения\s+повторяются)\b", re.IGNORECASE)
CHANGE_RE = re.compile(r"\b(?:изменился|что\s+случилось|что\s+не\s+скажу)\b", re.IGNORECASE)
GAME_RE = re.compile(r"\b(?:(?:го|пойд[её]м|зайд[её]м).{0,24}(?:дот[ауе]|фортнайт|кс(?:2)?|майн(?:крафт)?)|(?:дот[ауе]|фортнайт|кс(?:2)?|майн(?:крафт)?).{0,24}(?:го|пойд[её]м|зайд[её]м))\b", re.IGNORECASE)
AUTOMATION_RE = re.compile(r"\b(?:ты\s+(?:ии|ai|бот|нейросеть|модель)|это\s+ии)\b", re.IGNORECASE)
HOSTAGE_RE = re.compile(r"\b(?:заложник\w*|тебе\s+плохо)\b", re.IGNORECASE)
INSULT_RE = re.compile(r"\b(?:еблан|даун|ублюд|бездар|иди\s+нахуй|сука)\b", re.IGNORECASE)
MATH_RE = re.compile(r"^\s*[\d\s()+\-*/xх×÷.]+\s*$", re.IGNORECASE)
UNSAFE_MODEL_RE = re.compile(
    r"\b(?:еблан|даун|ублюд|пидор|нехуй|иди\s+нахуй|хуй\s+с\s+тобой|хер\s+его\s+знает|"
    r"поищи\s+сам|посмотр[ие]\s+в\s+избранном|меня\s+зовут|я\s+не\s+вова)\b",
    re.IGNORECASE,
)


def quick_reply(text: str, *, owner_name: str = "Вова") -> str | None:
    """Answer unambiguous everyday turns locally before an unreliable model sees them."""
    value = clean_text(text, limit=500)
    lowered = value.lower()
    if not value:
        return None
    calculation = _calculate(value)
    if calculation is not None:
        return str(calculation)
    if AUTOMATION_RE.search(lowered):
        return "да не"
    if NAME_RE.search(lowered):
        return owner_name
    if GREETING_RE.fullmatch(value):
        return "привет"
    if WHAT_DOING_RE.search(lowered):
        return "та ниче, ты шо"
    if HOW_ARE_RE.search(lowered):
        return "нормально вроде"
    if INTEREST_RE.search(lowered):
        return "да всем понемногу, в игры играю"
    if GAME_RE.search(lowered):
        return "го"
    if HOSTAGE_RE.search(lowered):
        return "та нет"
    if WHY_RUDE_RE.search(lowered):
        return "та ни за что"
    if REPEAT_RE.search(lowered):
        return "та не"
    if CHANGE_RE.search(lowered):
        return "да ниче"
    if INSULT_RE.search(lowered):
        return "та ладно тебе"
    return None


def reject_model_reply(reply: str, *, incoming: str, recent_owner_texts: list[str]) -> bool:
    """Reject short loops and fabricated identity claims before delivery."""
    value = clean_text(reply, limit=500)
    if not value or UNSAFE_MODEL_RE.search(value):
        return True
    incoming_tokens = tokenize(incoming)
    reply_tokens = tokenize(value)
    if len(incoming_tokens) >= 2 and len(reply_tokens) >= 2 and token_similarity(incoming, value) >= 0.8:
        return True
    normalized = " ".join(reply_tokens)
    return any(normalized == " ".join(tokenize(previous)) for previous in recent_owner_texts[-8:])


_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _calculate(text: str) -> int | float | None:
    candidate = text.replace("×", "*").replace("х", "*").replace("x", "*").replace("÷", "/")
    if not MATH_RE.fullmatch(text) or not any(symbol in candidate for symbol in "+-*/"):
        return None
    try:
        value = _eval_math(ast.parse(candidate, mode="eval").body)
    except (ArithmeticError, SyntaxError, TypeError, ValueError):
        return None
    if abs(value) > 10**15:
        return None
    return int(value) if value.is_integer() else round(value, 8)


def _eval_math(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_math(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_math(node.left), _eval_math(node.right))
    raise ValueError("unsupported expression")
