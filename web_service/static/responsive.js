(() => {
  const isMobile = window.matchMedia('(max-width: 768px)').matches;
  const isTablet = window.matchMedia('(min-width: 769px) and (max-width: 1100px), (min-width: 769px) and (max-width: 1366px) and (hover: none) and (pointer: coarse)').matches;
  const root = isMobile
    ? document.querySelector('.mobile-view')
    : isTablet ? document.querySelector('.tablet-view') : null;
  if (!root) return;

  const files = { registry: null, appraisal: null, mortgageList: null };
  const labels = { registry: '등기부등본', appraisal: '감정평가서', mortgageList: '저당목록' };
  let jobId = localStorage.getItem('collateral:lastJobId');
  let pollTimer = null;
  let historyJobId = null;

  const one = (selector) => root.querySelector(selector);
  const many = (selector) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');

  function renderInlineMarkdown(value) {
    return escapeHtml(value)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_]+)__/g, '<strong>$1</strong>')
      .replace(/\*([^*]+)\*/g, '<em>$1</em>');
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n');
    const html = [];
    let listType = null;
    let inCode = false;
    let codeLines = [];
    const closeList = () => {
      if (listType) html.push(`</${listType}>`);
      listType = null;
    };

    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      if (line.trim().startsWith('```')) {
        if (inCode) {
          html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
          codeLines = [];
          inCode = false;
        } else {
          closeList();
          inCode = true;
        }
        continue;
      }
      if (inCode) {
        codeLines.push(rawLine);
        continue;
      }
      if (!line.trim()) {
        closeList();
        continue;
      }
      const heading = /^(#{1,4})\s+(.+)$/.exec(line);
      if (heading) {
        closeList();
        html.push(`<h${heading[1].length + 1}>${renderInlineMarkdown(heading[2])}</h${heading[1].length + 1}>`);
        continue;
      }
      const unordered = /^[-*]\s+(.+)$/.exec(line.trim());
      if (unordered) {
        if (listType !== 'ul') { closeList(); html.push('<ul>'); listType = 'ul'; }
        html.push(`<li>${renderInlineMarkdown(unordered[1])}</li>`);
        continue;
      }
      const ordered = /^\d+\.\s+(.+)$/.exec(line.trim());
      if (ordered) {
        if (listType !== 'ol') { closeList(); html.push('<ol>'); listType = 'ol'; }
        html.push(`<li>${renderInlineMarkdown(ordered[1])}</li>`);
        continue;
      }
      const quote = /^>\s?(.+)$/.exec(line.trim());
      if (quote) {
        closeList();
        html.push(`<blockquote>${renderInlineMarkdown(quote[1])}</blockquote>`);
        continue;
      }
      closeList();
      html.push(`<p>${renderInlineMarkdown(line)}</p>`);
    }
    if (inCode) html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
    closeList();
    return html.join('');
  }

  const fileToBase64 = (file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

  function setMenu(open) {
    const header = one('.responsive-header');
    const button = one('.responsive-menu-button');
    header.classList.toggle('menu-open', open);
    button.setAttribute('aria-expanded', String(open));
  }

  function addMessage(role, message) {
    const item = document.createElement('div');
    item.className = `responsive-message ${role}`;
    if (role === 'assistant') {
      item.classList.add('responsive-markdown');
      item.innerHTML = renderMarkdown(message);
    } else {
      item.textContent = message || '';
    }
    one('.responsive-messages').appendChild(item);
    one('.responsive-messages').scrollTop = one('.responsive-messages').scrollHeight;
  }

  function renderJob(job) {
    one('.responsive-percent').textContent = `${job.status === 'completed' ? 100 : job.progress}%`;
    one('.responsive-progress-fill').style.width = `${job.status === 'completed' ? 100 : job.progress}%`;
    one('.responsive-summary').textContent = job.error || (job.status === 'completed' ? '작업이 완료되었습니다.' : job.currentStep || '작업 대기 중');
    one('.responsive-steps').innerHTML = (job.steps || []).map((step) => `<li class="responsive-step ${step.status}">${escapeHtml(step.label)}<br /><small>${escapeHtml(step.detail || '')}</small></li>`).join('');

    if (historyJobId !== job.jobId) {
      one('.responsive-messages').replaceChildren();
      (job.chatHistory || []).forEach((message) => addMessage(message.role, message.message));
      historyJobId = job.jobId;
    }
    if (job.status === 'completed') {
      one('.responsive-report').innerHTML = renderMarkdown(job.initialReport || '결과 리포트가 준비되었습니다.');
      one('.responsive-rag').disabled = false;
    }
  }

  async function poll() {
    if (!jobId) return;
    const response = await fetch(`/api/jobs/${jobId}`);
    if (!response.ok) return;
    const job = await response.json();
    renderJob(job);
    if (job.status === 'completed' || job.status === 'failed') {
      clearInterval(pollTimer);
      pollTimer = null;
      loadDashboard();
    }
  }

  async function createJob() {
    const start = one('.responsive-start');
    start.disabled = true;
    start.textContent = '업로드 중...';
    const payload = {
      registry: { filename: files.registry.name, contentBase64: await fileToBase64(files.registry) },
      appraisal: { filename: files.appraisal.name, contentBase64: await fileToBase64(files.appraisal) },
      mortgageList: { filename: files.mortgageList.name, contentBase64: await fileToBase64(files.mortgageList) },
    };
    const response = await fetch('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    jobId = data.jobId;
    localStorage.setItem('collateral:lastJobId', jobId);
    clearInterval(pollTimer);
    poll();
    pollTimer = setInterval(poll, 1200);
    start.textContent = '분석 진행 중';
  }

  async function buildRag() {
    if (!jobId) return;
    const button = one('.responsive-rag');
    button.disabled = true;
    button.textContent = '채팅 준비 중...';
    const response = await fetch(`/api/jobs/${jobId}/rag/build`, { method: 'POST' });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    one('.responsive-report').innerHTML = renderMarkdown(data.report || '결과 리포트가 준비되었습니다.');
    button.textContent = `채팅 준비 완료 · ${data.chunkCount || 0}개 근거`;
    one('.responsive-chat-form input').disabled = false;
    one('.responsive-chat-form button').disabled = false;
  }

  async function sendChat(message) {
    addMessage('user', message);
    const response = await fetch(`/api/jobs/${jobId}/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }) });
    if (!response.ok) return addMessage('assistant', '답변 생성 중 문제가 발생했습니다.');
    const data = await response.json();
    const sources = (data.sources || []).slice(0, 3).map((source, index) => `${index + 1}. ${source.source} ${source.jsonPath}`).join('\n');
    addMessage('assistant', `${data.answer || '답변을 받지 못했습니다.'}${sources ? `\n\n## 주요 출처\n${sources}` : ''}`);
  }

  async function loadDashboard() {
    const response = await fetch('/api/dashboard');
    if (!response.ok) return;
    const data = await response.json();
    for (const [key, value] of Object.entries({ total: data.totalJobs, completed: data.completedJobs, running: data.runningJobs, failed: data.failedJobs })) {
      one(`[data-metric="${key}"]`).textContent = value;
    }
    one('.responsive-job-list').innerHTML = (data.jobs || []).slice(0, 8).map((job) => `<div class="responsive-job"><strong>${escapeHtml(job.status)}</strong> · ${escapeHtml(job.requestTime || '')}<br />${escapeHtml(Object.values(job.inputFiles || {}).join(', '))}</div>`).join('') || '<div class="responsive-job">아직 작업이 없습니다.</div>';
    if (!jobId && data.jobs?.[0]) {
      jobId = data.jobs[0].jobId;
      localStorage.setItem('collateral:lastJobId', jobId);
      await poll();
    }
  }

  many('.responsive-file').forEach((input) => input.addEventListener('change', () => {
    const kind = input.dataset.kind;
    files[kind] = input.files?.[0] || null;
    one(`[data-file-name="${kind}"]`).textContent = files[kind]?.name || '파일을 선택하세요';
    one('.responsive-start').disabled = !Object.values(files).every(Boolean);
  }));
  one('.responsive-start').addEventListener('click', () => createJob().catch(() => { one('.responsive-start').textContent = '다시 시도'; }));
  one('.responsive-rag').addEventListener('click', () => buildRag().catch(() => { one('.responsive-rag').textContent = '채팅 준비 실패'; }));
  one('.responsive-chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = one('.responsive-chat-form input');
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    sendChat(message);
  });
  one('.responsive-menu-button').addEventListener('click', () => setMenu(!one('.responsive-header').classList.contains('menu-open')));
  many('.responsive-menu a').forEach((link) => link.addEventListener('click', () => setMenu(false)));

  loadDashboard();
  if (jobId) {
    poll();
    pollTimer = setInterval(poll, 1200);
  }
})();
