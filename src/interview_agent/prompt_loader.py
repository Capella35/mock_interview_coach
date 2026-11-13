"""Load versioned prompt files from `prompts/` (see prompts/README.md).

File format (`prompts/<name>.v<N>.md`):
    # title and notes for reviewers (ignored)
    ## System
    ...system text...
    ## User
    ...user template with {{variables}}...

Rendering substitutes `{{name}}` in ONE pass over the template, so text that is
inserted (e.g. a document containing "{{x}}") is never re-interpreted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


class PromptError(Exception):
    pass


@dataclass(frozen=True)
class Prompt:
    prompt_id: str  # "<name>.v<N>" — recorded with every output
    system: str
    user: str

    def render(self, **values: str) -> tuple[str, str]:
        def fill(template: str) -> str:
            def sub(m: re.Match) -> str:
                if m.group(1) not in values:
                    raise PromptError(f"{self.prompt_id}: missing value for {{{{{m.group(1)}}}}}")
                return str(values[m.group(1)])

            return _VAR_RE.sub(sub, template)

        return fill(self.system), fill(self.user)


def load_prompt(name: str, version: int, prompts_dir: Path = PROMPTS_DIR) -> Prompt:
    prompt_id = f"{name}.v{version}"
    path = prompts_dir / f"{prompt_id}.md"
    if not path.is_file():
        raise PromptError(f"prompt file not found: {path.name}")
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^## System[ \t]*\n(.*?)^## User[ \t]*\n(.*)\Z", text, re.MULTILINE | re.DOTALL)
    if not m:
        raise PromptError(f"{path.name}: needs '## System' then '## User' sections")
    system, user = m.group(1).strip(), m.group(2).strip()
    if not system or not user:
        raise PromptError(f"{path.name}: empty System or User section")
    return Prompt(prompt_id=prompt_id, system=system, user=user)
