"""燃气传感器实体。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GasCoordinator


@dataclass(frozen=True, kw_only=True)
class GasSensorDescription(SensorEntityDescription):
    """带取值函数的描述。"""

    value_fn: Callable[[dict[str, Any]], Any] = lambda d: None
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda d: {}


SENSORS: tuple[GasSensorDescription, ...] = (
    GasSensorDescription(
        key="balance",
        translation_key="balance",
        name="余额",
        native_unit_of_measurement="CNY",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:cash",
        value_fn=lambda d: d["balance"],
        attr_fn=lambda d: {"说明": "云端余额实时更新，含当月缴费与用气"},
    ),
    GasSensorDescription(
        key="arrears",
        translation_key="arrears",
        name="欠费",
        native_unit_of_measurement="CNY",
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:cash-remove",
        value_fn=lambda d: d["owe"],
    ),
    GasSensorDescription(
        key="remaining",
        translation_key="remaining",
        name="剩余气量",
        native_unit_of_measurement="m³",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gas-cylinder",
        value_fn=lambda d: d["remain_qty"],
        attr_fn=lambda d: {"计算公式": "余额 ÷ 当前阶梯气价", "使用气价": d["price"]},
    ),
    GasSensorDescription(
        key="price",
        translation_key="price",
        name="当前气价",
        native_unit_of_measurement="CNY/m³",
        icon="mdi:currency-cny",
        value_fn=lambda d: d["price"],
        attr_fn=lambda d: {
            "当前档位": d["tier_label"],
            "阶梯": " / ".join(
                f"{t['from']}-{t['to']}方 {t['price']}元" for t in d["tiers"]
            ),
        },
    ),
    GasSensorDescription(
        key="current_tier",
        translation_key="current_tier",
        name="当前阶梯档位",
        icon="mdi:stairs-up",
        value_fn=lambda d: d["tier_index"] + 1,
        attr_fn=lambda d: {
            "档位名称": d["tier_label"],
            "当前气价": d["price"],
            "今年累计用气": d["ytd"],
            "下一档起点": d["next_threshold"],
            "下一档气价": d["next_price"],
            "距下一档还有": d["tier_remain"],
            "阶梯明细": " / ".join(
                f"{t['from']}-{t['to']}方 {t['price']}元" for t in d["tiers"]
            ),
            "说明": "1=第一档 2=第二档 3=第三档；按今年累计用气自动匹配",
        },
    ),
    GasSensorDescription(
        key="tier_progress",
        translation_key="tier_progress",
        name="距下一档还有",
        native_unit_of_measurement="m³",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:stairs",
        value_fn=lambda d: d["tier_remain"],
        attr_fn=lambda d: {
            "当前档位": d["tier_label"],
            "当前气价": d["price"],
            "今年累计": d["ytd"],
            "下一档起点": d["next_threshold"],
            "下一档气价": d["next_price"],
        },
    ),
    GasSensorDescription(
        key="month_usage",
        translation_key="month_usage",
        name="最近一期抄表用气",
        native_unit_of_measurement="m³",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire",
        value_fn=lambda d: d["month_usage"],
        attr_fn=lambda d: {
            "抄表月份": d["month_record"],
            "抄表日期": d["month_read_date"],
            "说明": "云端只在抄表日（约每月 23 号）出用气量，下一个抄表日前这个值一直是上个月的",
        },
    ),
    GasSensorDescription(
        key="ytd",
        translation_key="ytd",
        name="今年累计用气",
        native_unit_of_measurement="m³",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=lambda d: d["ytd"],
        attr_fn=lambda d: {"截至月份": d["month_record"]},
    ),
    GasSensorDescription(
        key="usage_12m",
        translation_key="usage_12m",
        name="近12个月用气",
        native_unit_of_measurement="m³",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-bar",
        value_fn=lambda d: d["usage_12m"],
        attr_fn=lambda d: {
            "明细": "\n".join(f"{r['month']} {r['qty']}方" for r in d["rows"])
        },
    ),
    GasSensorDescription(
        key="meter_reading",
        translation_key="meter_reading",
        name="燃气表读数",
        native_unit_of_measurement="m³",
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:meter-gas",
        value_fn=lambda d: d["reading"],
        attr_fn=lambda d: {
            "抄表日期": d["reading_time"],
            "表号": d["meter_no"],
        },
    ),
    GasSensorDescription(
        key="reading_age",
        translation_key="reading_age",
        name="读数滞后",
        native_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-alert-outline",
        value_fn=lambda d: d["reading_age"],
    ),
    GasSensorDescription(
        key="daily_usage",
        translation_key="daily_usage",
        name="日用量（余额推算）",
        native_unit_of_measurement="m³",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-check-outline",
        value_fn=lambda d: d["daily_usage"],
        attr_fn=lambda d: {
            "数据日期": d["daily_date"],
            "结算时间": d["daily_settled_at"],
            "数据来源": {
                "balance": "余额差真实结算",
                "estimated": "跨天均摊或缴费冲正，估算",
                "seed": "月度数据均摊种子",
                "legacy": "旧版记录（日期口径可能偏移）",
            }.get(d["daily_source"], "旧版缓存或尚未结算"),
            "推算依据": d["daily_basis"],
            "本次状态": d["daily_status"],
            "使用气价": d["daily_price"] or d["price"],
            "说明": "最近一次成功结算的用气量；归属日期见数据日期，跨天或缴费冲正会标明估算",
        },
    ),
    GasSensorDescription(
        key="yesterday_usage",
        translation_key="yesterday_usage",
        name="昨日用气量",
        native_unit_of_measurement="m³",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-arrow-left",
        value_fn=lambda d: d["yesterday_usage"],
        attr_fn=lambda d: {
            "数据日期": d["yesterday_date"],
            "数据来源": "余额差真实结算" if d["yesterday_usage"] is not None else None,
            "推算依据": d["yesterday_basis"],
            "说明": (
                "严格昨日值：只接受单日、无缴费冲正的余额差；"
                "每天 08:00 前及云端未出账时为未知，均摊和种子数据不计入"
            ),
        },
    ),
    GasSensorDescription(
        key="payment",
        translation_key="payment",
        name="缴费",
        native_unit_of_measurement="CNY",
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:cash-plus",
        value_fn=lambda d: d["pay_latest"],
        attr_fn=lambda d: {
            "最近缴费月份": d["pay_latest_month"],
            "最近缴费单号": d["pay_latest_no"],
            "本月缴费合计": d["pay_month_total"],
            "近181天笔数": d["pay_count"],
            "近181天合计": d["pay_total"],
            "明细": d["pay_detail"],
        },
    ),
    GasSensorDescription(
        key="stop_notice",
        translation_key="stop_notice",
        name="停气通知",
        icon="mdi:gas-burner",
        value_fn=lambda d: "有停气通知" if d["has_stop"] else "无",
        attr_fn=lambda d: {
            "详情": d["stop_detail"],
            "说明": "「无」= 当前没有停气计划",
        },
    ),
    GasSensorDescription(
        key="gas_info",
        translation_key="gas_info",
        name="燃气信息",
        native_unit_of_measurement="元",
        icon="mdi:fire",
        value_fn=lambda d: d["balance"],
        attr_fn=lambda d: {
            "计费标准": d["tier_info"],
            "monthlist": d["month_list"],
            "daylist": d["day_list"],
            "yearlist": d["year_list"],
            "日均消费": d["avg_daily_cost"],
            "剩余天数": d["remain_days"],
            "预付费": "是",
            "数据源": "中燃在线",
            "最后同步日期": d["sync_time"],
            "本月用电": d["month_usage"] or 0,
            "本年用电": d["ytd"],
            "当前档位": d["tier_index"] + 1,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GasCoordinator = entry.runtime_data
    async_add_entities(
        GasSensor(coordinator, entry, desc) for desc in SENSORS
    )


class GasSensor(CoordinatorEntity[GasCoordinator], SensorEntity):
    """一个燃气传感器。"""

    entity_description: GasSensorDescription
    # 关掉设备名前缀，自己指定 entity_id（否则会变成
    # sensor.ran_qi_mou_ji_xia_yu_e_180_07_yu_e 这种拼音串）
    _attr_has_entity_name = False
    # 这些大列表只给 gas_info 的前端卡片实时读取；超过 16KB 的属性会被
    # recorder 整组拒存，所以只把它们排除出历史记录，不影响卡片。
    # 必须在类体定义：Entity.__init_subclass__ 此时才合并该集合，
    # 实例 __init__ 里赋值不会生效。其他实体没有这些属性名，不受影响。
    _unrecorded_attributes = frozenset({"daylist", "monthlist", "yearlist"})

    def __init__(
        self,
        coordinator: GasCoordinator,
        entry: ConfigEntry,
        description: GasSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_name = description.name
        self.entity_id = f"sensor.gas_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "中国燃气",
            "model": "中燃在线",
        }

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return self.entity_description.attr_fn(self.coordinator.data)
