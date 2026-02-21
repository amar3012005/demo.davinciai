/**
 * tara_sensor.js
 *
 * PURPOSE: Real-time DOM change detector and streamer. Replaces bulk DOM
 *          snapshot logic with incremental delta streaming for millisecond
 *          updates to the server-side Live Graph.
 *
 * DEPENDENCIES: None (vanilla ES6 JavaScript)
 *
 * USED BY:
 *   - tara-widget.js: Initializes TaraSensor on session start
 *   - WebSocket handler: Receives dom_delta messages
 *
 * MIGRATION STATUS: [NEW] - Perception layer for Ultimate TARA
 *
 * FEATURES:
 *   - MutationObserver for DOM change detection
 *   - Debounced delta transmission (100-200ms)
 *   - Stable ID generation using DJB2 hash
 *   - Client-side filtering (SVG, decorative elements)
 *   - Zone classification (nav, main, modal, sidebar)
 *
 * USAGE:
 *   const sensor = new TaraSensor(websocket, {
 *       sendFullScanOnInit: true,
 *       debounceMs: 150
 *   });
 *   sensor.start();
 */

class TaraSensor {
    /**
     * Create a new TaraSensor instance.
     * @param {WebSocket} websocket - WebSocket connection to server
     * @param {Object} config - Configuration options
     * @param {boolean} [config.sendFullScanOnInit=true] - Send full DOM scan on start
     * @param {number} [config.debounceMs=150] - Debounce delay for delta transmission
     * @param {number} [config.maxBatchSize=50] - Maximum deltas per batch
     */
    constructor(websocket, config = {}) {
        this.ws = websocket;
        this.config = {
            sendFullScanOnInit: true,
            debounceMs: 150,
            maxBatchSize: 50,
            ...config
        };

        /** @type {MutationObserver|null} */
        this.observer = null;

        /** @type {Map<string, Object>} */
        this.knownNodes = new Map();  // id -> NodeSnapshot

        /** @type {Array<Object>} */
        this.pendingDeltas = [];

        /** @type {ReturnType<typeof setTimeout>|null} */
        this.debounceTimer = null;

        /** @type {boolean} */
        this.isRunning = false;

        console.log('👁️ TaraSensor initialized', this.config);
    }

