const uploader = document.getElementById('uploader');
const fileInput = document.getElementById('file-input');
const folderInput = document.getElementById('folder-input');
const fileInfo = document.getElementById('file-info');
const startBtn = document.getElementById('start-btn');
const progressBox = document.getElementById('progress');
const progressText = document.getElementById('progress-text');
const errorBox = document.getElementById('error');
const resultBox = document.getElementById('result');
const resultJson = document.getElementById('result-json');
const outlineSection = document.getElementById('outline-section');
const outlineList = document.getElementById('outline-list');
const summarySection = document.getElementById('summary-section');
const summaryText = document.getElementById('summary-text');
const batchSection = document.getElementById('batch-section');
const batchSummary = document.getElementById('batch-summary');
const batchDetails = document.getElementById('batch-details');
const categorySection = document.getElementById('category-section');
const categoryText = document.getElementById('category-text');
const categoryCandidates = document.getElementById('category-candidates');
const subcategoryOptions = document.getElementById('subcategory-options');
const categoryConfirmBtn = document.getElementById('category-confirm-btn');
const categoryManualMessage = document.getElementById('category-manual-message');const securitySection = document.getElementById('security-section');
const securityText = document.getElementById('security-text');
// 统一 API 命名空间，避免未来与“对话”模块接口冲突
const API_BASE = '/api/classify';

let selectedFile = null;
let selectedFiles = null;
let currentTask = null;
let currentCategoryCandidates = [];
let currentSubcategoryOptions = [];
let batchCandidateMap = {};

const categoryDescriptions = {
  '军事基地': '军事设施、基地建设与布局',
  '体系运用': '作战体系与系统集成',
  '装备型号': '武器装备与技术参数',
  '作战环境': '战场环境与地理条件',
  '作战指挥': '指挥控制与决策流程'
};

const subCategoryDescriptions = {
  '空中装备': '战斗机、轰炸机、运输机、无人机等',
  '水面装备': '航母、驱逐舰、护卫舰、两栖舰等',
  '水下装备': '潜艇、水下无人器、鱼雷等',
  '条令条例': '联合出版物、指令、作战规程',
  '组织机构': '司令部、参联会、军兵种机构'
};

const subCategoryOptionsMap = {
  '装备型号': ['空中装备', '水面装备', '水下装备'],
  '作战指挥': ['条令条例', '组织机构']
};

function renderCategoryDisplay(categoryName, subCategory) {
  const description = categoryDescriptions[categoryName] || '';
  const subDesc = subCategory ? (subCategoryDescriptions[subCategory] || '') : '';
  
  let categoryHtml = `<strong>🏷️ ${categoryName}</strong>`;
  if (subCategory) {
    categoryHtml += `<span style="color:#2563eb;margin-left:8px;">› ${subCategory}</span>`;
  }
  if (description || subDesc) {
    categoryHtml += `<br><span style="color:#92400e;font-size:14px;font-weight:normal;margin-top:8px;display:inline-block;">${subDesc || description}</span>`;
  }
  return categoryHtml;
}

function getSelectedCandidate() {
  const selectedInput = document.querySelector('input[name="category-candidate"]:checked');
  if (!selectedInput) return null;
  const selectedIndex = Number(selectedInput.value);
  const candidate = currentCategoryCandidates[selectedIndex] || null;
  if (!candidate) return null;
  return { candidate, index: selectedIndex };
}

function renderSubcategoryOptions(categoryName, suggested, preselected) {
  const options = subCategoryOptionsMap[categoryName] || [];
  currentSubcategoryOptions = options;
  if (!options.length) {
    subcategoryOptions.innerHTML = '';
    subcategoryOptions.style.display = 'none';
    return;
  }

  const hintHtml = suggested
    ? `<div id="subcategory-hint">模型建议：${suggested}（请手动确认）</div>`
    : '';
  const itemsHtml = options
    .map((optionName, index) => {
      const checked = preselected && optionName === preselected ? 'checked' : '';
      const desc = subCategoryDescriptions[optionName] || '';
      const descHtml = desc ? `<span style="color:#6b7280;font-size:13px;">${desc}</span>` : '';
      return `<label class="subcategory-item"><input type="radio" name="category-subcategory" value="${optionName}" ${checked} />` +
        `<strong>${optionName}</strong>${descHtml}</label>`;
    })
    .join('');

  subcategoryOptions.innerHTML = `<div id="subcategory-title">请选择子分类</div>${hintHtml}${itemsHtml}`;
  subcategoryOptions.style.display = 'block';
}

