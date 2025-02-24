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
- デグレ防止のため、累積ロジックを保持し、既存データをリセットしない。
- 処理速度を考慮し、テスト用高速設定と本番用設定を切り替え可能とする。
- **🛠 修正案**: 各馬を追加するループ内で `time_df.iloc[start_step + step] = time_df.iloc[start_step + step - 1]` を必ず呼び出し、すべてのステップで累積データを維持するようにします。最初のステップには特別な処理を行います。

【設計】
1. CSV読み込み時に行と列を反転。
2. 特定列（例: 'hyperion'）をスコアとして使用。
3. 上位50頭をスコア順に抽出し、昇順にソート。
4. `time_df`作成時に累積ロジックを使用し、1頭ずつ表示。
5. bar_chart_raceを用いたアニメーション生成。
6. デグレを防止するための重要コメントを削除しない。

【詳細設計】
- `time_df`更新時、`time_df.iloc[step_idx] = time_df.iloc[step_idx - 1]`で前ステップの値を保持。
- 最初のステップは空データからの積み上げを考慮し、正確な累積ロジックを適用。
- FPSとperiod_lengthを調整し、テスト用では極端な高速処理を適用。
- 上限値をclipで制御し、不正な値を防止。
- コメントで各ステップの処理内容を明示し、今後の修正時に誤解を防ぐ。
- 全ての処理工程に進捗ログを出力して可視化。

このコメントは**絶対に削除しないこと**。今後のデグレを防ぐため、コード理解を容易にするために残す。
"""

# ─────────────────────────────────────
# 0) スタイル設定: 黒背景＆白文字 (バーやテキストが見切れないよう調整)
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
    'figure.subplot.left': 2,
    'figure.subplot.right': 9,
})

# ─────────────────────────────────────
# 1) CSVファイルから読み込み (行と列を反転)
# ─────────────────────────────────────
print("=== CSVファイルを読み込みます (行列反転) ===")
csv_file = os.path.join('C:', 'Users', 'user', 'source', 'repos', 'BloodlineCalculator', 'BloodlineCalculator', 'blood_percentage.csv')

print("  >> CSVファイル読み込み開始...")
df_raw = pd.read_csv(
    csv_file,
    index_col=0,
    dtype={0: str},
    low_memory=False
).T

print(f"  >> 読み込んだ行数: {df_raw.shape[0]}、列数: {df_raw.shape[1]}")

# ─────────────────────────────────────
# 2) 特定の列（例: hyperion）のスコアを使用
# ─────────────────────────────────────
TARGET_COLUMN = 'hyperion'
print(f"=== {TARGET_COLUMN} 列を使用してデータを抽出します ===")
if TARGET_COLUMN not in df_raw.columns:
    raise ValueError(f"❌ {TARGET_COLUMN} 列が存在しません。CSVファイルを確認してください。")

df = df_raw[[TARGET_COLUMN]].copy()
df.rename(columns={TARGET_COLUMN: 'score'}, inplace=True)
df['score'] = pd.to_numeric(df['score'], errors='coerce')
df = df.dropna(subset=['score'])
df = df[df['score'] > 0]

# ─────────────────────────────────────
# 3) 上位馬抽出とソート
# ─────────────────────────────────────
TOP_HORSES = 50
print(f"=== スコア上位 {TOP_HORSES} 件を抽出します ===")
df = df.sort_values(by='score', ascending=False).head(TOP_HORSES)
df = df.sort_values(by='score', ascending=True)

# ─────────────────────────────────────
# 4) time_df (アニメーション用データフレーム) 作成 (累積ロジック完全保持、デグレ対策)
# ─────────────────────────────────────
print("=== アニメーション用データフレームを作成します (累積ロジック完全保持) ===")
N_BARS_DISPLAY = 30
SECONDS_PER_HORSE = 0.5  # ✅ テスト用に1頭あたり0.5秒で表示
FPS = 3                  # ✅ FPSを1に設定 (極端な高速処理)
STEPS_PER_HORSE = max(1, int(SECONDS_PER_HORSE * FPS))
INITIAL_BLANK_STEPS = 1

labels_sorted = df.index.tolist()
vals_sorted = df['score'].tolist()
N = len(labels_sorted)
total_steps = N * STEPS_PER_HORSE + INITIAL_BLANK_STEPS

time_index = [f"Step {i+1}" for i in range(total_steps)]
time_df = pd.DataFrame(0, index=time_index, columns=labels_sorted, dtype=float)

print("  >> 各馬を順次追加中 (累積ロジック完全保持)...")
for idx, (label, val) in enumerate(zip(labels_sorted, vals_sorted)):
    print(f"    ... {label} の追加開始 (スコア: {val:.4f})")
    start_step = INITIAL_BLANK_STEPS + idx * STEPS_PER_HORSE
    for step in range(STEPS_PER_HORSE):
        if step == 0 and start_step > 0:
            time_df.iloc[start_step + step] = time_df.iloc[start_step + step - 1]  # ✅ 累積ロジック維持
        elif step > 0:
            time_df.iloc[start_step + step] = time_df.iloc[start_step + step - 1]
        progress = (step + 1) / STEPS_PER_HORSE
        current_val = val * progress
        time_df.iloc[start_step + step, time_df.columns.get_loc(label)] = current_val
    print(f"    ✅ {label} の追加完了")

time_df.iloc[:, :] = time_df.iloc[:, :].clip(upper=time_df.max().max())

# ─────────────────────────────────────
# 5) bar_chart_race でアニメーション生成 (デグレ対策済み)
# ─────────────────────────────────────
print("=== bar_chart_race によるアニメーションを生成します (デグレ対策済み) ===")

fig, ax = plt.subplots(figsize=(24, 8), dpi=100)
fig.patch.set_facecolor('black')
for spine in ax.spines.values():
    spine.set_visible(False)
ax.set_xticks([])
ax.set_yticks([])

bcr.bar_chart_race(
    df=time_df,
    filename='horse_ranking_barchart_race_csv_version_test_fast.mp4',
    orientation='h',
    sort='desc',
    n_bars=N_BARS_DISPLAY,
    fixed_order=False,
    fixed_max=True,
    steps_per_period=FPS,
    period_length=1000 // FPS,
    interpolate_period=False,
    title={'label': f'Bar Chart Race (Top {TOP_HORSES} by {TARGET_COLUMN}) [デグレ対策済み]', 'color': 'white', 'size': 24},
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

print("🎬 完了: デグレ対策済みアニメーションを生成しました。 ✅")
