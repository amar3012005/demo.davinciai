/**
 * TARA Visual Co-Pilot — DOM Scanner
 * Module: tara-scanner.js
 *
 * Owns: Production-grade DOM scanning, stable ID generation,
 *       zone classification, active state detection, table extraction.
 * Depends on: nothing (standalone)
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    // SVG noise tags — shared constant
    const SVG_NOISE_TAGS = new Set([
        'svg', 'path', 'rect', 'circle', 'line', 'polyline', 'polygon',
        'ellipse', 'use', 'defs', 'clippath', 'g', 'mask', 'symbol',
        'lineargradient', 'radialgradient', 'stop', 'pattern', 'marker',
        'filter', 'fegaussianblur', 'feoffset', 'feblend', 'fecolormatrix',
        'text', 'tspan'
    ]);

    const Scanner = {
        lastDOMHash: null,
        previousScanIds: null,

        /**
         * Full production-grade DOM scan. Returns array of element descriptors
         * or null if DOM hasn't changed (unless force=true).
         */
        scanPageBlueprint(force = false) {
            const elements = [];
            const seenIds = new Set();
            const currentScanIds = new Set();

            const collectAllElements = (root) => {
                let collected = [];
                if (!root) return collected;

                const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null, false);
                let node;
                while (node = walker.nextNode()) {
                    collected.push(node);
                    if (node.shadowRoot) {
                        collected = collected.concat(collectAllElements(node.shadowRoot));
                    }
                    if (node.tagName === 'IFRAME') {
                        try {
                            if (node.contentDocument && node.contentDocument.body) {
                                collected = collected.concat(collectAllElements(node.contentDocument.body));
                            }
                        } catch (e) { /* cross-origin */ }
                    }
                }
                return collected;
            };

            const allElements = collectAllElements(document.documentElement);

            allElements.forEach(el => {
                // Skip SVG internals
                if (el instanceof SVGElement || SVG_NOISE_TAGS.has(el.tagName.toLowerCase())) return;

                // Skip TARA's own UI
                if (el.closest('.tara-floating-widget, .tara-chat-bar, #tara-ui-container, [data-tara-widget]')) return;
                if (el.getRootNode() instanceof ShadowRoot) {
                    const host = el.getRootNode().host;
                    if (host && host.closest('.tara-floating-widget, #tara-ui-container, [data-tara-widget]')) return;
                }

                // Skip hidden/disabled
                if (el.disabled || el.type === 'hidden' || el.type === 'password') return;

                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;

                const isFocusable = el.matches('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"]), [contenteditable="true"]');
                const isClickableRole = el.matches('[role="button"], [role="link"], [role="menuitem"], [role="tab"], [role="checkbox"], [role="switch"]');
                const hasPointer = style.cursor === 'pointer';
                const isInteractive = (isFocusable || isClickableRole || hasPointer) && !el.disabled;

                const textContent = el.textContent ? el.textContent.trim() : '';
                const isContext = el.matches('h1, h2, h3, h4, h5, h6, label, th, td, nav, legend, p, li, dt, dd, span[class*="value"], span[class*="price"], span[class*="stat"], span[class*="count"], span[class*="total"], [class*="metric"], [class*="amount"]') ||
                    (el.children.length === 0 && textContent.length > 2 && textContent.length < 200);

                if (!isInteractive && !isContext) return;

                const rect = el.getBoundingClientRect();
                const isInViewport = rect.top < window.innerHeight + 100 &&
                    rect.bottom > -100 &&
                    rect.left < window.innerWidth &&
                    rect.right > 0;

                if (rect.width < 2 || rect.height < 2) return;

                // Assign Persistent ID
                let finalId = el.id || el.getAttribute('name');
                if (!finalId) {
                    if (el.hasAttribute('data-tara-id')) {
                        finalId = el.getAttribute('data-tara-id');
                    } else {
                        finalId = Scanner.generateStableId(el);
                        el.setAttribute('data-tara-id', finalId);
                    }
                }

                if (seenIds.has(finalId)) return;
                seenIds.add(finalId);
                currentScanIds.add(finalId);

                let type = el.tagName.toLowerCase();
                if (['h1', 'h2', 'h3', 'h4', 'h5', 'h6'].includes(type)) type = 'header';

                const rawText = Scanner.extractText(el);
                const cleanText = rawText.replace(/\s+/g, ' ').trim();

                const isNew = Scanner.previousScanIds && !Scanner.previousScanIds.has(finalId);

                let state = "";
                if (document.activeElement === el) state = "focused";
                else if (el.getAttribute('aria-current') || el.getAttribute('aria-selected') === 'true') state = "active";
                else if (el.matches('.active, .selected') && el.matches('a, [role="tab"], [role="link"], [role="menuitem"]')) state = "active";

                elements.push({
                    id: finalId,
                    tag: el.tagName.toLowerCase(),
                    text: cleanText,
                    type: type,
                    interactive: isInteractive,
                    isNew: isNew,
                    state: state,
                    zone: Scanner.classifyZone(el),
                    role: el.getAttribute('role') || '',
                    depth: Scanner.getDepth(el),
                    parentId: Scanner.getParentInteractiveId(el),
                    inViewport: isInViewport,
                    ariaSelected: el.getAttribute('aria-selected'),
                    ariaCurrent: el.getAttribute('aria-current'),
                    ariaExpanded: el.getAttribute('aria-expanded'),
                    rect: {
                        x: Math.round(rect.left + window.scrollX),
                        y: Math.round(rect.top + window.scrollY),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    }
                });
            });

            // Differential update check
            const newHash = Scanner.generateDOMHash(elements);
            if (!force && Scanner.lastDOMHash === newHash) {
                return null;
            }
            Scanner.lastDOMHash = newHash;
            Scanner.previousScanIds = currentScanIds;

            // Sort: NEW first, then interactive, then context
            elements.sort((a, b) => {
                if (a.isNew !== b.isNew) return b.isNew ? 1 : -1;
                if (a.interactive !== b.interactive) return b.interactive ? 1 : -1;
                return 0;
            });

            return elements.slice(0, 600);
        },

        /**
         * Force a DOM scan (reset hash).
         */
        forceScan() {
            Scanner.lastDOMHash = null;
            return Scanner.scanPageBlueprint();
        },

        /**
         * Generate stable hash-based ID using DJB2 + DOM path.
         */
        generateStableId(el) {
            const tag = el.tagName.toLowerCase();
            const text = (el.textContent || '').trim().substring(0, 30);
            const role = el.getAttribute('role') || '';
            const href = el.getAttribute('href') || '';
            const type = el.getAttribute('type') || '';

            let path = '';
            let node = el;
            while (node && node !== document.documentElement) {
                const parent = node.parentElement;
                if (parent) {
                    const siblings = Array.from(parent.children);
                    const index = siblings.indexOf(node);
                    path = `${index}.${path}`;
                }
                node = parent;
            }

            const key = `${tag}|${text}|${role}|${href}|${type}|${path}`;
            let hash = 5381;
            for (let i = 0; i < key.length; i++) {
                hash = ((hash << 5) + hash) ^ key.charCodeAt(i);
            }
            return `t-${(hash >>> 0).toString(36)}`;
        },

        /**
         * Generate hash of element array for diff detection.
         */
        generateDOMHash(elements) {
            let str = '';
            for (const el of elements) {
                str += `${el.id}:${el.text}:${el.rect.x}:${el.rect.y}|`;
            }
            let hash = 5381;
            for (let i = 0; i < str.length; i++) {
                hash = (hash * 33) ^ str.charCodeAt(i);
            }
            return hash >>> 0;
        },

        /**
         * Classify element's semantic zone.
         */
        classifyZone(el) {
            let node = el;
            let depth = 0;
            while (node && node !== document.documentElement && depth < 10) {
                const tag = node.tagName ? node.tagName.toLowerCase() : '';
                const role = node.getAttribute ? (node.getAttribute('role') || '') : '';

                if (tag === 'nav' || role === 'navigation') return 'nav';
                if (tag === 'aside' || role === 'complementary') return 'sidebar';
                if (tag === 'footer' || role === 'contentinfo') return 'footer';
                if (tag === 'header' || role === 'banner') {
                    if (!node.parentElement || node.parentElement === document.body) return 'nav';
                }
                if (role === 'dialog' || role === 'alertdialog' ||
                    (node.classList && (node.classList.contains('modal') || node.classList.contains('dialog')))) {
                    return 'modal';
                }
                if (tag === 'main' || role === 'main') return 'main';

                node = node.parentElement;
                depth++;
            }
            return 'main';
        },

        getDepth(el) {
            let depth = 0;
            let node = el;
            while (node && node !== document.documentElement && depth < 20) {
                node = node.parentElement;
                depth++;
            }
            return depth;
        },

        getParentInteractiveId(el) {
            let node = el.parentElement;
            let steps = 0;
            while (node && node !== document.documentElement && steps < 5) {
                const id = node.id || node.getAttribute('data-tara-id');
                if (id) {
                    const isInteractive = node.matches &&
                        node.matches('button, a[href], input, select, textarea, [role="button"], [role="link"], [role="tab"], [tabindex="0"]');
                    if (isInteractive || node.matches('nav, main, aside, header, footer, section, [role="navigation"]')) {
                        return id;
                    }
                }
                node = node.parentElement;
                steps++;
            }
            return '';
        },

        detectActiveStates() {
            const states = { activePage: null, activeTab: null, expandedSections: [] };
            try {
                const activeCandidates = document.querySelectorAll(
                    '[aria-current="page"], [aria-current="true"], [aria-current="step"], [aria-selected="true"]'
                );
                activeCandidates.forEach(el => {
                    if (el.closest('.tara-floating-widget, .tara-chat-bar')) return;
                    if (el.matches('a, [role="link"], [role="tab"], [role="menuitem"]')) {
                        const text = (el.textContent || '').trim().slice(0, 40);
                        if (!text) return;
                        if (el.closest('nav')) states.activePage = text;
                        else if (el.matches('[role="tab"], [aria-selected="true"]')) states.activeTab = text;
                    }
                });

                if (!states.activePage) {
                    document.querySelectorAll('nav a.active, nav [role="link"].active').forEach(el => {
                        if (el.closest('.tara-floating-widget, .tara-chat-bar')) return;
                        const text = (el.textContent || '').trim().slice(0, 40);
                        if (text && !states.activePage) states.activePage = text;
                    });
                }

                document.querySelectorAll('[aria-expanded="true"]').forEach(el => {
                    if (el.closest('.tara-floating-widget, .tara-chat-bar')) return;
                    states.expandedSections.push((el.textContent || '').trim().slice(0, 40));
                });
            } catch (e) { /* safety */ }
            return states;
        },

        extractVisibleTables() {
            const tables = [];
            try {
                document.querySelectorAll('table, [role="grid"], [role="table"]').forEach(table => {
                    const rows = [];
                    table.querySelectorAll('tr, [role="row"]').forEach(row => {
                        const cells = [];
                        row.querySelectorAll('td, th, [role="cell"], [role="columnheader"]')
                            .forEach(cell => cells.push(cell.textContent.trim().slice(0, 60)));
                        if (cells.length) rows.push(cells);
                    });
                    if (rows.length) tables.push({ headers: rows[0], rows: rows.slice(1).slice(0, 15) });
                });
            } catch (e) { /* safety */ }
            return tables;
        },

        extractText(el) {
            let text = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || '';
            if (text) return Scanner.cleanText(text);

            if (el.children.length === 0 || el.matches('td, th, li, dt, dd, span, label, p')) {
                text = el.innerText || el.textContent || el.value || '';
            } else {
                text = el.innerText || el.value || '';
            }
            const cleaned = Scanner.cleanText(text);
            if (cleaned.length > 0) return cleaned;

            const img = el.querySelector('img');
            if (img && img.alt) return Scanner.cleanText(img.alt);

            const svgTitle = el.querySelector('svg title');
            if (svgTitle) return Scanner.cleanText(svgTitle.textContent);

            return '';
        },

        cleanText(str) {
            if (str === null || str === undefined) return '';
            return String(str).replace(/\s+/g, ' ').trim().substring(0, 80);
        },

        detectModal() {
            const dialogs = document.querySelectorAll('dialog[open], [role="dialog"], [role="alertdialog"], .modal.show, .modal.active, [aria-modal="true"]');
            return dialogs.length > 0;
        },

        async waitForDOMSettle(maxWait = 3000, stableFor = 300) {
            return new Promise((resolve) => {
                let lastMutationTime = Date.now();
                let settled = false;

                const observer = new MutationObserver(() => {
                    lastMutationTime = Date.now();
                });

                observer.observe(document.body, {
                    childList: true,
                    subtree: true,
                    attributes: true,
                    characterData: true
                });

                const checkInterval = setInterval(() => {
                    const elapsed = Date.now() - lastMutationTime;
                    if (elapsed >= stableFor) {
                        settled = true;
                        cleanup();
                    }
                }, 50);

                const timeout = setTimeout(() => {
                    if (!settled) cleanup();
                }, maxWait);

                function cleanup() {
                    observer.disconnect();
                    clearInterval(checkInterval);
                    clearTimeout(timeout);
                    resolve();
                }
            });
        }
    };

    window.TARA.Scanner = Scanner;
    console.log('✅ [TARA] Scanner module loaded');
})();