function updateSubcategoryOptions() {
  const selectedInfo = getSelectedCandidate();
  if (!selectedInfo) {
    subcategoryOptions.innerHTML = '';
    subcategoryOptions.style.display = 'none';
    return;
  }
  const { candidate: selected, index: selectedIndex } = selectedInfo;
  const options = subCategoryOptionsMap[selected.category] || [];
  if (options.length > 0) {
    const hasSuggested = options.includes(selected.sub_category);
    const suggested = hasSuggested ? selected.sub_category : '';
    const preselected = hasSuggested ? selected.sub_category : options[0];
    renderSubcategoryOptions(selected.category, suggested, preselected);
    const target = categoryCandidates.querySelector(
      `.candidate-suboptions[data-index="${selectedIndex}"]`
    );
    if (target) {
      target.appendChild(subcategoryOptions);
      subcategoryOptions.style.display = 'block';
    }
  } else {
    subcategoryOptions.innerHTML = '';
    subcategoryOptions.style.display = 'none';
  }
}

function normalizeConfidence(value) {
  if (value === null || value === undefined || value === '') return null;
  const parsed = typeof value === 'number' ? value : Number(value);
  if (Number.isNaN(parsed)) return null;
  if (parsed > 1) return Math.max(0, Math.min(parsed / 100, 1));
  return Math.max(0, Math.min(parsed, 1));
}

function renderCategoryCandidates(candidates) {
  if (!Array.isArray(candidates)) return;
  currentCategoryCandidates = [];
  const items = [];
  candidates.forEach((candidate) => {
    if (!candidate || !candidate.category) return;
    const categoryName = String(candidate.category).trim();
    const subCategory = candidate.sub_category ? String(candidate.sub_category).trim() : '';
    const confidence = normalizeConfidence(candidate.confidence);
    const itemIndex = currentCategoryCandidates.length;
    currentCategoryCandidates.push({
      category: categoryName,
      sub_category: subCategory,
      confidence: confidence,
    });
    const label = subCategory ? `${categoryName} / ${subCategory}` : categoryName;
    const confidenceText = confidence === null ? '--' : `${(confidence * 100).toFixed(1)}%`;
    const checked = itemIndex === 0 ? 'checked' : '';
    items.push(
      `<div class="candidate-block" data-index="${itemIndex}">` +
      `<label class="candidate-item"><input type="radio" name="category-candidate" value="${itemIndex}" ${checked} />` +
      `<strong>${label}</strong><span class="candidate-confidence">${confidenceText}</span></label>` +
      `<div class="candidate-suboptions" data-index="${itemIndex}"></div>` +
      `</div>`
    );
  });
  const itemsHtml = items.join('');

  if (itemsHtml) {
    categoryCandidates.innerHTML = itemsHtml;
    categoryCandidates.style.display = 'block';
    categoryConfirmBtn.style.display = 'inline-flex';
    updateSubcategoryOptions();
  } else {
    categoryCandidates.innerHTML = '';
    categoryCandidates.style.display = 'none';
    categoryConfirmBtn.style.display = 'none';
    subcategoryOptions.innerHTML = '';
    subcategoryOptions.style.display = 'none';
  }
}

// 格式化信息抽取数据为HTML
function formatExtractData(extract) {
  if (!extract || typeof extract !== 'object') return '';
  
  const fieldLabels = {
    // 基础信息
    'base_name': '基地名称', 'location': '位置', 'country': '国家', 'military_branch': '军种',
    'facility_type': '设施类型', 'function': '功能', 'capacity': '容量', 'status': '状态',
    // 体系运用
    'system_type': '体系类型', 'components': '组成要素', 'capabilities': '能力特点',
    'coordination_mode': '协同方式', 'application_scenario': '应用场景',
    // 装备型号 - 基础信息
    'model': '型号', 'model_en': '英文型号', 'manufacturer': '制造商',
    'service_date': '服役时间', 'quantity': '数量', 'cost': '造价', 'features': '性能特点',
    // 装备型号 - 战技指标
    'dimensions': '尺寸', 'weight': '重量', 'performance': '性能', 'range': '航程/射程',
    'speed': '速度', 'ceiling': '升限', 'payload': '载荷',
    // 装备型号 - 运用数据
    'deployment': '部署情况', 'exercises': '演习参与', 'maintenance': '维修周期', 'incidents': '事故记录',
    // 装备型号 - 效能数据
    'damage_capability': '毁伤能力', 'vulnerability': '易损性',
    // 装备型号 - 目标特征
    'rcs': '雷达截面积', 'optical': '光学特征', 'infrared': '红外特征',
    // 作战环境
    'region': '区域', 'terrain': '地形', 'climate': '气候',
    'current': '海流', 'wave': '海浪', 'tide': '潮汐', 'temperature': '温度', 'salinity': '盐度',
    'electromagnetic': '电磁环境',
    // 作战指挥 - 条令条例
    'doc_name': '文件名称', 'doc_number': '文件编号', 'issuing_authority': '发布机构',
    'version': '版本', 'issue_date': '发布日期', 'scope': '适用范围', 'key_content': '主要内容',
    // 作战指挥 - 组织机构
    'org_name': '机构名称', 'commander': '指挥官', 'subordinate_units': '下属单位', 'mechanism': '运行机制'
  };
  
  const sectionLabels = {
    'basic_info': '📝 基础信息', 'specifications': '📊 战技指标',
    'operational_data': '🛠️ 运用数据', 'effectiveness': '🎯 效能数据',
    'signatures': '📡 目标特征', 'ocean_data': '🌊 海洋数据'
  };
  
  function renderObject(obj, depth = 0) {
    let html = '';
    for (const [key, value] of Object.entries(obj)) {
      if (value === null || value === undefined || value === '') continue;
      
      const label = fieldLabels[key] || sectionLabels[key] || key;
      
      if (typeof value === 'object' && !Array.isArray(value)) {
        const sectionLabel = sectionLabels[key] || label;
        const innerHtml = renderObject(value, depth + 1);
        if (innerHtml) {
          html += `<div style="margin-top:${depth === 0 ? '16px' : '8px'};${depth === 0 ? 'border-left:3px solid #3b82f6;padding-left:12px;' : ''}">`;
          html += `<div style="font-weight:600;color:#1e40af;margin-bottom:8px;">${sectionLabel}</div>`;
          html += innerHtml;
          html += '</div>';
        }
      } else if (Array.isArray(value) && value.length > 0) {
        html += `<div style="margin:4px 0;"><span style="color:#6b7280;">${label}：</span>${value.join(', ')}</div>`;
      } else if (value) {
        html += `<div style="margin:4px 0;"><span style="color:#6b7280;">${label}：</span><span style="color:#1f2937;">${value}</span></div>`;
      }
    }
    return html;
  }
  
  return renderObject(extract);
}

