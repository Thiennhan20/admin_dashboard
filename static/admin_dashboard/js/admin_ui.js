/* Admin Panel — shared UI logic: i18n, theme, sidebar, global search, toasts */

/* ── i18n helpers ───────────────────────────────────────────────── */
function mergeTranslations(extra) {
    Object.entries(extra || {}).forEach(([lang, messages]) => {
        window.AdminI18N[lang] = Object.assign(window.AdminI18N[lang] || {}, messages || {});
    });
    if (typeof translatePage === 'function') {
        translatePage();
    }
}

function currentLanguage() {
    return localStorage.getItem('admin-language') || 'vi';
}

function translateValue(key, params = {}) {
    const lang = currentLanguage();
    const hasLangKey = window.AdminI18N[lang] && (key in window.AdminI18N[lang]);
    const hasEnKey = window.AdminI18N.en && (key in window.AdminI18N.en);
    if (!hasLangKey && !hasEnKey) {
        return undefined;
    }
    const fallback = window.AdminI18N.en?.[key] || key;
    let value = window.AdminI18N[lang]?.[key] || fallback;
    Object.entries(params).forEach(([name, paramValue]) => {
        value = value.replaceAll(`{${name}}`, paramValue);
    });
    return value;
}

function translatePage(root = document) {
    const lang = currentLanguage();
    document.documentElement.lang = lang;
    root.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.dataset.i18n;
        if (!el.hasOwnProperty('i18nOriginal')) {
            el.i18nOriginal = el.textContent;
        }
        const translated = translateValue(key);
        el.textContent = (translated !== undefined && translated !== key) ? translated : el.i18nOriginal;
    });
    root.querySelectorAll('[data-i18n-title]').forEach(el => {
        const key = el.dataset.i18nTitle;
        if (!el.hasOwnProperty('i18nOriginalTitle')) {
            el.i18nOriginalTitle = el.title;
        }
        const translated = translateValue(key);
        el.title = (translated !== undefined && translated !== key) ? translated : el.i18nOriginalTitle;
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.dataset.i18nPlaceholder;
        if (!el.hasOwnProperty('i18nOriginalPlaceholder')) {
            el.i18nOriginalPlaceholder = el.placeholder;
        }
        const translated = translateValue(key);
        el.placeholder = (translated !== undefined && translated !== key) ? translated : el.i18nOriginalPlaceholder;
    });
    const langToggle = document.getElementById('langToggle');
    if (langToggle) {
        langToggle.querySelector('span').textContent = lang.toUpperCase();
        langToggle.title = translateValue('language.next');
    }
}

/* ── Theme ──────────────────────────────────────────────────────── */
function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('admin-theme', theme);
    const icon = document.querySelector('#themeToggle i');
    const button = document.getElementById('themeToggle');
    if (icon) icon.className = theme === 'dark' ? 'fas fa-sun' : 'fas fa-moon';
    if (button) button.title = translateValue(theme === 'dark' ? 'theme.light' : 'theme.dark');
    document.dispatchEvent(new CustomEvent('themeChanged', { detail: { theme } }));
}

function initializePreferences() {
    const savedTheme = localStorage.getItem('admin-theme') || 'light';
    applyTheme(savedTheme);
    translatePage();
}

window.AdminUI = {
    addTranslations: mergeTranslations,
    t: translateValue,
    translate: translatePage,
    language: currentLanguage,
    theme: () => document.documentElement.dataset.theme || 'light'
};

