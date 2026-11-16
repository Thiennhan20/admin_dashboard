/* Admin Panel — shared UI logic: i18n, theme, sidebar, global search, toasts */

/* ── i18n helpers ───────────────────────────────────────────────── */
let _initialTranslateScheduled = false;

function scheduleTranslatePage(root = document) {
    if (root === document && document.readyState === 'loading') {
        if (!_initialTranslateScheduled) {
            _initialTranslateScheduled = true;
            document.addEventListener('DOMContentLoaded', () => {
                translatePage(document);
            }, { once: true });
        }
        return;
    }
    translatePage(root);
}

function mergeTranslations(extra) {
    Object.entries(extra || {}).forEach(([lang, messages]) => {
        window.AdminI18N[lang] = Object.assign(window.AdminI18N[lang] || {}, messages || {});
    });
    if (typeof translatePage === 'function') {
        scheduleTranslatePage(document);
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
    root.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
        const key = el.dataset.i18nAriaLabel;
        if (!el.hasOwnProperty('i18nOriginalAriaLabel')) {
            el.i18nOriginalAriaLabel = el.getAttribute('aria-label') || '';
        }
        const translated = translateValue(key);
        const nextVal = (translated !== undefined && translated !== key) ? translated : el.i18nOriginalAriaLabel;
        if (nextVal) {
            el.setAttribute('aria-label', nextVal);
        }
    });
    const themeToggleBtn = document.getElementById('themeToggle');
    if (themeToggleBtn) {
        const currentTheme = document.documentElement.dataset.theme || 'dark';
        const themeLabel = translateValue(currentTheme === 'dark' ? 'theme.light' : 'theme.dark') || translateValue('theme.toggle') || 'Dark/Light Mode';
        themeToggleBtn.title = themeLabel;
        themeToggleBtn.setAttribute('aria-label', themeLabel);
    }
    const langToggle = document.getElementById('langToggle');
    if (langToggle) {
        const langSpan = langToggle.querySelector('span');
        if (langSpan) {
            const isCompact = langToggle.dataset.langCompact === 'true' || langToggle.classList.contains('theme-fab');
            langSpan.textContent = isCompact
                ? lang.toUpperCase()
                : `${translateValue('language.toggle')} (${lang.toUpperCase()})`;
        }
        const nextLangLabel = lang === 'vi' ? 'EN' : 'VI';
        const nextTitle = translateValue('language.next') || 'Switch language';
        langToggle.title = `${nextTitle} (${nextLangLabel})`;
        langToggle.setAttribute('aria-label', langToggle.title);
    }
    root.querySelectorAll('.flash-message-text').forEach((el) => {
        if (!el.dataset.viFlash) {
            el.dataset.viFlash = el.textContent;
        }
        el.textContent = (lang === 'en' && typeof window.translateAdminFlash === 'function')
            ? window.translateAdminFlash(el.dataset.viFlash)
            : el.dataset.viFlash;
    });
    root.querySelectorAll('.i18n-timesince').forEach((el) => {
        const raw = (el.dataset.rawTimesince || '').replace(/\u00a0/g, ' ').trim();
        if (!raw) return;
        const withAgo = el.dataset.withAgo === '1';
        if (lang === 'vi') {
            const viBody = raw
                .replace(/\byears?\b/gi, 'năm')
                .replace(/\bmonths?\b/gi, 'tháng')
                .replace(/\bweeks?\b/gi, 'tuần')
                .replace(/\bdays?\b/gi, 'ngày')
                .replace(/\bhours?\b/gi, 'giờ')
                .replace(/\bminutes?\b/gi, 'phút');
            el.textContent = withAgo ? `${viBody} ${translateValue('common.ago') || 'trước'}` : viBody;
        } else {
            el.textContent = withAgo ? `${raw} ${translateValue('common.ago') || 'ago'}` : raw;
        }
    });
    if (typeof window.syncAllCustomSelects === 'function') {
        window.syncAllCustomSelects(root);
    }
}

