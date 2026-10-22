# ADR-0048: 帳票の字体の見直し（D4）。中国語の字形は run の言語で決まる・規則の無い字体に規則を足す・Liberation を入れる

- 状態: 採用（2026-10-22。計画 `.checks/phase4/fix3/plan.md` の D4-1〜D4-7 はおすすめのとおり。D4-1 に「かなを含む run は書き換えない」を足した）
- 日付: 2026-10-22
- 関連: ADR-0018（帳票の基盤。4. フォント）・ADR-0032（PDF の直前の写しの例外の一覧）・ADR-0027（日中併記）・ADR-0035（中国語の訳文）。PR #1（クラウドの切り分けと `make blank-pages`）。調べのデータ `.checks/research/phase4-D4_fonts_data/`

## 背景

クラウドのセッション（PR #1）で、空の様式を PDF にして「代わりの字体」を出す道具（`make blank-pages`）を作った。手元（web のイメージ。本番と同じ。Debian 12・LibreOffice 7.4.7.2）の結果では、規則の無い字体（Century・Times New Roman・Arial・SimHei・DengXian・Microsoft YaHei・Microsoft JhengHei）が DejaVu や IPAゴシックに落ち、SimSun（規則で Noto Serif CJK SC）なども `NotoSerifCJKjp` で描かれていた。

## 調べて分かったこと

1. **中国語の字形は、字体ではなく run の言語で決まる。** PDF に埋め込まれた字形のデータで比べると、SimSun・Noto Serif CJK SC の run は、言語が zh-CN なら簡体字の形、ja-JP（または指定なし。様式の文書の既定は ja-JP）なら日本の形だった。LibreOffice 24.2（e2e のイメージ）でも同じ。fontconfig の lang、LibreOffice の版、字体の面の選び方（TTC から SC の面だけを取り出した単独の字体も試した）は効かない。
   - PDF の名前は字形を表さない: Noto CJK の TTC は全部の地域の面で字形の表（CFF）を共有し、その名前は先頭の jp の面のもの。LibreOffice 7.4 は面によらずこの名前（`NotoSerifCJKjp`）を書く。
   - 当たるのは日中併記（1-5・1-6・5-8・5-9）の、言語が ja-JP・zh-TW の中国語の字体の run だけ（template.docx の漢字で 1-5 40 字・1-6 316 字・5-8 27 字・5-9 11 字）。**1-6 の zh-hans（全部が zh-CN）と 1-17 の zh-hans（xlsx。Calc は簡体字の形）は、もともと正しく描かれていた。**
2. 規則の無い中国語のゴシック体は、言語が ja だと IPAゴシック・IPA明朝（簡体字の無い字体）に落ちていた。
3. Century・Times New Roman・Arial は DejaVu Sans・Serif（Century より 8〜14% 広いゴシック体）になっていた。字の幅は Word に同梱の Century.ttf などを読んで比べた（配っていない）:
   | 候補 | Century との幅 | 行の高さ（win。Century 1.20） | 結果 |
   |---|---|---|---|
   | Liberation Serif | 88〜92% | 1.11 | 採用（Times New Roman と幅が同じ。頁が増えない） |
   | C059・TeX Gyre Schola | 100%（同じ） | 1.42・1.47 | 行が高く、1-6 日中併記・1-29 の頁が増えた |
   | IPAP明朝の欧文 | 98〜111% | 1.10 | 数字が 1 割広い |
4. 第1-6号 別紙１の折り返し（「円）」「日元）」「計）」）は、どの候補でも直らない。日本語の版は IPA明朝だけで描かれているので、欧文の字体は原因ではない。

## 決定

1. **中国語の字体の run の言語**（D4-1・D4-2）: PDF にする直前の一時の写しで、東アジアの字体が中国語の字体（SimSun・宋体・NSimSun・新宋体・SimHei・黑体・DengXian・等线・Microsoft YaHei・微软雅黑・KaiTi・楷体・FangSong・仿宋・Noto Serif／Sans CJK SC）の run の東アジアの言語を zh-CN にする（スタイル・既定の継承を見て決める。言語が zh-TW のものも zh-CN にする）。`services/form_docx.with_chinese_language`。生成（`form_generation`。`pdf_layout` の写しの後）と `make blank-pages` で使う。保存する文書は変えない（ADR-0032 の例外の一覧に足した）。
   - **ひらがな・カタカナを含む run は書き換えない**（日本語の文）。中黒「・」（U+30FB）は中国語の文でも区切りに使うので、かなとして数えない。該当は、日中併記の 55 頁の原本（4 つの様式で共通の 1 ファイル）の 3 つ（「栏中的3アd的“房间大小”」「イ」「アａ」。中国語の文が様式の項目の記号を指すもの）だけで、差し込みに使う template.docx・extracted.docx は 0 件。
   - 繁体字の字体（Microsoft JhengHei）の run は言語を変えない。
