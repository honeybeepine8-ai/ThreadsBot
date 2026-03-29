# ThreadsBot クイックスタートガイド

## セットアップ手順

### 1. 事前準備

以下のAPIキー/トークンが必要:
- **Threads API**: Meta Developer Portal でアプリ作成 → OAuthトークン取得
- **Claude API**: Anthropic Console で APIキー取得
- **YouTube API**: Google Cloud Console で YouTube Data API v3 有効化 → APIキー取得

### 2. 環境構築

```powershell
# PowerShellで実行
cd C:\ThreadsBot
.\scripts\setup.ps1
```

手動の場合:
```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -r requirements.txt
cp .env.example .env
python scripts/init_data.py
```

### 3. 環境変数の設定

`.env` を編集して実際の値を設定:
```
THREADS_APP_ID=your_app_id
THREADS_APP_SECRET=your_app_secret
THREADS_ACCESS_TOKEN=your_token
THREADS_USER_ID=your_user_id
ANTHROPIC_API_KEY=sk-ant-...
YOUTUBE_API_KEY=AIza...
```

### 4. 動作確認

```bash
# 緊急停止の状態確認
python scripts/kill_switch.py status

# テスト実行
pytest tests/ -v

# 各エージェントの単体実行テスト
python -m core.scheduler researcher  # ネタ収集
python -m core.scheduler writer      # 投稿生成
python -m core.scheduler poster      # 投稿実行（※実際にThreadsに投稿される）
```

### 5. 本番運用開始

```bash
# daemon方式（推奨）
python -m core.scheduler all
```

---

## 運用コマンド

| 操作 | コマンド |
|------|---------|
| 全エージェント起動 | `python -m core.scheduler all` |
| 個別実行 | `python -m core.scheduler <agent名>` |
| 緊急停止 | `python scripts/kill_switch.py stop "理由"` |
| 停止解除 | `python scripts/kill_switch.py clear` |
| 状態確認 | `python scripts/kill_switch.py status` |
| テスト | `pytest tests/ -v` |
| データ初期化 | `python scripts/init_data.py --force` |

---

## 処理の流れ（1日の運用サイクル）

```
05:00  Analyst が前日の分析実行
06:00  Researcher がYouTubeからネタ収集
07:00  Writer がネタからキュー分の投稿を生成
07:30  Poster が朝の投稿を実行
12:15  Poster が昼の投稿を実行（★アフィリ優先）
18:00  Fetcher が朝・昼の投稿のメトリクス取得
20:00  Poster が夜の投稿を実行
21:30  Poster が深夜の投稿を実行（水木のみ）
※15分ごと Supervisor が全体監視
```
