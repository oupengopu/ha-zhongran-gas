"""中国燃气（中燃在线）接口客户端 —— 纯 Python，无第三方依赖。

移植自 Node-RED 流程（已实测跑通），三套不同签名：
  ① 简单签名:  md5(custCode + SALT + timeStamp)
  ② 参数签名:  md5(按key排序拼接 + "&key=" + SIGN_KEY).upper()
  ③ params 加密其实就是 Base64（不是 AES）
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from .const import (
    APP_KEY,
    ENVIR,
    SIGN_KEY,
    SALT,
    UA,
    URL_CUST_INFO,
    URL_METHOD_ENCRYPT,
    URL_PAYMENT_LOG,
    URL_PRICE_INFO,
    URL_STOP_GAS,
)

_LOGGER = logging.getLogger(__name__)


class GasApiError(Exception):
    """接口异常。"""


def _is_empty_payload(data: Any) -> bool:
    """判断“没有记录”的各种空外壳。

    停气接口用 {"list": []} / {"total": 0} / [{}] 表示无记录时，
    单纯用真值判断会把它当成“有停气”，进而误触发告警自动化。
    """
    if not data:
        return True
    if isinstance(data, list):
        return all(_is_empty_payload(x) for x in data)
    if isinstance(data, dict):
        if not any(v for v in data.values()):
            return True
        # “列表 + 计数”的空外壳：列表存在且为空、计数也没有正值
        for key in ("list", "rows", "records", "data"):
            value = data.get(key)
            if isinstance(value, list) and not value:
                if not any(data.get(k) for k in ("total", "count")):
                    return True
            elif isinstance(value, dict) and _is_empty_payload(value):
                return True
    return False


class ZhongranGasApi:
    """一个用户的接口封装。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        access_token: str,
        user_id: str,
        cust_code: str,
        cust_name: str,
        mas: str,
    ) -> None:
        self._session = session
        self.access_token = access_token
        self.user_id = user_id
        self.cust_code = cust_code
        self.cust_name = cust_name
        self.mas = mas

    # ── 内部工具 ────────────────────────────────────────────
    def _headers(self, form: bool = True) -> dict[str, str]:
        h = {
            "Accept": "*/*",
            "Origin": "https://zrds.95007.com",
            "Referer": "https://zrds.95007.com/controller/phone/uni/index.html",
            "User-Agent": UA,
            "accessFrom": "h5",
            "accessToken": self.access_token,
            "userId": self.user_id,
            "x-mas-app-info": self.mas,
        }
        if form:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        return h

    @staticmethod
    def _ts() -> str:
        return str(int(time.time() * 1000))

    def _simple_sign(self, seed: str, ts: str) -> str:
        return hashlib.md5(f"{seed}{SALT}{ts}".encode()).hexdigest()

    async def _post_form(self, url: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(
                url,
                data=data,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GasApiError(f"网络错误: {err}") from err

        if status >= 400:
            # 网关错误页通常是 HTML，直接解析只会得到“返回不是 JSON”，
            # 真正的状态码信息就丢了，排查时看不出是 502 还是 401
            raise GasApiError(f"HTTP {status}: {text[:200]}")

        try:
            return json.loads(text)
        except (ValueError, TypeError) as err:
            raise GasApiError(f"返回不是 JSON: {text[:200]}") from err

    # ── ① 账户信息 ───────────────────────────────────────────
    async def get_cust_info(self) -> dict[str, Any]:
        ts = self._ts()
        data = {
            "custCode": self.cust_code,
            "custName": self.cust_name,
            "timeStamp": ts,
            "signature": self._simple_sign(self.cust_code, ts),
        }
        j = await self._post_form(URL_CUST_INFO, data)
        if str(j.get("status")) != "1" or not isinstance(j.get("data"), dict):
            raise GasApiError(f"账户查询失败: {j.get('message')}")
        d = j["data"]
        return {
            "balance": d.get("newCountMoney"),
            "owe": d.get("oweMoney") or "0",
            "reading": d.get("lastRecord"),
            "reading_time": d.get("lastRecordTime"),
            "meter_no": d.get("meterNo"),
            "cust_code": d.get("custCode", self.cust_code),
        }

    # ── ② 阶梯气价 ───────────────────────────────────────────
    async def get_price_info(self) -> dict[str, Any]:
        ts = self._ts()
        data = {
            "custCode": self.cust_code,
            "timeStamp": ts,
            "signature": self._simple_sign(self.cust_code, ts),
        }
        try:
            j = await self._post_form(URL_PRICE_INFO, data)
        except GasApiError as err:
            _LOGGER.warning("阶梯气价查询失败，用默认值: %s", err)
            return {"price": None, "tiers": []}

        tiers: list[dict[str, Any]] = []
        price = None
        if str(j.get("status")) == "1" and isinstance(j.get("data"), list) and j["data"]:
            for x in j["data"]:
                try:
                    tiers.append(
                        {
                            "from": int(float(x.get("fromCal", 0))),
                            "to": int(float(x.get("endCal", 999999))),
                            "price": float(x.get("gasPrice", 0)),
                        }
                    )
                except (TypeError, ValueError):
                    continue
            # 不依赖服务端顺序：统一按起始气量升序，下游按顺序匹配档位
            tiers.sort(key=lambda t: t["from"])
            if tiers:
                price = tiers[0]["price"]
        return {"price": price, "tiers": tiers}

    # ── ③ 月度用气（methodEncrypt.do）────────────────────────
    async def get_monthly_usage(
        self, months: int = 12, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        # 用调用方传入的时间（HA 配置时区），避免容器是 UTC 时
        # 月初/跨日的查询窗口整体偏移
        now = now or datetime.now()
        end_m = now.strftime("%Y%m")
        start_m = (now - timedelta(days=365)).strftime("%Y%m")

        params = {
            "appKey": APP_KEY,
            "custCode": self.cust_code,
            "endTime": end_m,
            "envir": ENVIR,
            "idNo": self.cust_name,
            "nonce": str(random.random()).split(".")[-1] or "123456",
            "startTime": start_m,
            "timeStamp": int(time.time() * 1000),
        }

        # 按 key 排序拼 query 算签名
        keys = sorted(params)
        query = "&".join(f"{k}={params[k]}" for k in keys)
        sign = hashlib.md5(f"{query}&key={SIGN_KEY}".encode()).hexdigest().upper()

        signed = {k: params[k] for k in keys}
        signed["sign"] = sign

        payload = base64.b64encode(
            json.dumps(signed, ensure_ascii=False).encode("utf-8")
        ).decode()

        body = {
            "method": "getcustomerMoneyListForMpOl",
            "params": payload,
        }

        j = await self._post_form(URL_METHOD_ENCRYPT, body)
        if str(j.get("status")) != "1":
            raise GasApiError(f"月度用气查询失败: {j.get('message')}")

        data = j.get("data") or {}
        lst = data.get("payGasCheckList") or []
        rows: list[dict[str, Any]] = []
        for x in lst:
            try:
                month = str(x.get("recordMonth") or "")
                # 下游按 YYYYMM 切片/比较，格式不符的记录直接丢弃，
                # 避免 seed 里 int() 抛异常拖垮整个刷新
                if (
                    len(month) != 6
                    or not month.isdigit()
                    or not 1 <= int(month[4:6]) <= 12
                ):
                    _LOGGER.warning("忽略非法抄表月份: %r", month)
                    continue
                rows.append(
                    {
                        "month": month,
                        "prev": float(x.get("lastRecord", 0)),
                        "cur": float(x.get("curRecord", 0)),
                        "qty": float(x.get("curQty", 0)),
                        "read_date": str(x.get("timeCurRecord") or ""),
                    }
                )
            except (TypeError, ValueError):
                continue
        # 不依赖服务端返回顺序：最新的月份永远在 rows[0]
        rows.sort(key=lambda r: r["month"], reverse=True)
        return rows

    # ── ④ 停气通知 ───────────────────────────────────────────
    async def get_stop_gas(self) -> dict[str, Any]:
        ts = self._ts()
        data = {
            "custCode": self.cust_code,
            "timeStamp": ts,
            "signature": self._simple_sign(self.cust_code, ts),
        }
        try:
            j = await self._post_form(URL_STOP_GAS, data)
        except GasApiError as err:
            _LOGGER.warning("停气查询失败: %s", err)
            return {"has_stop": False, "detail": "查询失败"}

        if str(j.get("status")) != "1":
            return {"has_stop": False, "detail": j.get("message") or "无"}
        data = j.get("data")
        if _is_empty_payload(data):
            # {"list": []} / {"total": 0} / [{}] 这类“空外壳”代表没有停气，
            # 按真值判断会误报，进而误触发用户告警自动化
            return {"has_stop": False, "detail": j.get("message") or "无"}
        return {
            "has_stop": True,
            "detail": json.dumps(data, ensure_ascii=False)[:900],
        }

    # ── ⑤ 缴费明细 ───────────────────────────────────────────
    async def get_payments(
        self, days: int = 181, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        now = now or datetime.now()
        end = now.strftime("%Y%m%d")
        start = (now - timedelta(days=days)).strftime("%Y%m%d")
        ts = self._ts()
        data = {
            "custCode": self.cust_code,
            "startTime": start,
            "endTime": end,
            "timeStamp": ts,
            "signature": self._simple_sign(self.cust_code, ts),
        }
        # 缴费失败必须让调用方知道：否则会被当成“没有缴费”，
        # 进而把余额上升误判为异常并重置余额基准，永久丢掉当天用量
        j = await self._post_form(URL_PAYMENT_LOG, data)
        if str(j.get("status")) != "1":
            raise GasApiError(f"缴费记录查询失败: {j.get('message')}")

        rows: list[dict[str, Any]] = []
        if isinstance(j.get("data"), list):
            for x in j["data"]:
                try:
                    month_raw = str(x.get("recordmonth") or "")
                    # 归一化成 YYYYMM（兼容 2026-11 之类格式），
                    # 供 coordinator 与当月比较做缴费冲正
                    digits = "".join(c for c in month_raw if c.isdigit())
                    month = digits[:6] if len(digits) >= 6 else month_raw
                    rows.append(
                        {
                            "month": month,
                            "amount": float(x.get("amount") or 0),
                            "paymentno": str(x.get("paymentno") or ""),
                        }
                    )
                except (TypeError, ValueError):
                    continue
        # 稳定排序：保证最近月份在前；同月内保持服务端原顺序
        # （接口没有缴费日字段，同月多笔无法进一步区分先后）
        rows.sort(key=lambda r: r["month"], reverse=True)
        return rows
