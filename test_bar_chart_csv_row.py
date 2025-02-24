import matplotlib.pyplot as plt
import matplotlib as mpl
import pandas as pd
import numpy as np
import bar_chart_race as bcr
import os
import gc
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
    'figure.subplot.left': 0.4,
    'figure.subplot.right': 0.95
})

# 上位馬抽出とソート
TOP_HORSES = 140
INITIAL_DISPLAY_COUNT = 20

# time_df 作成
N_BARS_DISPLAY = INITIAL_DISPLAY_COUNT
# 🎵 BPM設定
BPM = 95  # 任意のBPM値を設定
BEATS_PER_HORSE = 4  # 各馬の登場に必要な拍数

SECONDS_PER_HORSE = (60 / BPM) * BEATS_PER_HORSE  # BPMに基づく1頭あたりの秒数
FPS = 5
STEPS_PER_HORSE = max(1, int(SECONDS_PER_HORSE * FPS))

print(f"🎼 BPM: {BPM}, 各馬の表示にかかる時間: {SECONDS_PER_HORSE:.2f}秒, ステップ数: {STEPS_PER_HORSE}")

INITIAL_BLANK_STEPS = 5
FINAL_HOLD_STEPS = 30
# 🎬 動画の予測時間を計算 (正確なロジック)
yosoku_total_steps = INITIAL_BLANK_STEPS + (TOP_HORSES - INITIAL_DISPLAY_COUNT - 1) * STEPS_PER_HORSE + FINAL_HOLD_STEPS
total_time_sec = yosoku_total_steps / FPS  # FPSを用いた総時間の計算

minutes, seconds = divmod(total_time_sec, 60)
print(f"⏱️ ✅ 修正版予測動画時間: {minutes:.0f}分 {seconds:.0f}秒 （合計: {total_time_sec:.2f}秒）")

# CSVファイル読み込み
csv_file = os.path.join('C:', 'Users', 'user', 'source', 'repos', 'BloodlineCalculator', 'BloodlineCalculator', 'blood_percentage.csv')
df_raw = pd.read_csv(csv_file, index_col=0, dtype={0: str}, low_memory=False).T

# 特定列をスコアとして使用
TARGET_COLUMN = 'EQUINOX   (JPN) [2019]'
if TARGET_COLUMN not in df_raw.columns:
    raise ValueError(f"❌ {TARGET_COLUMN} 列が存在しません。CSVファイルを確認してください。")

df = df_raw.copy()
df['horsename'] = df.index
df = df[["horsename", TARGET_COLUMN]].copy()
df.rename(columns={TARGET_COLUMN: 'score'}, inplace=True)
df['score'] = pd.to_numeric(df['score'], errors='coerce')
df = df.dropna(subset=['score'])
df = df[df['score'] > 0]
df = df.sort_values(by='score', ascending=False).head(TOP_HORSES)
df = df.sort_values(by='score', ascending=True).reset_index(drop=True)

# ✅ 同率順位を付与（1位を0位にし、全て1つずつ繰り上げ）
df['rank'] = df['score'].rank(method='min', ascending=False).astype(int) - 1
df['horsename'] = df.apply(lambda x: f"{x['rank']}位 {x['horsename']}", axis=1)

# 同値に微差を付与
df = df.sort_values(by='score', ascending=True).reset_index(drop=True)
vals = df["score"].to_numpy()
increment = 1e-6
for i in range(1, len(vals)):
    if vals[i] <= vals[i - 1]:
        vals[i] = vals[i - 1] + increment
df["score"] = vals


labels_sorted = df["horsename"].tolist()
vals_sorted = df['score'].tolist()
N = len(labels_sorted)

total_steps = INITIAL_BLANK_STEPS + (N - 1) * STEPS_PER_HORSE + FINAL_HOLD_STEPS

time_index = [f"Step {i+1}" for i in range(total_steps)]
time_df = pd.DataFrame(0, index=time_index, columns=labels_sorted, dtype=float)

fig, ax = plt.subplots(figsize=(19.2, 10.8), dpi=100)

# 初期表示馬を最初から最大スコアで設定
time_df.iloc[:INITIAL_BLANK_STEPS, :] = 0
for label, val in zip(labels_sorted[:INITIAL_DISPLAY_COUNT], vals_sorted[:INITIAL_DISPLAY_COUNT]):
    time_df.iloc[:, time_df.columns.get_loc(label)] = val

# 残りの馬をステップ単位で追加 (累積ロジック維持)
for idx, (label, val) in enumerate(zip(labels_sorted[INITIAL_DISPLAY_COUNT:], vals_sorted[INITIAL_DISPLAY_COUNT:])):
    start_step = INITIAL_BLANK_STEPS + idx * STEPS_PER_HORSE
    for step in range(STEPS_PER_HORSE):
        if start_step + step > 0:
            time_df.iloc[start_step + step] = time_df.iloc[start_step + step - 1]
        progress = (step + 1) / STEPS_PER_HORSE
        current_val = val * progress
        time_df.iloc[start_step + step, time_df.columns.get_loc(label)] = current_val

# 最後の馬登場前に静止ステップを追加
last_step_before_final_horse = INITIAL_BLANK_STEPS + (N - INITIAL_DISPLAY_COUNT - 1) * STEPS_PER_HORSE
hold_frame = time_df.iloc[[last_step_before_final_horse - 1]].values
hold_frames = np.repeat(hold_frame, FINAL_HOLD_STEPS, axis=0)
time_df = pd.concat([
    time_df.iloc[:last_step_before_final_horse],
    pd.DataFrame(hold_frames, columns=time_df.columns)],
    ignore_index=True
)

# ★★★ ここで最大値を1.2倍にクリップしてバーが長すぎないようにする ★★★
max_val = time_df.max().max()
time_df = time_df.clip(upper=max_val * 1.5)

time_df.iloc[:, :] = time_df.iloc[:, :].clip(upper=time_df.max().max())

# アニメーション生成
plt.subplots_adjust(left=0.1, right=0.9, top=0.9, bottom=0.1)
fig, ax = plt.subplots(figsize=(22, 22), dpi=100)
fig.patch.set_facecolor('black')
for spine in ax.spines.values():
    spine.set_visible(False)
ax.set_xticks([])
ax.set_yticks([])

bcr.bar_chart_race(
    df=time_df,
    filename=f'{TARGET_COLUMN}_barchart.mp4',
    orientation='h',
    sort='desc',
    n_bars=N_BARS_DISPLAY,
    fixed_order=False,
    fixed_max=True,
    steps_per_period=FPS,
    period_length=1000 // FPS,
    interpolate_period=False,
    title={'label': f'Bar Chart Race (Top {TOP_HORSES} by {TARGET_COLUMN}) [静止修正版: 0位開始]', 'color': 'white', 'size': 24},
    bar_size=0.5,
    period_label=False,
    bar_texttemplate='{x:.2%}',
    bar_textposition='inside',
    bar_label_font={'color': 'white', 'size': 24},
    tick_label_font={'color': 'white', 'size': 24},
    fig=fig,
    bar_kwargs={'ec': 'white', 'lw': 1.5, 'alpha': 0.9},
    scale='linear',
    filter_column_colors=True,
    writer='ffmpeg'
)

print("🎬 完了: 0位開始で順位を繰り上げたアニメーションを生成しました。 ✅")

# (処理後の最後に追加)


plt.close('all')
gc.collect()  # ガベージコレクションで未使用のメモリを解放
