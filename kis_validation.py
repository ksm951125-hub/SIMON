"""Optional independent validation; never a replacement price source."""
from __future__ import annotations
import os
import threading
import time

import requests

from krx import number

BASE_URL = "https://openapi.koreainvestment.com:9443"


class KisValidator:
    def __init__(self):
        self.key, self.secret = os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", "")
        if not self.key or not self.secret:
            raise RuntimeError("KIS validation enabled but KIS_APP_KEY/KIS_APP_SECRET missing")
        try:
            response = requests.post(BASE_URL + "/oauth2/tokenP",
                json={"grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret},
                timeout=(5, 20), allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError(f"KIS token HTTP {response.status_code}")
            self.token = response.json().get("access_token")
        except (requests.RequestException, ValueError):
            raise RuntimeError("KIS token request failed") from None
        if not self.token:
            raise RuntimeError("KIS token missing")
        self.lock = threading.Lock()
        self.last_request = 0.0

    def validate(self, code, previous, current, previous_close, close):
        with self.lock:
            wait = 0.12 - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
        try:
            response = requests.get(BASE_URL + "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                headers={"authorization": "Bearer " + self.token, "appkey": self.key,
                         "appsecret": self.secret, "tr_id": "FHKST03010100", "custtype": "P"},
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                        "FID_INPUT_DATE_1": previous.strftime("%Y%m%d"),
                        "FID_INPUT_DATE_2": current.strftime("%Y%m%d"),
                        "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"},
                timeout=(5, 15), allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError(f"KIS HTTP {response.status_code}")
            payload = response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError("KIS daily validation request failed") from None
        if not isinstance(payload, dict) or payload.get("rt_cd") != "0" or not isinstance(payload.get("output2"), list):
            raise ValueError("KIS daily response error/schema mismatch")
        values = {}
        for row in payload["output2"]:
            day = row.get("stck_bsop_date")
            if day in values:
                raise ValueError("KIS duplicate session")
            values[day] = number(row.get("stck_clpr"), "KIS raw close", positive=True)
        for day, expected in [(previous, previous_close), (current, close)]:
            actual = values.get(day.strftime("%Y%m%d"))
            if actual is None or actual != number(expected, "KRX close", positive=True):
                raise ValueError(f"{code} {day}: KRX close={expected}, KIS(J) close={actual}; excluded")


def optional_validator():
    setting = os.getenv("KIS_VALIDATE", "0").strip() or "0"
    if setting not in {"0", "1"}:
        raise ValueError("KIS_VALIDATE must be 0 or 1")
    return KisValidator() if setting == "1" else None