/* ── Sidebar (desktop collapse + mobile drawer + collapsible groups) ── */
(function() {
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const menuButton = document.getElementById('menuButton');
    const themeToggle = document.getElementById('themeToggle');
    const langToggle = document.getElementById('langToggle');

    const appShell = document.querySelector('.app-shell');
    const isMobile = () => window.innerWidth <= 980;

    // Restore desktop sidebar state
    if (!isMobile() && localStorage.getItem('sidebar-collapsed') === 'true') {
        appShell?.classList.add('sidebar-collapsed');
    }

    function closeSidebar() {
        sidebar.classList.remove('open');
        backdrop.classList.remove('show');
    }

    menuButton?.addEventListener('click', () => {
        if (isMobile()) {
            sidebar.classList.add('open');
            backdrop.classList.add('show');
        } else {
            appShell.classList.toggle('sidebar-collapsed');
            localStorage.setItem('sidebar-collapsed', appShell.classList.contains('sidebar-collapsed'));
        }
    });
    backdrop?.addEventListener('click', closeSidebar);
    document.querySelectorAll('.side-link').forEach(link => {
        link.addEventListener('click', (e) => {
            closeSidebar();
            const href = link.getAttribute('href');
            if (href && !href.startsWith('#') && !link.classList.contains('active')) {
                const loader = document.getElementById('globalLoader');
                if (loader) loader.classList.add('active');
            }
        });
    });

    // Collapsible nav groups: auto-expand the group containing the active link
    const groups = Array.from(document.querySelectorAll('.nav-group'));
    if (groups.length) {
        const stored = JSON.parse(localStorage.getItem('sidebar-groups') || '{}');
        const activeGroup = groups.find(g => g.querySelector('.side-link.active'))?.dataset.group;
        groups.forEach(group => {
            const key = group.dataset.group;
            let expanded = stored[key];
            if (expanded === undefined) expanded = (key === activeGroup);
            group.classList.toggle('collapsed', !expanded);
            group.querySelector('.nav-section')?.setAttribute('aria-expanded', String(expanded));
        });
        groups.forEach(group => {
            const btn = group.querySelector('.nav-section');
            if (!btn) return;
            btn.addEventListener('click', () => {
                const collapsed = group.classList.toggle('collapsed');
                btn.setAttribute('aria-expanded', String(!collapsed));
                const saved = JSON.parse(localStorage.getItem('sidebar-groups') || '{}');
                saved[group.dataset.group] = !collapsed;
                localStorage.setItem('sidebar-groups', JSON.stringify(saved));
            });
        });
    }

    themeToggle?.addEventListener('click', () => {
        applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    });
    langToggle?.addEventListener('click', () => {
        const next = currentLanguage() === 'vi' ? 'en' : 'vi';
        localStorage.setItem('admin-language', next);
        translatePage();
        document.dispatchEvent(new CustomEvent('admin:language-change', { detail: { language: next } }));
    });

    initializePreferences();

    // Hide loader when page is shown (handles browser back/forward cache)
    window.addEventListener('pageshow', (event) => {
        const loader = document.getElementById('globalLoader');
        if (loader) loader.classList.remove('active');
    });
})();

/* ── Global Search (desktop + mobile) ───────────────────────────── */
(function() {
    function initGlobalSearch(input, results) {
        if (!input || !results) return;
        let debounce;
        input.addEventListener('input', () => {
            clearTimeout(debounce);
            const q = input.value.trim();
            if (q.length < 2) { results.style.display = 'none'; return; }
            debounce = setTimeout(() => {
                fetch(`/dashboard/api/global-search/?q=${encodeURIComponent(q)}`)
                .then(r => r.json())
                .then(data => {
                    if (!data.success || !Object.keys(data.results).length) {
                        results.innerHTML = `<div class="p-3 text-muted small">${translateValue('search.noResults') || 'No results found'}</div>`;
                        results.style.display = 'block'; return;
                    }
                    let html = '';
                    const sections = {users: {icon: 'fa-user', label: translateValue('search.sectionUsers') || 'Users', url: '/dashboard/users/'}, comments: {icon: 'fa-comment', label: translateValue('search.sectionComments') || 'Comments', url: '#'}, movies: {icon: 'fa-film', label: translateValue('search.sectionMovies') || 'Movies', url: '#'}};
                    Object.entries(data.results).forEach(([key, items]) => {
                        const sec = sections[key] || {icon: 'fa-circle', label: key};
                        html += `<div class="px-3 py-1 border-bottom small fw-bold text-muted" style="background:var(--control-hover);border-color:var(--line)!important;"><i class="fas ${sec.icon} me-1"></i>${sec.label}</div>`;
                        items.forEach(item => {
                            const title = item.name || item.title || item.content || item.id;
                            const sub = item.email || item.movieId || item.type || '';
                            let href = '#';
                            if (key === 'users') href = `/dashboard/users/${item.id}/`;
                            else if (key === 'movies') href = item.type === 'tv' ? `/dashboard/tv-shows/${item.id}/` : `/dashboard/movies/${item.id}/`;
                            html += `<a href="${href}" class="d-block px-3 py-2 text-decoration-none border-bottom" style="color:var(--ink);border-color:var(--line)!important;" onmouseover="this.style.background='var(--control-hover)'" onmouseout="this.style.background=''"><div class="small">${title}</div>${sub ? `<div class="text-muted" style="font-size:11px;">${sub}</div>` : ''}</a>`;
                        });
                    });
                    results.innerHTML = html;
                    results.style.display = 'block';
                });
            }, 300);
        });
        input.addEventListener('blur', () => setTimeout(() => results.style.display = 'none', 200));
        input.addEventListener('focus', () => { if (results.innerHTML && input.value.length >= 2) results.style.display = 'block'; });
    }

    initGlobalSearch(document.getElementById('globalSearch'), document.getElementById('searchResults'));

    // Mobile: toggle a full-width search row under the topbar
    const mobileBtn = document.getElementById('mobileSearchBtn');
    const mobileWrap = document.getElementById('mobileSearchWrap');
    if (mobileBtn && mobileWrap) {
        mobileBtn.addEventListener('click', () => {
            const hidden = mobileWrap.style.display === 'none';
            mobileWrap.style.display = hidden ? 'block' : 'none';
            if (hidden) mobileWrap.querySelector('input').focus();
        });
        initGlobalSearch(document.getElementById('mobileSearch'), document.getElementById('mobileSearchResults'));
    }
})();

