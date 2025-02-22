import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, FFMpegWriter

# グラフの設定
fig, ax = plt.subplots()
x = np.linspace(0, 2 * np.pi, 200)
line, = ax.plot(x, np.sin(x))

ax.set_xlim(0, 2 * np.pi)
ax.set_ylim(-1.1, 1.1)
ax.set_title('動くサイン波アニメーション')
ax.set_xlabel('x')
ax.set_ylabel('sin(x)')

# フレームごとの更新関数
def update(frame):
    line.set_ydata(np.sin(x + frame / 10))  # フレームによって波の位相を変化
    return line,

# アニメーションの作成
ani = FuncAnimation(fig, update, frames=np.arange(0, 100), interval=50, blit=True)

# アニメーションをMP4形式で保存
writer = FFMpegWriter(fps=20, metadata=dict(artist='あなたの名前'), bitrate=1800)
ani.save("sine_wave_animation.mp4", writer=writer)

plt.show()
