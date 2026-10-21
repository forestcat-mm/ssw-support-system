# 一次資料の一覧

制度・様式の根拠に使った一次資料（入管・e-Gov・NIST・字体の配布元）。資料そのものはリポジトリに入れず、手元の作業の場所（`.checks/sources/`。git の外）に取得の記録（HTTP の応答の頭）といっしょに置いてある。クラウドのセッションで要るときは、URL から取り直し、SHA-256 で同じ版かを確かめる（入管のページは予告なく差し替わる。違えば新しい版として扱い、ADR の出典を見直す）。

取得は User-Agent を付け、間をあけて 1 件ずつ行う（CLAUDE.md の「制度の根拠」）。取得日は応答の頭の Date（日本時間）、記録が無いものはファイルの日付。

| 資料名 | URL | 取得日 | SHA-256 | 使う ADR |
|---|---|---|---|---|
| 支援委託契約書（参考様式第5-10号）記載例 | https://www.moj.go.jp/isa/content/001341759.pdf | 2026-10-18 | `bac94e81628e339b4ff28fee04dac722f73340a32feca64250dd16f6a0ba1855` | 0026 |
| 特定技能外国人受入れに関する運用要領（本体） | https://www.moj.go.jp/isa/content/001468647.pdf | 2026-10-18 | `ba0dbeaf1f1d8e7e7ee7b15e4314f6db589d64ddf1b614b4adb9912f73e19d4d` | 0009・0010・0011・0012・0015・0016・0017・0018・0026・0031・0035・0036 |
| 中国語の様式（10 様式の日中併記。Word） | https://www.moj.go.jp/isa/content/001339744.docx | 2026-10-19 | `9f8a6619ed8ff86cac20bfc193d242e77ce24ff9b8786b58ac6c3ad2641ddb37` | 0009・0027・0035 |
| 在留申請オンライン 操作説明書（所属機関等の職員用） | https://www.moj.go.jp/isa/content/001369283.pdf | 2026-10-19 | `9a04771d38f13589d7bcc1bf3e7e16d3725ebdee6208e4b5626939900abf71ce` | 0033・0039 |
| 健康診断個人票の留意点 | https://www.moj.go.jp/isa/content/001370297.pdf | 2026-10-19 | `06be12e9a5632315679ff27c1c5b695af36120e48bd5f7091bdf53f016a0a620` | 0011・0031 |
| 在留申請オンライン システム更改のお知らせ | https://www.moj.go.jp/isa/content/001449073.pdf | 2026-10-19 | `7af8bd06e5a5bab283083fe691e12b80e74fd32c466273860d1d4f498a9b897c` | 0033 |
| 支援計画書（第1-17号）記載例（全部委託） | https://www.moj.go.jp/isa/content/001459175.pdf | 2026-10-19 | `e40ba9b8571eaf77d5215ea95485039bc2853fb85b14e5d6306ee57e10fee5a6` | 0018・0020・0032 |
| 特定技能 参考様式の掲載ページ | https://www.moj.go.jp/isa/applications/ssw/10_00020.html | 2026-10-19 | `279bc64cf06fb7750757a3c4cc04548f2fa4fe9aae2aa7eb693069c77aac209c` | 0007・0009・0015・0016・0021・0023・0026・0027・0035・0036・0041・0043 |
| 在留申請オンライン 所属機関等の方の案内 | https://www.moj.go.jp/isa/applications/online/10_00114.html | 2026-10-19 | `abe451c9983a011eeabb3df058d54f7a15c203d693f17b5382495e470a1a9b7e` | 0033 |
| オンライン申請の案内 | https://www.moj.go.jp/isa/11_00064.html | 2026-10-19 | `5ded983868dc0c0c6546ec8501047ace35dac750f973a613ab3dd8ecdda7e1ce` | —（計画・調べで使う） |
| 在留申請オンライン よくある質問 | https://www.moj.go.jp/isa/applications/online/online-QA.html | 2026-10-19 | `7e6cd86d7adea6cd39f9551f152b17eb26f5cc58f2fcdc6c46cec992d38ad050` | 0033 |
| NIST SP 800-63B-4（認証） | https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf | 2026-10-19 | `d9ab89576b5ae2cfb3ba37781110788dea27a85a5079496dbfc1a79834670afd` | 0030 |
| 出入国管理及び難民認定法施行規則（e-Gov 法令 API） | https://laws.e-gov.go.jp/api/1/lawdata/356M50000010054 | 2026-10-20 | `974b13acf8b5239c886f1dbe7aefc1d03afdf5ae96a5e0c4d32bee399aa815fa` | 0017・0036 |
| 参考様式第1-4号 記載例 | https://www.moj.go.jp/isa/content/001338920.pdf | 2026-10-20 | `0af3ad6137b2d9c0699292d75fa550e537b19add1e7486b9b62ba84ed8740d40` | 0023 |
| 参考様式第1-3号 健康診断個人票 | https://www.moj.go.jp/isa/content/001338973.docx | 2026-10-20 | `d8c89f00b8bdef8e5c612c5b8e1a1bdb4218741ae294553a0d3294b9d1a7dc31` | 0011 |
| 参考様式第1-10号 技能移転に係る申告書 | https://www.moj.go.jp/isa/content/001338979.docx | 2026-10-20 | `67a87dcc7110c249c8ef7f9932b44831c5362e07def3deafa182f1d227b16e13` | —（計画・調べで使う） |
| 参考様式第1-11-1号 特定技能所属機関概要書 | https://www.moj.go.jp/isa/content/001338980.docx | 2026-10-20 | `05f0e9516cabda90dcc30ec11eecb73812ac64722398641d72a7ed23e16e5fc0` | 0038 |
| 参考様式第1-16号 雇用の経緯に係る説明書 | https://www.moj.go.jp/isa/content/001338986.docx | 2026-10-20 | `51d36c7cf5fc597318010d1f8759595cfd11d35850232c9e7c13a9714b2aa800` | 0043 |
| 参考様式第5-7号 報酬支払証明書（.doc） | https://www.moj.go.jp/isa/content/001340546.doc | 2026-10-20 | `0c95fa446cdd0a19095f9c56cfc0bbee6fa04bfad76ec3e27a2b3402ae01877a` | —（計画・調べで使う） |
| 電子届出 一括申込用ツール 特定技能雇用契約に係る届出 | https://www.moj.go.jp/isa/content/001344485.zip | 2026-10-20 | `71d8a0a390f0c42e9d6b4db19517295c530cc19f7da2375abc3a760957bdf6cd` | 0040 |
| 同 支援計画変更に係る届出 | https://www.moj.go.jp/isa/content/001344486.zip | 2026-10-20 | `e5152a375ab1ef2fb0ccfee6542330cd7aa457c120502ad62f82155edc2406d3` | 0040 |
| 同 支援委託契約に係る届出 | https://www.moj.go.jp/isa/content/001344487.zip | 2026-10-20 | `d757a6341defe4a7fee725ce409f0bf3096088bf6a4f2b2a4f7705f41c6ccb79` | 0040 |
| 同 受入れ困難に係る届出 | https://www.moj.go.jp/isa/content/001344488.zip | 2026-10-20 | `db01799dd453e0943240be7f33482984c1ae1b629666fc067d1f1d5cf9bb889b` | 0040 |
| 電子届出システム 操作マニュアル 第1.8版 | https://www.moj.go.jp/isa/content/001344694.pdf | 2026-10-20 | `84e5817e0e11a9e331688a6324a46a0261483c24d9365869913758d0d9d18d28` | 0040 |
| 電子届出システムに関する Q&A（制度編） | https://www.moj.go.jp/isa/content/001344706.pdf | 2026-10-20 | `75bd3205307236c8a0e450e13832e2b84342a5121bd564833b5c6883ce08b10f` | 0040 |
| 一括申込用 Excel ツール利用マニュアル 第1.3版 | https://www.moj.go.jp/isa/content/001346468.pdf | 2026-10-20 | `9a1afeebde1053df34fc72ab502a202cf282931e143836709eabdb9b925eecd8` | 0040 |
| 同 基準不適合に係る届出 | https://www.moj.go.jp/isa/content/001436520.zip | 2026-10-20 | `59c7916b245303a8e934acad3d28e55cb76fa4700d1478c09ee4772ff59a9b90` | 0040 |
| 同 支援計画の実施困難に係る届出 | https://www.moj.go.jp/isa/content/001436521.zip | 2026-10-20 | `07d6c8bac0f428898c545c1db2a4c36db33b77604d311f9209b7bd8036fe99e0` | 0040 |
| 同 支援計画の実施における特異事案報告 | https://www.moj.go.jp/isa/content/001436522.zip | 2026-10-20 | `f26f4d434f425ec36366d0755c4d104639beb450430dd4b01c94d7b1ad79aa9e` | 0040 |
| 「電子届出システム」の操作に関する Q&A | https://www.moj.go.jp/isa/content/001452966.pdf | 2026-10-20 | `bb2021c3c933c2fdf6d07293197da8854746a345afb2932386448ac862f44ee8` | 0040 |
| 一括登録用ツール ご利用いただくにあたって | https://www.moj.go.jp/isa/content/001453106.xlsx | 2026-10-20 | `336276235c1b915232ae4343f9a1091949e74dd455ad020352a4df36b23282e0` | —（計画・調べで使う） |
| 一括登録用ツール（在留資格認定証明書交付申請） | https://www.moj.go.jp/isa/content/001453107.xlsm | 2026-10-20 | `9dcde391a0916b0226eb1bc9515e4aa0eb2d2e7c9ff8095555b87d9483d795bc` | 0038・0039 |
| 一括登録用ツール（在留期間更新許可申請） | https://www.moj.go.jp/isa/content/001453108.xlsm | 2026-10-20 | `1bc640105860dfa8962aab10f517d61fbff59fed1173089ac8344af48f611731` | 0038・0039 |
| 一括登録用ツール（在留資格変更許可申請） | https://www.moj.go.jp/isa/content/001453109.xlsm | 2026-10-20 | `528dfdf2228e613065cd8d87a1f0ca7043c16ca7350f94193af85ac66ea33148` | 0038・0039 |
| 参考様式第3-6号 受入れ・活動・支援実施状況に係る届出書 | https://www.moj.go.jp/isa/content/001454511.xlsx | 2026-10-20 | `a64dc8c7df1bf285fa82b97517cc2da602dcbbdb590a44867f3c002bb3910c72` | 0012・0041 |
| 同 別紙1 | https://www.moj.go.jp/isa/content/001454512.xlsx | 2026-10-20 | `02fa686a8ea8bd1e2cbc24d1e7ccd7b1d61ffb28f7d08ed5e08b586f8b587d88` | 0012・0041 |
| 同 別紙2 | https://www.moj.go.jp/isa/content/001454513.xlsx | 2026-10-20 | `6d1a1f6957d9c5471051a246f5a983be2a3119c7abbffaf3efdbd8cc89a0b2c0` | 0012・0041 |
| 参考様式第3-6号 作成要領 | https://www.moj.go.jp/isa/content/001454514.pdf | 2026-10-20 | `42dee1f62df91d1ce2a14c963ce496511717e07ed0523bbd73ec19060f8bb6ff` | 0012・0040・0041 |
| 参考様式第1-26号 公的義務履行に関する誓約書 | https://www.moj.go.jp/isa/content/001459166.docx | 2026-10-20 | `5e6295e4da25c4d98e1379de867ca2b33cd41c7b10862bb9a6c7a355a64c09eb` | 0033 |
| 電子届出 一括届出用テンプレート（案内） | https://www.moj.go.jp/isa/applications/online/i-ens_faq_manual_00002.html | 2026-10-20 | `dae424dde5ed1f46f9626869b634939b98faee92a820efdda98bcb0d4c11b57b` | 0040 |
| 電子届出 よくある質問・マニュアル | https://www.moj.go.jp/isa/applications/online/i-ens_faq_manual.html | 2026-10-20 | `59fd6f93189778f4a9a4b21384d31b1f527d8961b060b80c1122122178eea918` | 0040 |
| 電子届出システム ポータルサイト | https://www.moj.go.jp/isa/applications/online/i-ens_index.html | 2026-10-20 | `c19dc1617c7394ab1f9e8eccf7ef54379c18e2bfe026f8cbe3c4c3018afd9156` | —（計画・調べで使う） |
| 出入国在留管理庁 更新情報 | https://www.moj.go.jp/isa/publications/newslist/index.html | 2026-10-20 | `9c9558c8d270e08552f28226256c9ed2cd97d10a70b516da575ab86c9683d68d` | —（計画・調べで使う） |
| 特定技能所属機関・登録支援機関による届出（ページ） | https://www.moj.go.jp/isa/applications/ssw/nyuukokukanri10_00002.html | 2026-10-20 | `6bd9a98abd64e1025a7a367ee6c0586432ef48d6c5a57f024b2d6e0cf2ee60c6` | —（計画・調べで使う） |
| 在留申請オンライン「各種様式」 | https://www.moj.go.jp/isa/applications/online/online_guidance.html | 2026-10-20 | `941bbc8ea3024055a108ff8ffe3bc2f36abbf860544ca447ec41533db2c14df9` | 0039 |
| 参考様式第1-23号 役員に関する誓約書 | https://www.moj.go.jp/isa/content/001440288.docx | 2026-10-21 | `b9ba7c167c8dbf145632be70be52c77b0d69c3a393db6d265494a1a2dd9292fa` | 0043 |
| 参考様式第1-29号 書類省略に当たっての誓約書 | https://www.moj.go.jp/isa/content/001459167.docx | 2026-10-21 | `b4517804cc51476b19e09f18edbdaa85ba46a508f0894475dec30159cebf14e1` | 0043 |
| 提出写真の規格（ページ） | https://www.moj.go.jp/isa/applications/status/photo_info_00002.html | 2026-10-21 | `b51591567adb305823613021bf26386e92a1e2245bbc70959f7afeee25827c99` | —（画面デザインの見直し U4 の計画で使う） |
| Noto Sans JP（可変の TTF。Google Fonts の配布） | https://raw.githubusercontent.com/google/fonts/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf | 2026-10-21 | `c2f3b4d463500a2ddcd3849cded1fceeb9fd6d1c32e6cbecd568453ba50fc68f` | 0046 |
| Noto Sans SC（可変の TTF。Google Fonts の配布） | https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf | 2026-10-21 | `a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da` | 0046 |
| SIL Open Font License 1.1（JP・SC 同じ文） | https://raw.githubusercontent.com/google/fonts/main/ofl/notosansjp/OFL.txt | 2026-10-21 | `1c05c68c34f9708415aada51f17e1b0092d2cea709bf4a94cd38114f9e73d7d9` | 0046 |

様式の original（`templates/forms/` の版ごとのフォルダ）は、リポジトリにある（manifest の `source` に URL・取得日・SHA-256）。この一覧はそれ以外の資料。
