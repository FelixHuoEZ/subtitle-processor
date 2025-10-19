#!/usr/bin/env bash
set -euo pipefail

# 更新 firefox_profile/ 下的 Firefox cookies，并给出 YouTube cookies 过期提醒。
# 使用方式：
#   ./scripts/update_firefox_cookies.sh               # 默认阈值 7 天
#   WARN_DAYS=3 ./scripts/update_firefox_cookies.sh   # 自定义过期提醒阈值（单位：天）

WARN_DAYS=${WARN_DAYS:-7}

PROFILE_BASE="${HOME}/Library/Application Support/Firefox"
PROFILES_DIR="${PROFILE_BASE}/Profiles"
PROFILES_INI="${PROFILE_BASE}/profiles.ini"
export PROFILES_INI

if [[ ! -f "${PROFILES_INI}" ]]; then
  echo "ERROR: 未找到 ${PROFILES_INI}，请确认 Firefox 是否已安装并运行过。" >&2
  exit 1
fi

# 使用 Python 解析 profiles.ini，定位 Default=1 的配置
DEFAULT_RELATIVE_PATH=$(python3 - <<'PY'
import configparser
import os

cfg = configparser.RawConfigParser()
cfg.read(os.environ['PROFILES_INI'])

default_path = None
for section in cfg.sections():
    if cfg.has_option(section, 'Default') and cfg.get(section, 'Default') == '1':
        default_path = cfg.get(section, 'Path')
        break

if not default_path:
    raise SystemExit('Default profile not found in profiles.ini')

print(default_path)
PY
)

PROFILE_PATH=${FIREFOX_PROFILE_PATH:-${DEFAULT_RELATIVE_PATH}}

# 根据取到的路径解析出绝对目录
resolve_profile_path() {
  local path="$1"
  if [[ -z "${path}" ]]; then
    return 1
  elif [[ "${path}" == /* ]]; then
    printf '%s' "${path}"
  elif [[ "${path}" == Profiles/* ]]; then
    printf '%s' "${PROFILE_BASE}/${path}"
  else
    printf '%s' "${PROFILES_DIR}/${path}"
  fi
}

SOURCE_PROFILE_DIR="$(resolve_profile_path "${PROFILE_PATH}")"

# 如果默认 profile 没有 cookies，尝试自动寻找 *.default-release
if [[ ! -f "${SOURCE_PROFILE_DIR}/cookies.sqlite" && -z "${FIREFOX_PROFILE_PATH:-}" ]]; then
  CANDIDATE=$(find "${PROFILES_DIR}" -maxdepth 1 -type d -name '*.default-release' | head -n 1)
  if [[ -n "${CANDIDATE}" && -f "${CANDIDATE}/cookies.sqlite" ]]; then
    echo "WARN: 默认 profile 未包含 cookies.sqlite，改用 ${CANDIDATE}" >&2
    SOURCE_PROFILE_DIR="${CANDIDATE}"
  fi
fi

if [[ ! -d "${SOURCE_PROFILE_DIR}" ]]; then
  echo "ERROR: 未找到默认配置目录 ${SOURCE_PROFILE_DIR}" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_ROOT="${REPO_ROOT}/firefox_profile"
TARGET_PROFILE_DIR="${TARGET_ROOT}/$(basename "${SOURCE_PROFILE_DIR}")"

mkdir -p "${TARGET_ROOT}"

mkdir -p "${TARGET_PROFILE_DIR}"
FILES_TO_COPY=(cookies.sqlite cookies.sqlite-wal cookies.sqlite-shm key4.db)

COPIED=0
echo "INFO: 正在同步必要的 Firefox cookies 文件:"
for file in "${FILES_TO_COPY[@]}"
do
  if [[ -f "${SOURCE_PROFILE_DIR}/${file}" ]]; then
    echo "      ${file}"
    cp "${SOURCE_PROFILE_DIR}/${file}" "${TARGET_PROFILE_DIR}/${file}"
    COPIED=1
  fi
  done

COOKIES_DB="${TARGET_PROFILE_DIR}/cookies.sqlite"
if [[ ${COPIED} -eq 0 || ! -f "${COOKIES_DB}" ]]; then
  echo "WARN: 未找到可用的 cookies.sqlite，可能需要重新登录或指定正确的 profile。" >&2
  exit 0
fi

export COOKIES_DB WARN_DAYS

python3 - <<'PY'
import os
import sqlite3
from datetime import datetime, timezone

cookies_db = os.environ['COOKIES_DB']
warn_days = float(os.environ['WARN_DAYS'])

TARGET_COOKIES = {
    'youtube': [
        '__Secure-1PAPISID',
        '__Secure-1PSID',
        '__Secure-3PAPISID',
        '__Secure-3PSID',
        'SAPISID',
        'HSID',
        'SID',
        'SSID',
        'SIDCC',
    ],
}

conn = sqlite3.connect(cookies_db)
try:
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in TARGET_COOKIES['youtube'])
    cursor.execute(
        f"""
        SELECT name, expiry
        FROM moz_cookies
        WHERE host LIKE '%youtube.com%'
          AND name IN ({placeholders})
        """,
        TARGET_COOKIES['youtube'],
    )
    rows = cursor.fetchall()
finally:
    conn.close()

if not rows:
    print("WARN: 未找到常用 YouTube 登录 cookies，可能尚未登录。")
    raise SystemExit(0)

latest_name, latest_expiry = max(rows, key=lambda item: item[1])

if latest_expiry > 1e12:
    latest_expiry //= 1000

if latest_expiry <= 0:
    print("WARN: 读取到的 YouTube 登录 cookie 时间戳无效。")
    raise SystemExit(0)

try:
    expiry_dt = datetime.fromtimestamp(latest_expiry, tz=timezone.utc)
except (OverflowError, ValueError):
    print(f"WARN: YouTube 登录 cookie {latest_name} 取得异常时间戳 {latest_expiry}，已跳过提醒。")
    raise SystemExit(0)

now = datetime.now(tz=timezone.utc)
delta = expiry_dt - now
delta_days = delta.total_seconds() / 86400

print(f"INFO: 参考 cookies（{latest_name}）到期时间 (UTC): {expiry_dt:%Y-%m-%d %H:%M:%S}")
print(f"INFO: 距离到期还有约 {delta_days:.2f} 天")

if delta_days <= warn_days:
    print(f"WARN: {latest_name} 剩余时间不足 {warn_days} 天，建议尽快刷新登录。")
PY

echo "INFO: Firefox cookies 更新完成。"
