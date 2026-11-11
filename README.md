# AIFA Smart Home Assistant Integration

[![GitHub Release](https://img.shields.io/github/release/PinLin/aifa-smart-homeassistant.svg?style=flat-square)](https://github.com/PinLin/aifa-smart-homeassistant/releases)
[![HACS Badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square)](https://github.com/hacs/integration)

把 AIFA Smart 帳號下的 i-Ctrl 主機與底下的冷氣遙控帶進 Home Assistant，提供裝置在線狀態、溫濕度感測，以及冷氣的即時控制。

> **目前只在 AIFA `i-Ctrl AC`（Smart AC Remote）冷氣控制器上實機驗證過。** 其它 AIFA 裝置可能可以登入並讀到資訊，但冷氣以外的紅外線家具（TV、燈、風扇等）目前還沒有控制路徑。

## 功能特色

- **AIFA 帳號登入**：用 AIFA Smart App 同一組 email 與密碼登入；密碼不留在裝置上，只保留會自動輪替的 token。
- **冷氣控制**：照 AIFA Smart App 的冷氣頁實作，模式、溫度與 App 上的每顆按鍵都能從 HA 操作。
- **雲端或區網**：指令可以經 AIFA 雲端送出，也可以在區網直接送到主機。
- **代碼設定**：在整合選項裡換冷氣的遙控器代碼，可以先試按再儲存，也可以用原廠遙控器找代碼。
- **狀態同步**：從 AIFA App 或實體遙控器改冷氣時，主機會推送狀態，HA 跟著更新。
- **巨集按鈕**：AIFA 帳號裡儲存的 macro 會自動建立成 `button` entity，新增／刪除 macro 不需要重新載入整合。
- **裝置感測器**：主機的環境溫度、濕度、韌體版本、線上狀態都會建立成對應 entity。
- **診斷資料**：從整合頁面可下載一份去除敏感資訊的診斷 JSON，內含連線與傳輸狀態，方便排查與回報問題。

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

### 傳輸方式

| 傳輸方式 | 說明 |
|---|---|
| `cloud`（預設） | 經 AIFA 雲端送到主機；HA 和主機不在同一個網路時只能用這個。 |
| `lan` | 在區網直接連到主機，不經過雲端；連不到主機時報錯，不會改走雲端。 |
| `auto` | 先試區網，區網連不上、什麼都沒送出時改走雲端。 |

- 區網模式需要 HA 和主機在同一個網段、能收發 UDP 廣播；Docker 要用 `host` network。選 `lan` 或 `auto` 時選項頁會先搜尋一次主機，找不到時會警告。
- 溫濕度數值一律來自雲端，不論哪種傳輸方式。
- **離線啟動**：`lan` 與 `auto` 會把裝置清單快取在本機。HA 啟動時雲端連不上，就從快取啟動，區網控制照常可用；溫濕度與巨集按鈕等雲端恢復後才能使用。

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
| Climate | `climate` | 冷氣主控：開關、模式、目標溫度 |
| 冷氣按鍵 | `button` | App 冷氣頁的按鍵：定時、風向、風速、節電、舒眠、強冷等，依代碼顯示 |
| 溫度 +／− | `button` | App 顯示 +／− 鍵的冷氣才建立 |
| 目前風速 | `sensor` | 主機回報的風速 |
| Device Code | `sensor` | 子裝置綁定的型號代碼（診斷） |
| Code Source | `sensor` | 型號資料來源（`catalog` / `cloud`，診斷） |
| Catalog Brand | `sensor` | 型號對應的品牌；在「代碼設定」選過品牌時顯示所選的品牌（診斷） |
| Function Count | `sensor` | 雲端記錄的可用功能數（診斷） |

### 帳號層

| 實體 | 類型 | 說明 |
|---|---|---|
| Macro Count | `sensor` | 帳號中的 macro 數量（診斷） |
| Macro 按鈕 | `button` | AIFA 帳號裡每個儲存的 macro 各建立一顆按鈕 |

`entity_id` 會以雲端 ID 為基礎產生，避免中文裝置名被轉成拼音。HA 裝置頁會看到 parent device（i-Ctrl Hub）+ sub-device（冷氣遙控 profile）兩層，是因為 AIFA 的資料模型本來就分這兩層，不是同一台機器被抓到兩次。

## 冷氣控制

`climate` entity 會自動掛到看起來是冷氣的子裝置（`type == 0` 且 `device_code` 存在），操作方式和 AIFA Smart App 相同：

- **模式**：`off / auto / cool / dry / fan_only / heat`，依代碼只顯示 App 有的模式；沒有模式鍵的代碼改用「模式」按鈕循環切換。`climate.turn_on` 只送開機鍵，從關機選模式時會先送開機鍵。
- **溫度**：App 顯示滑桿的代碼可直接設定 16–30 °C；App 只顯示 +／− 鍵的代碼改用「溫度 +／−」按鈕，`climate` 不提供目標溫度。
- **風速與風向**：App 只有循環鍵，所以用「風速」「風向」按鈕切換，目前風速看「目前風速」感測器。
- **其他按鍵**：定時、節電、舒眠、強冷等每按一次送一次按鍵碼。冷氣關機時，App 上「開機才能用」的按鍵會報錯。
- **指令確認**：送出指令後會等主機回報；沒有回報、回報不符或連線失敗時，服務呼叫會報錯，不會自動重送。
- **關機狀態**：有些代碼關機後主機不會回報關機，按下關機後整合會保留關機，直到主機回報不同的狀態或從 HA 開機。
- **重啟後**：在主機送來第一筆回報之前，`climate` 顯示重啟前最後的狀態，避免誤觸發自動化。
- **assumed state**：主機的回報只能證明它發出了紅外線，不能證明冷氣收到，所以 `climate` 標示為 `assumed_state`；目前溫度來自主機的環境溫度感測器。
- **巨集**：和 App 一樣不等主機回報，每一步送出就接著送下一步。

## 代碼設定

整合選項的「代碼設定」照 AIFA Smart App 的流程：選冷氣 → 選品牌或「用遙控器找代碼」 → 選代碼 → 「試按」或「儲存」。

- **試按**：讓主機暫時改用這個代碼並送一次電源鍵，看冷氣有沒有反應；不存到雲端。沒儲存就離開時，整合會把主機改回原本的代碼。
- **儲存**：確認主機已改用新代碼後才存到雲端，並重新載入整合。主機不支援這個代碼，或任何一步失敗時，會把主機改回原本的代碼並顯示錯誤。
- **用遙控器找代碼**：送出後將原廠遙控器對準主機按下開機鍵，主機會列出可能的代碼，再逐一試按找出正確的那個。
- 冷氣有學習按鍵時不能更換代碼，和 App 一樣。主機回報的代碼和雲端不一致時，會在「修復」建立通知，不會自動修正。

## 已知限制

- 子裝置清單只會反映設定當下抓到的內容；之後在 AIFA App 改裝置清單時，重新載入整合最穩。
- 冷氣以外的紅外線家具（TV、燈、風扇等）目前還沒有控制路徑；`favorite_channels` 也還沒納入。
- 冷氣按鍵實體照 Remote Config 清單建立；清單之後不再列出某顆按鍵時，下次載入會刪除該實體，自訂的名稱、區域與停用設定也會一起消失。
- 試按期間 Home Assistant 意外中止（斷電、當機）時，主機會停在試按的代碼，需要到「代碼設定」改回來。

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

## 備註

- 歡迎 PR 與 issue，對應你手邊冷氣機型的測試樣本特別有幫助。
- 各版本的變更見 [CHANGELOG](CHANGELOG.md)。

## 免責聲明

本整合為非官方、社群維護的專案，與艾法科技股份有限公司（AIFA Technology）及 AIFA Remote 服務無任何關係。資料來源為 AIFA 雲端 API 與 i-Ctrl 主機 socket；這些介面為非公開介面，可能在沒有預告的情況下變更或停用，本整合可能因此暫時或永久無法繼續運作。
