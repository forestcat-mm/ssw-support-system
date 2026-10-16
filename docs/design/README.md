# デザインの比較セット（T13a）

業務画面のデザイントークンを 5 セットで見比べるための資料。1 つに決めたら、不採用のセットのファイルとスクリーンショットを削除する。

## 見方

- 管理者でログインし、http://localhost:8000/ui/ を開く。上の「デザインの比較」のタブ（A〜E）で切り替える（`/ui/?theme=b` のように URL でも選べる）。
- 切り替えはこの画面だけに当たる。ほかの業務画面は決まるまで A。admin にはテーマを当てない。
- スクリーンショットは `make design-shots` で撮り直せる（1366px 幅と 390px 幅のページ全体、1 倍解像度）。色を見比べる資料なので、減色による圧縮はしていない。

| セット | 元 | トークン | PC 幅 | 390px 幅 |
|---|---|---|---|---|
| A | Aboard（DESIGN.md） | `tokens.css` | [a-desktop.png](a-desktop.png) | [a-mobile.png](a-mobile.png) |
| B | [ElevenLabs](https://styles.refero.design/style/031056ff-7af1-46db-8daa-115f731c5d26) | `tokens-b.css` | [b-desktop.png](b-desktop.png) | [b-mobile.png](b-mobile.png) |
| C | [Workable](https://styles.refero.design/style/0ab4c544-6147-4998-8365-3a0f6191e54f) | `tokens-c.css` | [c-desktop.png](c-desktop.png) | [c-mobile.png](c-mobile.png) |
| D | [Officevibe](https://styles.refero.design/style/ced1c98f-d489-48f7-a01f-1fa59a07b706) | `tokens-d.css` | [d-desktop.png](d-desktop.png) | [d-mobile.png](d-mobile.png) |
| E | [Glide](https://styles.refero.design/style/a80cb179-2d90-4896-b52d-4e674b421498) | `tokens-e.css` | [e-desktop.png](e-desktop.png) | [e-mobile.png](e-mobile.png) |

トークンのファイルは `backend/apps/core/static/ui/` にある。

## 仕組み

- `base.css`・`components.css` は役割の変数（`--button-primary-bg`、`--level-overdue-bg` など）だけを参照する。クラス名はセットによらず同じ。
- B〜E のファイルは、`tokens.css` と同じ役割の変数をすべて定義している（`--palette-*` はセットごとの名前付きの色で、数が違ってよい）。テスト `tests/ui/test_layout.py` で、変数の抜けがないことを確かめている。
- 見本画面では `tokens.css` の後ろに選んだセットのファイルを重ねて読み込む。

## 警告レベルの色

B〜E で定義した低彩度の赤・琥珀・緑は、色相を揃えている（HSL で赤 4°、琥珀 38°、緑 145°）。明度と彩度だけを各セットのキャンバスに合わせた。A と、C の 7日以内・14日以内は、依頼で指定された色をそのまま使う。

| セット | 超過（地 / 文字） | 7日以内（地 / 文字。太字＋左の印） | 14日以内（地 / 文字） | 対応期間中（枠線） | 受付前（文字） |
|---|---|---|---|---|---|
| A | Blush `#fbcfe8` / `#262626` | Butter `#ffe77a` / `#262626` | Mint Foam `#b6edee` / `#262626` | `#aeaeaf` | `#757577` |
| B | `#f2dfde` / `#76322d` | `#f1e1c6` / `#6d4e17` | `#d9e8df` / `#2e563f` | `#a59f97` | `#777169` |
| C | `#f8e4e2` / `#7e2b25` | ピーチ `#fef1e1` / `#0f161e` | バター `#fde8ce` / `#0f161e` | ティール `#004038` | `#6f7073` |
| D | `#f7e0de` / `#862d27` | `#f8e5c3` / `#745011` | `#d6ebdf` / `#255639` | 濃紺 `#0c1754` | `#969696` |
| E | `#f2d6d4` / `#7e2b25` | `#f4dcb3` / `#6b490f` | `#c9e3d4` / `#224f34` | 黒 `#000000` | `#5c5c5c` |

地と文字のコントラスト比は、B〜E の赤・琥珀・緑のすべてで 5.9:1 以上。

## 依頼に指定がなく、こちらで決めた値

- **B の分野の色**: 工業 `#e1e7e9`、飲食 `#e9e3ed`。どちらも低彩度で、警告レベルの琥珀と紛れない色相にした。
- **E の分野の色**: 工業 `#d6e0e6`、飲食 `#e2dbe8`。B と同じ考え方。
- **取り返しのつかない操作のボタン**: B と E は、低彩度の赤の文字と枠で塗りなし（B は色付きボタンなし、E は塗りを黒だけにするため）。C と D は低彩度の赤の地。
- **ナビの選択中**
  - B: ストーンの地。
  - C: ティールの薄い地。
  - D: 濃紺のナビの上に白い地とコバルトの文字。
  - E: 地は #edede8、左にティールの印。
- **E のフォーカス**: 黒い枠の外側にティールの 3px の輪を出す。ティールだけでは白地で見えにくいため。

## 決めるときの注意

- **C の 7日以内と 14日以内**: ピーチ `#fef1e1` とバター `#fde8ce` は、色だけでは見分けにくい。文字と、7日以内の太字・左の印で区別する。
- **D の補足の文字**: `#969696` は白地とのコントラストが 3.0:1 で、12px の補足の文字には弱い（WCAG AA の基準は 4.5:1）。D を採るなら、補足の文字を濃くすることを検討する。
- **D の分野の色**: 指定どおり 2 分野ともラベンダーなので、文字だけで区別する。
