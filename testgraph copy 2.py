import matplotlib.pyplot as plt
import matplotlib as mpl
import pandas as pd
import numpy as np
import bar_chart_race as bcr

# スタイル設定: 黒背景＆白文字
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
# 💡 定数設定：ここを変更するだけで調整可能
# ─────────────────────────────────────
N_BARS_DISPLAY = 5              # 表示するバーの数
INITIAL_DISPLAY_COUNT = 5       # 初期に表示する馬の数
STEPS_PER_HORSE = 10            # 各馬が成長するステップ数
INITIAL_BLANK_STEPS = 5         # 最初の空白ステップ数

# ─────────────────────────────────────
# 1) データ作成
# ─────────────────────────────────────
data = {
    "Year": [2019, 2012, 2010, 2005, 2001, 1998, 1995, 1758, 1724, 1764],
    "Horse Name": [
        "EQUINOX (JPN)", "KITASAN BLACK (JPN)", "CHATEAU BLANCHE (JPN)",
        "SUGAR HEART (JPN)", "BLACK TIDE (JPN)", "BLANCHERIE (JPN)",
        "KING HALO (JPN)", "HEROD (GB)", "GODOLPHIN ARABIAN", "ECLIPSE (GB)"
    ],
    "Percentage": [100.00, 50.00, 50.00, 25.00, 25.00, 25.00, 25.00, 18.45, 14.37, 13.54]
}
df = pd.DataFrame(data)

df = df[df["Percentage"] > 0]
df["Percentage"] = pd.to_numeric(df["Percentage"], errors='coerce').round(2)
df = df.sort_values(by="Percentage", ascending=True)
vals = df["Percentage"].to_numpy()
increment = 1e-6

start = 0
for i in range(1, len(vals) + 1):
    if i == len(vals) or abs(vals[i] - vals[i-1]) > 1e-12:
        group_size = i - start
        for j in range(start + 1, i):
            vals[j] = vals[j-1] + increment
        start = i

df["Percentage"] = vals
df["Horse Label"] = df["Horse Name"] + " [" + df["Year"].astype(str) + "]"

labels_sorted = df["Horse Label"].tolist()
vals_sorted = df["Percentage"].tolist()
N = len(labels_sorted)

# ─────────────────────────────────────
# 2) 各馬が徐々に値が増える DataFrame 作成
#    初期に設定した数の馬を最初から実際の値で表示
# ─────────────────────────────────────
total_steps = (N - INITIAL_DISPLAY_COUNT) * STEPS_PER_HORSE + INITIAL_BLANK_STEPS
time_index = [f"Step {i+1}" for i in range(total_steps)]

time_df = pd.DataFrame(0, index=time_index, columns=labels_sorted, dtype=float)  # 初期値は0

# 初期表示馬を最初から実際の値で表示
initial_labels = labels_sorted[:INITIAL_DISPLAY_COUNT]
initial_vals = vals_sorted[:INITIAL_DISPLAY_COUNT]
for label, val in zip(initial_labels, initial_vals):
    time_df[label] = val  # 全ステップにわたり初期表示

# 残りの馬をステップごとに追加
remaining_labels = labels_sorted[INITIAL_DISPLAY_COUNT:]
remaining_vals = vals_sorted[INITIAL_DISPLAY_COUNT:]

for step_idx in range(INITIAL_BLANK_STEPS, total_steps):
    time_df.iloc[step_idx] = time_df.iloc[step_idx - 1]  # 前のステップをコピー

    horse_index = (step_idx - INITIAL_BLANK_STEPS) // STEPS_PER_HORSE
    if horse_index < len(remaining_labels):
        label = remaining_labels[horse_index]
        step_in_horse = (step_idx - INITIAL_BLANK_STEPS) % STEPS_PER_HORSE
        progress = (step_in_horse + 1) / STEPS_PER_HORSE

        val = remaining_vals[horse_index]
        current_val = val * progress
        time_df.loc[time_index[step_idx], label] = current_val

# ─────────────────────────────────────
# 3) bar_chart_race でアニメーション生成
# ─────────────────────────────────────
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
    n_bars=N_BARS_DISPLAY,  # 💡 定数で設定
    fixed_order=False,
    fixed_max=False,
    steps_per_period=5,
    period_length=400,
    interpolate_period=False,
    title={
        'label': f'Horse Ranking (Top {N_BARS_DISPLAY}, Initial {INITIAL_DISPLAY_COUNT} Displayed)',
        'color': 'white',
        'size': 24
    },
    bar_size=0.6,
    period_label=False,
    bar_texttemplate='{x:.2f}%',
    bar_label_font={'color': 'white', 'size': 24},
    tick_label_font={'color': 'white', 'size': 24},
    fig=fig,
    bar_kwargs={'ec': 'white', 'lw': 1.5, 'alpha': 0.9},
    scale='linear',
    filter_column_colors=True,
    writer='ffmpeg'
)

print(f"🎬 ✅ 下位{INITIAL_DISPLAY_COUNT}頭を最初から実際の値で表示し、上位{N_BARS_DISPLAY}頭をアニメーションで表示しました！")
