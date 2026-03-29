# アカウントセットアップガイド

## 手順

### 1. テンプレートからアカウントを作成
```bash
python scripts/create_account.py <account_id>
```

### 2. 必須ファイルをカスタマイズ（★マーク付き）
1. `.env` — Threads APIトークンとユーザーIDを設定
2. `config/tone.yaml` — ペルソナ・口調を定義
3. `knowledge/profile.yaml` — アカウントプロフィールを定義
4. `knowledge/target.yaml` — ターゲット層を定義
5. `knowledge/genre.yaml` — ジャンル・カテゴリを定義
6. `knowledge/theme_tree.yaml` — テーマ階層を設計（30〜50ノード）
7. `knowledge/domain_knowledge.md` — ジャンルの専門知識を記載
8. `config/ng_words.txt` — ジャンル固有のNGワードを追加

### 3. 任意ファイルのカスタマイズ
- `config/schedule.yaml` — 投稿スケジュール調整
- `config/affiliate_products.yaml` — アフィリエイト商品登録
- `knowledge/debate_whitelist.yaml` — 議論テーマ設定
- `prompts/writer.md` — ライタープロンプト調整
- `prompts/replier.md` — リプライヤープロンプト調整

### 4. テスト実行
```bash
python scripts/preflight.py --account <account_id>
python -m core.scheduler writer --account <account_id>
```

### 5. 運用開始
```bash
python -m core.scheduler all --account <account_id>
```

## ディレクトリ構造
```
<account_id>/
├── .env                          # API credentials
├── config/
│   ├── settings.yaml             # アカウント固有オーバーライド
│   ├── tone.yaml                 # ペルソナ・口調
│   ├── schedule.yaml             # 投稿スケジュール
│   ├── ng_words.txt              # NGワード
│   ├── brand_names.txt           # ブランドNG
│   ├── cosmetic_claims_56.txt    # 許可表現
│   └── affiliate_products.yaml   # 商品カタログ
├── knowledge/
│   ├── profile.yaml              # プロフィール
│   ├── target.yaml               # ターゲット
│   ├── genre.yaml                # ジャンル
│   ├── theme_tree.yaml           # テーマ階層
│   ├── domain_knowledge.md       # 専門知識
│   ├── posting_rules.md          # 投稿ルール
│   ├── hook_stock.json           # フックストック
│   ├── season_matrix.yaml        # 季節マトリクス
│   └── debate_whitelist.yaml     # 議論テーマ
├── prompts/
│   ├── writer.md
│   ├── replier.md
│   ├── researcher.md
│   ├── analyst.md
│   └── compliance_check.txt
└── data/
    ├── state/                    # 自動生成
    ├── analytics/                # 自動生成
    ├── archive/                  # 自動生成
    └── backups/                  # 自動生成
```
