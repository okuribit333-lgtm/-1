# Railway デプロイガイド

このプロジェクトを Railway にデプロイする手順は以下の通りです。

## 1. 準備
- [Railway](https://railway.app/) のアカウントを作成します。
- GitHub にこのプロジェクトをプッシュします。

## 2. Railway での新規プロジェクト作成
1. Railway ダッシュボードで **"New Project"** をクリックします。
2. **"Deploy from GitHub repo"** を選択し、リポジトリを選択します。

## 3. 環境変数の設定
Railway の **"Variables"** タブで、`.env.example` を参考に必要な変数を設定してください。
特に以下は重要です：
- `DISCORD_WEBHOOK_URL` (通知用)
- `HELIUS_API_KEY` (推奨)

## 4. 永続化ストレージ（Volume）の設定
このアプリは `data/` フォルダに状態を保存します。Railway では再起動時にファイルが消えてしまうため、以下の設定を行ってください。
1. プロジェクトの **"Settings"** タブ、または **"Data"** セクションから **"Volume"** を作成します。
2. ボリューム名を `sol-screener-data` とします（`railway.toml` の設定と一致させる必要があります）。
3. マウントパスを `/app/data` に設定します。

## 5. デプロイの実行
設定が完了すると、自動的にビルドとデプロイが開始されます。
`railway.toml` により、自動的に `python main.py daemon` が実行されます。

## 注意事項
- **Free Plan (Trial)** の場合、21日間の使用制限や、スリープが発生する可能性があります。
- ログは Railway ダッシュボードの **"Logs"** タブで確認できます。
