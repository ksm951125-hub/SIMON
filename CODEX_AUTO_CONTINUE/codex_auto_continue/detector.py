"""사용량 제한 안내문 감지 + 초기화 시각 추출."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence

from .timeparse import ResetInfo, parse_reset_time

LIMIT_PATTERNS = [
    r"사용량[을를]?\s*(?:모두|전부)\s*(?:사용|소진)",
    r"사용량\s*(?:한도|제한)",
    r"요청\s*한도",
    r"한도[에를]?\s*(?:도달|초과|소진)",
    r"(?:usage|rate|request|message)\s+limit",
    r"(?:used|reached|hit)\s+(?:all\s+)?(?:of\s+)?your\b.{0,40}\b(?:usage|limit|quota)",
    r"out\s+of\s+(?:usage|credits|requests)",
    r"limit\s+(?:will\s+)?reset",
]
RESET_WORDS = r"재설정|초기화|리셋|다시\s*사용|\breset|\brenew|available\s+again|try\s+again"

_WS = re.compile(r"\s+")


@dataclass
class LimitDetection:
    message: str                    # 감지된 안내문(로그용)
    reset: Optional[ResetInfo]      # 초기화 시각(읽지 못하면 None)


def detect_limit(fragments: Iterable[str], now: datetime, *,
                 past_grace: timedelta = timedelta(hours=3),
                 max_len: int = 400,
                 extra_patterns: Sequence[str] = ()) -> Optional[LimitDetection]:
    """텍스트 조각들에서 사용량 제한 안내를 찾는다.

    - 긴 본문(대화 내용)은 max_len 으로 제외해 오탐을 줄인다.
    - 시각은 '재설정/초기화' 같은 단어가 있는 문장에서만 읽는다(다른 시각 오인 방지).
    - 안내문이 여러 요소로 쪼개져 있어도 인접 조각을 합쳐서 본다.
    """
    frags: List[str] = [_WS.sub(" ", f).strip() for f in fragments if f and f.strip()]
    limit_re = re.compile("|".join(list(LIMIT_PATTERNS) + list(extra_patterns)), re.I)
    reset_re = re.compile(RESET_WORDS, re.I)

    first_without_reset: Optional[LimitDetection] = None
    for i in range(len(frags) - 1, -1, -1):        # 아래쪽(최신)부터
        f = frags[i]
        if len(f) > max_len or not limit_re.search(f):
            continue
        ctx = [c for c in frags[max(0, i - 1): i + 3] if len(c) <= max_len]
        joined = " ".join(ctx)
        reset: Optional[ResetInfo] = None
        for sent in re.split(r"(?<=[.!?。])\s+", joined):
            if reset_re.search(sent):
                reset = parse_reset_time(sent, now, past_grace)
                if reset:
                    break
        det = LimitDetection(message=joined[:300], reset=reset)
        if reset:
            return det
        first_without_reset = first_without_reset or det
    return first_without_reset
