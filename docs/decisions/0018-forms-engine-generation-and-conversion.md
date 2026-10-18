# ADR-0018: 帳票の基盤。Excel はシートの XML のセルの値だけを書き換え、PDF は LibreOffice で `convert(path) -> pdf` に閉じ、フォントは fontconfig で置き換える

- 状態: 採用（CLAUDE.md の技術スタックの記述を更新）
- 日付: 2026-10-18
- 関連: 仕様書 第6章「様式のバージョン管理」「生成履歴」、第7章 form\_templates・form\_mappings・generated\_documents、第9章 原則3、制約2・3・5・6、ADR-0007、ADR-0009、ADR-0011、T29、T31〜T38

## 背景

段階3では、入管の参考様式（Excel と Word）から PDF を作る。CLAUDE.md の技術スタックは「Excel 生成: openpyxl（既存ブックを開いて値だけ書く）」「Word→PDF: `convert(docx) -> pdf`」だった。
T29 で見本の様式（第1-17号〔Excel〕・第5-9号〔Word〕）を実際に処理して、次のことを確かめた。

## 出典（2026-10-18 取得）

| 資料 | 版 | URL | SHA-256 |
|---|---|---|---|
| 参考様式第1-17号 支援計画書（EXCEL） | 改正日の記載なし | https://www.moj.go.jp/isa/content/001459168.xlsx | `e5d6e4a65a6117b7bb8a1668d56712b3119e74baf19e150d253a8a7e79e68c81`（ADR-0007 と同じ） |
| 同 記載例（全部委託。PDF） | Last-Modified 2026-03-24 | https://www.moj.go.jp/isa/content/001459175.pdf | `e40ba9b8571eaf77d5215ea95485039bc2853fb85b14e5d6306ee57e10fee5a6` |
| 参考様式第5-9号 事前ガイダンスの確認書（WORD） | 改正日の記載なし（Last-Modified 2025-02-26） | https://www.moj.go.jp/isa/content/001341755.docx | `601617564016926dfcba0e557a2f9e6d190ac2ad1ec4b1cac2f945d47550588e`（ADR-0009 と同じ） |

## 確かめたこと

### 1. 第1-17号の中身と、openpyxl で開いて保存した後の違い

| 項目 | original | openpyxl 3.1.5 で開いて保存した後 |
|---|---|---|
| ブックの中のファイル | 24 個 | 14 個が落ちた: `xl/drawings/drawing1.xml`・`xl/worksheets/_rels/sheet1.xml.rels`・`xl/printerSettings/printerSettings1.bin`・`xl/sharedStrings.xml`（インライン化）・`xl/calcChain.xml`・`customXml/` の 9 個 |
| 図形（drawing） | 5 つ（大かっこ `bracketPair`。行 70〜74・142〜147・326〜330） | **0（落ちた）** |
| VML（vmlDrawing）・コントロール（ctrlProps）・画像 | なし | — |
| 入力規則（dataValidations・x14 の extLst） | なし | — |
| 印刷範囲 | `'参考様式第１－１７号'!$A$1:$BP$777` | 同じ |
| 改ページ | 手動 9 か所（81・153・236・311・392・465・546・630・709 行の後） | 属性が書き換わった（`min="0"` が付く） |
| 縮小・用紙 | A4 横・68%・`fitToHeight="0"`・`r:id`（プリンタの設定への参照） | `r:id` が落ちた |
| ヘッダーとフッター | なし | — |
| 結合セル | 843 | 同じ |
| シート・ブックの保護 | なし | — |
| 計算式 | DA670 の 1 つだけ（印刷範囲の外。中身は `#REF!`）と calcChain | calcChain が落ちた |

**1 つ以上が落ちる・変わるので、openpyxl での保存はやめた**（下の決定 1）。

### 2. LibreOffice での変換とフォント

- LibreOffice 7.4.7.2（Debian bookworm `libreoffice-writer-nogui`・`libreoffice-calc-nogui` 4:7.4.7-1+deb12u14）。
- 公式の様式が前提にしているフォント:
  - 第5-9号: ＭＳ 明朝。
  - 第1-17号: ＭＳ Ｐ明朝・ＭＳ Ｐゴシック。ブックの既定のフォントはＭＳ Ｐゴシック 11pt（テーマの minor。欧文は Calibri）。
  - 中国語の部分: SimSun。
  - 記載例の PDF: MS-PMincho・MS-PGothic・MS-Gothic・Calibri・HGP行書体。
