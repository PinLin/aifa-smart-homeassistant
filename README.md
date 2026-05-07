# AIFA Smart Home Assistant Integration

[![GitHub Release](https://img.shields.io/github/release/PinLin/aifa-smart-homeassistant.svg?style=flat-square)](https://github.com/PinLin/aifa-smart-homeassistant/releases)
[![HACS Badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square)](https://github.com/hacs/integration)
[![MIT License](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](LICENSE)

把 AIFA Smart 帳號下的 i-Ctrl 主機與底下的冷氣遙控帶進 Home Assistant，提供裝置在線狀態、溫濕度感測，以及冷氣的即時控制。

> **目前只在 AIFA `i-Ctrl AC`（Smart AC Remote）冷氣控制器上實機驗證過。** 其它 AIFA 裝置可能可以登入並讀到資訊，但冷氣以外的紅外線家具（TV、燈、風扇等）目前還沒有控制路徑。

## 功能特色

- **AIFA 帳號登入**：用 AIFA Smart App 同一組 email 與密碼登入；密碼不留在裝置上，只保留會自動輪替的 token。
- **冷氣即時控制**：`climate` entity 支援開關、模式、目標溫度、風速、擺風方向。
- **AC helper switch**：`強力運轉 / 節電 / 省電` 拆成獨立的 `switch`，方便寫自動化。
- **狀態同步**：除了雲端輪詢，整合也會掛在 i-Ctrl 主機的長連線上，外部來源（AIFA App、實體遙控器）改冷氣狀態時可以快速反映到 HA。
- **巨集按鈕**：AIFA 帳號裡儲存的 macro 會自動建立成 `button` entity，新增 / 刪除 macro 不需要 reload 整合。
- **裝置感測器**：i-Ctrl 主機的環境溫度、濕度、韌體版本、線上狀態都會建立成對應 entity。
- **診斷資料**：從整合頁面可下載一份去敏感資訊的診斷 JSON，方便排查與回報問題。

## 安裝

### HACS 安裝（推薦）

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PinLin&repository=aifa-smart-homeassistant&category=integration)

1. 在 Home Assistant 中開啟 **HACS**。
2. 點選右上角三個點 → **Custom repositories**。
3. 加入 `https://github.com/PinLin/aifa-smart-homeassistant`，類別選 **Integration**。
4. 安裝後重啟 Home Assistant。

### 手動安裝

1. 將 `custom_components/aifa_smart/` 複製到 Home Assistant 的 `config/custom_components/`。
2. 重啟 Home Assistant。

## 設定流程

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aifa_smart)

前往 **設定 → 裝置與服務 → 新增整合 → AIFA Smart**，輸入 AIFA Smart 帳號的 email 與密碼即可。

### 設定參數

| 欄位 | 必填 | 說明 |
|---|---|---|
| Email | 是 | AIFA Smart App 註冊的 email。 |
| Password | 是 | 只在登入當下用一次，用來換取會自動輪替的 token；**不會被持久化**。 |

整合不提供 YAML 設定，全部走 UI。當 token 失效（例如帳號改密碼）時，Home Assistant 會自動進入 **Reauth** 流程，要你重新輸入一次密碼。

## 建立的實體

### 主機（i-Ctrl Hub）

| 實體 | 類型 | 說明 |
|---|---|---|
| Device Connection Status | `binary_sensor` | 主機線上狀態（`CONNECTIVITY` device class） |
| Temperature | `sensor` | 主機環境溫度 |
| Humidity | `sensor` | 主機環境濕度 |
| Firmware | `sensor` | i-Ctrl 韌體版本 |
| Refresh | `button` | 強制觸發一次 coordinator 更新 |

### 子裝置（冷氣遙控）

| 實體 | 類型 | 說明 |
|---|---|---|
| Climate | `climate` | 冷氣主控：模式、目標溫度、風速、擺風 |
| Turbo | `switch` | 強力運轉 |
| Sleep | `switch` | 節電 |
| Power Saving | `switch` | 省電 |
| Device Code | `sensor` | 子裝置綁定的型號代碼（診斷） |
| Code Source | `sensor` | 型號資料來源（`catalog` / `cloud`，診斷） |
| Catalog Brand | `sensor` | 型號對應的品牌（診斷） |
| Function Count | `sensor` | 雲端記錄的可用功能數（診斷） |

### 帳號層

| 實體 | 類型 | 說明 |
|---|---|---|
| Macro Count | `sensor` | 帳號中的 macro 數量（診斷） |
| Macro 按鈕 | `button` | AIFA 帳號裡每個儲存的 macro 各建立一顆按鈕 |

`entity_id` 會以雲端 ID 為基礎產生，避免中文裝置名被轉成拼音。HA 裝置頁會看到 parent device（i-Ctrl Hub）+ sub-device（冷氣遙控 profile）兩層，是因為 AIFA 的資料模型本來就分這兩層，不是同一台機器被抓到兩次。

## 冷氣控制

`climate` entity 會自動掛到看起來是冷氣的子裝置（`type == 0` 且 `device_code` 存在），目前支援：

- **HVAC mode**：`off / auto / cool / dry / fan_only / heat`
- **Fan mode**：`auto / low / medium / high`
- **Swing mode**：`off / auto / fixed_1 ~ fixed_5`
- **目標溫度範圍**：17–30 °C
- **目前溫度**：來自主機環境溫度感測器

整合會主動讀回主機的真實狀態，所以從 AIFA App 或實體遙控器改冷氣，HA 也會跟著更新。實測 `cool 26` 約 5 秒內會同步；`off` 在某些情況可能慢一些，整合會在背景持續重新探測直到收斂。

> 主機只會回報 `hvac_mode` 與 `target_temperature` 的真實狀態，`fan_mode` / `swing_mode` 與 helper switch（強力運轉 / 節電 / 省電）目前沒有可靠的回讀路徑，整合會以「使用者最後設定」為準。`climate` 實體會用 attribute 暴露目前狀態的來源，方便除錯。

## 已知限制

- 子裝置清單只會反映設定當下抓到的內容；之後在 AIFA App 改裝置清單時，重新載入整合最穩。
- 冷氣以外的紅外線家具（TV、燈、風扇等）目前還沒有控制路徑。
- learned keys、`favorite_channels` 目前只做 read-only 診斷或還沒納入。

## 移除整合

1. 前往 **設定 → 裝置與服務**。
2. 點選 **AIFA Smart** 整合卡片。
3. 點選右上角三個點 → **刪除**。
4. 若不再需要本整合的程式碼，可在 HACS 中移除，或手動刪除 `config/custom_components/aifa_smart/`。

## 開發檢查

```bash
python -m compileall custom_components/aifa_smart
python -m py_compile custom_components/aifa_smart/*.py tests/*.py
python -m pytest tests/ -v
```

## Notes

- PRs 與 issues 歡迎 — 對應你手邊 AC 機型的測試樣本特別有幫助。

## 免責聲明

本整合為非官方、社群維護的專案，與艾法科技股份有限公司（AIFA Technology）及 AIFA Remote 服務無任何關係。資料來源為 AIFA 雲端 API 與 i-Ctrl 主機 socket；這些介面為非公開介面，可能在沒有預告的情況下變更或停用，本整合可能因此暫時或永久無法繼續運作。

## 授權

[MIT License](LICENSE)
