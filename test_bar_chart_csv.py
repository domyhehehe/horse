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
# 1) CSVファイルから読み込み
# ─────────────────────────────────────
print("=== CSVファイルを読み込みます ===")
csv_file = os.path.join('C:', 'Users', 'user', 'source', 'repos', 'BloodlineCalculator', 'BloodlineCalculator', 'blood_percentage.csv')

df_raw = pd.read_csv(
    csv_file,
    index_col=0,
    dtype={0: str},             
    low_memory=False
)
print(f"  >> 読み込んだ行数: {df_raw.shape[0]}、列数: {df_raw.shape[1]}")
print("  >> データサンプル:")
print(df_raw.head(10))

# ─────────────────────────────────────
# 2) 特定の列（例: hyperion）のスコアを使用
# ─────────────────────────────────────
TARGET_COLUMN = 'hyperion'  # ⭐️ この列をスコアとして使用
if TARGET_COLUMN not in df_raw.columns:
    raise ValueError(f"❌ {TARGET_COLUMN} 列が存在しません。CSVファイルを確認してください。")

df = df_raw[[TARGET_COLUMN]].copy()
df.rename(columns={TARGET_COLUMN: 'score'}, inplace=True)

# 数値変換とフィルタリング
before_rows = df.shape[0]
df['score'] = pd.to_numeric(df['score'], errors='coerce')
df = df.dropna(subset=['score'])
df = df[df['score'] > 0]  # 0 以下は除去
after_rows = df.shape[0]
print(f"  >> スコアフィルタリング: {before_rows}行 → {after_rows}行 (score > 0)")

# ─────────────────────────────────────
# 3) 上位馬抽出とソート
# ─────────────────────────────────────
TOP_HORSES = 200
df = df.sort_values(by='score', ascending=False).head(TOP_HORSES)
df = df.sort_values(by='score', ascending=True)

# ─────────────────────────────────────
# 4) time_df (アニメーション用データフレーム) 作成
# ─────────────────────────────────────
print("=== アニメーション用データフレームを作成します ===")
N_BARS_DISPLAY = 10
INITIAL_DISPLAY_COUNT = N_BARS_DISPLAY
STEPS_PER_HORSE = 10
INITIAL_BLANK_STEPS = 5

labels_sorted = df.index.tolist()
vals_sorted = df['score'].tolist()
N = len(labels_sorted)
total_steps = (N - INITIAL_DISPLAY_COUNT) * STEPS_PER_HORSE + INITIAL_BLANK_STEPS

time_index = [f"Step {i+1}" for i in range(total_steps)]
time_df = pd.DataFrame(0, index=time_index, columns=labels_sorted, dtype=float)

# 初期表示馬の埋め込み
for label, val in zip(labels_sorted[:INITIAL_DISPLAY_COUNT], vals_sorted[:INITIAL_DISPLAY_COUNT]):
    time_df[label] = val

# 残りの馬をステップごとに追加
for step_idx in range(INITIAL_BLANK_STEPS, total_steps):
    time_df.iloc[step_idx] = time_df.iloc[step_idx - 1]
    horse_index = (step_idx - INITIAL_BLANK_STEPS) // STEPS_PER_HORSE
    if horse_index < len(labels_sorted[INITIAL_DISPLAY_COUNT:]):
        label = labels_sorted[INITIAL_DISPLAY_COUNT:][horse_index]
        val = vals_sorted[INITIAL_DISPLAY_COUNT:][horse_index]
        step_in_horse = (step_idx - INITIAL_BLANK_STEPS) % STEPS_PER_HORSE
        progress = (step_in_horse + 1) / STEPS_PER_HORSE
        current_val = val * progress
        time_df.loc[time_index[step_idx], label] = current_val

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
    filename='output.mp4',
    orientation='h',
    n_bars=N_BARS_DISPLAY,
    fixed_max=True,
    steps_per_period=FPS,
    period_length=1000 // FPS,
    interpolate_period=False,
    title={'label': 'Chart Title', 'color': 'white', 'size': 24},
    bar_size=0.5,
    bar_texttemplate='{x:.2%}',
    bar_label_font={'color': 'white', 'size': 18},  # ✅ フォントサイズを調整
    tick_label_font={'color': 'white', 'size': 18},
    fig_kwargs={'figsize': (26, 10), 'dpi': 120},   # ✅ 図のサイズを調整
    scale='linear',
    filter_column_colors=True,
    writer='ffmpeg'
)


print("🎬 完了: CSV版 bar_chart_race 動画が生成されました。")
