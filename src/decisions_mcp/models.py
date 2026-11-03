"""Typed inputs shared by MCP tools and the reusable API client."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictStr, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextEvidence(Model):
    type: Literal["text"]
    text: str


class JsonEvidence(Model):
    type: Literal["json"]
    data: dict[str, JsonValue] | list[dict[str, JsonValue]]

    @model_validator(mode="after")
    def within_limits(self):
        encoded = json.dumps(
            self.data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if len(encoded) > 256 * 1024:
            raise ValueError("JSON evidence exceeds 256 KiB")
        count = 0

        def visit(value, depth=0):
            nonlocal count
            count += 1
            if count > 10_000:
                raise ValueError("JSON evidence exceeds 10,000 values")
            if isinstance(value, (dict, list)):
                depth += 1
                if depth > 32:
                    raise ValueError("JSON evidence exceeds 32 container levels")
                for child in value.values() if isinstance(value, dict) else value:
                    visit(child, depth)

        visit(self.data)
        return self


Detail = Literal["auto", "low", "high", "original"]


class ImageUrlEvidence(Model):
    type: Literal["image_url"]
    url: str = Field(pattern=r"^(https?://|data:image/)")
    detail: Detail = "auto"


class ImagePathEvidence(Model):
    type: Literal["image_path"]
    path: str = Field(min_length=1)
    detail: Detail = "auto"


class TextPathEvidence(Model):
    type: Literal["text_path"]
    path: str = Field(min_length=1)


Evidence = Annotated[
    TextEvidence | JsonEvidence | ImageUrlEvidence | ImagePathEvidence | TextPathEvidence,
    Field(discriminator="type"),
]


class ChoiceOption(Model):
    value: StrictStr | StrictBool
    description: str | None = None


class ScoreLevel(Model):
    label: str
    description: str | None = None


class QuestionBase(Model):
    name: str = Field(min_length=1, max_length=1_048_576)
    instructions: str = Field(min_length=1, max_length=1_048_576)


class PredicateQuestion(QuestionBase):
    type: Literal["predicate"]


class ChoiceQuestion(QuestionBase):
    type: Literal["choice"]
    choices: list[ChoiceOption] = Field(min_length=2, max_length=255)

    @model_validator(mode="after")
    def distinct_choices(self):
        keys = [(type(option.value), option.value) for option in self.choices]
        if len(set(keys)) != len(keys):
            raise ValueError("Choice values must be unique")
        return self


class ScoreQuestion(QuestionBase):
    type: Literal["score"]
    levels: list[ScoreLevel] = Field(min_length=2, max_length=10)


Question = Annotated[
    PredicateQuestion | ChoiceQuestion | ScoreQuestion,
    Field(discriminator="type"),
]


class EvaluateRequest(Model):
    evidence: list[Evidence] = Field(min_length=1)
    questions: list[Question] = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def distinct_names(self):
        names = [q.name for q in self.questions]
        if len(set(names)) != len(names):
            raise ValueError("Question names must be unique")
        return self