/* ── Theme ──────────────────────────────────────────────────────── */
function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.bsTheme = theme;
    localStorage.setItem('admin-theme', theme);
    const icon = document.querySelector('#themeToggle i');
    const button = document.getElementById('themeToggle');
    if (icon) {
        const isFloating = button?.classList.contains('theme-fab');
        const baseIcon = theme === 'dark' ? 'fas fa-sun fa-fw' : 'fas fa-moon fa-fw';
        icon.className = isFloating ? baseIcon : `${baseIcon} text-muted`;
    }
    if (button) {
        const label = translateValue(theme === 'dark' ? 'theme.light' : 'theme.dark') || 'Dark/Light Mode';
        button.title = label;
        button.setAttribute('aria-label', label);
    }
    document.dispatchEvent(new CustomEvent('themeChanged', { detail: { theme } }));
}

function initializePreferences() {
    const savedTheme = localStorage.getItem('admin-theme') || 'dark';
    document.documentElement.lang = currentLanguage();
    applyTheme(savedTheme);
    scheduleTranslatePage(document);
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

window.AdminUI = {
    addTranslations: mergeTranslations,
    t: translateValue,
    translate: translatePage,
    escapeHtml: escapeHtml,
    language: currentLanguage,
    theme: () => document.documentElement.dataset.theme || 'dark',
    refreshSelects: () => initCustomSelects(document)
};

/* ── Custom Select Enhancer (Replaces Native OS Select Popups) ──── */
const customSelectRegistry = new Map();
let activeCustomSelect = null;
let customSelectGlobalsBound = false;

function getSelectParts(select) {
    return customSelectRegistry.get(select);
}

function closeActiveCustomSelect(restoreFocus = false) {
    if (!activeCustomSelect) return;
    const current = activeCustomSelect;
    activeCustomSelect = null;
    current.wrap.classList.remove('is-open');
    current.trigger.setAttribute('aria-expanded', 'false');
    current.menu.classList.remove('is-open', 'open-up');
    if (restoreFocus && typeof current.trigger.focus === 'function') {
        current.trigger.focus();
    }
}

function positionCustomSelectMenu(instance) {
    const { trigger, menu, isAutoWidth } = instance;
    if (!menu.parentNode) {
        document.body.appendChild(menu);
    }

    const rect = trigger.getBoundingClientRect();
    const minW = isAutoWidth ? Math.max(rect.width, 160) : rect.width;
    const maxAllowedWidth = Math.max(180, window.innerWidth - 24);

    menu.style.visibility = 'hidden';
    menu.style.minWidth = `${Math.min(minW, maxAllowedWidth)}px`;
    menu.style.width = isAutoWidth ? 'max-content' : `${rect.width}px`;
    menu.style.maxWidth = `${maxAllowedWidth}px`;
    menu.classList.add('is-open');

    const menuRect = menu.getBoundingClientRect();
    const menuHeight = menuRect.height;
    const menuWidth = menuRect.width;
    const spaceBelow = window.innerHeight - rect.bottom - 10;
    const spaceAbove = rect.top - 10;
    const openUp = spaceBelow < menuHeight && spaceAbove > spaceBelow;

    const top = openUp
        ? Math.max(8, rect.top - menuHeight - 6)
        : Math.min(rect.bottom + 6, Math.max(8, window.innerHeight - menuHeight - 8));

    let left = rect.left;
    if (left + menuWidth > window.innerWidth - 12) {
        left = Math.max(12, rect.right - menuWidth);
    }
    left = Math.max(12, Math.min(left, window.innerWidth - menuWidth - 12));

    menu.classList.toggle('open-up', openUp);
    menu.style.top = `${Math.round(top)}px`;
    menu.style.left = `${Math.round(left)}px`;
    menu.style.visibility = '';
}

function buildCustomSelectOptions(instance) {
    const { select, menu, closeAndCommit } = instance;
    menu.innerHTML = '';

    const appendOptionButton = (option) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'custom-select-option';
        btn.setAttribute('role', 'option');
        btn.dataset.optionIndex = String(option.index);
        if (option.disabled) btn.disabled = true;
        if (option.selected) {
            btn.classList.add('is-selected');
            btn.setAttribute('aria-selected', 'true');
        } else {
            btn.setAttribute('aria-selected', 'false');
        }

        const textSpan = document.createElement('span');
        textSpan.className = 'custom-select-option-text';
        textSpan.textContent = option.textContent.trim();

        const checkIcon = document.createElement('i');
        checkIcon.className = 'fas fa-check custom-select-check';
        checkIcon.setAttribute('aria-hidden', 'true');

        btn.append(textSpan, checkIcon);
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!option.disabled) {
                closeAndCommit(option.index);
            }
        });
        menu.appendChild(btn);
    };

    Array.from(select.children).forEach((child) => {
        if (child.tagName === 'OPTGROUP') {
            if (child.label) {
                const groupLabel = document.createElement('div');
                groupLabel.className = 'custom-select-group-label';
                groupLabel.textContent = child.label;
                menu.appendChild(groupLabel);
            }
            Array.from(child.children).forEach((opt) => {
                if (opt.tagName === 'OPTION') appendOptionButton(opt);
            });
        } else if (child.tagName === 'OPTION') {
            appendOptionButton(child);
        }
    });
}

