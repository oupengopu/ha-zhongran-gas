"""数据协调器：定时拉全部接口，并算出各实体需要的派生值。"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GasApiError, ZhongranGasApi
from .const import DEFAULT_SCAN_INTERVAL, DEFAULT_TIERS, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SETTLEMENT_HOUR = 8  # 云端通常每天 08:00 左右结算前一天用量
ZERO_CONFIRM_HOUR = 12  # 到这时余额仍未变化，才判定前一天为真实零用量
MAX_BACKFILL_DAYS = 120  # 超过这么多天不再均摊回填，直接重建基准
MAX_OVERPAY_RATIO = 3.0  # 缴费额超过缺口这么多倍就判为错配，不做冲正


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
        self._daily: dict[str, Any] = {}  # 当天已结算的日用量缓存
        self._rows: list[dict[str, Any]] = []  # 最近一次成功的月度用气数据
        # 已经用于缴费冲正的缴费单号：同月多笔缴费时，同一个窗口会反复
        # 看到同一批记录，不记住就会把同一笔钱重复冲正，算出成倍的用量
        self._used_pays: list[str] = []

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
            # 存储可能被手工改坏或来自不兼容的旧版本：逐项校验类型，
            # 任何异常字段都退回空值，避免启动时抛错或后续 .get 崩掉
            prev = data.get("prev")
            self._prev = prev if isinstance(prev, dict) else {}
            daylist = data.get("daylist")
            # day 必须是合法日期且 f_gas 是数字：否则 _upsert_days 的排序会
            # 拿 int 和 str 比较抛 TypeError，而异常发生在落盘之前，
            # 每次刷新都在同一处崩，集成会一直 unavailable，只能手工删存储
            self._daylist = _clean_daylist(daylist)
            daily = data.get("daily")
            self._daily = daily if isinstance(daily, dict) else {}
            rows = data.get("rows")
            # 存储可能来自不兼容的旧版本或被手工改坏：缺 qty 会让
            # sum(r["qty"]) 抛 KeyError，month 是 int 会让 .startswith 崩，
            # 两者都会把整次刷新打成失败（整屏 unavailable）
            self._rows = _clean_rows(rows)
            used = data.get("used_pays")
            self._used_pays = (
                [k for k in used if isinstance(k, str) and k]
                if isinstance(used, list)
                else []
            )
        else:
            self._prev = {}
            self._daylist = []
            self._daily = {}
            self._rows = []
            self._used_pays = []
        # 旧版记录没有来源且日期可能偏移一天：不修改历史日期，只标记为 legacy，
        # 这样它不会被新的“严格昨日”实体当成真实单日结算。
        for row in self._daylist:
            row.setdefault("source", "legacy")
        # 旧版 _daily 缓存同样没有来源，不能被当成新口径的真实值
        if self._daily and not self._daily.get("source"):
            self._daily["source"] = "legacy"

    async def _async_save_storage(self) -> None:
        # daylist 为降序：内存与持久化都只保留最新 400 条，
        # 避免实时属性体积随运行天数无限增长
        self._daylist = self._daylist[:400]
        # 已冲正缴费只需覆盖缴费接口的 181 天窗口，留 80 条足够
        self._used_pays = self._used_pays[-80:]
        await self._store.async_save(
            {
                "prev": self._prev,
                "daylist": self._daylist,
                "daily": self._daily,
                # 接口故障重启后仍能算出档位和 ytd，不至于整屏空白
                "rows": self._rows,
                "used_pays": self._used_pays,
            }
        )

    def _remember_pays(self, pays: list[dict[str, Any]]) -> None:
        """记下已经冲正过的缴费，避免同一笔钱被重复冲正。

        缴费接口只有月份没有缴费日，同月多笔时窗口里永远是同一批记录，
        下一次结算会再次选中它们，导致用量被重复扣减（实测 20.8 方 vs 真实 0.8 方）。
        """
        keys = list(self._used_pays)
        for p in pays:
            key = _pay_key(p)
            if key not in keys:
                keys.append(key)
        self._used_pays = keys[-80:]

    # ── 主刷新 ────────────────────────────────────────────
    async def _async_update_data(self) -> dict[str, Any]:
        # 时区统一用 HA 配置的时区：容器常是 UTC，直接用 datetime.now()
        # 会让月初/跨日的查询窗口和账期判断整体错位 8 小时
        now = dt_util.now()
        today = now.date()
        today_key = today.isoformat()
        this_month = today.strftime("%Y%m")
        now_iso = now.isoformat(timespec="seconds")

        # 只有账户信息是必需的：拿不到余额，其他派生值全都没有意义。
        try:
            info = await self.api.get_cust_info()
        except GasApiError as err:
            raise UpdateFailed(str(err)) from err

        # 气价 / 停气 / 月度用气都是辅助数据，任何一个失败都不该让整个
        # 集成变成 unavailable（连余额都看不到），各自降级并记警告。
        try:
            price = await self.api.get_price_info()
        except GasApiError as err:
            _LOGGER.warning("阶梯气价查询失败，本次回退默认阶梯: %s", err)
            price = {"price": None, "tiers": []}

        try:
            stop = await self.api.get_stop_gas()
        except GasApiError as err:
            _LOGGER.warning("停气查询失败: %s", err)
            stop = {"has_stop": False, "detail": "查询失败"}

        # 月度用气失败时沿用上次成功结果，避免 ytd/档位/种子一起归零
        try:
            # 再洗一遍：接口实现变了（比如 qty 变成字符串）也不会把刷新打崩
            rows = _clean_rows(await self.api.get_monthly_usage(now=now))
            monthly_ok = True
        except GasApiError as err:
            _LOGGER.warning("月度用气查询失败，沿用上次成功的数据: %s", err)
            rows = [dict(r) for r in self._rows]
            monthly_ok = False
        if monthly_ok:
            self._rows = [dict(r) for r in rows]

        # 缴费明细单独查询：它只用于缴费冲正和缴费实体，失败不应该
        # 让整次刷新失败，更不能被当成“没有缴费”去重置余额基准
        try:
            pays = await self.api.get_payments(now=now)
            payments_ok = True
        except GasApiError as err:
            _LOGGER.warning("缴费记录查询失败，本次不做缴费冲正: %s", err)
            pays = []
            payments_ok = False

        tiers = price.get("tiers") or DEFAULT_TIERS
        # 08:00 前看到的仍是上一个账期的余额；08:00 后才进入新账期。
        current_settlement = (
            today if now.hour >= SETTLEMENT_HOUR else today - timedelta(days=1)
        )
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
            # 只比上限：档位之间可能有间隙（如 360 < ytd < 361），
            # 双向比较会落空导致档位名空白、价格错用第一档
            if ytd <= t["to"] or i == len(tiers) - 1:
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

        # ── 日用量（按 08:00 账期，用余额差值反推）─────────────
        # daily_* 始终描述“最近一次成功结算”，跨天但尚未出账时继续显示它。
        daily_usage = _to_float(self._daily.get("usage"))
        daily_basis = self._daily.get("basis", "")
        daily_date = self._daily.get("data_date")
        daily_source = self._daily.get("source")
        daily_settled_at = self._daily.get("settled_at")
        daily_price = self._daily.get("price")
        daily_status = ""
        prev = self._prev

        if balance_num is None:
            # 余额没拿到：绝不能按 0 算，也不覆盖基准和最近成功结算。
            daily_status = "本次余额获取失败，跳过日用量计算，保留上一基准"
        else:
            # 存储里可能是脏值：统一转成数字，失败就当没有基准
            prev_balance = _to_float(prev.get("balance"))
            prev_date = prev.get("date")
            if prev_balance is None or not prev_date:
                daily_status = f"首次运行，已建立 {now_iso} 的余额基准"
                self._prev = {
                    "balance": balance_num,
                    "date": today_key,
                    "sampled_at": now_iso,
                }
            elif not prev.get("sampled_at"):
                # 旧版基准只有日期，无法判断它写在 08:00 之前还是之后。
                # 猜错会一次性多算一个账期（用量翻倍且日期错位），
                # 所以保守处理：不计算，直接用当前余额重建基准，只少算一天。
                daily_status = (
                    "旧版余额基准缺少采样时间，已按当前余额重建，"
                    "下一个账期开始结算"
                )
                self._prev = {
                    "balance": balance_num,
                    "date": today_key,
                    "sampled_at": now_iso,
                }
            else:
                # 按实际采样时间确定账期
                prev_settlement = None
                try:
                    sampled_at = datetime.fromisoformat(prev["sampled_at"])
                    prev_settlement = (
                        sampled_at.date()
                        if sampled_at.hour >= SETTLEMENT_HOUR
                        else sampled_at.date() - timedelta(days=1)
                    )
                except (TypeError, ValueError):
                    daily_status = "旧余额基准时间无效，已重新建立基准"
                    self._prev = {
                        "balance": balance_num,
                        "date": today_key,
                        "sampled_at": now_iso,
                    }

                if prev_settlement is not None:
                    days = (current_settlement - prev_settlement).days
                    delta = round(float(prev_balance) - balance_num, 2)

                    if days < 0:
                        # 系统时间被回拨之类：基准比当前账期还新，直接重建
                        daily_status = (
                            "账期早于余额基准（系统时间可能被调整），已重新建立基准"
                        )
                        self._prev = {
                            "balance": balance_num,
                            "date": today_key,
                            "sampled_at": now_iso,
                        }
                    elif days > MAX_BACKFILL_DAYS:
                        # 长期停机或时间异常：均摊到几十天毫无意义，
                        # 而且会写出几百条假记录，直接重建基准
                        daily_status = (
                            f"距上次结算已 {days} 天，超出可回填范围"
                            f"（{MAX_BACKFILL_DAYS} 天），已重新建立基准"
                        )
                        self._prev = {
                            "balance": balance_num,
                            "date": today_key,
                            "sampled_at": now_iso,
                        }
                    elif days == 0:
                        # 同一账期内余额又变了：零用量判定后的补扣，或同一天
                        # 第二次扣费。必须用“结算前余额 - 当前余额”重算整个
                        # 账期，只累加差额会把同一天的用量拆到两天。
                        fix_key = prev.get("settled_day") or (
                            current_settlement - timedelta(days=1)
                        ).isoformat()
                        fix_row = _find_row(self._daylist, fix_key)
                        from_balance = _to_float(prev.get("from_balance"))
                        if now.hour < SETTLEMENT_HOUR:
                            daily_status = (
                                f"等待今天 {SETTLEMENT_HOUR:02d}:00 后云端结算昨日用量"
                            )
                        elif delta < 0:
                            daily_status = "本账期已结算，余额上升（可能是缴费），不重复计算"
                        elif delta == 0:
                            daily_status = "当前账期已处理，等待下一次云端结算"
                        elif from_balance is not None and active_price:
                            cost = round(from_balance - balance_num, 2)
                            # 这笔扣费覆盖的是“结算前余额采样点 → 上一账期”
                            # 之间的天数，零用量判定后基准不前进时可能多于一天
                            n_days = 1
                            from_settlement = _parse_day(prev.get("from_settlement"))
                            if from_settlement is not None:
                                n_days = max(
                                    (current_settlement - from_settlement).days, 1
                                )
                            end_day = current_settlement - timedelta(days=1)
                            if cost < 0:
                                daily_status = (
                                    "同账期补扣后余额反而高于结算前，数据异常，本次跳过"
                                )
                            elif not fix_row and n_days == 1:
                                daily_status = (
                                    "同账期余额变动找不到对应日期，"
                                    "留待下一账期一并计算"
                                )
                            else:
                                per_cost = round(cost / n_days, 2)
                                usage = round(per_cost / active_price, 3)
                                base_source = (
                                    (fix_row or {}).get("source") or "balance"
                                )
                                daily_source = (
                                    "estimated" if n_days > 1 else base_source
                                )
                                daily_usage = usage
                                daily_date = end_day.isoformat()
                                daily_settled_at = now_iso
                                daily_price = active_price
                                daily_basis = (
                                    f"结算前余额 {from_balance:.2f} 元 → 当前 "
                                    f"{balance_num:.2f} 元，合计 {cost:.2f} 元，"
                                    f"归属 {daily_date} 前 {n_days} 天"
                                )
                                self._daylist = _upsert_days(
                                    self._daylist,
                                    end_day,
                                    n_days,
                                    usage,
                                    per_cost,
                                    tier=tier_idx + 1,
                                    balance=balance_num,
                                    source=daily_source,
                                    now_iso=now_iso,
                                    price=active_price,
                                )
                                daily_status = "同账期补扣已合并"
                                # 保留原始 from_balance：同一天再扣一次仍能算全
                                self._prev = {
                                    "balance": balance_num,
                                    "date": today_key,
                                    "sampled_at": now_iso,
                                    "from_balance": from_balance,
                                    "from_settlement": prev.get("from_settlement"),
                                    "settled_day": daily_date,
                                }
                                self._daily = {
                                    "date": today_key,
                                    "usage": daily_usage,
                                    "basis": daily_basis,
                                    "data_date": daily_date,
                                    "source": daily_source,
                                    "settled_at": daily_settled_at,
                                    "price": daily_price,
                                }
                        else:
                            daily_status = (
                                "本账期已结算，余额变动无法归属到具体日期，"
                                "留待下一账期一并计算"
                            )
                    else:
                        basis_extra = ""
                        skipped = False
                        pending_used: list[dict[str, Any]] = []
                        end_day = current_settlement - timedelta(days=1)

                        if delta < 0:
                            # 余额变多 = 有缴费。缴费接口只有月份没有日期，
                            # 所以按归属月/上一月/结算月取候选，并逐笔累加到
                            # 足够冲正为止：只取一笔会在同月多笔缴费时丢数据。
                            if not payments_ok:
                                # 缴费接口失败：无法判断是缴费还是异常，
                                # 保留基准，等接口恢复后再一起冲正
                                daily_status = (
                                    "缴费数据暂不可用，余额上升无法判定，"
                                    "保留基准等待下次刷新"
                                )
                                skipped = True
                            else:
                                need = round(-delta, 2)
                                fixed, used, reason = _reconcile_payments(
                                    pays,
                                    end_day,
                                    current_settlement,
                                    delta,
                                    skip=self._used_pays,
                                )
                                used_total = round(
                                    sum(
                                        _to_float(p.get("amount")) or 0.0
                                        for p in used
                                    ),
                                    2,
                                )
                                if fixed is not None:
                                    delta = fixed
                                    # 先别记“已冲正”：万一后面因为气价缺失
                                    # 放弃落账，这笔钱还要留给下次用
                                    pending_used = used
                                    basis_extra = (
                                        f"（已冲正 {len(used)} 笔缴费合计 "
                                        f"{used_total:.2f} 元，为估算）"
                                    )
                                else:
                                    if reason == "mismatch":
                                        detail = (
                                            "缴费金额远大于余额上升"
                                            f"（{used_total:.2f} 元 vs {need:.2f} 元），"
                                            "冲正会造出假用量，"
                                        )
                                    elif reason == "insufficient":
                                        detail = (
                                            f"已找到 {len(used)} 笔缴费但合计不足，"
                                        )
                                    elif reason == "all_used":
                                        # 窗口里有记录，但都已在此前冲正过：
                                        # 说成“无缴费”会让人误以为接口没数据
                                        detail = "窗口内缴费均已在往期冲正，"
                                    else:
                                        detail = "无缴费记录可冲正，"
                                    daily_status = (
                                        f"{prev_settlement} {float(prev_balance):.2f}元 → "
                                        f"{current_settlement} {balance_num:.2f}元："
                                        f"余额上升且{detail}本次不计算"
                                    )
                                    # 不伪造用量，但必须重置基准，否则下期算出负值
                                    self._prev = {
                                        "balance": balance_num,
                                        "date": today_key,
                                        "sampled_at": now_iso,
                                    }
                                    skipped = True

                        if not skipped:
                            if (
                                delta == 0
                                and not basis_extra
                                and days == 1
                                and now.hour < ZERO_CONFIRM_HOUR
                            ):
                                daily_status = (
                                    "余额与上一账期一致，可能云端尚未出账，"
                                    f"等 {ZERO_CONFIRM_HOUR:02d}:00 后仍无变化再判零用量"
                                )
                            elif delta == 0 and not basis_extra and days > 1:
                                daily_status = (
                                    f"跨 {days} 个账期余额均未变化，"
                                    "无法区分零用量与云端未出账，保持等待"
                                )
                            elif not active_price and not (
                                delta == 0 and not basis_extra
                            ):
                                # 气价缺失（接口返回 0 或解析失败）时不能做除法。
                                # 真实零用量不需要气价，只有它是例外；缴费冲正后
                                # 恰好归零（basis_extra 非空）同样要除，必须挡住。
                                daily_status = "气价未知，无法推算日用量"
                            else:
                                # 当前账期结算的是昨天；跨 N 账期则归属 prev_settlement
                                # 至 current_settlement-1，绝不把用量记到采样当天。
                                # 只有“余额原样未动”才算真实零用量；
                                # 缴费冲正后归零属于估算，不能标成 balance
                                is_zero = delta == 0 and not basis_extra
                                zero_pending = bool(prev.get("zero_pending"))
                                if is_zero:
                                    # 08:00 到 12:00 一直没变化：判定前一天真实零用量。
                                    # 不补零会一直等待，第二天按两天均摊并覆盖前一天真实值。
                                    per_cost = 0.0
                                    daily_usage = 0.0
                                    daily_source = "balance"
                                    daily_basis = (
                                        f"{prev_settlement} 账期余额 "
                                        f"{float(prev_balance):.2f}元 → "
                                        f"{current_settlement} 账期余额 "
                                        f"{balance_num:.2f}元，"
                                        f"{ZERO_CONFIRM_HOUR:02d}:00 后仍未变化，"
                                        f"判定 {end_day} 为零用量"
                                    )
                                    daily_status = "结算成功（零用量）"
                                else:
                                    per_cost = round(delta / days, 2)
                                    daily_usage = round(per_cost / active_price, 3)
                                    daily_source = (
                                        "estimated"
                                        if days > 1 or basis_extra
                                        else "balance"
                                    )
                                    daily_basis = (
                                        f"{prev_settlement} 账期余额 "
                                        f"{float(prev_balance):.2f}元 → "
                                        f"{current_settlement} 账期余额 "
                                        f"{balance_num:.2f}元，"
                                        f"归属 {prev_settlement} 至 {end_day}"
                                    )
                                    if days > 1:
                                        daily_basis += f"，跨 {days} 天均摊"
                                    daily_basis += basis_extra
                                    if zero_pending and delta > 0:
                                        # 上一个账期刚判过零用量：这笔扣费很可能是
                                        # 云端延迟出账（两天合一次扣），不能当成
                                        # 严格单日真实值，否则“昨日”会翻倍
                                        daily_source = "estimated"
                                        daily_basis += "，可能含上一账期延迟出账"
                                    daily_status = "结算成功"

                                daily_date = end_day.isoformat()
                                daily_settled_at = now_iso
                                daily_price = active_price

                                # 同日期先删再插；真实/估算记录会覆盖同日的种子数据。
                                self._daylist = _upsert_days(
                                    self._daylist,
                                    end_day,
                                    days,
                                    daily_usage,
                                    per_cost,
                                    tier=tier_idx + 1,
                                    balance=balance_num,
                                    source=daily_source,
                                    now_iso=now_iso,
                                    price=active_price,
                                )
                                if pending_used:
                                    # 落账成功后才把缴费记为“已用过”，
                                    # 避免中途放弃时白耗一笔缴费的冲正额度
                                    self._remember_pays(pending_used)
                                if is_zero:
                                    # 零用量已确认，基准必须跟着推进到当前采样点，
                                    # 否则下一个账期会按 days=2 均摊，把这天
                                    # 已确认的 0 覆盖成半天的用量（越算越错）。
                                    # 云端迟出账的补扣走 days==0 分支，靠下面的
                                    # from_balance / settled_day 合并回这一天。
                                    self._prev = {
                                        "balance": balance_num,
                                        "date": today_key,
                                        "sampled_at": now_iso,
                                        "from_balance": prev_balance,
                                        "from_settlement": (
                                            prev_settlement.isoformat()
                                            if prev_settlement is not None
                                            else None
                                        ),
                                        "settled_day": end_day.isoformat(),
                                        # 下一个账期若突然扣费，可能是这次的延迟
                                        # 出账，届时只能标估算，不能当单日真值
                                        "zero_pending": True,
                                    }
                                else:
                                    self._prev = {
                                        "balance": balance_num,
                                        "date": today_key,
                                        "sampled_at": now_iso,
                                        # 结算前余额与归属日：同一账期再次扣费时
                                        # 用它重算当天总量，而不是把差额拆到两天
                                        "from_balance": prev_balance,
                                        "from_settlement": prev_settlement.isoformat(),
                                        "settled_day": end_day.isoformat(),
                                    }
                                self._daily = {
                                    # date 保留给旧存储读取；语义是结算发生日，不是归属日。
                                    "date": today_key,
                                    "usage": daily_usage,
                                    "basis": daily_basis,
                                    "data_date": daily_date,
                                    "source": daily_source,
                                    "settled_at": daily_settled_at,
                                    "price": daily_price,
                                }

        if not daily_basis:
            daily_basis = daily_status

        # ── 缴费 ─────────────────────────────────────────────
        if payments_ok:
            pay_total = round(sum(p["amount"] for p in pays), 2)
            pay_month = round(
                sum(p["amount"] for p in pays if p["month"] == this_month), 2
            )
            latest_pay = pays[0] if pays else None
            pay_detail = "\n".join(
                f"{p['month']} {p['amount']:.2f}元 {p['paymentno']}" for p in pays
            )
        else:
            # 接口失败时给未知而不是 0，避免把“查不到”显示成“没有缴费”
            pay_total = None
            pay_month = None
            latest_pay = None
            pay_detail = "缴费记录接口本次查询失败，稍后自动重试"

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
                            "source": "seed",
                            "settled_at": None,
                            "price": active_price,
                        }
                    )
            seed.reverse()
            self._daylist = seed[:400]  # 已 reverse 为降序，取最新 400 天

        # 昨日实体只接受“单个账期、无缴费冲正”的真实余额差记录。
        # 08:00 前、跨天均摊、缴费估算、种子及旧版无来源记录都保持 unknown。
        yesterday_key = (today - timedelta(days=1)).isoformat()
        yesterday_row = next(
            (
                r
                for r in self._daylist
                if r.get("day") == yesterday_key and r.get("source") == "balance"
            ),
            None,
        )
        yesterday_usage = (
            _to_float(yesterday_row.get("f_gas")) if yesterday_row else None
        )
        if yesterday_row:
            yesterday_basis = (
                f"{yesterday_key} 真实余额差结算，"
                f"结算时间 {yesterday_row.get('settled_at') or '未知'}"
            )
        elif now.hour < SETTLEMENT_HOUR:
            yesterday_basis = f"等待今天 {SETTLEMENT_HOUR:02d}:00 后云端结算昨日用量"
        else:
            yesterday_basis = "昨日暂无真实结算数据；均摊、缴费估算和种子数据不计入"

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

        # 让“哪一个数据源降级了”在实体属性里可见，不用翻日志
        notes = []
        if not monthly_ok:
            notes.append("月度用气沿用上次缓存")
        if not payments_ok:
            notes.append("缴费记录查询失败")
        data_status = "；".join(notes) or "正常"

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
            "daily_status": daily_status,
            "daily_date": daily_date,
            "daily_source": daily_source,
            "daily_settled_at": daily_settled_at,
            "daily_price": daily_price,
            "yesterday_usage": yesterday_usage,
            "yesterday_basis": yesterday_basis,
            "yesterday_date": yesterday_key,
            "rows": rows,
            # 停气
            "has_stop": stop["has_stop"],
            "stop_detail": stop["detail"],
            # 缴费
            "pay_latest": latest_pay["amount"] if latest_pay else None,
            "pay_latest_month": latest_pay["month"] if latest_pay else None,
            "pay_latest_no": latest_pay["paymentno"] if latest_pay else None,
            "pay_month_total": pay_month,
            "pay_count": len(pays) if payments_ok else None,
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
            "sync_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "data_status": data_status,
        }


def _find_row(daylist: list[dict[str, Any]], day_key: str) -> dict[str, Any] | None:
    """按日期找 daylist 里的一条记录。"""
    for r in daylist:
        if r.get("day") == day_key:
            return r
    return None


def _parse_day(value: Any) -> date | None:
    """把存储里的 ISO 日期字符串转成 date，非法值返回 None。"""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _upsert_days(
    daylist: list[dict[str, Any]],
    end_day: date,
    days: int,
    usage: float,
    per_cost: float,
    *,
    tier: int,
    balance: float | None,
    source: str,
    now_iso: str,
    price: float | None,
) -> list[dict[str, Any]]:
    """把连续 days 天（end_day 往前）写进 daylist，保持降序。

    同日期先删再插：真实/估算记录要能覆盖同日的种子数据，也不能产生重复。
    """
    for i in range(days):
        day_key = (end_day - timedelta(days=i)).isoformat()
        daylist = [r for r in daylist if r.get("day") != day_key]
        daylist.insert(
            0,
            {
                "day": day_key,
                "f_gas": usage,
                "e_gas": per_cost,
                "tier": tier,
                "balance": balance if i == 0 else None,
                "source": source,
                "settled_at": now_iso,
                "price": price,
            },
        )
    # 加载时已归一化，这里再加一层 str()：任何漏网的脏 day 都只影响排序
    # 位置，不会让整个刷新抛 TypeError
    daylist.sort(key=lambda r: str(r.get("day") or ""), reverse=True)
    return daylist


def _window_payments(
    pays: list[dict[str, Any]], end_day: date, settle_day: date | None = None
) -> list[dict[str, Any]]:
    """挑出可能落在本次结算窗口内的缴费，按最新优先排序。

    缴费接口没有缴费日期，只有 recordmonth，所以按三个可能月份匹配：
    数据归属日所在月、它的上一个月（月末缴费次月结算），以及结算发生月
    （结算当天早上刚缴的费）。
    """
    if not pays or end_day is None:
        return []
    months = {
        end_day.strftime("%Y%m"),
        (end_day.replace(day=1) - timedelta(days=1)).strftime("%Y%m"),
    }
    if settle_day is not None:
        months.add(settle_day.strftime("%Y%m"))
    candidates = [p for p in pays if p.get("month") in months]
    # 优先最新月份；同月内保持服务端顺序（接口没有更细的时间字段）
    candidates.sort(key=lambda p: str(p.get("month") or ""), reverse=True)
    return candidates


def _pay_key(p: dict[str, Any]) -> str:
    """缴费的唯一标识。

    接口偶尔没有 paymentno，用“月份@金额”兜底，保证仍能去重；
    这类兜底键在金额相同的两笔缴费上会误合并，但比重复冲正安全。
    """
    no = str(p.get("paymentno") or "").strip()
    if no:
        return no
    amount = _to_float(p.get("amount")) or 0.0
    return f"{p.get('month') or ''}@{amount:.2f}"


def _reconcile_payments(
    pays: list[dict[str, Any]],
    end_day: date,
    settle_day: date | None,
    delta: float,
    skip: Any = (),
) -> tuple[float | None, list[dict[str, Any]], str]:
    """用窗口内的缴费把负差额（余额上升）冲正回真实用气成本。

    返回 (冲正后成本, 实际采用的缴费, 原因)：
      - (成本, [..], "")              冲正成功
      - (None, [], "no_candidate")    窗口内没有可用缴费
      - (None, [..], "all_used")      窗口内的缴费都已在此前冲正过
      - (None, [..], "insufficient")  合计仍不足以覆盖上升额
      - (None, [..], "mismatch")      金额远大于上升额，冲了会造出假用量

    选笔策略：先找“够用且金额最小”的单笔（最常见的就是一笔正好对上），
    都不够才按金额从小到大累加。原来的“按月份取最新、逐笔累加”会把一笔
    100 元缴费拿去冲 5 元的上升，算出 38 方这种错值。
    skip 里是往期已经冲正过的缴费键（见 _pay_key）：不排除掉它们，
    同一笔钱会在后续每个账期被再冲一次，用量成倍虚高。
    """
    candidates = _window_payments(pays, end_day, settle_day)
    if skip:
        used_keys = {str(k) for k in skip}
        remaining = [p for p in candidates if _pay_key(p) not in used_keys]
        if candidates and not remaining:
            return None, [], "all_used"
        candidates = remaining
    if not candidates:
        return None, [], "no_candidate"

    need = round(-delta, 2)
    amounts = {_pay_key(p): (_to_float(p.get("amount")) or 0.0) for p in candidates}

    covering = sorted(
        (p for p in candidates if amounts[_pay_key(p)] >= need),
        key=lambda p: amounts[_pay_key(p)],
    )
    if covering:
        chosen = covering[0]
        total = round(amounts[_pay_key(chosen)], 2)
        if total > need * MAX_OVERPAY_RATIO:
            # 缺口只有一点点，窗口里却只有一笔大额缴费：多半是它已经
            # 在别的账期花掉了，再冲就会凭空造出一大笔用量
            return None, [chosen], "mismatch"
        return round(delta + total, 2), [chosen], ""

    used: list[dict[str, Any]] = []
    total = 0.0
    for p in sorted(candidates, key=lambda p: amounts[_pay_key(p)]):
        used.append(p)
        total = round(total + amounts[_pay_key(p)], 2)
        if total >= need:
            break

    if total < need:
        return None, used, "insufficient"
    if total > need * MAX_OVERPAY_RATIO:
        return None, used, "mismatch"
    return round(delta + total, 2), used, ""


def _to_float(v: Any) -> float | None:
    """转数字；非数字和 NaN/inf 都返回 None。

    NaN/inf 是“能转成 float 但不能参与计算”的脏值：放进去会让全年累计
    变成 nan、档位匹配全部落空，而且 JSON 序列化出来也不是合法数字。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _clean_daylist(daylist: Any) -> list[dict[str, Any]]:
    """校验并归一化持久化的逐日用量。

    必须是合法日期（统一成 ISO 字符串）且 f_gas 是有限数字，其余丢弃。
    存储被手工改坏或来自不兼容版本时，int 类型的 day 会让 _upsert_days
    的排序抛 TypeError，而落盘在排序之后，等于永久卡死。
    """
    if not isinstance(daylist, list):
        return []
    out: list[dict[str, Any]] = []
    for r in daylist:
        if not isinstance(r, dict):
            continue
        day = _parse_day(r.get("day"))
        f_gas = _to_float(r.get("f_gas"))
        if day is None or f_gas is None:
            _LOGGER.debug("忽略非法逐日记录: %r", r)
            continue
        row = dict(r)
        row["day"] = day.isoformat()
        row["f_gas"] = f_gas
        out.append(row)
    # 下游按降序取最新，存储里也保持同样顺序
    out.sort(key=lambda r: r["day"], reverse=True)
    return out


def _clean_rows(rows: Any) -> list[dict[str, Any]]:
    """校验并归一化月度用气数据。

    month 必须是 6 位数字（YYYYMM）且月份 1~12，qty 必须能转成数字；
    不合法的整条丢弃，缺 prev/cur/read_date 的补默认值。逐条过滤而不是
    整组丢弃，避免一条脏数据把一年的历史全清空。
    """
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        month = str(r.get("month") or "").strip()
        qty = _to_float(r.get("qty"))
        if (
            len(month) != 6
            or not month.isdigit()
            or not 1 <= int(month[4:6]) <= 12
            or qty is None
        ):
            _LOGGER.debug("忽略非法月度用气记录: %r", r)
            continue
        out.append(
            {
                "month": month,
                "qty": qty,
                "prev": _to_float(r.get("prev")) or 0.0,
                "cur": _to_float(r.get("cur")) or 0.0,
                "read_date": str(r.get("read_date") or ""),
            }
        )
    # 下游依赖“最新的月份在 rows[0]”
    out.sort(key=lambda r: r["month"], reverse=True)
    return out
