"""Stage D: versioned system prompts for the engine context.

`engine.rca_prompt/v1` keeps the product v3 rules and replaces the embedded JSON Schema
with the short field list; schema enforcement stays in the validator.
"""

from __future__ import annotations

from cloudops.features.diagnosis.context import SYSTEM_PROMPT
from cloudops.features.diagnosis.engine.selection import FIELD_LIST

PROMPT_V1 = "engine.rca_prompt/v1"
PROMPTS: dict[str, str] = {
    PROMPT_V1: SYSTEM_PROMPT + " " + FIELD_LIST,
}
