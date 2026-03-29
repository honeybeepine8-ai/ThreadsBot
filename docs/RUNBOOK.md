# ThreadsBot 障害復旧手順書（RUNBOOK）

## 1. Threads API 認証切れ

**検知方法:** Poster/Fetcherの401エラー、トークン期限通知

**復旧手順:**

1. Meta Developer Console（https://developers.facebook.com/）にログイン
2. アプリ設定 → Threads API → アクセストークンを再発行
3. `.env` の `THREADS_ACCESS_TOKEN` を新しいトークンで更新
4. `data/state/token_state.json` の `expires_at` を更新
5. 動作確認:
   ```bash
   python -m core.scheduler poster
   ```
6. 正常に投稿できることを確認

**予防:** Supervisorがトークン期限7日前にTelegram通知を送信する。

---

## 2. Claude API レート制限

**検知方法:** Writer/AnalystのCLIタイムアウトまたはエラー

**復旧手順:**

1. Anthropic Console で使用量を確認
2. リセットまで待機（通常は数時間〜1日）
3. 一時的に呼び出し回数を抑制:
   ```yaml
   # config/settings.yaml
   writer:
     max_generation_attempts: 2  # 5→2に一時変更
   ```
4. 必要に応じてサブスクプラン変更（$100→$200）

---

## 3. YouTube API quota 超過

**検知方法:** Researcherの403エラー

**復旧手順:**

1. Google Cloud Console で quota使用量を確認
2. 太平洋時間0:00の自動リセットを待つ
3. 緊急時は別プロジェクトのAPIキーに切替:
   ```
   # .env
   YOUTUBE_API_KEY=new_key_here
   ```

---

## 4. JSON 破損

**検知方法:** StateManagerのパースエラー、ログの `Failed to load`

**復旧手順:**

1. 破損ファイルをバックアップ:
   ```bash
   # 例: post_queue.json が破損した場合
   mv data/state/post_queue.json data/state/post_queue.json.bak.$(date +%Y%m%d_%H%M%S)
   ```
2. StateManagerが次回アクセス時にデフォルト値で自動再作成する
3. 必要に応じてバックアップからデータ復旧:
   ```bash
   # 直近のバックアップを展開
   cd data/backups
   unzip -l state_YYYYMMDD_HHMMSS.zip  # 中身を確認
   unzip -o state_YYYYMMDD_HHMMSS.zip post_queue.json -d ../state/
   ```

---

## 5. プロセスクラッシュ

**検知方法:** heartbeat 30分未更新、外部監視アラート

**復旧手順:**

1. ログで直近のエラーを確認:
   ```bash
   tail -20 data/logs/threadsbot.log
   ```
2. サービスを再起動:
   ```powershell
   nssm restart ThreadsBot
   ```
3. OOMの場合は `system_state.json` の `daily_counters` を確認して進捗を把握
4. ダッシュボードで状態を確認:
   ```bash
   python scripts/dashboard.py
   ```

---

## 6. 炎上・苦情

**検知方法:** コンプラリスクコメント通知、手動発見

**復旧手順:**

1. **即座に停止**:
   ```bash
   python scripts/kill_switch.py stop "炎上対応"
   ```
2. Threadsアプリから該当投稿を手動削除
3. 必要に応じて謝罪投稿を**手動**で作成（Botからは投稿しない）
4. 原因分析:
   - `data/state/post_history.json` で該当投稿の内容を確認
   - 該当表現を `config/ng_words.txt` に追加
5. 再発防止策を実施した後に再開:
   ```bash
   python scripts/kill_switch.py clear
   ```

---

## 7. コンプラリスクコメント検知

**検知方法:** Telegram通知 `[COMPLIANCE]`

**復旧手順:**

1. 通知内容を確認し、コメントの文脈を把握
2. 重大度に応じて対応:
   - **中（ステマ疑惑・信頼性疑惑）:** 投稿内容を確認し、必要に応じてPR表記追加
   - **高（法令違反指摘）:** 該当投稿を確認、必要に応じて手動削除
   - **最高（通報・訴訟）:** 即停止 → 手順6に従う
3. 該当コメントに対する返信は**手動で**行う（Bot返信しない）

---

## 8. フォロワー急減

**検知方法:** Supervisorのフォロワー急減検知（5%以上の減少）

**復旧手順:**

1. ダッシュボードで現状確認:
   ```bash
   python scripts/dashboard.py
   ```
2. 直近の投稿内容を確認し、不適切な投稿がないかチェック
3. 原因が特定できない場合は一時停止して様子見:
   ```bash
   python scripts/kill_switch.py stop "フォロワー急減調査"
   ```
4. Threads側のスパム判定の可能性もあるため、Meta Developer Consoleも確認

---

## 緊急連絡先

| 用途 | 連絡先 |
|------|--------|
| Threads API | Meta Developer Support |
| Claude API | Anthropic Support |
| YouTube API | Google Cloud Support |

## コマンドリファレンス

```bash
# 緊急停止
python scripts/kill_switch.py stop "理由"

# 停止解除
python scripts/kill_switch.py clear

# ダッシュボード
python scripts/dashboard.py

# レビュー
python scripts/review.py

# バックアップ
python scripts/backup.py

# ログ確認（直近のエラー）
python -c "import json; [print(json.dumps(json.loads(l),indent=2)) for l in open('data/logs/threadsbot.log') if '\"ERROR\"' in l or '\"CRITICAL\"' in l]"
```
