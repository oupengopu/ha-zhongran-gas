"""数据协调器：定时拉全部接口，并算出各实体需要的派生值。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GasApiError, ZhongranGasApi
from .const import DEFAULT_SCAN_INTERVAL, DEFAULT_TIERS, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


class GasCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """一次刷新把所有接口都拉一遍。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        cfg = {**entry.data, **entry.options}
        self.api = ZhongranGasApi(
            async_get_clientsession(hass),
            access_token=cfg["access_token"],
            user_id=cfg["user_id"],
            cust_code=cfg["cust_code"],
            cust_name=cfg["cust_name"],
            mas=cfg["mas"],
        )
        self.scan_minutes = int(cfg.get("scan_interval", DEFAULT_SCAN_INTERVAL))
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._prev: dict[str, Any] = {}
        self._daylist: list[dict[str, Any]] = []

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=self.scan_minutes),
        )

    # ── 首次加载时恢复上次的快照和日用量列表 ──────────────────
    async def async_load_storage(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._prev = data.get("prev") or {}
            self._daylist = data.get("daylist") or []

    async def _async_save_storage(self) -> None:
        await self._store.async_save(
            {"prev": self._prev, "daylist": self._daylist[-400:]}
        )

    # ── 主刷新 ────────────────────────────────────────────
    async def _async_update_data(self) -> dict[str, Any]:
        try:
            info = await self.api.get_cust_info()
            price = await self.api.get_price_info()
            rows = await self.api.get_monthly_usage()
            stop = await self.api.get_stop_gas()
            pays = await self.api.get_payments()
        except GasApiError as err:
            raise UpdateFailed(str(err)) from err

        tiers = price.get("tiers") or DEFAULT_TIERS
        today = date.today()
        year = today.year

        # 本年度累计 + 近 12 个月合计
        ytd = sum(r["qty"] for r in rows if r["month"].startswith(str(year)))
        m12 = sum(r["qty"] for r in rows)
        ytd = round(ytd, 2)
        m12 = round(m12, 2)

        # 当前档位
        tier_idx = 0
        tier_label = ""
        next_threshold = None
        next_price = None
        names = ["第一档", "第二档", "第三档", "第四档", "第五档"]
        for i, t in enumerate(tiers):
            if t["from"] <= ytd <= t["to"]:
                tier_idx = i
                tier_label = names[i] if i < len(names) else f"第{i + 1}档"
                if i + 1 < len(tiers):
                    next_threshold = tiers[i + 1]["from"]
                    next_price = tiers[i + 1]["price"]
                break
        active_price = tiers[tier_idx]["price"] if tiers else None

        # 剩余气量
        balance_num = _to_float(info.get("balance"))
        remain_qty = None
        if balance_num is not None and active_price:
            remain_qty = round(balance_num / active_price, 2)

        # 读数滞后天数
        age_days = None
        rt = info.get("reading_time") or ""
        if len(rt) >= 10:
            try:
                age_days = (today - datetime.strptime(rt[:10], "%Y-%m-%d").date()).days
            except ValueError:
                age_days = None

        # ── 日用量（用余额差值反推）──────────────────────────
        daily_usage = None
        daily_basis = ""
        today_key = today.isoformat()
        prev = self._prev
        if prev.get("balance") is not None and prev.get("date") != today_key:
            delta = float(prev["balance"]) - (balance_num or 0)
            if active_price and delta >= 0:
                daily_usage = round(delta / active_price, 3)
                daily_basis = (
                    f"{prev.get('date')} {float(prev['balance']):.2f}元 → "
                    f"{today_key} {(balance_num or 0):.2f}元"
                )
                self._daylist.insert(
                    0,
                    {
                        "day": today_key,
                        "f_gas": daily_usage,
                        "e_gas": round(delta, 2),
                        "tier": tier_idx + 1,
                        "balance": balance_num,
                    },
                )
            else:
                daily_basis = "余额上升（有缴费）或数据异常，本次不计算"
        elif not prev:
            daily_basis = f"首次运行，{today_key} 之后才有日用量"

        self._prev = {"balance": balance_num, "date": today_key}

        # ── 缴费 ─────────────────────────────────────────────
        this_month = today.strftime("%Y%m")
        pay_total = round(sum(p["amount"] for p in pays), 2)
        pay_month = round(
            sum(p["amount"] for p in pays if p["month"] == this_month), 2
        )
        latest_pay = pays[0] if pays else None
        pay_detail = "\n".join(
            f"{p['month']} {p['amount']:.2f}元 {p['paymentno']}" for p in pays
        )

        # ── 给 electricity-info-card 用的结构 ────────────────
        latest_row = rows[0] if rows else None
        avg_month = round(m12 / len(rows), 2) if rows else 0
        avg_daily_cost = round(avg_month * (active_price or 0) / 30.44, 2) if avg_month else 0
        remain_days = (
            int(balance_num / avg_daily_cost)
            if (balance_num is not None and avg_daily_cost > 0)
            else None
        )

        month_list = [
            {
                "month": f"{r['month'][:4]}-{r['month'][4:6]}",
                "monthEleNum": r["qty"],
                "monthEleCost": round(r["qty"] * (active_price or 0), 2),
            }
            for r in rows
        ]
        # daylist：先用月度数据均摊成每日做种子，之后每天的真实值会覆盖
        if not self._daylist and rows:
            seed: list[dict[str, Any]] = []
            for r in sorted(rows, key=lambda x: x["month"]):
                y, m = int(r["month"][:4]), int(r["month"][4:6])
                days = (date(y + (m == 12), (m % 12) + 1, 1) - date(y, m, 1)).days
                for d in range(1, days + 1):
                    seed.append(
                        {
                            "day": f"{y}-{m:02d}-{d:02d}",
                            "f_gas": round(r["qty"] / days, 3),
                            "e_gas": round(r["qty"] * (active_price or 0) / days, 2),
                            "tier": tier_idx + 1,
                            "balance": balance_num,
                        }
                    )
            seed.reverse()
            self._daylist = seed[-400:]

        tier_info = {
            "计费标准": "年阶梯",
            "当前年阶梯档": f"第{tier_idx + 1}档",
            "年阶梯累计用气量": ytd,
            "年阶梯第1档起始用气量": tiers[0]["from"] if len(tiers) > 0 else 0,
            "年阶梯第2档起始用气量": tiers[1]["from"] if len(tiers) > 1 else None,
            "年阶梯第3档起始用气量": tiers[2]["from"] if len(tiers) > 2 else None,
            "年阶梯第1档气价": tiers[0]["price"] if len(tiers) > 0 else None,
            "年阶梯第2档气价": tiers[1]["price"] if len(tiers) > 1 else None,
            "年阶梯第3档气价": tiers[2]["price"] if len(tiers) > 2 else None,
            "当前年阶梯起始日期": f"{year}.01.01",
            "当前年阶梯结束日期": f"{year}.12.31",
        }

        await self._async_save_storage()

        return {
            # 基础
            "balance": balance_num,
            "owe": info.get("owe"),
            "reading": info.get("reading"),
            "reading_time": info.get("reading_time"),
            "meter_no": info.get("meter_no"),
            "reading_age": age_days,
            # 气价
            "price": active_price,
            "tiers": tiers,
            "tier_index": tier_idx,
            "tier_label": tier_label,
            "next_threshold": next_threshold,
            "next_price": next_price,
            "tier_remain": (
                round(next_threshold - ytd, 2) if next_threshold is not None else None
            ),
            # 用气
            "month_usage": latest_row["qty"] if latest_row else None,
            "month_record": latest_row["month"] if latest_row else None,
            "month_read_date": latest_row["read_date"] if latest_row else None,
            "ytd": ytd,
            "usage_12m": m12,
            "remain_qty": remain_qty,
            "daily_usage": daily_usage,
            "daily_basis": daily_basis,
            "rows": rows,
            # 停气
            "has_stop": stop["has_stop"],
            "stop_detail": stop["detail"],
            # 缴费
            "pay_latest": latest_pay["amount"] if latest_pay else None,
            "pay_latest_month": latest_pay["month"] if latest_pay else None,
            "pay_latest_no": latest_pay["paymentno"] if latest_pay else None,
            "pay_month_total": pay_month,
            "pay_count": len(pays),
            "pay_total": pay_total,
            "pay_detail": pay_detail,
            # 卡片
            "tier_info": tier_info,
            "month_list": month_list,
            "day_list": self._daylist,
            "year_list": [
                {
                    "year": str(year),
                    "yearEleNum": ytd,
                    "yearEleCost": round(ytd * (active_price or 0), 2),
                }
            ],
            "avg_daily_cost": avg_daily_cost,
            "remain_days": remain_days,
            "sync_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
