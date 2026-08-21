const files = { registry: null, appraisal: null, mortgageList: null };
const labels = { registry: '등기부등본', appraisal: '감정평가서', mortgageList: '저당목록' };

const state = { jobId: localStorage.getItem('collateral:lastJobId'), pollTimer: null, dashboardTimer: null, ragReady: false, initialReportShown: false, historyJobId: null };

const $ = (selector) => document.querySelector(selector);
const startButton = $('#startButton');
const jobSummary = $('#jobSummary');
const progressFill = $('#progressFill');
const progressLabel = $('#progressLabel');
const elapsedPill = $('#elapsedPill');
const stepList = $('#stepList');
const stepTemplate = $('#stepTemplate');
const ragButton = $('#ragButton');
const reportCard = $('#reportCard');
const chatMessages = $('#chatMessages');
const chatForm = $('#chatForm');
const chatInput = $('#chatInput');
const chatSubmit = chatForm.querySelector('button');
const toast = $('#toast');

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('show');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('show'), 3200);
}

function formatBytes(bytes) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatNumber(value) {
  return new Intl.NumberFormat('ko-KR').format(Number(value || 0));
}

function escapeHtml(value) {
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('\"', '&quot;').replaceAll("'", '&#39;');
}

function fileList(inputFiles) {
  return Object.entries(inputFiles || {}).map(([kind, name]) => `${escapeHtml(labels[kind] || kind)}: ${escapeHtml(name)}`).join('\n');
}

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
    if (listType) {
      html.push(`</${listType}>`);
      listType = null;
    }
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
      const level = heading[1].length + 1;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    if (/^[-*_]{3,}$/.test(line.trim())) {
      closeList();
      html.push('<hr />');
      continue;
    }
    const unordered = /^[-*]\s+(.+)$/.exec(line.trim());
    if (unordered) {
      if (listType !== 'ul') {
        closeList();
        html.push('<ul>');
        listType = 'ul';
      }
      html.push(`<li>${renderInlineMarkdown(unordered[1])}</li>`);
      continue;
    }
    const ordered = /^\d+\.\s+(.+)$/.exec(line.trim());
    if (ordered) {
      if (listType !== 'ol') {
        closeList();
        html.push('<ol>');
        listType = 'ol';
      }
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

function setReportCard(title, markdown, downloadUrl = null) {
  reportCard.innerHTML = '<span class="orb"></span><h3></h3><div class="markdown-body"></div>';
  reportCard.querySelector('h3').textContent = title;
  reportCard.querySelector('.markdown-body').innerHTML = renderMarkdown(markdown);
  if (downloadUrl) {
    const actions = document.createElement('div');
    actions.className = 'hero-actions';
    actions.innerHTML = `<a class="button primary" href="${escapeHtml(downloadUrl)}">Excel 다운로드</a>`;
    reportCard.appendChild(actions);
  }
}

function bindDropzone(zone) {
  const kind = zone.dataset.kind;
  const input = zone.querySelector('input');
  const button = zone.querySelector('button');
  const openPicker = () => input.click();

  button.addEventListener('click', (event) => { event.stopPropagation(); openPicker(); });
  zone.addEventListener('click', openPicker);
  input.addEventListener('change', () => input.files[0] && setFile(kind, input.files[0]));

  for (const eventName of ['dragenter', 'dragover']) {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.add('dragging');
    });
  }
  for (const eventName of ['dragleave', 'drop']) {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.remove('dragging');
    });
  }
  zone.addEventListener('drop', (event) => {
    const file = event.dataTransfer.files[0];
    if (file) setFile(kind, file);
  });
}

