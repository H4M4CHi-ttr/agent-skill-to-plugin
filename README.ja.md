# Agent Skill to Plugin

[English](README.md)

Agent Skill to Pluginは、複数のAgent Skills環境から既存の有効な`SKILL.md`を安全寄りの手順で解決し、OpenAI skills-only pluginへ梱包して標準の個人用Marketplaceへ登録するツールです。取得元の解決、固定スナップショット、決定的な候補選択、検証、来歴、梱包、登録、レポートを分離し、リモートコンテンツは常に信頼できないデータとして扱います。

バージョン0.6.1は公開ベータです。生成された警告と元ライセンスを確認してから、インストールまたは再配布してください。

本ツールは独立したオープンソースプロジェクトであり、OpenAIまたはAnthropicの公式・提携製品ではありません。

## なぜSkillをPluginにするのか

本プロジェクトの大きな目的の一つは、再利用可能なAgent SkillをWorkやCodexだけでなく、通常のChatGPT Chatでも使える形にすることです。[OpenAIのPluginモデル](https://developers.openai.com/plugins/build/plugins)は、Skillにインストール可能な単位を与え、どのワークフローと同梱リソースが一組なのかをChatGPTとCodexへ伝えます。変換してインストールすれば、指示や同梱リソースを中心とするSkillを、agent型の作業画面だけに閉じず通常のChatからも利用できます。

ただしChatには重要な境界があります。Chatは会話を中心とする画面であり、汎用的なローカルファイルシステム環境ではありません。ユーザーのPC上にある任意のパスやリポジトリツリーを読み書きすることを、Chat向けワークフローの前提にしないでください。Chatでは会話、同梱リソース、ユーザーが明示的に提供したファイル、その画面で利用可能なツールを中心に設計します。ファイル中心の成果物にはWork、リポジトリやローカルファイルシステムを扱うワークフローにはCodexまたはCLIを使ってください。この区別は[OpenAIの現在の製品案内](https://learn.chatgpt.com/)に基づきますが、実際の利用可否は画面、ロールアウト、ワークスペースポリシーによって異なります。

2026年8月29日時点で、OpenAI公式ドキュメントが案内する[ローカル／リポジトリMarketplace Pluginのインストール・テスト経路](https://developers.openai.com/plugins/build/plugins)はChatGPTデスクトップアプリです。この機能を利用できるProなどの個人アカウントでも、本ツールが生成するローカルMarketplace出力については、現時点ではデスクトップ版を前提にしてください。ただし、公式ドキュメントには個人プラン別の対応表がないため、「Proだけの制限」または「すべての個人プランで常に同じ対応」とまでは確認できません。Public Pluginとworkspace-published Pluginは別の配布経路です。

## 解決する課題

Agent Skillは、リポジトリ内のディレクトリ、`npx skills add`、Claude Plugin、アーカイブ、ローカルフォルダーなど、異なる形で配布されています。本ツールはこれらを共通モデルへ正規化し、有効な`SKILL.md`を発見します。構造上の候補が一意でなければ選択を要求し、次を生成します。

- Skillだけを含むOpenAI Pluginディレクトリ
- ローカル実行時の標準個人用Marketplace登録
- JSON／Markdown変換レポート
- 次ターンの選択にも同じ取得物を使う固定済みResolution

来歴と明示的な配布依頼のため、決定的なZIPも内部生成します。通常の人間向け出力で表示するのは`--show-zip`を指定した場合だけです。バージョン付きJSON出力とJSON変換レポートには`zip_path`と`zip_sha256`を常に残しますが、MarkdownレポートとSkillの応答では明示的に求められない限り提示しません。

Claudeのcommands、agents、hooks、MCP、settingsなど、Skillではない機能を新しいSkillへ意味変換しません。

## 前提条件

- 推奨ランタイム: [`uv`](https://docs.astral.sh/uv/getting-started/installation/)。Skillが内部で利用し、対応するPythonとPyYAMLを隔離環境へ自動準備します
- 代替ランタイム: Python 3.10以上とPyYAML 6.x
- GitHub／GitソースにはGit
- 下記インストールコマンド、および`npx skills add`入力の変換にはNode.js／npm／npx
- 登録済みClaude Marketplaceの読取解決にはClaude CLI（任意）

公開リポジトリからSkillをインストールします。

```bash
npx skills add H4M4CHi-ttr/agent-skill-to-plugin
```

この`npx skills add`コマンドを、Agent Skill to Plugin本体の唯一の利用者向け配布経路とします。本Skillをインストールするための別個のZIPは公開しません。変換ツールが生成するアーカイブは変換後のPlugin成果物であり、Agent Skill to Plugin本体のインストーラーではありません。明示的に必要とされた場合だけ提示します。

`npx skills add`はSkillファイルをインストールしますが、`uv`自体はインストールしません。対応する実行画面で`uv`が利用できれば、Skillが内部で`uv run`を実行します。通常、ChatGPTやCodexからSkillを利用する人がPythonバージョンを選んだり、PyYAMLを導入したり、`uv`コマンドを入力したりする必要はありません。

開発時、またはPython代替経路を手動準備する場合:

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

Python実行時依存はPyYAML 6.xのみです。Pythonパッケージ情報と起動スクリプトのPEP 723インラインメタデータの両方で宣言しています。PyYAMLは同梱しません。推奨経路では`uv`が隔離環境へ解決し、代替経路ではPythonパッケージインストーラーが別途取得します。

Windows PowerShellでは上記`.venv/bin/python`を`.venv\Scripts\python.exe`へ置き換えるか、`.venv\Scripts\Activate.ps1`を実行してください。`npx.cmd`形式の入力も受理しますが、コマンドシェルへは渡しません。

## クイックスタート

### 通常のChatGPT／Codex Chatから

対応する画面へAgent Skill to Plugin Skill／Pluginをインストールした後の流れです。

変換ツール自体には、参照する取得元へのアクセス、`uv`（推奨）またはPython代替経路を実行できる環境、登録先へのローカルファイルアクセスが必要です。通常のChatだけ、またはクラウド実行環境からは、ユーザーPC上のパスを解決したり、`~/plugins`と`~/.agents/plugins/marketplace.json`へ書き込んだりできるとは限りません。到達可能なGitHub／アーカイブを使い、ローカルのDesktop／CodexまたはCLIで変換してください。画面によってはファイルを明示提供できますが、それだけでPC上の個人用Marketplaceへアクセスできるわけではありません。登録後のPluginはChatへインストールして利用できます。

1. 一つのインストールコマンド、GitHub URL、またはアクセス可能なローカルパスをローカルDesktop／CodexのChatへ貼り、変換を依頼します。
2. Skillが論理的依頼をUTF-8入力ファイルへ保存し、`--json`付きでツールを実行します。
3. `needs_selection`なら、番号、Skill名、パス、または「すべて」で選びます。Skillはブランチを再取得せず、保存済みResolutionから再開します。
4. ツールが検証済みPluginを`~/plugins/<plugin-name>`へ配置し、`~/.agents/plugins/marketplace.json`へ追加します。ローカル実行環境が要求した場合は、ファイル書込権限を許可します。
5. Skill一覧、登録状態、警告、来歴、JSON／Markdownレポートを確認し、対応する画面から個人用MarketplaceのPluginをインストールします。
6. 新しいChatを開き、最初の動作確認では取り込んだSkillを明示的に呼び出します。

このSkillはネットワーク取得とローカル取得ツールの実行を伴うため、明示呼び出し用に設定します。無関係な会話から暗黙に開始すべきではありません。

### CLI

UTF-8の`input.txt`へ一つの論理的なインポート依頼を書きます。

```text
npx skills add vercel-labs/agent-skills --skill web-design-guidelines
```

手動でCLIを使う場合は、推奨ランタイムで実行します。

```bash
uv run scripts/skill_to_plugin.py run \
  --input-file input.txt \
  --output-root converted-skills-marketplace \
  --json
```

`uv`を利用できず、Python 3.10以上とPyYAML 6.xを導入済みの場合は、`uv run`を`python`（または`python3`）へ置き換えます。

決定的な規則で一つに絞れれば、そのまま変換します。複数候補が残れば`status: needs_selection`、終了コード10、再開用JSONを返します。同じブランチを再取得せず、次のように再開します。

```bash
uv run scripts/skill_to_plugin.py convert \
  --resolution converted-skills-marketplace/resolutions/<resolution-id>.json \
  --select <candidate-id> \
  --json
```

`--select`には候補IDのほか、完全一致するSkill名、リポジトリ内パス、`all`を指定できます。複数を明示する場合は繰り返せます。

解決だけを先に行う場合:

```bash
uv run scripts/skill_to_plugin.py resolve \
  --input-file input.txt \
  --output-root converted-skills-marketplace \
  --json
```

インストール後の`agent-skill-to-plugin`コマンドも同じ機能です。

```bash
agent-skill-to-plugin run --input-file input.txt --output-root converted-skills-marketplace --json
```

ローカルの`run`と`convert`は、成功した変換を標準の個人用Marketplaceへデフォルトで登録します。このファイルは自動検出されるため、`codex plugin marketplace add`で追加しないでください。意図的にワークスペース生成物だけを作る場合は`--no-register-personal`を使います。登録はMarketplaceから選べる状態にする処理であり、Pluginのインストール／再インストールではありません。人間向け出力に生成ZIPのパスが明示的に必要な場合だけ`--show-zip`を使います。バージョン付きJSONとJSON変換レポートは自動化用の成果物メタデータを保持します。

梱包に成功した後で個人用登録だけが失敗した場合は、報告された権限、lock、競合を解決してから登録だけを再試行します。

```bash
agent-skill-to-plugin register-personal \
  --plugin-dir converted-skills-marketplace/plugins/<plugin-name>
```

このコマンドは、取得元の再解決、再梱包、Pluginのインストール／再インストールを行いません。個人用領域にある同名の異なる内容を調べ、置換を明示承認した場合だけ`--force-personal`を追加します。ワークスペース用の`--force`は、ホームディレクトリ側の置換を許可しません。異なる内容を強制更新した結果は`status: "updated"`、`reinstall_required: true`、`installation_performed: false`を返します。すでにインストール済みのキャッシュへ新しいファイルを反映する必要があれば、Pluginを明示的に再インストールします。

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

Chatは、貼り付けた裸のURLと明示的な`$agent-skill-to-plugin`呼び出しを、Skillへ渡す前にMarkdownリンクへ変換することがあります。一致するローカル呼び出しリンクは転送メタデータとして扱い、コマンド内の自動リンクは表示された取得元とリンク先が完全に同じ場合だけリンク先へ戻します。そのため一つの`npx`依頼に含まれたままとなり、二つ目の取得元にはなりません。表示とリンク先が異なるコマンド内Markdownリンクは拒否します。

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

Claude PluginはPlugin単位の指定なので、その境界に含まれる有効なSkillをデフォルトですべて一つのOpenAI Pluginへ含めます。非Skillコンポーネントはレポートするだけです。0.6.1では、相対パス、GitHub、Git、git-subdir、HTTPSアーカイブ、npmレジストリのPlugin sourceに対応します。npmはレジストリメタデータとtarballを直接取得し、npmやlifecycle scriptを実行しません。`command` sourceは拒否します。

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

ワークスペースには確認可能な変換記録を残します。

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

通常のローカル`run`／`convert`では、検証済みPluginを次の場所にも登録します。

```text
~/plugins/<plugin-name>/
~/.agents/plugins/marketplace.json
```

`packages/`配下のアーカイブは決定的な来歴用生成物です。人間向けCLI出力では、`--show-zip`を明示しない限りパスを表示しません。MarkdownレポートとSkillの応答でも、ユーザーがZIP／アーカイブ、配布バンドル、オフライン転送を求めた場合以外は提示しません。バージョン付きJSON出力とJSON変換レポートには、表示方法にかかわらずパスとSHA-256を保持します。

レポートには、正規化した取得元、要求ref／固定commit、取得物と生成物のハッシュ、Skill一覧、選択理由、ライセンス根拠、外部参照の処理、生成コピーへの互換性調整、互換性／セキュリティ診断を記録します。ライセンス検出は証拠収集であり、法的判断ではありません。

ツールは固定取得元スナップショットを変更せず、変換前にそのハッシュを再検証します。生成コピーも原則そのまま保持しますが、2026-08-29にローカルCodex環境同梱のOpenAI Pluginバリデータで観測したメタデータ差には限定的な例外があります。Front Matter終端`...`は`---`へ正規化します。取得元Skillが`disable-model-invocation: true`を使う場合、生成`SKILL.md`では`false`へ変更し、明示呼び出し限定の意図を表すため`agents/openai.yaml`へ`policy.allow_implicit_invocation: false`を書きます。このポリシーの実際の挙動はChatGPT／Codexの各画面・バージョンで別途確認が必要です。既存agent metadataも必要な場合、0.6.1の保守的allowlist（interfaceの表示／説明／icon／色／default prompt、`policy.allow_implicit_invocation`、`dependencies.tools`）へ限定します。default promptには`$skill-name`を含め、icon pathはPlugin内の実在ファイルに限定し、追加／削除／変更したfield pathを値なしで記録します。変更ファイル、理由、取得元ハッシュ、生成後ハッシュはJSON／Markdown両レポートの`compatibility_adaptations`へ記録します。

既存のワークスペース出力はデフォルトで上書きしません。`--force`は、そのワークスペース生成物を置き換えるためだけの明示的なオプトインです。個人用登録は別の境界です。同一内容なら冪等な成功として扱い、同名Pluginの内容またはMarketplace entryが異なる場合は`output_conflict`になります。個人用状態を確認し、置換を明示的に決めた場合だけ`--force-personal`で許可します。

## ChatGPT／Codexで使う

現行OpenAI Plugin仕様では`.codex-plugin/plugin.json`が必要で、Skillは`skills/<name>/SKILL.md`へ配置します。通常のローカル変換は、検証済みPluginを`~/plugins/<plugin-name>`へコピーし、標準の個人用Marketplace `~/.agents/plugins/marketplace.json`を更新します。Codexはこのファイルを暗黙に検出するため、このデフォルト経路では`codex plugin marketplace add`を実行しないでください。

登録は個人用MarketplaceからPluginを選べるようにするだけで、インストール／再インストールではありません。結果は`installation_performed: false`を記録し、強制更新時は`reinstall_required: true`も記録します。対応するChatGPT／Codex画面からPluginを明示的にインストールし、フラグが付いた更新では再インストールします。entryが表示されなければデスクトップアプリを再起動し、新しいChatで最初はSkillを明示的に呼び出して確認してください。ローカルMarketplaceの利用可否は画面ごとに異なります。最新手順は[OpenAI公式Pluginドキュメント](https://developers.openai.com/plugins/build/plugins)を確認してください。

本ツールは`codex plugin add`、Pluginのインストール／再インストール、公開、pushを実行しません。ホームディレクトリに残すことを意図した変更は、登録対象Pluginディレクトリと標準の個人用Marketplaceファイルに限定します。ただし登録処理では、個人用Marketplaceルートに所有者を確認するlockと復旧journalを置き、個人用Pluginルートに一時stage／backupを作ります。通常は削除しますが、cleanupやrollbackが完了しなかった場合は、所有者を推測して未知のデータを削除しないため、診断付きで残すことがあります。Chatだけ／クラウドだけの環境ではこのローカル登録を実行できないため、ローカルDesktop／CodexまたはCLIで変換してください。

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

`output_conflict`: ワークスペース生成物の競合なら、別の`--output-root`を使うか、確認後に競合物を整理するか、ワークスペースだけを置換する`--force`を使います。`~/plugins`配下の内容が異なる同名Pluginまたは個人用Marketplace entryの競合なら、報告された状態を確認し、置換を明示承認した場合だけ別の`--force-personal`を使います。`--force`は個人用領域の置換を許可しません。

梱包完了後に個人用登録だけが失敗した場合は、検証済みワークスペースPluginを残し、報告された権限、lock、journal、cleanup、競合を解決してから`agent-skill-to-plugin register-personal --plugin-dir <generated-plugin-dir>`で登録だけを再試行します。残されたlockや復旧journalは確認すべき証拠であり、推測で削除しないでください。異なる内容の置換には、明示承認後の`--force-personal`だけを使います。意図的に個人用Marketplaceを変更しない場合だけ`--no-register-personal`を使います。

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
