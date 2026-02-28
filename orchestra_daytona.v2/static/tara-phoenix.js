/**
 * TARA Visual Co-Pilot — Phoenix Protocol (Session Persistence)
 * Module: tara-phoenix.js
 *
 * Owns: Session recovery across navigations, mission state persistence,
 *       beforeunload universal autosave, and session restoration logic.
 * Depends on: tara-config.js (window.TARA.Constants)
 */
(function () {
    'use strict';

    window.TARA = window.TARA || {};

    const Phoenix = {
        /**
         * Recover session ID from all available sources.
         * Returns { sessionId, isRestoredSession, wasNavigating, pendingMissionGoal }
         */
        recoverSession() {
            let recoveredSessionId = null;
            let isRestoredSession = false;
            let wasNavigating = false;
            let pendingMissionGoal = null;

            // ─── PRIORITY 1: Cross-Domain (URL Hash) ────────────────
            const hashStr = window.location.hash.substring(1);
            const hashParams = new URLSearchParams(hashStr);
            if (hashParams.has('tara_session')) {
                recoveredSessionId = hashParams.get('tara_session');
                console.log('🔥 PRIORITY 1 — Cross-Domain Phoenix:', recoveredSessionId);

                const cleanHash = window.location.hash.replace(/[&?]?tara_session=[^&]+/, '').replace(/^#$/, '');
                window.history.replaceState(null, "", window.location.pathname + window.location.search + cleanHash);

                sessionStorage.setItem('tara_session_id', recoveredSessionId);
                localStorage.setItem('tara_session_id', recoveredSessionId);
                isRestoredSession = true;
                wasNavigating = true;
                pendingMissionGoal = hashParams.get('tara_goal') || sessionStorage.getItem('tara_goal') || localStorage.getItem('tara_goal');
            }
            // Legacy tara_phoenix= format
            else if (window.location.hash.includes('tara_phoenix=')) {
                try {
                    const encodedData = window.location.hash.split('tara_phoenix=')[1];
                    const phoenixData = JSON.parse(atob(encodedData));
                    recoveredSessionId = phoenixData.sid;
                    console.log('🔥 PRIORITY 1 — Legacy Phoenix:', recoveredSessionId);
                    window.history.replaceState(null, "", window.location.pathname + window.location.search);
                    sessionStorage.setItem('tara_session_id', recoveredSessionId);
                    localStorage.setItem('tara_session_id', recoveredSessionId);
                    isRestoredSession = true;
                    wasNavigating = true;
                    if (phoenixData.goal) pendingMissionGoal = phoenixData.goal;
                } catch (e) {
                    console.error('Phoenix URL parse failed:', e);
                }
            }

            // ─── PRIORITY 2: Same-Tab (sessionStorage) ──────────────
            const isNavigating = sessionStorage.getItem('tara_is_navigating') === 'true';
            const savedSession = sessionStorage.getItem('tara_session_id') || sessionStorage.getItem('tara_active_session');

            if (!recoveredSessionId && isNavigating && savedSession) {
                recoveredSessionId = savedSession;
                console.log('🔥 PHOENIX PROTOCOL — Recovery:', recoveredSessionId);
                isRestoredSession = true;
                wasNavigating = true;
                pendingMissionGoal = sessionStorage.getItem('tara_goal') || localStorage.getItem('tara_goal');
                sessionStorage.removeItem('tara_is_navigating');
                sessionStorage.setItem('tara_session_id', recoveredSessionId);
                localStorage.setItem('tara_session_id', recoveredSessionId);
            }
            else if (!recoveredSessionId && sessionStorage.getItem('tara_session_id')) {
                recoveredSessionId = sessionStorage.getItem('tara_session_id');
                console.log('🔥 PRIORITY 2 — Same-Tab:', recoveredSessionId);
                isRestoredSession = true;
                wasNavigating = false;
            }

            // ─── PRIORITY 3: New Tab (localStorage with 5s expiry) ───
            if (!recoveredSessionId) {
                const handoffTime = localStorage.getItem('tara_handoff_time');
                if (handoffTime && (Date.now() - parseInt(handoffTime)) < 5000) {
                    recoveredSessionId = localStorage.getItem('tara_handoff_session');
                    if (recoveredSessionId) {
                        console.log('🔥 PRIORITY 3 — New Tab Handoff:', recoveredSessionId);
                        localStorage.removeItem('tara_handoff_session');
                        localStorage.removeItem('tara_handoff_time');
                        sessionStorage.setItem('tara_session_id', recoveredSessionId);
                        localStorage.setItem('tara_session_id', recoveredSessionId);
                        isRestoredSession = true;
                        wasNavigating = true;
                        pendingMissionGoal = localStorage.getItem('tara_goal');
                    }
                }
                // Fallback: plain localStorage
                else if (localStorage.getItem('tara_session_id')) {
                    recoveredSessionId = localStorage.getItem('tara_session_id');
                    console.log('🔥 FALLBACK — Existing localStorage:', recoveredSessionId);
                    isRestoredSession = true;
                    wasNavigating = true;
                    pendingMissionGoal = localStorage.getItem('tara_goal');
                }
            }

            if (!pendingMissionGoal) {
                pendingMissionGoal = sessionStorage.getItem('tara_goal') || localStorage.getItem('tara_goal');
            }

            return {
                sessionId: recoveredSessionId, isRestoredSession, wasNavigating, pendingMissionGoal,
                missionId: sessionStorage.getItem('tara_mission_id') || localStorage.getItem('tara_mission_id') || null,
                subgoalIndex: parseInt(sessionStorage.getItem('tara_subgoal_index') || localStorage.getItem('tara_subgoal_index') || '0', 10),
                stepCount: parseInt(sessionStorage.getItem('tara_step_count') || localStorage.getItem('tara_step_count') || '0', 10)
            };
        },

        /**
         * Register beforeunload handler for mission + session persistence.
         * @param {Object} widget - The TaraWidget instance (for isActive, _currentMissionGoal)
         */
        registerNavigationPersistence(widget) {
            window.addEventListener('beforeunload', () => {
                Phoenix.saveMissionState(widget);

                // 🛡️ UNIVERSAL AUTOSAVE: Protect against manual clicks & hard refreshes
                const currentSessionId = sessionStorage.getItem('tara_session_id') || localStorage.getItem('tara_session_id');
                if (currentSessionId && widget.isActive) {
                    sessionStorage.setItem('tara_is_navigating', 'true');
                    sessionStorage.setItem('tara_active_session', currentSessionId);
                    localStorage.setItem('tara_session_id', currentSessionId);
                    console.log('🛡️ Phoenix Protocol: Universal autosave triggered on unload');
                }
            });
        },

        /**
         * Save current mission state to localStorage for navigation survival.
         */
        saveMissionState(widget) {
            try {
                if (!widget.isActive) return;

                const sessionId = sessionStorage.getItem('tara_session_id') || localStorage.getItem('tara_session_id');
                if (!sessionId) return;

                const currentGoal = widget._currentMissionGoal || '';
                if (!currentGoal) return;

                const state = {
                    sessionId: sessionId,
                    goal: currentGoal,
                    url: window.location.href,
                    mode: widget.sessionMode,
                    timestamp: Date.now(),
                    missionId: sessionStorage.getItem('tara_mission_id') || localStorage.getItem('tara_mission_id') || null,
                    subgoalIndex: parseInt(sessionStorage.getItem('tara_subgoal_index') || localStorage.getItem('tara_subgoal_index') || '0', 10),
                    stepCount: parseInt(sessionStorage.getItem('tara_step_count') || localStorage.getItem('tara_step_count') || '0', 10)
                };

                localStorage.setItem(window.TARA.Constants.MISSION_STATE_KEY, JSON.stringify(state));
                console.log('💾 Sticky Agent: Saved mission state');
            } catch (e) { /* ignore */ }
        },

        /**
         * Load persisted mission state (if fresh < 5 min).
         */
        loadMissionState() {
            try {
                const raw = localStorage.getItem(window.TARA.Constants.MISSION_STATE_KEY);
                if (!raw) return null;

                const state = JSON.parse(raw);

                if (Date.now() - state.timestamp > 300000) {
                    console.log('🗑️ Sticky Agent: Saved state is stale (>5min), discarding');
                    localStorage.removeItem(window.TARA.Constants.MISSION_STATE_KEY);
                    return null;
                }

                return state;
            } catch (e) {
                return null;
            }
        },

        /**
         * Clear persisted mission state.
         */
        clearMissionState() {
            try {
                localStorage.removeItem(window.TARA.Constants.MISSION_STATE_KEY);
            } catch (e) { /* ignore */ }
        },

        /**
         * Plant session seeds across all storage layers before a click/action.
         * Called by the executor before clicks and type_text.
         * @param {string} currentMissionGoal
         * @param {string} pendingMissionGoal
         */
        plantSessionSeeds(currentMissionGoal, pendingMissionGoal) {
            const currentSessionId = sessionStorage.getItem('tara_session_id') || localStorage.getItem('tara_session_id');
            if (!currentSessionId) return;

            sessionStorage.setItem('tara_active_session', currentSessionId);
            sessionStorage.setItem('tara_session_id', currentSessionId);
            localStorage.setItem('tara_session_id', currentSessionId);
            sessionStorage.setItem('tara_is_navigating', 'true');

            if (currentMissionGoal || pendingMissionGoal) {
                const goal = currentMissionGoal || pendingMissionGoal;
                sessionStorage.setItem('tara_goal', goal);
                localStorage.setItem('tara_goal', goal);
            }

            // Persist mission context for resume
            const missionId = sessionStorage.getItem('tara_mission_id');
            if (missionId) {
                localStorage.setItem('tara_mission_id', missionId);
            }
            const subgoalIdx = sessionStorage.getItem('tara_subgoal_index');
            if (subgoalIdx) {
                localStorage.setItem('tara_subgoal_index', subgoalIdx);
            }
            const stepCount = sessionStorage.getItem('tara_step_count');
            if (stepCount) {
                localStorage.setItem('tara_step_count', stepCount);
            }
        },

        /**
         * Plant cross-domain session into a link's URL hash.
         */
        injectCrossDomainSession(element) {
            const currentSessionId = sessionStorage.getItem('tara_session_id') || localStorage.getItem('tara_session_id');
            if (!currentSessionId) return;

            // Layer 2: New tab survival
            localStorage.setItem('tara_handoff_session', currentSessionId);
            localStorage.setItem('tara_handoff_time', Date.now().toString());

            // Layer 3: Cross-domain URL injection
            const isLink = element.tagName.toLowerCase() === 'a';
            if (isLink && element.href) {
                try {
                    let targetUrl = new URL(element.href, window.location.href);
                    if (targetUrl.origin !== window.location.origin) {
                        targetUrl.hash = targetUrl.hash
                            ? targetUrl.hash + `&tara_session=${currentSessionId}`
                            : `#tara_session=${currentSessionId}`;
                        element.href = targetUrl.toString();
                        console.log(`🛡️ Layer 3: Injected session into cross-domain URL`);
                    }
                } catch (e) {
                    console.warn('Could not inject session into URL');
                }
            }
        },

        /**
         * Clean up all session data (called on stop).
         */
        clearAllSessionData() {
            sessionStorage.removeItem('tara_mode');
            sessionStorage.removeItem('tara_session_id');
            sessionStorage.removeItem('tara_active_session');
            sessionStorage.removeItem('tara_is_navigating');
            sessionStorage.removeItem('tara_interaction_mode');
            sessionStorage.removeItem('tara_goal');
            sessionStorage.removeItem('tara_mission_id');
            sessionStorage.removeItem('tara_subgoal_index');
            sessionStorage.removeItem('tara_step_count');

            localStorage.removeItem('tara_session_id');
            localStorage.removeItem('tara_handoff_session');
            localStorage.removeItem('tara_mode');
            localStorage.removeItem('tara_interaction_mode');
            localStorage.removeItem('tara_goal');
            localStorage.removeItem('tara_mission_id');
            localStorage.removeItem('tara_subgoal_index');
            localStorage.removeItem('tara_step_count');

            Phoenix.clearMissionState();
        }
    };

    window.TARA.Phoenix = Phoenix;
    console.log('✅ [TARA] Phoenix module loaded');
})();
