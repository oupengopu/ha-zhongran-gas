"""常量定义：接口地址、签名密钥、实体描述。

注意：
- SALT / APP_KEY / SIGN_KEY 是【服务端固定的】常量，不是用户密钥，可以写死在代码里。
- accessToken / userId / custCode / custName / mas 是【用户的】，只能通过 config entry 传入。
"""
from __future__ import annotations

DOMAIN = "zhongran_gas"

# ── 接口地址 ──────────────────────────────────────────────
BASE = "https://zrds.95007.com"
URL_CUST_INFO = f"{BASE}/crm_controller/user/findCustInfoByCustCodeAndCustName"
URL_PRICE_INFO = f"{BASE}/crm_controller/user/getUserPriceInfoForHsh"
URL_METHOD_ENCRYPT = f"{BASE}/controller/payfee/methodEncrypt.do"
URL_STOP_GAS = f"{BASE}/controller/crm/getStopGas.do"
URL_PAYMENT_LOG = f"{BASE}/crm_controller/payfee/getPaymentLogForHsh"

# ── 签名常量 ──────────────────────────────────────────────
# 简单签名：md5(custCode + SALT + timeStamp)
SALT = "yph1234567890"

# methodEncrypt.do 专用：参数按 key 排序拼接后 + "&key=" + SIGN_KEY
APP_KEY = "HSH3141584003200621363QHL9"
SIGN_KEY = "9QHLFB50ACA1424F91F31989942DDBD8"
ENVIR = "2"

# ── 请求头 ────────────────────────────────────────────────
UA = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

# ── 默认配置 ──────────────────────────────────────────────
DEFAULT_SCAN_INTERVAL = 30  # 分钟
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 1440

# ── 阶梯默认值（接口拿不到时兜底，玉林中燃 2026）────────────
DEFAULT_TIERS = [
    {"from": 0, "to": 360, "price": 3.29},
    {"from": 361, "to": 600, "price": 3.95},
    {"from": 601, "to": 999999, "price": 4.94},
]
