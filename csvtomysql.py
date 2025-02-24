import pandas as pd
import psycopg2
import simplejson as json
import numpy as np

# === 1. CSVファイルを読み込む ===
print("📂 CSVファイルを読み込み中...")
csv_file = 'C:/Users/user/source/repos/BloodlineCalculator/BloodlineCalculator/blood_percentage.csv'
df = pd.read_csv(csv_file, index_col=0)
print(f"✅ CSVファイルの読み込み完了。データ件数: {len(df)} 行、カラム数: {len(df.columns)} 列。")

# === 2. 異常値をnullに変換 & 浮動小数点値を四捨五入 ===
print("🔄 NaN、inf、-inf、極端な数値をnullに変換中 & 浮動小数点を四捨五入中...")
df.replace([np.inf, -np.inf], None, inplace=True)  # 無限大をnull

# 浮動小数点数を四捨五入（小数点以下4桁）し、極端な値をnullに変換
df = df.applymap(lambda x: round(x, 4) if isinstance(x, float) and abs(x) <= 1e308 else (None if isinstance(x, float) else x))

# 最終的なNaNをnullに変換
df = df.where(pd.notnull(df), None)
print("✅ 異常値処理および四捨五入完了。")

# === 3. PostgreSQLへの接続情報 ===
print("🔌 PostgreSQLに接続中...")
connection = psycopg2.connect(
    host='localhost',
    user='domyhehehe',
    password='doxu1357',
    dbname='horse',
    options='-c client_encoding=UTF8'
)
print("✅ PostgreSQLへの接続に成功。")

table_name = 'blood_percentage_jsonb'

# === 4. CREATE TABLE with JSONB型 ===
print(f"🛠️ テーブル '{table_name}' を作成中...")
with connection.cursor() as cursor:
    cursor.execute(f'DROP TABLE IF EXISTS "{table_name}";')
    print(f"⚡ 既存のテーブル '{table_name}' があれば削除しました。")

    cursor.execute(f'''
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            row_label TEXT,   -- インデックス用カラム
            data JSONB
        );
    ''')
    print(f"✅ テーブル '{table_name}' の作成完了。")

# === 5. データをJSONB形式で挿入 ===
print(f"📦 データを '{table_name}' テーブルに挿入中...")
error_rows = []
with connection.cursor() as cursor:
    total_rows = len(df)
    for i, (index, row) in enumerate(df.iterrows(), start=1):
        try:
            json_data = json.dumps(row.to_dict(), ensure_ascii=False, allow_nan=False)
            cursor.execute(
                f'INSERT INTO "{table_name}" (row_label, data) VALUES (%s, %s);',
                (str(index), json_data)
            )
            if i % 100 == 0 or i == total_rows:
                print(f"📊 {i}/{total_rows} 行を挿入しました...")
        except ValueError as e:
            print(f"⚠️ エラー発生 (行 {i} - {index}): {e}")
            error_rows.append((i, index, str(e)))

    connection.commit()
    print(f"🎉 全 {total_rows} 行のデータ挿入が完了しました。")

# === 6. エラーログ出力 ===
if error_rows:
    print(f"⚠️ 異常データ行: {len(error_rows)} 件")
    for error in error_rows:
        print(f"⚡ 行 {error[0]} ({error[1]}): {error[2]}")
else:
    print("✅ エラーなく全てのデータが挿入されました。")

# === 7. 接続を閉じる ===
connection.close()
print(f"✅ JSONB型テーブル '{table_name}' が作成され、四捨五入処理済みのデータが挿入されました。pgAdminで確認してください。")
