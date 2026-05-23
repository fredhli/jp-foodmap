# 同期設定ガイド

お気に入りレストラン / 非表示レストラン（地図に表示しないもの） / カスタムスポット（観光地、ホテルなど）をクラウドに同期し、複数のデバイスで共有します。

---

## 手順

1. **GitHub にサインアップ / サインイン**
   <https://github.com> を開いて、サインインしてください（アカウントがなければ作成）。

   ![GitHub トップページ](step1_github_register.png)

2. **新しい Gist を作成**
   <https://gist.github.com> を開き、filename に `favorites.json`、内容に `[]` を入れて、右下の **Create secret gist** をクリック。

   ![新規 Gist 作成](step2_create_gist.png)

   作成後、アドレスバーの `gist.github.com/<ユーザー名>/` の後ろにある十六進数の文字列が **Gist ID** です。コピーしてください — あとでサイトに貼り付けます。

   ![アドレスバーの Gist ID](step2_create_gist_2.jpg)

3. **PAT（Personal Access Token）を作成**
   <https://github.com/settings/tokens?type=beta> を開き、右上の **Generate new token** をクリック。

   ![PAT 設定ページ](step3_PAT.png)

   - Token name：任意
   - Expiration：**No expiration** 推奨
   - **Permissions** → **Add permissions** までスクロール → **Gists** を検索してチェック

   ![No expiration + Gists を選択](step3_PAT_2.png)

   **Gists** のアクセスを **Read and write** に変更し、最下部の **Generate token** をクリック。

   ![Gists を Read and write に設定](step3_PAT_3.png)

   生成された `github_pat_...` を**今すぐコピー**してください — 表示は一度だけ、ページを離れると二度と見られません。

   ![生成された PAT](step3_PAT_4.png)

4. **サイトに貼り付け**
   地図に戻る → 左下 🎛 → 最下部までスクロール → **⚙️ 同期設定** → Gist ID と PAT を貼り付け → **保存してテスト**。

   ![同期設定パネル](step4_fill.jpg)

**✓ 設定完了** と表示されれば OK。以降、⭐ / 🚫 の操作は自動で同期されます。

---

## 他のデバイス / 他のブラウザ

**同期設定** に同じ Gist ID / PAT を貼り付けると、クラウドに保存されたお気に入り / 非表示 / カスタムスポットがいつでも取得できます。

---

## よくある質問

- **左下のボタンがまだ赤く点滅している。** ページを更新してください。それでも点滅する場合は、同期設定の下部にあるステータス行を確認 — *HTTP 403* は通常 PAT に *Read and write* がないことを示します。手順 3 をやり直してください。
- **Gist を設定しないとどうなる？** すべての編集はこのデバイスにのみ残ります。ブラウザを変えたり、デバイスを変えたり、キャッシュを消すと消えます。
- **Gist だけ設定して PAT を入れないとどうなる？** お気に入り / 非表示 / カスタムスポットを閲覧できますが、変更はクラウドに同期されません（読み取り専用）。