- 第1-17号を空のまま変換した結果（ＭＳ Ｐゴシックの置き換え先を変えて比べた。ＭＳ Ｐ明朝は IPAP明朝）:

| ＭＳ Ｐゴシック → | Calibri → | 頁数 |
|---|---|---|
| IPAPゴシック | Carlito | 21（列が広くなり、各頁の右側が次の頁に送られた） |
| IPAPゴシック | IPAPゴシック | 21 |
| IPAゴシック（等幅） | Carlito | 11 |
| **Noto Sans CJK JP** | Carlito | **11（採用）** |
| Noto Sans CJK JP | Noto Sans CJK JP（明朝も Noto Serif CJK JP） | 11 |

- 列幅はブックの既定のフォント（ＭＳ Ｐゴシック）の数字の幅から計算されるので、その置き換え先で横幅が決まる。Noto Sans CJK JP は、1 頁目の列の配分と折り返しが記載例にいちばん近かった。
- **11 頁と記載例の 10 頁の差**: 最後の区切り（710〜777 行）の行の高さの合計は、公式のファイルの値で 753.5pt、68% で 512pt。A4 横から上下の余白を引いた印刷できる高さ（約 487pt）を超えるので、最後の「１号特定技能外国人の署名」の行だけが 11 頁目にあふれる。
  - これは公式のファイル自身の設定から来るもので、縮小を 66% まで下げても変わらなかった。
  - 記載例の PDF は記入済みの別のファイルなので、頁数の比べる相手にしない。
  - 完了条件 A の①は「公式の様式を空のまま同じ方法で変換したもの」との比較なので、11 頁どうしで比べる。
- **残っている見た目の差**: 第1-17号の「（電話」の文字がセルの幅で一部切れる。人による見比べ（完了条件 A の③）で確かめる。
- **Wingdings**: 第5-9号は箇条書きの定義（`numbering.xml`）でだけ Wingdings を指定していて、本文では使っていない（本文の番号は普通の文字）。変換した PDF の表示に影響は無かった。
- **チェックの記号**: マッピングの書式 `check` は ☑（U+2611）と □（U+25A1）を書く。
  - □ は IPA・Noto CJK のどちらにもある。
  - ☑ はどちらにも無く、LibreOffice は DejaVu Sans・OpenSymbol に代えて描く（`fc-list ":charset=2611"` で確かめた）。
  - 実際に ☑ を書く最初の様式（T31）で PDF を見て確かめ、崩れるなら置き換える文字をこの ADR に追記する。

### 未確認の点

- Microsoft Excel で第1-17号を空のまま印刷したときの頁数（Excel が無いので確かめていない。上の計算からは Excel でも 11 頁になる見込み）。
- ☑ の描画（上のとおり T31 で確かめる）。
- 中国語（簡体字）の様式の変換（T38）。

## 決定

### 1. Excel の様式はシートの XML のセルの値だけを書き換える（`services/form_xlsx.py`）

- ブック（zip）の中のファイルをそのまま写し、マッピング定義のセルのシートの XML の、そのセルの要素だけを書き換える。ほかのファイルはバイト単位で同じ。
- **共有文字列**: 文字はインライン文字列（`t="inlineStr"`）で書き、`sharedStrings.xml` は変えない。
- **セルの型**: 数値は `<v>` に書き、文字はインライン文字列。真偽値は受け付けない（書式で文字にする）。セルの書式（`s` 属性）は元のまま残す。行・セルの要素が無ければ、列の順に挿入する。
- **計算結果のキャッシュ**: 計算式を持つセルには書かない（エラー）。`calcChain.xml` と計算の設定（`calcPr`）には触れない。
- **書くセルを参照する計算式**（2026-10-18 に確かめた）: 第1-17号の original の計算式は次の 1 つだけで、マッピングで書くセル（O18・Q26・V26・Z26・BC26）を参照しない。名前の定義は印刷範囲（`_xlnm.Print_Area`）だけ、条件付き書式は無い。そのため、変換のときの再計算の設定（LibreOffice の OOXML の読み込み時の再計算を「常に」にする設定）は入れない。

  | 計算式のセル | 計算式（`<f>` の中身そのまま） | 参照している範囲 | 書くセルとの重なり |
  |---|---|---|---|
  | DA670（印刷範囲 A1:BP777 の外。キャッシュは `#REF!`） | `+#REF!:DA673BC671BC667:DA673AO6BC667:DA673` | DA673・BC671・BC667:DA673・AO6（`#REF!` は参照先なし） | 無し |

