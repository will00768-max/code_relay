const { createApp, ref, computed, onMounted, onUnmounted, nextTick } = Vue;

createApp({
  setup() {
    // ── 时钟 ──
    const now = ref(new Date());
    let clockTimer = null;
    const timeStr = computed(() =>
      now.value.toLocaleTimeString('zh-CN', { hour12: false })
    );
    const dateStr = computed(() =>
      now.value.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
    );

    // ── 余额 ──
    const balLoading = ref(false);
    const balRemain  = ref(null);
    const balToday   = ref(null);
    const balAll     = ref(null);
    const balTs      = ref('点击"刷新"后记录基准快照');
    const balErr     = ref('');

    async function loadBalance() {
      balLoading.value = true;
      balErr.value = '';
      try {
        const r = await fetch('/admin/balance');
        const d = await r.json();
        if (d.error) { balErr.value = d.error; return; }
        const b = d.balance_infos?.[0]?.total_balance;
        balRemain.value = b != null ? '¥' + Number(b).toFixed(4) : '--';
        const s = d._stats || {};
        balToday.value = s.today_spent != null ? '¥' + s.today_spent.toFixed(4) : '（需2次快照）';
        balAll.value   = s.total_spent != null ? '¥' + s.total_spent.toFixed(4) : '（需2次快照）';
        balTs.value    = '更新于 ' + new Date().toLocaleTimeString('zh-CN');
      } catch (e) {
        balErr.value = '请求失败: ' + e.message;
      } finally {
        balLoading.value = false;
      }
    }

    // ── Token 统计 ──
    const statsLoading = ref(false);
    const statsToday   = ref(null);
    const statsTotal   = ref(null);

    const fmt = n => (n ?? 0).toLocaleString();
    const fmtCost = v => v != null ? '≈ ¥' + Number(v).toFixed(4) : '--';

    async function loadStats() {
      statsLoading.value = true;
      try {
        const r = await fetch('/admin/stats');
        const d = await r.json();
        statsToday.value = d.today;
        statsTotal.value = d.total;
      } catch (e) {
        console.error('stats error', e);
      } finally {
        statsLoading.value = false;
      }
    }

    // ── API 调用详情 ──
    const callsLoading    = ref(false);
    const callsByModelToday = ref([]);
    const callsByModelTotal = ref([]);
    const callsRecent     = ref([]);
    const callsTab        = ref('recent'); // 'today' | 'total' | 'recent'

    // 最近记录分页
    const recentPage     = ref(1);
    const recentPageSize = 15;
    const recentTotalPages = computed(() => Math.max(1, Math.ceil(callsRecent.value.length / recentPageSize)));
    const recentPaged = computed(() => {
      const start = (recentPage.value - 1) * recentPageSize;
      return callsRecent.value.slice(start, start + recentPageSize);
    });

    async function loadCalls() {
      callsLoading.value = true;
      try {
        const r = await fetch('/admin/calls');
        const d = await r.json();
        callsByModelToday.value = d.by_model_today || [];
        callsByModelTotal.value = d.by_model_total || [];
        callsRecent.value       = d.recent || [];
        recentPage.value        = 1;
      } catch (e) {
        console.error('calls error', e);
      } finally {
        callsLoading.value = false;
      }
    }

    // ── 图表 ──
    const chartLoading = ref(false);
    const chartDays    = ref(30);
    let _charts = {};   // { cost, calls, tokens }

    const _CHART_DEFAULTS = {
      responsive: true,
      animation: false,
      plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
      scales: {
        x: { grid: { color: 'rgba(255,255,255,.05)' }, ticks: { color: '#64748b', font: { size: 11 } } },
        y: { grid: { color: 'rgba(255,255,255,.05)' }, ticks: { color: '#64748b', font: { size: 11 } }, beginAtZero: true },
      },
    };

    function _makeBarDataset(label, data, color) {
      return {
        label, data,
        backgroundColor: color,
        borderColor: color,
        borderRadius: 3,
        barPercentage: 0.6,
      };
    }

    function _makeLineDataset(label, data, color) {
      return {
        label, data,
        borderColor: color,
        backgroundColor: color + '33',
        borderWidth: 2,
        pointRadius: 2,
        fill: true,
        tension: 0.3,
      };
    }

    // 颜色池，按模型分配
    const _MODEL_COLORS = ['#4f8ef7','#22c55e','#f59e0b','#ef4444','#a78bfa','#06b6d4'];
    function _modelColor(name, idx) { return _MODEL_COLORS[idx % _MODEL_COLORS.length]; }

    function _initOrUpdate(id, type, labels, datasets) {
      if (_charts[id]) {
        _charts[id].data.labels = labels;
        _charts[id].data.datasets = datasets;
        _charts[id].update('none');
        return;
      }
      const ctx = document.getElementById(id);
      if (!ctx) return;
      _charts[id] = new Chart(ctx, {
        type,
        data: { labels, datasets },
        options: JSON.parse(JSON.stringify(_CHART_DEFAULTS)),
      });
    }

    async function loadChart() {
      chartLoading.value = true;
      try {
        const r = await fetch(`/admin/chart?days=${chartDays.value}`);
        const d = await r.json();
        const labels = d.labels;

        await nextTick();

        // 调用次数 - 按模型堆叠，tooltip 显示模型名和数量
        const models = Object.keys(d.by_model);
        const callsDs = models.length > 0
          ? models.map((m, i) =>
              ({ ..._makeBarDataset(m, d.by_model[m].calls, _modelColor(m, i)), stack: 'calls' })
            )
          : [_makeBarDataset('调用次数', d.calls, '#4f8ef7')];
        _initOrUpdate('chartCalls', 'bar', labels, callsDs);
        if (_charts['chartCalls']) {
          _charts['chartCalls'].options.plugins.legend.display = models.length > 1;
          _charts['chartCalls'].options.plugins.tooltip = {
            mode: 'index',
            intersect: false,
            callbacks: {
              label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y.toLocaleString()} 次`,
            },
          };
          _charts['chartCalls'].update('none');
        }

        // Token - 三类折线：缓存命中、缓存未命中、输出
        const tokDs = [
          _makeLineDataset('缓存命中输入', d.input_cache_hit_tokens,  '#06b6d4'),
          _makeLineDataset('缓存未命中输入', d.input_cache_miss_tokens, '#f59e0b'),
          _makeLineDataset('输出 Tokens',  d.output_tokens,           '#22c55e'),
        ];
        _initOrUpdate('chartTokens', 'line', labels, tokDs);
        if (_charts['chartTokens']) {
          _charts['chartTokens'].options.plugins.legend.display = true;
          _charts['chartTokens'].update('none');
        }
      } catch (e) {
        console.error('chart error', e);
      } finally {
        chartLoading.value = false;
      }
    }

    async function setChartDays(n) {
      chartDays.value = n;
      // 销毁旧图，重新创建以适应新数据范围
      Object.values(_charts).forEach(c => c.destroy());
      _charts = {};
      await loadChart();
    }
    const modelLoading  = ref(false);
    const modelList     = ref([]);
    const modelSelected = ref('');
    const modelCustom   = ref('');
    const modelMsg      = ref('');
    const modelMsgType  = ref('');
    const modelSource   = ref('');

    async function loadModel() {
      modelLoading.value = true;
      try {
        const [rList, rCur] = await Promise.all([
          fetch('/admin/models'),
          fetch('/admin/model'),
        ]);
        const dList = await rList.json();
        const dCur  = await rCur.json();
        modelList.value   = dList.models || [];
        modelSource.value = dList.source;
        const cur = dCur.model || '';
        if (modelList.value.includes(cur)) {
          modelSelected.value = cur;
          modelCustom.value   = '';
        } else {
          modelSelected.value = modelList.value[0] || '';
          modelCustom.value   = cur;
        }
      } catch (e) {
        modelMsg.value     = '加载失败: ' + e.message;
        modelMsgType.value = 'err';
      } finally {
        modelLoading.value = false;
      }
    }

    async function saveModel() {
      const model = modelCustom.value.trim() || modelSelected.value;
      try {
        const r = await fetch('/admin/model', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        const d = await r.json();
        if (d.ok) {
          modelMsg.value     = '✓ 已更新为 ' + d.model;
          modelMsgType.value = 'ok';
        } else {
          modelMsg.value     = '失败: ' + JSON.stringify(d);
          modelMsgType.value = 'err';
        }
      } catch (e) {
        modelMsg.value     = '请求失败';
        modelMsgType.value = 'err';
      }
    }

    // ── 生命周期 ──
    let statsTimer = null;
    let callsTimer = null;
    onMounted(() => {
      clockTimer  = setInterval(() => { now.value = new Date(); }, 1000);
      statsTimer  = setInterval(loadStats, 30000);
      callsTimer  = setInterval(loadCalls, 30000);
      loadBalance();
      loadStats();
      loadModel();
      loadCalls();
      loadChart();
    });
    onUnmounted(() => {
      clearInterval(clockTimer);
      clearInterval(statsTimer);
      clearInterval(callsTimer);
      Object.values(_charts).forEach(c => c.destroy());
    });

    return {
      timeStr, dateStr,
      // balance
      balLoading, balRemain, balToday, balAll, balTs, balErr, loadBalance,
      // stats
      statsLoading, statsToday, statsTotal, loadStats, fmt, fmtCost,
      // model
      modelLoading, modelList, modelSelected, modelCustom,
      modelMsg, modelMsgType, modelSource, loadModel, saveModel,
      // calls
      callsLoading, callsByModelToday, callsByModelTotal, callsRecent, callsTab, loadCalls,
      recentPage, recentTotalPages, recentPaged,
      // chart
      chartLoading, chartDays, loadChart, setChartDays,
    };
  },
}).mount('#app');
