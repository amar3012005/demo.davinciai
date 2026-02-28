/**
 * TARA Visual Co-Pilot — Ghost Cursor
 * Module: tara-ghost-cursor.js
 *
 * Animated cursor overlay that visually shows where TARA is clicking.
 * Depends on: nothing (standalone)
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    class GhostCursor {
        constructor(shadowRoot) {
            this.cursor = document.createElement('div');
            this.cursor.className = 'tara-ghost-cursor';
            this.cursor.innerHTML = `
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
          <path d="M5.5 3.21V20.8c0 .45.54.67.85.35l4.86-4.86a.5.5 0 0 1 .35-.15h6.87c.44 0 .66-.53.35-.85L6.35 2.86a.5.5 0 0 0-.85.35z" 
                fill="white" stroke="#333" stroke-width="1.5"/>
        </svg>
      `;
            this.currentX = window.innerWidth / 2;
            this.currentY = window.innerHeight / 2;

            shadowRoot.appendChild(this.cursor);
            this.hide();
        }

        async moveTo(element, duration = 500) {
            const rect = element.getBoundingClientRect();
            const targetX = rect.left + rect.width / 2 - 12;
            const targetY = rect.top + rect.height / 2 - 12;

            this.show();

            const startX = this.currentX;
            const startY = this.currentY;
            const startTime = performance.now();

            return new Promise((resolve) => {
                const animate = (currentTime) => {
                    const elapsed = currentTime - startTime;
                    const progress = Math.min(elapsed / duration, 1);
                    const easeOut = 1 - Math.pow(1 - progress, 3);

                    this.currentX = startX + (targetX - startX) * easeOut;
                    this.currentY = startY + (targetY - startY) * easeOut;

                    this.cursor.style.transform = `translate(${this.currentX}px, ${this.currentY}px)`;

                    if (progress < 1) {
                        requestAnimationFrame(animate);
                    } else {
                        resolve();
                    }
                };

                requestAnimationFrame(animate);
            });
        }

        async click() {
            const originalTransform = this.cursor.style.transform;
            this.cursor.style.transform = `${originalTransform} scale(0.8)`;
            await new Promise(r => setTimeout(r, 150));
            this.cursor.style.transform = originalTransform;
        }

        show() {
            this.cursor.style.opacity = '1';
        }

        hide() {
            this.cursor.style.opacity = '0';
        }
    }

    window.TARA.GhostCursor = GhostCursor;
    console.log('✅ [TARA] GhostCursor module loaded');
})();
