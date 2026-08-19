# -*- coding: utf-8 -*-
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle
import matplotlib.font_manager as fm

fp = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'
fm.fontManager.addfont(fp)
plt.rcParams['font.family'] = 'WenQuanYi Zen Hei'
plt.rcParams['axes.unicode_minus'] = False

NAVY, BLUE, GREY, LIGHT = '#17365D', '#3C6E9E', '#6B7684', '#E8EEF5'

# ---------------- 图 3：数据覆盖与预测时点时间轴 ----------------
fig, (axA, axB) = plt.subplots(2, 1, figsize=(10.2, 6.6),
                               gridspec_kw={'height_ratios': [1.35, 1]})

# --- A: 数据源覆盖 + 分期 ---
years = [2021, 2022, 2023, 2024, 2025, 2026, 2027]
x0, x1 = 2021.0, 2027.0
spans = [
    ('市场与微观结构（交易所 API / 聚合源）', 2021.0, 2027.0, BLUE, '实线=已确认可得'),
    ('Reddit（官方 API + 公开历史归档）',       2021.0, 2027.0, BLUE, ''),
    ('Telegram（Telethon 公开频道）',           2021.6, 2027.0, BLUE, '起点受频道存续限制'),
    ('X / Twitter（需 Enterprise 许可）',        2021.0, 2027.0, GREY, '虚线=预算与许可未定'),
    ('链上实体标注（BTC / ETH / ERC-20）',      2021.0, 2027.0, GREY, '虚线=供应商未签约'),
]
for k, (label, a, b, c, note) in enumerate(spans):
    y = len(spans) - k
    dashed = (c == GREY)
    axA.plot([a, b], [y, y], lw=7, color=c, solid_capstyle='butt',
             alpha=0.35 if dashed else 1.0,
             linestyle='-' if not dashed else (0, (4, 2)))
    axA.text(x0 - 0.08, y, label, ha='right', va='center', fontsize=8.5, color=NAVY)
    if note:
        axA.text(b + 0.05, y, note, ha='left', va='center', fontsize=7, color=GREY)

# 分期带
bands = [(2021.0, 2024.0, '训练期', '#DCE6F1'),
         (2024.0, 2025.0, '验证 / 阈值校准', '#C6D9EC'),
         (2025.0, 2026.5, '已部分观察的\n时间外评价期', '#F2DCDB'),
         (2026.5, 2027.0, '前瞻性\n样本外', '#D8E4BC')]
for a, b, lab, col in bands:
    axA.add_patch(Rectangle((a, 0.25), b - a, 0.55, color=col, zorder=0))
    axA.text((a + b) / 2, 0.52, lab, ha='center', va='center', fontsize=7.5, color=NAVY)
axA.axvline(2026.5, color='#C0504D', lw=1.2, ls='--')
axA.text(2026.5, len(spans) + 0.62, '预注册与配置哈希锁定', ha='center', fontsize=7.5, color='#C0504D')

axA.set_xlim(x0 - 2.35, x1 + 1.15); axA.set_ylim(0.1, len(spans) + 0.95)
axA.set_xticks(years); axA.set_xticklabels([str(y) for y in years], fontsize=8, color=NAVY)
axA.set_yticks([]); axA.tick_params(axis='x', length=0)
for s in axA.spines.values(): s.set_visible(False)
axA.set_title('A. 数据源覆盖与样本分期', fontsize=9.5, color=NAVY, loc='left', pad=6)

# --- B: 单次预测的错位时间窗 ---
axB.set_xlim(-0.5, 10.5); axB.set_ylim(0, 4.4)
def bar(y, a, b, col, lab, txtcol='white'):
    axB.add_patch(FancyBboxPatch((a, y), b - a, 0.52, boxstyle='round,pad=0.02,rounding_size=0.06',
                                 fc=col, ec='none'))
    axB.text((a + b) / 2, y + 0.26, lab, ha='center', va='center', fontsize=7.8, color=txtcol)

