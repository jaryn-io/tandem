/**
 * ForgeDesk - Vanilla JavaScript client helpers.
 * 100% offline, zero external dependencies.
 */

document.addEventListener('DOMContentLoaded', () => {
    // Auto-dismiss info and success flash alerts after 6 seconds
    const autoAlerts = document.querySelectorAll('.alert-success, .alert-info');
    autoAlerts.forEach(alert => {
        setTimeout(() => {
            alert.style.transition = 'opacity 0.5s ease';
            alert.style.opacity = '0';
            setTimeout(() => alert.remove(), 500);
        }, 6000);
    });
});

/**
 * Open a modal by ID.
 * @param {string} modalId 
 */
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('show');
        document.body.style.overflow = 'hidden';
    }
}

/**
 * Close a modal by ID.
 * @param {string} modalId 
 */
function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('show');
        document.body.style.overflow = '';
    }
}

/**
 * Prompt user before performing a critical destructive action.
 * @param {string} message 
 * @returns {boolean}
 */
function confirmAction(message) {
    return window.confirm(message || 'Are you sure you want to perform this action?');
}

/**
 * Live search filtering for HTML tables.
 * @param {string} inputId 
 * @param {string} tableId 
 */
function filterTable(inputId, tableId) {
    const input = document.getElementById(inputId);
    const table = document.getElementById(tableId);
    if (!input || !table) return;

    const query = input.value.toLowerCase().trim();
    const rows = table.querySelectorAll('tbody tr');

    rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(query) ? '' : 'none';
    });
}

/**
 * Get active CSRF token from document meta tag.
 * @returns {string}
 */
function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute('content') || '') : '';
}

/**
 * Authenticated, CSRF-protected fetch helper for front-end actions.
 * @param {string} url 
 * @param {object} options 
 * @returns {Promise<Response>}
 */
async function forgeFetch(url, options = {}) {
    const opts = { ...options };
    opts.headers = { ...(opts.headers || {}) };
    const csrf = getCsrfToken();
    if (csrf && !opts.headers['X-CSRF-Token']) {
        opts.headers['X-CSRF-Token'] = csrf;
    }
    return fetch(url, opts);
}

