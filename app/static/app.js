const { createApp, ref, computed, onMounted, onUnmounted } = Vue;

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

    // ── 模型配置 ──
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
    onMounted(() => {
      clockTimer  = setInterval(() => { now.value = new Date(); }, 1000);
      statsTimer  = setInterval(loadStats, 30000);
      loadBalance();
      loadStats();
      loadModel();
    });
    onUnmounted(() => {
      clearInterval(clockTimer);
      clearInterval(statsTimer);
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
    };
  },
}).mount('#app');
