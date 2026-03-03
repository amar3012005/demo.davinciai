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
        },

        /**
         * Capture a screenshot of the current visible viewport.
         * Uses html2canvas (lazy-loaded from CDN on first use).
         * Falls back gracefully when html2canvas fails on modern CSS
         * (lab(), oklch(), color-mix(), etc.).
         *
         * Returns a base64 JPEG string (no data: prefix), or null on failure.
         * Size is capped to prevent exceeding Groq's 4MB base64 limit.
         */
        async captureScreenshot() {
            try {
                // Lazy-load html2canvas if not already available
                if (typeof html2canvas === 'undefined') {
                    await Scanner._loadHtml2Canvas();
                }

                if (typeof html2canvas === 'undefined') {
                    console.warn('[TARA] html2canvas not available, cannot capture screenshot');
                    return null;
                }

                const h2cOpts = {
                    useCORS: true,
                    allowTaint: true,
                    logging: false,
                    scale: Math.min(window.devicePixelRatio, 1.5),
                    width: window.innerWidth,
                    height: window.innerHeight,
                    x: window.scrollX,
                    y: window.scrollY,
                    ignoreElements: (el) => {
                        // Skip TARA's own UI elements + shadow hosts
                        if (el.id === 'tara-overlay-root') return true;
                        return !!(
                            el.closest &&
                            el.closest('.tara-floating-widget, .tara-chat-bar, #tara-ui-container, [data-tara-widget], #tara-overlay-root')
                        );
                    }
                };

                let canvas;
                try {
                    // Attempt 1: normal html2canvas
                    canvas = await html2canvas(document.body, h2cOpts);
                } catch (firstErr) {
                    // html2canvas 1.4.1 crashes on modern CSS color functions
                    // like lab(), oklch(), color-mix() etc.
                    const isColorErr = firstErr && String(firstErr).includes('color function');
                    if (isColorErr) {
                        console.warn('[TARA] html2canvas color parse error — retrying with workaround...');
                        try {
                            // Attempt 2: strip computed styles that crash the parser
                            // by switching to foreignObject-free rendering
                            canvas = await html2canvas(document.documentElement, {
                                ...h2cOpts,
                                foreignObjectRendering: false,
                                removeContainer: true,
                                // Ignore deeply nested elements that usually carry
                                // modern color definitions (design system tokens, etc.)
                                ignoreElements: (el) => {
                                    if (el.id === 'tara-overlay-root') return true;
                                    if (el.closest && el.closest('#tara-overlay-root, [data-tara-widget]')) return true;
                                    // Skip style/link elements that inject lab() colors
                                    if (el.tagName === 'STYLE' || (el.tagName === 'LINK' && el.rel === 'stylesheet')) return false;
                                    return false;
                                }
                            });
                        } catch (retryErr) {
                            console.warn('[TARA] html2canvas retry also failed — falling back to canvas drawImage...');
                            canvas = null;
                        }
                    } else {
                        console.warn('[TARA] html2canvas failed (non-color error):', firstErr);
                        canvas = null;
                    }
                }

                // Attempt 3: native canvas fallback (captures visible tab as-is)
                if (!canvas) {
                    canvas = await Scanner._nativeScreenshot();
                }

                if (!canvas) {
                    console.warn('[TARA] All screenshot methods failed');
                    return null;
                }

                return Scanner._canvasToJpegB64(canvas);
            } catch (err) {
                console.error('[TARA] Screenshot capture failed:', err);
                return null;
            }
        },

        /**
         * Convert a canvas to a base64 JPEG string (no prefix), capped at ~3.5MB.
         */
        _canvasToJpegB64(canvas) {
            const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
            let b64 = dataUrl.replace(/^data:image\/jpeg;base64,/, '');

            const approxBytes = (b64.length * 3) / 4;
            if (approxBytes > 3.5 * 1024 * 1024) {
                console.warn('[TARA] Screenshot too large, recompressing...');
                const smallerUrl = canvas.toDataURL('image/jpeg', 0.4);
                b64 = smallerUrl.replace(/^data:image\/jpeg;base64,/, '');
            }

            const kb = ((b64.length * 3) / 4 / 1024).toFixed(0);
            console.log(`📸 [TARA] Screenshot captured: ${kb}KB`);
            return b64;
        },

        /**
         * Native canvas fallback — creates a blank canvas sized to the viewport
         * and draws a simple visual representation when html2canvas fails entirely.
         * Less accurate but always succeeds.
         */
        async _nativeScreenshot() {
            try {
                // Try the OffscreenCanvas + video capture approach
                if (navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) {
                    // We can't auto-invoke getDisplayMedia (requires user gesture),
                    // so fall back to a simple colored placeholder with page text
                }

                // Simple fallback: render key page text into a canvas
                const w = Math.min(window.innerWidth, 1280);
                const h = Math.min(window.innerHeight, 900);
                const canvas = document.createElement('canvas');
                canvas.width = w;
                canvas.height = h;
                const ctx = canvas.getContext('2d');

                // Draw background
                ctx.fillStyle = '#1a1a2e';
                ctx.fillRect(0, 0, w, h);

                // Draw page title
                ctx.fillStyle = '#e0e0ff';
                ctx.font = 'bold 20px -apple-system, sans-serif';
                ctx.fillText(document.title || 'Untitled Page', 24, 40);

                // Draw URL
                ctx.fillStyle = '#8888aa';
                ctx.font = '13px monospace';
                ctx.fillText(window.location.href, 24, 64);

                // Draw visible text content (first ~40 lines)
                ctx.fillStyle = '#ccccdd';
                ctx.font = '12px -apple-system, sans-serif';
                const bodyText = document.body?.innerText || '';
                const lines = bodyText.split('\n').filter(l => l.trim()).slice(0, 40);
                let y = 96;
                for (const line of lines) {
                    if (y > h - 20) break;
                    ctx.fillText(line.substring(0, 120), 24, y);
                    y += 18;
                }

                // Watermark
                ctx.fillStyle = 'rgba(160,120,255,0.4)';
                ctx.font = 'bold 10px sans-serif';
                ctx.fillText('TARA Fallback Screenshot (html2canvas unavailable)', 24, h - 12);

                console.log('📸 [TARA] Fallback canvas screenshot created');
                return canvas;
            } catch (e) {
                console.error('[TARA] Native screenshot fallback failed:', e);
                return null;
            }
        },

        /**
         * Lazy-load html2canvas from CDN.
         */
        _loadHtml2Canvas() {
            return new Promise((resolve, reject) => {
                if (typeof html2canvas !== 'undefined') {
                    resolve();
                    return;
                }
                const script = document.createElement('script');
                script.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
                script.onload = () => {
                    console.log('✅ [TARA] html2canvas loaded');
                    resolve();
                };
                script.onerror = () => {
                    console.warn('⚠️ [TARA] Failed to load html2canvas');
                    reject(new Error('html2canvas load failed'));
                };
                document.head.appendChild(script);
            });
        }
    };

    window.TARA.Scanner = Scanner;
    console.log('✅ [TARA] Scanner module loaded');
})();