function bindCustomSelectGlobals() {
    if (customSelectGlobalsBound) return;
    customSelectGlobalsBound = true;

    document.addEventListener('mousedown', (e) => {
        if (!activeCustomSelect) return;
        const { wrap, menu } = activeCustomSelect;
        if (!wrap.contains(e.target) && !menu.contains(e.target)) {
            closeActiveCustomSelect(false);
        }
    });

    document.addEventListener('keydown', (e) => {
        if (!activeCustomSelect) return;
        const { menu, closeAndCommit } = activeCustomSelect;
        const options = Array.from(menu.querySelectorAll('.custom-select-option:not(:disabled)'));
        if (!options.length) return;

        const existingIdx = options.findIndex(o => o.classList.contains('is-focused'));
        const selectedIdx = options.findIndex(o => o.classList.contains('is-selected'));
        const currentIdx = existingIdx >= 0 ? existingIdx : Math.max(0, selectedIdx);
        const setFocused = (nextIdx) => {
            options.forEach(o => o.classList.remove('is-focused'));
            const target = options[nextIdx];
            if (target) {
                target.classList.add('is-focused');
                target.scrollIntoView({ block: 'nearest' });
            }
        };

        if (e.key === 'Escape') {
            e.preventDefault();
            closeActiveCustomSelect(true);
        } else if (e.key === 'Tab') {
            closeActiveCustomSelect(false);
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            setFocused(existingIdx >= 0 ? (currentIdx + 1) % options.length : (selectedIdx >= 0 ? (selectedIdx + 1) % options.length : 0));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setFocused(existingIdx >= 0 ? (currentIdx - 1 + options.length) % options.length : (selectedIdx >= 0 ? (selectedIdx - 1 + options.length) % options.length : options.length - 1));
        } else if (e.key === 'Home') {
            e.preventDefault();
            setFocused(0);
        } else if (e.key === 'End') {
            e.preventDefault();
            setFocused(options.length - 1);
        } else if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            const focused = options[currentIdx];
            if (focused) closeAndCommit(Number(focused.dataset.optionIndex));
        } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
            const needle = e.key.toLowerCase();
            const start = (currentIdx + 1) % options.length;
            const ordered = options.slice(start).concat(options.slice(0, start));
            const match = ordered.find(o => o.textContent.trim().toLowerCase().startsWith(needle));
            if (match) setFocused(options.indexOf(match));
        }
    });

    window.addEventListener('resize', () => closeActiveCustomSelect(false));
    document.addEventListener('scroll', (e) => {
        if (!activeCustomSelect) return;
        const { menu } = activeCustomSelect;
        if (e.target !== menu && !menu.contains(e.target)) {
            closeActiveCustomSelect(false);
        }
    }, true);
}

