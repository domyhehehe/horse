import subprocess
import os
import time
import sys
import chardet  # エンコーディング検出用

# psql.exe のパスを明示的に指定する
PSQL_PATH = r"C:\\Program Files\\PostgreSQL\\17\\bin\\psql.exe"

def run_as_admin(cmd):
    """管理者権限でコマンドを実行する"""
    try:
        subprocess.run([
            "powershell",
            "Start-Process",
            "powershell",
            "-ArgumentList", f"'{cmd}'",
            "-Verb", "runas"
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ コマンドを管理者として実行できませんでした: {e}")
        sys.exit(1)

def check_postgresql_service():
    """PostgreSQL サービスの状態を確認して、停止中なら起動する"""
    try:
        result = subprocess.run([
            'powershell',
            '-Command',
            'Get-Service -Name postgresql-x64-17'
        ], capture_output=True, text=True)

        if "Running" in result.stdout:
            print("✅ PostgreSQL サービスはすでに実行中です。")
        else:
            print("❌ PostgreSQL サービスが停止しています。起動します...")
            start_postgresql_service()
    except Exception as e:
        print(f"エラー: {e}")
        start_postgresql_service()

def start_postgresql_service():
    """PostgreSQL サービスを開始する"""
    try:
        run_as_admin('Start-Service -Name postgresql-x64-17')
        print("✅ PostgreSQL サービスが起動しました。")
        time.sleep(5)
    except subprocess.CalledProcessError as e:
        print(f"❌ PostgreSQL サービスを起動できませんでした: {e}")
        sys.exit(1)

def change_role_password(role_name, new_password, superuser_name="postgres"):
    """指定したロールのパスワードを変更する"""
    try:
        change_password_sql = f"ALTER ROLE {role_name} WITH PASSWORD '{new_password}';"
        subprocess.run([
            PSQL_PATH,
            '-U', superuser_name,
            '-h', 'localhost',
            '-c', change_password_sql
        ], check=True, text=True)
        print(f"✅ ロール '{role_name}' のパスワードが正常に変更されました。")
    except subprocess.CalledProcessError as e:
        print(f"❌ ロール '{role_name}' のパスワード変更に失敗しました: {e}")
        sys.exit(1)

def check_role_exists(role_name, superuser_name="postgres"):
    """指定したロールが存在するか確認、なければ作成する"""
    try:
        result = subprocess.run([
            PSQL_PATH,
            '-U', superuser_name,
            '-h', 'localhost',
            '-t',
            '-c', f"SELECT 1 FROM pg_roles WHERE rolname = '{role_name}';"
        ], capture_output=True, text=True, check=True)

        if "1" in result.stdout:
            print(f"✅ ロール '{role_name}' は既に存在します。パスワードを更新します...")
            change_role_password(role_name, "doxu1357", superuser_name)
        else:
            print(f"❌ ロール '{role_name}' が見つかりません。作成します...")
            create_role(role_name, superuser_name)

    except subprocess.CalledProcessError as e:
        print(f"❌ ロール確認に失敗しました: {e}")
        sys.exit(1)

def create_role(role_name, superuser_name="postgres"):
    """指定のロールを作成する"""
    role_password = "doxu1357"
    create_role_sql = (
        f"CREATE ROLE {role_name} WITH "
        f"LOGIN SUPERUSER CREATEDB CREATEROLE INHERIT "
        f"PASSWORD '{role_password}';"
    )

    try:
        subprocess.run([
            PSQL_PATH,
            '-U', superuser_name,
            '-h', 'localhost',
            '-c', create_role_sql
        ], check=True, text=True)
        print(f"✅ ロール '{role_name}' が作成されました。")
    except subprocess.CalledProcessError as e:
        print(f"❌ ロール '{role_name}' の作成に失敗しました: {e}")
        sys.exit(1)

def check_database_exists(database_name, superuser_name="postgres"):
    """指定したデータベースが存在するか確認し、なければ作成"""
    try:
        result = subprocess.run([
            PSQL_PATH,
            '-U', superuser_name,
            '-h', 'localhost',
            '-t',
            '-c', f"SELECT 1 FROM pg_database WHERE datname = '{database_name}';"
        ], capture_output=True, text=True, check=True)

        if "1" in result.stdout:
            print(f"✅ データベース '{database_name}' は既に存在します。")
        else:
            print(f"❌ データベース '{database_name}' が見つかりません。作成します...")
            create_database_if_not_exists(database_name, superuser_name)

    except subprocess.CalledProcessError as e:
        print(f"❌ データベース確認に失敗しました: {e}")
        sys.exit(1)

def create_database_if_not_exists(database_name, superuser_name="postgres"):
    """データベースが存在しない場合に作成する"""
    try:
        subprocess.run([
            PSQL_PATH,
            '-U', superuser_name,
            '-h', 'localhost',
            '-c', f"CREATE DATABASE {database_name};"
        ], check=True, text=True)
        print(f"✅ データベース '{database_name}' が作成されました。")
    except subprocess.CalledProcessError as e:
        print(f"❌ データベース '{database_name}' の作成に失敗しました: {e}")
        sys.exit(1)

def run_sql_commands(db_user, db_name):
    """SQL コマンドを実行して接続確認"""
    temp_sql_file = "temp_connect.sql"
    output_file = "output_check.txt"
    role_password = "doxu1357"

    try:
        with open(temp_sql_file, 'w', encoding='utf-8') as file:
            file.write("""SHOW SERVER_ENCODING;
SET client_encoding TO 'UTF8';
SELECT '✅ PostgreSQLへの接続に成功しました！　AS メッセージ';
\\q
""")

        with open(output_file, 'w', encoding='utf-8') as out:
            subprocess.run(
                [
                    PSQL_PATH,
                    '-U', db_user,
                    '-h', 'localhost',
                    '-d', db_name,
                    '-f', temp_sql_file
                ],
                check=True,
                text=True,
                stdout=out,
                stderr=out,
                env={**os.environ, "PGPASSWORD": role_password, "PGCLIENTENCODING": "UTF8"}
            )

        with open(output_file, 'rb') as file:
            raw_data = file.read()
            detected_encoding = chardet.detect(raw_data)['encoding'] or 'utf-8'
            print(f"🔍 検出されたエンコーディング: {detected_encoding}")

        try:
            print(raw_data.decode(detected_encoding))
        except UnicodeDecodeError:
            print("⚡ エンコーディングの自動検出に失敗。utf-8 にフォールバックします。")
            print(raw_data.decode('utf-8', errors='replace'))

    except subprocess.CalledProcessError as e:
        print(f"❌ SQL コマンドの実行に失敗しました: {e}")
        sys.exit(1)
    finally:
        if os.path.exists(temp_sql_file):
            os.remove(temp_sql_file)
        if os.path.exists(output_file):
            os.remove(output_file)

def main():
    print("⚡ PostgreSQL サービスを確認および起動します...")
    check_postgresql_service()

    check_role_exists('domyhehehe', superuser_name="postgres")
    check_database_exists('horse', superuser_name="postgres")

    print("✅ PostgreSQL の初期設定が完了しました。接続テストを実行します...")
    run_sql_commands(db_user='domyhehehe', db_name='horse')
    print("🎉 ✅ 全ての処理が完了しました！")

if __name__ == '__main__':
    main()
