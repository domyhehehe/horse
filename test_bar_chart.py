import matplotlib.pyplot as plt
import matplotlib as mpl
import pandas as pd
import numpy as np
import bar_chart_race as bcr
import os

# ─────────────────────────────────────
# 0) スタイル設定: 黒背景＆白文字
# ─────────────────────────────────────
print("=== スタイル設定を行います ===")
mpl.rcParams.update({
    'figure.facecolor': 'black',
    'axes.facecolor': 'black',
    'axes.edgecolor': 'white',
    'axes.labelcolor': 'white',
    'xtick.color': 'white',
    'ytick.color': 'white',
    'text.color': 'white',
    'savefig.facecolor': 'black',
    'font.family': 'Meiryo',
})

# ─────────────────────────────────────
# ★ ここが重要 ★ (Excel の列名を確認してください)
# ─────────────────────────────────────
COL_YEAR = 'Year'
COL_HORSE_NAME = 'horsename'
COL_NUMERIC = 'hyperion'  # これを棒の長さに使う数値列とする
TOP_HORSES = 50

# その他パラメータ
N_BARS_DISPLAY = 5
INITIAL_DISPLAY_COUNT = 5
STEPS_PER_HORSE = 10
INITIAL_BLANK_STEPS = 5

# ─────────────────────────────────────
# 1) Excel から読み込み
# ─────────────────────────────────────
print("=== Excel ファイルを読み込みます ===")
excel_file = os.path.join('C:', 'Users', 'user', 'OneDrive', 'ドキュメント', '血の量縦.xlsx')
sheet_name = 'Sheet1'
use_columns = [COL_YEAR, COL_HORSE_NAME, COL_NUMERIC]

df_raw = pd.read_excel(
    excel_file,
    sheet_name=sheet_name,
    usecols=use_columns
)
print(f"  >> 読み込んだ行数: {df_raw.shape[0]}、列: {df_raw.shape[1]} 列")
print("  >> 列名:", df_raw.columns.tolist())

# ─────────────────────────────────────
# 2) リネーム
# ─────────────────────────────────────
print("=== 列をリネームして内部処理用にします ===")
df = df_raw.rename(
    columns = {
        COL_YEAR: "year",
        COL_HORSE_NAME: "horsename",
        COL_NUMERIC: "score"
    }
)
print("  >> リネーム後の列名:", df.columns.tolist())

# ─────────────────────────────────────
# 3) bar_chart_race 用の前処理
# ─────────────────────────────────────
print("=== 前処理 (フィルタリング・ソート等) を行います ===")

# (1) 数値列 "score" が 0 以下 or 欠損は除外
before_rows = df.shape[0]
df = df[df["score"] > 0]
df["score"] = pd.to_numeric(df["score"], errors='coerce').round(2)
after_rows = df.shape[0]
print(f"  >> score>0 でフィルタ: {before_rows} 行 → {after_rows} 行")

# (2) 上位 TOP_HORSES 件を抽出 → 昇順ソート
print(f"  >> スコア上位 {TOP_HORSES} 件を抽出します")
df = df.sort_values(by="score", ascending=False).head(TOP_HORSES)
df = df.sort_values(by="score", ascending=True)
print(f"  >> 抽出後行数: {df.shape[0]}")

# (3) 同値に微差を付与
vals = df["score"].to_numpy()
increment = 1e-6
start = 0
for i in range(1, len(vals) + 1):
    if i == len(vals) or abs(vals[i] - vals[i-1]) > 1e-12:
        for j in range(start + 1, i):
            vals[j] = vals[j-1] + increment
        start = i
df["score"] = vals

# (4) ラベル列作成
df["label"] = df["horsename"] + " [" + df["year"].astype(str) + "]"
labels_sorted = df["label"].tolist()
vals_sorted = df["score"].tolist()
N = len(labels_sorted)

print("  >> データフレームの最終形:")
print(df.head(10))

# ─────────────────────────────────────
# 4) バーチャートレース用の DataFrame 作成
# ─────────────────────────────────────
print("=== バーチャートレース用の DataFrame (time_df) を作成します ===")
total_steps = (N - INITIAL_DISPLAY_COUNT) * STEPS_PER_HORSE + INITIAL_BLANK_STEPS
print(f"  >> 合計ステップ数: {total_steps}")

time_index = [f"Step {i+1}" for i in range(total_steps)]
time_df = pd.DataFrame(0, index=time_index, columns=labels_sorted, dtype=float)

print("  >> 初期表示馬のスコアを埋め込み中...")
initial_labels = labels_sorted[:INITIAL_DISPLAY_COUNT]
initial_vals = vals_sorted[:INITIAL_DISPLAY_COUNT]
for label, val in zip(initial_labels, initial_vals):
    time_df[label] = val

print("  >> 残り馬をステップごとに徐々に追加中...")
for step_idx in range(INITIAL_BLANK_STEPS, total_steps):
    # 適宜、進捗ログを出す(多すぎるとログで埋まるので控えめに)
    if (step_idx - INITIAL_BLANK_STEPS) % 10 == 0:
        print(f"    ...ステップ {step_idx+1}/{total_steps} 処理中")

    time_df.iloc[step_idx] = time_df.iloc[step_idx - 1]
    horse_index = (step_idx - INITIAL_BLANK_STEPS) // STEPS_PER_HORSE
    if horse_index < len(labels_sorted[INITIAL_DISPLAY_COUNT:]):
        label = labels_sorted[INITIAL_DISPLAY_COUNT:][horse_index]
        val = vals_sorted[INITIAL_DISPLAY_COUNT:][horse_index]
        step_in_horse = (step_idx - INITIAL_BLANK_STEPS) % STEPS_PER_HORSE
        progress = (step_in_horse + 1) / STEPS_PER_HORSE
        current_val = val * progress
        time_df.iloc[step_idx, time_df.columns.get_loc(label)] = current_val

# ─────────────────────────────────────
# 5) bar_chart_race でアニメーション生成
# ─────────────────────────────────────
print("=== bar_chart_race によるアニメーションを生成します ===")

fig, ax = plt.subplots(figsize=(30, 10), dpi=200)
fig.patch.set_facecolor('black')
for spine in ax.spines.values():
    spine.set_visible(False)
ax.set_xticks([])
ax.set_yticks([])

bcr.bar_chart_race(
    df=time_df,
    filename='horse_ranking_barchart_race_custom_config.mp4',
    orientation='h',
    sort='desc',
    n_bars=N_BARS_DISPLAY,
    fixed_order=False,
    fixed_max=False,
    steps_per_period=5,
    period_length=400,
    interpolate_period=False,
    title={
        'label': f'Bar Chart Race (Top {TOP_HORSES} by score)',
        'color': 'white',
        'size': 24
    },
    bar_size=0.6,
    period_label=False,
    bar_texttemplate='{x:.2%}',  # 0.12 なら 12.00% 表示
    bar_label_font={'color': 'white', 'size': 24},
    tick_label_font={'color': 'white', 'size': 24},
    fig=fig,
    bar_kwargs={'ec': 'white', 'lw': 1.5, 'alpha': 0.9},
    scale='linear',
    filter_column_colors=True,
    writer='ffmpeg'
)

print("🎬 Done! bar_chart_race の動画ファイルが生成されました。")