function enhanceSelectElement(select, premeasuredHeight = null) {
    if (!select || select.dataset.customSelectInit === '1') return;
    if (select.multiple || Number(select.size) > 1 || select.dataset.nativeSelect !== undefined) return;
    if (!select.parentNode) return;

    bindCustomSelectGlobals();

    const isAutoWidth = select.style.width === 'auto';
    const rectHeight = premeasuredHeight !== null
        ? premeasuredHeight
        : Math.round(select.getBoundingClientRect().height || 0);
    select.dataset.customSelectInit = '1';

    const wrap = document.createElement('div');
    wrap.className = 'custom-select-wrap';
    if (select.classList.contains('form-select-sm')) wrap.classList.add('custom-select-sm');
    else if (select.classList.contains('form-select-lg')) wrap.classList.add('custom-select-lg');
    if (isAutoWidth) wrap.classList.add('custom-select-auto');

    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.classList.add('custom-select-native');
    select.tabIndex = -1;

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'custom-select-trigger';
    trigger.setAttribute('role', 'combobox');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    if (select.getAttribute('aria-label')) {
        trigger.setAttribute('aria-label', select.getAttribute('aria-label'));
    }
    if (!wrap.classList.contains('custom-select-sm') && !wrap.classList.contains('custom-select-lg') && rectHeight >= 42) {
        trigger.style.minHeight = `${rectHeight}px`;
        trigger.style.paddingLeft = '1rem';
        trigger.style.paddingRight = '1rem';
    }

    const labelEl = document.createElement('span');
    labelEl.className = 'custom-select-label';
    const caretEl = document.createElement('i');
    caretEl.className = 'fas fa-chevron-down custom-select-caret';
    caretEl.setAttribute('aria-hidden', 'true');
    trigger.append(labelEl, caretEl);
    wrap.appendChild(trigger);

    const menu = document.createElement('div');
    menu.className = 'custom-select-menu';
    menu.setAttribute('role', 'listbox');
    menu.addEventListener('pointermove', () => {
        menu.querySelectorAll('.custom-select-option.is-focused').forEach(el => el.classList.remove('is-focused'));
    });

    const syncUI = () => {
        const disabled = !!select.disabled;
        trigger.disabled = disabled;
        wrap.classList.toggle('is-disabled', disabled);

        const selectedOpt = select.options[select.selectedIndex] || select.options[0];
        labelEl.textContent = selectedOpt ? selectedOpt.textContent.trim() : '';
        labelEl.classList.toggle('is-placeholder', !!(selectedOpt && selectedOpt.disabled && !selectedOpt.value));

        if (activeCustomSelect && activeCustomSelect.select === select) {
            buildCustomSelectOptions(instance);
        }
    };

    const closeAndCommit = (optionIndex) => {
        const changed = select.selectedIndex !== optionIndex;
        select.selectedIndex = optionIndex;
        wrap.classList.remove('is-invalid');
        syncUI();
        closeActiveCustomSelect(true);
        if (changed) {
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }
    };

    const openMenu = () => {
        if (select.disabled) return;
        if (activeCustomSelect && activeCustomSelect.select === select) {
            closeActiveCustomSelect(false);
            return;
        }
        closeActiveCustomSelect(false);
        buildCustomSelectOptions(instance);
        activeCustomSelect = instance;
        wrap.classList.add('is-open');
        trigger.setAttribute('aria-expanded', 'true');
        positionCustomSelectMenu(instance);
        const selectedOptionEl = menu.querySelector('.custom-select-option.is-selected');
        if (selectedOptionEl) {
            selectedOptionEl.scrollIntoView({ block: 'nearest' });
        }
    };

    const instance = { select, wrap, trigger, labelEl, menu, isAutoWidth, syncUI, closeAndCommit };
    customSelectRegistry.set(select, instance);

    trigger.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        openMenu();
    });

    trigger.addEventListener('keydown', (e) => {
        if (activeCustomSelect && activeCustomSelect.select === select) return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            openMenu();
        }
    });

    let isInvalidating = false;

    select.addEventListener('change', () => {
        wrap.classList.remove('is-invalid');
        syncUI();
    });

    select.addEventListener('invalid', () => {
        isInvalidating = true;
        wrap.classList.add('is-invalid');
        setTimeout(() => { isInvalidating = false; }, 200);
    });

    select.addEventListener('focus', () => {
        if (!isInvalidating) {
            trigger.focus();
        }
    });

    if (select.form) {
        select.form.addEventListener('reset', () => {
            setTimeout(syncUI, 0);
        });
    }

    const valueDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
    const indexDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
    if (valueDesc && valueDesc.set) {
        Object.defineProperty(select, 'value', {
            configurable: true,
            get() { return valueDesc.get.call(this); },
            set(v) {
                valueDesc.set.call(this, v);
                syncUI();
            }
        });
    }
    if (indexDesc && indexDesc.set) {
        Object.defineProperty(select, 'selectedIndex', {
            configurable: true,
            get() { return indexDesc.get.call(this); },
            set(v) {
                indexDesc.set.call(this, v);
                syncUI();
            }
        });
    }

    const observer = new MutationObserver(() => syncUI());
    observer.observe(select, { childList: true, attributes: true, attributeFilter: ['disabled'] });

    syncUI();
}