function setFile(kind, file) {
  if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
    showToast(`${labels[kind]}은 PDF 파일만 업로드할 수 있습니다.`);
    return;
  }
  files[kind] = file;
  const zone = document.querySelector(`.dropzone[data-kind="${kind}"]`);
  zone.classList.add('ready');
  $(`#${kind}Name`).textContent = `${file.name} · ${formatBytes(file.size)}`;
  startButton.disabled = !Object.values(files).every(Boolean);
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function createJob() {
  startButton.disabled = true;
  startButton.textContent = '업로드 중...';
  ragButton.disabled = true;
  ragButton.textContent = '실행내용 기반 채팅시작';
  state.ragReady = false;
  state.initialReportShown = false;
  setChatEnabled(false);
  setReportCard('작업을 시작합니다', '문서 업로드 후 분석 단계로 이동합니다.');

  const payload = {
    registry: { filename: files.registry.name, contentBase64: await fileToBase64(files.registry) },
    appraisal: { filename: files.appraisal.name, contentBase64: await fileToBase64(files.appraisal) },
    mortgageList: { filename: files.mortgageList.name, contentBase64: await fileToBase64(files.mortgageList) },
  };
  const response = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  state.jobId = data.jobId;
  localStorage.setItem('collateral:lastJobId', data.jobId);
  showToast(`${data.requestTime} 요청이 접수되었습니다.`);
  pollJob(data.jobId);
  window.clearInterval(state.pollTimer);
  state.pollTimer = window.setInterval(() => pollJob(data.jobId), 1200);
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  if (!response.ok) return;
  const job = await response.json();
  renderJob(job);
  if (job.status === 'completed' || job.status === 'failed') {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
    startButton.textContent = 'Excel 자동화 시작';
    startButton.disabled = !Object.values(files).every(Boolean);
    loadDashboard();
  }
}

function renderJob(job) {
  if (state.historyJobId !== job.jobId) {
    chatMessages.replaceChildren();
    for (const message of (job.chatHistory || [])) {
      addMessage(message.role, message.role === 'user' ? '나' : '분석 어시스턴트', message.message || '');
    }
    state.historyJobId = job.jobId;
    state.initialReportShown = (job.chatHistory || []).some((message) => message.type === 'initial_report');
  }
  const progress = job.status === 'completed' ? 100 : job.progress;
  progressFill.style.width = `${progress}%`;
  progressLabel.textContent = `${progress}%`;
  elapsedPill.textContent = `${Math.round(job.elapsedSeconds || 0)}초`;
  stepList.replaceChildren(...job.steps.map(renderStep));

  if (job.status === 'completed') {
    jobSummary.textContent = '완료되었습니다. Excel 다운로드와 결과 리포트 확인이 가능합니다.';
    ragButton.disabled = false;
    const report = job.initialReport || '결과 리포트를 불러오는 중입니다. 잠시 후 작업 상태를 새로고침해 주세요.';
    setReportCard('첫 결과 보고서', report, job.downloadUrl);
    if (job.initialReport && !state.initialReportShown) {
      addMessage('assistant', '분석 어시스턴트', job.initialReport);
      state.initialReportShown = true;
    }
  } else if (job.status === 'failed') {
    jobSummary.textContent = '작업 처리 중 오류가 발생했습니다.';
    setReportCard('확인이 필요합니다', '문서 형식 또는 처리 단계에서 문제가 발생했습니다. 업로드 문서와 입력 유형을 확인해 주세요.');
  } else {
    const active = job.steps.find((step) => step.status === 'running');
    jobSummary.textContent = active ? `${active.label} 진행 중` : '작업 준비 중';
  }

  if (job.ragReady) {
    state.ragReady = true;
    ragButton.disabled = true;
    ragButton.textContent = `채팅 준비 완료 · ${formatNumber(job.ragChunkCount)}개 근거`;
    setChatEnabled(true);
  }
}

function renderStep(step) {
  const fragment = stepTemplate.content.cloneNode(true);
  const item = fragment.querySelector('.timeline-item');
  item.classList.add(step.status);
  fragment.querySelector('.step-title').textContent = step.label;
  fragment.querySelector('.step-detail').textContent = step.detail || '대기 중';
  return fragment;
}

async function buildRag() {
  if (!state.jobId) return;
  ragButton.disabled = true;
  ragButton.textContent = '보고서 작성 및 RAG 준비 중...';
  const response = await fetch(`/api/jobs/${state.jobId}/rag/build`, { method: 'POST' });
  if (!response.ok) {
    ragButton.disabled = false;
    ragButton.textContent = '실행내용 기반 채팅시작';
    throw new Error(await response.text());
  }
  const data = await response.json();
  state.ragReady = true;
  ragButton.textContent = `채팅 준비 완료 · ${formatNumber(data.chunkCount)}개 근거`;
  setChatEnabled(true);
  setReportCard('첫 결과 보고서', data.report || '보고서가 작성되었습니다. 산정 근거, 출처, 확인 필요 항목에 대해 질문해 주세요.');
  if (!state.initialReportShown) {
    addMessage('assistant', '분석 어시스턴트', data.report || '보고서가 작성되었습니다. 산정 근거, 출처, 확인 필요 항목에 대해 질문해 주세요.');
    state.initialReportShown = true;
  }
  loadDashboard();
}

