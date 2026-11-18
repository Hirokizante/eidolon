"""Conservative work estimates within an unchanged provider response allowance.

Token counts are estimates across compatible tokenizers. Leave a response margin,
reserve JSON/reference bookkeeping per operation, and share the remaining prose
allowance across the entire proposal. Reasoning still shares the provider cap.
"""
from dataclasses import asdict, dataclass

from .continuity import ContinuityValidationError


@dataclass(frozen=True)
class ResponseLimits:
    max_operations: int
    max_content_chars: int
    max_decision_chars: int
    max_total_text_chars: int
    response_tokens: int

    @classmethod
    def for_task(cls, task_id: str, tokens: int, chars: int) -> "ResponseLimits":
        if task_id.startswith("repair:"):
            ceiling, overhead, minimum_prose = 1, 256, 64
        elif task_id.startswith("review:"):
            ceiling, overhead, minimum_prose = 24, 64, 64
        elif task_id.startswith("narrative:"):
            ceiling, overhead, minimum_prose = 4, 96, 192
        else:
            ceiling, overhead, minimum_prose = 24, 192, 256
        usable = min(tokens - max(32, (tokens + 9) // 10), chars - 32)
        count = min(ceiling, usable // (overhead + minimum_prose))
        if count < 1:
            raise ContinuityValidationError("No room for a substantive operation",
                                            code="output_budget")
        prose = usable - count * overhead
        return cls(count, min(1200, prose), min(600, prose), prose, usable)

    def document(self) -> dict[str, int]:
        return asdict(self)
