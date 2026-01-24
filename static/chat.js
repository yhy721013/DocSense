// 对话模块前端（占位）。
// 开发建议：
// 1) 上传文件/文件夹 -> POST /api/chat/upload
// 2) 发送消息 -> POST /api/chat/message
// 3) 维护会话ID / thread_slug / workspace_slug 等必要状态

console.info('[chat] 对话功能尚未实现：请在 static/chat.js 补全前端交互。');


// 对话模块前端 - 文件列表显示功能

// DOM 元素引用
const selectFilesBtn = document.getElementById('select-files-btn');
const reselectFilesBtn = document.getElementById('reselect-files-btn');
const selectedFilesSection = document.getElementById('selected-files-section');
const selectedFilesList = document.getElementById('selected-files-list');

// 模态框相关
const fileSelectionModal = document.getElementById('file-selection-modal');
const closeModalBtn = document.getElementById('close-modal-btn');
const loadFilesModalBtn = document.getElementById('load-files-modal-btn');
const modalLoadingIndicator = document.getElementById('modal-loading-indicator');
const modalFileListContainer = document.getElementById('modal-file-list-container');
const modalFileList = document.getElementById('modal-file-list');
const modalFileStats = document.getElementById('modal-file-stats');
const modalNoFilesMessage = document.getElementById('modal-no-files-message');
const modalErrorMessage = document.getElementById('modal-error-message');
const selectionCount = document.getElementById('selection-count');
const cancelSelectionBtn = document.getElementById('cancel-selection-btn');
const confirmSelectionBtn = document.getElementById('confirm-selection-btn');

// API 基础路径
const API_BASE = '/api/chat';

// 状态管理
let availableFiles = []; // 所有可用文件
let selectedFiles = [];  // 已选中的文件

// 初始化
document.addEventListener('DOMContentLoaded', function() {
    console.log('[chat] 文件选择功能已加载');
    
    // 绑定事件
    selectFilesBtn.addEventListener('click', openFileSelectionModal);
    reselectFilesBtn.addEventListener('click', openFileSelectionModal);
    closeModalBtn.addEventListener('click', closeFileSelectionModal);
    loadFilesModalBtn.addEventListener('click', loadFilesForModal);
    cancelSelectionBtn.addEventListener('click', closeFileSelectionModal);
    confirmSelectionBtn.addEventListener('click', confirmFileSelection);
    
    // 点击模态框背景关闭
    fileSelectionModal.addEventListener('click', function(e) {
        if (e.target === fileSelectionModal) {
            closeFileSelectionModal();
        }
    });
    
    // ESC键关闭模态框
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && fileSelectionModal.style.display !== 'none') {
            closeFileSelectionModal();
        }
    });
});

/**
 * 根据文件扩展名获取图标
 * @param {string} filename - 文件名
 * @returns {string} 文件图标
 */
function getFileIcon(filename) {
    const ext = filename.toLowerCase().split('.').pop();
    
    const iconMap = {
        'pdf': '📄',
        'doc': '📝',
        'docx': '📝',
        'xls': '📊',
        'xlsx': '📊',
        'ppt': '📽️',
        'pptx': '📽️',
        'txt': '📄',
        'jpg': '🖼️',
        'jpeg': '🖼️',
        'png': '🖼️',
        'gif': '🖼️',
        'bmp': '🖼️'
    };
    
    return iconMap[ext] || '📄';
}

/**
 * 格式化文件大小
 * @param {number} bytes - 文件大小（字节）
 * @returns {string} 格式化的文件大小
 */
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

/**
 * HTML 转义
 * @param {string} text - 需要转义的文本
 * @returns {string} 转义后的文本
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * 打开文件选择模态框
 */
function openFileSelectionModal() {
    fileSelectionModal.style.display = 'flex';
    document.body.style.overflow = 'hidden'; // 防止背景滚动
    
    // 如果还没有加载过文件，则自动加载
    if (availableFiles.length === 0) {
        loadFilesForModal();
    } else {
        // 重新渲染文件列表
        renderModalFileList(availableFiles);
        updateSelectionCount();
    }
}

/**
 * 关闭文件选择模态框
 */
function closeFileSelectionModal() {
    fileSelectionModal.style.display = 'none';
    document.body.style.overflow = ''; // 恢复背景滚动
}

/**
 * 为模态框加载文件列表
 */
async function loadFilesForModal() {
    try {
        // 显示加载状态
        showModalLoading(true);
        hideModalError();
        hideModalFileList();
        
        // 调用 API
        const response = await fetch(`${API_BASE}/files`);
        const data = await response.json();
        
        if (!response.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }
        
        // 保存文件数据
        availableFiles = data.files || [];
        
        // 渲染模态框文件列表
        renderModalFileList(availableFiles);
        
    } catch (error) {
        console.error('[chat] 加载文件列表失败:', error);
        showModalError(`加载文件列表失败：${error.message}`);
    } finally {
        showModalLoading(false);
    }
}

/**
 * 渲染模态框文件列表（带复选框）
 * @param {Array} files - 文件列表
 */
function renderModalFileList(files) {
    // 更新统计信息
    updateModalFileStats(files.length);
    
    if (files.length === 0) {
        showModalNoFilesMessage();
        return;
    }
    
    // 清空现有内容
    modalFileList.innerHTML = '';
    
    // 渲染每个文件
    files.forEach((file, index) => {
        const fileItem = createModalFileItem(file, index);
        modalFileList.appendChild(fileItem);
    });
    
    // 显示文件列表
    showModalFileList();
    updateSelectionCount();
}

