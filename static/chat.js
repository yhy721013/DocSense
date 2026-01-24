// 对话模块前端（占位）。
// 开发建议：
// 1) 上传文件/文件夹 -> POST /api/chat/upload
// 2) 发送消息 -> POST /api/chat/message
// 3) 维护会话ID / thread_slug / workspace_slug 等必要状态

console.info('[chat] 对话功能尚未实现：请在 static/chat.js 补全前端交互。');


// 对话模块前端 - 文件列表显示功能

// DOM 元素引用
const loadFilesBtn = document.getElementById('load-files-btn');
const loadingIndicator = document.getElementById('loading-indicator');
const fileListContainer = document.getElementById('file-list-container');
const fileList = document.getElementById('file-list');
const fileStats = document.getElementById('file-stats');
const noFilesMessage = document.getElementById('no-files-message');
const errorMessage = document.getElementById('error-message');

// API 基础路径
const API_BASE = '/api/chat';

// 初始化
document.addEventListener('DOMContentLoaded', function() {
    console.log('[chat] 文件列表显示功能已加载');
    
    // 绑定事件
    loadFilesBtn.addEventListener('click', loadFileList);
    
    // 页面加载时自动加载文件列表
    loadFileList();
});

/**
 * 加载文件列表
 */
async function loadFileList() {
    try {
        // 显示加载状态
        showLoading(true);
        hideError();
        hideFileList();
        
        // 调用 API
        const response = await fetch(`${API_BASE}/files`);
        const data = await response.json();
        
        if (!response.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }
        
        // 处理成功响应
        renderFileList(data.files || []);
        
    } catch (error) {
        console.error('[chat] 加载文件列表失败:', error);
        showError(`加载文件列表失败：${error.message}`);
    } finally {
        showLoading(false);
    }
}

/**
 * 渲染文件列表
 * @param {Array} files - 文件列表
 */
function renderFileList(files) {
    // 更新统计信息
    updateFileStats(files.length);
    
    if (files.length === 0) {
        showNoFilesMessage();
        return;
    }
    
    // 清空现有内容
    fileList.innerHTML = '';
    
    // 渲染每个文件
    files.forEach(file => {
        const fileItem = createFileItem(file);
        fileList.appendChild(fileItem);
    });
    
    // 显示文件列表
    showFileList();
}

/**
 * 创建文件项元素
 * @param {Object} file - 文件信息
 * @returns {HTMLElement} 文件项DOM元素
 */
function createFileItem(file) {
    const item = document.createElement('div');
    item.className = 'file-item';
    
    // 获取文件图标
    const fileIcon = getFileIcon(file.name);
    
    // 格式化文件大小
    const formattedSize = formatFileSize(file.size);
    
    item.innerHTML = `
        <div class="file-icon">${fileIcon}</div>
        <div class="file-info">
            <div class="file-name">${escapeHtml(file.name)}</div>
            <div class="file-meta">
                <div class="file-path">📍 ${escapeHtml(file.path)}</div>
                <span class="file-size">📏 ${formattedSize}</span>
                <span class="file-modified">🕒 ${file.modified}</span>
            </div>
        </div>
    `;
    
    return item;
}

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
 * 更新文件统计信息
 * @param {number} count - 文件数量
 */
function updateFileStats(count) {
    fileStats.textContent = `共 ${count} 个文件`;
}

/**
 * 显示/隐藏加载状态
 * @param {boolean} show - 是否显示
 */
function showLoading(show) {
    loadingIndicator.style.display = show ? 'inline' : 'none';
    loadFilesBtn.disabled = show;
}

/**
 * 显示文件列表
 */
function showFileList() {
    fileListContainer.style.display = 'block';
    noFilesMessage.style.display = 'none';
}

/**
 * 隐藏文件列表
 */
function hideFileList() {
    fileListContainer.style.display = 'none';
}

/**
 * 显示无文件消息
 */
function showNoFilesMessage() {
    noFilesMessage.style.display = 'block';
    fileListContainer.style.display = 'block';
}

/**
 * 显示错误消息
 * @param {string} message - 错误消息
 */
function showError(message) {
    errorMessage.innerHTML = `<strong>错误：</strong>${escapeHtml(message)}`;
    errorMessage.style.display = 'block';
}

/**
 * 隐藏错误消息
 */
function hideError() {
    errorMessage.style.display = 'none';
}