"""KRX Open API bulk closes (optional; enabled only when KRX_API_KEY is set).

``stk_bydd_trd.TDD_CLSPRC`` is the official KRX regular-session close.
"""
from __future__ import annotations

from datetime import date

from providers.base import OK, STALE_SOURCE, FieldValue, PriceObservation, positive

SOURCE = "KRX 공식"


def closes(client, day: date) -> dict[str, float]:
    values = {}
    for row in client.rows("stk_bydd_trd", day):
        raw = str(row.get("ISU_CD") or row.get("ISU_SRT_CD") or "")
        code = raw[3:9] if len(raw) == 12 else raw
        value = positive(row.get("TDD_CLSPRC"))
        if code and value is not None:
            values[code] = value
    return values


def observations(client, codes: list[str], previous_date: date, session_date: date) -> dict[str, PriceObservation]:
    previous, current = closes(client, previous_date), closes(client, session_date)

    def field(values, code, day):
        if code in values:
            return FieldValue(values[code], day, OK)
        return FieldValue(date=day, status=STALE_SOURCE, detail="KRX 응답에 없음")

    return {code: PriceObservation(code, SOURCE, close=field(current, code, session_date),
                                   previous_close=field(previous, code, previous_date))
            for code in codes}