bar(3.3, 0.2, 2.6, '#8DB4E2', '基础情绪易感性 X\n窗口（t-2）', NAVY)
bar(2.5, 2.8, 4.6, '#4F81BD', '协同推广 M\n窗口（t-1）')
bar(1.7, 0.2, 4.6, '#B9CDE5', '市场 / 链上控制变量（截至预测起点）', NAVY)
bar(0.9, 5.6, 9.9, '#C0504D', '结果窗口：未来 h 内是否发生 BSADF 进入')
axB.add_patch(Rectangle((4.6, 0.8), 1.0, 3.1, fc='#F2F2F2', ec='#BFBFBF', ls='--', lw=0.8))
axB.text(5.1, 2.35, '缓冲区', ha='center', va='center', fontsize=7.5, color=GREY, rotation=90)
axB.axvline(4.6, color=NAVY, lw=1.4)
axB.text(4.6, 4.05, '预测起点 t（特征截止）', ha='center', fontsize=8, color=NAVY)
axB.text(5.05, 0.35, '自动校验：feature_cutoff ≤ forecast_origin < outcome_start',
         ha='left', fontsize=7.5, color=GREY)
axB.axis('off')
axB.set_title('B. 单次预测的错位时间窗（H2 要求 X 与 M 的窗口和构造成分均互斥）',
              fontsize=9.5, color=NAVY, loc='left', pad=6)

plt.tight_layout()
plt.savefig('fig3.png', dpi=190, facecolor='white')
plt.close()

# ---------------- 图 4：识别策略与允许表述 ----------------
fig, ax = plt.subplots(figsize=(10.2, 5.4))
ax.set_xlim(0, 10); ax.set_ylim(0, 6.6); ax.axis('off')

cols = [
    (0.15, '假设', ['H1\n增量预测', 'H2\n机制 / 中介', 'H3\n阶段与先后']),
    (2.35, '主估计量', ['同复杂度基准 vs 社交模型的\n时间外 PR-AUC / 校准 / 提前期差',
                        '概率尺度自然间接效应 NIE\n（g-computation）',
                        '阶段脉冲响应；两两先后概率；\n事件层面 Kendall τ']),
    (5.35, '关键识别条件', ['基准与完整模型函数复杂度对齐；\n基准含 BSADF-临界值距离',
                            '严格错位窗口；X 与 M 构造成分\n与作者集合互斥；序贯可忽略性',
                            '实体与方向识别；\n块状置换保留序列相关']),
    (7.9, '允许的表述', ['预测效用，不等于因果', '中介关联；满足假设才作因果表述',
                          '动态与时间先后，不等于操纵认定']),
]
rowy = [4.35, 2.75, 1.15]
colw = [1.9, 2.7, 2.25, 1.95]
for (x, head, cells), w in zip(cols, colw):
    ax.text(x + w / 2, 5.95, head, ha='center', fontsize=9, color=NAVY, weight='bold')
    for y, txt in zip(rowy, cells):
        ax.add_patch(FancyBboxPatch((x, y), w, 1.25,
                                    boxstyle='round,pad=0.03,rounding_size=0.08',
                                    fc=LIGHT if head != '假设' else '#4F81BD',
                                    ec='#CBD3DD', lw=0.8))
        ax.text(x + w / 2, y + 0.625, txt, ha='center', va='center', fontsize=7.6,
                color='white' if head == '假设' else NAVY)
for y in rowy:
    for x, w in [(2.05, 0.28), (5.05, 0.28), (7.6, 0.28)]:
        ax.add_patch(FancyArrowPatch((x, y + 0.625), (x + w, y + 0.625),
                                     arrowstyle='-|>', mutation_scale=9, color=GREY, lw=0.9))
ax.text(0.15, 0.45, '否定条件见表 2；必须完成的稳健性检验见表 7；打开评价期前需冻结的决策见附录 A。',
        fontsize=7.6, color=GREY)
plt.tight_layout()
plt.savefig('fig4.png', dpi=190, facecolor='white')
plt.close()
print('figures written')
