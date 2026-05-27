# Mnemosyne

> *記憶の女神が、あなたのラボの構成を覚えておく。*

ホームラボのマシン構成を JSON で管理し、トポロジ図・サービスマップを自動生成するツール群です。

[CloseCraw](https://github.com/AideaHandesen-dvs/CloseCraw)（Prometheus アラート→AI診断エージェント）と同じ `inventory.json` を共有して動作します。

![Mnemosyne](assets/logo/logo.jpg)

## 概要

```
inventory.json          ← あなたの構成データ（非公開・.gitignore済み）
inventory.example.json  ← サンプル（架空データ）

collect/
  survey_the_worlds.sh  ← SSH で各ホストの情報を収集し inventory.json を更新

gen/
  build_topology.py     ← inventory.json → Mermaid トポロジ図 (L2)
  build_service_map.py  ← inventory.json → サービスマップ HTML (L3)
  deploy.sh             ← 生成物を Web サーバーへデプロイ
```

## セットアップ

### 1. リポジトリをクローン

```bash
git clone https://github.com/AideaHandesen-dvs/mnemosyne.git
cd mnemosyne
```

### 2. inventory.json を準備

サンプルをコピーして自分の環境に合わせて編集します。

```bash
cp inventory.example.json inventory.json
$EDITOR inventory.json
```

`inventory.json` は `.gitignore` に含まれているため、コミットされません。

### inventory.json のフィールド

| フィールド | 必須 | 説明 |
|---|---|---|
| `hostname` | ✓ | ホスト名（SSH 接続先として使用） |
| `os` | ✓ | OS 文字列（`OpenWrt` / `Windows` / その他 で分岐） |
| `ssh_user` | ✓ | SSH ログインユーザー名 |
| `ip` | ✓ | IPアドレス（`unknown` も可） |
| `status` | — | `"offline"` を設定するとスキップされる |
| `services` | — | サービス一覧（ポート・プロトコル等） |

→ 全フィールドの例は `inventory.example.json` を参照してください。

### 3. SSH の事前確認

`survey_the_worlds.sh` は SSH 公開鍵認証を前提としています。  
各ホストへパスフレーズなしでログインできることを確認してください。

```bash
ssh alice@nas   # パスワードなしで入れること
```

### 4. 収集スクリプトを実行

```bash
cd collect
bash survey_the_worlds.sh
```

スクリプトは `inventory.json` を読み込み、`status: "offline"` でないホストへ SSH して情報を収集します。

OS 判定ロジック：

| `os` フィールドの内容 | 判定 |
|---|---|
| `OpenWrt` を含む | `owrt`（dropbear 経由） |
| `Windows` を含む | `windows`（PowerShell コマンド） |
| それ以外 | `linux` |

### 5. 図を生成・デプロイ

```bash
cd gen
python3 build_topology.py   # → gen/L2_full_topology/full_topology.mmd
python3 build_service_map.py  # → gen/L3_service_map/service_map.html
bash deploy.sh              # → デプロイ先へコピー
```

デプロイ先は `deploy.sh` 内の変数で設定します（後述）。

## デプロイ先の設定

`gen/deploy.sh` の先頭にある変数を環境に合わせて変更してください。

```bash
# gen/deploy.sh
DEPLOY_DIR="/var/www/html/mnemosyne"   # ← 変更する
```

または `.env` ファイルで上書きすることもできます（`.env` も `.gitignore` 済み）。

```bash
# .env
DEPLOY_DIR="/srv/http/mnemosyne"
```

## 依存

| ツール | 用途 |
|---|---|
| `bash` | 収集スクリプト |
| `python3` | ノード一覧の読み込み・図生成 |
| `jq` | JSON 整形（オプション） |
| `ssh` + 公開鍵 | 各ホストへのログイン |

## ライセンス

MIT
