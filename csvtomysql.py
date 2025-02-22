import pandas as pd
import pymysql

# === 1. CSVファイルを読み込む ===
csv_file = 'C:/Users/user/source/repos/BloodlineCalculator/BloodlineCalculator/blood_percentage.csv'

df = pd.read_csv(csv_file)

# === 2. MySQLへの接続情報 ===
connection = pymysql.connect(
    host='localhost',        # ← 適切なホスト名に変更
    user='domyhehehe',    # ← ユーザー名を入力
    password='doxu1357',# ← パスワードを入力
    database='horse',# ← 使用するデータベース名を入力
    charset='utf8mb4'
)

table_name = 'blood_percentage'  # 作成するテーブル名

# === 3. CREATE TABLE文を生成 ===
def generate_create_table(df, table_name):
    sql = f"CREATE TABLE `{table_name}` (\n"
    for col in df.columns:
        sql += f"  `{col}` VARCHAR(255),\n"
    sql = sql.rstrip(',\n') + "\n);"
    return sql

create_table_sql = generate_create_table(df, table_name)

# === 4. テーブル作成 & データ挿入 ===
with connection.cursor() as cursor:
    cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`;")
    cursor.execute(create_table_sql)

    # INSERT文を生成
    cols = ",".join([f"`{col}`" for col in df.columns])
    for _, row in df.iterrows():
        values = ",".join([f"'{str(value)}'" for value in row])
        insert_sql = f"INSERT INTO `{table_name}` ({cols}) VALUES ({values});"
        cursor.execute(insert_sql)

    connection.commit()

connection.close()
print(f"✅ テーブル '{table_name}' が作成され、CSVデータが挿入されました。HeidiSQLで確認してください。")
