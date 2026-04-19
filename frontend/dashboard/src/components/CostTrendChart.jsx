import React from 'react'

function normalizeSeries(series) {
  if (!series || series.length === 0) return []

  const values = series.map((item) => Number(item.total_cost || 0))
  const max = Math.max(...values, 1)

  return series.map((item, index) => {
    const x = (index / Math.max(series.length - 1, 1)) * 100
    const y = 100 - (Number(item.total_cost || 0) / max) * 100
    return { x, y, item }
  })
}

export default function CostTrendChart({ series }) {
  const points = normalizeSeries(series)

  if (points.length === 0) {
    return <div className="empty-chart">暂无成本趋势数据</div>
  }

  const polyline = points.map((point) => `${point.x},${point.y}`).join(' ')

  return (
    <div className="trend-chart">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="成本趋势图">
        <defs>
          <linearGradient id="cost-line" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#17d4b2" />
            <stop offset="100%" stopColor="#ffbf46" />
          </linearGradient>
        </defs>
        <polyline points={polyline} fill="none" stroke="url(#cost-line)" strokeWidth="2.2" />
      </svg>
      <div className="trend-axis">
        <span>{series[0].day}</span>
        <span>{series[series.length - 1].day}</span>
      </div>
    </div>
  )
}