/* ── Notification Bell Polling ──────────────────────────────────── */
(function() {
    const list = document.getElementById('notifList');
    const badge = document.getElementById('notifBadge');
    if (!list) return;
    function fetchNotifs() {
        fetch('/dashboard/api/admin-notifications/')
        .then(r => r.json())
        .then(data => {
            if (!data.success) return;
            if (badge) { badge.textContent = data.count > 99 ? '99+' : data.count; badge.classList.toggle('d-none', !data.count); }
            if (!data.events.length) { list.innerHTML = `<div class="text-center py-3 text-muted small">${translateValue('notif.noRecent') || 'No recent events'}</div>`; return; }
            list.innerHTML = data.events.map(e => `<div class="d-flex gap-2 px-3 py-2 border-bottom" style="border-color:var(--line)!important;"><i class="fas ${e.icon} text-muted mt-1" style="font-size:12px;"></i><div class="small">${e.title}<br><span class="text-muted" style="font-size:11px;">${e.time || ''}</span></div></div>`).join('');
        }).catch(() => {});
    }
    fetchNotifs();
    setInterval(fetchNotifs, 30000);
})();

/* ── CSRF Helper for AJAX ───────────────────────────────────────── */
window.getCsrfToken = function() {
    const cookie = document.cookie.match(/csrftoken=([^;]+)/);
    if (cookie) return cookie[1];
    const input = document.querySelector('[name=csrfmiddlewaretoken]');
    return input ? input.value : '';
};

/* ── Shimmering Table Skeleton Loader Helper ────────────────────── */
window.getTableSkeleton = function(cols = 5, rows = 5) {
    let html = '';
    for (let r = 0; r < rows; r++) {
        html += '<tr>';
        for (let c = 0; c < cols; c++) {
            const width = 45 + Math.floor(Math.random() * 45);
            html += `<td><span class="skeleton-line" style="width: ${width}%;"></span></td>`;
        }
        html += '</tr>';
    }
    return html;
};

/* ── Global Toast Notification helper ───────────────────────────── */
window.showToast = function(message, type = 'success') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast align-items-center text-white border-0 bg-${type === 'error' ? 'danger' : type === 'warning' ? 'warning' : 'success'}`;
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'assertive');
    toast.setAttribute('aria-atomic', 'true');

    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">
                <i class="fas ${type === 'error' ? 'fa-circle-xmark' : type === 'warning' ? 'fa-triangle-exclamation' : 'fa-circle-check'} me-2"></i>
                ${message}
            </div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
        </div>
    `;

    container.appendChild(toast);
    const bsToast = new bootstrap.Toast(toast, { delay: 3000 });
    bsToast.show();

    toast.addEventListener('hidden.bs.toast', () => {
        toast.remove();
    });
};
