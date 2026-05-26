#!/bin/bash
# deploy.sh - inventory.json から図を生成し、Web ディレクトリへ配置する
#
# Usage: ./deploy.sh
# cron例: 0 * * * * ~/mnemosyne/gen/deploy.sh >> ~/mnemosyne/gen/deploy.log 2>&1
#
# デプロイ先を変更するには DEPLOY_DIR を設定する:
#   export DEPLOY_DIR="/srv/http/mnemosyne"
#   または .env ファイルに記述する

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# --- 設定 ---
# .env があれば読み込む（inventory.json と同様に .gitignore 済み）
if [[ -f "$REPO_DIR/.env" ]]; then
    # shellcheck disable=SC1091
    source "$REPO_DIR/.env"
fi

INVENTORY="${INVENTORY:-$REPO_DIR/inventory.json}"
DEPLOY_DIR="${DEPLOY_DIR:-/var/www/html}"

L2_DIR="$REPO_DIR/gen/L2_full_topology"
L3_DIR="$REPO_DIR/gen/L3_service_map"

# L4 はオプション（ディレクトリが存在する場合のみ処理）
L4_DIR="$REPO_DIR/gen/L4_stream_flow"

# --- 事前チェック ---
if [[ ! -f "$INVENTORY" ]]; then
    echo "ERROR: inventory.json が見つかりません: $INVENTORY" >&2
    echo "  cp inventory.example.json inventory.json して編集してください。" >&2
    exit 1
fi

echo "=== deploy.sh $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "    REPO:    $REPO_DIR"
echo "    INVENTORY: $INVENTORY"
echo "    DEPLOY:  $DEPLOY_DIR"

# 1. pull
echo "[1/5] git pull..."
cd "$REPO_DIR"
git pull --ff-only

# 2. L2: full_topology.mmd 生成
echo "[2/5] Generating L2 full_topology.mmd..."
python3 "$L2_DIR/gen_topology.py" \
    --spec "$INVENTORY" \
    --out  "$L2_DIR/full_topology.mmd"

# 3. L3: service_map 生成
echo "[3/5] Generating L3 service_map..."
python3 "$L3_DIR/gen_service_map.py" \
    --spec   "$INVENTORY" \
    --outdir "$L3_DIR"

# 4. Web 配置
# 注意: inventory.json は個人データのため絶対にコピーしない
echo "[4/5] Deploying to $DEPLOY_DIR..."
sudo cp "$L2_DIR/full_topology.mmd"  "$DEPLOY_DIR/full_topology.mmd"
sudo cp "$L3_DIR/service_map.mmd"    "$DEPLOY_DIR/service_map.mmd"
sudo cp "$L3_DIR/service_map.html"   "$DEPLOY_DIR/service_map.html"

# L4 はオプション（ファイルが存在する場合のみコピー）
if [[ -f "$L4_DIR/stream_flow.mmd" ]]; then
    sudo cp "$L4_DIR/stream_flow.mmd" "$DEPLOY_DIR/stream_flow.mmd"
fi

echo "[5/5] Done."
echo "=== Finished $(date '+%Y-%m-%d %H:%M:%S') ==="