function getBatchSelectedCandidate(fileIndex) {
  const selectedInput = document.querySelector(`input[name="batch-category-candidate-${fileIndex}"]:checked`);
  if (!selectedInput) return null;
  const candidateIndex = Number(selectedInput.value);
  const candidates = batchCandidateMap[fileIndex] || [];
  const candidate = candidates[candidateIndex] || null;
  if (!candidate) return null;
  return { candidate, index: candidateIndex };
}

function renderBatchSubcategoryOptions(container, categoryName, suggested, fileIndex) {
  const options = subCategoryOptionsMap[categoryName] || [];
  if (!container) return;
  if (!options.length) {
    container.innerHTML = '';
    container.style.display = 'none';
    return;
  }
  const hasSuggested = suggested && options.includes(suggested);
  const preselected = hasSuggested ? suggested : options[0];
  const hintHtml = hasSuggested
    ? `<div style="margin-bottom:6px; color:#0f766e; font-size:13px;">模型建议：${suggested}（请确认）</div>`
    : '';
  const itemsHtml = options.map((optionName) => {
    const checked = optionName === preselected ? 'checked' : '';
    const desc = subCategoryDescriptions[optionName] || '';
    const descHtml = desc ? `<span style="color:#6b7280; font-size:13px;">${desc}</span>` : '';
    return `<label class="subcategory-item"><input type="radio" name="batch-category-subcategory-${fileIndex}" value="${optionName}" ${checked} />` +
      `<strong>${optionName}</strong>${descHtml}</label>`;
  }).join('');
  container.innerHTML = `<div style="font-weight:600; margin-bottom:6px;">请选择子分类</div>${hintHtml}${itemsHtml}`;
  container.style.display = 'block';
}

function updateBatchSubcategoryOptions(fileIndex) {
  const selectedInfo = getBatchSelectedCandidate(fileIndex);
  const candidateBox = document.getElementById(`batch-candidate-box-${fileIndex}`);
  if (!candidateBox) return;
  const containers = candidateBox.querySelectorAll('.candidate-suboptions');
  containers.forEach((item) => {
    item.innerHTML = '';
    item.style.display = 'none';
  });
  if (!selectedInfo) {
    return;
  }
  const { candidate, index: selectedIndex } = selectedInfo;
  const options = subCategoryOptionsMap[candidate.category] || [];
  if (!options.length) {
    return;
  }
  const target = candidateBox.querySelector(
    `.candidate-block[data-index="${selectedIndex}"] .candidate-suboptions`
  );
  if (target) {
    renderBatchSubcategoryOptions(target, candidate.category, candidate.sub_category, fileIndex);
  }
}

