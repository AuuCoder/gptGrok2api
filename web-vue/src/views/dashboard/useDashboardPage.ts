import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { statsApi } from '@/api/stats'
import { usePageQuery } from '@/composables/usePageQuery'
import { usePageRuntime } from '@/composables/usePageRuntime'
import {
  getLineChartTheme,
  getPieChartTheme,
  createLineSeries,
  createPieDataItem,
  chartColors,
  getModelColor,
  filterValidModels,
} from '@/lib/chartTheme'
import { DEFAULT_DASHBOARD_TIME_RANGE, type DashboardTimeRange } from '@/lib/timeRanges'


export function useDashboardPage() {
  type ChartInstance = {
    setOption: (
      option: unknown,
      opts?: boolean | { notMerge?: boolean; lazyUpdate?: boolean; replaceMerge?: string[] }
    ) => void
    resize: () => void
    dispose: () => void
    clear?: () => void
    off?: (eventName: string) => void
    on?: (eventName: string, handler: (params: any) => void) => void
    dispatchAction?: (payload: Record<string, unknown>) => void
  }
  type RenderMode = 'initial' | 'range' | 'refresh'
  type ChartType = 'hourlyRequests' | 'trend' | 'successRate' | 'model' | 'modelRank' | 'responseTime'
  type OverviewPayload = Record<string, any>
  const pageRuntime = usePageRuntime('dashboard')
  const DASHBOARD_DATA_REQUEST_KEY = 'dashboard:data'
  const CHART_BOOTSTRAP_TIMER_KEY = 'dashboard:chart-bootstrap'
  const chartRequestKey = (chartType: ChartType) => `dashboard:chart:${chartType}`
  const dashboardDataQuery = usePageQuery({
    runtime: pageRuntime,
    key: DASHBOARD_DATA_REQUEST_KEY,
    errorMessage: '概览加载失败',
  })

  // 每个图表独立的时间范围
  const timeRangeHourlyRequests = ref<DashboardTimeRange>(DEFAULT_DASHBOARD_TIME_RANGE)
  const timeRangeTrend = ref<DashboardTimeRange>(DEFAULT_DASHBOARD_TIME_RANGE)
  const timeRangeSuccessRate = ref<DashboardTimeRange>(DEFAULT_DASHBOARD_TIME_RANGE)
  const timeRangeModel = ref<DashboardTimeRange>(DEFAULT_DASHBOARD_TIME_RANGE)
  const timeRangeModelRank = ref<DashboardTimeRange>(DEFAULT_DASHBOARD_TIME_RANGE)
  const timeRangeResponseTime = ref<DashboardTimeRange>(DEFAULT_DASHBOARD_TIME_RANGE)

  // 创建图表监听器的工厂函数
  function createChartWatcher(chartType: ChartType, updateFn: (mode?: RenderMode) => void) {
    return async (newVal: DashboardTimeRange) => {
      if (!pageRuntime.canRun.value) return
      const requestId = pageRuntime.nextRequest(chartRequestKey(chartType))
      const applied = await loadChartData(chartType, newVal, requestId)
      if (applied) updateFn('range')
    }
  }

  // 监听各图表时间范围变化 - 只更新对应图表
  watch(timeRangeHourlyRequests, createChartWatcher('hourlyRequests', updateHourlyRequestsChart))
  watch(timeRangeTrend, createChartWatcher('trend', updateTrendChart))
  watch(timeRangeSuccessRate, createChartWatcher('successRate', updateSuccessRateChart))
  watch(timeRangeModel, createChartWatcher('model', updateModelChart))
  watch(timeRangeModelRank, createChartWatcher('modelRank', updateModelRankChart))
  watch(timeRangeResponseTime, createChartWatcher('responseTime', updateResponseTimeChart))

  function createDefaultStats() {
    return [
      {
        label: '账号总数',
        value: '0',
        meta: '',
        icon: 'lucide:users',
        iconBg: 'bg-sky-100',
        iconColor: 'text-sky-600'
      },
      {
        label: '正常账号',
        value: '0',
        meta: '',
        icon: 'lucide:check-circle',
        iconBg: 'bg-emerald-100',
        iconColor: 'text-emerald-600'
      },
      {
        label: '限流账号',
        value: '0',
        meta: '',
        icon: 'lucide:clock',
        iconBg: 'bg-amber-100',
        iconColor: 'text-amber-600'
      },
      {
        label: '异常账号',
        value: '0',
        meta: '',
        icon: 'lucide:alert-circle',
        iconBg: 'bg-rose-100',
        iconColor: 'text-rose-600'
      },
      {
        label: '禁用账号',
        value: '0',
        meta: '',
        icon: 'lucide:ban',
        iconBg: 'bg-slate-100',
        iconColor: 'text-slate-600'
      },
      {
        label: '剩余额度',
        value: '0',
        meta: '',
        icon: 'lucide:coins',
        iconBg: 'bg-cyan-100',
        iconColor: 'text-cyan-600'
      },
    ]
  }

  const stats = ref(createDefaultStats())

  // 每个图表独立的数据状态
  function createEmptyChartData() {
    return {
      hourlyRequests: {
        labels: [] as string[],
        modelRequests: {} as Record<string, number[]>,
      },
      trend: {
        labels: [] as string[],
        totalRequests: [] as number[],
        failedRequests: [] as number[],
        rateLimitedRequests: [] as number[],
        successRequests: [] as number[],
      },
      successRate: {
        labels: [] as string[],
        totalRequests: [] as number[],
        failedRequests: [] as number[],
      },
      model: {
        modelRequests: {} as Record<string, number[]>,
      },
      modelRank: {
        modelRequests: {} as Record<string, number[]>,
      },
      responseTime: {
        labels: [] as string[],
        modelTtfbTimes: {} as Record<string, number[]>,
        modelTotalTimes: {} as Record<string, number[]>,
      },
    }
  }

  const chartData = ref(createEmptyChartData())

  const overviewCache = new Map<string, OverviewPayload>()
  const overviewRequests = new Map<string, Promise<OverviewPayload>>()

  const trendChartRef = ref<HTMLDivElement | null>(null)
  const modelChartRef = ref<HTMLDivElement | null>(null)
  const successRateChartRef = ref<HTMLDivElement | null>(null)
  const hourlyRequestsChartRef = ref<HTMLDivElement | null>(null)
  const modelRankChartRef = ref<HTMLDivElement | null>(null)
  const responseTimeChartRef = ref<HTMLDivElement | null>(null)

  const charts = {
    trend: null as ChartInstance | null,
    model: null as ChartInstance | null,
    successRate: null as ChartInstance | null,
    hourlyRequests: null as ChartInstance | null,
    modelRank: null as ChartInstance | null,
    responseTime: null as ChartInstance | null,
  }

  type ChartKey = keyof typeof charts
  const renderProfiles: Record<RenderMode, {
    duration: number
    updateDuration: number
    delayStep: number
    lazyUpdate: boolean
  }> = {
    initial: { duration: 860, updateDuration: 620, delayStep: 14, lazyUpdate: false },
    range: { duration: 560, updateDuration: 460, delayStep: 8, lazyUpdate: false },
    refresh: { duration: 260, updateDuration: 220, delayStep: 0, lazyUpdate: true },
  }
  const chartFirstRenderState = ref<Record<ChartKey, boolean>>({
    trend: true,
    model: true,
    successRate: true,
    hourlyRequests: true,
    modelRank: true,
    responseTime: true,
  })
  const chartsBootstrapped = ref(false)
  const dashboardDataReady = ref(false)
  let dashboardEntrySeq = 0
  const modelLayoutIsMobile = ref<boolean | null>(null)

  function bindResizeListener() {
    window.removeEventListener('resize', handleResize)
    window.addEventListener('resize', handleResize)
  }

  function unbindResizeListener() {
    window.removeEventListener('resize', handleResize)
  }

  function applyAnimatedOption(key: ChartKey, option: Record<string, unknown>, mode: RenderMode = 'refresh') {
    const chart = charts[key]
    if (!chart) return
    const isFirstRender = chartFirstRenderState.value[key]
    const activeMode: RenderMode = isFirstRender ? 'initial' : mode
    const profile = renderProfiles[activeMode]
    const optionWithAnimation = {
      ...option,
      animation: true,
      animationDuration: profile.duration,
      animationDurationUpdate: profile.updateDuration,
      animationEasing: 'cubicOut',
      animationEasingUpdate: 'cubicOut',
      animationDelay: profile.delayStep > 0 ? (idx: number) => Math.min(idx * profile.delayStep, 180) : 0,
      animationDelayUpdate: profile.delayStep > 0 ? (idx: number) => Math.min(idx * Math.max(4, Math.floor(profile.delayStep / 2)), 120) : 0,
    }
    if (activeMode === 'range') {
      chart.clear?.()
    }
    chart.setOption(optionWithAnimation, {
      notMerge: activeMode === 'range',
      lazyUpdate: profile.lazyUpdate,
      replaceMerge: ['series', 'xAxis', 'yAxis', 'legend'],
    })
    chartFirstRenderState.value[key] = false
  }

  function initChart(
    ref: HTMLDivElement | null,
    key: ChartKey,
    updateFn: (mode?: RenderMode) => void
  ) {
    const echarts = (window as any).echarts as { init: (el: HTMLElement) => ChartInstance } | undefined
    if (!echarts || !ref) return
    charts[key] = echarts.init(ref)
    updateFn('initial')
  }

  function bootstrapCharts() {
    if (chartsBootstrapped.value) return
    initChart(trendChartRef.value, 'trend', updateTrendChart)
    initChart(modelChartRef.value, 'model', updateModelChart)
    initChart(successRateChartRef.value, 'successRate', updateSuccessRateChart)
    initChart(hourlyRequestsChartRef.value, 'hourlyRequests', updateHourlyRequestsChart)
    initChart(modelRankChartRef.value, 'modelRank', updateModelRankChart)
    initChart(responseTimeChartRef.value, 'responseTime', updateResponseTimeChart)
    chartsBootstrapped.value = true
  }

  function resetChartFirstRenderState() {
    chartFirstRenderState.value = {
      trend: true,
      model: true,
      successRate: true,
      hourlyRequests: true,
      modelRank: true,
      responseTime: true,
    }
  }

  function disposeCharts() {
    ;(Object.keys(charts) as ChartKey[]).forEach((key) => {
      charts[key]?.dispose()
      charts[key] = null
    })
    chartsBootstrapped.value = false
    resetChartFirstRenderState()
  }

  function clearChartBootstrapTimer() {
    pageRuntime.clearTimer(CHART_BOOTSTRAP_TIMER_KEY)
  }

  function cancelDashboardDataRequests(options: { clearRequests?: boolean } = {}) {
    dashboardDataQuery.invalidate()
    ;(['hourlyRequests', 'trend', 'successRate', 'model', 'modelRank', 'responseTime'] as ChartType[]).forEach((chartType) => {
      pageRuntime.invalidateRequest(chartRequestKey(chartType))
    })
    if (options.clearRequests !== false) {
      overviewRequests.clear()
    }
  }

  function resetDashboardViewState() {
    cancelDashboardDataRequests()
    dashboardDataReady.value = false
    stats.value = createDefaultStats()
    chartData.value = createEmptyChartData()
    overviewCache.clear()
    disposeCharts()
    clearChartBootstrapTimer()
    modelLayoutIsMobile.value = null
  }

  function scheduleChartBootstrap(delayMs = 80) {
    if (chartsBootstrapped.value) return
    clearChartBootstrapTimer()
    pageRuntime.setTimer(CHART_BOOTSTRAP_TIMER_KEY, delayMs, () => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          bootstrapCharts()
        })
      })
    })
  }

  pageRuntime.onActivate(({ initial }) => {
    bindResizeListener()
    if (initial) {
      void reloadDashboardOnEnter()
      return
    }
    void reloadDashboardOnEnter()
  })

  pageRuntime.onDeactivate(() => {
    unbindResizeListener()
    dashboardEntrySeq += 1
    resetDashboardViewState()
  })

  pageRuntime.onHide(() => {
    unbindResizeListener()
    dashboardEntrySeq += 1
    resetDashboardViewState()
  })

  pageRuntime.onShow(() => {
    bindResizeListener()
    void reloadDashboardOnEnter()
  })

  onBeforeUnmount(() => {
    unbindResizeListener()
    dashboardEntrySeq += 1
    clearChartBootstrapTimer()
    disposeCharts()
  })

  function updateTrendChart(mode: RenderMode = 'refresh') {
    if (!charts.trend) return

    const theme = getLineChartTheme()

    applyAnimatedOption('trend', {
      ...theme,
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.trend.labels,
      },
      series: [
        createLineSeries('成功', chartData.value.trend.successRequests, chartColors.primary, {
          areaOpacity: 0.25,
          zIndex: 1,
        }),
        createLineSeries('失败', chartData.value.trend.failedRequests, chartColors.danger, {
          areaOpacity: 0.3,
          zIndex: 2,
        }),
        createLineSeries('限流', chartData.value.trend.rateLimitedRequests, chartColors.warning, {
          areaOpacity: 0.3,
          zIndex: 2,
        }),
      ],
    }, mode)
  }

  function getModelTotals() {
    return Object.entries(chartData.value.model.modelRequests)
      .map(([model, data]) => ({
        model,
        data: createPieDataItem(model, data.reduce((sum, item) => sum + item, 0), getModelColor(model)),
        total: data.reduce((sum, item) => sum + item, 0),
      }))
      .filter(item => item.total > 0)
  }

  function updateModelChart(mode: RenderMode = 'refresh') {
    if (!charts.model) return

    const isMobile = window.innerWidth < 768
    modelLayoutIsMobile.value = isMobile
    const theme = getPieChartTheme(isMobile)
    const modelData = getModelTotals().map(item => item.data)
    const modelColors = modelData.map(item => String(item?.itemStyle?.color || getModelColor(String(item?.name || ''))))

    applyAnimatedOption('model', {
      ...theme,
      color: modelColors,
      tooltip: {
        ...theme.tooltip,
        formatter: (params: { name: string; value: number; percent: number }) =>
          `${params.name}: ${params.value} 次 (${params.percent}%)`,
      },
      legend: {
        ...theme.legend,
        data: modelData.map(item => item.name),
      },
      series: [
        {
          ...theme.series,
          center: ['50%', '50%'],
          data: modelData,
        },
      ],
    }, mode)
  }

  function handleResize() {
    Object.entries(charts).forEach(([key, chart]) => {
      if (chart) {
        if (key === 'model') {
          const nowMobile = window.innerWidth < 768
          if (modelLayoutIsMobile.value !== nowMobile) {
            updateModelChart()
          } else {
            chart.resize()
          }
        } else {
          chart.resize()
        }
      }
    })
  }

  function getChartRange(chartType: ChartType) {
    switch (chartType) {
      case 'hourlyRequests':
        return timeRangeHourlyRequests.value
      case 'trend':
        return timeRangeTrend.value
      case 'successRate':
        return timeRangeSuccessRate.value
      case 'model':
        return timeRangeModel.value
      case 'modelRank':
        return timeRangeModelRank.value
      case 'responseTime':
        return timeRangeResponseTime.value
    }
  }

  async function getOverview(timeRange: string, options: { force?: boolean } = {}) {
    if (!options.force) {
      const cached = overviewCache.get(timeRange)
      if (cached) return cached
    }

    const inflight = overviewRequests.get(timeRange)
    if (inflight && !options.force) return inflight

    const request = statsApi
      .overview(timeRange)
      .then((overview) => {
        const payload = overview as OverviewPayload
        if (overviewRequests.get(timeRange) === request) {
          overviewCache.set(timeRange, payload)
        }
        return payload
      })
      .finally(() => {
        if (overviewRequests.get(timeRange) === request) {
          overviewRequests.delete(timeRange)
        }
      })

    overviewRequests.set(timeRange, request)
    return request
  }

  function formatStatNumber(value: unknown) {
    const number = Number(value || 0)
    if (!Number.isFinite(number)) return '0'
    return Math.max(0, Math.trunc(number)).toLocaleString('zh-CN')
  }

  function applyAccountStats(overview: OverviewPayload) {
    stats.value[0].value = formatStatNumber(overview.total_accounts)
    stats.value[1].value = formatStatNumber(overview.active_accounts)
    stats.value[2].value = formatStatNumber(overview.rate_limited_accounts)
    stats.value[3].value = formatStatNumber(overview.abnormal_accounts)
    stats.value[4].value = formatStatNumber(overview.disabled_accounts)
    const totalQuota = Number(overview.total_quota || 0)
    stats.value[5].value = formatStatNumber(totalQuota)
    const providers = overview.account_providers || {}
    const gpt = providers.gpt || {}
    const grok = providers.grok || {}
    stats.value[0].meta = `GPT ${formatStatNumber(gpt.total)} · Grok ${formatStatNumber(grok.total)}`
    stats.value[1].meta = `GPT ${formatStatNumber(gpt.active)} · Grok ${formatStatNumber(grok.active)}`
    stats.value[5].meta = `GPT ${formatStatNumber(gpt.total_quota)} · Grok ${formatStatNumber(grok.total_quota)}`
  }

  function getTrendPayload(overview: OverviewPayload) {
    return overview.trend || {
      labels: [],
      total_requests: [],
      success_requests: [],
      failed_requests: [],
      rate_limited_requests: [],
      model_requests: {},
      model_ttfb_times: {},
      model_total_times: {},
    }
  }

  function normalizeLabels(raw: unknown): string[] {
    return Array.isArray(raw) ? raw.map(item => String(item || '')) : []
  }

  function normalizeNumberSeries(raw: unknown, pointCount?: number): number[] {
    const source = Array.isArray(raw) ? raw : []
    const normalized = source.map((item) => {
      const value = Number(item || 0)
      return Number.isFinite(value) ? value : 0
    })
    if (pointCount === undefined) return normalized
    return Array.from({ length: pointCount }, (_, index) => normalized[index] ?? 0)
  }

  function normalizeModelSeries(raw: unknown, pointCount?: number): Record<string, number[]> {
    const filtered = filterValidModels(raw as Record<string, number[]>)
    return Object.fromEntries(
      Object.entries(filtered).map(([model, data]) => [
        model,
        normalizeNumberSeries(data, pointCount),
      ])
    )
  }

  function applyOverviewToChartData(chartType: ChartType, overview: OverviewPayload) {
    const trend = getTrendPayload(overview)
    const labels = normalizeLabels(trend.labels)
    const pointCount = labels.length > 0 ? labels.length : undefined
    const totalSeries = normalizeNumberSeries(trend.total_requests, pointCount)
    const failed = normalizeNumberSeries(trend.failed_requests, pointCount)
    const limited = normalizeNumberSeries(trend.rate_limited_requests, pointCount)
    const success = normalizeNumberSeries(trend.success_requests, pointCount)
    const modelRequests = normalizeModelSeries(trend.model_requests, pointCount)
    const modelTtfbTimes = normalizeModelSeries(trend.model_ttfb_times, pointCount)
    const modelTotalTimes = normalizeModelSeries(trend.model_total_times, pointCount)
    const failureSeries = totalSeries.map((_: number, idx: number) => (failed[idx] || 0) + (limited[idx] || 0))
    const successSeries = totalSeries.map((total: number, idx: number) => {
      const explicitSuccess = Number(success[idx])
      if (Number.isFinite(explicitSuccess)) return explicitSuccess
      return Math.max(Number(total || 0) - (failed[idx] || 0) - (limited[idx] || 0), 0)
    })

    switch (chartType) {
      case 'hourlyRequests':
        chartData.value.hourlyRequests.labels = labels
        chartData.value.hourlyRequests.modelRequests = modelRequests
        break
      case 'trend':
        chartData.value.trend.labels = labels
        chartData.value.trend.totalRequests = totalSeries
        chartData.value.trend.failedRequests = failed
        chartData.value.trend.rateLimitedRequests = limited
        chartData.value.trend.successRequests = successSeries
        break
      case 'successRate':
        chartData.value.successRate.labels = labels
        chartData.value.successRate.totalRequests = totalSeries
        chartData.value.successRate.failedRequests = failureSeries
        break
      case 'model':
        chartData.value.model.modelRequests = modelRequests
        break
      case 'modelRank':
        chartData.value.modelRank.modelRequests = modelRequests
        break
      case 'responseTime':
        chartData.value.responseTime.labels = labels
        chartData.value.responseTime.modelTtfbTimes = modelTtfbTimes
        chartData.value.responseTime.modelTotalTimes = modelTotalTimes
        break
    }
  }

  function getDashboardChartRanges(): Record<ChartType, DashboardTimeRange> {
    return {
      hourlyRequests: timeRangeHourlyRequests.value,
      trend: timeRangeTrend.value,
      successRate: timeRangeSuccessRate.value,
      model: timeRangeModel.value,
      modelRank: timeRangeModelRank.value,
      responseTime: timeRangeResponseTime.value,
    }
  }

  function getDashboardOverviewRanges(chartRanges: Record<ChartType, DashboardTimeRange>) {
    return Array.from(
      new Set<string>([
        DEFAULT_DASHBOARD_TIME_RANGE,
        ...Object.values(chartRanges),
      ])
    )
  }

  async function loadDashboardOverviewRanges(chartRanges: Record<ChartType, DashboardTimeRange>, force: boolean) {
    await Promise.all(
      getDashboardOverviewRanges(chartRanges).map((timeRange) => getOverview(timeRange, { force }))
    )
  }

  function applyDashboardOverview(chartRanges: Record<ChartType, DashboardTimeRange>) {
    const accountOverview = overviewCache.get(DEFAULT_DASHBOARD_TIME_RANGE)
    if (accountOverview) {
      applyAccountStats(accountOverview)
    }

    ;(['hourlyRequests', 'trend', 'successRate', 'model', 'modelRank', 'responseTime'] as ChartType[]).forEach((chartType) => {
      if (chartRanges[chartType] !== getChartRange(chartType)) return
      const overview = overviewCache.get(chartRanges[chartType])
      if (overview) applyOverviewToChartData(chartType, overview)
    })
  }

  async function refreshDashboardData(force = false) {
    const chartRanges = getDashboardChartRanges()

    const refreshed = await dashboardDataQuery.run(
      async () => {
        await loadDashboardOverviewRanges(chartRanges, force)
        return true
      },
      {
        apply: () => applyDashboardOverview(chartRanges),
        onError: (_message, error) => {
          console.error('Failed to refresh dashboard data:', error)
        },
        silentError: true,
      },
    )
    return Boolean(refreshed)
  }

  async function reloadDashboardOnEnter() {
    const entrySeq = ++dashboardEntrySeq
    resetDashboardViewState()
    await nextTick()
    const refreshed = await refreshDashboardData(true)
    if (entrySeq !== dashboardEntrySeq) return
    dashboardDataReady.value = true
    await nextTick()
    if (entrySeq !== dashboardEntrySeq) return
    scheduleChartBootstrap(refreshed ? 0 : 80)
  }

  async function loadChartData(chartType: ChartType, timeRange: DashboardTimeRange, requestId?: number) {
    try {
      const overview = await getOverview(timeRange)
      if (
        requestId !== undefined &&
        (!pageRuntime.isLatestRequest(chartRequestKey(chartType), requestId) || timeRange !== getChartRange(chartType))
      ) {
        return false
      }
      applyOverviewToChartData(chartType, overview)
      return true
    } catch (error) {
      console.error(`Failed to load ${chartType} data:`, error)
      return false
    }
  }

  function updateSuccessRateChart(mode: RenderMode = 'refresh') {
    if (!charts.successRate) return

    const theme = getLineChartTheme()
    const successRates = chartData.value.successRate.totalRequests.map((total, idx) => {
      const failure = chartData.value.successRate.failedRequests[idx] || 0
      return total > 0 ? Math.round(((total - failure) / total) * 100) : 100
    })

    applyAnimatedOption('successRate', {
      ...theme,
      tooltip: {
        ...theme.tooltip,
        trigger: 'axis',
        formatter: (params: any) => {
          if (!params || params.length === 0) return ''
          const param = params[0]
          return `<div style="font-weight: 600; margin-bottom: 4px;">${param.axisValue}</div>
            <div style="display: flex; justify-content: space-between; gap: 16px; align-items: center;">
              <span>${param.marker} ${param.seriesName}</span>
              <span style="font-weight: 600;">${param.value}%</span>
            </div>`
        },
      },
      grid: {
        ...theme.grid,
        top: 32,
        bottom: 32,
      },
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.successRate.labels,
      },
      yAxis: {
        ...theme.yAxis,
        max: 100,
        axisLabel: {
          ...theme.yAxis.axisLabel,
          formatter: '{value}%',
        },
      },
      series: [
        {
          name: '成功率',
          type: 'line',
          data: successRates,
          smooth: true,
          showSymbol: false,
          lineStyle: {
            width: 3,
          },
          areaStyle: {
            opacity: 0.3,
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: chartColors.success },
                { offset: 1, color: 'rgba(16, 185, 129, 0.1)' },
              ],
            },
          },
          itemStyle: {
            color: chartColors.success,
          },
        },
      ],
    }, mode)
  }

  function updateHourlyRequestsChart(mode: RenderMode = 'refresh') {
    if (!charts.hourlyRequests) return

    const theme = getLineChartTheme()
    const modelNames = Object.keys(chartData.value.hourlyRequests.modelRequests)

    if (modelNames.length === 0) {
      applyAnimatedOption('hourlyRequests', {
        ...theme,
        grid: {
          ...theme.grid,
          left: 34,
          right: 24,
          top: 32,
          bottom: 32,
        },
        xAxis: {
          ...theme.xAxis,
          data: chartData.value.hourlyRequests.labels,
          boundaryGap: true,
        },
        yAxis: {
          ...theme.yAxis,
        },
        series: [
          {
            name: '总请求',
            type: 'bar',
            data: [],
            barWidth: '60%',
            itemStyle: {
              color: chartColors.primary,
              borderRadius: [4, 4, 0, 0],
            },
          },
        ],
      }, mode)
      return
    }

    const pointCount = chartData.value.hourlyRequests.labels.length
    const topSeriesIndexByPoint = Array.from({ length: pointCount }, (_, pointIndex) => {
      for (let seriesIndex = modelNames.length - 1; seriesIndex >= 0; seriesIndex -= 1) {
        const value = Number(chartData.value.hourlyRequests.modelRequests[modelNames[seriesIndex]]?.[pointIndex] || 0)
        if (value > 0) return seriesIndex
      }
      return -1
    })

    const series = modelNames.map((modelName, seriesIndex) => ({
      name: modelName,
      type: 'bar',
      stack: 'total',
      itemStyle: {
        color: getModelColor(modelName),
      },
      data: (chartData.value.hourlyRequests.modelRequests[modelName] || []).map((value, pointIndex) => ({
        value,
        itemStyle: {
          color: getModelColor(modelName),
          borderRadius: topSeriesIndexByPoint[pointIndex] === seriesIndex ? [4, 4, 0, 0] : [0, 0, 0, 0],
        },
      })),
    }))

    applyAnimatedOption('hourlyRequests', {
      ...theme,
      color: modelNames.map(name => getModelColor(name)),
      tooltip: {
        ...theme.tooltip,
        trigger: 'axis',
        axisPointer: {
          type: 'shadow',
        },
        formatter: (params: any) => {
          if (!params || params.length === 0) return ''
          let result = `<div style="font-weight: 600; margin-bottom: 4px;">${params[0].axisValue}</div>`
          let total = 0
          params.forEach((item: any) => {
            total += item.value || 0
            result += `<div style="display: flex; justify-content: space-between; gap: 16px; align-items: center;">
              <span>${item.marker} ${item.seriesName}</span>
              <span style="font-weight: 600;">${item.value || 0}</span>
            </div>`
          })
          result += `<div style="margin-top: 6px; padding-top: 6px; border-top: 1px solid #e5e5e5; font-weight: 600;">
            总计: ${total}
          </div>`
          return result
        },
      },
      legend: {
        ...theme.legend,
        data: modelNames,
        top: 0,
        right: 0,
        type: 'scroll',
        pageIconSize: 10,
        pageTextStyle: {
          fontSize: 10,
        },
      },
      grid: {
        ...theme.grid,
        left: 34,
        right: 24,
        top: modelNames.length > 5 ? 56 : 48,
        bottom: 32,
      },
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.hourlyRequests.labels,
        boundaryGap: true,
      },
      yAxis: {
        ...theme.yAxis,
      },
      series: series,
    }, mode)

  }

  function updateModelRankChart(mode: RenderMode = 'refresh') {
    if (!charts.modelRank) return

    const theme = getLineChartTheme()
    const modelTotals = Object.entries(chartData.value.modelRank.modelRequests)
      .map(([model, data]) => ({
        model,
        total: data.reduce((sum, item) => sum + item, 0),
      }))
      .filter(item => item.total > 0)
      .sort((a, b) => b.total - a.total)

    const modelNames = modelTotals.map(item => item.model)
    const modelValues = modelTotals.map(item => item.total)
    const modelColors = modelNames.map(name => getModelColor(name))

    applyAnimatedOption('modelRank', {
      ...theme,
      grid: {
        left: 12,
        right: 60,
        top: 16,
        bottom: 16,
        containLabel: true,
      },
      xAxis: {
        type: 'value',
        minInterval: 1,
        axisLine: {
          show: false,
        },
        axisTick: {
          show: false,
        },
        axisLabel: {
          ...theme.xAxis.axisLabel,
          fontSize: 10,
          formatter: (value: number) => `${Math.trunc(Number(value || 0))}`,
        },
        splitLine: {
          lineStyle: {
            color: '#e5e5e5',
            type: 'solid',
          },
        },
      },
      yAxis: {
        type: 'category',
        data: modelNames,
        axisLine: {
          show: false,
        },
        axisTick: {
          show: false,
        },
        axisLabel: {
          ...theme.yAxis.axisLabel,
          fontSize: 11,
        },
      },
      series: [
        {
          type: 'bar',
          data: modelValues.map((value, idx) => ({
            value,
            itemStyle: {
              color: modelColors[idx],
              borderRadius: [0, 4, 4, 0],
            },
          })),
          barWidth: '50%',
          label: {
            show: true,
            position: 'right',
            fontSize: 11,
            color: '#6b6b6b',
            formatter: '{c}',
          },
        },
      ],
    }, mode)
  }

  function updateResponseTimeChart(mode: RenderMode = 'refresh') {
    if (!charts.responseTime) return

    const theme = getLineChartTheme()
    const responseSeriesByModel = chartData.value.responseTime.modelTotalTimes
    const modelNames = Object.keys(responseSeriesByModel)
      .filter((modelName) => (responseSeriesByModel[modelName] || []).some((value) => Number(value || 0) > 0))

    if (modelNames.length === 0) {
      applyAnimatedOption('responseTime', {
        ...theme,
        grid: {
          ...theme.grid,
          top: 32,
          bottom: 32,
        },
        xAxis: {
          ...theme.xAxis,
          data: chartData.value.responseTime.labels,
        },
        yAxis: {
          ...theme.yAxis,
          axisLabel: {
            ...theme.yAxis.axisLabel,
            formatter: '{value}s',
          },
        },
        series: [],
      }, mode)
      return
    }

    const series = modelNames.map((modelName) => {
      const color = getModelColor(modelName)
      const seconds = (responseSeriesByModel[modelName] || []).map((ms) => Number((Number(ms || 0) / 1000).toFixed(2)))
      return createLineSeries(modelName, seconds, color, {
        smooth: true,
        areaOpacity: 0.15,
        zIndex: 2,
      })
    })

    applyAnimatedOption('responseTime', {
      ...theme,
      color: modelNames.map((modelName) => getModelColor(modelName)),
      tooltip: {
        ...theme.tooltip,
        trigger: 'axis',
        formatter: (params: any) => {
          if (!params || params.length === 0) return ''
          let result = `<div style="font-weight: 600; margin-bottom: 4px;">${params[0].axisValue}</div>`
          params.forEach((item: any) => {
            result += `<div style="display: flex; justify-content: space-between; gap: 16px; align-items: center;">
              <span>${item.marker} ${item.seriesName}</span>
              <span style="font-weight: 600;">${item.value || 0}s</span>
            </div>`
          })
          return result
        },
      },
      legend: {
        ...theme.legend,
        data: modelNames,
        top: 0,
        right: 0,
        type: 'scroll',
        pageIconSize: 10,
        pageTextStyle: {
          fontSize: 10,
        },
      },
      grid: {
        ...theme.grid,
        top: modelNames.length > 5 ? 56 : 48,
        bottom: 32,
      },
      xAxis: {
        ...theme.xAxis,
        data: chartData.value.responseTime.labels,
      },
      yAxis: {
        ...theme.yAxis,
        axisLabel: {
          ...theme.yAxis.axisLabel,
          formatter: '{value}s',
        },
      },
      series,
    }, mode)
  }

  return {
    stats,
    dashboardDataReady,
    timeRangeHourlyRequests,
    timeRangeTrend,
    timeRangeSuccessRate,
    timeRangeModel,
    timeRangeModelRank,
    timeRangeResponseTime,
    hourlyRequestsChartRef,
    trendChartRef,
    successRateChartRef,
    responseTimeChartRef,
    modelChartRef,
    modelRankChartRef,
  }
}