- **読み込みの検査**: 今後の様式で、計算式が参照するセルに書くと、PDF に古い計算結果（キャッシュ）が出る。そのため `make forms` の読み込みで、Excel の様式のマッピングの書き込み先が次のどれかに当たれば止める（`FormTemplateError`。`form_xlsx.targets_referenced_by_formulas`）。
  - すべてのシートの計算式が参照するセル・範囲（シート名つきの参照、列全体 `C:D`・行全体 `2:3` を含む。文字列の定数の中は除く）
  - 共有の計算式（`t="shared"`）は、親の式の参照を共有の範囲の大きさだけ広げて安全側に見る
  - 名前の定義（印刷範囲などの `_xlnm.` で始まるものを除く）が指す範囲。シート名の無い参照は、どのシートのセルとも重なるものとして見る
  - 参照の取り出しは字句の照合なので、関数名（例: `LOG10`）をセルと見誤って止まることはある（安全側）。止まったら、その様式のタスクで計算式を確かめ、再計算の設定を入れてからこの検査を緩める。
  - あわせて、書き込み先が書けるセルであること（計算式のセル・結合セルの左上以外でない）も読み込みで確かめる。
- **結合セル**は左上のセルにだけ書く（左上以外はエラー）。読み込みの後に `check_targets` でマッピング定義の書き込み先を確かめられる。
- **制約3 のテスト**（`tests/forms_engine/test_files.py`）: 書いた後に次が original と同じことを確かめる。
  - ブックの中のファイルの一覧、シート以外のファイルの中身（バイト単位）
  - 図形（drawing）、印刷の設定（pageSetup・余白・printOptions・ヘッダーとフッター・改ページ・印刷範囲）
  - 結合セル、入力規則・extLst・保護、calcPr
  - あわせて、書いたセルを openpyxl で読み戻して値を確かめる（openpyxl は確かめだけに使う）。

### 2. Word の様式は docxtpl で差し込む（`services/form_docx.py`）

- `template.docx` は original の写しに差込タグ（`{{ 名前 }}`）を置くことだけをしたもの。タグは既存の文字の区切り（run）の中に入れる。
- タグを外した本文が original の本文と同じことを、テストで確かめる。
- 第5-9号の見本では、実施日時の 1 行目・所属機関名・説明者の 3 か所に置いた。全欄は T32 で置く。
- **template.docx の作り方**: 開発用の道具 `tools/forms/make_5_9_template.py`（標準ライブラリだけ）で、original の `word/document.xml` を文字列の置き換えで書き換え、ほかの zip の中身はそのまま写して作った。**出力は手で直していない**（直した場合は、ここにその箇所を書く）。使い方と、出力が登録済みの template.docx とバイト単位で同じことはテスト（`test_template_5_9_is_reproduced_by_the_tool`）で確かめる。

  ```
  python3 tools/forms/make_5_9_template.py \
      templates/forms/5-9/2026-10-17/original.docx \
      templates/forms/5-9/2026-10-17/template.docx
  ```

  作り直したら manifest の `template_sha256` を合わせて `make forms` で読み込む（生成履歴がある版は置き換えられないので、新しい版として足す）。

### 3. PDF の変換は `convert(path) -> pdf` の 1 関数に閉じる（`services/form_convert.py`）

- LibreOffice headless を web・qcluster の共通のイメージに入れる（`backend/Dockerfile`）。e2e のイメージには、生成の画面ができる T31 で入れる。
- docx と xlsx を受ける。変換のたびに別のユーザープロファイルを使い（同時に動かしてもぶつからない）、時間の上限（`FORM_CONVERT_TIMEOUT`、120 秒）を設ける。
- PDF にパスワード・印刷禁止は付けない。
- 生成履歴に変換の環境（`soffice --version`）を残す。

### 4. フォント（`backend/config/fonts/99-form-fonts.conf`）