function renderBatchCandidateSelector(file, parsed, index) {
  const candidates = file.category_candidates || (parsed ? parsed.category_candidates : null);
  if (!Array.isArray(candidates) || candidates.length === 0) {
    return '';
  }
  batchCandidateMap[index] = candidates;
  const candidateItems = candidates.map((candidate, candidateIndex) => {
    if (!candidate || !candidate.category) return '';
    const categoryName = String(candidate.category).trim();
    const subCategory = candidate.sub_category ? String(candidate.sub_category).trim() : '';
    const confidence = normalizeConfidence(candidate.confidence);
    const label = subCategory ? `${categoryName} / ${subCategory}` : categoryName;
    const confidenceText = confidence === null ? '--' : `${(confidence * 100).toFixed(1)}%`;
    const checked = candidateIndex === 0 ? 'checked' : '';
    return (
      `<div class="candidate-block" data-index="${candidateIndex}">` +
      `<label class="candidate-item">` +
      `<input type="radio" name="batch-category-candidate-${index}" value="${candidateIndex}" ${checked} onchange="updateBatchSubcategoryOptions(${index})" />` +
      `<strong>${label}</strong><span class="candidate-confidence">${confidenceText}</span>` +
      `</label>` +
      `<div class="candidate-suboptions" data-index="${candidateIndex}"></div>` +
      `</div>`
    );
  }).filter(Boolean).join('');
  return `
    <div id="batch-candidate-box-${index}" style="margin-top:12px; padding:12px; border:1px solid #fde68a; border-radius:12px; background:#fffbeb;">
      <div style="font-weight:600; color:#92400e; margin-bottom:8px;">模型给出多分类，请人工确认</div>
      <div>${candidateItems}</div>
      <button type="button" class="btn" style="margin-top:12px;" onclick="confirmBatchCategory(${index})">✅ 确认分类</button>
      <div id="batch-manual-message-${index}" style="margin-top:8px; display:none;"></div>
    </div>
  `;
}

