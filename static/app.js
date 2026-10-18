/* =============================================================================
   《体检之后》前端逻辑
   =============================================================================
   调用链与后端三段式接口一一对应：

       POST /api/extract   图片 → 脱敏文本 + 四元组     （纯本地）
       POST /api/analyze   四元组 → 本地判定 + 红线     （纯本地）
       POST /api/explain/stream   判定结果 → 人话       （SSE 流式）

   进度条不造假：每个阶段的推进都对应一次真实的接口返回，
   「思考过程」里每一行都是后端实际报告的事实，不是定时器编出来的动画。

   流式通道不可用时（部分代理会缓冲 SSE）自动退回普通 POST，功能不受影响。
   ============================================================================= */

(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const state = {
    file: null,
    sex: 'male',          // 只保留男/女两档：性别会影响肌酐、血红蛋白等区间，
                          // 不提供「不确定」可避免走兜底区间（男女专属区间更准）
    extract: null,
    analysis: null,
    explain: null,
    week: null,           // 第 7 步取回的一周趋势（真实数据，失败时为 null）
    isLlm: true,          // 表达层是否真在调大模型；由 /api/about 校正，
                          // 用于让第 6 步的措辞不说假话
  };

  /* ─── 阶段定义 ───
     七步对应真实链路，顺序即用户看到的顺序：
       read     选好文件、装箱上传
       ocr      /api/extract 在途（服务端此时确实在跑 OCR）
       redact   /api/extract 返回后，汇报实际抹掉了哪些字段
       judge    /api/analyze 在途 + 返回（判定高低、匹配红线）
       verify   /api/explain/stream 在途：大模型写作 + 数字回溯校验
       write    收到 explain 事件后，正文逐段流式吐出
       redline  整理红线卡片 + 拉取 /api/week 的一周趋势
     进度百分比只用于让进度条推进得均匀，不冒充服务端回调。 */
  const STAGE_PCT = {
    read: 6, ocr: 22, redact: 38, judge: 56,
    verify: 72, write: 86, redline: 96,
  };
  const STAGE_ORDER = ['read', 'ocr', 'redact', 'judge', 'verify', 'write', 'redline'];

  /* ─── 小工具 ─── */
  const FLAG_TEXT = { high: '偏高', low: '偏低', normal: '正常', unknown: '待确认' };
  const FLAG_PILL = { high: 'pill-high', low: 'pill-low', normal: 'pill-normal', unknown: 'pill-unknown' };

  function fmt(v) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    return Number.isFinite(n) ? (Number.isInteger(n) ? String(n) : String(n)) : String(v);
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('is-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('is-show'), 3200);
  }

  function showView(name) {
    $('#view-home').classList.toggle('is-hidden', name !== 'home');
    $('#view-progress').classList.toggle('is-hidden', name !== 'progress');
    $('#view-result').classList.toggle('is-hidden', name !== 'result');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /* ─── 进度与思考日志 ─── */
  let progressValue = 0;      // 进度只增不减：SSE 事件的 progress 会乱序到达

  function resetProgress() {
    $$('.stage').forEach(s => s.classList.remove('is-active', 'is-done'));
    $('#thinking-log').innerHTML = '';
    progressValue = 0;
    setProgress(0);
    // 第 6 步措辞跟着表达层的真实模式走：没配密钥时不能声称在调 DeepSeek
    const w = $('.stage[data-stage="write"] .s-doing');
    if (w) {
      w.textContent = state.isLlm
        ? '正在让 DeepSeek 写人话解释…'
        : '正在用本地模板写人话解释…';
    }
  }

  function setProgress(pct) {
    const p = Math.max(0, Math.min(100, Math.round(pct)));
    if (p < progressValue) return;
    progressValue = p;
    $('#progress-bar').style.width = p + '%';
    $('#progress-pct').textContent = p + '%';
  }

  function setStage(name) {
    const idx = STAGE_ORDER.indexOf(name);
    $$('.stage').forEach(el => {
      const i = STAGE_ORDER.indexOf(el.dataset.stage);
      el.classList.toggle('is-done', i < idx);
      el.classList.toggle('is-active', i === idx);
    });
    if (STAGE_PCT[name] !== undefined) setProgress(STAGE_PCT[name]);
  }

  function completeStage(name) {
    const el = $(`.stage[data-stage="${name}"]`);
    if (el) { el.classList.remove('is-active'); el.classList.add('is-done'); }
  }

  function completeAllStages() {
    $$('.stage').forEach(el => {
      el.classList.remove('is-active');
      el.classList.add('is-done');
    });
  }

  function log(msg) {
    const ul = $('#thinking-log');
    const li = document.createElement('li');
    li.textContent = msg;
    ul.appendChild(li);
    ul.scrollTop = ul.scrollHeight;
  }

  /* ─── 网络 ─── */
  async function postJson(url, body) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `请求失败（${resp.status}）`);
    return data;
  }

  async function postExtract(file) {
    const fd = new FormData();
    fd.append('file', file);
    const resp = await fetch('/api/extract', { method: 'POST', body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `识别失败（${resp.status}）`);
    return data;
  }

  /* ─── 主流程 ─── */
  async function runFlow() {
    if (!state.file) { toast('请先选择一份报告照片'); return; }

    showView('progress');
    resetProgress();
    $('#progress-title').textContent = '正在解读…';

    try {
      /* ── 1 / 2 读取图片 + OCR ──
         服务端 /api/extract 内部顺序是「读图 → OCR → 脱敏 → 解析」，
         这里如实对应：请求发出后服务端正在 OCR，所以第 2 步点亮。 */
      setStage('read');
      log(`已选「${state.file.name}」，${(state.file.size / 1024).toFixed(0)} KB`);

      setStage('ocr');
      log('正在调用本地 OCR 引擎识别图片文字…');
      const ex = await postExtract(state.file);
      state.extract = ex;
      completeStage('ocr');
      log(`OCR 完成，提取到 ${ex.rows} 行文本`);

      /* ── 3 脱敏：返回里带着实际抹掉了哪些字段，照实汇报 ── */
      setStage('redact');
      const fields = (ex.privacy && ex.privacy.redacted_fields) || [];
      log(fields.length
        ? `已抹除个人信息：${fields.join('、')}`
        : '未检测到需要抹除的个人信息');
      log(`解析出 ${ex.markers.length} 个指标四元组`);
      completeStage('redact');

      if (!ex.markers.length) {
        throw new Error(
          '没有识别到任何指标。可能是照片不清晰、角度倾斜，或参考区间一列被裁掉了。'
          + '建议在自然光下把报告放平重拍，并确保右侧参考区间也在画面内。'
        );
      }

      /* ── 4 本地规则引擎：判定高低 + 匹配红线 ── */
      setStage('judge');
      log('正在查指标字典、比对参考区间、匹配日常红线…');
      const an = await postJson('/api/analyze', { markers: ex.markers, sex: state.sex });
      state.analysis = an;
      completeStage('judge');
      log(`判定完成：${an.matched} 项命中字典，${an.abnormal_count} 项不在参考区间内`);
      if (an.unmatched && an.unmatched.length) {
        log(`有 ${an.unmatched.length} 项未收录进字典，已标记为待确认`);
      }
      const nRedline = (an.redlines || []).length;
      log(nRedline ? `命中 ${nRedline} 条日常红线` : '未触发日常红线');

      /* 判定结果落库：红线成为「今日记录」页的日常对照依据 */
      try {
        const saved = await postJson('/api/report',
          { result: an, sex: state.sex, source: 'upload' });
        state.compare = saved.compare;      // 有上一份报告时返回趋势对比
        log(`已保存到本地档案，激活 ${nRedline} 条日常红线`);
      } catch { /* 落库失败不阻塞解读展示 */ }

      /* ── 5 / 6 表达层：校验与写人话由 SSE 真实事件推进 ── */
      setStage('verify');
      log('正在把正文里的每个数字回查到本地事实清单…');
      await streamExplain(an);

      /* ── 7 红线卡片 + 一周趋势 ── */
      await assembleRedlines(an);

      completeAllStages();
      renderResult(state.explain);

    } catch (err) {
      console.error(err);
      $('#progress-title').textContent = '出了点问题';
      log(`× ${err.message}`);
      toast(err.message);
      setTimeout(() => showView('home'), 2600);
    }
  }

  /* ─── 演示数据流程 ─── */
  async function runDemo() {
    showView('progress');
    resetProgress();
    $('#progress-title').textContent = '正在用演示数据跑一遍…';

    try {
      setStage('read');
      log('正在现场合成一份演示报告…');

      // /api/demo 一次调用里包含「合成 → OCR → 脱敏 → 解析」，
      // 所以第 2 步从请求发出到返回期间点亮，返回后一并结清第 2、3 步
      setStage('ocr');
      log('合成完成，正在调用本地 OCR 引擎识别…');
      const ex = await postJson('/api/demo', { sex: state.sex });
      state.extract = ex;
      completeStage('ocr');
      log(`OCR 完成，提取到 ${ex.rows} 行文本`);

      setStage('redact');
      const fields = (ex.privacy && ex.privacy.redacted_fields) || [];
      log(fields.length
        ? `已抹除个人信息：${fields.join('、')}`
        : '演示数据不含个人信息');
      log(`解析出 ${ex.markers.length} 个指标四元组`);
      completeStage('redact');

      setStage('judge');
      log('正在查指标字典、比对参考区间、匹配日常红线…');
      const an = await postJson('/api/analyze', { markers: ex.markers, sex: state.sex });
      state.analysis = an;
      completeStage('judge');
      log(`判定完成：${an.matched} 项命中字典，${an.abnormal_count} 项不在参考区间内`);
      const nRedline = (an.redlines || []).length;
      log(nRedline ? `命中 ${nRedline} 条日常红线` : '未触发日常红线');

      try {
        const saved = await postJson('/api/report',
          { result: an, sex: state.sex, source: 'demo' });
        state.compare = saved.compare;
        log(`已保存到本地档案，激活 ${nRedline} 条日常红线`);
      } catch { /* 落库失败不阻塞演示 */ }

      setStage('verify');
      log('正在把正文里的每个数字回查到本地事实清单…');
      await streamExplain(an);

      await assembleRedlines(an);

      completeAllStages();
      renderResult(state.explain);
    } catch (err) {
      console.error(err);
      $('#progress-title').textContent = '出了点问题';
      log(`× ${err.message}`);
      toast(err.message);
      setTimeout(() => showView('home'), 2600);
    }
  }

  /* ─── 第 7 步：红线卡片 + 一周趋势 ───
     红线其实在第 4 步就已经算出来了，这一步做的是「整理成可对照的卡片」
     并把日常层最近七天的真实记录取回来——取不到不影响结果页。 */
  async function assembleRedlines(analysis) {
    setStage('redline');
    const n = (analysis.redlines || []).length;
    log(n ? `${n} 条红线已整理成可对照的卡片` : '本次没有红线，已如实标注');

    try {
      const wk = await (await fetch('/api/week?days=7')).json();
      const series = wk.series || [];
      const days = series.filter(d => d.meals).length;
      const hits = series.reduce((a, d) => a + (d.hit_count || 0), 0);
      state.week = { days, hits };
      log(`最近七天：${days} 天有记录，累计撞线 ${hits} 次`);
    } catch {
      state.week = null;
    }

    setProgress(100);
    completeStage('redline');
  }

  /* ─── 流式解读（同时推进第 5、6 步的真实状态） ───
     服务端的事件顺序就是真实执行顺序：
       status/thinking judge → thinking redline*  判定与红线（第 4 步已做完，这里只是复述）
       —— 阻塞：大模型写作 + 数字回溯校验，校验不过会重写一次 ——
       thinking explain                            这条发出时校验已有结论
       token*                                      正文逐段吐出
       status family → done(payload)
     因此「校验完成」的分界点就是 explain 事件或第一个 token：
     在此之前第 5 步转圈，之后第 6 步转圈。 */
  async function streamExplain(analysis) {
    let payload = null;
    let streamed = '';
    let writing = false;

    const enterWrite = () => {
      if (writing) return;
      writing = true;
      completeStage('verify');
      setStage('write');
    };

    try {
      const resp = await fetch('/api/explain/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(analysis),
      });
      if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);

      const reader = resp.body.getReader();
      // stream: true 很关键——中文是多字节编码，一个汉字可能被拆在两个 chunk 里
      const decoder = new TextDecoder('utf-8');
      let buf = '';

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data: ')) continue;
            let ev;
            try { ev = JSON.parse(line.slice(6)); } catch { continue; }

            if (ev.stage === 'explain' || ev.type === 'token') enterWrite();
            if (ev.message && ev.type !== 'token') log(ev.message);
            if (ev.type === 'token' && ev.message) {
              streamed += ev.message;
              $('#summary-text').textContent = streamed;
            }
            if (ev.type === 'status' && ev.stage === 'family') completeStage('write');
            if (typeof ev.progress === 'number') {
              // 70→88：给第 7 步留出 88→100 的收尾区间，不到终点不冲满
              setProgress(70 + ev.progress * 0.18);
            }
            if (ev.type === 'done') payload = ev.payload;
          }
        }
      }
    } catch (e) {
      // SSE 被代理缓冲时走这条路，结果一致，只是没有逐字效果
      log('流式通道不可用，已切换普通请求');
      enterWrite();
      payload = await postJson('/api/explain', analysis);
    }

    completeStage('write');
    if (!payload) throw new Error('没有收到解读结果，请重试');

    state.explain = payload;
    logExplainFacts(payload);
    return payload;
  }

  /* 如实汇报表达层的实际情况：走了大模型、还是校验没过退回本地模板。
     这段日志是「数字不让大模型编」这条铁律在界面上可见的证据。 */
  function logExplainFacts(payload) {
    const qs = (payload.report && payload.report.follow_up_questions) || [];
    const fellBack = qs.some(q => q.includes('本地模板生成') || q.includes('大模型不可用'));

    if (payload.mode !== 'llm') {
      log('未配置大模型密钥：正文由本地模板生成，数字全部取自本地判定结果');
    } else if (fellBack) {
      log('数字回溯校验未通过：正文已退回本地模板，报告值仍由本地回填');
    } else {
      log('数字回溯校验通过：正文中的数字都能在本地事实清单里找到出处');
      log(`由 ${payload.model} 改写措辞；报告值由本地回填，未采信模型写的数字`);
    }
  }

  /* ─── 报告对比 ─── */
  const FLAG_CN = { high: '偏高', low: '偏低', normal: '正常', unknown: '待确认' };

  function renderCompare(diff) {
    const card = $('#compare-card');
    if (!diff) { card.classList.add('is-hidden'); return; }
    const c = diff.counts;
    if (!c.improved && !c.worsened && !c.persistent && !c.reversed && !c.new) {
      card.classList.add('is-hidden');
      return;
    }
    card.classList.remove('is-hidden');
    const chips = [
      { cls: 'is-ok', label: '好转', n: c.improved },
      { cls: 'is-warn', label: '加重', n: c.worsened },
      { cls: '', label: '持续异常', n: c.persistent },
      { cls: 'is-unknown', label: '方向反转', n: c.reversed },
      { cls: 'is-unknown', label: '新增', n: c.new },
    ].filter(x => x.n > 0);
    $('#compare-chips').innerHTML = chips.map(x =>
      `<span class="chip ${x.cls}">${x.label} <b>${x.n}</b> 项</span>`).join('');

    $('#compare-rows').innerHTML = diff.rows.map(r => `
      <div class="compare-row">
        <span class="compare-name">${escapeHtml(r.standard_name)}</span>
        <span class="verdict verdict-${r.verdict}">${escapeHtml(r.verdict_text)}</span>
        <span class="compare-vals">${
          r.from_value ? `${escapeHtml(r.from_value)}（${FLAG_CN[r.from_flag] || '—'}）` : '本次首次出现'
        } → ${escapeHtml(r.to_value)}（${FLAG_CN[r.to_flag] || '—'}）</span>
      </div>`).join('');
  }

  /* ─── 结果渲染 ─── */
  function renderResult(data) {
    renderCompare(state.compare);
    chat.reset();          // 换了一份报告，旧的问答就作废了
    const report = data.report || {};
    const an = state.analysis || {};
    const judged = an.judged || [];
    const explainMap = new Map((report.tests || []).map(t => [t.test_name, t]));

    const nTotal = an.matched ?? judged.length;
    const nOk = judged.filter(j => j.flag === 'normal').length;
    const nWarn = judged.filter(j => j.flag === 'high' || j.flag === 'low').length;
    const nUnknown = judged.filter(j => j.flag === 'unknown').length;

    /* 概览 chips */
    const chips = [
      { cls: '', label: '共识别', n: nTotal, unit: '项' },
      { cls: 'is-ok', label: '正常', n: nOk, unit: '项' },
      { cls: 'is-warn', label: '需注意', n: nWarn, unit: '项' },
      { cls: 'is-unknown', label: '待确认', n: nUnknown, unit: '项' },
    ];
    $('#chips').innerHTML = chips.map(c =>
      `<span class="chip ${c.cls}">${c.label} <b>${c.n}</b> ${c.unit}</span>`
    ).join('');

    /* 模式徽标：如实展示当前是本地模板还是大模型 */
    const isLlm = data.mode === 'llm';
    const badge = $('#mode-badge');
    badge.textContent = isLlm ? `AI 解读 · ${data.model}` : '本地模板解读';
    badge.classList.toggle('is-template', !isLlm);
    badge.title = isLlm
      ? '数字由本地链路定稿，大模型只改写措辞，输出经过数字回溯校验'
      : '未配置大模型密钥，解释由本地模板生成——产品功能不受影响';

    /* 整体解读 */
    $('#summary-text').textContent = report.summary || '';

    /* 逐项结果 */
    $('#marker-note').textContent = nWarn
      ? `点击任意一行可查看判定依据` : '所有指标均在参考区间内';
    $('#markers').innerHTML = judged.length
      ? judged.map(j => markerRow(j, explainMap.get(j.standard_name))).join('')
      : '<li class="empty-note">没有可展示的指标。</li>';
    $$('#markers .marker-row').forEach(row => {
      const toggle = () => {
        const open = row.parentElement.classList.toggle('is-open');
        row.setAttribute('aria-expanded', open ? 'true' : 'false');
      };
      row.addEventListener('click', toggle);
      row.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
      });
    });

    /* 日常红线 */
    const rl = an.redlines || [];
    $('#redlines').innerHTML = rl.length
      ? rl.map(r => redlineCard(r)).join('')
      : '<p class="empty-note">这份报告没有触发日常饮食红线。'
      + '三餐记录功能上线后，依然可以在这里看到每天的对照结果。</p>';

    /* 一周趋势：第 7 步顺带取回的真实数据，只有一句事实，不铺成图表 */
    const weekEl = $('#redline-week');
    const wk = state.week;
    if (weekEl) {
      weekEl.classList.toggle('is-hidden', !wk);
      weekEl.textContent = wk
        ? `最近七天：${wk.days} 天有记录，累计撞线 ${wk.hits} 次。去「今日记录」可以逐日回看。`
        : '';
    }

    /* 问诊清单 */
    const qs = report.follow_up_questions || [];
    $('#questions').innerHTML = qs.length
      ? qs.map(q => `<li>${escapeHtml(q)}</li>`).join('')
      : '<li class="empty-note">本次没有需要特别追问的问题。</li>';

    /* 给爸妈说的话 */
    const fam = data.family_message || {};
    $('#family-text').textContent = fam.message || '';

    /* 识别原文（演示模式下附上刚才合成的那份报告图） */
    const ex = state.extract || {};
    const rawFields = (ex.privacy && ex.privacy.redacted_fields) || [];
    $('#raw-note').textContent = rawFields.length
      ? `已抹除：${rawFields.join('、')}` : '未检测到个人信息';
    $('#raw-text').textContent = ex.text || '';
    $('#raw-extra').innerHTML = ex.image_b64
      ? `<img class="raw-image" alt="本次处理的报告图片"
              src="data:image/png;base64,${ex.image_b64}">`
      : '';

    /* 免责声明 */
    $('#disclaimer').textContent =
      report.disclaimer || '本内容不构成医疗建议，请遵医嘱。';

    showView('result');
  }

  function markerRow(j, explain) {
    const flag = j.flag || 'unknown';
    const ref = (j.ref && j.ref.length === 2)
      ? `${fmt(j.ref[0])} – ${fmt(j.ref[1])}` : '报告未提供';
    const unit = j.unit ? ` ${escapeHtml(j.unit)}` : '';
    const detail = explain || {};
    return `
      <li class="marker">
        <div class="marker-row" role="button" tabindex="0"
             aria-expanded="false" aria-label="${escapeHtml(j.standard_name || '')} 判定依据">
          <span class="marker-name">${escapeHtml(j.standard_name || '未知指标')}</span>
          <span class="marker-value">${escapeHtml(fmt(j.reported_value))}</span>
          <span class="marker-ref">参考 ${escapeHtml(ref)}${unit}</span>
          <span class="pill ${FLAG_PILL[flag]}">${FLAG_TEXT[flag] || '待确认'}</span>
          <span class="marker-caret">▶</span>
        </div>
        <div class="marker-detail">
          ${detail.simple_explanation ? `<p>${escapeHtml(detail.simple_explanation)}</p>` : ''}
          ${detail.recommended_next_step
            ? `<p><strong>下一步：</strong>${escapeHtml(detail.recommended_next_step)}</p>` : ''}
          ${j.reason ? `<div class="why">判定依据：${escapeHtml(j.reason)}</div>` : ''}
        </div>
      </li>`;
  }

  function redlineCard(r) {
    const boost = r.action === 'boost';
    const kind = boost ? '建议补充' : '日常限制';
    const foods = (r.trigger_foods || []).slice(0, 6);
    return `
      <div class="redline ${boost ? 'is-boost' : ''}">
        <div class="redline-head">
          <span class="redline-name">${escapeHtml(r.restriction || '')}</span>
          <span class="redline-kind">${kind}</span>
          <span class="redline-src">${escapeHtml(r.marker || '')} · ${escapeHtml(r.rule_id || '')}</span>
        </div>
        <p class="redline-advice">${escapeHtml(r.advice || '')}</p>
        ${foods.length
          ? `<div class="redline-foods">${foods.map(f =>
              `<span class="food-tag">${escapeHtml(f)}</span>`).join('')}</div>`
          : ''}
      </div>`;
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  /* ══════════════════ 问问 AI ══════════════════
     只把「问题 + 已判定好的结果」发给后端，绝不发图片、不发原始 OCR 文本。
     回答里的数字由后端回溯校验，前端只负责如实显示「校验过了没有、重写过几次」。
     这一行 meta 不是装饰：用户能一眼看出这次是 AI 写的还是本地拼的。 */

  const chat = {
    busy: false,

    reset() {
      const log = $('#chat-log');
      if (log) log.innerHTML = '';
      chat.busy = false;
      const btn = $('#btn-chat');
      if (btn) btn.disabled = false;
      const input = $('#chat-input');
      if (input) { input.disabled = false; input.value = ''; }
    },

    turn(question) {
      const wrap = document.createElement('div');
      wrap.className = 'chat-turn';
      wrap.innerHTML = '<p class="chat-q"></p>'
        + '<div class="chat-a"><span class="chat-spin"></span>正在查这份报告…</div>';
      wrap.querySelector('.chat-q').textContent = question;
      $('#chat-log').appendChild(wrap);
      wrap.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      return wrap;
    },

    fill(wrap, reply) {
      const a = wrap.querySelector('.chat-a');
      a.textContent = reply.answer;      // 纯文本渲染，不解析 markdown

      const bits = [];
      if (reply.mode === 'llm') {
        bits.push(`AI 解读 · ${reply.model}`);
        bits.push(reply.validated
          ? `数字已回溯 · ${reply.numbers_used.length} 个`
          : '含报告外数字');
        if (reply.attempts > 1) bits.push(`重写 ${reply.attempts - 1} 次`);
      } else {
        // 说「来自本地」而不是「来自报告」：阈值 140/90 这类本地常量不在报告上，
        // 但它们同样不是模型编的，用「本地」才既不夸大也不贬低
        bits.push('本地生成');
        if (reply.numbers_used.length) {
          bits.push(`数字全部来自本地 · ${reply.numbers_used.length} 个`);
        }
      }
      if (reply.foods && reply.foods.length) {
        bits.push(`菜名来自菜品库 · ${reply.foods.length} 道`);
      }
      // 提问里如果带了身份证/姓名，明确告诉用户已经抹掉了——「已脱敏」要看得见
      const masked = reply.masked_fields || [];
      if (masked.length) {
        const cn = { id_card: '身份证', phone: '电话', name: '姓名', long_number: '长编号' };
        bits.push('已抹除：' + [...new Set(masked.map(k => cn[k] || k))].join('、'));
      }

      const meta = document.createElement('p');
      meta.className = 'chat-meta' + (reply.validated ? '' : ' is-warn');
      meta.textContent = bits.join(' · ');
      wrap.appendChild(meta);
    },

    async ask(question) {
      const q = (question || '').trim();
      if (!q || chat.busy) return;
      if (!state.analysis) {
        toast('先解读一份报告，我才能对着它回答');
        return;
      }

      chat.busy = true;
      $('#btn-chat').disabled = true;
      $('#chat-input').disabled = true;
      const wrap = chat.turn(q);

      try {
        const reply = await postJson('/api/chat', {
          question: q,
          analysis: state.analysis,
        });
        chat.fill(wrap, reply);
      } catch (err) {
        const a = wrap.querySelector('.chat-a');
        a.classList.add('is-error');
        a.textContent = `没答上来：${err.message}`;
      } finally {
        chat.busy = false;
        $('#btn-chat').disabled = false;
        $('#chat-input').disabled = false;
      }
    },
  };

  function bindChat() {
    $$('#chat-quick .chat-ask').forEach(btn => {
      btn.addEventListener('click', () => chat.ask(btn.dataset.q));
    });

    $('#chat-form').addEventListener('submit', e => {
      e.preventDefault();
      const input = $('#chat-input');
      const q = input.value.trim();
      if (!q) return;
      input.value = '';
      chat.ask(q);
    });
  }

  /* ─── 文件选择 ─── */
  function bindFile(picked) {
    if (!picked) return;
    if (picked.size > 20 * 1024 * 1024) {
      toast('文件超过 20MB，请压缩后再试');
      return;
    }
    state.file = picked;
    $('#chosen-file').textContent = `${picked.name}（${(picked.size / 1024).toFixed(0)} KB）`;
    $('#btn-start').disabled = false;
  }

  function bindEvents() {
    const dz = $('#dropzone');
    const input = $('#file-input');

    dz.addEventListener('click', () => input.click());
    dz.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
    });
    input.addEventListener('change', e => bindFile(e.target.files[0]));

    ['dragenter', 'dragover'].forEach(ev =>
      dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('is-drag'); }));
    ['dragleave', 'drop'].forEach(ev =>
      dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('is-drag'); }));
    dz.addEventListener('drop', e => bindFile(e.dataTransfer.files[0]));

    $('#btn-start').addEventListener('click', runFlow);
    $('#btn-demo').addEventListener('click', runDemo);
    $('#btn-again').addEventListener('click', () => {
      state.file = null;
      state.extract = state.analysis = state.explain = state.compare = null;
      state.week = null;
      $('#file-input').value = '';
      $('#chosen-file').textContent = '尚未选择文件';
      $('#btn-start').disabled = true;
      showView('home');
    });

    $('#btn-copy').addEventListener('click', async () => {
      const text = $('#family-text').textContent;
      if (!text) { toast('还没有可复制的内容'); return; }
      try {
        await navigator.clipboard.writeText(text);
        toast('已复制，可以直接发家庭群了');
      } catch {
        // 浏览器禁用剪贴板 API 时退回传统方案
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
        toast('已复制，可以直接发家庭群了');
      }
    });

    $$('#sex-seg .seg').forEach(btn => {
      btn.addEventListener('click', () => {
        $$('#sex-seg .seg').forEach(b => {
          b.classList.remove('is-active');
          b.setAttribute('aria-checked', 'false');
        });
        btn.classList.add('is-active');
        btn.setAttribute('aria-checked', 'true');
        state.sex = btn.dataset.sex;
      });
    });
  }

  /* ══════════════════ 今日记录（日常层） ══════════════════ */

  const daily = {
    picked: new Set(),
    mealType: 'lunch',
    searchTimer: null,
  };

  const MEAL_TEXT = { breakfast: '早餐', lunch: '午餐', dinner: '晚餐', snack: '加餐' };
  const KEY_TEXT = {
    purine: '嘌呤', sugar: '糖', sodium: '钠', fat: '脂肪', alcohol: '酒', iron: '铁',
  };

  function setTab(name) {
    $('#tab-report').classList.toggle('is-active', name === 'report');
    $('#tab-daily').classList.toggle('is-active', name === 'daily');
    ['home', 'progress', 'result'].forEach(v =>
      $('#view-' + v).classList.toggle('is-hidden', name !== 'report'));
    $('#view-daily').classList.toggle('is-hidden', name !== 'daily');
    if (name === 'daily') loadDaily();
  }

  async function loadDaily() {
    $('#daily-date').textContent = new Date().toLocaleDateString('zh-CN',
      { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' });
    try {
      const [rl, today] = await Promise.all([
        fetch('/api/contract/active-redlines').then(r => r.json()),
        fetch('/api/today').then(r => r.json()),
      ]);
      renderActiveRedlines(rl.redlines || []);
      renderToday(today);
      renderDishList(await fetchDishes(''));
      refreshWeek();
    } catch (e) {
      toast('今日记录加载失败：' + e.message);
    }
  }

  function renderActiveRedlines(redlines) {
    const el = $('#active-redlines');
    if (!redlines.length) {
      el.innerHTML = '<p class="empty-note">还没有生效的红线。'
        + '先去「报告解读」上传一份体检报告，解读完成后红线会自动出现在这里。</p>';
      $('#redline-src-note').textContent = '等待第一份报告';
      return;
    }
    $('#redline-src-note').textContent = `来自最近一次报告 · 共 ${redlines.length} 条`;
    el.innerHTML = redlines.map(r => {
      const boost = r.action === 'boost';
      return `<span class="chip ${boost ? 'is-ok' : 'is-warn'}"
        style="margin:0 8px 8px 0">${escapeHtml(r.restriction)}
        <b style="font-size:13px">${boost ? '要补' : '盯住' + (KEY_TEXT[r.nutrient_key] || r.nutrient_key)}</b></span>`;
    }).join('');
  }

  async function fetchDishes(q) {
    const resp = await fetch('/api/dishes?q=' + encodeURIComponent(q));
    if (!resp.ok) throw new Error('菜品搜索失败');
    return (await resp.json()).dishes || [];
  }

  function dishDot(dish) {
    // 圆点颜色 = 该菜最"危险"的那一档（有 high 就红，否则有 medium 就黄，全低则绿）
    const tags = dish.tags || {};
    if (Object.values(tags).includes('high')) return 'dot-high';
    if (Object.values(tags).includes('medium')) return 'dot-med';
    return 'dot-low';
  }

  function renderDishList(dishes) {
    const el = $('#dish-list');
    el.innerHTML = dishes.length
      ? dishes.map(d => `
        <button type="button" class="dish-chip ${daily.picked.has(d.name) ? 'is-picked' : ''}"
                data-name="${escapeHtml(d.name)}" title="${dishTip(d)}">
          <span class="dot ${dishDot(d)}"></span>${escapeHtml(d.name)}
        </button>`).join('')
      : '<p class="empty-note">没有找到，换个关键词试试（例如：火锅 / 奶茶 / 鱼）</p>';
    $$('.dish-chip', el).forEach(chip => {
      chip.addEventListener('click', () => togglePick(chip.dataset.name));
    });
  }

  function dishTip(dish) {
    const tags = dish.tags || {};
    const highs = Object.entries(tags).filter(([, v]) => v === 'high')
      .map(([k]) => KEY_TEXT[k] || k);
    return highs.length ? `${dish.category} · 注意${highs.join('、')}` : dish.category;
  }

  function togglePick(name) {
    if (daily.picked.has(name)) daily.picked.delete(name);
    else daily.picked.add(name);
    $$('.dish-chip').forEach(chip => {
      if (chip.dataset.name === name) chip.classList.toggle('is-picked');
    });
    renderPicked();
  }

  function renderPicked() {
    const n = daily.picked.size;
    $('#picked-dishes').textContent =
      n ? [...daily.picked].join('、') : '还没选，点上面的菜名';
    $('#btn-meal').disabled = n === 0;
  }

  function renderToday(s) {
    $('#today-kcal').textContent =
      `已记 ${s.meal_count} 餐 · 估算 ${s.calories} 千卡` + (s.meal_count ? '' : '（还没记录）');

    const el = $('#today-hits');
    if (!s.redlines_active) {
      el.innerHTML = '<p class="empty-note">没有生效的红线，记录仍会保存。</p>';
    } else if (!s.hits.length) {
      el.innerHTML = `<div class="all-clear">今天 ${s.redlines_active} 条红线一条都没碰到</div>`;
    } else {
      el.innerHTML = '<div class="hit-list">' + s.hits.map(h => {
        const boost = h.action === 'boost';
        return `
          <div class="hit-item ${boost ? 'is-boost-hit' : 'is-hit'}">
            <div>
              <div class="hit-name">${escapeHtml(h.restriction)}</div>
              <div class="hit-meta">${boost ? '补铁达标' : '今天碰了线'} · 来源：${escapeHtml(h.marker)}</div>
              ${h.foods.length ? `<div class="hit-foods">${escapeHtml(h.foods.join('、'))}</div>` : ''}
            </div>
            <span class="hit-count">${h.hit_count} 次</span>
          </div>`;
      }).join('') + '</div>';
    }

    const mealsEl = $('#today-meals');
    mealsEl.innerHTML = (s.meals || []).length
      ? '<ul>' + s.meals.map(m =>
          `<li><span class="meal-tag">${MEAL_TEXT[m.meal_type] || m.meal_type}</span>${escapeHtml((m.dishes || []).join('、'))}</li>`
        ).join('') + '</ul>'
      : '';
    renderNumerics(s.numerics);
  }

  /* ══════════════════ 周报曲线（手绘 SVG，不引 CDN） ══════════════════ */

  const W = 660, H = 132, PAD_B = 24, GAP = 12;
  const _COLOR = { hit: '#9e422a', kcal: '#24423a', sys: '#9e422a', dia: '#24423a' };

  function esc(s) { return escapeHtml(String(s)); }

  function barChart(series, key, color, max) {
    const n = series.length;
    const slot = (W - GAP * (n - 1)) / n;
    const bw = slot * 0.56;          // 柱子只占槽位一半多，留白比塞满更耐看
    const bars = series.map((d, i) => {
      const v = d[key] || 0;
      const h = max ? (v / max) * (H - PAD_B - 14) : 0;
      const x = i * (slot + GAP) + (slot - bw) / 2;
      const y = H - PAD_B - h;
      return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}"
                    height="${Math.max(h, 1).toFixed(1)}" fill="${color}"/>`
        + (v ? `<text x="${(x + bw / 2).toFixed(1)}" y="${(y - 5).toFixed(1)}"
                      text-anchor="middle" font-size="11" fill="${color}"
                      font-weight="700">${v}</text>` : '')
        + `<text x="${(x + bw / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle"
                 font-size="10" fill="#8b9189">${esc(d.date.slice(5))}</text>`;
    }).join('');
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
                 role="img" aria-label="最近七天柱状图">
              <line x1="0" y1="${H - PAD_B}" x2="${W}" y2="${H - PAD_B}"
                    stroke="#e6e3db" stroke-width="1"/>${bars}</svg>`;
  }

  function lineChart(series, keyA, keyB) {
    const pts = series.map(d => [d[keyA], d[keyB]]);
    const all = pts.flat().filter(v => v !== null && v !== undefined);
    if (!all.length) return '';

    let lo = Math.min(...all), hi = Math.max(...all);
    if (hi - lo < 12) { const mid = (hi + lo) / 2; lo = mid - 6; hi = mid + 6; }
    const pad = (hi - lo) * 0.15;
    lo -= pad; hi += pad;

    const xAt = i => (i + 0.5) * (W / series.length);
    const yAt = v => 12 + (1 - (v - lo) / (hi - lo)) * (H - PAD_B - 12);

    function polyline(key, color) {
      // 缺失的日期断开线段——连起来会画出一段根本不存在的趋势
      const segs = [];
      let cur = [];
      series.forEach((d, i) => {
        if (d[key] === null || d[key] === undefined) {
          if (cur.length) segs.push(cur);
          cur = [];
        } else {
          cur.push([xAt(i), yAt(d[key]), d[key]]);
        }
      });
      if (cur.length) segs.push(cur);
      return segs.map(seg => `
        <polyline points="${seg.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')}"
                  fill="none" stroke="${color}" stroke-width="2"
                  stroke-linecap="round" stroke-linejoin="round"/>
        ${seg.map(p => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}"
                                r="3.5" fill="#fff" stroke="${color}" stroke-width="2"/>`).join('')}
        ${seg.map(p => `<text x="${p[0].toFixed(1)}" y="${(p[1] - 9).toFixed(1)}"
                              text-anchor="middle" font-size="10"
                              fill="${color}">${p[2]}</text>`).join('')}`).join('');
    }

    const labels = series.map((d, i) =>
      `<text x="${xAt(i).toFixed(1)}" y="${H - 6}" text-anchor="middle"
             font-size="10" fill="#8b9189">${esc(d.date.slice(5))}</text>`).join('');

    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
                 role="img" aria-label="最近七天血压折线图">
              <line x1="0" y1="${(H - PAD_B).toFixed(1)}" x2="${W}"
                    y2="${(H - PAD_B).toFixed(1)}" stroke="#e6e3db" stroke-width="1"/>
              ${polyline(keyA, _COLOR.sys)}${polyline(keyB, _COLOR.dia)}${labels}</svg>`;
  }

  async function refreshWeek() {
    try {
      const week = await (await fetch('/api/week?days=7')).json();
      const s = week.series || [];
      const maxHit = Math.max(2, ...s.map(d => d.hit_count));   // 至少 2 留余量，单根柱子才不会顶满
      const hasBp = s.some(d => d.systolic !== null);
      $('#week-note').textContent = s.filter(d => d.meals).length
        + ' / 7 天有记录';
      $('#week-charts').innerHTML = `
        <div class="chart">
          <div class="chart-title">每天碰到红线的次数
            <span class="legend"><i style="background:${_COLOR.hit}"></i>撞线</span></div>
          ${maxHit > 1 || s.some(d => d.hit_count)
            ? barChart(s, 'hit_count', _COLOR.hit, maxHit)
            : '<p class="chart-empty">这七天一条红线都没碰到</p>'}
        </div>
        <div class="chart">
          <div class="chart-title">血压趋势
            <span class="legend"><i style="background:${_COLOR.sys}"></i>收缩压</span>
            <span class="legend"><i style="background:${_COLOR.dia}"></i>舒张压</span></div>
          ${hasBp
            ? lineChart(s, 'systolic', 'diastolic')
            : '<p class="chart-empty">还没有血压记录，在上面记一次就会出曲线</p>'}
        </div>`;
    } catch { /* 曲线加载失败不影响其他功能 */ }
  }

  /* ══════════════════ 测量记录（血压 / 血糖 / 体重） ══════════════════ */

  const numeric = { kind: 'blood_pressure' };

  function renderNumerics(list) {
    const el = $('#today-numerics');
    if (!list || !list.length) { el.innerHTML = ''; return; }
    el.innerHTML = '<ul>' + list.map(n => {
      const txt = n.kind === 'blood_pressure'
        ? `${n.systolic ?? '—'} / ${n.diastolic ?? '—'} mmHg`
        : n.kind === 'blood_glucose'
          ? `${n.glucose ?? '—'} mmol/L` : `${n.weight ?? '—'} kg`;
      return `<li><span class="meal-tag">${
        { blood_pressure: '血压', blood_glucose: '血糖', weight: '体重' }[n.kind]
      }</span>${esc(txt)}${n.note ? ' · ' + esc(n.note) : ''}</li>`;
    }).join('') + '</ul>';
  }

  function bindDaily() {
    $('#tab-report').addEventListener('click', () => setTab('report'));
    $('#tab-daily').addEventListener('click', () => setTab('daily'));

    $$('#meal-seg .seg').forEach(btn => {
      btn.addEventListener('click', () => {
        $$('#meal-seg .seg').forEach(b => {
          b.classList.remove('is-active');
          b.setAttribute('aria-checked', 'false');
        });
        btn.classList.add('is-active');
        btn.setAttribute('aria-checked', 'true');
        daily.mealType = btn.dataset.meal;
      });
    });

    $('#dish-q').addEventListener('input', e => {
      clearTimeout(daily.searchTimer);
      const q = e.target.value;
      daily.searchTimer = setTimeout(async () => {
        try { renderDishList(await fetchDishes(q)); } catch { /* 搜索失败不打断 */ }
      }, 250);
    });

    $('#btn-meal').addEventListener('click', async () => {
      if (!daily.picked.size) return;
      try {
        const summary = await postJson('/api/meals', {
          meal_type: daily.mealType,
          dishes: [...daily.picked],
        });
        daily.picked.clear();
        renderPicked();
        renderToday(summary);
        refreshWeek();
        toast('已记录，对照结果更新了');
      } catch (e) {
        toast('记录失败：' + e.message);
      }
    });

    /* 测量记录：切换类型只显示对应输入框 */
    $$('#num-seg .seg').forEach(btn => {
      btn.addEventListener('click', () => {
        $$('#num-seg .seg').forEach(b => {
          b.classList.remove('is-active');
          b.setAttribute('aria-checked', 'false');
        });
        btn.classList.add('is-active');
        btn.setAttribute('aria-checked', 'true');
        numeric.kind = btn.dataset.kind;
        $$('.num-row').forEach(row => {
          row.classList.toggle('is-hidden', row.dataset.for !== numeric.kind);
        });
      });
    });

    $('#btn-num').addEventListener('click', async () => {
      const payload = { kind: numeric.kind };
      if (numeric.kind === 'blood_pressure') {
        payload.systolic = +$('#n-sys').value || null;
        payload.diastolic = +$('#n-dia').value || null;
        if (!payload.systolic && !payload.diastolic) {
          toast('至少填一个血压值'); return;
        }
      } else if (numeric.kind === 'blood_glucose') {
        payload.glucose = +$('#n-glu').value || null;
        if (!payload.glucose) { toast('请填写血糖值'); return; }
      } else {
        payload.weight = +$('#n-wt').value || null;
        if (!payload.weight) { toast('请填写体重'); return; }
      }
      try {
        const summary = await postJson('/api/numerics', payload);
        renderToday(summary);
        renderActiveRedlines((await (await fetch('/api/contract/active-redlines')).json()).redlines);
        refreshWeek();
        // 血压偏高激活限钠红线时，明确告诉用户发生了什么
        toast(summary.activated_redline
          ? `已记录。血压偏高，已激活「${summary.activated_redline}」红线`
          : '已记录');
        $('#n-sys').value = $('#n-dia').value = $('#n-glu').value = $('#n-wt').value = '';
      } catch (e) {
        toast('记录失败：' + e.message);
      }
    });
  }

  /* ══════════════════ 启动 ══════════════════ */

  async function boot() {
    bindEvents();
    bindDaily();
    bindChat();
    try {
      const info = await (await fetch('/api/about')).json();
      const isLlm = info.explain_mode === 'llm';
      state.isLlm = isLlm;              // 第 6 步的措辞据此决定，避免说假话
      const chip = $('#mode-chip');
      chip.textContent = isLlm ? `AI 解读 · ${info.explain_model}` : '本地模板解读';
      chip.title = isLlm
        ? '已配置大模型密钥，数字仍由本地链路定稿'
        : '未配置大模型密钥，解释由本地模板生成';
    } catch { /* 顶栏徽标不重要，失败就保持占位 */ }

    // ?demo=1 自动跑演示：录演示视频、跑无头回归、评委快速体验都用得上
    const params = new URLSearchParams(location.search);
    if (params.get('demo') === '1') {
      runDemo();
    } else if (params.get('tab') === 'daily') {
      setTab('daily');
    }
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
