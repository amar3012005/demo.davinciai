/**
 * TARA Visual Co-Pilot — Action Executor
 * Module: tara-executor.js
 *
 * Owns: All command execution — click, type_text, scroll, highlight, etc.
 *       Also owns element finding and robust scroll strategies.
 * Depends on: tara-phoenix.js, tara-scanner.js, tara-ghost-cursor.js
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    /**
     * Create an Executor bound to a widget instance.
     * @param {Object} widget - The TaraWidget instance
     * @returns {Object} Executor methods
     */
    function createExecutor(widget) {
        const Phoenix = window.TARA.Phoenix;
        const Scanner = window.TARA.Scanner;

        const Executor = {
            /**
             * Execute a command dispatched by the backend.
             */
            async executeCommand(type, targetId, text) {
                widget.setOrbState('executing');

                try {
                    if (type === 'wait') {
                        console.log("⏳ TARA Waiting (as requested)...");
                        await new Promise(r => setTimeout(r, 2000));
                    }
                    else if (type === 'click') {
                        const el = Executor.findElement(targetId, text);
                        if (el) {
                            await widget.ghostCursor.moveTo(el);
                            await widget.ghostCursor.click();

                            // Robust click strategy
                            const opts = { bubbles: true, cancelable: true, view: window };
                            el.dispatchEvent(new MouseEvent('mousedown', opts));
                            el.dispatchEvent(new MouseEvent('mouseup', opts));
                            el.dispatchEvent(new MouseEvent('click', opts));

                            if (typeof el.click === 'function') el.click();
                            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') el.focus();

                            // Phoenix Protocol: Protect against click navigation
                            Phoenix.plantSessionSeeds(widget._currentMissionGoal, widget.pendingMissionGoal);
                            Phoenix.injectCrossDomainSession(el);

                            const currentSessionId = sessionStorage.getItem('tara_session_id') || localStorage.getItem('tara_session_id');
                            sessionStorage.setItem('tara_is_navigating', 'true');

                            widget.isNavigating = true;
                            await new Promise(resolve => {
                                setTimeout(() => {
                                    widget.isNavigating = false;
                                    resolve();
                                }, 2000);
                            });

                            // 🛡️ SPA Safety Net: If the page didn't hard reload after 2 seconds,
                            // the flag is still set — clear it so the WS handler can send execution_complete
                            if (sessionStorage.getItem('tara_is_navigating') === 'true') {
                                console.log("📡 No hard navigation detected after click. Clearing flag for WS handler...");
                                sessionStorage.removeItem('tara_is_navigating');
                            }
                        }
                    }
                    else if (type === 'type_text') {
                        const el = Executor.findElement(targetId);
                        if (el) {
                            await widget.ghostCursor.moveTo(el);
                            el.focus();

                            // Support React/controlled components
                            let setter;
                            if (el.tagName === 'TEXTAREA') {
                                setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                            } else {
                                setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                            }

                            if (setter) {
                                try { setter.call(el, text); } catch (e) { el.value = text; }
                            } else {
                                el.value = text;
                            }

                            el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));

                            // Phoenix Protocol: Protect against Enter key navigation
                            Phoenix.plantSessionSeeds(widget._currentMissionGoal, widget.pendingMissionGoal);
                            const currentSessionId = sessionStorage.getItem('tara_session_id') || localStorage.getItem('tara_session_id');
                            sessionStorage.setItem('tara_is_navigating', 'true');

                            // Fire Enter key
                            const enterOpts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true };
                            el.dispatchEvent(new KeyboardEvent('keydown', enterOpts));
                            el.dispatchEvent(new KeyboardEvent('keypress', enterOpts));
                            el.dispatchEvent(new KeyboardEvent('keyup', enterOpts));

                            // Form submit fallback
                            if (el.form) {
                                setTimeout(() => {
                                    try {
                                        if (typeof el.form.requestSubmit === 'function') {
                                            el.form.requestSubmit();
                                        } else {
                                            el.form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                                        }
                                    } catch (e) {
                                        console.warn('Tara: Form requestSubmit failed', e);
                                        el.form.submit();
                                    }
                                }, 100);
                            }

                            widget.isNavigating = true;
                            // Wait a bit to block execution_complete, just like click
                            await new Promise(resolve => {
                                setTimeout(() => {
                                    widget.isNavigating = false;
                                    resolve();
                                }, 2000);
                            });

                            // Safety net: if no navigation happened, clear flag for WS handler
                            setTimeout(() => {
                                if (sessionStorage.getItem('tara_is_navigating') === 'true') {
                                    console.log("📡 No navigation detected after typing. Clearing flag for WS handler...");
                                    sessionStorage.removeItem('tara_is_navigating');
                                }
                            }, 1500);
                        }
                    }
                    else if (type === 'scroll_to') {
                        const el = Executor.findElement(targetId, text);
                        if (el) {
                            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            Executor.highlightElement(el);
                        } else {
                            Executor.robustScroll(1, window.innerHeight * 0.5);
                        }
                    }
                    else if (type === 'scroll') {
                        const direction = (text && text.includes('up')) ? -1 : 1;
                        Executor.robustScroll(direction);

                        // Visual feedback flash
                        const flash = document.createElement('div');
                        flash.style.cssText = `
                            position: fixed; top: 0; ${direction > 0 ? 'bottom: 0' : 'top: 0'}; 
                            right: 0; width: 6px; background: rgba(59, 130, 246, 0.5); z-index: 10000;
                            pointer-events: none; transition: opacity 0.5s; opacity: 1;
                        `;
                        document.body.appendChild(flash);
                        setTimeout(() => { flash.style.opacity = '0'; setTimeout(() => flash.remove(), 500); }, 200);
                    }
                    else if (type === 'highlight') {
                        Executor.executeHighlight(targetId, text);
                    }
                    else if (type === 'spotlight') {
                        if (widget.spotlight) {
                            widget.spotlight.classList.add('active');
                            setTimeout(() => widget.spotlight.classList.remove('active'), 3000);
                        }
                    }
                    else if (type === 'clear') {
                        Executor.clearHighlights();
                    }

                    // Adaptive DOM settle
                    const settleTime = (type === 'scroll' || type === 'scroll_to') ? 800 : 300;
                    await Scanner.waitForDOMSettle(3000, settleTime);

                } catch (err) {
                    console.warn("Execution partial error:", err);
                }
            },

            /**
             * Standalone click with full Phoenix session planting.
             */
            async executeClick(targetId) {
                const element = Executor.findElement(targetId);
                if (!element) {
                    console.warn(`⚠️ Element NOT found: ${targetId}`);
                    return;
                }

                const currentSessionId = sessionStorage.getItem('tara_session_id') || localStorage.getItem('tara_session_id');
                if (currentSessionId) {
                    Phoenix.plantSessionSeeds(widget._currentMissionGoal, widget.pendingMissionGoal);
                    Phoenix.injectCrossDomainSession(element);
                    console.log(`🛡️ Session planted in all layers: ${currentSessionId}`);
                }

                await widget.ghostCursor.moveTo(element, 600);
                await new Promise(r => setTimeout(r, 300));
                await widget.ghostCursor.click();
                await new Promise(r => setTimeout(r, 100));

                element.click();
                setTimeout(() => widget.ghostCursor.hide(), 500);
                console.log(`👆 Clicked: ${targetId}`);
            },

            /**
             * Robust element finder with multiple fallback strategies.
             */
            findElement(targetId, fallbackText = null) {
                if (!targetId) return null;

                // Strategy 1: Exact ID
                let element = document.getElementById(targetId);
                if (element) return element;

                // Strategy 2: data-tara-id
                element = document.querySelector(`[data-tara-id="${targetId}"]`);
                if (element) return element;

                // Strategy 3: Name or Test ID
                element = document.querySelector(`[name="${targetId}"]`) ||
                    document.querySelector(`[data-testid="${targetId}"]`);
                if (element) return element;

                // Strategy 4: Fallback text search
                if (fallbackText) {
                    console.warn(`⚠️ Target ID "${targetId}" not found. Trying text: "${fallbackText}"`);
                    const allInteractive = document.querySelectorAll('button, a, [role="button"], h1, h2, h3, h4, span, div');
                    for (const el of allInteractive) {
                        const elText = Scanner.extractText(el).toLowerCase().trim();
                        const targetText = fallbackText.toLowerCase().trim();
                        if (elText === targetText || (targetText.length > 5 && elText.includes(targetText))) {
                            console.log(`✅ Text fallback found element:`, el);
                            return el;
                        }
                    }
                }

                // Strategy 5: ID string in text
                console.warn(`⚠️ Target ID "${targetId}" not found. Trying text fallback...`);
                const allInteractive = document.querySelectorAll('button, a, [role="button"]');
                for (const el of allInteractive) {
                    if (Scanner.extractText(el).toLowerCase().includes(targetId.toLowerCase())) {
                        console.log(`✅ Text fallback found element:`, el);
                        return el;
                    }
                }

                return null;
            },

            executeScroll(targetId) {
                const element = Executor.findElement(targetId);
                if (element) {
                    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    console.log(`📜 Scrolled to: ${targetId}`);
                } else {
                    console.warn(`⚠️ Scroll target not found: ${targetId}`);
                }
            },

            executeHighlight(targetId, text) {
                const element = document.getElementById(targetId);
                if (!element) return;

                const rect = element.getBoundingClientRect();
                const highlight = document.createElement('div');
                highlight.className = 'tara-highlight';
                highlight.style.cssText = `
                    top: ${rect.top - 4}px;
                    left: ${rect.left - 4}px;
                    width: ${rect.width + 8}px;
                    height: ${rect.height + 8}px;
                `;

                if (widget.highlightContainer) {
                    widget.highlightContainer.appendChild(highlight);
                }
                setTimeout(() => highlight.remove(), 3000);
                console.log(`✨ Highlighted: ${targetId}`);
            },

            highlightElement(el) {
                // Brief visual confirmation
                if (!el) return;
                const rect = el.getBoundingClientRect();
                const highlight = document.createElement('div');
                highlight.className = 'tara-highlight';
                highlight.style.cssText = `
                    top: ${rect.top - 4}px;
                    left: ${rect.left - 4}px;
                    width: ${rect.width + 8}px;
                    height: ${rect.height + 8}px;
                `;
                if (widget.highlightContainer) {
                    widget.highlightContainer.appendChild(highlight);
                }
                setTimeout(() => highlight.remove(), 2000);
            },

            clearHighlights() {
                if (widget.highlightContainer) {
                    widget.highlightContainer.innerHTML = '';
                }
            },

            robustScroll(direction = 1, amount = null) {
                if (!amount) amount = window.innerHeight * 0.7;
                const top = amount * direction;

                console.log(`📜 Robust Scroll: direction=${direction}, amount=${amount}`);

                window.scrollBy({ top, behavior: 'smooth' });

                const containerSelectors = [
                    'main', 'section', '#content', '.content',
                    '[role="main"]', '.overflow-y-auto', '.overflow-auto', '.main-content'
                ];

                let containerScrolled = false;
                containerSelectors.forEach(selector => {
                    const containers = document.querySelectorAll(selector);
                    containers.forEach(c => {
                        if (c.scrollHeight > c.clientHeight) {
                            c.scrollBy({ top, behavior: 'smooth' });
                            containerScrolled = true;
                        }
                    });
                });

                if (!containerScrolled) {
                    const all = document.querySelectorAll('div, aside, article');
                    for (const el of all) {
                        const style = window.getComputedStyle(el);
                        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
                            el.scrollBy({ top, behavior: 'smooth' });
                            console.log("📜 Found ad-hoc scroller:", el);
                            break;
                        }
                    }
                }
            }
        };

        return Executor;
    }

    window.TARA.createExecutor = createExecutor;
    console.log('✅ [TARA] Executor module loaded');
})();