function initCustomSelects(root = document) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    const targets = Array.from(root.querySelectorAll('select.form-select')).filter((select) => (
        select
        && select.dataset.customSelectInit !== '1'
        && !select.multiple
        && Number(select.size) <= 1
        && select.dataset.nativeSelect === undefined
        && select.parentNode
    ));
    if (!targets.length) return;
    // Batch all geometry reads before mutating the DOM to prevent layout thrashing
    const heights = targets.map((s) => Math.round(s.getBoundingClientRect().height || 0));
    targets.forEach((select, idx) => enhanceSelectElement(select, heights[idx]));
}

function syncAllCustomSelects(root = document) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    const uninitialized = [];
    root.querySelectorAll('select.form-select').forEach((select) => {
        const instance = getSelectParts(select);
        if (instance) instance.syncUI();
        else if (select.dataset.customSelectInit !== '1') uninitialized.push(select);
    });
    if (uninitialized.length) {
        const valid = uninitialized.filter((s) => !s.multiple && Number(s.size) <= 1 && s.dataset.nativeSelect === undefined && s.parentNode);
        const heights = valid.map((s) => Math.round(s.getBoundingClientRect().height || 0));
        valid.forEach((select, idx) => enhanceSelectElement(select, heights[idx]));
    }
}

window.syncAllCustomSelects = syncAllCustomSelects;