function confirmBatchCategory(fileIndex) {
  if (!currentTask) return;
  const selectedInfo = getBatchSelectedCandidate(fileIndex);
  if (!selectedInfo) {
    alert('请选择一个分类');
    return;
  }
  const selected = selectedInfo.candidate;
  const options = subCategoryOptionsMap[selected.category] || [];
  let finalSubcategory = selected.sub_category || '';
  let finalCategory = selected.category;
  if (options.length > 0) {
    const subInput = document.querySelector(`input[name="batch-category-subcategory-${fileIndex}"]:checked`);
    if (subInput) {
      finalSubcategory = subInput.value;
    } else if (selected.sub_category && options.includes(selected.sub_category)) {
      finalSubcategory = selected.sub_category;
    } else {
      alert('请选择子分类');
      return;
    }
  }
  if (options.length > 0 && finalSubcategory && !finalCategory.includes('/')) {
    finalCategory = `${finalCategory}/${finalSubcategory}`;
  }
  const messageEl = document.getElementById(`batch-manual-message-${fileIndex}`);
  if (messageEl) {
    messageEl.style.display = 'block';
    messageEl.style.background = '#ecfdf5';
    messageEl.style.color = '#065f46';
    messageEl.style.padding = '8px 12px';
    messageEl.style.borderRadius = '8px';
    messageEl.textContent = '正在提交分类...';
  }

  fetch(`${API_BASE}/select_category_batch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_id: currentTask,
      file_index: fileIndex,
      category: finalCategory,
      sub_category: finalSubcategory,
    }),
  })
    .then(resp => resp.json())
    .then(data => {
      if (data.error) {
        throw new Error(data.error);
      }
      const display = document.getElementById(`batch-category-display-${fileIndex}`);
      if (display) {
        display.innerHTML = renderCategoryDisplay(selected.category, finalSubcategory);
      }
      const candidateBox = document.getElementById(`batch-candidate-box-${fileIndex}`);
      if (candidateBox) {
        candidateBox.style.display = 'none';
      }
      if (messageEl) {
        messageEl.textContent = data.message || '已完成分类';
      }
    })
    .catch(err => {
      if (messageEl) {
        messageEl.style.background = '#fef2f2';
        messageEl.style.color = '#991b1b';
        messageEl.textContent = '分类确认失败：' + err.message;
      }
    });
}

function renderBatchResult(batchResult) {
  if (!batchResult) return;
  batchCandidateMap = {};
  batchSection.style.display = 'block';
  outlineSection.style.display = 'none';
  summarySection.style.display = 'none';
  categorySection.style.display = 'none';
  categoryCandidates.innerHTML = '';
  categoryCandidates.style.display = 'none';
  subcategoryOptions.innerHTML = '';
  subcategoryOptions.style.display = 'none';
  categoryConfirmBtn.style.display = 'none';
  categoryManualMessage.style.display = 'none';

  const summary = batchResult.batch_summary || {};
  const total = summary.total !== undefined ? summary.total : 0;
  const successful = summary.successful !== undefined ? summary.successful : 0;
  const failed = summary.failed !== undefined ? summary.failed : 0;

  batchSummary.innerHTML = `
    <div style="background:#f0f9ff; padding:16px; border-radius:12px; border:1px solid #e0f2fe;">
      <div style="font-weight:600; color:#0369a1; margin-bottom:8px;">批量处理摘要</div>
      <div>总计: ${total} | 成功: ${successful} | 失败: ${failed}</div>
    </div>
  `;

  const files = Array.isArray(batchResult.files) ? batchResult.files : [];
  if (files.length === 0) {
    batchDetails.innerHTML = '<div style="margin-top:12px; color:#6b7280;">未返回文件详情。</div>';
    resultJson.textContent = JSON.stringify(batchResult, null, 2);
    return;
  }

  let filesHtml = '<ul style="list-style:none; padding:0; margin-top:16px;">';
  files.forEach((file, index) => {
    const statusIcon = file.success ? '✅' : '❌';
    const fileId = `batch-file-${index}`;
    const displayName = file.file || file.name || `文件${index + 1}`;
    const threadName = file.thread_name ? ` | ${file.thread_name}` : '';
    const errorText = file.error ? `<div style="color:#dc2626; font-size:0.9em; margin-top:6px;">错误: ${file.error}</div>` : '';
    const categoryError = file.category_error ? `<div style="color:#b45309; font-size:0.9em; margin-top:6px;">分类提示: ${file.category_error}</div>` : '';
    const manualHint = file.manual_selection_required ? `<div style="color:#0f766e; font-size:0.9em; margin-top:6px;">需人工确认分类</div>` : '';
    filesHtml += `
      <li style="margin-bottom:16px; border:1px solid #e5e7eb; border-radius:12px; overflow:hidden;">
        <div style="padding:12px; background:#f9fafb; cursor:pointer;" onclick="toggleFileDetail('${fileId}')">
          <strong>${statusIcon} ${displayName}</strong>
          <span style="float:right; font-size:0.9em; color:#6b7280;">${threadName}</span>
          ${errorText}
          ${categoryError}
          ${manualHint}
        </div>
        <div id="${fileId}" style="display:none; padding:12px; background:#ffffff; border-top:1px solid #e5e7eb;">
          ${file.success ? renderFileDetail(file, index) : ''}
        </div>
      </li>`;
  });
  filesHtml += '</ul>';
  batchDetails.innerHTML = filesHtml;
  files.forEach((file, index) => {
    if (batchCandidateMap[index]) {
      updateBatchSubcategoryOptions(index);
    }
  });
  resultJson.textContent = JSON.stringify(batchResult, null, 2);
}

function renderFileDetail(file, index) {
  try {
    const parsed = typeof file.result === 'string' ? JSON.parse(file.result) : file.result;
    let html = '';

    if (file.move_message) {
      html += `<div style="margin:8px 0; color:#047857;">${file.move_message}</div>`;
    }

    if (file.category_error) {
      html += `<div style="margin:8px 0; color:#b45309;">${file.category_error}</div>`;
    }

    const categoryDisplayId = `batch-category-display-${index}`;
    if (parsed && parsed.category) {
      const subCategory = parsed.sub_category ? ` › ${parsed.sub_category}` : '';
      html += `<div id="${categoryDisplayId}" style="margin:8px 0;"><strong>🏷️ 分类:</strong> ${parsed.category}${subCategory}</div>`;
    } else {
      html += `<div id="${categoryDisplayId}" style="margin:8px 0;"></div>`;
    }

    // 批量处理：显示保密类别
    if (parsed && parsed.security_level) {
      const securityLevel = String(parsed.security_level).trim();
      const securityStyles = {
        '公开': { color: '#059669', bg: '#d1fae5', icon: '🟢' },
        '非公开：1级': { color: '#d97706', bg: '#fef3c7', icon: '🟡' },
        '非公开：2级': { color: '#ea580c', bg: '#ffedd5', icon: '🟠' },
        '非公开：3级': { color: '#dc2626', bg: '#fee2e2', icon: '🔴' }
      };
      const style = securityStyles[securityLevel] || securityStyles['公开'];
      html += `<div style="margin:8px 0;"><strong>🔒 保密类别:</strong> <span style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;background:${style.bg};color:${style.color};font-weight:600;font-size:0.9em;">${style.icon} ${securityLevel}</span></div>`;
    }

    const candidates = file.category_candidates || (parsed ? parsed.category_candidates : null);
    if (Array.isArray(candidates) && candidates.length > 0) {
      const candidateLines = candidates.map((candidate) => {
        if (!candidate || !candidate.category) return '';
        const subCategory = candidate.sub_category ? ` / ${candidate.sub_category}` : '';
        const confidence = normalizeConfidence(candidate.confidence);
        const confidenceText = confidence === null ? '' : ` (${(confidence * 100).toFixed(1)}%)`;
        return `<div>${candidate.category}${subCategory}${confidenceText}</div>`;
      }).filter(Boolean).join('');
      if (candidateLines) {
        html += `<div style="margin:8px 0; padding:8px; background:#ecfeff; border-radius:8px;"><strong>候选分类:</strong>${candidateLines}</div>`;
      }
    }

    if (parsed && parsed.outline && Array.isArray(parsed.outline) && parsed.outline.length > 0) {
      html += `<div style="margin:8px 0;"><strong>📚 目录:</strong><ul style="padding-left:20px; margin:4px 0;">`;
      parsed.outline.slice(0, 10).forEach((item, i) => {
        html += `<li style="margin:4px 0;">${i + 1}. ${item}</li>`;
      });
      if (parsed.outline.length > 10) {
        html += `<li style="color:#6b7280;">... 共 ${parsed.outline.length} 项</li>`;
      }
      html += `</ul></div>`;
    }

    if (parsed && parsed.extract && typeof parsed.extract === 'object') {
      const extractHtml = formatExtractData(parsed.extract);
      if (extractHtml) {
        html += `<div style="margin:8px 0; padding:8px; background:#f0f9ff; border-radius:6px;"><strong>📝 信息抽取:</strong><br>${extractHtml}</div>`;
      }
    }

    if (parsed && parsed.summary && typeof parsed.summary === 'string' && parsed.summary.trim()) {
      html += `<div style="margin:8px 0; padding:8px; background:#f0f9ff; border-radius:6px;"><strong>📌 摘要:</strong><br>${parsed.summary.trim()}</div>`;
    }

    const candidateSelector = renderBatchCandidateSelector(file, parsed, index);
    if (candidateSelector) {
      html += candidateSelector;
    }

    return html;
  } catch (e) {
    return `<div style="color:#dc2626; font-size:0.9em;">解析失败: ${e.message}</div>`;
  }
}

