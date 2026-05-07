"""Human-readable metadata helpers for AIFA devices."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import AifaDevice, AifaSubDevice

_DEVICE_TYPE_LABELS: dict[str, str] = {
    "4": "AIFA IR Hub",
}
_TV_NAME_TOKENS = ("tv", "television", "電視", "機上盒", "settop", "stb")
_FAN_NAME_TOKENS = ("fan", "風扇")
_LIGHT_NAME_TOKENS = ("light", "lamp", "燈")
_AC_NAME_TOKENS = ("ac", "aircon", "conditioner", "冷氣", "空調")


def _normalize_text(text: str | None) -> str:
    """Normalize free text for simple keyword checks."""
    if text is None:
        return ""
    return "".join(char for char in text.casefold().strip() if char.isalnum())


def device_model_label(device: AifaDevice) -> str:
    """Build a user-facing parent device model label."""
    raw_type = (device.device_type or "").strip()
    if not raw_type:
        return "AIFA Device"
    return _DEVICE_TYPE_LABELS.get(raw_type, f"AIFA Device Type {raw_type}")


def sub_device_category_label(sub_device: AifaSubDevice) -> str:
    """Guess a user-facing category label for a sub-device."""
    raw_type = (sub_device.type or "").strip()
    normalized_name = _normalize_text(sub_device.name)

    if raw_type == "0" or any(token in normalized_name for token in _AC_NAME_TOKENS):
        return "Air Conditioner"
    if any(token in normalized_name for token in _TV_NAME_TOKENS):
        return "Television"
    if any(token in normalized_name for token in _FAN_NAME_TOKENS):
        return "Fan"
    if any(token in normalized_name for token in _LIGHT_NAME_TOKENS):
        return "Light"
    if raw_type:
        return f"AIFA Sub-device Type {raw_type}"
    return "AIFA Sub-device"


def sub_device_model_label(sub_device: AifaSubDevice) -> str:
    """Build a user-facing sub-device model label."""
    category = sub_device_category_label(sub_device)
    parts: list[str] = [category]

    brand_name = sub_device.device_code_brand_localized_name or sub_device.device_code_brand_name
    model_name = sub_device.device_code_model_name
    # AIFA's catalog brand can be a code-family label rather than the real AC brand.
    # Prefer the concrete remote/model code in the main device label to avoid misleading users.
    if model_name:
        parts.append(model_name)
    elif category != "Air Conditioner" and brand_name:
        parts.append(brand_name)

    if sub_device.device_code is not None:
        parts.append(f"device code {sub_device.device_code}")

    return " / ".join(parts)


def is_probably_tv_like(sub_device: AifaSubDevice) -> bool:
    """Return True when a sub-device name looks TV-like."""
    normalized_name = _normalize_text(sub_device.name)
    return any(token in normalized_name for token in _TV_NAME_TOKENS)
