# Agent Skill to Plugin

[English](README.md)

Agent Skill to Pluginは、複数のAgent Skills環境から既存の有効な`SKILL.md`を安全寄りの手順で解決し、OpenAI skills-only pluginへ梱包するSkill変換・梱包ツールです。取得元の解決、固定スナップショット、決定的な候補選択、検証、来歴、梱包、レポートを分離し、リモートコンテンツは常に信頼できないデータとして扱います。

バージョン0.5.0は公開ベータです。生成された警告と元ライセンスを確認してから、インストールまたは再配布してください。

本ツールは独立したオープンソースプロジェクトであり、OpenAIまたはAnthropicの公式・提携製品ではありません。

## なぜSkillをPluginにするのか

本プロジェクトの大きな目的の一つは、再利用可能なAgent SkillをWorkやCodexだけでなく、通常のChatGPT Chatでも使える形にすることです。[OpenAIのPluginモデル](https://developers.openai.com/plugins/build/plugins)は、Skillにインストール可能な単位を与え、どのワークフローと同梱リソースが一組なのかをChatGPTとCodexへ伝えます。変換してインストールすれば、指示や同梱リソースを中心とするSkillを、agent型の作業画面だけに閉じず通常のChatからも利用できます。

ただしChatには重要な境界があります。Chatは会話を中心とする画面であり、汎用的なローカルファイルシステム環境ではありません。ユーザーのPC上にある任意のパスやリポジトリツリーを読み書きすることを、Chat向けワークフローの前提にしないでください。Chatでは会話、同梱リソース、ユーザーが明示的に提供したファイル、その画面で利用可能なツールを中心に設計します。ファイル中心の成果物にはWork、リポジトリやローカルファイルシステムを扱うワークフローにはCodexまたはCLIを使ってください。この区別は[OpenAIの現在の製品案内](https://learn.chatgpt.com/)に基づきますが、実際の利用可否は画面、ロールアウト、ワークスペースポリシーによって異なります。

## 解決する課題

Agent Skillは、リポジトリ内のディレクトリ、`npx skills add`、Claude Plugin、アーカイブ、ローカルフォルダーなど、異なる形で配布されています。本ツールはこれらを共通モデルへ正規化し、有効な`SKILL.md`を発見します。構造上の候補が一意でなければ選択を要求し、次を生成します。

- Skillだけを含むOpenAI Pluginディレクトリ
- ローカルMarketplace
- トップレベルPluginディレクトリが一つだけのZIP
- JSON／Markdown変換レポート
- 次ターンの選択にも同じ取得物を使う固定済みResolution

Claudeのcommands、agents、hooks、MCP、settingsなど、Skillではない機能を新しいSkillへ意味変換しません。

## 前提条件

- Python 3.10以上
- GitHub／GitソースにはGit
- `npx skills add`入力に限りNode.js／npm／npx
- 登録済みClaude Marketplaceの読取解決にはClaude CLI（任意）

開発用インストール:

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

Python実行時依存はPyYAML 6.xのみです。PyYAMLはリリースZIPへ同梱せず、
パッケージインストーラーが上流ライセンスのもとで別途取得します。

Windows PowerShellでは上記`.venv/bin/python`を`.venv\Scripts\python.exe`へ置き換えるか、`.venv\Scripts\Activate.ps1`を実行してください。`npx.cmd`形式の入力も受理しますが、コマンドシェルへは渡しません。

アップロード可能で決定的なSkill ZIPは、取得元ツリー外へ生成します。

```bash
python -B scripts/build_skill_zip.py --output ../agent-skill-to-plugin-v0.5.0.zip
```

ビルダーはsymlinkとパス衝突を拒否し、build／cacheを除外し、ZIPが単一トップレベル`agent-skill-to-plugin/`だけを持つことを検証します。

## クイックスタート

### 通常のChatGPT／Codex Chatから

対応する画面へAgent Skill to Plugin Skill／Pluginをインストールした後の流れです。

変換ツール自体には、参照する取得元へのアクセスとPythonを実行できる環境が必要です。通常のChatからユーザーPC上のローカルパスを解決できるとは限りません。到達可能なGitHub／アーカイブを使う、画面が対応する場合は必要なファイルを明示的に提供する、またはWork、Codex、CLIで変換してください。生成したPluginは、その後Chatへインストールして利用できます。

1. 一つのインストールコマンド、GitHub URL、または現在の画面からアクセスできるローカルパスをChatへ貼り、変換を依頼します。
2. Skillが論理的依頼をUTF-8入力ファイルへ保存し、`--json`付きでツールを実行します。
3. `needs_selection`なら、番号、Skill名、パス、または「すべて」で選びます。Skillはブランチを再取得せず、保存済みResolutionから再開します。
4. Skill一覧、警告、来歴、JSON／Markdownレポート、ZIP SHA-256を確認します。
5. 生成したローカルMarketplaceを手動登録し、対応する画面からPluginをインストールします。
6. 新しいChatを開き、最初の動作確認では取り込んだSkillを明示的に呼び出します。

このSkillはネットワーク取得とローカル取得ツールの実行を伴うため、明示呼び出し用に設定します。無関係な会話から暗黙に開始すべきではありません。

### CLI

UTF-8の`input.txt`へ一つの論理的なインポート依頼を書きます。

```text
npx skills add vercel-labs/agent-skills --skill web-design-guidelines
```

```bash
python scripts/skill_to_plugin.py run \
  --input-file input.txt \
  --output-root converted-skills-marketplace \
  --json
```

決定的な規則で一つに絞れれば、そのまま変換します。複数候補が残れば`status: needs_selection`、終了コード10、再開用JSONを返します。同じブランチを再取得せず、次のように再開します。

```bash
python scripts/skill_to_plugin.py convert \
  --resolution converted-skills-marketplace/resolutions/<resolution-id>.json \
  --select <candidate-id> \
  --json
```

`--select`には候補IDのほか、完全一致するSkill名、リポジトリ内パス、`all`を指定できます。複数を明示する場合は繰り返せます。

解決だけを先に行う場合:

```bash
python scripts/skill_to_plugin.py resolve \
  --input-file input.txt \
  --output-root converted-skills-marketplace \
  --json
```

インストール後の`agent-skill-to-plugin`コマンドも同じ機能です。

## 対応入力

コードブロック、インラインコード、Markdownリンク、通常の文章に含まれる入力を解析します。一つのMarketplace追加コマンドと一つのPluginインストールコマンドは、一つのClaude Plugin依頼として扱います。

### `npx skills add`

```text
npx skills add vercel-labs/agent-skills --skill web-design-guidelines
```

```text
npx --yes skills@latest add https://github.com/vercel-labs/agent-skills/tree/main/skills/web-design-guidelines
```

`npx.cmd`、Bash／PowerShell／cmdの行継続に対応します。npmパッケージは正規の`skills`だけ、オプションは限定した許可リストだけを受理します。ユーザー指定のglobal／agentターゲットは除去し、一時プロジェクトへコピーして取得します。

### GitHubリポジトリ／Skillパス

```text
[https://github.com/mattpocock/skills](https://github.com/mattpocock/skills)
```

リポジトリルートに複数候補があれば`needs_selection`を返します。

```text
https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me
```

Skillディレクトリまたは`SKILL.md`を直接指定し、有効なSkillが一つなら質問せず選択します。branch、tag、commit SHA、URLエンコードされたパス、`/`を含むbranch名は、固定位置の文字列分割ではなくGit refに照合して解決します。

### Claude Plugin

```text
/plugin marketplace add mattpocock/skills
/plugin install mattpocock-skills@mattpocock
```

```text
claude plugin install skill-creator@claude-plugins-official
```

Marketplaceは、同じ入力のmarketplace add、読取専用の`claude plugin marketplace list --json`、既知の安全な対応表、Marketplace名とPlugin名の両方を検証する限定的なGitHub公開検索、の順で解決します。一意でなければMarketplaceのリポジトリまたはURLを求めます。

Claude PluginはPlugin単位の指定なので、その境界に含まれる有効なSkillをデフォルトですべて一つのOpenAI Pluginへ含めます。非Skillコンポーネントはレポートするだけです。0.5.0では、相対パス、GitHub、Git、git-subdir、HTTPSアーカイブ、npmレジストリのPlugin sourceに対応します。npmはレジストリメタデータとtarballを直接取得し、npmやlifecycle scriptを実行しません。`command` sourceは拒否します。

### ローカルソース

```text
C:\work\skills\my-skill
```

```text
./skills/my-skill
```

ローカルSkillディレクトリ、リポジトリ、`SKILL.md`、対応アーカイブを利用できます。相対パスは`--source-base`（デフォルトは現在のディレクトリ）を基準にします。GitHub shorthandにも見えるパスは、どちらかを明示して選びます。

ローカルリポジトリを入力にする場合、`--output-root`はそのリポジトリの
外側へ置いてください。取得元と出力の境界が重なる場合はコピー前に拒否し、
解決スナップショットや生成物を自己再帰的に取り込まない設計です。

### その他

- `owner/repo`形式のGitHub shorthand
- GitLab形式を含む一般的なGit URL
- HTTPS上の単体`SKILL.md`
- ZIP、tar、tar.gz、tgz

無関係な取得元が複数ある場合は`needs_input`を返します。勝手に統合せず、一つを選ぶか、提示された「すべてを統合」を明示的に選択してください。

## 候補選択

選択は意味的な好みではなく、ファイル構造、URLの具体性、Plugin／Skill境界、一般配置、パス距離、一意性に基づきます。候補が一つなら自動選択し、複数ならユーザーへ尋ねます。Front Matterが不正なSkillも黙って消さず、解析診断に残します。

Claude PluginだけはPlugin全体が依頼単位のため、境界内の有効なSkillをまとめて選択します。

ResolutionにはGit取得元なら固定commit、その他は取得物のハッシュを保存します。`convert`は再開前にスナップショットのハッシュを再検証します。

## 生成物

```text
converted-skills-marketplace/
├── .agents/plugins/marketplace.json
├── plugins/<plugin-name>/
│   ├── .codex-plugin/plugin.json
│   ├── skills/<skill-name>/SKILL.md
│   ├── skills/<skill-name>/agents/openai.yaml # 生成または保持する場合
│   └── THIRD_PARTY_LICENSES/                 # 該当時
├── packages/<plugin-name>.zip
├── reports/<plugin-name>.json
├── reports/<plugin-name>.md
└── resolutions/
    ├── <resolution-id>.json
    └── <resolution-id>.snapshot/
```

レポートには、正規化した取得元、要求ref／固定commit、取得物と生成物のハッシュ、Skill一覧、選択理由、ライセンス根拠、外部参照の処理、生成コピーへの互換性調整、互換性／セキュリティ診断を記録します。ライセンス検出は証拠収集であり、法的判断ではありません。

ツールは固定取得元スナップショットを変更せず、変換前にそのハッシュを再検証します。生成コピーも原則そのまま保持しますが、2026-08-29にローカルCodex環境同梱のOpenAI Pluginバリデータで観測したメタデータ差には限定的な例外があります。Front Matter終端`...`は`---`へ正規化します。取得元Skillが`disable-model-invocation: true`を使う場合、生成`SKILL.md`では`false`へ変更し、明示呼び出し限定の意図を表すため`agents/openai.yaml`へ`policy.allow_implicit_invocation: false`を書きます。このポリシーの実際の挙動はChatGPT／Codexの各画面・バージョンで別途確認が必要です。既存agent metadataも必要な場合、0.5.0の保守的allowlist（interfaceの表示／説明／icon／色／default prompt、`policy.allow_implicit_invocation`、`dependencies.tools`）へ限定します。default promptには`$skill-name`を含め、icon pathはPlugin内の実在ファイルに限定し、追加／削除／変更したfield pathを値なしで記録します。変更ファイル、理由、取得元ハッシュ、生成後ハッシュはJSON／Markdown両レポートの`compatibility_adaptations`へ記録します。

既存出力はデフォルトで上書きしません。`--force`は明示的な置換オプトインです。

## ChatGPT／Codexで使う

現行OpenAI Plugin仕様では`.codex-plugin/plugin.json`が必要で、Skillは`skills/<name>/SKILL.md`へ配置します。生成したローカルMarketplaceを手動登録します。

```bash
codex plugin marketplace add "<converted-skills-marketplaceの絶対パス>"
```

対応するChatGPT／Codex画面から生成Pluginをインストールします。Marketplaceが表示されなければデスクトップアプリを再起動し、新しいChatで最初はSkillを明示的に呼び出して確認してください。ローカル／リポジトリMarketplaceの利用可否は画面ごとに異なります。最新手順は[OpenAI公式Pluginドキュメント](https://developers.openai.com/plugins/build/plugins)を確認してください。

本ツールはMarketplace登録、Pluginインストール、公開、ホームディレクトリ変更を自動実行しません。

## セキュリティモデル

- `shell=True`を使わず、サブプロセスはargv配列で実行
- imported Skillのスクリプトを実行しない
- Claude `command` sourceを拒否
- npmをinstallせず、レジストリtarballを取得
- Git hooks／submodule取得を無効化
- URL埋め込み認証情報と危険なshell構文を拒否
- リダイレクト先を再検証し、private／local networkを拒否したうえで、検証済みpublic IPへ接続を固定（元hostnameのTLS証明書検証は維持）
- アーカイブ、パス、symlink／reparse point、衝突、件数、サイズ、深さを検証
- 既存の外部Skill参照は固定snapshot内からだけコピーし、未解決参照は明示警告、escape／unsafe targetは失敗
- 秘密鍵、`.env`、credentialらしいファイルを拒否
- README、Skill本文、説明をResolverへの命令として扱わない
- 保存前にプロセス出力とエラーをサニタイズ

これらは取得・梱包リスクを低減しますが、後で呼び出したSkillの無害性を保証しません。インストール前に`SKILL.md`、同梱ファイル、警告、来歴、配布元を確認してください。詳しくは[docs/security-model.md](docs/security-model.md)と[SECURITY.md](SECURITY.md)を参照してください。

## ライセンス

生成Pluginには第三者の元コンテンツが含まれます。本ツールはLICENSE／COPYING／NOTICE、Skill Front Matter、Claude Manifest、Resolverのライセンス情報を探し、レポートし、場合により同梱します。不明なら再配布権を確認できない旨を警告します。利用・再配布が元ライセンス上許可されるかは利用者が確認してください。

Agent Skill to Plugin本体はApache-2.0です。このライセンスは取得したSkillを再ライセンスしません。

## 意図的な非対応

- Claude commands／agents／hooks／MCP／settings／LSP／live artifacts／monitors／依存関係の意味変換
- Claude `command` sourceの実行
- 製品固有指示の自動書き換え（上記の形式上必要なOpenAIメタデータ正規化を除く）
- 静的スキャンによる安全認定
- private repository用credentialの作成・変更

## トラブルシューティング

`needs_selection`（終了10）: JSONの候補`id`を`convert --select`へ渡します。対応するsnapshotを削除しないでください。

`needs_input`（終了11）: 取得元やMarketplaceを一意に決められません。要求されたリポジトリ／URLを追加するか、返された選択肢から指定してください。

`dependency_missing`: 取得元に必要な実行ファイルを導入します。GitHub／GitにはGit、npx入力にはNode.js／npm／npxが必要です。

`authentication_failed`: 既存のGit／SSH認証をツール外で確認してください。tokenをURLへ埋め込まないでください。

`security_rejected`: 診断を確認してください。command source、credential付きURL、危険なパス、symlink、秘密情報らしいファイルを回避するバイパスは用意していません。

`output_conflict`: 別の`--output-root`を使うか、内容を確認して競合物を整理するか、置換する意図がある場合だけ`--force`を使います。

スペースを含むWindowsパスは`--input-file`を使うか引用符で囲みます。相対パスが曖昧なら`--source-base`を明示してください。

ローカルリポジトリ入力で`relationship: destination_within_source`の
`output_conflict`が出た場合、出力先が取得元ツリー内です。リポジトリ外の
兄弟ディレクトリなどを`--output-root`へ指定してください。

## 開発

通常CIと同じ外部ネットワーク非依存のテスト:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions workflowはWindows、Linux、macOSで同テストを実行する定義です。live smoke testは任意で、通常CIの契約には含めません。

[CONTRIBUTING.md](CONTRIBUTING.md)、[docs/architecture.md](docs/architecture.md)、[docs/source-resolution.md](docs/source-resolution.md)も参照してください。

## 旧エントリポイント

未公開の旧プロトタイプ向けラッパーを残しています。

```bash
python scripts/pluginize.py --command-file command.txt --output-root converted-skills-marketplace --json
```

新規連携は`skill_to_plugin.py`または`agent-skill-to-plugin`を使い、schema version付きJSONを処理してください。