function toggleFileDetail(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function resetUI() {
  errorBox.textContent = '';
  resultBox.style.display = 'none';
  resultJson.textContent = '';
  progressBox.style.display = 'none';
  progressText.textContent = '';
  batchSection.style.display = 'none';
  batchSummary.innerHTML = '';
  batchDetails.innerHTML = '';
  outlineSection.style.display = 'none';
  outlineList.innerHTML = '';
  summarySection.style.display = 'none';
  summaryText.textContent = '';
  securitySection.style.display = 'none';
  securityText.innerHTML = '';
  categorySection.style.display = 'none';
  categoryText.textContent = '';
  categoryCandidates.innerHTML = '';
  categoryCandidates.style.display = 'none';
  subcategoryOptions.innerHTML = '';
  subcategoryOptions.style.display = 'none';
  categoryConfirmBtn.style.display = 'none';
  categoryManualMessage.style.display = 'none';
  categoryManualMessage.textContent = '';
  currentCategoryCandidates = [];
  currentSubcategoryOptions = [];
  batchCandidateMap = {};
}

function showFileInfo(file) {
  fileInfo.textContent = `已选择: ${file.name} (${(file.size/1024/1024).toFixed(2)} MB)`;
  fileInfo.style.display = 'block';
  startBtn.disabled = false;
  selectedFiles = null;
  if (folderInput) {
    folderInput.value = '';
  }
        }

async function getDroppedFilesFromItems(items) {
  const entries = [];
  const fallbackFiles = [];
  let hasDirectory = false;

  for (const item of items) {
    if (item.kind !== 'file') continue;
    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    if (entry) {
      entries.push(entry);
      if (entry.isDirectory) {
        hasDirectory = true;
      }
    } else {
      const file = item.getAsFile();
      if (file) {
        fallbackFiles.push(file);
      }
    }
  }

  if (entries.length === 0) {
    return { files: fallbackFiles, hasDirectory: false };
  }

  const files = [];
  const traverseEntry = async (entry) => {
    if (entry.isFile) {
      const file = await new Promise((resolve) => entry.file(resolve));
      files.push(file);
      return;
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      const readBatch = () => new Promise((resolve) => reader.readEntries(resolve));
      let batch = await readBatch();
      while (batch.length > 0) {
        for (const child of batch) {
          if (child.isDirectory) {
            hasDirectory = true;
          }
          await traverseEntry(child);
        }
        batch = await readBatch();
      }
    }
  };

  for (const entry of entries) {
    await traverseEntry(entry);
  }

  return { files, hasDirectory };
}

['dragenter','dragover','dragleave','drop'].forEach(evt => {
  document.addEventListener(evt, e => {
        e.preventDefault();
    e.stopPropagation();
  });
});

uploader.addEventListener('dragover', () => uploader.classList.add('drag'));
uploader.addEventListener('dragleave', () => uploader.classList.remove('drag'));
uploader.addEventListener('drop', async evt => {
  uploader.classList.remove('drag');
  const items = evt.dataTransfer.items;
  if (items && items.length > 0 && items[0].webkitGetAsEntry) {
    const dropped = await getDroppedFilesFromItems(items);
    const files = dropped.files || [];
    if (files.length > 1 || dropped.hasDirectory) {
      handleFolderUpload(files);
      resetUI();
      return;
    }
    if (files.length === 1) {
      selectedFile = files[0];
      showFileInfo(selectedFile);
      resetUI();
    }
    return;
  }

  const files = evt.dataTransfer.files;
  if (files && files.length > 0) {
    if (files.length > 1) {
      handleFolderUpload(Array.from(files));
    } else {
      selectedFile = files[0];
      showFileInfo(selectedFile);
    }
    resetUI();
  }
});

fileInput.addEventListener('change', evt => {
  if (evt.target.files && evt.target.files.length > 0) {
    selectedFile = evt.target.files[0];
    showFileInfo(selectedFile);
    resetUI();
        }
    });

folderInput.addEventListener('change', evt => {
  if (evt.target.files && evt.target.files.length > 0) {
    const files = Array.from(evt.target.files);
    handleFolderUpload(files);
    resetUI();
  }
});

function handleFolderUpload(files) {
  fileInfo.textContent = `已选择文件夹: 包含 ${files.length} 个文件`;
  fileInfo.style.display = 'block';
  startBtn.disabled = false;
  selectedFiles = files;
  selectedFile = null;
  if (fileInput) {
    fileInput.value = '';
  }
}

startBtn.addEventListener('click', () => {
  if (!selectedFile && (!selectedFiles || selectedFiles.length === 0)) {
    alert('请先选择文件或文件夹');
    return;
  }

  startBtn.disabled = true;
  errorBox.textContent = '';
  progressBox.style.display = 'block';

  if (selectedFiles && selectedFiles.length > 0) {
    progressText.textContent = '准备上传文件夹...';
    uploadFolder(selectedFiles);
    return;
  }

  progressText.textContent = '上传文件中...';
        
  const formData = new FormData();
  formData.append('file', selectedFile);

  fetch(`${API_BASE}/upload`, { method: 'POST', body: formData })
    .then(resp => resp.json())
    .then(data => {
      if (data.error) {
        throw new Error(data.error);
      }
      currentTask = data.task_id;
      pollStatus();
    })
    .catch(err => {
      startBtn.disabled = false;
      progressBox.style.display = 'none';
      errorBox.textContent = '上传失败：' + err.message;
    });
});

function uploadFolder(files) {
  const formData = new FormData();
  files.forEach((file) => {
    formData.append('files', file);
  });
  formData.append('workspace_prefix', 'folder_workspace');
  formData.append('thread_name', document.getElementById('thread')?.value || '批量处理线程');

  fetch(`${API_BASE}/upload_folder`, { method: 'POST', body: formData })
    .then(resp => resp.json())
    .then(data => {
      if (data.error) {
        throw new Error(data.error);
      }
      currentTask = data.task_id;
      pollStatus();
    })
    .catch(err => {
      startBtn.disabled = false;
      progressBox.style.display = 'none';
      errorBox.textContent = '文件夹上传失败：' + err.message;
    });
}

categoryCandidates.addEventListener('change', (event) => {
  if (event.target && event.target.name === 'category-candidate') {
    updateSubcategoryOptions();
  }
});

categoryConfirmBtn.addEventListener('click', () => {
  if (!currentTask || currentCategoryCandidates.length === 0) return;
  const selectedInfo = getSelectedCandidate();
  if (!selectedInfo) {
    alert('请选择一个分类');
    return;
  }
  const selected = selectedInfo.candidate;
  const options = subCategoryOptionsMap[selected.category] || [];
  let finalSubcategory = selected.sub_category || '';
  let finalCategory = selected.category;
  if (options.length > 0) {
    const subInput = document.querySelector('input[name="category-subcategory"]:checked');
    if (subInput) {
      finalSubcategory = subInput.value;
    } else if (selected.sub_category && options.includes(selected.sub_category)) {
      finalSubcategory = selected.sub_category;
    } else {
      alert('请选择子分类');
      return;
    }
  }
  if (options.length > 0 && finalSubcategory && !finalCategory.includes('/')) {
    finalCategory = `${finalCategory}/${finalSubcategory}`;
  }
  categoryConfirmBtn.disabled = true;
  categoryManualMessage.style.display = 'none';
  categoryManualMessage.textContent = '';

  fetch(`${API_BASE}/select_category`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_id: currentTask,
      category: finalCategory,
      sub_category: finalSubcategory,
    })
  })
    .then(resp => resp.json())
    .then(data => {
      if (data.error) {
        throw new Error(data.error);
      }
      categoryText.innerHTML = renderCategoryDisplay(selected.category, finalSubcategory);
      categoryCandidates.style.display = 'none';
      subcategoryOptions.style.display = 'none';
      categoryConfirmBtn.style.display = 'none';
      categoryManualMessage.textContent = data.message || '已完成分类';
      categoryManualMessage.style.display = 'block';
    })
    .catch(err => {
      categoryManualMessage.textContent = '分类确认失败：' + err.message;
      categoryManualMessage.style.display = 'block';
    })
    .finally(() => {
      categoryConfirmBtn.disabled = false;
    });
});

