"""Load voice model prompts from ``prompts.md``.

The prompts live in a markdown file next to this module so that a reader of the repo can
find and edit them without reading Python. Each ``## name`` heading starts a prompt.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_FILE = Path(__file__).with_name("prompts.md")


@lru_cache(maxsize=1)
def _parse() -> dict[str, str]:
    """Split the file on ``## `` headings. Text before the first heading is ignored."""
    sections = re.split(r"^## (\S+)\s*$", PROMPTS_FILE.read_text(), flags=re.MULTILINE)
    # re.split gives [preamble, name1, body1, name2, body2, ...]
    return {name: body.strip() for name, body in zip(sections[1::2], sections[2::2])}


def load_prompt(name: str) -> str:
    """Return the prompt called ``name`` from ``prompts.md``."""
    prompts = _parse()
    if name not in prompts:
        raise KeyError(f"No prompt named {name!r} in {PROMPTS_FILE.name}. Found: {sorted(prompts)}")
    return prompts[name]