| 様式のフォント | 置き換え先 | パッケージ（Debian bookworm） |
|---|---|---|
| ＭＳ 明朝・MS Mincho | IPA明朝 | fonts-ipafont-mincho 00303-23 |
| ＭＳ Ｐ明朝・MS PMincho | IPAP明朝 | 同上 |
| ＭＳ ゴシック・MS Gothic | IPAゴシック | fonts-ipafont-gothic 00303-23 |
| ＭＳ Ｐゴシック・MS PGothic | **Noto Sans CJK JP**（列幅が合うため） | fonts-noto-cjk 1:20220127+repack1-1 |
| SimSun・宋体（中国語） | Noto Serif CJK SC | 同上 |
| Calibri | Carlito | fonts-crosextra-carlito 20220224-1 |

fontconfig は 2.14.1-4。パッケージの版を変えたら、空の様式の変換の頁数（テスト）と見た目を確かめ直す。

### 5. 様式マスタと版の選び方（`services/form_templates.py`）

- ファイルは `templates/forms/<様式ID>/<適用開始日>/`（`original.*`・`template.docx`・`manifest.json`）と、`mappings/<様式ID>/<適用開始日>.json`。読み込みは `make forms`（`load_form_templates`）。
- **改正日の分からない版の適用開始日は、ADR に記録した最も早い取得日**にする。第1-17号（ADR-0007）・第5-9号（ADR-0009）とも 2026-10-17。manifest に「改正日不明」と書く。
- **版の選び方**: 基準日以前に始まる最新の版。どの版の適用開始日より前でも警告は出さず、最も古い版を使う。生成履歴に、使った版が改正日不明である印（`revision_unknown`）を残す。
- **original の SHA-256** は manifest と一致し、読み込み済みの版から変えられない（止める）。**template とマッピング定義**は、その版で生成履歴がまだ無いときだけ置き換えられる（開発中の調整のため）。生成履歴がある版を変えるときは、新しい版として足す（制約5）。

### 6. 生成履歴（`apps/forms_engine`・`services/form_generation.py`）

- 生成はジョブ（Django-Q2。テストではその場で実行）。依頼 → 完了／失敗の変更だけを許し、その後は変更・削除できない。同じ内容の再生成は新しい行。
- データのスナップショットは暗号化の列（`EncryptedCharField`。TextField なので長さの上限なし）に JSON で持つ。監査ログでは伏せる。
- **失敗**したときは出力を残さず、失敗の理由を残す。理由は自前の例外なら文言、それ以外は例外の種類だけにし、個人情報を含めない。
- 監査ログ: 依頼（作成）・生成（完了・失敗。`generate`）・ダウンロード（`download`）・様式とマッピング定義の読み込み（作成・更新）。

### 7. ダウンロード

- 画面からダウンロードできるのは **PDF だけ**。`forms.generate`（管理者・担当者）と、対象の外国人を見る権限（`workers.view`）の両方が要る。
- URL は作らない（非公開のファイルの列。ADR-0011 と同じ）。
- 記入済みの docx・xlsx は保存するだけで、取り出しは段階4（F13）で決める。

## 影響

- CLAUDE.md の技術スタックを「Excel はシートの XML のセルの値だけを書き換える」「Word は docxtpl」「PDF 変換は `convert(path) -> pdf`（docx・xlsx）」に直した。
- イメージに LibreOffice とフォントが入り、大きくなる。初回の `make build` に時間がかかる。
- qcluster はコードの変更を自動では読み直さない。新しいアプリ・ジョブの関数を足したら `docker compose restart qcluster` が要る（README）。
- T29 では生成の画面を作らない。生成のボタン・e2e は T31、帳票タブは T33。

## 検討した代替案

- **openpyxl で開いて保存する**: 図形・プリンタの設定・customXml・calcChain が落ちる（上の表）。
- **変換だけの別のコンテナ**: CLAUDE.md の「Docker で同居」と違い、コンテナどうしの呼び出しが要る。
- **ＭＳ Ｐゴシックを IPAPゴシックに置き換える**: 字形は近いが、列幅が広くなり頁が倍になる。
- **変換のときだけ縮小率を下げて 10 頁に収める**: 66% でも変わらず、公式の設定を変えることにもなる。