    /**
     * Initialize the MutationObserver and start watching the DOM.
     * Sends full scan on startup if configured.
     */
    start() {
        if (this.isRunning) {
            console.warn('TaraSensor already running');
            return;
        }

        this.isRunning = true;

        // Full scan on startup
        if (this.config.sendFullScanOnInit) {
            this.performFullScan();
        }

        // Setup MutationObserver
        this.observer = new MutationObserver((mutations) => {
            this.handleMutations(mutations);
        });

        // Observe body with attribute filtering for performance
        this.observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class', 'aria-selected', 'aria-expanded', 'disabled', 'hidden', 'style']
        });

        console.log('👁️ TaraSensor started, watching DOM...');
    }

    /**
     * Stop observing DOM changes.
     */
    stop() {
        if (!this.isRunning) return;

        this.isRunning = false;

        if (this.observer) {
            this.observer.disconnect();
            this.observer = null;
        }

        if (this.debounceTimer) {
            clearTimeout(this.debounceTimer);
            this.debounceTimer = null;
        }

        console.log('👁️ TaraSensor stopped');
    }

    /**
     * Process mutation records from MutationObserver.
     * Filters out noise, identifies deltas, queues for transmission.
     * @param {MutationRecord[]} mutations - Array of mutation records
     */
    handleMutations(mutations) {
        if (!this.isRunning) return;

        for (const mutation of mutations) {
            if (mutation.type === 'childList') {
                // Nodes added
                mutation.addedNodes.forEach(node => {
                    if (this.isInteractive(node)) {
                        this.registerNode(node, 'add');
                    }
                });

                // Nodes removed
                mutation.removedNodes.forEach(node => {
                    const nodeId = this.getNodeId(node);
                    if (nodeId && this.knownNodes.has(nodeId)) {
                        this.pendingDeltas.push({
                            type: 'remove',
                            id: nodeId
                        });
                        this.knownNodes.delete(nodeId);
                    }
                });
            } else if (mutation.type === 'attributes') {
                const node = mutation.target;
                if (this.isInteractive(node)) {
                    this.registerNode(node, 'update');
                }
            }
        }

        // Debounce transmission
        this.scheduleDeltaTransmission();
    }

    /**
     * Filter: Only interactive elements pass through.
     * Discards 95% of DOM (divs, spans, decorative elements).
     * @param {Node} node - DOM node to check
     * @returns {boolean} True if node is interactive
     */
    isInteractive(node) {
        if (!(node instanceof Element)) return false;

        // ══════════════════════════════════════════════════
        // VULNERABILITY FIX: Self-Observation Hazard
        // Never scan TARA's own UI overlay/container
        // ══════════════════════════════════════════════════
        if (node.closest('.tara-floating-widget, .tara-chat-bar, #tara-ui-container, [data-tara-widget]')) return false;
        if (node.getRootNode() instanceof ShadowRoot) {
            const host = node.getRootNode().host;
            if (host && host.closest('.tara-floating-widget, #tara-ui-container, [data-tara-widget]')) return false;
        }

        const tag = node.tagName.toLowerCase();

        // SVG noise - always reject
        const SVG_NOISE = new Set([
            'svg', 'path', 'rect', 'circle', 'line', 'polyline', 'polygon',
            'ellipse', 'use', 'defs', 'clippath', 'g', 'mask', 'symbol',
            'linearGradient', 'radialGradient', 'stop', 'pattern'
        ]);
        if (SVG_NOISE.has(tag)) return false;

        // Interactive tags
        const INTERACTIVE_TAGS = new Set([
            'button', 'a', 'input', 'select', 'textarea'
        ]);
        if (INTERACTIVE_TAGS.has(tag)) return true;

        // Role-based detection
        const role = node.getAttribute('role');
        if (role && ['button', 'link', 'menuitem', 'tab', 'option', 'menuitemcheckbox', 'menuitemradio'].includes(role)) {
            return true;
        }

        // Clickable headers/labels with meaningful text
        if (['h1', 'h2', 'h3', 'h4', 'label', 'th', 'summary'].includes(tag)) {
            const text = this.extractText(node);
            return text.length > 2 && text.length < 100;
        }

        // Check for onclick handler or event listeners
        if (node.onclick || node.getAttribute('onclick')) {
            return true;
        }

        return false;
    }

    /**
     * Extract stable ID for node.
     * Uses existing data-tara-id or generates new one.
     * @param {Element} node - DOM element
     * @returns {string} Stable node ID
     */
    getNodeId(node) {
        if (!(node instanceof Element)) return '';

        let id = node.getAttribute('data-tara-id');
        if (!id) {
            id = this.generateStableId(node);
            node.setAttribute('data-tara-id', id);
        }
        return id;
    }

    /**
     * Generate stable hash-based ID using DJB2 algorithm.
     * Same algorithm as current widget for consistency.
     * @param {Element} node - DOM element
     * @returns {string} Stable hash ID (e.g., "tara-abc123")
     */
    generateStableId(node) {
        const tag = node.tagName.toLowerCase();
        const text = this.extractText(node).substring(0, 30);
        const role = node.getAttribute('role') || '';
        const href = node.getAttribute('href') || '';
        const className = node.getAttribute('class') || '';

        // DJB2 hash
        const key = `${tag}|${text}|${role}|${href}|${className}`;
        let hash = 5381;
        for (let i = 0; i < key.length; i++) {
            hash = ((hash << 5) + hash) ^ key.charCodeAt(i);
        }

        return `tara-${(hash >>> 0).toString(36)}`;
    }

    /**
     * Serialize node to GraphNode format for server.
     * @param {Element} node - DOM element
     * @returns {Object} GraphNode-compatible object
     */
    serializeNode(node) {
        const rect = node.getBoundingClientRect();

        return {
            id: this.getNodeId(node),
            tag: node.tagName.toLowerCase(),
            text: this.extractText(node),
            role: node.getAttribute('role') || '',
            zone: this.classifyZone(node),
            interactive: true,
            visible: this.isVisible(node),
            rect: {
                x: Math.round(rect.left + window.scrollX),
                y: Math.round(rect.top + window.scrollY),
                w: Math.round(rect.width),
                h: Math.round(rect.height)
            },
            parent_id: this.getParentId(node),
            depth: this.getDepth(node),
            state: this.getState(node),
            aria_selected: node.getAttribute('aria-selected'),
            aria_expanded: node.getAttribute('aria-expanded'),
            timestamp: Date.now()
        };
    }

    /**
     * Queue delta for transmission.
     * @param {Element} node - DOM element
     * @param {string} deltaType - Type of delta: 'add' | 'update'
     */
    registerNode(node, deltaType) {
        const serialized = this.serializeNode(node);
        this.knownNodes.set(serialized.id, serialized);

        this.pendingDeltas.push({
            type: deltaType,
            node: serialized
        });
    }

    /**
     * Debounced delta transmission.
     * Batches changes to avoid spamming the server.
     */
    scheduleDeltaTransmission() {
        if (this.debounceTimer) {
            clearTimeout(this.debounceTimer);
        }

        this.debounceTimer = setTimeout(() => {
            this.transmitDeltas();
        }, this.config.debounceMs);
    }

    /**
     * Send batched deltas to server via WebSocket.
     */
    transmitDeltas() {
        if (this.pendingDeltas.length === 0) return;
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('TaraSensor: WebSocket not ready, dropping deltas');
            return;
        }

        // Process in batches
        while (this.pendingDeltas.length > 0) {
            const batch = this.pendingDeltas.splice(0, this.config.maxBatchSize);

            const message = {
                type: 'dom_delta',
                delta_type: 'update',
                changes: batch,
                url: window.location.href,
                timestamp: Date.now()
            };

            try {
                this.ws.send(JSON.stringify(message));
                console.debug(`📤 Sent ${batch.length} deltas`);
            } catch (err) {
                console.error('TaraSensor: Failed to send deltas', err);
                // Put back in queue for retry
                this.pendingDeltas.unshift(...batch);
                break;
            }
        }
    }

    /**
     * Full DOM scan (startup only).
     * Collects all interactive elements and sends to server.
     */
    performFullScan() {
        console.log('🔍 Performing full DOM scan...');
        const startTime = performance.now();

        const allInteractive = [];
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_ELEMENT,
            null
        );

        let node;
        while (node = walker.nextNode()) {
            if (this.isInteractive(node)) {
                const serialized = this.serializeNode(node);
                this.knownNodes.set(serialized.id, serialized);
                allInteractive.push(serialized);
            }
        }

        const message = {
            type: 'dom_delta',
            delta_type: 'full_scan',
            nodes: allInteractive,
            url: window.location.href,
            timestamp: Date.now()
        };

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(JSON.stringify(message));
                const elapsed = performance.now() - startTime;
                console.log(`📸 Full scan: ${allInteractive.length} nodes in ${elapsed.toFixed(2)}ms`);
            } catch (err) {
                console.error('TaraSensor: Failed to send full scan', err);
            }
        } else {
            console.warn('TaraSensor: WebSocket not ready for full scan');
        }
    }

    // ═══════════════════════════════════════════════════════════
    // HELPER METHODS
    // ═══════════════════════════════════════════════════════════

    /**
     * Extract text content from element.
     * Prioritizes ARIA attributes, then visible text.
     * @param {Element} el - DOM element
     * @returns {string} Extracted text
     */
    extractText(el) {
        const text = el.getAttribute('aria-label') ||
            el.getAttribute('title') ||
            el.getAttribute('placeholder') ||
            el.textContent ||
            el.value ||
            '';
        return text.trim().substring(0, 80);
    }

    /**
     * Check if element is visible.
     * @param {Element} el - DOM element
     * @returns {boolean} True if visible
     */
    isVisible(el) {
        const style = window.getComputedStyle(el);
        return style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            style.opacity !== '0';
    }

    /**
     * Classify element's zone (nav, main, modal, sidebar, etc).
     * @param {Element} el - DOM element
     * @returns {string} Zone name
     */
    classifyZone(el) {
        let node = el;
        let depth = 0;

        while (node && node instanceof Element && depth < 10) {
            const tag = node.tagName ? node.tagName.toLowerCase() : '';
            const role = node.getAttribute ? (node.getAttribute('role') || '') : '';

            if (tag === 'nav' || role === 'navigation') return 'nav';
            if (tag === 'aside') return 'sidebar';
            if (tag === 'footer') return 'footer';
            if (tag === 'header') return 'header';
            if (role === 'dialog' || role === 'alertdialog') return 'modal';
            if (tag === 'main' || role === 'main') return 'main';

            node = node.parentElement;
            depth++;
        }

        return 'main';
    }

    /**
     * Get parent node's Tara ID.
     * @param {Element} el - DOM element
     * @returns {string|null} Parent ID or null
     */
    getParentId(el) {
        let node = el.parentElement;
        while (node && node !== document.body) {
            const id = node.getAttribute('data-tara-id');
            if (id) return id;
            node = node.parentElement;
        }
        return null;
    }

    /**
     * Get DOM depth of element.
     * @param {Element} el - DOM element
     * @returns {number} Depth level (capped at 20)
     */
    getDepth(el) {
        let depth = 0;
        let node = el;
        while (node && node !== document.documentElement) {
            node = node.parentElement;
            depth++;
        }
        return Math.min(depth, 20);
    }

    /**
     * Get element state (focused, active, disabled, etc).
     * @param {Element} el - DOM element
     * @returns {string} State string
     */
    getState(el) {
        if (document.activeElement === el) return 'focused';
        if (el.getAttribute('aria-current')) return 'active';
        if (el.getAttribute('aria-selected') === 'true') return 'active';
        if (el.disabled) return 'disabled';
        if (el.hasAttribute('hidden')) return 'hidden';
        return '';
    }

    /**
     * Get current stats.
     * @returns {Object} Stats object
     */
    getStats() {
        return {
            knownNodes: this.knownNodes.size,
            pendingDeltas: this.pendingDeltas.length,
            isRunning: this.isRunning
        };
    }

    /**
     * Clear known nodes cache.
     * Use on navigation or full scan.
     */
    clearCache() {
        this.knownNodes.clear();
        this.pendingDeltas = [];
        console.log('🧹 TaraSensor cache cleared');
    }
}

// Export for integration
if (typeof window !== 'undefined') {
    window.TaraSensor = TaraSensor;
}

// Export for module systems
if (typeof module !== 'undefined' && module.exports) {
    module.exports = TaraSensor;
}