function setChatEnabled(enabled) {
  chatInput.disabled = !enabled;
  chatSubmit.disabled = !enabled;
}

function addMessage(role, title, text) {
  const div = document.createElement('div');
  div.className = `message ${role}`;
  div.innerHTML = '<strong></strong><div class="message-content"></div>';
  div.querySelector('strong').textContent = title;
  const content = div.querySelector('.message-content');
  if (role === 'assistant') {
    content.classList.add('markdown-body');
    content.innerHTML = renderMarkdown(text);
  } else {
    content.textContent = text;
  }
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function sendChat(message) {
  addMessage('user', '나', message);
  chatInput.value = '';
  setChatEnabled(false);
  addMessage('assistant', '분석 어시스턴트', '분석 산출물에서 근거를 찾고 있습니다...');
  const lastMessage = chatMessages.lastElementChild.querySelector('.message-content');
  const response = await fetch(`/api/jobs/${state.jobId}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) {
    lastMessage.textContent = '답변 생성 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.';
    setChatEnabled(true);
    return;
  }
  const data = await response.json();
  const sources = (data.sources || []).slice(0, 3).map((source, index) => `${index + 1}. ${source.source} ${source.jsonPath}`).join('\n');
  lastMessage.innerHTML = renderMarkdown(`${data.answer}${sources ? `\n\n## 주요 출처\n${sources}` : ''}`);
  setChatEnabled(true);
}

async function loadDashboard() {
  const response = await fetch('/api/dashboard');
  if (!response.ok) return;
  const data = await response.json();
  if (!state.jobId && data.jobs.length) {
    state.jobId = data.jobs[0].jobId;
    localStorage.setItem('collateral:lastJobId', state.jobId);
    await restoreJob(state.jobId);
  }
  $('#metricJobs').textContent = formatNumber(data.totalJobs);
  $('#metricCompleted').textContent = formatNumber(data.completedJobs);
  $('#metricRunning').textContent = formatNumber(data.runningJobs);
  $('#metricFailed').textContent = formatNumber(data.failedJobs);
  const rows = $('#dashboardRows');
  if (!data.jobs.length) {
    rows.innerHTML = '<tr><td colspan="6">아직 작업이 없습니다.</td></tr>';
    return;
  }
  rows.innerHTML = data.jobs.map((job) => `
    <tr>
      <td>${escapeHtml(job.requestTime || '-')}</td>
      <td><div class="file-list">${fileList(job.inputFiles)}</div></td>
      <td><span class="status-chip ${job.status}">${job.status}</span></td>
      <td>${job.progress}%</td>
      <td>${escapeHtml(job.currentStep)}</td>
      <td>${job.downloadUrl ? `<a class="link-soft" href="${job.downloadUrl}">Excel</a>` : '-'}</td>
    </tr>`).join('');
}

async function restoreJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  if (!response.ok) {
    localStorage.removeItem('collateral:lastJobId');
    state.jobId = null;
    return;
  }
  const job = await response.json();
  state.jobId = job.jobId;
  renderJob(job);
  if (job.status !== 'completed' && job.status !== 'failed') {
    window.clearInterval(state.pollTimer);
    state.pollTimer = window.setInterval(() => pollJob(job.jobId), 1200);
  }
}

startButton.addEventListener('click', () => {
  createJob().catch((error) => {
    console.error(error);
    showToast('작업 시작에 실패했습니다. 파일을 다시 확인해 주세요.');
    startButton.textContent = 'Excel 자동화 시작';
    startButton.disabled = !Object.values(files).every(Boolean);
  });
});

ragButton.addEventListener('click', () => {
  buildRag().catch((error) => {
    console.error(error);
    showToast('채팅 준비 중 문제가 발생했습니다.');
  });
});

chatForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (message) sendChat(message);
});

$('#refreshDashboard').addEventListener('click', loadDashboard);
document.querySelectorAll('.dropzone').forEach(bindDropzone);
if (state.jobId) restoreJob(state.jobId).catch(() => {});
loadDashboard();
state.dashboardTimer = window.setInterval(loadDashboard, 8000);