/* ── Sidebar (desktop collapse + mobile drawer + collapsible groups) ── */
(function() {
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const menuButton = document.getElementById('menuButton');
    const themeToggle = document.getElementById('themeToggle');
    const langToggle = document.getElementById('langToggle');

    const appShell = document.querySelector('.app-shell');
    const sidebarMenu = sidebar?.querySelector('.sidebar-menu');
    const isMobile = () => window.innerWidth <= 1024;

    // Floating tooltip when desktop sidebar is collapsed into icon rail
    const railTooltip = sidebar ? document.createElement('div') : null;
    const railTooltipText = sidebar ? document.createElement('span') : null;
    const railTooltipBadge = sidebar ? document.createElement('span') : null;
    if (railTooltip) {
        railTooltip.className = 'sidebar-rail-tooltip';
        railTooltip.setAttribute('role', 'tooltip');
        railTooltipBadge.className = 'sidebar-rail-tooltip-badge';
        railTooltip.append(railTooltipText, railTooltipBadge);
        document.body.appendChild(railTooltip);
    }

    function hideRailTooltip() {
        railTooltip?.classList.remove('show');
    }

    function showRailTooltip(target) {
        if (!railTooltip || !target || isMobile() || !appShell?.classList.contains('sidebar-collapsed')) {
            hideRailTooltip();
            return;
        }
        const labelEl = target.classList.contains('brand')
            ? target.querySelector('.brand-title')
            : target.querySelector('span:not(.side-badge)');
        const label = labelEl ? labelEl.textContent.trim() : (target.getAttribute('aria-label') || '');
        if (!label) {
            hideRailTooltip();
            return;
        }
        const badgeEl = target.querySelector('.side-badge');
        const badgeText = badgeEl ? badgeEl.textContent.trim() : '';

        railTooltipText.textContent = label;
        if (badgeText) {
            railTooltipBadge.textContent = badgeText;
            railTooltipBadge.style.display = '';
        } else {
            railTooltipBadge.style.display = 'none';
        }

        const rect = target.getBoundingClientRect();
        const centerY = Math.max(24, Math.min(window.innerHeight - 24, rect.top + rect.height / 2));
        railTooltip.style.left = `${Math.round(rect.right + 10)}px`;
        railTooltip.style.top = `${Math.round(centerY)}px`;
        railTooltip.classList.add('show');
    }

    // Restore desktop sidebar state and remove pre-render FOUC helper class
    if (!isMobile() && localStorage.getItem('sidebar-collapsed') === 'true') {
        appShell?.classList.add('sidebar-collapsed');
    }
    document.documentElement.classList.remove('sidebar-collapsed-init');

    function closeSidebar() {
        sidebar?.classList.remove('open');
        backdrop?.classList.remove('show');
    }

    window.addEventListener('resize', () => {
        hideRailTooltip();
        if (!isMobile()) {
            closeSidebar();
            appShell?.classList.toggle('sidebar-collapsed', localStorage.getItem('sidebar-collapsed') === 'true');
        }
    }, { passive: true });

    sidebarMenu?.addEventListener('scroll', hideRailTooltip, { passive: true });

    menuButton?.addEventListener('click', () => {
        hideRailTooltip();
        if (isMobile()) {
            sidebar.classList.add('open');
            backdrop.classList.add('show');
        } else {
            appShell.classList.toggle('sidebar-collapsed');
            localStorage.setItem('sidebar-collapsed', appShell.classList.contains('sidebar-collapsed'));
        }
    });
    backdrop?.addEventListener('click', closeSidebar);
    sidebar?.querySelectorAll('.side-link, .brand').forEach(item => {
        item.addEventListener('mouseenter', () => showRailTooltip(item));
        item.addEventListener('focusin', () => showRailTooltip(item));
        item.addEventListener('mouseleave', hideRailTooltip);
        item.addEventListener('focusout', hideRailTooltip);
    });
    document.querySelectorAll('.side-link').forEach(link => {
        link.addEventListener('click', (e) => {
            hideRailTooltip();
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
            const expanded = key === activeGroup ? true : Boolean(stored[key]);
            group.classList.toggle('collapsed', !expanded);
            group.querySelector('.nav-section')?.setAttribute('aria-expanded', String(expanded));
        });
        void sidebar?.offsetHeight;
        sidebar?.classList.add('sidebar-groups-ready');
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

        const activeLink = sidebar?.querySelector('.side-link.active');
        if (activeLink) {
            requestAnimationFrame(() => activeLink.scrollIntoView({ block: 'nearest' }));
        }
    }

    themeToggle?.addEventListener('click', () => {
        applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    });
    langToggle?.addEventListener('click', () => {
        hideRailTooltip();
        const next = currentLanguage() === 'vi' ? 'en' : 'vi';
        localStorage.setItem('admin-language', next);
        translatePage();
        document.dispatchEvent(new CustomEvent('admin:language-change', { detail: { language: next } }));
    });

    initCustomSelects(document);
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
    const CACHE_KEY = 'adminNotifCacheV1';
    const CACHE_TTL_MS = 25000;

    let lastNotifData = null;

    function formatNotifTitle(ev) {
        let detail = ev.detail;
        if (detail === undefined) {
            const raw = String(ev.title || '');
            if (ev.type === 'user' && raw.startsWith('New user: ')) {
                detail = raw.slice('New user: '.length);
            } else if (ev.type === 'comment' && raw.startsWith('New comment: ')) {
                detail = raw.slice('New comment: '.length);
            }
        }
        if (ev.type === 'user') {
            const who = (!detail || detail === 'Unknown') ? translateValue('notif.unknownUser') : detail;
            return `${escapeHtml(translateValue('notif.newUser'))}: ${escapeHtml(who)}`;
        }
        if (ev.type === 'comment') {
            return `${escapeHtml(translateValue('notif.newComment'))}: ${escapeHtml(detail || '')}`;
        }
        if (ev.type === 'report') {
            return `${escapeHtml(translateValue('notif.pendingReport'))}: ${escapeHtml(detail || '')}`;
        }
        if (ev.type === 'error') {
            return `${escapeHtml(translateValue('notif.newError'))}: ${escapeHtml(detail || '')}`;
        }
        if (ev.type === 'room') {
            return `${escapeHtml(translateValue('notif.liveRoom'))}: ${escapeHtml(detail || '')}`;
        }
        return escapeHtml(ev.title || '');
    }

    function renderNotifs(data) {
        if (!data || !data.success) return;
        lastNotifData = data;
        if (badge) { badge.textContent = data.count > 99 ? '99+' : data.count; badge.classList.toggle('d-none', !data.count); }
        if (!data.events.length) { list.innerHTML = `<div class="text-center py-3 text-muted small">${escapeHtml(translateValue('notif.noRecent') || 'No recent events')}</div>`; return; }
        list.innerHTML = data.events.map(e => {
            const iconClass = /^fa-[a-z0-9-]+$/i.test(e.icon || '') ? e.icon : 'fa-bell';
            const iconColor = e.type === 'error' ? 'text-danger' : (e.type === 'report' ? 'text-warning' : (e.type === 'room' ? 'text-success' : 'text-muted'));
            const body = `<div class="d-flex gap-2 px-3 py-2 border-bottom" style="border-color:var(--line)!important;"><i class="fas ${iconClass} ${iconColor} mt-1" style="font-size:12px;"></i><div class="small">${formatNotifTitle(e)}<br><span class="text-muted" style="font-size:11px;">${escapeHtml(e.time || '')}</span></div></div>`;
            if (typeof e.url === 'string' && e.url.startsWith('/')) {
                return `<a href="${escapeHtml(e.url)}" class="d-block text-decoration-none text-reset">${body}</a>`;
            }
            return body;
        }).join('');
    }

    document.addEventListener('admin:language-change', () => {
        if (lastNotifData) renderNotifs(lastNotifData);
    });

    function fetchNotifs(ignoreSessionCache) {
        if (!ignoreSessionCache) {
            try {
                const raw = sessionStorage.getItem(CACHE_KEY);
                if (raw) {
                    const cached = JSON.parse(raw);
                    if (cached && (Date.now() - cached.ts) < CACHE_TTL_MS && cached.data) {
                        renderNotifs(cached.data);
                        return;
                    }
                }
            } catch (e) {}
        }
        fetch('/dashboard/api/admin-notifications/')
        .then(r => r.json())
        .then(data => {
            renderNotifs(data);
            if (data && data.success) {
                try { sessionStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), data })); } catch (e) {}
            }
        }).catch(() => {});
    }

    fetchNotifs(false);
    setInterval(() => {
        if (!document.hidden) fetchNotifs(true);
    }, 60000);
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

    const palette = {
        error: { bg: 'danger', text: 'text-white', closeWhite: true, icon: 'fa-circle-xmark' },
        danger: { bg: 'danger', text: 'text-white', closeWhite: true, icon: 'fa-circle-xmark' },
        warning: { bg: 'warning', text: 'text-dark', closeWhite: false, icon: 'fa-triangle-exclamation' },
        info: { bg: 'info', text: 'text-dark', closeWhite: false, icon: 'fa-circle-info' },
        success: { bg: 'success', text: 'text-white', closeWhite: true, icon: 'fa-circle-check' }
    };
    const cfg = palette[type] || palette.success;

    const toast = document.createElement('div');
    toast.className = `toast align-items-center ${cfg.text} border-0 bg-${cfg.bg}`;
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'assertive');
    toast.setAttribute('aria-atomic', 'true');
    if (!cfg.closeWhite) {
        toast.setAttribute('data-bs-theme', 'light');
    }

    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">
                <i class="fas ${cfg.icon} me-2"></i>
                ${message}
            </div>
            <button type="button" class="btn-close ${cfg.closeWhite ? 'btn-close-white ' : ''}me-2 m-auto" data-bs-dismiss="toast" aria-label="${translateValue('common.close') || 'Đóng'}"></button>
        </div>
    `;

    container.appendChild(toast);
    const bsToast = new bootstrap.Toast(toast, { delay: 3000 });
    bsToast.show();

    toast.addEventListener('hidden.bs.toast', () => {
        toast.remove();
    });
};