/**
 * 创建模态框文件项（带复选框）
 * @param {Object} file - 文件信息
 * @param {number} index - 文件索引
 * @returns {HTMLElement} 文件项DOM元素
 */
function createModalFileItem(file, index) {
    const item = document.createElement('div');
    item.className = 'file-item';
    
    // 检查是否已选中
    const isSelected = selectedFiles.some(selected => selected.path === file.path);
    
    // 获取文件图标
    const fileIcon = getFileIcon(file.name);
    
    // 格式化文件大小
    const formattedSize = formatFileSize(file.size);
    
    item.innerHTML = `
        <label class="file-item-label">
            <input type="checkbox" class="file-checkbox" data-index="${index}" ${isSelected ? 'checked' : ''}>
            <div class="file-icon">${fileIcon}</div>
            <div class="file-info">
                <div class="file-name">${escapeHtml(file.name)}</div>
                <div class="file-meta">
                    <div class="file-path">📍 ${escapeHtml(file.path)}</div>
                    <span class="file-size">📏 ${formattedSize}</span>
                    <span class="file-modified">🕒 ${file.modified}</span>
                </div>
            </div>
        </label>
    `;
    
    // 绑定复选框事件
    const checkbox = item.querySelector('.file-checkbox');
    checkbox.addEventListener('change', function() {
        handleFileSelection(file, this.checked);
    });
    
    return item;
}

/**
 * 处理文件选择/取消选择
 * @param {Object} file - 文件信息
 * @param {boolean} isSelected - 是否选中
 */
function handleFileSelection(file, isSelected) {
    if (isSelected) {
        // 添加到已选文件
        if (!selectedFiles.some(selected => selected.path === file.path)) {
            selectedFiles.push(file);
        }
    } else {
        // 从已选文件中移除
        selectedFiles = selectedFiles.filter(selected => selected.path !== file.path);
    }
    
    updateSelectionCount();
}

/**
 * 确认文件选择
 */
function confirmFileSelection() {
    if (selectedFiles.length === 0) {
        alert('请至少选择一个文件');
        return;
    }
    
    // 关闭模态框
    closeFileSelectionModal();
    
    // 显示已选文件区域
    renderSelectedFiles();
    selectedFilesSection.style.display = 'block';
    
    // 隐藏文件选择头部
    document.querySelector('.file-selection-header').style.display = 'none';
}

/**
 * 渲染已选文件列表
 */
function renderSelectedFiles() {
    selectedFilesList.innerHTML = '';
    
    selectedFiles.forEach((file, index) => {
        const fileItem = createSelectedFileItem(file, index);
        selectedFilesList.appendChild(fileItem);
    });
}

/**
 * 创建已选文件项
 * @param {Object} file - 文件信息
 * @param {number} index - 文件索引
 * @returns {HTMLElement} 文件项DOM元素
 */
function createSelectedFileItem(file, index) {
    const item = document.createElement('div');
    item.className = 'selected-file-item';
    
    // 获取文件图标
    const fileIcon = getFileIcon(file.name);
    
    // 格式化文件大小
    const formattedSize = formatFileSize(file.size);
    
    item.innerHTML = `
        <div class="selected-file-icon">${fileIcon}</div>
        <div class="selected-file-info">
            <div class="selected-file-name">${escapeHtml(file.name)}</div>
            <div class="selected-file-meta">
                <span class="file-size">📏 ${formattedSize}</span>
                <span class="file-modified">🕒 ${file.modified}</span>
            </div>
        </div>
        <button class="remove-file-btn" data-index="${index}" title="移除文件">×</button>
    `;
    
    // 绑定移除按钮事件
    const removeBtn = item.querySelector('.remove-file-btn');
    removeBtn.addEventListener('click', function() {
        removeSelectedFile(index);
    });
    
    return item;
}

/**
 * 移除已选文件
 * @param {number} index - 文件索引
 */
function removeSelectedFile(index) {
    selectedFiles.splice(index, 1);
    renderSelectedFiles();
    
    // 如果没有文件了，隐藏已选文件区域
    if (selectedFiles.length === 0) {
        selectedFilesSection.style.display = 'none';
        document.querySelector('.file-selection-header').style.display = 'block';
    }
}

/**
 * 更新选择计数
 */
function updateSelectionCount() {
    const count = selectedFiles.length;
    selectionCount.textContent = `已选择 ${count} 个文件`;
    confirmSelectionBtn.disabled = count === 0;
}

/**
 * 更新模态框文件统计信息
 * @param {number} count - 文件数量
 */
function updateModalFileStats(count) {
    modalFileStats.textContent = `共 ${count} 个文件`;
}

// 模态框相关的显示/隐藏函数
function showModalLoading(show) {
    modalLoadingIndicator.style.display = show ? 'inline' : 'none';
    loadFilesModalBtn.disabled = show;
}

function showModalFileList() {
    modalFileListContainer.style.display = 'block';
    modalNoFilesMessage.style.display = 'none';
}

function hideModalFileList() {
    modalFileListContainer.style.display = 'none';
}

function showModalNoFilesMessage() {
    modalNoFilesMessage.style.display = 'block';
    modalFileListContainer.style.display = 'block';
}

function showModalError(message) {
    modalErrorMessage.innerHTML = `<strong>错误：</strong>${escapeHtml(message)}`;
    modalErrorMessage.style.display = 'block';
}

function hideModalError() {
    modalErrorMessage.style.display = 'none';
}