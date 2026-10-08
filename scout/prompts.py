"""Loading and splitting the Markdown prompt files in prompts/.

Each prompt file has a `## System` section and a `## User` section. The user
section is a `str.format`-style template. Keeping them on disk means the wording
can be tuned (and its effect measured with eval/run_eval.py) without touching code.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .config import Config, ConfigError, load_prompt

_SECTION_RE = re.compile(r"^##\s+(System|User)\s*$", re.MULTILINE | re.IGNORECASE)


def split_prompt(text: str) -> tuple[str, str]:
    """Split a prompt file into (system, user_template)."""
    matches = list(_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[match.group(1).lower()] = text[match.end() : end].strip()
    if "system" not in sections or "user" not in sections:
        raise ConfigError(
            "prompt file must contain a '## System' and a '## User' section"
        )
    return sections["system"], sections["user"]


@lru_cache(maxsize=None)
def _cached(root: str, name: str, text: str) -> tuple[str, str]:
    return split_prompt(text)


def get_prompt(cfg: Config, name: str) -> tuple[str, str]:
    """Return (system, user_template) for prompts/<name>.md."""
    text = load_prompt(cfg, name)
    return _cached(str(cfg.root), name, text)
