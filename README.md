# 放課後ノート

高校生向けの英語・数学の学習掲示板。Flask + Azure App Service F1 + Blob Storage。
要件は[requirements.md](./requirements.md)に記載しています。

公開サイト：[放課後ノート](https://sk1127ihb50609-azayg4h8ahchcqhc.japaneast-01.azurewebsites.net/)

## ローカル起動（Windows PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:SECRET_KEY = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:STORAGE_BACKEND = 'local'
$env:COOKIE_SECURE = 'false'
.\.venv\Scripts\python.exe app.py
```

http://127.0.0.1:5000 を開きます。ローカル用データはGit管理対象外の`instance/board.json`です。
ローカルモードは1プロセス専用です。Azureでは必ず`STORAGE_BACKEND=azure`を使います。
起動ごとに上記の秘密キーを作り直すとログイン状態と匿名IDが変わります。
継続して同じIDを使う場合は、一度生成した値を安全な環境変数に保管してください。

## Azure公開

既存ワークフロー[master_sk1127ihb50609.yml](./.github/workflows/master_sk1127ihb50609.yml)を利用します。
`master`へのpush → Python依存関係のインストール → テスト → 既存のOIDC認証 → App Service設定 → 公開の順です。
Pull Requestではテストのみ実行します。公開先は既存の`sk1127ihb50609`です。
配置後に`/healthz`とホームへ実際に接続して確認します。起動確認に失敗した場合は、
App Serviceの環境変数の値を隠したうえで起動ログを収集します。

### App Serviceの環境変数

接続文字列は**AzureポータルのApp Service → 設定 → 環境変数**にだけ入力してください。
GitHub・ソースコード・チャットには保存しません。`.env.example`は項目の説明用で、自動読み込みしません。

| 名前 | 設定内容 |
| --- | --- |
| `AZURE_STORAGE_CONNECTION_STRING` | 既存ストレージアカウントの接続文字列。値は非公開 |
| `SECRET_KEY` | 32文字以上のランダム値。公開ワークフローで未設定時のみ自動生成し、Azureに保持 |
| `STORAGE_BACKEND` | `azure`。ワークフローが設定 |
| `COOKIE_SECURE` | `true`。ワークフローが設定 |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true`。ワークフローが設定 |
| `AZURE_STORAGE_CONTAINER` | 任意。省略時`studyboard`。存在しなければ非公開で作成 |
| `AZURE_STORAGE_BLOB` | 任意。省略時`board.json` |

既存の接続設定が別名でも、公開用スクリプトはストレージアカウント`ih50609`の接続文字列を
App Serviceの環境変数・接続文字列設定から探し、正規の変数名へコピーします。値はログに出しません。
自動検出できない場合は、`AZURE_STORAGE_CONNECTION_STRING`を明示的に設定してください。
GitHubの連携IDには既存App Serviceを参照・設定変更・デプロイできる権限が必要です。

接続文字列の代わりに、`AZURE_STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net`と
App ServiceのマネージドIDも使用できます。その場合は対象ストレージに
`Storage Blob Data Contributor`ロールを付与してください。

起動コマンドは`bash startup.sh`です。ワークフローが設定します。
手動設定時はApp Service → 構成 → 全般設定（またはスタック設定）のスタートアップコマンドへ入力します。
Gunicornは1ワーカー・4スレッドで起動します。ローカルWindowsではWaitressを使用します。

### 秘密キーの取り扱い

- ストレージのアクセスキーを共有してしまった場合は、Azureポータルで該当キーを再生成し、App Serviceの接続設定を更新してください。
- `SECRET_KEY`を変更すると既存セッションが無効になり、スレッドの匿名IDも変わります。
- Blobコンテナーを公開しないでください。JSONにはパスワードハッシュ等を含みます。
- パスワードはscryptでハッシュ化します。平文では保存しません。

## データ保存と制約

非公開Blobの1つのJSONに、アカウント・スレッド・返信・投稿間隔の情報を保存します。
読み込んだデータと同じETagを指定して更新し、競合したら最新データで最大8回試行します。
新規Blobは上書き禁止で作成します。返信番号と投稿間隔は同じ更新内で確定します。
競合が解決しない場合は503を返し、既存データは上書きしません。

登録50人程度の試験公開向けです。毎回JSON全体を読み書きするため、大規模・長期利用向けの構成ではありません。
JSON上限は12 MiB、本文は1万文字、タイトルは120文字、投稿間隔は同一アカウントにつき10秒です。
一覧は20スレッド、スレッド本文は50投稿ごとに表示します。
データ上限に達すると更新を拒否します。既存データの自動削除はしません。
独自バックアップ・Blobバージョン管理を追加しません。既存のAzure側設定は変更しません。

認証試行制限はプロセス内のメモリーで保持するため、再起動でリセットされます。
Azureの既知のリバースプロキシを1段だけ信頼し、送信元IPを認証試行制限に使用します。
公開のまま運用する場合、登録数が想定を大きく超えないかと、Azureの使用量を確認してください。

入力内容による投稿削除・利用停止、投稿者による編集・削除は実装していません。
投稿失敗に備え、本文の下書きをそのタブのsessionStorageに保存します。パスワードは保存しません。
数式表示は固定バージョンのKaTeXをjsDelivrから取得します。CDNに接続できない場合は記法のまま表示されます。

## 動作確認

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

テスト対象：登録から投稿・検索・返信・再ログインまで、CSRF・XSS・文字数・投稿間隔、
匿名IDのスレッド内一貫性、再起動後のローカル保存、ページ分割、10件同時更新のETag競合と再試行、
Blob未作成とコンテナー欠落の区別、保存先障害時の応答。
Azure自体への接続はモック化しています。公開後は実環境で次を確認してください。

1. 公開URLでホームが表示される。
2. 登録→英語・数学の質問→別アカウントで返信→返信本文のキーワード検索ができる。
3. `$x^2$`や`$$\frac{1}{2}$$`が数式として表示される。
4. 投稿者ID・パスワードハッシュ・接続情報が公開画面やHTMLに含まれない。
5. App Serviceの再起動・再公開後も登録と投稿が残る。
6. `/healthz`はプロセスの稼働確認のみ。Blob接続の確認にはホーム表示・投稿を使う。

F1の上限や起動待ち時間は、ローカルテストでは検証できません。
公開確認用に、平方完成の解説スレッドを1件投稿しています。数式と返信番号の使用例も兼ねています。

2026-11-27の公開確認では、登録・投稿・返信・返信本文の検索・数式表示・幅390pxでの表示を確認しました。
再公開後のアカウント／投稿の保持と再ログインも確認済みです。
同時10件のホーム閲覧はすべてHTTP 200（確認時の最長157ms）でした。
これは小規模な動作確認の結果であり、継続的な負荷やF1上限到達時の性能保証ではありません。
