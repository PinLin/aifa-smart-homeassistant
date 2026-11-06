"""Constants for the AIFA Smart integration."""
from __future__ import annotations

DOMAIN = "aifa_smart"
MANUFACTURER = "AIFA Technology Corp."

API_BASE_URL = "https://api.aifaremote.com"
CLASSIC_SOCKET_HOST = "aifaremote.com"
CLASSIC_SOCKET_PORT = 8751
OAUTH_TOKEN_PATH = "/oauth2/token"
OAUTH_CLIENT_ID = "Ecp5TUQxtOjdQ24u"
DEVICES_PATH = "/devices"
FUNCTIONS_PATH = "/functions"
DEVICE_TRANSFER_PATH = "/devices/transfer"
SUB_DEVICE_DEVICE_CODES_PATH = "/sub-devices/{sub_device_id}/device-codes"
SUB_DEVICE_SET_CODE_PATH = "/sub-devices/{sub_device_id}/set-code"
MACROS_PATH = "/macros"

# Firebase Remote Config — used to refresh AC capability metadata at runtime.
# These values are EXTRACTED from AIFA Smart's APK (res/values/strings.xml +
# the App ID found at runtime). Android Firebase API keys are not secrets —
# they ship in every APK. AIFA's GCP project has been stable across 1.2.5.
# If AIFA rotates the key, refresh fails and the integration falls back to
# the bundled ac_catalog.py — no breakage of control commands.
FIREBASE_PROJECT_ID = "i-ctrl-pro"
FIREBASE_PROJECT_NUMBER = "240868916940"
FIREBASE_APP_ID = "1:240868916940:android:a1bbaac859617483224c69"
FIREBASE_API_KEY = "AIzaSyC5uitwFWZC0V8wsDG-UaQdXJ7OYoSE1tY"
FIREBASE_RC_NAMESPACE = "firebase"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_TYPE = "token_type"
CONF_TOKEN_EXPIRES_AT = "token_expires_at"

CONF_TRANSPORT = "transport"
TRANSPORT_CLOUD = "cloud"
TRANSPORT_LAN = "lan"
TRANSPORT_AUTO = "auto"
TRANSPORTS = (TRANSPORT_CLOUD, TRANSPORT_LAN, TRANSPORT_AUTO)
DEFAULT_TRANSPORT = TRANSPORT_CLOUD

# The AIFA app also gives the LAN connect-back 3 s before using the cloud.
LAN_CONNECT_TIMEOUT = 3.0
LAN_AUTO_SUPPRESS_SECONDS = 60
LAN_UNAVAILABLE_GRACE_SECONDS = 60

# Hub pushes SENSOR{...} every 60s via TLS 8751 broadcast. AC observed state and
# sensor packets both arrive on the long-lived classic listener, so the
# REST poll is mainly a structural-data refresher (devices, functions,
# macros, favorites, device-codes catalog). Hard-coding to 60s aligns with
# the hub's natural cadence; faster polling buys nothing.
SCAN_INTERVAL = 60

ATTR_DEVICE_TYPE = "device_type"
ATTR_SUB_DEVICE_COUNT = "sub_device_count"
ATTR_LAST_UPDATE = "last_update"
ATTR_RAW_DEVICE = "raw_device"


def transport_from_options(options: object) -> str:
    """Return the configured transport, falling back to the cloud default."""
    value = options.get(CONF_TRANSPORT) if hasattr(options, "get") else None
    return value if value in TRANSPORTS else DEFAULT_TRANSPORT
