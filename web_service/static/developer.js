const $ = (selector) => document.querySelector(selector);
const labels = { registry: '등기부등본', appraisal: '감정평가서', mortgage_list: '저당목록' };

function formatNumber(value) {
  return new Intl.NumberFormat('ko-KR').format(Number(value || 0));
}

function escapeHtml(value) {
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('\"', '&quot;').replaceAll("'", '&#39;');
}

function formatCost(value) {
  return `$${Number(value || 0).toFixed(6)}`;
}

function fileList(inputFiles) {
  return Object.entries(inputFiles || {}).map(([kind, name]) => `${escapeHtml(labels[kind] || kind)}: ${escapeHtml(name)}`).join('\n');
}

function llmBlock(summary) {
  const s = summary || {};
  return [
    `calls=${formatNumber(s.llmCallCount)}`,
    `tokens=${formatNumber(s.totalTokens)}`,
    `input=${formatNumber(s.inputTokens)} / output=${formatNumber(s.outputTokens)}`,
    `usd=${formatCost(s.estimatedTotalCostUsd)}`,
  ].join('\n');
}

function embeddingBlock(summary) {
  const e = summary || {};
  return [
    `calls=${formatNumber(e.embeddingCallCount)}`,
    `tokens=${formatNumber(e.embeddingTokens)}`,
    `usd=${formatCost(e.embeddingEstimatedCostUsd)}`,
  ].join('\n');
}

function pathsBlock(paths) {
  const keys = ['uploadDir', 'pipelineArtifactsDir', 'finalOutputPath', 'costSummaryPath', 'ragIndexPath', 'ragUsageSummaryPath'];
  return keys.filter((key) => paths?.[key]).map((key) => `${key}: ${paths[key]}`).join('\n');
}

async function loadDeveloperDashboard() {
  const response = await fetch('/api/developer/dashboard');
  if (!response.ok) return;
  const data = await response.json();
  $('#devHeroCost').textContent = formatCost(data.estimatedTotalCostUsd);
  $('#devHeroCalls').textContent = formatNumber(data.totalLlmCalls);
  $('#devHeroTokens').textContent = formatNumber(data.totalTokens);
  $('#devHeroJobs').textContent = formatNumber(data.totalJobs);
  $('#metricDevJobs').textContent = formatNumber(data.totalJobs);
  $('#metricDevCalls').textContent = formatNumber(data.totalLlmCalls);
  $('#metricDevTokens').textContent = formatNumber(data.totalTokens);
  $('#metricDevCost').textContent = formatCost(data.estimatedTotalCostUsd);

  const rows = $('#developerRows');
  if (!data.jobs.length) {
    rows.innerHTML = '<tr><td colspan="7">아직 작업이 없습니다.</td></tr>';
    return;
  }
  rows.innerHTML = data.jobs.map((job) => {
    const rag = job.ragCostSummary || {};
    return `
      <tr>
        <td>${escapeHtml(job.requestTime || '-')}</td>
        <td><div class="file-list">${fileList(job.inputFiles)}</div></td>
        <td><span class="status-chip ${job.status}">${job.status}</span><br />${Math.round(job.elapsedSeconds || 0)}초<br />${escapeHtml(job.error || '')}</td>
        <td><pre class="stage-pre">${escapeHtml(llmBlock(job.costSummary))}</pre></td>
        <td><pre class="stage-pre">${escapeHtml(llmBlock(rag.llm))}</pre></td>
        <td><pre class="stage-pre">${escapeHtml(embeddingBlock(rag.embedding))}</pre></td>
        <td><pre class="stage-pre">${escapeHtml(pathsBlock(job.paths))}</pre></td>
      </tr>`;
  }).join('');
}

$('#refreshDeveloperDashboard').addEventListener('click', loadDeveloperDashboard);
loadDeveloperDashboard();
window.setInterval(loadDeveloperDashboard, 8000);
