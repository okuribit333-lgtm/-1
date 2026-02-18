# SOL Auto Screener - 統合版

新規Solanaプロジェクトを自動発見 → スコアリング → Discord/Telegram/LINEに通知。
月額¥0、24時間稼働。

---

## できること

```
1. DexScreener 3系統から新規Solanaトークンを自動スキャン
2. オンチェーン・Twitter・Discord・GitHubの4ソースでスコアリング
3. 上位5プロジェクトに絞り込み
4. Discord / Telegram / LINE に同時通知
5. 実際の操作は人が判断（セキュリティ・ガス調整）
```

---

## Railway でデプロイする方法

### 1. GitHubにリポジトリを作る

```bash
cd sol-screener-final
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_NAME/sol-screener.git
git push -u origin main
```

### 2. Railwayアカウント作成

https://railway.app にアクセス → GitHubアカウントでサインアップ（無料）

### 3. プロジェクト作成

1. Railway ダッシュボード → 「New Project」
2. 「Deploy from GitHub repo」を選択
3. 作成したリポジトリを選択
4. 自動でビルドが始まる

### 4. 環境変数を設定

Railway ダッシュボード → 作成したサービス → 「Variables」タブ

以下を追加（使うものだけ）：

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxx/xxxxx
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=123456789
LINE_NOTIFY_TOKEN=xxxxx
```

### 5. Worker として設定

Railway ダッシュボード → サービス → 「Settings」タブ

- 「Start Command」を確認: `python main.py daemon`
- Web サービスではなく Worker として動く（HTTPポート不要）

### 6. デプロイ完了

環境変数を保存すると自動で再デプロイされる。
ログは Railway ダッシュボード → 「Deployments」→ 「View Logs」で確認。

---

## Railway の料金

- Trial Plan: 無料（$5分/月、500時間）
- Hobby Plan: $5/月（$5分のクレジット含む、十分動く）
- このBotの消費リソース: 月$1-2程度

---

## 通知先の設定方法

### Discord Webhook
1. Discordサーバー → サーバー設定 → 連携サービス → ウェブフック
2. 「新しいウェブフック」→ URLをコピー

### Telegram Bot
1. Telegram で @BotFather にメッセージ
2. `/newbot` → 名前入力 → トークン取得
3. ボットにメッセージ送信
4. `https://api.telegram.org/bot<TOKEN>/getUpdates` でChat ID確認

### LINE Notify
1. https://notify-bot.line.me/ にログイン
2. マイページ → トークン発行

---

## ローカルで動かす場合

```bash
# セットアップ
pip install -r requirements.txt
cp .env.example .env
nano .env  # 通知先を設定

# 1回実行
python main.py once

# デーモン（定期実行）
python main.py daemon
```

---

## ファイル構成

```
sol-screener-final/
├── main.py              # エントリーポイント & スケジューラー
├── src/
│   ├── config.py        # 設定管理
│   ├── scanner.py       # DexScreener 3系統スキャン
│   ├── scorer.py        # 4ソーススコアリング（Twitter=Nitter/BS4）
│   └── notifier.py      # 3チャネル通知（Discord Embed対応）
├── Procfile             # Railway用
├── railway.toml         # Railway設定
├── .env.example         # 環境変数テンプレート
├── requirements.txt     # 依存関係
└── README.md
```

---

## 統合で採用した部分

| 機能 | 採用元 | 理由 |
|------|--------|------|
| 3系統スキャン | v2 | 最新/ブースト/トレンドの3ルートでカバー範囲広い |
| 対数スケールスコア | v2 | 閾値ベースより精度が高い |
| BeautifulSoup Nitter | 元コード | 正規表現より堅牢にHTML解析できる |
| トークン名GitHub検索 | 元コード | URLなくてもGitHub存在チェックできる |
| Discord公開API | v2 | Bot不要でメンバー数/オンライン率が取れる |
| Discord Embed通知 | v2 | スコアバー・色分けで視認性が高い |
| APScheduler | v2 | cron不要でデーモン内で完結 |
| Railway対応 | 新規 | Git push → 自動デプロイ |
