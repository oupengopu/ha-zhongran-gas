# 中国燃气（中燃在线）Home Assistant 集成

把「中燃在线」小程序的燃气数据接进 Home Assistant —— 余额、阶梯气价、用气量、停气通知、缴费记录，全部是 HA 原生实体。

> 移植自一套已经稳定运行很久的 Node-RED 流程，三套不同签名全部实测跑通。

## 提供的实体

| 实体 | 说明 |
|---|---|
| `sensor.gas_balance` | 账户余额（云端实时，含当月缴费与用气） |
| `sensor.gas_arrears` | 欠费 |
| `sensor.gas_remaining` | 剩余气量（余额 ÷ 当前阶梯气价） |
| `sensor.gas_price` | 当前阶梯气价（按今年累计自动匹配档位） |
| `sensor.gas_tier_progress` | 距下一档还有多少方 |
| `sensor.gas_month_usage` | 最近一期抄表用气（**注意：是上个月，不是本月**） |
| `sensor.gas_ytd` | 今年累计用气 |
| `sensor.gas_usage_12m` | 近 12 个月用气 |
| `sensor.gas_meter_reading` | 燃气表读数（`device_class: gas`，可用于能源面板） |
| `sensor.gas_reading_age` | 读数滞后天数 |
| `sensor.gas_daily_usage` | 最近结算日用量（余额差值推算，**不需要硬件**；属性含归属日期和来源） |
| `sensor.gas_yesterday_usage` | 昨日用气量（严格值：仅单日真实余额差，08:00 前为未知） |
| `sensor.gas_payment` | 缴费（最近一笔 + 本月合计 + 明细） |
| `sensor.gas_stop_notice` | 停气通知 |
| `sensor.gas_info` | 结构化实体，给 [`electricity-info-card`](https://github.com/chjspp520/electricity-info-card) 画燃气卡片用 |
| `binary_sensor.gas_sync_ok` | 同步状态 |

## 安装

### 方式一：HACS（推荐）

1. HACS → 集成 → 右上角 `⋮` → **自定义存储库**
2. 仓库地址：`https://github.com/oupengopu/ha-zhongran-gas`
3. 类别选 **Integration**
4. 搜索「中国燃气」→ 下载 → 重启 HA

### 方式二：手动

把 `custom_components/zhongran_gas` 整个目录拷到 HA 的 `/config/custom_components/` 下，重启 HA。

## 配置

**设置 → 设备与服务 → 添加集成 → 搜索「中国燃气」**

需要填 5 个值，都用抓包工具从「中燃在线」微信小程序里抄：

| 字段 | 从哪来 |
|---|---|
| `accessToken` | 请求头 `accessToken` |
| `userId` | 请求头 `userId` |
| `custCode` | 燃气户号（10 位数字） |
| `custName` | 户主姓名（全名） |
| `x-mas-app-info` | 请求头 `x-mas-app-info` 的**完整值** |

📖 **详细步骤见 [docs/获取私有凭据.md](docs/获取私有凭据.md)** —— 包含抓包工具选型、证书安装、5 个值分别从哪抄、常见问题。

> **抓包方法**：手机装 Surge / Loon / Quantumult X / Reqable / HttpCanary，打开「中燃在线」小程序，点进「燃气缴费」看到余额，然后在抓包历史里找 `findCustInfoByCustCodeAndCustName` 这条请求，看它的请求头。

## 刷新频率

默认 **30 分钟**一次，可在集成选项里改（10 ~ 1440 分钟）。

- **余额**：云端是日更的，30 分钟够用
- **用气量**：云端只在**抄表日**（约每月 23 号）出，所以 `gas_month_usage` 平时一直是上个月的值 —— 这是服务端的限制，不是集成的问题
- **日用量**：云端每天约 08:00 结算前一天余额，集成据此反推。`gas_daily_usage` 是最近一次成功结算值（可能来自更早日期），`gas_yesterday_usage` 只在昨日有单日真实余额差时出值；08:00 前、云端未出账、停机跨天均摊或缴费冲正时，“昨日”保持未知，不用估算值充数
- **零用量**：08:00 到 12:00 余额一直没变，判定前一天为真实零用量（这时“昨日”显示 0，不会卡死，也不会把两天的量摊到一天）；12:00 后云端才补扣的，会回头修正当天的 0 值。跨多个账期余额都没变时不自动补零，继续等待
- **历史记录**：升级前的旧日用量不整体平移日期，只标记为 `legacy`（日期口径可能偏移）；新记录从升级后开始按归属日期和来源区分

## 配合 electricity-info-card

把卡片配置成：

```yaml
type: custom:electricity-info-card
name: 我的燃气
multiclass:
  "1":
    utility_type: gas
    info: My
    entity: sensor.gas_info
```

`sensor.gas_info` 的属性就是那张卡片要的格式（`计费标准` / `monthlist` / `daylist` / `yearlist` / `日均消费` / `剩余天数` / `预付费` / `数据源` / `最后同步日期` / `本月用电` / `本年用电` / `当前档位`）。

⚠️ **不要在卡片里写 `tier1_max` / `tier1_price` / `period_*`** —— 一旦写了，卡片就不再读实体的「计费标准」，反而会显示成 `0-0m³` / `--元/m³`。

## 告警建议（用 HA 自动化）

```yaml
# 余额不足 5 元
alias: 燃气余额不足
trigger:
  - platform: numeric_state
    entity_id: sensor.gas_balance
    below: 5
action:
  - service: notify.xxx
    data:
      message: "⚠️ 燃气余额只剩 {{ states('sensor.gas_balance') }} 元"

# 有停气通知
alias: 燃气停气通知
trigger:
  - platform: state
    entity_id: sensor.gas_stop_notice
    from: "无"
action:
  - service: notify.xxx
    data:
      message: "🔥 燃气停气通知\n{{ state_attr('sensor.gas_stop_notice','详情') }}"
```

## 注意事项

- 这是**非官方**集成，用的是「中燃在线」小程序的私有接口，接口变了就要跟着改
- `accessToken` 会过期，过期后集成会显示 `binary_sensor.gas_sync_ok = off`，重新抓一次 token 然后到集成选项里粘贴新值保存即可（选项里留空的凭据保持不变，保存后自动重载）
- **不要高频请求** —— 服务端有防护，30 分钟一次已经很够了

## License

MIT
