import matplotlib.pyplot as plt
import matplotlib as mpl
import pandas as pd
import numpy as np
import bar_chart_race as bcr
import os

"""
⚠️ **重要な注意事項: 仕様・設計・詳細設計**

【要件定義】
- CSVファイルからデータを読み込み、バーチャートレース形式でアニメーションを生成する。
- アニメーションは馬を1頭ずつ順次表示し、全ての馬が同じ時間で最大スコアに達する。
- **horsename列を正確に表示**し、表示順序を正確に反映する。
- **初期表示馬もステップごとに増加**し、即座に最大スコアを反映しない。
- デグレ防止のため、累積ロジックを保持し、既存データをリセットしない。
- **同値に微差を付与してスコア順序の一貫性を確保。**
- **累積ロジック強化**: time_dfを更新する際、すべての馬のスコアと名前を正確に引き継ぐ処理を維持。
- **STEP数分解動作を保持**: 各馬の追加がステップ単位で進行する。

【設計・反省と対策】
- 初期表示馬もステップ分解で増加させる。
- time_df全体を上書きせず、必要な箇所のみ更新。
- 各ステップで全ての馬のスコアが保持される処理を保証。
- 各ステップの更新では、time_df.iloc[step] = time_df.iloc[step - 1]を使用して、前ステップの値を確実に引き継ぐ。
- **コメントを強化**: 重要な箇所にコメントを追加し、再発を防止。

このコメントは**絶対に削除しないこと**。今後のデグレを防ぐため、コード理解を容易にするために残す。
"""

# スタイル設定
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
    'figure.subplot.left': 0.2,
    'figure.subplot.right': 0.95
})

# CSVファイル読み込み
csv_file = os.path.join('C:', 'Users', 'user', 'source', 'repos', 'BloodlineCalculator', 'BloodlineCalculator', 'blood_percentage.csv')
df_raw = pd.read_csv(csv_file, index_col=0, dtype={0: str}, low_memory=False).T

# 特定列をスコアとして使用
TARGET_COLUMN = 'hyperion'
if TARGET_COLUMN not in df_raw.columns:
    raise ValueError(f"❌ {TARGET_COLUMN} 列が存在しません。CSVファイルを確認してください。")

df = df_raw.copy()
df['horsename'] = df.index
df = df[["horsename", TARGET_COLUMN]].copy()
df.rename(columns={TARGET_COLUMN: 'score'}, inplace=True)
df['score'] = pd.to_numeric(df['score'], errors='coerce')
df = df.dropna(subset=['score'])
df = df[df['score'] > 0]

# 同値に微差を付与
df = df.sort_values(by='score', ascending=True).reset_index(drop=True)
vals = df["score"].to_numpy()
increment = 1e-6
for i in range(1, len(vals)):
    if vals[i] <= vals[i - 1]:
        vals[i] = vals[i - 1] + increment
df["score"] = vals

# 上位馬抽出とソート
TOP_HORSES = 50
INITIAL_DISPLAY_COUNT = 5
df = df.sort_values(by='score', ascending=False).head(TOP_HORSES)
df = df.sort_values(by='score', ascending=True).reset_index(drop=True)

# time_df 作成 (horsenameを使用して累積ロジック保持、STEP数分解動作を保持)
N_BARS_DISPLAY = 5
SECONDS_PER_HORSE = 2
FPS = 5
STEPS_PER_HORSE = max(1, int(SECONDS_PER_HORSE * FPS))
INITIAL_BLANK_STEPS = 5

labels_sorted = df["horsename"].tolist()
vals_sorted = df['score'].tolist()
N = len(labels_sorted)
total_steps = N * STEPS_PER_HORSE + INITIAL_BLANK_STEPS

time_index = [f"Step {i+1}" for i in range(total_steps)]
time_df = pd.DataFrame(0, index=time_index, columns=labels_sorted, dtype=float)

# ✅ 初期表示馬もステップ分解で増加
total_progress_steps = []
for idx, (label, val) in enumerate(zip(labels_sorted[:INITIAL_DISPLAY_COUNT], vals_sorted[:INITIAL_DISPLAY_COUNT])):
    print(f"初期表示馬 {label} をステップごとに追加中...")
    for step in range(STEPS_PER_HORSE):
        if step > 0:
            time_df.iloc[step] = time_df.iloc[step - 1]
        progress = (step + 1) / STEPS_PER_HORSE
        current_val = val * progress
        time_df.iloc[step, time_df.columns.get_loc(label)] = current_val

# ✅ 残りの馬を累積ロジックでステップ単位で追加
for idx, (label, val) in enumerate(zip(labels_sorted[INITIAL_DISPLAY_COUNT:], vals_sorted[INITIAL_DISPLAY_COUNT:])):
    start_step = INITIAL_BLANK_STEPS + idx * STEPS_PER_HORSE
    for step in range(STEPS_PER_HORSE):
        time_df.iloc[start_step + step] = time_df.iloc[start_step + step - 1]
        progress = (step + 1) / STEPS_PER_HORSE
        current_val = val * progress
        time_df.iloc[start_step + step, time_df.columns.get_loc(label)] = current_val

# 上限値を制御
time_df.iloc[:, :] = time_df.iloc[:, :].clip(upper=time_df.max().max())

# アニメーション生成
fig, ax = plt.subplots(figsize=(24, 8), dpi=100)
fig.patch.set_facecolor('black')
for spine in ax.spines.values():
    spine.set_visible(False)
ax.set_xticks([])
ax.set_yticks([])

bcr.bar_chart_race(
    df=time_df,
    filename='horse_ranking_barchart_race_csv_version_test_fixed.mp4',
    orientation='h',
    sort='desc',
    n_bars=N_BARS_DISPLAY,
    fixed_order=False,
    fixed_max=True,
    steps_per_period=FPS,
    period_length=1000 // FPS,
    interpolate_period=False,
    title={'label': f'Bar Chart Race (Top {TOP_HORSES} by {TARGET_COLUMN}) [STEP分解動作完全保持版]', 'color': 'white', 'size': 24},
    bar_size=0.5,
    period_label=False,
    bar_texttemplate='{x:.2%}',
    bar_label_font={'color': 'white', 'size': 24},
    tick_label_font={'color': 'white', 'size': 24},
    fig=fig,
    bar_kwargs={'ec': 'white', 'lw': 1.5, 'alpha': 0.9},
    scale='linear',
    filter_column_colors=True,
    writer='ffmpeg'
)

print("🎬 完了: horsename表示＆STEP分解動作完全保持済みアニメーションを生成しました。 ✅")
