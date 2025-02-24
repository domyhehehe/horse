import bar_chart_race as bcr

# ヘルプテキストを取得
help_text = ""
import io
from contextlib import redirect_stdout

with io.StringIO() as buf, redirect_stdout(buf):
    help(bcr.bar_chart_race)
    help_text = buf.getvalue()

# 外部ファイルに保存
with open("bar_chart_race_help.txt", "w", encoding="utf-8") as f:
    f.write(help_text)

print("✅ bar_chart_race 関数のヘルプを 'bar_chart_race_help.txt' に出力しました。")
