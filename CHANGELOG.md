# Changelog

本專案的重要變更都記錄在這個檔案。格式依照 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本號遵循 [Semantic Versioning](https://semver.org/lang/zh-TW/)。

## Unreleased

### Added

- 整合選項新增「傳輸方式」：`cloud`（預設）、`lan`（區網直連主機）、`auto`（先試區網，連不上改走雲端）。
- `lan` 與 `auto` 會把裝置清單快取在本機，雲端連不上時仍能從快取啟動並以區網控制。
- 整合選項新增「代碼設定」：依品牌選代碼，或用原廠遙控器讓主機找代碼；可以先試按再儲存，失敗時會把主機改回原本的代碼。
- 在「代碼設定」選過的品牌會記在整合選項，品牌感測器改顯示所選的品牌。
- App 冷氣頁的每顆按鍵各自建立 `button` 實體：定時、風向、風速、節電、舒眠、強冷等，依代碼顯示。
- App 只顯示 +／− 鍵的代碼，新增「溫度 +／−」按鈕。
- 新增「目前風速」感測器，顯示主機回報的風速。
- 主機回報的代碼和雲端不一致時，在「修復」建立通知。
- 診斷資料加入區網與雲端連線的傳輸狀態。

### Changed

- 風速與風向改為循環按鍵，`climate` 不再提供 `fan_mode` 與 `swing_mode`。
- `climate.turn_on` 只送開機鍵，不再同時設定模式或溫度。
- 指令送不出去、主機沒有回應或回報不符時，服務呼叫會報錯，不會自動重送。
- 冷氣關機時，「開機才能用」的按鍵會報錯；`climate.set_temperature` 不再自動開機而是報錯，自動與送風模式也不能設定溫度。
- `climate` 一律標示為 `assumed_state`；原本的 `assumed_state` 屬性改名為 `aifa_state_assumed`。
- HA 重啟或重新載入後，在主機送來第一筆回報之前，`climate` 與「目前風速」顯示重啟前最後的狀態。
- 整合不再定期查詢主機狀態，改為每次連線時查詢一次，以及指令送出後沒收到推送時查詢一次確認。

### Removed

- 移除 `switch.*_turbo`、`switch.*_sleep`、`switch.*_power_saving`，改用「強冷」「舒眠」「節電」按鈕；引用這些開關的自動化與儀表板需要更新。
- 移除 `aifa_requested_fan_mode`、`aifa_requested_swing_mode`、`aifa_requested_turbo`、`aifa_requested_sleep`、`aifa_requested_power_saving` 屬性。

### Fixed

- 主機不回報關機的代碼，按下關機後不再跳回原本的模式，會保留關機直到主機回報不同的狀態或從 HA 開機。
