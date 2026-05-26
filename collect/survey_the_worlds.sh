#!/usr/bin/env bash
# survey_the_worlds.sh
# inventory.json からホスト一覧を動的に読み込む版
#
# 変更点: NODES 配列のハードコードを廃止し、inventory.json を参照するように変更
# -----------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INVENTORY="${SCRIPT_DIR}/../inventory.json"

if [[ ! -f "$INVENTORY" ]]; then
  echo "ERROR: inventory.json が見つかりません: $INVENTORY" >&2
  echo "  cp inventory.example.json inventory.json して編集してください。" >&2
  exit 1
fi

# -----------------------------------------------------------------------
# inventory.json → "hostname:type:user" の行リストに変換
# OS 判定:
#   os に "openwrt" → owrt
#   os に "windows" → windows
#   それ以外        → linux
# status == "offline" のホストはスキップ
# -----------------------------------------------------------------------
NODES=$(python3 - <<PYEOF
import json, sys

with open('$INVENTORY') as f:
    spec = json.load(f)

for h in spec:
    if h.get('status') == 'offline':
        continue
    os_str = h.get('os', '').lower()
    if 'openwrt' in os_str:
        t = 'owrt'
    elif 'windows' in os_str:
        t = 'windows'
    else:
        t = 'linux'
    user = h.get('ssh_user', 'root')
    print(f"{h['hostname']}:{t}:{user}")
PYEOF
)

# -----------------------------------------------------------------------
# ここから下は既存のループ処理をそのまま流用する
# 変数 NODES の形式: "hostname:type:user" （1行1ホスト）
# -----------------------------------------------------------------------
while IFS=: read -r HOST TYPE USER; do
  echo "=== $HOST ($TYPE) ==="

  case "$TYPE" in
    owrt)
      # --- OpenWrt (dropbear) ---
      # TODO: 既存の owrt 収集コマンドをここに移植
      ssh -o StrictHostKeyChecking=no "${USER}@${HOST}" \
        'ubus call system board' || true
      ;;

    windows)
      # --- Windows (PowerShell over OpenSSH) ---
      # TODO: 既存の windows 収集コマンドをここに移植
      ssh -o StrictHostKeyChecking=no "${USER}@${HOST}" \
        'powershell -Command "Get-ComputerInfo | Select-Object CsName,OsName,TotalPhysicalMemory"' || true
      ;;

    linux)
      # --- Linux (通常の ssh) ---
      # TODO: 既存の linux 収集コマンドをここに移植
      ssh -o StrictHostKeyChecking=no "${USER}@${HOST}" \
        'uname -a && free -m && df -h' || true
      ;;
  esac

done <<< "$NODES"

echo "Done."