2. **規則を足す**（`backend/config/fonts/99-form-fonts.conf`）: SimHei・黑体・DengXian・等线・Microsoft YaHei・微软雅黑 → Noto Sans CJK SC、Microsoft JhengHei・微軟正黑體 → Noto Sans CJK TC、Times New Roman → Liberation Serif、Arial → Liberation Sans、Century → Liberation Serif（D4-3）。
3. **web のイメージ**に `fonts-liberation2`（2.1.5-1、4.3MB）と `fonts-dejavu-core`（2.37-6、3.0MB）を入れる。fontconfig の依存は DejaVu か Liberation のどれかでよいので、Liberation を入れると DejaVu が入らなくなる。DejaVu は字が無いときの行き先（※ Ⅰ など）なので明示した。
4. **e2e のイメージとクラウドを web に近づける**（D4-5）: e2e のイメージ（Ubuntu 24.04 の Playwright）は DejaVu が無く、WenQuanYi・Free・Unifont などがあった。`fonts-liberation2`・`fonts-dejavu-core` を入れ、web に無い字体を `backend/config/fonts/97-reject-fonts-not-in-web.conf`（rejectfont）で使わせない。クラウドは `tools/cloud/setup.sh` に `fonts-liberation2` を足し、SessionStart フックが同じ rejectfont の規則を写す。web のイメージには置かない（もともと無い）。LibreOffice の版の違い（e2e・クラウドは 24.2）は残る。
5. **blank-pages の道具**（D4-6）: Noto CJK の地域の名前（jp・sc・tc・kr・hk）を同じ字体として扱う。変換の前に 1 の書き換えを通し、書き換えた run とかなで残した run の数を出す。
6. □ ※ ○ Ⅰ〜Ⅸ ● （`w:hint="eastAsia"` の記号を LibreOffice が欧文の字体で描く）は、この直しに入れない（D4-4。後で直す一覧）。⑴〜⑿・㊞（IPA明朝に字が無い）は今のまま。

## 結果（直す前と後。`make blank-pages MD=1`、手元の web のイメージ）

- 頁数: 空の様式で変わったのは、差込のタグの文字が残る template.docx の 2 つだけ（1-6 日中併記 17 → 15、5-4 3 → 2。タグの文字が Century〔DejaVu Sans → Liberation Serif〕で狭くなったため）。生成物の第1-6号（日本語 4 頁・日中併記 8 頁）は変わらない。
- 代わりの字体（字の数）: 規則が無い 18,275 → 4（Malgun Gothic Semilight の「）」）、置き換え先で描かれない 73,599 → 379、欧文の字体で描かれた 989 → 605（記号。6 の件）、置き換え先に字が無い 90 → 138（⑴・㊞ と、Liberation に無い ①②）。

## 影響

- 帳票の見た目が変わる: 半角の数字・英字がセリフ体に、中国語のゴシック体が Noto Sans CJK に、日中併記の一部の中国語が簡体字の形になる。保存済みの生成物は変わらない（作り直したときから）。
- 言語を zh-CN にした run は、行の高さが少し変わる所がある（5-8 の題の行が少し下がった）。頁数は変わらなかった。
- イメージが約 7MB 大きくなる。`make build` と e2e のイメージの作り直しが要る。
- 配布が .doc の様式（1-4・1-25）を変換し直すと、styles.xml の文書の既定の欧文の字体と fontTable.xml に、環境の既定の字体（Liberation があれば Liberation、無ければ DejaVu）の名前が書かれる。保存してある converted.docx（と、それから作った template.docx）は Liberation の無い環境で作ったので DejaVu のまま。本文の XML は同じで、この既定の字体を使う字も無い（空の様式の PDF の埋め込みは IPA明朝だけ）ので作り直さない（生成履歴のある版の template を変えると、新しい版として足す決まりになる）。変換し直しのテストは、比べる前にこの名前をそろえる（`tests/forms_engine/doc_conversion.neutral_fonts`）。

## 未確認の点

- Word（Windows）で描いた PDF との見比べ（人の見比べで行う。PR の説明文の表）。
- 第1-6号 別紙１の折り返しの原因。見当: 文書の設定の「句読点の詰め」（`w:characterSpacingControl="compressPunctuation"`）で Word は全角の括弧・読点を詰めて 1 行に収めるが、LibreOffice は同じようには詰めていない（確かめていない）。
- LibreOffice 24.2（e2e・クラウド）と 7.4（web・本番）の、字体以外の組み方の違い。
