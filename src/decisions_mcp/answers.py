"""Validate core answer fields before caching an API response."""

import math

from .models import ChoiceQuestion, PredicateQuestion, ScoreQuestion


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Expected a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Number outside range")


def validate_response(payload, questions):
    if not isinstance(payload, dict):
        raise TypeError("Expected an object")
    answers = payload.get("answers")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError("Answer count differs")
    for question, answer in zip(questions, answers):
        if not isinstance(answer, dict) or answer.get("name") != question.name:
            raise ValueError("Answer name differs")
        if answer.get("type") == "refusal":
            continue
        if answer.get("type") != question.type:
            raise ValueError("Answer type differs")
        if isinstance(question, PredicateQuestion):
            number(answer.get("probability"), 0, 1)
            continue
        number(answer.get("confidence"), 0, 1)
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, list):
            raise TypeError("Expected probabilities")
        if isinstance(question, ChoiceQuestion):
            allowed = {(type(option.value), option.value) for option in question.choices}
            selected = answer.get("choice")
            if (type(selected), selected) not in allowed:
                raise ValueError("Unexpected choice")
            for item in probabilities:
                if not isinstance(item, dict):
                    raise TypeError("Invalid probability entry")
                value = item.get("value")
                if (type(value), value) not in allowed:
                    raise ValueError("Unexpected choice probability")
                number(item.get("probability"), 0, 1)
        elif isinstance(question, ScoreQuestion):
            number(answer.get("score"), 0, len(question.levels) - 1)
            for item in probabilities:
                if not isinstance(item, dict):
                    raise TypeError("Invalid probability entry")
                value = item.get("value")
                if type(value) is not int or not 0 <= value < len(question.levels):
                    raise ValueError("Unexpected score level")
                if item.get("label") != question.levels[value].label:
                    raise ValueError("Unexpected score label")
                number(item.get("probability"), 0, 1)