function pollStatus() {
  if (!currentTask) return;
  fetch(`${API_BASE}/status/${currentTask}`)
    .then(resp => resp.json())
    .then(data => {
      if (data.status === 'processing') {
        if (data.total_files && data.processed !== undefined) {
          const percent = Math.round((data.processed / data.total_files) * 100);
          progressText.textContent = `${data.message || '处理中...'} (${percent}%)`;
        } else {
          progressText.textContent = data.message || '处理中...';
        }
        setTimeout(pollStatus, 1500);
      } else if (data.status === 'completed') {
        progressBox.style.display = 'none';
        startBtn.disabled = false;
        resultBox.style.display = 'block';
        const isBatch = data.result && data.result.batch_summary;
        if (isBatch) {
          renderBatchResult(data.result);
          return;
        }
        const rawString = typeof data.result === 'string'
          ? data.result
          : JSON.stringify(data.result, null, 2);
        resultJson.textContent = rawString || '';

        try {
          const parsed = typeof data.result === 'string'
            ? JSON.parse(data.result)
            : data.result;

          if (parsed && Array.isArray(parsed.outline) && parsed.outline.length > 0) {
            outlineList.innerHTML = parsed.outline
              .map((item, index) => `<li><span style="font-weight:600;margin-right:8px;">${index + 1}.</span>${item}</li>`)
              .join('');
            outlineSection.style.display = 'block';
            }
            
          if (parsed && typeof parsed.summary === 'string' && parsed.summary.trim() !== '') {
            summaryText.textContent = parsed.summary.trim();
            summarySection.style.display = 'block';
            }
          
          // 处理保密类别 (security_level)
          if (parsed && parsed.security_level) {
            const securityLevel = String(parsed.security_level).trim();
            const securityStyles = {
              '公开': { color: '#059669', bg: '#d1fae5', icon: '🟢' },
              '非公开：1级': { color: '#d97706', bg: '#fef3c7', icon: '🟡' },
              '非公开：2级': { color: '#ea580c', bg: '#ffedd5', icon: '🟠' },
              '非公开：3级': { color: '#dc2626', bg: '#fee2e2', icon: '🔴' }
            };
            const style = securityStyles[securityLevel] || securityStyles['公开'];
            securityText.innerHTML = `<span style="display:inline-flex;align-items:center;gap:8px;padding:8px 16px;border-radius:8px;background:${style.bg};color:${style.color};font-weight:600;">${style.icon} ${securityLevel}</span>`;
            securitySection.style.display = 'block';
          }
          
          // 处理信息抽取结果 (extract)
          if (parsed && parsed.extract && typeof parsed.extract === 'object') {
            const extractHtml = formatExtractData(parsed.extract);
            if (extractHtml) {
              summaryText.innerHTML = extractHtml;
              summarySection.style.display = 'block';
            }
          }
          
          // 处理军事分类（支持多候选）
          const candidates = parsed && parsed.category_candidates;
          const hasCandidates = Array.isArray(candidates) && candidates.length > 0;
          if (hasCandidates) {
            categoryText.textContent = '模型无法完全确定分类，请选择可能的类别：';
            renderCategoryCandidates(candidates);
            categorySection.style.display = 'block';
          } else {
            let categoryValue = null;
            if (parsed && parsed.category) {
              if (typeof parsed.category === 'string') {
                categoryValue = parsed.category.trim();
              } else if (Array.isArray(parsed.category) && parsed.category.length > 0) {
                // 如果错误地返回了数组，只取第一个
                categoryValue = String(parsed.category[0]).trim();
              }
            }
            
            if (categoryValue && categoryValue !== '') {
              const categoryName = categoryValue;
              const subCategory = parsed.sub_category ? parsed.sub_category.trim() : '';
              categoryText.innerHTML = renderCategoryDisplay(categoryName, subCategory);
              categorySection.style.display = 'block';
            }
          }
        } catch (err) {
          console.warn('解析 JSON 失败，显示原始内容', err);
        }
      } else if (data.status === 'error') {
        progressBox.style.display = 'none';
        startBtn.disabled = false;
        errorBox.textContent = data.error || '处理失败';
    }
    })
    .catch(err => {
      progressBox.style.display = 'none';
      startBtn.disabled = false;
      errorBox.textContent = '状态查询失败：' + err.message;
            });
    }

